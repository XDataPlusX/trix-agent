"""Regression: strip_snap_browser_override() must clean up BOTH brand markers.

The function exists to delete a stale Snap Chromium override left in the
customer's ``.env`` by an EARLIER install, along with its auto-written
comment (``configure_browser_env_from_system_browser`` writes the pair
together). Installs from before the Trix rebrand wrote
``# Hermes Agent browser tools``; installs from after it write
``# Trix Agent browser tools``. The strip regex has to match both, or every
already-installed customer who upgrades keeps a stray, orphaned,
upstream-branded comment line in their ``.env`` forever, with a second,
new-branded block appended below it on the next write.

This is exactly the class of bug that only shows up for customers who
UPGRADED an existing install rather than installed fresh -- a clean-VM
acceptance run never exercises it, since a fresh VM never has an old-marker
``.env`` to begin with. Nothing else in the suite covers this path.

Runs the real ``strip_snap_browser_override()`` and
``configure_browser_env_from_system_browser()`` shell functions in isolation
(extracted verbatim from install.sh, same technique as
test_install_sh_bootstrap_marker.py and test_install_sh_origin_repoint.py) --
not a reimplementation, not a source-text assertion.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

_HARNESS_PRELUDE = """
set -e
HERMES_HOME={home!r}
log_info() {{ echo "INFO: $*"; }}
log_warn() {{ echo "WARN: $*"; }}
log_success() {{ echo "OK: $*"; }}
"""


def _run_strip(home: Path) -> subprocess.CompletedProcess:
    script = (
        _HARNESS_PRELUDE.format(home=str(home))
        + "\n"
        + f"""eval "$(sed -n '/^strip_snap_browser_override()/,/^}}/p' {str(INSTALL_SH)!r})"
strip_snap_browser_override
"""
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )


def _run_configure(home: Path, browser_path: str) -> subprocess.CompletedProcess:
    script = (
        _HARNESS_PRELUDE.format(home=str(home))
        + f"DETECTED_BROWSER_EXECUTABLE={browser_path!r}\n"
        + "find_system_browser() { echo ''; return 1; }\n"
        + f"""eval "$(sed -n '/^configure_browser_env_from_system_browser()/,/^}}/p' {str(INSTALL_SH)!r})"
configure_browser_env_from_system_browser
"""
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )


def test_old_marker_env_is_fully_cleaned(tmp_path: Path):
    """(a) An old-branded .env: both the snap line and the old comment go."""
    home = tmp_path / "home"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text(
        "SOME_KEY=value\n"
        "\n"
        "# Hermes Agent browser tools — explicit browser override.\n"
        "AGENT_BROWSER_EXECUTABLE_PATH=/snap/bin/chromium\n",
        encoding="utf-8",
    )

    result = _run_strip(home)

    assert result.returncode == 0, result.stderr
    text = env_file.read_text(encoding="utf-8")
    assert "SOME_KEY=value" in text
    assert "AGENT_BROWSER_EXECUTABLE_PATH" not in text
    assert "Agent browser tools" not in text, (
        "old-branded comment orphaned — this is the exact regression"
    )


def test_new_marker_env_is_fully_cleaned(tmp_path: Path):
    """(b) A new-branded .env: same cleanup, so the fix isn't one-sided."""
    home = tmp_path / "home"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text(
        "SOME_KEY=value\n"
        "\n"
        "# Trix Agent browser tools — explicit browser override.\n"
        "AGENT_BROWSER_EXECUTABLE_PATH=/snap/bin/chromium\n",
        encoding="utf-8",
    )

    result = _run_strip(home)

    assert result.returncode == 0, result.stderr
    text = env_file.read_text(encoding="utf-8")
    assert "SOME_KEY=value" in text
    assert "AGENT_BROWSER_EXECUTABLE_PATH" not in text
    assert "Agent browser tools" not in text


def test_configuring_twice_does_not_duplicate_the_block(tmp_path: Path):
    """(c) A fresh run, then a second run: no duplicated comment+var pair."""
    home = tmp_path / "home"
    home.mkdir()

    first = _run_configure(home, "/usr/bin/chromium-browser")
    assert first.returncode == 0, first.stderr
    second = _run_configure(home, "/usr/bin/chromium-browser")
    assert second.returncode == 0, second.stderr

    text = (home / ".env").read_text(encoding="utf-8")
    assert text.count("AGENT_BROWSER_EXECUTABLE_PATH=") == 1
    assert text.count("Agent browser tools") == 1


def test_deliberate_non_snap_override_is_left_untouched(tmp_path: Path):
    """A customer's own non-Snap override must survive the strip untouched.

    strip_snap_browser_override() only fires on a Snap-pointing path (see
    its own guard); this pins that a non-Snap override -- even one using
    the exact same comment text -- is never a target.
    """
    home = tmp_path / "home"
    home.mkdir()
    env_file = home / ".env"
    original = (
        "SOME_KEY=value\n"
        "\n"
        "# Trix Agent browser tools — explicit browser override.\n"
        "AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium-browser\n"
    )
    env_file.write_text(original, encoding="utf-8")

    result = _run_strip(home)

    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == original
