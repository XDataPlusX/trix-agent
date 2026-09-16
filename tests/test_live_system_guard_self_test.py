"""Self-test for the live-system guard fixture in tests/conftest.py.

This file is the canary. If anyone removes a guard or weakens it, these
tests fail. If anyone adds a NEW kill primitive to the codebase without
adding it to the guard, the corresponding test added here will fail too.

The guard exists to protect the developer's live ``hermes-gateway`` process
from being SIGTERMed by tests. See PR #23397 for the original incident
(5+ live gateway kills in 3 days). Per Teknium 2026-05-10:

  > "You better do such a deep scan and scrub of the tests that this
  >  never is possible ever again for all eternity."

Every primitive that can deliver a signal to a foreign process or mutate
the live systemd unit MUST be exercised below. Adding a new primitive to
the guard? Add a test here too.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import types
from pathlib import Path

import pytest

# A guaranteed-foreign PID: PID 1 (init).  Owned by root, not us, and
# always exists. A sane guard refuses to signal it.
FOREIGN_PID = 1

# Same computation tests/conftest.py uses for PROJECT_ROOT: this file lives
# in tests/, so its grandparent is the repo root the npm-install guard
# protects.
REPO_ROOT = Path(__file__).resolve().parent.parent


# ──────────────────── fail-closed self-protection ──────────────
#
# This file executes REAL kill primitives — os.kill(-1, SIGTERM), os.killpg,
# pkill -f python — and depends entirely on the autouse ``_live_system_guard``
# fixture in tests/conftest.py to intercept them. That makes the canary
# fail-OPEN: in any collection context where this file is present but its home
# conftest is not, the primitives fire for real and ``os.kill(-1, SIGTERM)``
# SIGTERMs every process the invoking user owns (a full desktop-session kill was
# reported in the field — see issue #68311). Such contexts are not exotic:
# published sdists that ship ``tests/`` but not ``tests/conftest.py``, trees
# assembled by copying ``test*.py`` files (that glob does NOT match
# ``conftest.py``), ``pytest --noconftest``, or running from a foreign rootdir.
#
# The fixture below makes the canary fail-CLOSED instead: it refuses to run any
# test in this file unless the guard is provably active, so no collection
# context can ever detonate the primitives. The one thing the canary can detect
# about its own safety is that the guard monkeypatches ``os.kill`` with a plain
# Python function, whereas the unguarded primitive is a C builtin.


def _live_system_guard_is_active() -> bool:
    """True iff tests/conftest.py's ``_live_system_guard`` has patched os.kill.

    The guard replaces ``os.kill`` with a plain Python function; the raw,
    unguarded primitive is a C builtin (``types.BuiltinFunctionType``). If
    ``os.kill`` is still the builtin, the guard never loaded and every kill
    primitive in this file would fire for real.
    """
    return not isinstance(os.kill, types.BuiltinFunctionType)


@pytest.fixture(autouse=True)
def _refuse_to_fire_live_weapons(request):
    """Fail closed: refuse to run a canary test unless the guard is active.

    Tests genuinely marked ``@pytest.mark.live_system_guard_bypass`` opt out
    (they run the raw primitive deliberately and harmlessly, e.g. a signal-0
    liveness probe of our own PID), matching the guard's own bypass contract.
    """
    if request.node.get_closest_marker("live_system_guard_bypass"):
        yield
        return
    if not _live_system_guard_is_active():
        pytest.fail(
            "REFUSING TO RUN: the live-system guard from tests/conftest.py is "
            "not active in this interpreter (os.kill is still the raw C "
            "builtin). This canary file executes real kill primitives — "
            "os.kill(-1, SIGTERM), os.killpg, pkill -f python — and relies on "
            "the guard to intercept them; unguarded, they SIGTERM every process "
            "the current user owns. This usually means the file was collected "
            "without its home tests/conftest.py (note: a test*.py copy glob "
            "does NOT match conftest.py). See issue #68311.",
            pytrace=False,
        )
    yield


def test_fail_closed_probe_reports_guard_active():
    """In the real suite the guard is loaded, so the probe reports active and
    ``_refuse_to_fire_live_weapons`` stays out of the way (no false positives
    that would wedge CI)."""
    assert _live_system_guard_is_active() is True


def test_fail_closed_probe_classifies_raw_builtin_as_unguarded():
    """The probe's discriminator, exercised against real objects: a raw C
    builtin the guard never touches (``os.getpid``) is exactly what an
    unguarded ``os.kill`` looks like and must read as 'guard not active', while
    the loaded guard's ``os.kill`` is a plain Python function."""
    assert isinstance(os.getpid, types.BuiltinFunctionType)
    assert not isinstance(os.kill, types.BuiltinFunctionType)


