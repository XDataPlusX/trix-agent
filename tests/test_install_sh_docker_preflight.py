"""Regression: check_docker_preflight() must not abort the whole installer
when the venv python can't run the Docker check, and must route the check's
verdict to log_success (OK) or log_warn (WARN) correctly.

install.sh runs under `set -e` (line 16). check_docker_preflight() (called
right before print_success(), the installer's final success banner) resolves
Docker's status via a short Python subprocess
(`hermes_cli.docker_preflight.check_docker_backend`). Assigning its output
with a plain `docker_report=$(...)` would abort the ENTIRE installer the
instant that subprocess exits non-zero (e.g. an ImportError inside
hermes_cli) -- right at the finish line, after the gateway is already
running, with no success banner and no clue why. Exactly the failure mode
fixed for the config.yaml/.env template resolvers in tasks 1 and 2
(test_install_sh_config_template_choice.py, test_install_sh_env_template_choice.py).

Runs the real `check_docker_preflight()` shell function in isolation
(extracted verbatim from install.sh), same technique as those two tests --
not a reimplementation, not a source-text assertion.
"""

from __future__ import annotations

import os
import re
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


def _extract_function(name: str) -> str:
    """Read a function body out of install.sh with Python's own regex.

    Shelling out to `sed` for this is what forced /usr/bin onto the test's
    PATH, and /usr/bin is exactly where a real `docker` lives on the CI
    runner -- so the harness that existed to prove "no docker found" was
    handing the check a docker to find. Extracting here means the narrowed
    PATH needs no coreutils at all, and can therefore be genuinely empty.
    """
    src = INSTALL_SH.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", src, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from install.sh"
    return m.group(0)


