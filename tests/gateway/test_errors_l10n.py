"""Task 7: localize gateway errors, empty-response handling, and session info.

Covers ``trix.errors.*`` -- the strings a Trix Agent customer sees when
something goes wrong: provider errors, empty/dropped agent responses,
HTTP-status hints, session-info summaries, and context-compression notices.

Each pair asserts the same call is byte-identical under ``en`` (the literal
that used to live in the code) and carries real Russian text under ``ru``.
``tests/gateway/conftest.py`` pins ``HERMES_LANGUAGE=en`` via an autouse
fixture, so the ``ru`` half of every pair pins its own language explicitly.
"""

from __future__ import annotations

import types

import pytest

from agent import i18n
from gateway.run import (
    GatewayRunner,
    TurnRunner,
    _gateway_provider_error_reply,
    _gateway_unexpected_error_reply,
    _normalize_empty_agent_response,
)


@pytest.fixture(autouse=True)
def _reset_i18n_after():
    yield
    i18n.reset_language_cache()


# ---------------------------------------------------------------------------
# _gateway_provider_error_reply
# ---------------------------------------------------------------------------


class TestProviderErrorReply:
    # trix.errors.provider.auth_failed points a Trix client at the setup
    # wizard (the only place they can act), not at gateway logs they can't
    # see -- so this checks the invariant (mentions the key, points at the
    # wizard), not a byte-identical sentence. See task-1-report.md, round 1.
    def test_auth_failure_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _gateway_provider_error_reply("401 invalid api key").lower()
        assert "key" in result
        assert "wizard" in result

    def test_auth_failure_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _gateway_provider_error_reply("401 invalid api key").lower()
        assert "ключ" in result
        assert "мастер" in result

    # trix.errors.provider.policy_rejected no longer tells a Trix client to
    # "check gateway logs" -- they have no access to them (round 2, item 4).
    # It says what happened (a safety filter) and the one action available
    # (rephrase), so this checks that invariant instead of a byte-identical
    # sentence.
    def test_policy_rejected_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _gateway_provider_error_reply("policy violation detected").lower()
        assert "safety filter" in result
        assert "gateway logs" not in result

    def test_policy_rejected_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _gateway_provider_error_reply("policy violation detected").lower()
        assert "отклонил" in result
        assert "логах" not in result

    def test_rate_limited_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert _gateway_provider_error_reply("429 rate limit exceeded") == (
            "⏱️ The model provider is rate-limiting requests. Please wait a "
            "moment and try again."
        )

    # An empty balance (HTTP 402) must win over the rate-limit branch even
    # when the provider's own error body also contains "quota" -- some
    # providers phrase a depleted-balance rejection as "quota exceeded:
    # insufficient credits". Before the billing check existed, that body
    # landed in rate_limited ("wait a moment and try again"), which is a
    # direct lie to a client whose balance is empty: waiting changes
    # nothing until it's topped up. This pins the ORDER, not just the
    # existence of a billing branch -- a regression that moved the billing
    # check after the rate-limit check would still pass a billing-only
    # input but fail this one.
    def test_billing_wins_over_rate_limit_when_both_patterns_are_present_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _gateway_provider_error_reply(
            "402 payment required: quota exceeded, insufficient credits"
        ).lower()
        assert "fund" in result or "credit" in result or "balance" in result
        assert "wait a moment" not in result

    def test_billing_wins_over_rate_limit_when_both_patterns_are_present_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _gateway_provider_error_reply(
            "402 payment required: quota exceeded, insufficient credits"
        ).lower()
        assert "средства" in result or "баланс" in result
        assert "подождите" not in result

    def test_billing_alone_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _gateway_provider_error_reply("insufficient balance on account").lower()
        assert "fund" in result or "credit" in result or "balance" in result

    def test_billing_alone_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _gateway_provider_error_reply("insufficient balance on account").lower()
        assert "средства" in result or "баланс" in result

    def test_rate_limited_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _gateway_provider_error_reply("429 rate limit exceeded").lower()
        assert "частоту запросов" in result

    # trix.errors.provider.generic_failed: same fix as policy_rejected above
    # -- no "check gateway logs", just an honest "this is on their end, try
    # again" (round 2, item 4).
    def test_generic_failure_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _gateway_provider_error_reply("internal server hiccup").lower()
        assert "try again" in result
        assert "gateway logs" not in result

    def test_generic_failure_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _gateway_provider_error_reply("internal server hiccup").lower()
        assert "провайдер" in result
        assert "логах" not in result


