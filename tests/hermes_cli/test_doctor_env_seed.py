"""``hermes doctor --fix`` must seed a missing ``.env`` from the same
curated template the installer uses, not fall back to writing an
uninformative empty file.

Before this fix, ``run_doctor()``'s missing-``.env`` branch
(``hermes_cli/doctor.py``) always wrote an empty file via ``touch()``,
even though ``scripts/install.sh`` and ``docker/stage2-hook.sh`` both
seed a fresh install from ``assets/config/trix.env.example`` (or upstream's
``.env.example`` as a fallback). A machine repaired via ``doctor --fix``
got a blank file with no guidance on which three variables to fill in --
the same class of gap ``_seed_missing_config_yaml()`` closed for
``config.yaml`` (see ``tests/hermes_cli/test_doctor_config_seed.py``).

``_seed_missing_env()`` is the extracted, directly-testable helper that
fixes this: it goes through the same resolver install.sh uses
(``hermes_cli.config_template.resolve_env_template``), and only falls
back to an empty file if neither template exists on disk at all.

Follow-up regression this file also guards against: seeding the curated
template introduced a SECOND bug -- ``doctor``'s "API key or custom
endpoint configured" check (``_has_provider_env_config``) used to do a
raw substring search for provider variable NAMES, so
``OPENROUTER_API_KEY=`` (present, deliberately empty in our template)
read as "configured". ``doctor --fix`` would create the curated .env and
the very next ``doctor`` run would report a green checkmark on a machine
that cannot authenticate at all. See ``TestDoctorFixThenDoctorEndToEnd``
below and ``TestProviderEnvDetection`` in ``test_doctor.py`` for the fix
(``_has_provider_env_config`` now parses via ``dotenv_values()`` and
requires ``has_usable_secret()`` on the value).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from hermes_cli.doctor import _seed_missing_env

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_prefers_the_curated_trix_template_when_present(tmp_path):
    project_root = tmp_path / "project"
    assets_dir = project_root / "assets" / "config"
    assets_dir.mkdir(parents=True)
    (assets_dir / "trix.env.example").write_text(
        "TELEGRAM_BOT_TOKEN=\n", encoding="utf-8"
    )
    (project_root / ".env.example").write_text(
        "SECRET=1\n# UPSTREAM_EXAMPLE_MARKER\n", encoding="utf-8"
    )

    env_path = tmp_path / "home" / ".env"

    source = _seed_missing_env(env_path, project_root)

    assert env_path.is_file()
    written = env_path.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=" in written, (
        "doctor --fix must seed the curated template, not an empty file"
    )
    assert "UPSTREAM_EXAMPLE_MARKER" not in written
    assert "trix.env.example" in source


def test_falls_back_to_upstream_example_when_trix_template_is_absent(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env.example").write_text(
        "SECRET=1\n# UPSTREAM_EXAMPLE_MARKER\n", encoding="utf-8"
    )

    env_path = tmp_path / "home" / ".env"

    source = _seed_missing_env(env_path, project_root)

    assert env_path.is_file()
    assert "UPSTREAM_EXAMPLE_MARKER" in env_path.read_text(encoding="utf-8")
    assert ".env.example" in source


def test_falls_back_to_empty_file_when_neither_template_exists(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    env_path = tmp_path / "home" / ".env"

    source = _seed_missing_env(env_path, project_root)

    assert env_path.is_file()
    assert env_path.read_text(encoding="utf-8") == ""
    assert "empty" in source.lower()


class TestDoctorFixThenDoctorEndToEnd:
    """Real subprocess, real `hermes doctor` CLI entrypoint, real venv
    python -- not a reimplementation and not a mock of run_doctor()'s
    internals. Reproduces the exact sequence a customer hits: a fresh
    machine with no .env, `doctor --fix` seeds it from the curated
    template, and the immediately-following `doctor` run must say the
    provider key is still missing (nothing in the template is a real
    secret -- the customer hasn't typed anything in yet)."""

    def _run_doctor(self, hermes_home: Path, *extra_args: str) -> subprocess.CompletedProcess:
        import os

        env = dict(os.environ)
        env["HERMES_HOME"] = str(hermes_home)
        return subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "doctor", *extra_args],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_fix_then_doctor_reports_missing_api_key(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()

        fix_result = self._run_doctor(hermes_home, "--fix")
        assert fix_result.returncode in (0, 1), (
            f"stdout: {fix_result.stdout}\nstderr: {fix_result.stderr}"
        )
        env_path = hermes_home / ".env"
        assert env_path.is_file(), "doctor --fix must have created .env"
        assert "OPENROUTER_API_KEY=" in env_path.read_text(encoding="utf-8"), (
            "doctor --fix did not seed the curated trix.env.example template"
        )

        doctor_result = self._run_doctor(hermes_home)
        assert "No API key found" in doctor_result.stdout, (
            "doctor must report the missing API key on a freshly-seeded, "
            "unfilled .env -- got:\n" + doctor_result.stdout
        )
        assert "API key or custom endpoint configured" not in doctor_result.stdout, (
            "doctor reported a false-positive green checkmark on an .env "
            "with no real secret in it -- got:\n" + doctor_result.stdout
        )