def _run_check_docker_preflight(install_dir: Path, docker_bin_dir: Path | None = None) -> subprocess.CompletedProcess:
    # PATH is rebuilt from scratch (never inherited) AND carries nothing but
    # what the caller planted, so a real `docker` on the dev or CI machine
    # (/usr/local/bin/docker on macOS, /usr/bin/docker on the GitHub Ubuntu
    # runner) can never leak into a test asserting "no docker found".
    #
    # This used to append /usr/bin:/bin "for the coreutils", which defeated
    # the sentence above: CI found /usr/bin/docker and the test failed with
    # "OK: Docker найден" the first time this suite ever ran on a runner.
    # The body itself uses only parameter expansion (see
    # test_function_body_does_not_depend_on_sed_at_runtime) and calls the
    # venv python by absolute path, so an empty PATH is enough.
    path_entries = [str(docker_bin_dir)] if docker_bin_dir is not None else []
    script = (
        _HARNESS_PRELUDE.format(install_dir=str(install_dir))
        + f'export PATH={":".join(path_entries)!r}\n'
        + "\n"
        + _extract_function("check_docker_preflight")
        + """
check_docker_preflight
echo "EXIT_CODE=$?"
"""
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
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
    just its python binary). A venv's site-packages live at
    <venv_root>/lib/python3.X/site-packages, resolved relative to
    sys.prefix -- symlinking only the python executable makes sys.prefix
    point at the fake, dep-less directory tree instead of the real one,
    so any check that imports third-party packages (e.g. PyYAML via
    tools.browser_tool) fails with ModuleNotFoundError even though
    hermes_cli itself (needing no third-party deps) happens to still
    import fine. Symlinking the whole directory keeps sys.prefix pointed
    at a path that, via the symlink, resolves to the real site-packages."""
    real_venv_dir = _find_dev_venv_dir()
    venv_link = install_dir / "venv"
    venv_link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(real_venv_dir, venv_link)


def _plant_fake_docker(bin_dir: Path, script_body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    docker_path = bin_dir / "docker"
    docker_path.write_text(script_body, encoding="utf-8")
    docker_path.chmod(docker_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_broken_venv_python_does_not_abort_the_installer(tmp_path):
    """set -e safety: an ImportError (or any failure) inside the Docker
    check's Python subprocess must not kill the rest of install.sh -- it
    must just skip the check, since docker_report=$(...) is guarded by
    `|| docker_report=""`. It must also SAY that it couldn't verify --
    silence here would read as "Docker is fine" to an operator scanning
    the success banner (I4)."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _make_broken_venv_python(install_dir)

    result = _run_check_docker_preflight(install_dir)

    assert "EXIT_CODE=0" in result.stdout, (
        "check_docker_preflight() must return 0 even when the venv python "
        f"fails to run the check.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" not in result.stdout
    assert "не проверен" in result.stdout


def test_no_venv_python_at_all_says_so_instead_of_staying_silent(tmp_path):
    """--no-venv installs never have $INSTALL_DIR/venv/bin/python. Before
    I4, this path returned 0 with no output at all, which reads to an
    operator scanning the (now Docker-preflight-terminated) success banner
    as "Docker is fine" -- say plainly that nothing was verified instead."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    # Deliberately no venv/ directory at all -- the --no-venv case.

    result = _run_check_docker_preflight(install_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" not in result.stdout
    assert "не проверен" in result.stdout


def test_healthy_docker_routes_to_log_success(tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _symlink_real_venv_python(install_dir)

    fake_bin = tmp_path / "fakebin"
    _plant_fake_docker(fake_bin, "#!/bin/sh\necho ok\nexit 0\n")

    result = _run_check_docker_preflight(install_dir, docker_bin_dir=fake_bin)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" not in result.stdout
    assert "готова к работе" in result.stdout


def test_missing_docker_routes_to_log_warn_and_still_succeeds(tmp_path):
    """No Docker on the machine: the installer must warn, in Russian, and
    still finish (return code 0) -- the whole point of §4.5/§10: an agent
    without a sandbox still answers questions."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _symlink_real_venv_python(install_dir)

    result = _run_check_docker_preflight(install_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "WARN:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" not in result.stdout
    assert "Docker не найден" in result.stdout


def test_function_body_does_not_depend_on_sed_at_runtime(tmp_path):
    """check_docker_preflight() parses its Python subprocess's two-line
    output with pure parameter expansion, not sed/printf subshells -- a
    hostile or broken `sed` shadowing the real one on PATH must not be able
    to break status parsing (previously it could: a stubbed `sed` exiting
    non-zero killed this function's `docker_status=$(... | sed ...)` line
    under `set -e`).

    Extracts the function body with Python's own regex (not a shelled-out
    `sed -n`) so the extraction step itself has no dependency on which
    `sed` ends up on PATH -- only the function's OWN behavior under a
    poisoned PATH is under test here.
    """
    import re

    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _symlink_real_venv_python(install_dir)

    docker_bin = tmp_path / "dockerbin"
    _plant_fake_docker(docker_bin, "#!/bin/sh\necho ok\nexit 0\n")

    poisoned_sed_bin = tmp_path / "poisoned"
    poisoned_sed_bin.mkdir(parents=True)
    poisoned_sed = poisoned_sed_bin / "sed"
    poisoned_sed.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    poisoned_sed.chmod(poisoned_sed.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    src = INSTALL_SH.read_text(encoding="utf-8")
    m = re.search(r"^check_docker_preflight\(\) \{.*?^\}", src, re.MULTILINE | re.DOTALL)
    assert m, "could not extract check_docker_preflight() from install.sh"
    function_body = m.group(0)

    script = (
        _HARNESS_PRELUDE.format(install_dir=str(install_dir))
        # Poisoned `sed` goes first on PATH -- if the function body called
        # sed at all, this would make it exit 7 and (pre-fix) abort under
        # set -e.
        + f'export PATH={f"{poisoned_sed_bin}:{docker_bin}:/usr/bin:/bin"!r}\n'
        + "\n"
        + function_body
        + "\ncheck_docker_preflight\necho \"EXIT_CODE=$?\"\n"
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)

    assert "EXIT_CODE=0" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK:" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "готова к работе" in result.stdout
