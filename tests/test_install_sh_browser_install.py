"""Regression tests for install.sh browser setup.

Browser automation is optional. The installer should not leave Hermes
half-installed just because Playwright's managed Chromium download hangs on an
unsupported distribution.

Every test in this file runs real shell functions (or, for the CLI argument
parser, the real top-level parsing loop) extracted out of install.sh with
`sed` INSIDE the bash subprocess under test, then drives them with stubbed
external commands and asserts on the resulting behavior -- process exit
codes, recorded command invocations, wall-clock time, real stdout. install.sh's
own text never enters Python: only its path is handed to `sed`. This is the
same technique as test_install_sh_bootstrap_marker.py,
test_install_sh_strip_snap_browser_override.py and
test_install_sh_origin_repoint.py. No assertion here is a substring/regex
check against the script's source -- see CLAUDE.md, "Never read source code
in tests".
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _extract(*names: str) -> str:
    """Bash snippet sourcing exactly these function bodies out of install.sh.

    One `eval "$(sed -n '/^name()/,/^}/p' install.sh)"` line per name. The
    sed range starts at the function's own `name() {` line and stops at the
    first UNINDENTED `}` after it -- which is always the function's own
    closing brace, even for functions (playwright_host_unrecognized) that
    define an indented nested helper function of their own.
    """
    lines = [
        "eval \"$(sed -n '/^" + name + "()/,/^}/p' " + repr(str(INSTALL_SH)) + ")\""
        for name in names
    ]
    return "\n".join(lines)


def _extract_argv_parser() -> str:
    """Bash snippet sourcing install.sh's real `while [[ $# -gt 0 ]]; do ...
    esac; done` CLI argument loop (top-level code, not a function -- so it is
    pulled out by a stable comment-anchored line range instead of a function
    name). Running it for real against real argv is how
    test_skip_browser_flag_parsing_prevents_playwright_install proves
    --skip-browser/--no-playwright actually flow through to skip the
    install, rather than asserting the case-pattern text exists.
    """
    return (
        "eval \"$(sed -n '/^# Parse arguments$/,/^done$/p' "
        + repr(str(INSTALL_SH))
        + ")\""
    )


def _final_rc(stdout: str) -> int | None:
    for line in stdout.splitlines():
        if line.startswith("FINAL_RC="):
            return int(line.split("=", 1)[1])
    return None


# ---------------------------------------------------------------------------
# find_system_browser(): must honor ONLY an explicit AGENT_BROWSER_EXECUTABLE_
# PATH override -- no PATH/well-known-location scanning. Auto-detection used
# to silently bind the install to whatever `command -v chromium` resolved to,
# most damagingly a Snap Chromium whose sandbox blocks agent-browser's control
# socket ("opening web page failed"). Drives the real function with a fake
# PATH that DOES contain a `chromium` binary, to prove it is never consulted.
# ---------------------------------------------------------------------------


def _run_find_system_browser(*, override: str, path_dirs: list[str]) -> dict:
    # Extraction happens BEFORE PATH is narrowed to path_dirs -- `sed` (used
    # by _extract's eval line) must still be reachable at that point. Once
    # find_system_browser() is defined, PATH is free to shrink to exactly
    # what the test wants find_system_browser to see.
    script = f"""
set -u
{_extract("find_system_browser")}

export PATH={":".join(path_dirs)!r}
AGENT_BROWSER_EXECUTABLE_PATH={override!r}
[ -z "$AGENT_BROWSER_EXECUTABLE_PATH" ] && unset AGENT_BROWSER_EXECUTABLE_PATH

out="$(find_system_browser)"
echo "RC=$?"
echo "OUT=$out"
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    rc = None
    out = None
    for line in proc.stdout.splitlines():
        if line.startswith("RC="):
            rc = int(line.split("=", 1)[1])
        elif line.startswith("OUT="):
            out = line.split("=", 1)[1]
    return {"rc": rc, "out": out, "stdout": proc.stdout, "stderr": proc.stderr}


def _run_find_system_browser_with_snap_on_path(*, override: str) -> dict:
    """Same function, but with ``command -v`` resolving to a Snap wrapper.

    A real ``/snap/...`` file cannot be created in the test sandbox (``/`` is
    read-only and this needs the literal absolute prefix), so the resolver is
    stubbed instead — the same technique this file already uses for ``uname``
    and ``npx``. What is stubbed is an external lookup, not the function under
    test: ``find_system_browser`` runs for real and its own guard decides.
    """
    script = f"""
set -u
{_extract("find_system_browser")}

# On a Snap box `command -v chromium` prints /snap/bin/chromium. Reproduce
# exactly that, and nothing else about the environment.
command() {{
    if [ "$1" = "-v" ] && [ "$2" = "chromium" ]; then
        echo /snap/bin/chromium
        return 0
    fi
    builtin command "$@"
}}

AGENT_BROWSER_EXECUTABLE_PATH={override!r}
out="$(find_system_browser)"
echo "RC=$?"
echo "OUT=$out"
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    rc = out = None
    for line in proc.stdout.splitlines():
        if line.startswith("RC="):
            rc = int(line.split("=", 1)[1])
        elif line.startswith("OUT="):
            out = line.split("=", 1)[1]
    return {"rc": rc, "out": out, "stdout": proc.stdout, "stderr": proc.stderr}


def test_bare_name_resolving_to_snap_is_refused() -> None:
    """The guard has to judge where the name LANDS, not how it was spelled.

    The absolute form (``AGENT_BROWSER_EXECUTABLE_PATH=/snap/bin/chromium``)
    was always refused. A bare ``chromium`` was not: it does not start with
    ``/snap/``, so the guard never fired and the function returned the Snap
    wrapper — the binary whose confinement breaks every ``browser_*`` call and
    the only reason the guard exists. Found while removing this file's
    source-reading tests, 2026-09-04.
    """
    r = _run_find_system_browser_with_snap_on_path(override="chromium")

    assert r["rc"] == 1, r["stdout"]
    assert not r["out"], f"вернул snap-путь: {r['out']!r}"


def test_find_system_browser_ignores_path_and_only_honors_explicit_override(tmp_path: Path) -> None:
    fake_chromium_dir = tmp_path / "fakepath"
    fake_chromium_dir.mkdir()
    fake_chromium = fake_chromium_dir / "chromium"
    fake_chromium.write_text("#!/bin/sh\nexit 0\n")
    fake_chromium.chmod(0o755)

    # No override set, but a `chromium` binary IS reachable on PATH: the
    # function must still report nothing found -- it never scans PATH for
    # well-known browser names.
    r = _run_find_system_browser(override="", path_dirs=[str(fake_chromium_dir)])
    assert r["rc"] == 1, r
    assert r["out"] == "", r


def test_find_system_browser_accepts_explicit_absolute_override(tmp_path: Path) -> None:
    override_bin = tmp_path / "mybrowser"
    override_bin.write_text("#!/bin/sh\nexit 0\n")
    override_bin.chmod(0o755)

    r = _run_find_system_browser(override=str(override_bin), path_dirs=["/usr/bin", "/bin"])
    assert r["rc"] == 0, r
    assert r["out"] == str(override_bin), r


# NOTE: find_system_browser() also rejects an explicit /snap/* override
# outright (the Snap Chromium confinement is the exact bug the whole
# override-only design exists to stop rebinding to). There is deliberately
# no test for that branch here: proving it black-box requires an override
# path that (a) literally starts with "/snap/" -- case-matched as a plain
# string, not path-normalized -- and (b) would otherwise resolve via `-x`/
# `command -v`, which means a real, writable file actually has to exist at
# that literal absolute path. Filesystem root isn't writable by a normal
# user (confirmed: `touch /snap_test_write` -> "Read-only file system" on
# this box, and plain non-root elsewhere), so a nonexistent /snap/... probe
# returns rc=1 via the SAME final fallback the function already falls back
# to for any nonexistent path -- indistinguishable from the guard actually
# firing. A test that can't tell the guard-branch from the fallback-branch
# apart is exactly the "asserts on current values, not on the property"
# kind CLAUDE.md warns against, so it's left out rather than shipped
# non-discriminating.


def test_find_system_browser_resolves_a_bare_command_name_via_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bindir"
    bin_dir.mkdir()
    override_bin = bin_dir / "mybrowser"
    override_bin.write_text("#!/bin/sh\nexit 0\n")
    override_bin.chmod(0o755)

    r = _run_find_system_browser(override="mybrowser", path_dirs=[str(bin_dir)])
    assert r["rc"] == 0, r
    assert r["out"] == str(override_bin), r


# ---------------------------------------------------------------------------
# install_node_deps(): the function that actually drives the Playwright
# Chromium download. Shared harness for the three behavioral properties the
# old text-assertion tests were standing in for:
#   - an explicit browser override skips the bundled Chromium download
#   - --skip-browser/--no-playwright skip it too
#   - both apt distro branches (sudo-available "--with-deps" and sudo-less
#     plain) still route through run_playwright_install(), so the #35166
#     unrecognized-host retry still fires from inside the real call site
# ---------------------------------------------------------------------------

_INSTALL_NODE_DEPS_FNS = (
    "restore_dirty_lockfiles",
    "find_system_browser",
    "strip_snap_browser_override",
    "run_with_timeout",
    "run_browser_install_with_timeout",
    "playwright_host_unrecognized",
    "playwright_fallback_platform",
    "run_playwright_install",
    "install_node_deps",
)


def _run_install_node_deps(
    *,
    distro: str = "ubuntu",
    distro_version: str = "",
    skip_browser: bool = False,
    override: str = "",
    id_uid: str = "0",
    sudo_ok: bool = False,
    npx_body: str | None = None,
    argv: list[str] | None = None,
) -> dict:
    """Drive the real install_node_deps() (and its real, side-effect-free
    dependencies) against a temp INSTALL_DIR, with fake npm/npx/id/sudo/uname
    so no real network, privilege escalation, or user lookup ever runs.

    `npx` is deliberately a shell FUNCTION (not a script on PATH): inside
    run_with_timeout(), `type -t "$1" = function` routes the call through the
    pure-shell watchdog instead of an external `timeout`/`gtimeout` binary,
    which makes a wedged-npx test finish in ~timeout_seconds instead of
    depending on whether the host has GNU coreutils installed.
    """
    with tempfile.TemporaryDirectory() as td:
        install_dir = Path(td) / "install"
        install_dir.mkdir()
        (install_dir / "package.json").write_text("{}\n")
        hermes_home = Path(td) / "hermes_home"
        hermes_home.mkdir()

        npx_log = Path(td) / "npx.log"
        default_npx = (
            "npx() {\n"
            f"  echo \"$*\" >>{str(npx_log)!r}\n"
            "  return 0\n"
            "}\n"
        )
        npx_fn = npx_body if npx_body is not None else default_npx

        if argv is None:
            skip_browser_line = f'SKIP_BROWSER={"true" if skip_browser else "false"}'
            parse_argv_block = ""
        else:
            # Match install.sh's own initial default (line ~72); the real
            # parser loop then may or may not flip it, same as production.
            skip_browser_line = "SKIP_BROWSER=false"
            parse_argv_block = (
                "set -- " + " ".join(shlex.quote(a) for a in argv) + "\n"
                + _extract_argv_parser() + "\n"
            )

        script = f"""
set -u
INSTALL_DIR={str(install_dir)!r}
HERMES_HOME={str(hermes_home)!r}
export HERMES_HOME
HAS_NODE=true
DISTRO={distro!r}
DISTRO_VERSION={distro_version!r}
NODE_DEPS_TIMEOUT=5
{skip_browser_line}
AGENT_BROWSER_EXECUTABLE_PATH={override!r}
[ -z "$AGENT_BROWSER_EXECUTABLE_PATH" ] && unset AGENT_BROWSER_EXECUTABLE_PATH

{parse_argv_block}

log_info() {{ echo "INFO: $*"; }}
log_warn() {{ echo "WARN: $*"; }}
log_success() {{ echo "OK: $*"; }}

npm() {{ return 0; }}
{npx_fn}
id() {{ if [ "$1" = "-u" ]; then echo {id_uid!r}; else command id "$@"; fi; }}
sudo() {{ if [ "$1" = "-n" ] && [ "$2" = "true" ]; then return {0 if sudo_ok else 1}; fi; return 1; }}
uname() {{ if [ "$1" = "-m" ]; then echo "x86_64"; else command uname "$@"; fi; }}

{_extract(*_INSTALL_NODE_DEPS_FNS)}

install_node_deps
echo "FINAL_RC=$?"
"""
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        npx_calls = npx_log.read_text().splitlines() if npx_log.exists() else []

    return {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "npx_calls": npx_calls,
        "final_rc": _final_rc(proc.stdout),
    }


def test_install_node_deps_skips_bundled_download_when_override_set(tmp_path: Path) -> None:
    """An explicit, valid AGENT_BROWSER_EXECUTABLE_PATH must skip the
    Playwright Chromium download entirely -- not just log about it."""
    override_bin = tmp_path / "mybrowser"
    override_bin.write_text("#!/bin/sh\nexit 0\n")
    override_bin.chmod(0o755)

    r = _run_install_node_deps(distro="ubuntu", override=str(override_bin))

    assert r["final_rc"] == 0, r
    assert r["npx_calls"] == [], (
        f"npx must never run when an explicit browser override resolves: {r}"
    )
    assert "Using explicit browser override" in r["stdout"], r


def test_install_node_deps_skips_playwright_when_skip_browser_flag_set() -> None:
    """--skip-browser must skip the Playwright install -- and the same
    install run WITHOUT the flag must actually invoke it, so this isn't a
    vacuous "npx never runs" assertion."""
    skipped = _run_install_node_deps(distro="ubuntu", skip_browser=True, id_uid="0")
    assert skipped["final_rc"] == 0, skipped
    assert skipped["npx_calls"] == [], (
        f"npx must never run with SKIP_BROWSER=true: {skipped}"
    )
    assert "--skip-browser" in skipped["stdout"], skipped

    not_skipped = _run_install_node_deps(distro="ubuntu", skip_browser=False, id_uid="0")
    assert not_skipped["npx_calls"] != [], (
        f"control case: npx must run when SKIP_BROWSER is false: {not_skipped}"
    )


def test_skip_browser_flag_parsing_prevents_playwright_install() -> None:
    """End-to-end: the real `--skip-browser` / `--no-playwright` CLI argument
    parsing loop actually flips SKIP_BROWSER, and install_node_deps() honors
    it -- proving the wiring from argv to the browser-install skip, not just
    the two halves in isolation."""
    for flag in ("--skip-browser", "--no-playwright"):
        r = _run_install_node_deps(distro="ubuntu", id_uid="0", argv=[flag])
        assert r["final_rc"] == 0, (flag, r)
        assert r["npx_calls"] == [], (flag, "npx must not run", r)

    # Control: an unrelated flag must NOT skip the browser install.
    r = _run_install_node_deps(distro="ubuntu", id_uid="0", argv=["--no-venv"])
    assert r["npx_calls"] != [], ("control case regressed", r)


def test_install_node_deps_routes_both_playwright_branches_through_run_playwright_install() -> None:
    """Both apt distro branches -- sudo-available ("--with-deps chromium")
    and sudo-less (plain "chromium") -- must still go through
    run_playwright_install(), not a bare `npx` call, so the #35166
    unrecognized-host override retry still fires from inside install_node_
    deps() itself. Ubuntu 26.04 is deliberately "too new" (see
    playwright_host_unrecognized()): npx fails without
    PLAYWRIGHT_HOST_PLATFORM_OVERRIDE and succeeds once it's set, so two npx
    invocations (and a clean final exit) only happen if the retry logic
    actually ran.
    """
    for id_uid, sudo_ok, label in (("0", False, "root/with-deps"), ("1000", False, "no-sudo/plain")):
        with tempfile.TemporaryDirectory() as td:
            runlog = Path(td) / "runlog"
            npx_body_inline = (
                "npx() {\n"
                f"  echo \"override=${{PLAYWRIGHT_HOST_PLATFORM_OVERRIDE:-<none>}}\" >>{str(runlog)!r}\n"
                '  if [ -n "${PLAYWRIGHT_HOST_PLATFORM_OVERRIDE:-}" ]; then return 0; fi\n'
                "  return 1\n"
                "}\n"
            )
            r = _run_install_node_deps(
                distro="ubuntu", distro_version="26.04", id_uid=id_uid, sudo_ok=sudo_ok,
                npx_body=npx_body_inline,
            )
            runs = runlog.read_text().splitlines() if runlog.exists() else []

        assert len(runs) == 2, (label, runs, r)
        assert runs[0] == "override=<none>", (label, runs)
        assert runs[1].startswith("override=ubuntu24.04"), (label, runs)
        assert r["final_rc"] == 0, (label, r)
        assert "Playwright browser installation failed" not in r["stdout"], (label, r)


# ---------------------------------------------------------------------------
# run_playwright_install(): a wedged download must not hang install.sh
# forever. Uses a real (function-based) `npx` that sleeps, so the pure-shell
# watchdog inside run_with_timeout does the killing -- deterministic
# regardless of whether the host has GNU `timeout`/`gtimeout` installed.
# ---------------------------------------------------------------------------


def test_run_playwright_install_is_timeout_guarded() -> None:
    script = f"""
set -u
log_warn() {{ :; }}
log_info() {{ :; }}
npx() {{ sleep 300; }}

{_extract("run_with_timeout", "run_browser_install_with_timeout",
           "playwright_host_unrecognized", "playwright_fallback_platform",
           "run_playwright_install")}

run_playwright_install 2 npx playwright install --with-deps chromium
echo "FINAL_RC=$?"
"""
    start = time.monotonic()
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    elapsed = time.monotonic() - start

    assert elapsed < 15, (
        f"run_playwright_install must not hang past its timeout.\n"
        f"elapsed={elapsed:.1f}s\n{proc.stdout}\n{proc.stderr}"
    )
    assert _final_rc(proc.stdout) != 0, (
        "a wedged download must be reported as a failure, not silently swallowed",
        proc.stdout,
    )


# ---------------------------------------------------------------------------
# run_with_timeout(): GNU `timeout` runs its child in its own process group,
# so a terminal Ctrl+C reaches `timeout` but never the child -- the download
# looks frozen and ignores Ctrl+C (#35166). `--foreground` keeps the child in
# the shell's foreground group so Ctrl+C reaches it; `-k 10` guarantees a
# SIGKILL 10s after the deadline. Both are GNU-only, so run_with_timeout
# probes support once (`timeout --foreground -k 10 1 true`) and only uses the
# flags when the probe succeeds, falling back to plain `timeout N cmd` on
# BusyBox/non-GNU. A fake `timeout` records every invocation and rejects any
# --foreground call when the probe is meant to fail, so the assertions below
# are driven by the function's actual dispatch, not by string-matching the
# flags out of install.sh.
# ---------------------------------------------------------------------------


def _run_with_timeout_dispatch(*, supports_foreground: bool) -> dict:
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "timeout_calls.log"
        target = Path(td) / "target.sh"
        target.write_text("#!/bin/sh\necho ran\n")
        target.chmod(0o755)

        script = f"""
set -u
timeout() {{
    printf '%s\\n' "$*" >>{str(log)!r}
    if [ "$1" = "--foreground" ] && [ "$2" = "-k" ] && [ "$3" = "10" ]; then
        if [ {"1" if supports_foreground else "0"} = 1 ]; then
            shift 4
            "$@"
            return $?
        fi
        return 1
    fi
    shift 1
    "$@"
}}

{_extract("run_with_timeout")}

run_with_timeout 5 {str(target)!r}
echo "FINAL_RC=$?"
"""
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        calls = log.read_text().splitlines() if log.exists() else []

    return {"calls": calls, "final_rc": _final_rc(proc.stdout), "stdout": proc.stdout}


def test_run_with_timeout_uses_foreground_kill_flags_when_gtimeout_supports_them() -> None:
    r = _run_with_timeout_dispatch(supports_foreground=True)

    assert len(r["calls"]) == 2, r  # probe, then the real guarded call
    assert r["calls"][0] == "--foreground -k 10 1 true", r
    assert r["calls"][1].startswith("--foreground -k 10 5 "), (
        "the guarded call must reuse --foreground -k 10 once the probe "
        f"confirms support: {r}"
    )
    assert r["final_rc"] == 0, r


def test_run_with_timeout_falls_back_to_plain_timeout_when_unsupported() -> None:
    r = _run_with_timeout_dispatch(supports_foreground=False)

    assert len(r["calls"]) == 2, r
    assert r["calls"][0] == "--foreground -k 10 1 true", r  # probe still attempted
    assert not r["calls"][1].startswith("--foreground"), (
        "BusyBox/non-GNU timeout must not receive --foreground/-k once the "
        f"probe fails: {r}"
    )
    assert r["final_rc"] == 0, r


# ---------------------------------------------------------------------------
# Behavioral tests: source the install.sh helpers in a stubbed shell and assert
# the override retry fires ONLY on a too-new apt release (#35166), and not on a
# host Playwright already supports.
# ---------------------------------------------------------------------------


def _run_install_fn(distro: str, version: str, *, native_fails: bool,
                    arch: str = "x86_64", operator_override: str = "") -> dict:
    """Drive run_playwright_install() end to end.

    Stubs `npx` (the install command) to fail/succeed, `uname -m` for arch, and
    `log_warn`/`log_info` to no-ops. Returns parsed observations: how many times
    the install command ran, and the override value seen on each run.
    """
    native_rc = 1 if native_fails else 0
    harness = f"""
set -u
DISTRO={distro!r}
DISTRO_VERSION={version!r}
export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE={operator_override!r}
[ -z "$PLAYWRIGHT_HOST_PLATFORM_OVERRIDE" ] && unset PLAYWRIGHT_HOST_PLATFORM_OVERRIDE

log_warn() {{ :; }}
log_info() {{ :; }}

# Stub `uname -m` for arch control without touching the real binary.
uname() {{ if [ "$1" = "-m" ]; then echo {arch!r}; else command uname "$@"; fi }}

# Stub the install command. Record each invocation + the override in effect.
npx() {{
    echo "RUN override=${{PLAYWRIGHT_HOST_PLATFORM_OVERRIDE:-<none>}}" >>"$RUNLOG"
    # First run reflects native_fails; the override retry (if any) succeeds.
    if [ -n "${{PLAYWRIGHT_HOST_PLATFORM_OVERRIDE:-}}" ]; then return 0; fi
    return {native_rc}
}}

{_extract("run_browser_install_with_timeout", "run_with_timeout",
           "playwright_host_unrecognized", "playwright_fallback_platform",
           "run_playwright_install")}

run_playwright_install 600 npx playwright install --with-deps chromium
echo "FINAL_RC=$?"
"""
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as lf:
        runlog = lf.name
    try:
        import os
        env = dict(os.environ, RUNLOG=runlog)
        proc = subprocess.run(["bash", "-c", harness], capture_output=True,
                              text=True, env=env)
        runs = Path(runlog).read_text().strip().splitlines()
        return {"runs": runs, "final_rc": _final_rc(proc.stdout), "stderr": proc.stderr}
    finally:
        Path(runlog).unlink(missing_ok=True)


def test_override_retry_fires_on_ubuntu_26() -> None:
    """Ubuntu 26.04 (too new) → native fails → retry with ubuntu24.04 override."""
    r = _run_install_fn("ubuntu", "26.04", native_fails=True)
    assert len(r["runs"]) == 2, r["runs"]
    assert "override=<none>" in r["runs"][0]
    assert "override=ubuntu24.04-x64" in r["runs"][1]
    assert r["final_rc"] == 0


def test_override_retry_fires_on_debian_14() -> None:
    """Debian 14 (> 13) is the too-new apt case → retry with override."""
    r = _run_install_fn("debian", "14", native_fails=True)
    assert len(r["runs"]) == 2, r["runs"]
    assert "override=ubuntu24.04-x64" in r["runs"][1]
    assert r["final_rc"] == 0


def test_no_retry_when_native_succeeds_on_ubuntu_26() -> None:
    """Even on Ubuntu 26.04, a successful native install is never retried."""
    r = _run_install_fn("ubuntu", "26.04", native_fails=False)
    assert len(r["runs"]) == 1, r["runs"]
    assert "override=<none>" in r["runs"][0]
    assert r["final_rc"] == 0


# ---------------------------------------------------------------------------
# Regression: ensure_browser()'s own `"$ab_bin" install` (the plain
# `agent-browser install` invocation, distinct from run_playwright_install()
# tested above) must also be timeout-guarded. Before this fix it was the one
# neighbor of run_playwright_install()/the npm install a few lines above it
# in the same function that had NO timeout at all -- and check_chromium_
# preflight() (added in 570cba350) now reaches this exact function on every
# negative first Chromium read with no --skip-browser, including the client
# VM creation recipe (docs/product/cloud_init/hermes_install_v2.sh:371-372,
# which passes only --skip-setup --hermes-home, never --skip-browser). A
# wedged download here used to mean an installer that never returns, called
# from a cloud-init recipe with no console/SSH to interrupt it.
# ---------------------------------------------------------------------------


def _run_ensure_browser_chromium_step(*, node_deps_timeout: str, ab_bin_body: str) -> dict:
    """Source the real ensure_browser() (and its real, side-effect-free
    helpers find_system_browser/strip_snap_browser_override) in a stubbed
    shell with fake node/npm/agent-browser binaries, and drive the Chromium
    download step for real. Same extraction technique as _run_install_fn()
    above; unlike that helper we don't need to stub `npx`/`timeout` here --
    real `node`/`npm` are trivial fakes and the real `timeout`/`gtimeout`
    (or the pure-shell watchdog fallback) is exactly the mechanism under
    test, so it must run unstubbed.
    """
    with tempfile.TemporaryDirectory() as td:
        node_bin_dir = Path(td) / "hermes_home" / "node" / "bin"
        node_bin_dir.mkdir(parents=True)
        hermes_home = node_bin_dir.parent.parent

        (node_bin_dir / "node").write_text("#!/bin/sh\nexit 0\n")
        (node_bin_dir / "node").chmod(0o755)
        (node_bin_dir / "npm").write_text("#!/bin/sh\nexit 0\n")
        (node_bin_dir / "npm").chmod(0o755)
        ab_bin = node_bin_dir / "agent-browser"
        ab_bin.write_text(ab_bin_body)
        ab_bin.chmod(0o755)

        harness = f"""
set -u
HERMES_HOME={str(hermes_home)!r}
export HERMES_HOME
NODE_DEPS_TIMEOUT={node_deps_timeout!r}
unset AGENT_BROWSER_EXECUTABLE_PATH
DISTRO=unknown
PATH={str(node_bin_dir)!r}:/usr/bin:/bin
export PATH

log_info() {{ :; }}
log_warn() {{ echo "WARN: $*"; }}
log_error() {{ echo "ERROR: $*"; }}

{_extract("run_with_timeout", "find_system_browser", "strip_snap_browser_override", "ensure_browser")}

ensure_browser
echo "FINAL_RC=$?"
"""
        start = time.monotonic()
        proc = subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
            timeout=30,
        )
        elapsed = time.monotonic() - start

    return {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "final_rc": _final_rc(proc.stdout),
        "elapsed": elapsed,
    }


def test_ensure_browser_chromium_download_is_timeout_guarded() -> None:
    """A wedged `agent-browser install` must be killed by NODE_DEPS_TIMEOUT,
    not left to hang forever -- the exact #35166 hang class the neighboring
    npm install a few lines above it in ensure_browser() is already guarded
    against. Uses a 300s fake download against a 2s NODE_DEPS_TIMEOUT and
    asserts real wall-clock time stayed far below 300s (generous 15s bound
    for CI jitter -- this is an upper bound, not a tight timing race)."""
    r = _run_ensure_browser_chromium_step(
        node_deps_timeout="2",
        ab_bin_body="#!/bin/sh\nsleep 300\n",
    )

    assert r["elapsed"] < 15, (
        "ensure_browser() must not hang past NODE_DEPS_TIMEOUT while installing "
        f"Chromium.\nelapsed={r['elapsed']:.1f}s\n{r}"
    )
    # A timed-out repair is not an installer error (docs/product/cloud_init's
    # client VM recipe never passes --skip-browser, and install.sh's exit
    # code must not change because of this step) -- ensure_browser() must
    # still report 0 and fall through to its own warning, same as any other
    # Chromium-install failure.
    assert r["final_rc"] == 0, r
    assert "Chromium install failed" in r["stdout"], r


def test_ensure_browser_chromium_download_succeeds_quickly_when_not_wedged() -> None:
    """Sanity check for the harness itself: a fast, well-behaved install
    must not be affected by the timeout wrap -- no false WARN, no needless
    delay."""
    r = _run_ensure_browser_chromium_step(
        node_deps_timeout="2",
        ab_bin_body="#!/bin/sh\nexit 0\n",
    )

    assert r["elapsed"] < 5, r
    assert r["final_rc"] == 0, r
    assert "Chromium install failed" not in r["stdout"], r


# ---------------------------------------------------------------------------
# Regression: agent-browser has no way to point its Chromium download at a
# directory of the caller's choosing -- it always lands in the invoking
# user's own $HOME. Provisioning runs install.sh as root while the agent
# runs as an unprivileged `user`, so a plain root-invoked `agent-browser
# install` puts the browser in /root/.agent-browser -- where the agent, in
# `user`'s $HOME, will never find it. Confirmed by running install.sh on a
# clean client VM 2026-09-03.
#
# The fix in ensure_browser(): when running as root and $HERMES_HOME is
# owned by someone else, re-run the download as that owner via
# `sudo -u <owner> -H`. The `-H` is load-bearing on its own -- without it,
# sudo keeps the CALLER's $HOME (root's), and the download still lands in
# /root, silently defeating the whole fix. That is exactly why "-H present"
# gets its own dedicated assertion below, not just a substring check folded
# into the general sudo-invocation test.
#
# All of `id`, `stat`, `sudo`, and `agent-browser` are stubbed shell
# functions/scripts -- no real privilege escalation, user lookup, or
# download ever runs.
# ---------------------------------------------------------------------------