# ──────────────────── kill primitives ─────────────────────────


def test_os_kill_blocks_foreign_pid():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.kill(FOREIGN_PID, signal.SIGTERM)


def test_os_kill_blocks_negative_one():
    """``os.kill(-1, sig)`` signals every process we can reach. Must be blocked."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.kill(-1, signal.SIGTERM)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="killpg POSIX-only")
def test_os_killpg_blocks_foreign_pgid():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.killpg(FOREIGN_PID, signal.SIGTERM)


# ──────────────────── subprocess regex bypasses ────────────────


def test_subprocess_run_systemctl_restart_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_run_full_path_systemctl_blocked():
    """``/usr/bin/systemctl`` (full path) must be blocked too."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["/usr/bin/systemctl", "--user", "stop", "hermes-gateway"])


def test_subprocess_run_sudo_systemctl_blocked():
    """``sudo systemctl ...`` defeated the old head==systemctl check."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["sudo", "systemctl", "restart", "hermes-gateway"])


def test_subprocess_run_env_systemctl_blocked():
    """``env systemctl ...`` similarly defeated the old head check."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["env", "systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_run_bash_c_systemctl_blocked():
    """``bash -c "systemctl ..."`` must also be caught."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["bash", "-c", "systemctl --user restart hermes-gateway"])


def test_subprocess_run_sh_c_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["sh", "-c", "systemctl --user stop hermes-gateway"])


def test_subprocess_run_setsid_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["setsid", "systemctl", "kill", "hermes-gateway"])


def test_subprocess_run_string_shell_true_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(
            "systemctl --user restart hermes-gateway",
            shell=True,
        )


def test_subprocess_popen_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(["systemctl", "--user", "stop", "hermes-gateway"])


def test_subprocess_call_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.call(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_check_call_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.check_call(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_check_output_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.check_output(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_getoutput_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.getoutput("systemctl --user restart hermes-gateway")


def test_subprocess_getstatusoutput_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.getstatusoutput("systemctl --user restart hermes-gateway")


# ──────────────────── os.system / os.popen ────────────────────


def test_os_system_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.system("systemctl --user restart hermes-gateway")


def test_os_popen_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.popen("systemctl --user restart hermes-gateway")


# ──────────────────── pty.spawn ────────────────────────────────


def test_pty_spawn_systemctl_blocked():
    import pty
    with pytest.raises(RuntimeError, match="live-system guard"):
        pty.spawn(["systemctl", "--user", "restart", "hermes-gateway"])


# ──────────────────── asyncio.create_subprocess_* ──────────────


def test_asyncio_create_subprocess_exec_systemctl_blocked():
    import asyncio

    async def _attempt():
        await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", "hermes-gateway"
        )

    with pytest.raises(RuntimeError, match="live-system guard"):
        asyncio.run(_attempt())


def test_asyncio_create_subprocess_shell_systemctl_blocked():
    import asyncio

    async def _attempt():
        await asyncio.create_subprocess_shell(
            "systemctl --user restart hermes-gateway"
        )

    with pytest.raises(RuntimeError, match="live-system guard"):
        asyncio.run(_attempt())


# ──────────────────── pkill / killall / taskkill ───────────────


def test_subprocess_pkill_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "hermes"])


def test_subprocess_pkill_hermes_gateway_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "hermes-gateway"])


def test_subprocess_pkill_python_dash_f_blocked():
    """``pkill -f python`` matches the gateway's "python -m hermes_cli.main"."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "python"])


def test_subprocess_killall_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["killall", "hermes"])


# ──────────────────── npm install guard ─────────────────────────


