"""Regression: check_chromium_preflight() must not abort the whole
installer when the venv python can't run the Chromium check, must say so
explicitly when it can't verify (I4, mirrored from the Docker preflight),
and must route the check's verdict to log_success (OK) or log_warn (WARN)
correctly.

Chromium is the second external dependency the curated config template
picks (browser.backend: "off" -> built-in browser_* tools over a local
Chromium) that install.sh does not verify the outcome of. Unlike Docker,
silence here is worse: proven by direct execution (see
hermes_cli/browser_preflight.py's docstring), with no Chromium reachable
the ENTIRE browser_* tool surface silently disappears from the model's
schema -- no leftover tool, no error text, nothing.

Also covers the repair-before-verdict behavior: on a negative first read,
check_chromium_preflight() now tries ensure_browser() (the same repair
path `--ensure browser` drives) exactly once before printing anything,
then reports the REAL post-repair state -- never the stale pre-repair
one. See docs/product/PROMPT-spec15-support-page.md, "Блокирующие
зависимости" #3.

Runs the real check_chromium_preflight() shell function in isolation
(extracted verbatim from install.sh), same technique as
test_install_sh_docker_preflight.py. _probe_chromium_backend() is
extracted alongside it since check_chromium_preflight() now calls it by
name (twice: before and after the repair attempt). ensure_browser()
itself is stubbed rather than extracted -- exercising the real one would
mean real npm/network calls in the test suite, which is exactly the kind
of environment-dependent state this file otherwise goes out of its way to
isolate away (see the isolation note below). The stub's default behavior
(defined, but leaves Chromium exactly as it found it) is overridable per
test via extra_env_lines, the same mechanism already used to control
AGENT_BROWSER_EXECUTABLE_PATH.

Isolation note: tools.browser_tool.check_browser_requirements() branches on
more than just "is Chromium on disk" -- an unset browser.backend defaults
to Browser Use CLI mode whenever `uvx`/`browser-use` is runnable at all
(tools/browser_use_cli.py::is_browser_use_cli_mode), and Chromium discovery
also checks the real $HOME's Playwright cache. Both of those are ambient,
developer-machine-dependent state that has nothing to do with what this
test wants to isolate. Every test here therefore pins HOME to an empty
temp dir, writes an explicit `browser: {backend: "off"}` config.yaml under
an isolated HERMES_HOME (matching what copy_config_templates() already
wrote by the time check_chromium_preflight() runs in production), and
scopes PATH per-case -- reproducing exactly the config state install.sh
runs this check against, not whatever happens to be true of the box
running the test suite.
"""

from __future__ import annotations

import os
import stat
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _find_dev_venv_dir() -> Path:
    """Mirrors scripts/run_tests.sh's venv probe order (.venv, venv,
    ~/.hermes/hermes-agent/venv). Returns the venv's ROOT directory, not
    just its python binary -- see _symlink_real_venv_python()."""
    candidates = [
        REPO_ROOT / ".venv",
        REPO_ROOT / "venv",
        Path.home() / ".hermes" / "hermes-agent" / "venv",
    ]
    for candidate in candidates:
        if (candidate / "bin" / "python").is_file():
            return candidate
    raise AssertionError(
        f"expected a real dev venv with hermes_cli installed at one of: "
        f"{[str(c) for c in candidates]}"
    )


def _symlink_real_venv_python(install_dir: Path) -> None:
    """Symlinks $install_dir/venv to the REAL venv's root directory.
    Symlinking only the python executable would leave sys.prefix pointed
    at a dep-less fake tree (site-packages lives at
    <venv_root>/lib/python3.X/site-packages) -- this check needs
    tools.browser_tool's real third-party imports (PyYAML via `utils`),
    unlike the Docker check which needs only stdlib."""
    real_venv_dir = _find_dev_venv_dir()
    venv_link = install_dir / "venv"
    venv_link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(real_venv_dir, venv_link)