def _run_ensure_browser_sudo_step(*, uid: str, hermes_owner: str, owner_exists: bool) -> dict:
    """Extract the real ensure_browser() (and its real, side-effect-free
    helpers) and drive only the branch that decides whether the Chromium
    download is re-run as $HERMES_HOME's owner via `sudo -u <owner> -H`.

    Stubs `id` (both `id -u` and the `id <owner>` existence probe used by
    ensure_browser), `stat -c '%U'` (ownership lookup), and `sudo` itself
    (records its full argv, never execs through) as shell functions, plus a
    trivial fake `agent-browser` binary that records a direct (non-sudo)
    invocation. Same extraction technique as
    _run_ensure_browser_chromium_step() above.
    """
    with tempfile.TemporaryDirectory() as td:
        node_bin_dir = Path(td) / "hermes_home" / "node" / "bin"
        node_bin_dir.mkdir(parents=True)
        hermes_home = node_bin_dir.parent.parent

        (node_bin_dir / "node").write_text("#!/bin/sh\nexit 0\n")
        (node_bin_dir / "node").chmod(0o755)
        (node_bin_dir / "npm").write_text("#!/bin/sh\nexit 0\n")
        (node_bin_dir / "npm").chmod(0o755)

        sudo_log = Path(td) / "sudo.log"
        ab_log = Path(td) / "ab_direct.log"

        ab_bin = node_bin_dir / "agent-browser"
        ab_bin.write_text(
            "#!/bin/sh\n"
            'echo "AB_DIRECT:$*" >>"$AB_LOG"\n'
            "exit 0\n"
        )
        ab_bin.chmod(0o755)

        harness = f"""
set -u
HERMES_HOME={str(hermes_home)!r}
export HERMES_HOME
NODE_DEPS_TIMEOUT=5
unset AGENT_BROWSER_EXECUTABLE_PATH
DISTRO=unknown
PATH={str(node_bin_dir)!r}:/usr/bin:/bin
export PATH
SUDO_LOG={str(sudo_log)!r}
export SUDO_LOG
AB_LOG={str(ab_log)!r}
export AB_LOG
FAKE_UID={uid!r}
FAKE_OWNER={hermes_owner!r}
OWNER_EXISTS={"1" if owner_exists else "0"!r}

log_info() {{ :; }}
log_warn() {{ echo "WARN: $*"; }}
log_error() {{ echo "ERROR: $*"; }}

# Stub `id`: `id -u` reports the fake uid; `id <name>` (existence probe,
# no flag) succeeds only for the configured owner when OWNER_EXISTS=1.
id() {{
    if [ "$1" = "-u" ]; then
        echo "$FAKE_UID"
        return 0
    fi
    if [ "$OWNER_EXISTS" = "1" ] && [ "$1" = "$FAKE_OWNER" ]; then
        return 0
    fi
    return 1
}}

# Stub `stat -c '%U' <path>`: always reports the configured owner,
# regardless of the path argument.
stat() {{
    echo "$FAKE_OWNER"
}}

# Stub `sudo`: records the full invocation and returns success WITHOUT
# execing through to the wrapped command -- we only care whether
# ensure_browser decided to shell out via sudo, and with what flags.
sudo() {{
    printf 'SUDO:%s\\n' "$*" >>"$SUDO_LOG"
    return 0
}}

{_extract("run_with_timeout", "find_system_browser", "strip_snap_browser_override", "ensure_browser")}

ensure_browser
echo "FINAL_RC=$?"
"""
        proc = subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Read the log files BEFORE the TemporaryDirectory context exits --
        # __exit__ deletes td (and everything under it, including these
        # logs), so reading them after the `with` block silently returns
        # empty results instead of raising.
        sudo_calls = sudo_log.read_text().splitlines() if sudo_log.exists() else []
        ab_direct_calls = ab_log.read_text().splitlines() if ab_log.exists() else []

    return {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "final_rc": _final_rc(proc.stdout),
        "sudo_calls": sudo_calls,
        "ab_direct_calls": ab_direct_calls,
    }


