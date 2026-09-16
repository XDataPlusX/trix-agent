"""Tests for the ``secret_request`` tool (tools/secret_request_tool.py)."""

from __future__ import annotations

import json
from unittest.mock import patch

from tools.secret_request_tool import secret_request_tool


def _clear_capture_state():
    from tools import secret_capture_gateway as scg

    with scg._lock:
        scg._entries.clear()
        scg._session_index.clear()
        scg._notify_cbs.clear()


class TestValidation:
    def test_missing_env_var_is_a_tool_error(self):
        result = json.loads(secret_request_tool(env_var="", purpose="web search"))
        assert result.get("success") is False or "error" in result

    def test_invalid_env_var_name_is_a_tool_error(self):
        result = json.loads(
            secret_request_tool(env_var="not a valid name!", purpose="web search")
        )
        assert result.get("success") is False or "error" in result

    def test_missing_purpose_is_a_tool_error(self):
        result = json.loads(secret_request_tool(env_var="TAVILY_API_KEY", purpose=""))
        assert result.get("success") is False or "error" in result


class TestNoSession:
    def test_unavailable_outside_a_messaging_session(self):
        with patch("gateway.session_context.get_session_env", return_value=""):
            result = json.loads(
                secret_request_tool(env_var="TAVILY_API_KEY", purpose="web search")
            )
        assert result.get("success") is False or "error" in result

    def test_the_refusal_tells_the_model_what_to_do_instead(self):
        """Отказ читает МОДЕЛЬ, и от его формулировки зависит, что увидит клиент.

        Наблюдение на живой модели 2026-09-08: прежний текст («доступно
        только внутри живой сессии») был прочитан как «не тот контекст,
        попробуй иначе». Агент пошёл щупать терминал, искать `.env`, а
        клиенту рассказал про свою песочницу и «сессию в полу-битом
        состоянии» — ровно то, что скилл запрещает.

        Проверяется договор, а не формулировка: отказ обязан запретить
        поиск обходных путей, запретить пересказ устройства клиенту и
        назвать работающий путь — мастер настройки.
        """
        with patch("gateway.session_context.get_session_env", return_value=""):
            result = json.loads(
                secret_request_tool(env_var="TAVILY_API_KEY", purpose="web search")
            )
        text = json.dumps(result, ensure_ascii=False).lower()
        assert "stop" in text, "отказ не велит остановиться"
        assert "/setup" in text, "отказ не называет работающий путь"
        assert "probe" in text, "отказ не запрещает щупать окружение"
        assert ".env" in text, "отказ не запрещает искать .env"
        assert "describe your environment" in text, (
            "отказ не запрещает пересказывать клиенту своё устройство"
        )


class TestFreeFormNameEndToEnd:
    def setup_method(self):
        _clear_capture_state()

    def test_free_form_env_var_name_is_accepted_and_never_leaks(self):
        """(g) — a name outside the wizard catalog works the same way, and
        the tool's JSON result never carries the secret value."""
        from tools import secret_capture_gateway as scg

        session_key = "telegram:owner-chat"
        secret_value = "https://example.bitrix24.ru/rest/1/leak-me-not/"

        def _notify(entry):
            with patch("hermes_cli.config.save_env_value_secure"):
                scg.resolve_secret_reply(entry.session_key, secret_value)

        scg.register_notify(session_key, _notify)

        with patch("gateway.session_context.get_session_env", return_value=session_key):
            raw = secret_request_tool(env_var="BITRIX_WEBHOOK", purpose="Bitrix24 integration")

        assert secret_value not in raw
        result = json.loads(raw)
        assert result["env_var"] == "BITRIX_WEBHOOK"
        assert result["success"] is True
        assert "value" not in result
