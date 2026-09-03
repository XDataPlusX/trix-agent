"""Regression: copy_config_templates() must not abort the whole installer
when the venv python can't resolve the curated config.yaml template.

install.sh runs under `set -e` (line 16). `copy_config_templates()`
resolves which config.yaml template to copy via a short Python subprocess
(`hermes_cli.config_template.resolve_config_template`). Assigning its
output with a plain `config_template=$(...)` aborts the ENTIRE installer
the instant that subprocess exits non-zero (e.g. an ImportError inside
`hermes_cli`) -- not just the config.yaml step. Because the subprocess's
stderr is redirected to /dev/null, the operator sees the installer stop
silently right after ".env" is created, with no config.yaml, no SOUL.md,
and no skills seeded, and no clue why.

Runs the real `copy_config_templates()` shell function in isolation
(extracted verbatim from install.sh), same technique as
test_install_sh_strip_snap_browser_override.py and
test_install_sh_bootstrap_marker.py -- not a reimplementation, not a
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

    upstream_example = install_dir / "cli-config.yaml.example"
    upstream_example.write_text("model: {}\n# UPSTREAM_EXAMPLE_MARKER\n", encoding="utf-8")

    result = _run_copy_config_templates(home, install_dir)

    assert result.returncode == 0, (
        "copy_config_templates() must complete even when the venv python "
        f"fails to resolve the template.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    config_path = home / "config.yaml"
    assert config_path.is_file(), "config.yaml must still be created via the fallback"
    assert "UPSTREAM_EXAMPLE_MARKER" in config_path.read_text(encoding="utf-8")

    # Steps AFTER the config.yaml block must still have run -- proof the
    # function reached the end instead of being killed by `set -e`.
    assert (home / "SOUL.md").is_file(), "SOUL.md step never ran -- installer aborted early"
    assert (home / ".no-bundled-skills").is_file(), "skills step never ran -- installer aborted early"


def test_working_python_prefers_the_curated_trix_template(tmp_path):
    home = tmp_path / "home"
    install_dir = tmp_path / "install"
    install_dir.mkdir()

    # Real venv python (editable-installs hermes_cli from THIS checkout,
    # independent of install_dir) so resolve_config_template() actually runs.
    real_python = Path(os.path.realpath(REPO_ROOT / ".venv" / "bin" / "python"))
    assert real_python.is_file(), f"expected a real dev venv at {real_python}"
    venv_python = install_dir / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(real_python, venv_python)

    assets_dir = install_dir / "assets" / "config"
    assets_dir.mkdir(parents=True)
    trix_content = (REPO_ROOT / "assets" / "config" / "trix-config.yaml").read_text(encoding="utf-8")
    (assets_dir / "trix-config.yaml").write_text(trix_content, encoding="utf-8")

    upstream_example = install_dir / "cli-config.yaml.example"
    upstream_example.write_text("model: {}\n# UPSTREAM_EXAMPLE_MARKER\n", encoding="utf-8")

    result = _run_copy_config_templates(home, install_dir)

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    config_path = home / "config.yaml"
    assert config_path.is_file()
    written = config_path.read_text(encoding="utf-8")
    assert written == trix_content, "curated template must win when it resolves successfully"
    assert "UPSTREAM_EXAMPLE_MARKER" not in written

    # The success log names the actual file chosen, not a generic "template".
    assert "trix-config.yaml" in result.stdout