def test_ensure_browser_uses_sudo_dash_H_when_root_and_home_owned_by_other_user() -> None:
    """root installer + $HERMES_HOME owned by `user` -> download re-run as
    `user` via `sudo -u user -H`, not run directly as root."""
    r = _run_ensure_browser_sudo_step(uid="0", hermes_owner="user", owner_exists=True)

    assert r["final_rc"] == 0, r
    assert len(r["sudo_calls"]) == 1, r
    assert not r["ab_direct_calls"], (
        "agent-browser must not run directly as root when HERMES_HOME "
        f"belongs to another, existing user: {r}"
    )

    args = shlex.split(r["sudo_calls"][0].split("SUDO:", 1)[1])
    assert args[:2] == ["-u", "user"], args
    assert args[-2:] == ["agent-browser", "install"] or args[-1] == "install", args


def test_ensure_browser_sudo_call_includes_dash_H_flag() -> None:
    """Dedicated check for the `-H` flag itself: without it sudo keeps the
    CALLER's $HOME (root's), so the whole ownership fix silently does
    nothing and the browser still lands in /root. This must fail loudly if
    `-H` is ever dropped from the sudo invocation -- a plain substring
    check on the whole line would not catch `-H` moving out of the
    tokenized argv (e.g. into a comment or a different flag's value), so
    the invocation is parsed into tokens first."""
    r = _run_ensure_browser_sudo_step(uid="0", hermes_owner="user", owner_exists=True)

    assert len(r["sudo_calls"]) == 1, r
    args = shlex.split(r["sudo_calls"][0].split("SUDO:", 1)[1])
    assert "-H" in args, (
        f"sudo invocation is missing -H, HOME will leak from the caller (root): {args}"
    )