# ---------------------------------------------------------------------------
# _normalize_empty_agent_response
# ---------------------------------------------------------------------------


class TestNormalizeEmptyAgentResponse:
    def test_not_processed_retry_hint_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _normalize_empty_agent_response({"api_calls": 0}, "")
        assert result == (
            "⚠️ Your message wasn't processed (the previous turn was still "
            "being cleaned up). Please send it again."
        )

    def test_not_processed_retry_hint_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _normalize_empty_agent_response({"api_calls": 0}, "")
        assert "отправьте" in result.lower()

    def test_interrupted_before_start_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _normalize_empty_agent_response(
            {"api_calls": 0, "interrupted": True}, ""
        )
        assert result == (
            "⚠️ Your message was interrupted before processing started "
            "(likely by a recent /stop). Please send it again."
        )

    def test_interrupted_before_start_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _normalize_empty_agent_response(
            {"api_calls": 0, "interrupted": True}, ""
        )
        assert "прервана" in result.lower()

    def test_session_too_large_via_failed_context_error_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _normalize_empty_agent_response(
            {"failed": True, "error": "context length exceeded"}, ""
        )
        assert result == (
            "⚠️ Session too large for the model's context window.\n"
            "Use /compact to compress the conversation, or "
            "/reset to start fresh."
        )

    def test_session_too_large_via_failed_context_error_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _normalize_empty_agent_response(
            {"failed": True, "error": "context length exceeded"}, ""
        )
        assert "контекстного окна" in result.lower()

    def test_no_response_generated_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _normalize_empty_agent_response({"api_calls": 3}, "")
        assert result == (
            "⚠️ Processing completed but no response was generated. "
            "This may be a transient error — try sending your message again."
        )

    def test_no_response_generated_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _normalize_empty_agent_response({"api_calls": 3}, "")
        assert "не был сформирован" in result.lower() or "не сформирован" in result.lower()


# ---------------------------------------------------------------------------
# TurnRunner.run_sync -- provider-resolution exception at the top of a turn
# ---------------------------------------------------------------------------


def _make_turn_runner(*, raise_exc: Exception):
    """Bare TurnRunner whose provider-runtime resolution always raises.

    Only the ``run_sync`` prelude before the try/except matters here, so the
    fake ``_runner`` stubs exactly what that prelude touches.
    """
    from gateway.config import Platform
    from gateway.session import SessionSource

    class _FakeRunner:
        def _resolve_session_agent_runtime(self, **kwargs):
            raise raise_exc

        def _get_system_prompt_for_channel(self, *args, **kwargs):
            return ""

    tr = object.__new__(TurnRunner)
    tr._runner = _FakeRunner()
    tr._ctx = types.SimpleNamespace(
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="1"),
        context_prompt="",
        channel_prompt="",
        session_key="s",
        user_config={},
    )
    return tr


class TestRunSyncProviderAuthException:
    def test_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        tr = _make_turn_runner(raise_exc=RuntimeError("bad credentials"))
        result = tr.run_sync()
        assert result["final_response"] == "⚠️ Provider authentication failed: bad credentials"

    def test_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        tr = _make_turn_runner(raise_exc=RuntimeError("bad credentials"))
        result = tr.run_sync()
        assert "аутентиф" in result["final_response"].lower()
        assert "bad credentials" in result["final_response"]


# ---------------------------------------------------------------------------
# GatewayRunner._format_session_info -- session config surfacing
# ---------------------------------------------------------------------------


def _patch_info(tmp_path, config_yaml, model, runtime, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    if config_yaml is not None:
        cfg_path.write_text(config_yaml)
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda: model)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: runtime)


