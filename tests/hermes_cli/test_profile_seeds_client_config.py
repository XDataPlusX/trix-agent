"""Свежесозданный профиль получает клиентский шаблон конфига.

Без него профиль остаётся вовсе без config.yaml и резолвится из
DEFAULT_CONFIG, где terminal.backend == "local" — то есть команды агента
выполняются на самой машине, с доступом к .env клиента. Решение по
безопасности (спека 4, Ruling 1) молча откатывалось при создании профиля.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_new_profile_gets_the_client_config(profile_env):
    from hermes_cli.profiles import create_profile

    profile_dir = create_profile("проба".encode("ascii", "ignore").decode() or "probe")
    config_path = profile_dir / "config.yaml"
    assert config_path.exists(), "профиль создан без config.yaml"

    created = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    assert created["terminal"]["backend"] == template["terminal"]["backend"]
    assert created["_config_version"] == template["_config_version"]


def test_russian_comments_survive_into_the_profile(profile_env):
    """Комментарии — единственная русская документация конфига; профиль
    обязан получить их, а не голый дамп значений."""
    from hermes_cli.profiles import create_profile

    profile_dir = create_profile("probe2")
    text = (profile_dir / "config.yaml").read_text(encoding="utf-8")
    assert "# Trix Agent — конфигурация." in text


def test_backend_is_never_local_in_a_fresh_profile(profile_env):
    """Инвариант-отношение: значение берётся из шаблона, а не из литерала,
    поэтому тест краснеет и если шаблон однажды вернётся к local."""
    from hermes_cli.profiles import create_profile

    profile_dir = create_profile("probe3")
    created = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert created["terminal"]["backend"] != "local"


def test_clone_still_wins_over_the_template(profile_env, tmp_path):
    """--clone копирует конфиг источника; шаблон не должен его перебивать."""
    from hermes_cli.profiles import create_profile

    source = profile_env / "config.yaml"
    source.write_text("terminal:\n  backend: ssh\n_config_version: 34\n", encoding="utf-8")

    profile_dir = create_profile("cloned", clone_config=True)
    created = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert created["terminal"]["backend"] == "ssh"


def test_broken_resolver_logs_a_warning_instead_of_silently_swallowing(
    profile_env, monkeypatch, caplog
):
    """A future regression in config_template.py must be VISIBLE.

    Before this test existed, both seeding blocks caught the resolver
    failure with a bare `except Exception: pass` — the profile looked
    successfully created (directory + SOUL.md present) while silently
    landing in exactly the pre-fix hole this task closes: no config.yaml,
    no .env, everything resolving from DEFAULT_CONFIG (terminal.backend
    "local", agent able to read the profile's .env). Simulate that
    regression by making both resolvers raise, and require a warning
    naming each failure so it cannot regress invisibly again.
    """
    import logging
    from hermes_cli import config_template
    from hermes_cli.profiles import create_profile

    def _boom(_install_dir):
        raise RuntimeError("simulated config_template regression")

    monkeypatch.setattr(config_template, "resolve_config_template", _boom)
    monkeypatch.setattr(config_template, "resolve_env_template", _boom)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.profiles"):
        profile_dir = create_profile("brokenresolver")

    # Profile creation must still succeed — an incomplete profile is worse
    # than one without the template.
    assert profile_dir.is_dir()
    assert not (profile_dir / "config.yaml").exists()
    assert not (profile_dir / ".env").exists()

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("config.yaml" in msg for msg in warnings), warnings
    assert any(".env" in msg for msg in warnings), warnings
