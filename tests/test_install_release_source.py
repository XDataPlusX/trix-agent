"""Installer ships our product, not upstream.

Tests are behavioral where a full install isn't required: install.sh's
argument parser rejects unknown options and exits 1 before doing any
work, so removal of --branch is verified by actually running the script,
not by reading its source.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install.sh"


def _run(*args, timeout=30):
    return subprocess.run(
        ["bash", str(INSTALLER), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=REPO_ROOT,
    )


def test_installer_rejects_branch_flag():
    """--branch is gone: trying to pick a branch stops the install."""
    result = _run("--branch", "main")
    assert result.returncode != 0
    assert "Unknown option" in result.stdout + result.stderr


def test_installer_help_mentions_no_branch_option():
    result = _run("--help")
    assert result.returncode == 0
    assert "--branch" not in result.stdout


def test_print_banner_does_not_name_upstream():
    """The banner used to print "...by Nous Research." -- gone now.

    --help never reaches print_banner() (it exits before calling it), so
    asserting "nousresearch not in --help output" was vacuous: it passed
    on the pre-fix commit too, since --help never printed the repo URL or
    the banner's attribution line either. Extract and call print_banner()
    directly instead -- the actual customer-facing surface that carried
    the upstream mention, and the one line where this assertion would have
    genuinely failed before the fix.
    """
    script = f"""
set -e
MAGENTA=''; BOLD=''; NC=''
eval "$(sed -n '/^print_banner()/,/^}}/p' {INSTALLER!s})"
print_banner
"""
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    combined = (result.stdout + result.stderr).lower()
    assert "nous research" not in combined
    assert "nousresearch" not in combined