def test_subprocess_npm_install_against_repo_root_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["npm", "install"], cwd=str(REPO_ROOT))


def test_subprocess_npm_ci_against_repo_root_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["npm", "ci"], cwd=str(REPO_ROOT))


def test_subprocess_npm_i_against_repo_root_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["npm", "i"], cwd=str(REPO_ROOT))


def test_subprocess_npm_install_absolute_path_blocked():
    """The real diagnosed call site (``find_node_executable("npm")``) passes
    an absolute path, not a bare ``"npm"``."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["/usr/local/bin/npm", "install"], cwd=str(REPO_ROOT))


def test_subprocess_npm_cmd_windows_path_blocked():
    """Windows' ``find_node_executable("npm")`` returns a ``.cmd`` shim path
    with backslash separators, as an argv element — not a shell string.
    Must be detected without joining argv into one string and re-splitting
    it (that eats backslashes: ``C:\\nodejs\\npm.cmd`` -> ``C:nodejsnpm.cmd``)."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["C:\\nodejs\\npm.cmd", "install"], cwd=str(REPO_ROOT))


def test_subprocess_npm_cmd_windows_path_with_space_blocked():
    """A path containing a space (``Program Files``) must not be split into
    separate tokens by a join-then-shlex-split round trip."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(
            ["C:\\Program Files\\nodejs\\npm.cmd", "install"], cwd=str(REPO_ROOT)
        )


def test_subprocess_bash_c_npm_install_blocked():
    """``bash -c "cd /tmp && npm install"`` — the guard can't statically
    resolve the wrapped shell's effective cwd (it could ``cd`` anywhere), so
    it stays blocked whenever the OUTER subprocess cwd is the repo root,
    regardless of the inner ``cd``. Blocking is the deliberately safe
    direction here, not an oversight."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["bash", "-c", "cd /tmp && npm install"], cwd=str(REPO_ROOT))


def test_npm_install_against_scratch_dir_not_blocked(tmp_path):
    """Same command, but cwd is a scratch dir, not the tracked repo — the
    guard protects the real checkout, not npm itself, so this must run for
    real (and fail on its own terms: no package.json in the empty dir)."""
    result = subprocess.run(
        ["npm", "install"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode != 0


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_npm_version_not_blocked():
    """``npm --version`` has no install-shaped verb at all."""
    result = subprocess.run(
        ["npm", "--version"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_npm_run_build_not_blocked():
    """``npm run build`` runs a script named "build" — not an install."""
    result = subprocess.run(
        ["npm", "run", "build"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert isinstance(result, subprocess.CompletedProcess)


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_npm_run_ci_not_blocked():
    """``npm run ci`` is a script literally named "ci" — distinct from the
    install verb ``npm ci``."""
    result = subprocess.run(
        ["npm", "run", "ci"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert isinstance(result, subprocess.CompletedProcess)


def test_git_commit_mentioning_npm_install_not_blocked(tmp_path):
    """A commit message that merely CONTAINS the words "npm install" must
    not be mistaken for actually running npm. Run against a non-repo
    scratch dir so the real (guaranteed-to-fail-fast) git invocation is
    harmless."""
    result = subprocess.run(
        ["git", "commit", "-m", "fix npm install flakiness"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode != 0


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx not installed")
def test_npx_not_blocked():
    """``npx`` is a different executable from ``npm`` and must not match."""
    result = subprocess.run(
        ["npx", "--version"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0


# ──────────────────── pass-through cases (must NOT raise) ──────
















# ──────────────────── bypass marker ─────────────────────────────


@pytest.mark.live_system_guard_bypass
def test_bypass_marker_disables_guard():
    """The bypass marker exists for tests that genuinely need real signal delivery
    (e.g. PTY tests SIGINTing their own child). Verify it works.

    We use it harmlessly here by signaling our own PID 0 (own group) so we
    don't actually kill anything — but the call goes through real os.kill.
    """
    # With bypass, the guard yields without installing the monkeypatch,
    # so we get the real os.kill. Calling os.kill(os.getpid(), 0) just
    # checks that the PID exists — harmless.
    os.kill(os.getpid(), 0)  # No exception — guard is OFF.