def _make_broken_venv_python(install_dir: Path) -> None:
    python_path = install_dir / "venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    python_path.chmod(python_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_trix_style_config(hermes_home: Path) -> None:
    """browser.backend: "off" -- the exact key the curated config template
    sets (assets/config/trix-config.yaml), already on disk by the time
    check_chromium_preflight() runs in the real install (it's called from
    print_success(), well after copy_config_templates())."""
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        'browser:\n  backend: "off"\n', encoding="utf-8"
    )


def _extract_function(name: str) -> str:
    """Read a function body out of install.sh with Python's own regex.

    Shelling out to `sed` for this is what forced /usr/bin onto the test's
    PATH, and /usr/bin is exactly where a browser lives on the CI runner --
    so the harness that existed to prove "no Chromium found" was handing the
    check a Chrome to find. Extracting here means the narrowed PATH needs no
    coreutils at all, and can therefore be genuinely empty.
    """
    src = INSTALL_SH.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", src, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from install.sh"
    return m.group(0)


def _run_check_chromium_preflight(
    install_dir: Path,
    *,
    hermes_home: Path,
    home: Path,
    path_entries: list[str],
    extra_env_lines: str = "",
) -> subprocess.CompletedProcess:
    home.mkdir(parents=True, exist_ok=True)
    script = f"""
set -e
INSTALL_DIR={str(install_dir)!r}
HERMES_HOME={str(hermes_home)!r}
export HERMES_HOME
HOME={str(home)!r}
export HOME
export PATH={":".join(path_entries)!r}
unset AGENT_BROWSER_EXECUTABLE_PATH
SKIP_BROWSER=false
log_info() {{ echo "INFO: $*"; }}
log_warn() {{ echo "WARN: $*"; }}
log_success() {{ echo "OK: $*"; }}
# Default repair stub: defined (so a WARN-path run always has something to
# call), but a no-op that leaves Chromium exactly as found -- i.e. "the
# repair ran and changed nothing", the honest default for a test that
# isn't specifically exercising the repair. Placed BEFORE extra_env_lines
# so a test that needs a different ensure_browser can simply define its
# own -- the later definition wins in bash regardless of which is textually
# first below, but this ordering keeps that override visually obvious.
ensure_browser() {{ echo "STUB_ENSURE_BROWSER_CALLED" >&2; return 1; }}
{extra_env_lines}
{_extract_function("_probe_chromium_backend")}
{_extract_function("check_chromium_preflight")}
check_chromium_preflight
echo "EXIT_CODE=$?"
"""
    # cwd=install_dir so python-dotenv's upward .env search (triggered by
    # importing tools/browser_tool.py's dependency chain) can never find
    # this REPO's own .env instead of the isolated HERMES_HOME above.
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=install_dir,
    )


