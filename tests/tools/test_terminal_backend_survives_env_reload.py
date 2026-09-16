"""Решение о песочнице нельзя снять перечитыванием ``.env``.

``terminal.backend: docker`` в ``config.yaml`` — решение по безопасности:
команды агента исполняются в контейнере, а не на хосте. Само значение
доезжает до ``tools/terminal_tool.py`` через переменную окружения
``TERMINAL_ENV`` (мост ``config.yaml`` → env), но в ``.env`` этой переменной
нет и не должно быть — источник истины именно ``config.yaml``.

``reload_env()`` удаляет из ``os.environ`` известные Hermes-переменные,
которых нет в ``.env``. Для переменной, чей источник истины — ``.env``
(ключ провайдера, прокси) это правильно. Для переменной, которая является
зеркалом ``config.yaml``, это отмена решения владельца: после того как
клиент прислал секрет через ``secret_request`` (спека 19,
``secret_capture_gateway._save_and_classify`` зовёт ``reload_env()``),
``TERMINAL_ENV`` исчезала и следующая команда шла на хост.

Тесты ниже — контракты этого класса переменных, а не снимок значений:
набор «переменных, которые ``reload_env()`` вправе удалить И которые
являются зеркалом ``terminal.*``» вычисляется из самих реестров, так что
новая ключ-переменная того же рода попадает под проверку автоматически.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


def _seed_home(tmp_path, monkeypatch, config_yaml: str, env_text: str):
    """Профиль на диске: config.yaml с решением, .env без TERMINAL_*."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(config_yaml, encoding="utf-8")
    (home / ".env").write_text(env_text, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def docker_home(tmp_path, monkeypatch):
    """Шлюз стартовал с ``terminal.backend: docker`` и смостил его в env."""
    _seed_home(
        tmp_path,
        monkeypatch,
        "terminal:\n  backend: docker\n  cwd: /workspace\n",
        "TAVILY_API_KEY=tvly-old\n",
    )
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CWD", "/workspace")
    return tmp_path / ".hermes"


class TestReloadEnvKeepsConfigOwnedTerminalKeys:
    def test_backend_survives_reload_env(self, docker_home):
        from hermes_cli.config import reload_env

        reload_env()

        assert os.environ.get("TERMINAL_ENV") == "docker", (
            "reload_env() сняла решение о песочнице: .env не источник истины "
            "для terminal.backend, но перечитывание .env удалило TERMINAL_ENV"
        )

    def test_every_config_owned_terminal_key_survives_reload_env(
        self, tmp_path, monkeypatch
    ):
        """Инвариант по классу, а не по одному имени.

        Любая переменная, которая одновременно (а) является зеркалом ключа
        ``terminal.*`` из ``config.yaml`` и (б) числится известной для
        ``reload_env()``, обязана пережить перечитывание ``.env``.
        """
        from hermes_cli.config import (
            OPTIONAL_ENV_VARS,
            TERMINAL_CONFIG_ENV_MAP,
            _EXTRA_ENV_KEYS,
            reload_env,
        )

        known = set(OPTIONAL_ENV_VARS) | set(_EXTRA_ENV_KEYS)
        env_var_to_cfg_key = {v: k for k, v in TERMINAL_CONFIG_ENV_MAP.items()}
        at_risk = sorted(set(env_var_to_cfg_key) & known)
        assert at_risk, "ожидались зеркала terminal.* среди известных ключей"

        values = {env_var: f"value-{env_var}" for env_var in at_risk}
        cfg_lines = ["terminal:"]
        for env_var in at_risk:
            cfg_lines.append(f"  {env_var_to_cfg_key[env_var]}: {values[env_var]}")
        _seed_home(
            tmp_path, monkeypatch, "\n".join(cfg_lines) + "\n", "TAVILY_API_KEY=tvly\n"
        )
        for env_var, value in values.items():
            monkeypatch.setenv(env_var, value)

        reload_env()

        for env_var, value in values.items():
            assert os.environ.get(env_var) == value, (
                f"{env_var} — зеркало config.yaml, но перечитывание .env его "
                "потеряло"
            )

    def test_env_only_key_is_still_removed(self, tmp_path, monkeypatch):
        """Сторож обратной стороны: переменная, чей источник истины — .env,
        по-прежнему удаляется при удалении из файла (спека 17, Ruling 3)."""
        from hermes_cli.config import reload_env

        _seed_home(
            tmp_path,
            monkeypatch,
            "terminal:\n  backend: docker\n",
            "OTHER=keep\n",
        )
        monkeypatch.setenv("HTTPS_PROXY", "http://dead-proxy.example:8080")

        reload_env()

        assert "HTTPS_PROXY" not in os.environ
        assert os.environ.get("TERMINAL_ENV") == "docker"


class TestTerminalToolResolvesBackendAfterEnvWipe:
    """Сторож на стороне инструмента: даже если переменную снесли уже после
    того, как мост отработал, бэкенд обязан вернуться из config.yaml.

    ``_ensure_terminal_env_bridged()`` — одноразовый: после первого вызова
    он больше никогда не восстанавливал ``TERMINAL_ENV``, поэтому любой,
    кто чистит окружение, тихо понижал песочницу до хоста.
    """

    def test_backend_is_docker_after_terminal_env_disappears(self, docker_home):
        from tools import terminal_tool

        terminal_tool._terminal_config_bridge_attempted = False
        assert terminal_tool._get_env_config()["env_type"] == "docker"

        # Кто-то (reload_env, чужой лаунчер, ручной unset) снёс переменную.
        del os.environ["TERMINAL_ENV"]

        assert terminal_tool._get_env_config()["env_type"] == "docker", (
            "инструмент терминала выбрал хост после того, как переменную "
            "стёрли из окружения — решение config.yaml должно перевесить"
        )


class TestSecretCaptureDoesNotDowngradeSandbox:
    """Живая цепочка с клиентской машины: клиент прислал ключ в мессенджер →
    ``_save_and_classify`` зовёт ``reload_env()`` → следующая команда агента
    уходила на хост."""

    def test_tool_key_capture_keeps_docker_backend(self, docker_home, monkeypatch):
        from tools import secret_capture_gateway as scg
        from tools import terminal_tool

        with scg._lock:
            scg._entries.clear()
            scg._session_index.clear()
            scg._notify_cbs.clear()
        terminal_tool._terminal_config_bridge_attempted = False
        assert terminal_tool._get_env_config()["env_type"] == "docker"

        scg.register("req-sb", "sess-sb", "TAVILY_API_KEY", "web search")
        with patch("hermes_cli.config.save_env_value_secure"):
            outcome = scg.resolve_secret_reply("sess-sb", "tvly-new")

        assert outcome["provider"] is False
        assert outcome["needs_restart"] is False
        assert os.environ.get("TERMINAL_ENV") == "docker"
        assert terminal_tool._get_env_config()["env_type"] == "docker", (
            "приём секрета от клиента вывел агента из песочницы на хост"
        )
