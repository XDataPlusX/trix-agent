"""Regression: check_ddgs_preflight() must not abort the whole installer
when the venv python can't run the ddgs check, and must route the check's
verdict to log_success (OK) or log_warn (WARN) correctly.

Same failure mode as check_docker_preflight() / check_chromium_preflight()
(tests/test_install_sh_docker_preflight.py, tests/test_install_sh_chromium_preflight.py)
-- install.sh runs under `set -e` (line 16), and check_ddgs_preflight()
(called right after those two, at the tail of print_success()) resolves the
ddgs backend's status via a short Python subprocess
(`hermes_cli.search_preflight.check_ddgs_backend`). A plain
`ddgs_report=$(...)` would abort the ENTIRE installer the instant that
subprocess exits non-zero, right at the finish line.

Stakes: unlike Docker (whose absence just means "no sandbox, still answers
questions") and Chromium (whose absence just drops browser_* from the
schema), a missing ddgs package makes _get_capability_backend() in
tools/web_tools.py silently resolve web.search_backend: ddgs to the
firecrawl default instead -- the client gets "buy a Firecrawl key / log
into Nous Portal" instead of a working DuckDuckGo search, and the DDGS
provider cannot self-heal that at runtime the way exa/firecrawl/parallel
do (see plugins/web/ddgs/provider.py's search() docstring). This preflight
is the only place that failure is ever reported to anyone.

Runs the real `check_ddgs_preflight()` shell function in isolation
(extracted verbatim from install.sh), same technique as the Docker/Chromium
preflight tests -- not a reimplementation, not a source-text assertion.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

_HARNESS_PRELUDE = """
set -e
INSTALL_DIR={install_dir!r}
log_info() {{ echo "INFO: $*"; }}
log_warn() {{ echo "WARN: $*"; }}
log_success() {{ echo "OK: $*"; }}
"""


def _run_check_ddgs_preflight(
    install_dir: Path, *, extra_pythonpath: Path | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if extra_pythonpath is not None:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{extra_pythonpath}{os.pathsep}{existing}" if existing else str(extra_pythonpath)
        )
    else:
        env.pop("PYTHONPATH", None)

    script = (
        _HARNESS_PRELUDE.format(install_dir=str(install_dir))
        + f"""eval "$(sed -n '/^check_ddgs_preflight()/,/^}}/p' {str(INSTALL_SH)!r})"
check_ddgs_preflight
echo "EXIT_CODE=$?"
"""
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env
    )


def _make_broken_venv_python(install_dir: Path) -> None:
    """A venv/bin/python that exists, is executable, and always fails --
    simulating an ImportError inside hermes_cli (or any other subprocess
    failure) without needing a real broken virtualenv."""
    python_path = install_dir / "venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    python_path.chmod(python_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _find_dev_venv_dir() -> Path:
    """Mirrors scripts/run_tests.sh's venv probe order (.venv, venv,
    ~/.hermes/hermes-agent/venv) rather than hardcoding .venv -- a worktree
    or CI box that only has one of the other two must still be able to run
    this test. Returns the venv's ROOT directory, not just its python
    binary -- see _symlink_real_venv_python() for why."""
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
    """Symlinks $install_dir/venv to the REAL venv's root directory (not
    just its python binary) -- see the identical helper in
    test_install_sh_docker_preflight.py for the sys.prefix/site-packages
    rationale."""
    real_venv_dir = _find_dev_venv_dir()
    venv_link = install_dir / "venv"
    venv_link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(real_venv_dir, venv_link)


def _plant_fake_ddgs_module(bin_dir: Path) -> None:
    """A minimal stub `ddgs` module importable via PYTHONPATH -- stands in
    for the real PyPI package so the OK branch can be exercised without
    actually installing anything into the dev venv."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "ddgs.py").write_text(
        "class DDGS:\n"
        "    def __init__(self, *a, **k): pass\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): return False\n"
        "    def text(self, *a, **k): return []\n",
        encoding="utf-8",
    )


def test_broken_venv_python_does_not_abort_the_installer(tmp_path):
    """set -e safety: an ImportError (or any failure) inside the ddgs
    check's Python subprocess must not kill the rest of install.sh -- it
    must just skip the check, since ddgs_report=$(...) is guarded by
    `|| ddgs_report=""`. It must also SAY that it couldn't verify --
    silence here would read as "ddgs is fine" to an operator scanning the
    success banner."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _make_broken_venv_python(install_dir)

    result = _run_check_ddgs_preflight(install_dir)

    assert "EXIT_CODE=0" in result.stdout, (
        "check_ddgs_preflight() must return 0 even when the venv python "
        f"fails to run the check.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" not in result.stdout
    assert "не проверен" in result.stdout


def test_no_venv_python_at_all_says_so_instead_of_staying_silent(tmp_path):
    """--no-venv installs never have $INSTALL_DIR/venv/bin/python. Silently
    returning 0 with no output at all reads to an operator scanning the
    success banner as "ddgs is fine" -- say plainly nothing was verified."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    # Deliberately no venv/ directory at all -- the --no-venv case.

    result = _run_check_ddgs_preflight(install_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" not in result.stdout
    assert "не проверен" in result.stdout


def test_installed_ddgs_routes_to_log_success(tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _symlink_real_venv_python(install_dir)

    fake_pkg_dir = tmp_path / "fake_ddgs_pkg"
    _plant_fake_ddgs_module(fake_pkg_dir)

    result = _run_check_ddgs_preflight(install_dir, extra_pythonpath=fake_pkg_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" not in result.stdout
    assert "готов" in result.stdout


def test_missing_ddgs_routes_to_log_warn_with_the_truth_not_a_firecrawl_ad(tmp_path):
    """The actual defect this preflight exists to close, proven by
    execution against the REAL dev venv, where the `ddgs` package is
    genuinely not installed (it is LAZY_DEPS-only, never a repo
    dependency): the installer must warn, in Russian, with an actionable
    install instruction -- never silently let the client discover the gap
    as a Firecrawl / Nous Portal upsell from a live web_search call."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _symlink_real_venv_python(install_dir)

    result = _run_check_ddgs_preflight(install_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" not in result.stdout
    assert "не установлен" in result.stdout
    assert "ddgs" in result.stdout.lower()
    assert "nous" not in result.stdout.lower()