class TestFormatSessionInfoL10n:
    def test_config_context_en(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        _patch_info(
            tmp_path,
            "model:\n  default: test-model\n  context_length: 32768\n",
            "test-model",
            {"provider": "custom", "base_url": "", "api_key": ""},
            monkeypatch,
        )
        runner = GatewayRunner.__new__(GatewayRunner)
        info = runner._format_session_info()
        assert "◆ Model: `test-model`" in info
        assert "◆ Context: 32K tokens (config)" in info

    def test_config_context_ru(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        _patch_info(
            tmp_path,
            "model:\n  default: test-model\n  context_length: 32768\n",
            "test-model",
            {"provider": "custom", "base_url": "", "api_key": ""},
            monkeypatch,
        )
        runner = GatewayRunner.__new__(GatewayRunner)
        info = runner._format_session_info()
        assert "◆ Модель: `test-model`" in info
        assert "32K" in info
        assert "конфига" in info

    def test_default_fallback_en(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        _patch_info(
            tmp_path,
            "model:\n  default: unknown-model-xyz\n",
            "unknown-model-xyz",
            {"provider": "", "base_url": "", "api_key": ""},
            monkeypatch,
        )
        runner = GatewayRunner.__new__(GatewayRunner)
        info = runner._format_session_info()
        assert "256K" in info
        assert "model.context_length" in info
        assert "default — set model.context_length in config to override" in info

    def test_default_fallback_ru(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        _patch_info(
            tmp_path,
            "model:\n  default: unknown-model-xyz\n",
            "unknown-model-xyz",
            {"provider": "", "base_url": "", "api_key": ""},
            monkeypatch,
        )
        runner = GatewayRunner.__new__(GatewayRunner)
        info = runner._format_session_info()
        assert "256K" in info
        assert "model.context_length" in info
        assert "по умолчанию" in info

    def test_local_endpoint_en(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        _patch_info(
            tmp_path,
            "model:\n  default: qwen3:8b\n  provider: custom\n  base_url: http://localhost:11434/v1\n  context_length: 8192\n",
            "qwen3:8b",
            {"provider": "custom", "base_url": "http://localhost:11434/v1", "api_key": ""},
            monkeypatch,
        )
        runner = GatewayRunner.__new__(GatewayRunner)
        info = runner._format_session_info()
        assert "◆ Endpoint: http://localhost:11434/v1" in info

    def test_local_endpoint_ru(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        _patch_info(
            tmp_path,
            "model:\n  default: qwen3:8b\n  provider: custom\n  base_url: http://localhost:11434/v1\n  context_length: 8192\n",
            "qwen3:8b",
            {"provider": "custom", "base_url": "http://localhost:11434/v1", "api_key": ""},
            monkeypatch,
        )
        runner = GatewayRunner.__new__(GatewayRunner)
        info = runner._format_session_info()
        assert "◆ Эндпоинт: http://localhost:11434/v1" in info


# ---------------------------------------------------------------------------
# /suggestions and /blueprint command-failure replies
# ---------------------------------------------------------------------------


class _FakeSource:
    platform = None
    chat_id = None
    chat_name = None
    thread_id = None


def _make_command_event(args=""):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource

    return MessageEvent(
        text=f"/suggestions {args}".strip(),
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="1"),
    )


class TestCommandFailureReplies:
    @pytest.mark.asyncio
    async def test_suggestions_failed_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        import hermes_cli.suggestions_cmd as suggestions_cmd

        def _boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(suggestions_cmd, "handle_suggestions_command", _boom)
        runner = GatewayRunner.__new__(GatewayRunner)
        result = await runner._handle_suggestions_command(_make_command_event())
        assert result == "Suggestions command failed: kaboom"

    @pytest.mark.asyncio
    async def test_suggestions_failed_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        import hermes_cli.suggestions_cmd as suggestions_cmd

        def _boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(suggestions_cmd, "handle_suggestions_command", _boom)
        runner = GatewayRunner.__new__(GatewayRunner)
        result = await runner._handle_suggestions_command(_make_command_event())
        assert "команда suggestions" in result.lower()
        assert "kaboom" in result

    @pytest.mark.asyncio
    async def test_blueprint_failed_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        import hermes_cli.blueprint_cmd as blueprint_cmd

        def _boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(blueprint_cmd, "handle_blueprint_command", _boom)
        runner = GatewayRunner.__new__(GatewayRunner)
        result = await runner._handle_blueprint_command(_make_command_event())
        assert result.text == "Cron blueprint command failed: kaboom"

    @pytest.mark.asyncio
    async def test_blueprint_failed_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        import hermes_cli.blueprint_cmd as blueprint_cmd

        def _boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(blueprint_cmd, "handle_blueprint_command", _boom)
        runner = GatewayRunner.__new__(GatewayRunner)
        result = await runner._handle_blueprint_command(_make_command_event())
        assert "команда blueprint" in result.text.lower()
        assert "kaboom" in result.text


# ---------------------------------------------------------------------------
# Catalog-level coverage for the HTTP-status hints, the unexpected-error
# wrapper and the context-compression notices. These exercise the same real
# agent.i18n.t() + locales/*.yaml path the gateway call sites use, proving
# the catalog entries exist, resolve, and format correctly in both languages.
#
# The status-hint call sites themselves used to be unreachable from a test --
# they lived inline in _handle_message_with_agent's except handler. Round 4
# lifted them into _gateway_unexpected_error_reply(); TestUnexpectedErrorReply
# below drives that function directly, including with an unreadable catalog
# so the default= literals are exercised too.
# ---------------------------------------------------------------------------


class TestStatusHintCatalogEntries:
    def test_unauthorized_hint_en_points_at_the_wizard_not_a_local_file(self, monkeypatch):
        # Round-4 review: the Russian entry was moved off ~/.hermes/.env in
        # round 3 but English was left behind, so an en-locale Trix client
        # (and any ru client whose catalog failed to load and fell through to
        # English) was still being sent to a file they cannot open. Same
        # invariant as the ru test below, not a byte-identical sentence.
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = i18n.t("trix.errors.status_hint.unauthorized")
        assert ".env" not in result
        assert "~/.hermes" not in result
        assert "wizard" in result.lower()

    def test_unauthorized_hint_ru_does_not_mention_claude_code(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.errors.status_hint.unauthorized")
        assert "claude" not in result.lower()

    def test_unauthorized_hint_ru_points_at_the_wizard_not_a_local_file(self, monkeypatch):
        # Round-3 review: a Telegram-only Trix client cannot reach
        # ~/.hermes/.env on the VPS -- pointing there is the same class of
        # unreachable-advice defect Task 3 already fixed twice elsewhere.
        # The setup wizard (/setup) is the real, Telegram-reachable way to
        # change the provider key.
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.errors.status_hint.unauthorized")
        assert ".env" not in result
        assert "~/.hermes" not in result
        assert "мастер" in result.lower()

    def test_usage_limit_resets_in_formats_hours_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = i18n.t("trix.errors.status_hint.usage_limit_resets_in", hours=3)
        assert result == " Your plan's usage limit has been reached. It resets in ~3h."

    def test_usage_limit_resets_in_formats_hours_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.errors.status_hint.usage_limit_resets_in", hours=3)
        assert "3" in result and "лимит" in result.lower()

    def test_unexpected_error_wrapper_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = i18n.t("trix.errors.unexpected_error", status_hint="")
        assert result == (
            "Sorry, I encountered an unexpected error.\n"
            "Try again or use /reset to start a fresh session."
        )

    def test_unexpected_error_wrapper_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.errors.unexpected_error", status_hint="")
        assert "ошибка" in result.lower()


class TestUnexpectedErrorReply:
    """``_gateway_unexpected_error_reply`` -- the reply a client gets when an
    exception escapes the agent turn.

    Round-4 review: these branches were inline in
    ``_handle_message_with_agent``'s ``except`` handler, unreachable from any
    test, and one of them (401) was still telling a Telegram-only client to
    check ``~/.hermes/.env``. The literal mattered twice over: it is what a
    ru-locale client reads whenever the catalog fails to load, which is the
    single scenario every ``default=`` in this codebase exists to cover.
    """

    class _Err(Exception):
        def __init__(self, status_code, body=None):
            super().__init__(f"http {status_code}")
            self.status_code = status_code
            if body is not None:
                self.response = types.SimpleNamespace(json=lambda: body)

    def test_401_points_at_the_wizard_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _gateway_unexpected_error_reply(self._Err(401))
        assert ".env" not in result
        assert "~/.hermes" not in result
        assert "мастер" in result.lower()

    def test_401_points_at_the_wizard_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _gateway_unexpected_error_reply(self._Err(401))
        assert ".env" not in result
        assert "~/.hermes" not in result
        assert "wizard" in result.lower()

    def test_401_default_literal_survives_an_unreadable_catalog(self, monkeypatch):
        """The scenario ``default=`` exists for: agent.i18n._load_catalog
        swallows any read/parse error and caches an EMPTY catalog for the
        process, so every t() call in this function falls through to its
        literal. That literal is what the client reads -- it must carry the
        same advice as the catalog entry, not point at a file on the VPS.
        """
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        monkeypatch.setattr(i18n, "_load_catalog", lambda lang: {})
        result = _gateway_unexpected_error_reply(self._Err(401))
        assert "trix.errors" not in result, f"leaked a catalog key path: {result!r}"
        assert ".env" not in result, f"unreachable advice in the default literal: {result!r}"
        assert "~/.hermes" not in result
        assert "wizard" in result.lower()

    def test_402_names_the_balance_not_the_key(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _gateway_unexpected_error_reply(self._Err(402)).lower()
        assert "баланс" in result or "квота" in result
        assert "мастер" not in result

    def test_429_usage_limit_reports_the_reset_window(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        err = self._Err(
            429,
            {"error": {"type": "usage_limit_reached", "resets_in_seconds": 7200}},
        )
        result = _gateway_unexpected_error_reply(err)
        assert "2h" in result
        assert "rate-limited" not in result.lower()

    def test_429_plain_rate_limit_is_not_the_usage_limit_wording(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _gateway_unexpected_error_reply(self._Err(429)).lower()
        assert "rate-limited" in result
        assert "usage limit" not in result

    def test_400_on_a_long_session_says_the_session_is_too_large(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = _gateway_unexpected_error_reply(self._Err(400), history_len=200)
        assert "/compact" in result

    def test_400_on_a_short_session_is_a_plain_rejection(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _gateway_unexpected_error_reply(self._Err(400), history_len=3)
        assert "/compact" not in result
        assert "rejected by the api" in result.lower()

    def test_unknown_status_carries_no_hint_and_never_leaks_the_exception(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = _gateway_unexpected_error_reply(RuntimeError("boom: sk-secret-token"))
        assert "boom" not in result
        assert "sk-secret-token" not in result
        assert "unexpected error" in result.lower()


# Advice a Telegram-only customer cannot act on. They have a chat and the web
# setup wizard; no shell, no config file, no log directory. A message naming
# any of these is a dead end dressed as help, and this product has shipped
# that defect five separate times — so it is asserted, not remembered.
UNREACHABLE_ADVICE = (
    "config.yaml",
    "auxiliary.",
    "~/.hermes",
    ".env",
    "hermes ",
    "gateway logs",
)


def assert_no_unreachable_advice(text: str) -> None:
    lowered = text.lower()
    offenders = [needle for needle in UNREACHABLE_ADVICE if needle in lowered]
    assert not offenders, (
        f"message tells a Telegram-only customer to touch {offenders}: {text!r}"
    )


class TestCompressionCatalogEntries:
    """What these messages must carry, in either language.

    Byte-exact English copy used to be asserted here. That froze the sentence
    and, worse, froze the wrong sentence: all three compression notices ended
    with "check your auxiliary.compression model configuration" — a config key
    the customer this product ships to cannot open, in a chat that is their
    only interface. The snapshot did not merely fail to catch that; it would
    have failed the fix.

    Asserted instead: the value the caller passed survives, the message says
    nothing was lost, it names the two commands the customer really has, and
    it sends them nowhere they cannot go.
    """

    def test_timeout_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = i18n.t("trix.errors.compression_timeout", seconds=12.3)
        assert "12.3" in result
        assert "/compress" in result and "/reset" in result
        assert "no messages were dropped" in result.lower()
        assert_no_unreachable_advice(result)

    def test_timeout_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.errors.compression_timeout", seconds=12.3)
        assert "12.3" in result
        assert "сжати" in result.lower()
        assert "/compress" in result and "/reset" in result
        assert "не удалено" in result.lower()
        assert_no_unreachable_advice(result)

    def test_aborted_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        result = i18n.t("trix.errors.compression_aborted", err="boom")
        assert "boom" in result
        assert "/compress" in result and "/reset" in result
        assert "no messages were dropped" in result.lower()
        assert_no_unreachable_advice(result)

    def test_aborted_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.errors.compression_aborted", err="boom")
        assert "boom" in result
        assert "прервано" in result.lower()
        assert "не удалено" in result.lower()
        assert_no_unreachable_advice(result)

    def test_aux_fallback_sends_the_customer_nowhere_they_cannot_go(self, monkeypatch):
        """The third of the three, and the one that named a config key twice.

        It used to end with "check `auxiliary.compression.model` in
        config.yaml" — and it fires on the HAPPY path, when compression
        silently recovered on the main model and nothing is actually wrong for
        the customer to fix.
        """
        for lang in ("en", "ru"):
            monkeypatch.setenv("HERMES_LANGUAGE", lang)
            i18n.reset_language_cache()
            result = i18n.t(
                "trix.errors.compression_aux_fallback", model="aux-1", err="boom"
            )
            assert "boom" in result
            assert_no_unreachable_advice(result)


class TestEmptyModelResponseCatalogEntry:
    def test_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.errors.empty_model_response") == (
            "⚠️ The model returned no response after processing tool results. "
            "This can happen with some models — try again or rephrase your "
            "question."
        )

    def test_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.errors.empty_model_response")
        assert "модель" in result.lower()
