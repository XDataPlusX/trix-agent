"""Клиент, вставивший токен вместе со словом Bearer, всё равно подключается.

Заголовок собирается как ``Authorization: Bearer ${ПЕРЕМЕННАЯ}``. Строку из
документации сервиса обычно копируют целиком, вместе со словом ``Bearer`` —
и сервер получает его дважды. У апстрима нормализация есть, но живёт на
пути, которому нужно сырое значение токена; механизм спеки 19 сырое
значение наружу не отдаёт, поэтому нормализуем уже сохранённое.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    return path


def _saved(env_var: str) -> str | None:
    from hermes_cli.config import load_env

    return (load_env() or {}).get(env_var)


def test_duplicate_prefix_is_removed(env_file):
    from hermes_cli.config import save_env_value
    from tools.mcp_connect_gateway import _normalize_saved_bearer_token

    save_env_value("MCP_DEMO_TOKEN", "Bearer abc.def.ghi")
    _normalize_saved_bearer_token("MCP_DEMO_TOKEN")

    assert _saved("MCP_DEMO_TOKEN") == "abc.def.ghi"


def test_a_clean_token_is_left_alone(env_file):
    from hermes_cli.config import save_env_value
    from tools.mcp_connect_gateway import _normalize_saved_bearer_token

    save_env_value("MCP_DEMO_TOKEN", "abc.def.ghi")
    _normalize_saved_bearer_token("MCP_DEMO_TOKEN")

    assert _saved("MCP_DEMO_TOKEN") == "abc.def.ghi"


def test_a_token_that_merely_starts_with_the_word_survives(env_file):
    """Срезается только префикс со пробелом, а не любое совпадение букв."""
    from hermes_cli.config import save_env_value
    from tools.mcp_connect_gateway import _normalize_saved_bearer_token

    save_env_value("MCP_DEMO_TOKEN", "bearerish-token-value")
    _normalize_saved_bearer_token("MCP_DEMO_TOKEN")

    assert _saved("MCP_DEMO_TOKEN") == "bearerish-token-value"


def test_missing_variable_is_not_an_error(env_file):
    from tools.mcp_connect_gateway import _normalize_saved_bearer_token

    _normalize_saved_bearer_token("MCP_NEVER_SAVED")

    assert _saved("MCP_NEVER_SAVED") is None


def test_an_unexpected_failure_reaches_the_model_as_a_clean_refusal(monkeypatch):
    """Сырое исключение не должно доходить до модели.

    Ниже по стеку — запись конфига и сторонний клиент MCP. Текст их
    исключений может нести и детали окружения, и подставленный заголовок с
    ключом, а формат ответа инструмента обязан оставаться одним и тем же.
    """
    import json
    from unittest.mock import patch

    import tools.mcp_connect_gateway as gw
    from tools.mcp_connect_tool import mcp_connect_tool

    def _boom(**kwargs):
        raise RuntimeError("Authorization: Bearer super-secret-value")

    monkeypatch.setattr(gw, "connect_mcp_server", _boom)

    with patch(
        "gateway.session_context.get_session_env", return_value="telegram:owner"
    ):
        raw = mcp_connect_tool(
            name="demo", url="https://example.invalid/mcp", purpose="тест"
        )
    payload = json.loads(raw)

    assert payload["success"] is False
    assert payload["reason"] == "unexpected_error"
    assert "super-secret-value" not in raw