def test_ensure_browser_no_sudo_when_not_root() -> None:
    """Non-root installer (the common case) never shells out via sudo, even
    when $HERMES_HOME belongs to someone else -- the download just runs
    directly as the invoking user, same as before this fix."""
    r = _run_ensure_browser_sudo_step(uid="1000", hermes_owner="user", owner_exists=True)

    assert r["final_rc"] == 0, r
    assert not r["sudo_calls"], r
    assert r["ab_direct_calls"], (
        f"agent-browser should have been invoked directly (no sudo): {r}"
    )


def test_ensure_browser_no_sudo_when_hermes_home_owned_by_root() -> None:
    """root installer + $HERMES_HOME also owned by root -> no re-exec
    needed, runs directly."""
    r = _run_ensure_browser_sudo_step(uid="0", hermes_owner="root", owner_exists=True)

    assert r["final_rc"] == 0, r
    assert not r["sudo_calls"], r
    assert r["ab_direct_calls"], r


def test_ensure_browser_no_sudo_when_owner_user_does_not_exist() -> None:
    """root installer + $HERMES_HOME owned by a name with no matching
    system account (e.g. a UID left over from a deleted user, or a
    container artifact) -> ensure_browser must not attempt
    `sudo -u <ghost>` (which would just fail) and must not crash; it falls
    back to running directly."""
    r = _run_ensure_browser_sudo_step(uid="0", hermes_owner="ghostuser", owner_exists=False)

    assert r["final_rc"] == 0, r
    assert not r["sudo_calls"], r
    assert r["ab_direct_calls"], r


