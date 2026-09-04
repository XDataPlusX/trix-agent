"""Regression: copy_config_templates() must not abort the whole installer
when the venv python can't resolve the curated .env template, and must
prefer Trix's curated .env template over upstream's 496-line example.

Mirrors test_install_sh_config_template_choice.py's coverage of the
config.yaml branch, but for the .env branch added in the same function.
install.sh runs under `set -e` (line 16); `copy_config_templates()`
resolves which .env template to copy via a short Python subprocess
(`hermes_cli.config_template.resolve_env_template`). Assigning its output
with a plain `env_template=$(...)` would abort the ENTIRE installer the
instant that subprocess exits non-zero (e.g. an ImportError inside
`hermes_cli`) -- not just the .env step, taking config.yaml, SOUL.md, and
skills seeding down with it, silently (stderr is redirected).

Runs the real `copy_config_templates()` shell function in isolation
(extracted verbatim from install.sh), same technique as
test_install_sh_config_template_choice.py -- not a reimplementation, not a
source-text assertion.
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
HERMES_HOME={home!r}
INSTALL_DIR={install_dir!r}
NO_SKILLS=true
log_info() {{ echo "INFO: $*"; }}
log_warn() {{ echo "WARN: $*"; }}
log_success() {{ echo "OK: $*"; }}
configure_browser_env_from_system_browser() {{ :; }}
"""


def _run_copy_config_templates(home: Path, install_dir: Path) -> subprocess.CompletedProcess:
    script = (
        _HARNESS_PRELUDE.format(home=str(home), install_dir=str(install_dir))
        + "\n"
        + f"""eval "$(sed -n '/^copy_config_templates()/,/^}}/p' {str(INSTALL_SH)!r})"
copy_config_templates
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


def test_broken_venv_python_falls_back_instead_of_aborting_the_installer(tmp_path):
    home = tmp_path / "home"
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    _make_broken_venv_python(install_dir)

    upstream_example = install_dir / ".env.example"
    upstream_example.write_text("SECRET=1\n# UPSTREAM_ENV_MARKER\n", encoding="utf-8")

    result = _run_copy_config_templates(home, install_dir)

    assert result.returncode == 0, (
        "copy_config_templates() must complete even when the venv python "
        f"fails to resolve the .env template.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    env_path = home / ".env"
    assert env_path.is_file(), ".env must still be created via the fallback"
    assert "UPSTREAM_ENV_MARKER" in env_path.read_text(encoding="utf-8")

    # chmod 600 must still apply regardless of which template was chosen --
    # this file holds API keys and tokens.
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600, f".env must be chmod 600, got {oct(mode)}"

    # Steps AFTER the .env block must still have run -- proof the function
    # reached the end instead of being killed by `set -e`.
    assert (home / "SOUL.md").is_file(), "SOUL.md step never ran -- installer aborted early"
    assert (home / ".no-bundled-skills").is_file(), "skills step never ran -- installer aborted early"


def test_working_python_prefers_the_curated_trix_env_template(tmp_path):
    home = tmp_path / "home"
    install_dir = tmp_path / "install"
    install_dir.mkdir()

    # Real venv python (editable-installs hermes_cli from THIS checkout,
    # independent of install_dir) so resolve_env_template() actually runs.
    real_python = Path(os.path.realpath(REPO_ROOT / ".venv" / "bin" / "python"))
    assert real_python.is_file(), f"expected a real dev venv at {real_python}"
    venv_python = install_dir / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(real_python, venv_python)

    assets_dir = install_dir / "assets" / "config"
    assets_dir.mkdir(parents=True)
    trix_content = (REPO_ROOT / "assets" / "config" / "trix.env.example").read_text(encoding="utf-8")
    (assets_dir / "trix.env.example").write_text(trix_content, encoding="utf-8")

    upstream_example = install_dir / ".env.example"
    upstream_example.write_text("SECRET=1\n# UPSTREAM_ENV_MARKER\n", encoding="utf-8")

    result = _run_copy_config_templates(home, install_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    env_path = home / ".env"
    assert env_path.is_file()
    written = env_path.read_text(encoding="utf-8")
    assert written == trix_content, "curated .env template must win when it resolves successfully"
    assert "UPSTREAM_ENV_MARKER" not in written

    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600, f".env must be chmod 600, got {oct(mode)}"

    # The success log names the actual file chosen, not a generic "template".
    assert "trix.env.example" in result.stdout


def test_no_env_templates_at_all_falls_back_to_touch(tmp_path):
    """Neither our template nor upstream's .env.example exist -- matches
    the pre-existing "touch an empty file" fallback, not a crash."""
    home = tmp_path / "home"
    install_dir = tmp_path / "install"
    install_dir.mkdir()

    result = _run_copy_config_templates(home, install_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    env_path = home / ".env"
    assert env_path.is_file()
    assert env_path.read_text(encoding="utf-8") == ""
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600, f".env must be chmod 600, got {oct(mode)}"