def test_broken_venv_python_does_not_abort_the_installer(tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _make_broken_venv_python(install_dir)

    result = _run_check_chromium_preflight(
        install_dir,
        hermes_home=tmp_path / "hermes_home",
        home=tmp_path / "home",
        path_entries=["/usr/bin", "/bin"],
    )

    assert "EXIT_CODE=0" in result.stdout, (
        "check_chromium_preflight() must return 0 even when the venv "
        f"python fails to run the check.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" not in result.stdout
    assert "не проверен" in result.stdout


def test_no_venv_python_at_all_says_so_instead_of_staying_silent(tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    # Deliberately no venv/ directory at all -- the --no-venv case.

    result = _run_check_chromium_preflight(
        install_dir,
        hermes_home=tmp_path / "hermes_home",
        home=tmp_path / "home",
        path_entries=["/usr/bin", "/bin"],
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" not in result.stdout
    assert "не проверен" in result.stdout


def test_healthy_chromium_routes_to_log_success(tmp_path):
    """AGENT_BROWSER_EXECUTABLE_PATH pointed at a fake executable, with
    browser.backend: "off" pinned so Browser Use CLI mode (which would
    short-circuit check_browser_requirements() to False regardless of
    Chromium) never enters the picture -- the real
    check_browser_requirements() -> _chromium_installed() chain, run for
    real through the venv python subprocess, reports "found"."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _symlink_real_venv_python(install_dir)
    hermes_home = tmp_path / "hermes_home"
    _write_trix_style_config(hermes_home)

    fake_chrome = tmp_path / "fake-chrome"
    fake_chrome.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_chrome.chmod(fake_chrome.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    result = _run_check_chromium_preflight(
        install_dir,
        hermes_home=hermes_home,
        home=tmp_path / "home",
        path_entries=["/usr/bin", "/bin"],
        extra_env_lines=f"export AGENT_BROWSER_EXECUTABLE_PATH={str(fake_chrome)!r}",
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" not in result.stdout
    assert "готов" in result.stdout.lower()


def test_missing_chromium_routes_to_log_warn_and_still_succeeds(tmp_path):
    """browser.backend: "off" pinned, HOME isolated to an empty temp dir
    (no real Playwright cache to accidentally find), PATH has no
    chromium/google-chrome/chrome/chromium-browser and no uvx/browser-use
    (so Browser Use CLI mode can't kick in either) -- the real check must
    report "not found"."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _symlink_real_venv_python(install_dir)
    hermes_home = tmp_path / "hermes_home"
    _write_trix_style_config(hermes_home)

    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()

    result = _run_check_chromium_preflight(
        install_dir,
        hermes_home=hermes_home,
        home=tmp_path / "home",
        # PATH carries nothing but the empty dir. It used to keep
        # /usr/bin:/bin "for the harness's own `sed -n`", on the reasoning
        # that a stock box has no browser there. The GitHub Ubuntu runner
        # is not a stock box: it ships Chrome, the check found it, and this
        # test failed with "OK: ... найден" the first time the suite ever
        # ran on a runner. The extraction is done in Python now, so no
        # coreutils are needed and the assumption is gone with them.
        path_entries=[str(empty_bin)],
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" not in result.stdout
    assert "Chromium не найден" in result.stdout
    # The repair attempt DID fire (the default stub is a no-op, so the
    # verdict correctly stays WARN) -- confirms check_chromium_preflight()
    # tries to fix a negative read before reporting it, not just before
    # this specific message string.
    assert "STUB_ENSURE_BROWSER_CALLED" in result.stderr


# ---------------------------------------------------------------------------
# Repair-before-verdict: check_chromium_preflight() must try ensure_browser()
# exactly once on a negative first read, then report the REAL post-repair
# state -- never the stale pre-repair one -- except when --skip-browser was
# passed, which it must honor by not touching the browser at all.
# ---------------------------------------------------------------------------


def test_repair_attempt_upgrades_warn_to_ok(tmp_path):
    """A negative first read followed by a successful repair must report OK,
    not the stale pre-repair WARN. The stub `ensure_browser` here does what
    the real one does on success: it makes Chromium findable (by creating
    the file AGENT_BROWSER_EXECUTABLE_PATH already points at, but does not
    yet exist when the FIRST probe runs) -- driving the exact same
    check_chromium_backend() code path the real repair would."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _symlink_real_venv_python(install_dir)
    hermes_home = tmp_path / "hermes_home"
    _write_trix_style_config(hermes_home)

    fake_chrome = tmp_path / "fake-chrome"  # does not exist yet -> first probe is WARN
    call_log = tmp_path / "ensure_browser_calls.log"

    extra_env_lines = f"""
export AGENT_BROWSER_EXECUTABLE_PATH={str(fake_chrome)!r}
ensure_browser() {{
    echo "called" >> {str(call_log)!r}
    printf '#!/bin/sh\\nexit 0\\n' > {str(fake_chrome)!r}
    chmod +x {str(fake_chrome)!r}
    return 0
}}
"""
    result = _run_check_chromium_preflight(
        install_dir,
        hermes_home=hermes_home,
        home=tmp_path / "home",
        path_entries=["/usr/bin", "/bin"],
        extra_env_lines=extra_env_lines,
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "EXIT_CODE=0" in result.stdout
    assert "OK:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" not in result.stdout, (
        "a successful repair must not leave the stale pre-repair WARN in "
        f"the output.\nstdout: {result.stdout}"
    )
    # Text-content assertions, not just the log-level prefix: a bug that
    # flips chromium_status to OK while leaving chromium_message pointed at
    # the stale pre-repair text (a copy-paste/typo on the assignment right
    # after the retry parse) would still satisfy "OK: present, WARN: absent"
    # above -- and did, in review of 570cba350, print exactly
    # "OK: Chromium не найден. ... Установите вручную: ...". Pin the real
    # post-repair message and rule out the stale one by content.
    # Isolate the final verdict line specifically -- check_chromium_preflight()
    # always prints an informational "Chromium не найден -- пробую
    # доустановить..." line before attempting the repair, so asserting
    # "не найден" absent from the WHOLE output would false-positive on that
    # unrelated, always-present line. What must never contain the stale
    # wording is the verdict line itself.
    verdict_lines = [
        line for line in result.stdout.splitlines() if line.startswith(("OK:", "WARN:"))
    ]
    assert len(verdict_lines) == 1, f"expected exactly one verdict line.\nstdout: {result.stdout}"
    verdict_line = verdict_lines[0]
    assert verdict_line == (
        "OK: Локальный Chromium для браузерных инструментов найден и готов к работе."
    ), (
        "a successful repair must report the REAL post-repair OK message "
        f"text, not just an OK-prefixed line.\nverdict_line: {verdict_line!r}"
    )
    assert "не найден" not in verdict_line, (
        "a successful repair must not leave the stale pre-repair 'Chromium "
        f"не найден' wording in the verdict line.\nverdict_line: {verdict_line!r}"
    )
    assert call_log.read_text().strip().splitlines() == ["called"]


def test_repair_attempt_runs_at_most_once(tmp_path):
    """Even when the repair does not fix anything, ensure_browser() must be
    called exactly once -- no retry loop."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _symlink_real_venv_python(install_dir)
    hermes_home = tmp_path / "hermes_home"
    _write_trix_style_config(hermes_home)

    fake_chrome = tmp_path / "fake-chrome"  # never created -> stays WARN
    call_log = tmp_path / "ensure_browser_calls.log"

    extra_env_lines = f"""
export AGENT_BROWSER_EXECUTABLE_PATH={str(fake_chrome)!r}
ensure_browser() {{ echo "called" >> {str(call_log)!r}; return 1; }}
"""
    result = _run_check_chromium_preflight(
        install_dir,
        hermes_home=hermes_home,
        home=tmp_path / "home",
        path_entries=["/usr/bin", "/bin"],
        extra_env_lines=extra_env_lines,
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "EXIT_CODE=0" in result.stdout
    assert "WARN:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" not in result.stdout
    assert call_log.read_text().strip().splitlines() == ["called"], (
        "ensure_browser() must run exactly once, never in a retry loop"
    )


def test_skip_browser_flag_prevents_repair_attempt(tmp_path):
    """--skip-browser is an explicit operator choice install_node_deps()
    already honored earlier in the same run; check_chromium_preflight()
    must not silently override it by attempting a repair anyway."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _symlink_real_venv_python(install_dir)
    hermes_home = tmp_path / "hermes_home"
    _write_trix_style_config(hermes_home)

    fake_chrome = tmp_path / "fake-chrome"  # never created -> stays WARN
    call_log = tmp_path / "ensure_browser_calls.log"

    extra_env_lines = f"""
SKIP_BROWSER=true
export AGENT_BROWSER_EXECUTABLE_PATH={str(fake_chrome)!r}
ensure_browser() {{ echo "called" >> {str(call_log)!r}; return 0; }}
"""
    result = _run_check_chromium_preflight(
        install_dir,
        hermes_home=hermes_home,
        home=tmp_path / "home",
        path_entries=["/usr/bin", "/bin"],
        extra_env_lines=extra_env_lines,
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "EXIT_CODE=0" in result.stdout
    assert "WARN:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" not in result.stdout
    assert not call_log.exists(), "ensure_browser() must not run when --skip-browser was passed"


def test_already_healthy_chromium_never_attempts_repair(tmp_path):
    """A positive first read is not a reason to touch anything -- no wasted
    repair attempt when Chromium is already usable."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _symlink_real_venv_python(install_dir)
    hermes_home = tmp_path / "hermes_home"
    _write_trix_style_config(hermes_home)

    fake_chrome = tmp_path / "fake-chrome"
    fake_chrome.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_chrome.chmod(fake_chrome.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    call_log = tmp_path / "ensure_browser_calls.log"

    extra_env_lines = f"""
export AGENT_BROWSER_EXECUTABLE_PATH={str(fake_chrome)!r}
ensure_browser() {{ echo "called" >> {str(call_log)!r}; return 0; }}
"""
    result = _run_check_chromium_preflight(
        install_dir,
        hermes_home=hermes_home,
        home=tmp_path / "home",
        path_entries=["/usr/bin", "/bin"],
        extra_env_lines=extra_env_lines,
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert not call_log.exists(), "ensure_browser() must not run when the first read is already OK"


# ---------------------------------------------------------------------------
# Retry-report parsing: the two guards around the SECOND (post-repair)
# _probe_chromium_backend() call must keep the verdict sane even when that
# probe returns something the real python-backed probe never produces (an
# empty output, or a single line with no message body). The real probe
# always emits exactly two lines (hermes_cli/browser_preflight.py's
# check_chromium_backend() always sets both `ok` and `message`), so these
# cases are stubbed directly rather than driven through the real probe --
# same rationale as stubbing ensure_browser() above (isolating
# check_chromium_preflight()'s own parsing/branching from a dependency's
# real behavior, which is covered elsewhere).
#
# _probe_chromium_backend is stubbed here (not extracted from install.sh)
# specifically because _run_check_chromium_preflight() always appends the
# REAL extracted _probe_chromium_backend() after any stub in
# extra_env_lines, so the real one would win. This second harness skips
# that extraction entirely.
# ---------------------------------------------------------------------------


def _run_check_chromium_preflight_with_stubbed_probe(
    install_dir: Path,
    *,
    hermes_home: Path,
    home: Path,
    probe_stub_body: str,
) -> subprocess.CompletedProcess:
    home.mkdir(parents=True, exist_ok=True)
    script = f"""
set -e
INSTALL_DIR={str(install_dir)!r}
HERMES_HOME={str(hermes_home)!r}
export HERMES_HOME
HOME={str(home)!r}
export HOME
export PATH="/usr/bin:/bin"
unset AGENT_BROWSER_EXECUTABLE_PATH
SKIP_BROWSER=false
log_info() {{ echo "INFO: $*"; }}
log_warn() {{ echo "WARN: $*"; }}
log_success() {{ echo "OK: $*"; }}
ensure_browser() {{ echo "called" >&2; return 0; }}
{probe_stub_body}
{_extract_function("check_chromium_preflight")}
check_chromium_preflight
echo "EXIT_CODE=$?"
"""
    # No venv/python needed at all here: check_chromium_preflight()'s only
    # use of $INSTALL_DIR/venv/bin/python is the executable-existence check
    # up front, and every probe call below is the stub, never the real one.
    python_path = install_dir / "venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python_path.chmod(python_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=install_dir,
    )


def _first_call_warn_second_call(second_call_output: str) -> str:
    """Shared stub body: first call to _probe_chromium_backend() returns a
    normal two-line WARN report (so check_chromium_preflight() enters the
    repair branch); the second (post-repair) call returns whatever the
    caller wants to test the retry-parsing guards against."""
    return f"""
_PROBE_CALL_COUNT_FILE=$(mktemp)
echo 0 > "$_PROBE_CALL_COUNT_FILE"
_probe_chromium_backend() {{
    local n
    n=$(cat "$_PROBE_CALL_COUNT_FILE")
    n=$((n + 1))
    echo "$n" > "$_PROBE_CALL_COUNT_FILE"
    if [ "$n" -eq 1 ]; then
        printf 'WARN\\nChromium не найден (исходное сообщение первого чтения)\\n'
    else
        {second_call_output}
    fi
}}
"""


def test_repair_retry_probe_empty_output_keeps_sane_verdict(tmp_path):
    """If the post-repair probe produces NO output at all -- something the
    real check_chromium_backend() never does, but check_chromium_preflight()
    still guards against (`[ -n "$chromium_retry_report" ]`, install.sh
    around line 3019) -- the installer must not crash or print an
    empty/garbled verdict. It falls back to the original pre-repair verdict
    (see the asymmetry paragraph in check_chromium_preflight()'s comment:
    this is the known, intentional-if-imperfect fallback, not a crash)."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    hermes_home = tmp_path / "hermes_home"
    home = tmp_path / "home"

    probe_stub_body = _first_call_warn_second_call("printf ''")

    result = _run_check_chromium_preflight_with_stubbed_probe(
        install_dir,
        hermes_home=hermes_home,
        home=home,
        probe_stub_body=probe_stub_body,
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "EXIT_CODE=0" in result.stdout
    verdict_lines = [
        line for line in result.stdout.splitlines() if line.startswith(("OK:", "WARN:"))
    ]
    assert len(verdict_lines) == 1, f"expected exactly one verdict line.\nstdout: {result.stdout}"
    assert verdict_lines[0] == "WARN: Chromium не найден (исходное сообщение первого чтения)", (
        "an empty post-repair probe must fall back to the original pre-repair "
        f"verdict, not print an empty/garbled one.\nstdout: {result.stdout}"
    )


def test_repair_retry_probe_single_line_output_keeps_sane_verdict(tmp_path):
    """If the post-repair probe returns a single line with no message body
    (malformed -- the real probe always emits status+message on two lines),
    check_chromium_preflight()'s second guard (`[ -n "$chromium_retry_message"
    ] && [ "$chromium_retry_message" != "$chromium_retry_report" ]`, install.sh
    around line 3022) must keep the original pre-repair verdict rather than
    promote a status with no matching message (which would print e.g.
    "OK: OK" -- a garbled, meaningless line)."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    hermes_home = tmp_path / "hermes_home"
    home = tmp_path / "home"

    # No trailing newline -> chromium_retry_report == chromium_retry_status
    # == chromium_retry_message == "OK", tripping the "message != whole
    # report" guard.
    probe_stub_body = _first_call_warn_second_call("printf 'OK'")

    result = _run_check_chromium_preflight_with_stubbed_probe(
        install_dir,
        hermes_home=hermes_home,
        home=home,
        probe_stub_body=probe_stub_body,
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "EXIT_CODE=0" in result.stdout
    verdict_lines = [
        line for line in result.stdout.splitlines() if line.startswith(("OK:", "WARN:"))
    ]
    assert len(verdict_lines) == 1, f"expected exactly one verdict line.\nstdout: {result.stdout}"
    assert verdict_lines[0] == "WARN: Chromium не найден (исходное сообщение первого чтения)", (
        "a single-line (no message body) post-repair probe must fall back to "
        f"the original pre-repair verdict, not a garbled 'OK: OK'-style line.\n"
        f"stdout: {result.stdout}"
    )