# ---------------------------------------------------------------------------
# The final line must reflect what actually happened
# ---------------------------------------------------------------------------


def test_failed_browser_install_does_not_report_success() -> None:
    """A failed Chromium install must not be followed by a success line.

    Снято с живой машины 2026-09-04, две строки подряд в логе установки:

        ⚠ Playwright browser installation failed — browser tools will not work.
        ✓ Browser engine setup complete

    Второй строкой печаталась безусловная `log_success`, и читающий лог
    (человек на приёмке, поддержка, будущий разбор жалобы) видел зелёную
    галочку и шёл дальше. На машине браузера при этом не было, а весь
    набор `browser_*` пропадает из схемы модели молча — без единой строки
    ошибки в рантайме.
    """
    failing_npx = "npx() {\n  return 1\n}\n"
    r = _run_install_node_deps(distro="ubuntu", id_uid="0", npx_body=failing_npx)

    out = r["stdout"]
    assert "browser tools will not work" in out.lower(), out
    assert "Browser engine setup complete" not in out, (
        "установщик отрапортовал об успехе шага, который провалился:\n" + out
    )


def test_successful_browser_install_still_reports_success() -> None:
    """Противоположная сторона того же контракта — иначе «починка» свелась бы
    к тому, чтобы никогда ничего не сообщать."""
    r = _run_install_node_deps(distro="ubuntu", id_uid="0")

    assert "Browser engine setup complete" in r["stdout"], r["stdout"]
    assert "browser tools will not work" not in r["stdout"].lower(), r["stdout"]
