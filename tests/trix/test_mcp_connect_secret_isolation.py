"""Дефект 1 (ревью на Opus): MCP-токен не должен уезжать в песочницу.

Спека 19 сознательно кладёт имя захваченного ключа ИНСТРУМЕНТА в набор
проброса (`tools/env_passthrough.py::record_captured_passthrough`), потому
что агент зовёт такой ключ из `terminal`, то есть ИЗ КОНТЕЙНЕРА
(``tests/tools/test_captured_secret_reaches_the_sandbox.py`` держит это
поведение). `tools/mcp_connect_gateway.py` (спека 20) переиспользует тот же
примитив (`tools.secret_capture_gateway.request_secret`) для токена
MCP-сервера — но этот токен агенту в контейнере не нужен НИ ДЛЯ ЧЕГО, им
пользуется только хостовый MCP-клиент шлюза. Без явного отказа модель могла
бы вызвать `terminal("echo $BITRIX_TOKEN")` и получить значение обратно в
транскрипт — ровно то, что спека 20 §5.1 обещает не допускать.

Тест держит обе стороны одним и тем же путём (`resolve_secret_reply`):
токен, захваченный через ``mcp_connect_gateway.connect_mcp_server``, НЕ
попадает в набор проброса; обычный захват ключа инструмента (путь спеки 19,
без `mcp_connect`) — по-прежнему попадает, поведение по умолчанию не
изменилось.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def _clear_mcp_connect_state():
    from tools import mcp_connect_gateway as mcg

    with mcg._lock:
        mcg._notify_cbs.clear()
        mcg._pending_restart.clear()


def _clear_secret_capture_state():
    from tools import secret_capture_gateway as scg

    with scg._lock:
        scg._entries.clear()
        scg._session_index.clear()
        scg._notify_cbs.clear()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Свой HERMES_HOME и чистый набор проброса на каждый тест — тот же
    приём, что в ``tests/tools/test_captured_secret_reaches_the_sandbox.py``.
    """
    from tools.env_passthrough import clear_env_passthrough

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _clear_mcp_connect_state()
    _clear_secret_capture_state()
    clear_env_passthrough()

    import tools.env_passthrough as ep

    monkeypatch.setattr(ep, "_config_passthrough", None)
    yield home
    _clear_mcp_connect_state()
    _clear_secret_capture_state()
    clear_env_passthrough()


def _auth_error(status: int = 401):
    import httpx

    request = httpx.Request("GET", "https://example.invalid/mcp")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("auth required", request=request, response=response)


class TestMcpTokenNeverPassesThrough:
    def test_mcp_connect_captured_token_is_not_registered_as_passthrough(self):
        from tools.env_passthrough import is_env_passthrough
        from tools.mcp_connect_gateway import connect_mcp_server
        from tools import secret_capture_gateway as scg

        session_key = "telegram:mcp-owner"
        token_value = "sk-bitrix-mcp-token"

        def _notify(entry):
            with patch("hermes_cli.config.save_env_value_secure"):
                scg.resolve_secret_reply(entry.session_key, token_value)

        scg.register_notify(session_key, _notify)

        def _fake_probe(name, config, *args, **kwargs):
            if "headers" not in config:
                raise _auth_error(401)
            return [("crm_lead_create", "create a lead")]

        with patch("hermes_cli.mcp_config._probe_single_server", side_effect=_fake_probe):
            result = connect_mcp_server(
                session_key=session_key,
                name="bitrix",
                url="https://bitrix.example.invalid/mcp",
                purpose="Bitrix24 CRM integration",
                secret_env_var="BITRIX_TOKEN",
            )

        assert result["success"] is True

        # The load-bearing assertion: the MCP token's env var name must NOT
        # be reachable to a sandboxed terminal/execute_code call.
        assert not is_env_passthrough("BITRIX_TOKEN"), (
            "MCP-токен попал в набор проброса — агент в контейнере сможет "
            "прочитать его через terminal(\"echo $BITRIX_TOKEN\")"
        )

    def test_mcp_connect_captured_token_is_not_persisted_to_the_sidecar(self, isolated):
        """Тот же запрет обязан пережить рестарт — sidecar-файл не должен
        получить имя MCP-токена, иначе после перезапуска шлюза проброс
        включится сам собой."""
        from tools.env_passthrough import CAPTURED_PASSTHROUGH_FILENAME
        from tools.mcp_connect_gateway import connect_mcp_server
        from tools import secret_capture_gateway as scg

        session_key = "telegram:mcp-owner-2"

        def _notify(entry):
            with patch("hermes_cli.config.save_env_value_secure"):
                scg.resolve_secret_reply(entry.session_key, "sk-another-mcp-token")

        scg.register_notify(session_key, _notify)

        def _fake_probe(name, config, *args, **kwargs):
            if "headers" not in config:
                raise _auth_error(401)
            return [("t", "d")]

        with patch("hermes_cli.mcp_config._probe_single_server", side_effect=_fake_probe):
            result = connect_mcp_server(
                session_key=session_key,
                name="linear",
                url="https://linear.example.invalid/mcp",
                purpose="Linear issue tracking",
                secret_env_var="LINEAR_TOKEN",
            )

        assert result["success"] is True

        path = isolated / CAPTURED_PASSTHROUGH_FILENAME
        names = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        assert "LINEAR_TOKEN" not in names


class TestOrdinaryToolKeyPassthroughUnchanged:
    """Контроль: обычный захват ключа инструмента (спека 19, БЕЗ
    mcp_connect) обязан по-прежнему попадать в проброс — умолчание менять
    было нельзя."""

    def test_plain_secret_request_capture_still_passes_through(self):
        # НЕ TAVILY_API_KEY: это ключ провайдера поиска и он в
        # _HERMES_PROVIDER_ENV_BLOCKLIST (через OPTIONAL_ENV_VARS,
        # category="tool") — блокируется по имени вне зависимости от этой
        # правки, см. tests/tools/test_captured_secret_reaches_the_sandbox.py.
        # Свободное, некаталожное имя — ровно то, что шлёт клиент.
        from tools import secret_capture_gateway as scg
        from tools.env_passthrough import is_env_passthrough

        scg.register("req-plain", "sess-plain", "BITRIX24_WEBHOOK_URL", "webhook")
        with patch("hermes_cli.config.save_env_value_secure"):
            outcome = scg.resolve_secret_reply(
                "sess-plain", "https://example.bitrix24.ru/hook"
            )

        assert outcome["success"] is True
        assert outcome["provider"] is False
        assert is_env_passthrough("BITRIX24_WEBHOOK_URL"), (
            "обычный ключ инструмента перестал пробрасываться — это "
            "регрессия спеки 19, а не про MCP"
        )
