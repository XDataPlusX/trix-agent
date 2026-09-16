"""``hermes doctor --fix`` must seed a missing config.yaml from the same
curated template the installer uses, not from upstream's
cli-config.yaml.example directly.

Before this fix, ``run_doctor()``'s missing-config.yaml branch
(``hermes_cli/doctor.py``) copied ``cli-config.yaml.example`` on its own,
bypassing ``hermes_cli.config_template.resolve_config_template()``. On a
machine repaired via ``doctor --fix``, that silently wrote
``terminal.backend: local`` (agent runs commands on the host, can read
``.env``) instead of the sandboxed ``docker`` backend Ruling 1 requires —
exactly the security decision the curated template exists to pin down.

``_seed_missing_config_yaml()`` is the extracted, directly-testable helper
that fixes this: it goes through the same resolver install.sh uses, and
only falls back to writing DEFAULT_CONFIG verbatim if neither template
exists on disk at all.
"""

from __future__ import annotations

import yaml

from hermes_cli.doctor import _seed_missing_config_yaml
from hermes_constants import get_hermes_home


def test_prefers_the_curated_trix_template_when_present(tmp_path):
    project_root = tmp_path / "project"
    assets_dir = project_root / "assets" / "config"
    assets_dir.mkdir(parents=True)
    (assets_dir / "trix-config.yaml").write_text(
        "terminal:\n  backend: docker\n", encoding="utf-8"
    )
    (project_root / "cli-config.yaml.example").write_text(
        "terminal:\n  backend: local\n# UPSTREAM_EXAMPLE_MARKER\n", encoding="utf-8"
    )

    config_path = tmp_path / "home" / "config.yaml"

    source = _seed_missing_config_yaml(config_path, project_root)

    assert config_path.is_file()
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert written["terminal"]["backend"] == "docker", (
        "doctor --fix must not silently revert the sandboxed terminal.backend "
        "decision back to 'local'"
    )
    assert "UPSTREAM_EXAMPLE_MARKER" not in config_path.read_text(encoding="utf-8")
    assert "trix-config.yaml" in source


def test_falls_back_to_upstream_example_when_trix_template_is_absent(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "cli-config.yaml.example").write_text(
        "terminal:\n  backend: local\n# UPSTREAM_EXAMPLE_MARKER\n", encoding="utf-8"
    )

    config_path = tmp_path / "home" / "config.yaml"

    source = _seed_missing_config_yaml(config_path, project_root)

    assert config_path.is_file()
    assert "UPSTREAM_EXAMPLE_MARKER" in config_path.read_text(encoding="utf-8")
    assert "cli-config.yaml.example" in source


def test_falls_back_to_default_config_when_neither_template_exists(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    # The DEFAULT_CONFIG fallback goes through hermes_cli.config.save_config(),
    # which always writes to get_hermes_home()/config.yaml (it ignores the
    # config_path argument entirely) — the same behavior the pre-existing
    # code relied on. The autouse _hermetic_environment fixture already
    # points get_hermes_home() at a per-test tempdir, so use that exact path.
    config_path = get_hermes_home() / "config.yaml"
    assert not config_path.exists()

    source = _seed_missing_config_yaml(config_path, project_root)

    assert config_path.is_file()
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(written, dict) and written, "must write a real config, not an empty file"
    assert "default" in source.lower()
