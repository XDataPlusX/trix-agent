"""Task: localize the remaining agent/conversation_loop.py status/final
lines that reach a Trix Telegram client, plus the two /compress replies in
agent/manual_compression_feedback.py.

Every case below DRIVES the real call site (``run_conversation`` end-to-end
where practical, the module function directly where the call site is a
standalone helper) in both ``en`` and ``ru`` and asserts the actual rendered
text, mirroring the pattern in tests/gateway/test_errors_l10n.py and
tests/run_agent/test_trix_billing_terminal_client_message.py. None of these
strings are checked against source text -- only against what the code
actually produced.

See tests/gateway/test_noisy_status_l10n.py for the companion guard proving
these lines (and their shipped translations) never start matching
``_TELEGRAM_NOISY_STATUS_RE`` -- if they did, the gateway would silently
swallow them instead of delivering them.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import i18n
from agent.manual_compression_feedback import describe_compression_lock_skip
from run_agent import AIAgent


@pytest.fixture(autouse=True)
def _reset_i18n_after():
    yield
    i18n.reset_language_cache()


def _set_lang(monkeypatch, lang: str) -> None:
    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    i18n.reset_language_cache()


# ---------------------------------------------------------------------------
# agent/manual_compression_feedback.py -- direct /compress replies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_compression_lock_busy_with_holder(monkeypatch, lang):
    _set_lang(monkeypatch, lang)
    text = describe_compression_lock_skip("worker-7")
    assert "worker-7" in text
    if lang == "ru":
        assert "исполнитель" in text
        assert "in progress" not in text
    else:
        assert "already running" in text or "already in progress" in text


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_compression_lock_unknown_holder(monkeypatch, lang):
    _set_lang(monkeypatch, lang)
    text = describe_compression_lock_skip(None)
    assert text
    if lang == "ru":
        assert "блокировку" in text
    else:
        assert "lock" in text.lower()


def test_compression_lock_messages_are_distinct():
    holder_text = describe_compression_lock_skip("worker-7")
    unknown_text = describe_compression_lock_skip(None)
    assert holder_text != unknown_text


# ---------------------------------------------------------------------------
# Shared AIAgent harness (mirrors tests/run_agent/test_trix_billing_terminal_
# client_message.py and tests/run_agent/test_trix_fallback_*.py)
# ---------------------------------------------------------------------------


def _make_agent(statuses: list, **overrides) -> AIAgent:
    kwargs = dict(
        api_key="test-key-1234567890",
        base_url="https://openrouter.ai/api/v1",
        provider="openrouter",
        api_mode="chat_completions",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        status_callback=lambda kind, message: statuses.append((kind, message)),
    )
    kwargs.update(overrides)
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(**kwargs)
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _lifecycle_texts(statuses: list) -> list[str]:
    return [msg for kind, msg in statuses if kind == "lifecycle"]


# ---------------------------------------------------------------------------
# Nous Portal rate-limit guard (agent/conversation_loop.py ~2507-2551)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_nous_rate_limit_no_fallback_is_localized_and_debranded(monkeypatch, lang):
    _set_lang(monkeypatch, lang)
    statuses: list = []
    agent = _make_agent(statuses, provider="nous")

    with (
        patch(
            "agent.nous_rate_guard.nous_rate_limit_remaining",
            return_value=120.0,
        ),
        patch(
            "agent.nous_rate_guard.format_remaining",
            return_value="2m",
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do the work")

    # Guard against a vacuous pass -- the client provider never gets called
    # at all when the rate-limit guard trips (that's the whole point of the
    # guard), so the only signal of a real run is the returned failure shape.
    assert result.get("failed") is True
    assert not agent.client.chat.completions.create.called

    final_response = result.get("final_response") or ""
    lifecycle_texts = _lifecycle_texts(statuses)
    assert lifecycle_texts, "клиент не получил статус вовсе"
    joined = "\n".join(lifecycle_texts) + "\n" + final_response

    # De-branding: upstream's literal ("Nous Portal rate limit active") must
    # never reach the client -- neither language.
    assert "Nous Portal" not in joined
    assert "Hermes" not in joined
    # Config-file advice is not actionable for a Telegram-only client.
    assert "config.yaml" not in joined

    if lang == "ru":
        assert "провайдер" in joined.lower()
        assert any("а" <= ch.lower() <= "я" for ch in joined)
    else:
        assert "provider" in joined.lower()


# ---------------------------------------------------------------------------
# Ollama runtime context too small (agent/conversation_loop.py ~2245-2262)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_ollama_context_too_small_status_is_localized(monkeypatch, lang):
    _set_lang(monkeypatch, lang)
    statuses: list = []
    agent = _make_agent(statuses, provider="ollama", base_url="http://localhost:11434/v1")
    agent.tools = [{"type": "function", "function": {"name": "terminal"}}]
    agent._ollama_num_ctx = 2048  # far below MINIMUM_CONTEXT_LENGTH

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do the work")

    assert result.get("failed") is True
    assert not agent.client.chat.completions.create.called

    lifecycle_texts = _lifecycle_texts(statuses)
    assert lifecycle_texts, "клиент не получил статус вовсе"
    joined = "\n".join(lifecycle_texts)
    assert "Hermes" not in joined
    if lang == "ru":
        assert any("а" <= ch.lower() <= "я" for ch in joined)
        assert "инструмент" in joined.lower()
    else:
        assert "tool" in joined.lower()


# ---------------------------------------------------------------------------
# HTTP 413 payload-too-large compression retry
# (agent/conversation_loop.py ~5100-5115)
# ---------------------------------------------------------------------------


def _make_413_error() -> Exception:
    err = Exception("Request entity too large")
    err.status_code = 413
    return err


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_payload_too_large_retry_status_is_localized(monkeypatch, lang):
    _set_lang(monkeypatch, lang)
    statuses: list = []
    agent = _make_agent(statuses)
    agent.compression_enabled = True
    agent.client.chat.completions.create.side_effect = _make_413_error()

    buffered: list = []
    orig_buffer_status = agent._buffer_status

    def capture_buffer_status(message):
        buffered.append(message)
        return orig_buffer_status(message)

    agent._buffer_status = capture_buffer_status

    # Compression that never shrinks the request -- forces every attempt to
    # re-buffer the "payload too large, compression attempt N/M" status
    # until max_compression_attempts is exhausted and the buffer flushes.
    def _noop_compress(messages, system_message, **kwargs):
        return list(messages), system_message

    with (
        patch.object(agent, "_compress_context", side_effect=_noop_compress),
        patch.object(agent, "_try_strip_image_parts_from_tool_messages", return_value=False),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("time.sleep", lambda *a, **k: None),
    ):
        result = agent.run_conversation("do the work")

    assert agent.client.chat.completions.create.called
    assert result.get("failed") is True

    assert buffered, "ни одного буферизованного статуса не было"
    matches = [m for m in buffered if "attempt" in m.lower() or "попытка" in m.lower()]
    assert matches, f"no payload-too-large retry status buffered: {buffered!r}"
    text = matches[0]
    assert "413" not in text or lang != "ru"  # RU copy drops the raw HTTP code
    if lang == "ru":
        assert "попытка" in text.lower()
        assert any("а" <= ch.lower() <= "я" for ch in text)
    else:
        assert "attempt" in text.lower()


# ---------------------------------------------------------------------------
# Safety refusal (finish_reason == "content_filter", HTTP 200)
# (agent/conversation_loop.py ~3195-3260)
# ---------------------------------------------------------------------------


def _refusal_response():
    msg = SimpleNamespace(
        content="", tool_calls=None, reasoning=None,
        reasoning_content=None, reasoning_details=None, refusal="I can't help with that.",
    )
    choice = SimpleNamespace(message=msg, finish_reason="content_filter")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_safety_refusal_final_status_is_localized(monkeypatch, lang):
    _set_lang(monkeypatch, lang)
    statuses: list = []
    agent = _make_agent(statuses)
    agent.client.chat.completions.create.return_value = _refusal_response()

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do something unsafe")

    assert agent.client.chat.completions.create.called
    assert result.get("failed") is True

    lifecycle_texts = _lifecycle_texts(statuses)
    assert lifecycle_texts, "клиент не получил статус об отказе модели"
    joined = "\n".join(lifecycle_texts)
    if lang == "ru":
        assert "фильтр" in joined.lower() or "отказал" in joined.lower()
    else:
        assert "declined" in joined.lower() or "safety" in joined.lower()


# ---------------------------------------------------------------------------
# Empty-response cluster (agent/conversation_loop.py ~7280-7520): retry
# buffered status, and the two "no content after all retries" terminals.
# ---------------------------------------------------------------------------


def _empty_response():
    msg = SimpleNamespace(
        content="", tool_calls=None, reasoning=None,
        reasoning_content=None, reasoning_details=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_empty_response_retrying_and_no_fallback_terminal_are_localized(monkeypatch, lang):
    _set_lang(monkeypatch, lang)
    statuses: list = []
    agent = _make_agent(statuses)  # no fallback_model configured
    agent.client.chat.completions.create.return_value = _empty_response()

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.conversation_loop.jittered_backoff", return_value=0.01),
        patch("time.sleep", lambda *a, **k: None),
    ):
        result = agent.run_conversation("hello")

    assert agent.client.chat.completions.create.called
    assert result.get("final_response"), "клиент не получил финального ответа вовсе"

    lifecycle_texts = _lifecycle_texts(statuses)
    assert lifecycle_texts, "клиент не получил ни одного статуса"
    joined = "\n".join(lifecycle_texts)

    retry_matches = [t for t in lifecycle_texts if "/3" in t]
    assert retry_matches, f"no empty-response retry status delivered: {lifecycle_texts!r}"
    # "/3" alone is language-independent (it survives in both catalogs
    # unchanged), so it can't tell a localized retry status apart from an
    # English literal slipped back in. Assert the actual wording of
    # ``trix.agent.empty_response_retrying`` on the isolated retry line(s),
    # not just on "some status somewhere in the whole turn".
    retry_joined = "\n".join(retry_matches)

    if lang == "ru":
        assert "Модель ничего не ответила" in retry_joined
        assert any("а" <= ch.lower() <= "я" for ch in joined)
        assert "резервный провайдер не настроен" in joined.lower()
    else:
        assert "Empty response from model" in retry_joined
        assert "nothing" in joined.lower() or "empty" in joined.lower()
        assert "no fallback" in joined.lower()


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_empty_response_with_fallback_configured_but_failing_terminal_is_localized(
    monkeypatch, lang
):
    _set_lang(monkeypatch, lang)
    statuses: list = []
    agent = _make_agent(
        statuses,
        fallback_model=[
            {
                "provider": "openrouter",
                "model": "test/fallback-model",
                "base_url": "https://openrouter.ai/api/v1",
            }
        ],
    )
    agent.client.chat.completions.create.return_value = _empty_response()

    with (
        patch.object(agent, "_try_activate_fallback", return_value=False),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.conversation_loop.jittered_backoff", return_value=0.01),
        patch("time.sleep", lambda *a, **k: None),
    ):
        result = agent.run_conversation("hello")

    assert agent.client.chat.completions.create.called
    assert result.get("final_response"), "клиент не получил финального ответа вовсе"

    lifecycle_texts = _lifecycle_texts(statuses)
    joined = "\n".join(lifecycle_texts)
    if lang == "ru":
        assert "резервного провайдера" in joined.lower()
    else:
        assert "fallback attempt" in joined.lower()


# ---------------------------------------------------------------------------
# RAF-221: the remaining English status/final lines the audit confirmed as
# DELIVERED to a Trix Telegram client. Each test drives the real call site
# end-to-end (mocked provider, real loop) exactly like the cases above.
# ---------------------------------------------------------------------------


def _make_status_error(message: str, status_code: int) -> Exception:
    err = Exception(message)
    err.status_code = status_code
    return err


class TestRetryExhaustedTerminals:
    """❌ Rate limited after N retries — / ❌ API failed after N retries — …"""

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_rate_limit_exhausted_is_localized(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)
        statuses: list = []
        agent = _make_agent(statuses)  # no fallback chain configured
        agent._api_max_retries = 2
        agent.client.chat.completions.create.side_effect = _make_status_error(
            "Too many requests", 429
        )

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.conversation_loop.jittered_backoff", return_value=0.01),
            patch("time.sleep", lambda *a, **k: None),
        ):
            result = agent.run_conversation("hello")

        assert result.get("failed") is True
        lifecycle_texts = _lifecycle_texts(statuses)
        assert lifecycle_texts, "клиент не получил терминального статуса"
        joined = "\n".join(lifecycle_texts)
        final = result.get("final_response") or ""

        assert "Hermes" not in joined + final
        if lang == "ru":
            assert "Rate limited after" not in joined
            assert "ограничила запросы" in joined
            assert "Попробуйте" in joined
            # The RU final says what to do; the raw HTTP summary stays in logs.
            assert "HTTP 429" not in final
            assert any("а" <= ch.lower() <= "я" for ch in final)
        else:
            assert "Rate limited after" in joined

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_api_failure_exhausted_is_localized(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)
        statuses: list = []
        agent = _make_agent(statuses)
        agent._api_max_retries = 2
        agent.client.chat.completions.create.side_effect = _make_status_error(
            "Internal Server Error", 500
        )

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.conversation_loop.jittered_backoff", return_value=0.01),
            patch("time.sleep", lambda *a, **k: None),
        ):
            result = agent.run_conversation("hello")

        assert result.get("failed") is True
        lifecycle_texts = _lifecycle_texts(statuses)
        joined = "\n".join(lifecycle_texts)
        final = result.get("final_response") or ""
        assert "Hermes" not in joined + final
        if lang == "ru":
            assert "API failed after" not in joined
            assert "не удалось получить ответ" in joined.lower()
            assert "HTTP 500" not in final
            assert any("а" <= ch.lower() <= "я" for ch in final)
        else:
            assert "API failed after" in joined

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_zai_overload_wait_status_is_localized(self, monkeypatch, lang):
        """The DELIVERED 'Provider overloaded. Waiting...' line (RAF-221).

        The z.ai coding-plan overload path is the production provider for
        this deployment, and its wait line reaches the client un-filtered —
        previously as English with an internal "(Z.AI Coding overload
        adaptive long backoff)" policy note. The suppressed "Rate limited.
        Waiting ..." variant is deliberately NOT translated (Ruling 8); this
        test pins the delivered branch only.
        """
        _set_lang(monkeypatch, lang)
        statuses: list = []
        agent = _make_agent(
            statuses,
            provider="zai",
            base_url="https://api.z.ai/api/coding/paas/v4",
            model="glm-5.2-plus",
        )
        agent._api_max_retries = 2

        agent.client.chat.completions.create.side_effect = _make_status_error(
            "Error code: 1305 - The service may be temporarily overloaded",
            429,
        )

        buffered: list = []
        orig_buffer = agent._buffer_status

        def capture_buffer(message):
            buffered.append(message)
            return orig_buffer(message)

        agent._buffer_status = capture_buffer

        # Keep the adaptive policy on the SHORT tier with a tiny wait: the
        # retry-wait loop spins on wall-clock time between sleep() calls,
        # so the real 30-120s long-tier waits would stall the test.
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.conversation_loop.jittered_backoff", return_value=0.01),
            patch(
                "agent.conversation_loop.adaptive_rate_limit_backoff",
                return_value=(0.01, "zai_coding_overload_short"),
            ),
            patch("time.sleep", lambda *a, **k: None),
        ):
            agent.run_conversation("hello")

        wait_lines = [
            m for m in buffered + _lifecycle_texts(statuses)
            if "Waiting" in m or "жду" in m or "перегружен" in m
        ]
        assert wait_lines, f"no overload wait status was produced: {buffered!r}"
        for line in wait_lines:
            assert "Z.AI Coding overload" not in line, (
                "internal backoff-policy note leaked into a client status"
            )
        if lang == "ru":
            assert any("Перегружен" in line or "перегружен" in line for line in wait_lines)
        else:
            assert any("Provider overloaded" in line for line in wait_lines)

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_interrupt_during_retry_wait_final_response_is_localized(
        self, monkeypatch, lang
    ):
        """A /stop landing mid-backoff must not answer in English (RAF-221)."""
        _set_lang(monkeypatch, lang)
        statuses: list = []
        agent = _make_agent(statuses)
        agent._api_max_retries = 5
        agent.client.chat.completions.create.side_effect = _make_status_error(
            "Internal Server Error", 500
        )

        def _interrupt_and_sleep(*a, **k):
            agent._interrupt_requested = True
            return None

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.conversation_loop.jittered_backoff", return_value=0.01),
            patch("time.sleep", _interrupt_and_sleep),
        ):
            result = agent.run_conversation("hello")

        assert result.get("interrupted") is True
        final = result.get("final_response") or ""
        if lang == "ru":
            assert "Operation interrupted" not in final
            assert "прервано" in final.lower()
            assert "ещё раз" in final
        else:
            assert "Operation interrupted" in final


class TestRefusalAndBudgetFinals:
    def _refusal_run(self, statuses, response):
        agent = _make_agent(statuses)
        agent.client.chat.completions.create.return_value = response
        return agent

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_content_policy_refusal_final_response_is_localized(
        self, monkeypatch, lang
    ):
        _set_lang(monkeypatch, lang)
        statuses: list = []
        agent = self._refusal_run(statuses, _refusal_response())

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("do something unsafe")

        assert result.get("failed") is True
        final = result.get("final_response") or ""
        assert final, "клиент не получил ответа об отказе"
        if lang == "ru":
            assert "отказалась отвечать" in final
            assert "фильтр безопасности" in final
            assert "safety refusal" not in final
            assert "Try rephrasing" not in final
            assert "переформулировать" in final
        else:
            assert "safety filter" in final

    @staticmethod
    def _length_response(content: str):
        msg = SimpleNamespace(
            content=content, tool_calls=None, reasoning=None,
            reasoning_content=None, reasoning_details=None,
        )
        choice = SimpleNamespace(message=msg, finish_reason="length")
        return SimpleNamespace(choices=[choice], model="test/model", usage=None)

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_thinking_budget_exhausted_final_is_localized(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)
        statuses: list = []
        agent = _make_agent(statuses)
        # Truncation whose content is one bare think block: all output spent
        # on reasoning, none left for the answer.
        agent.client.chat.completions.create.return_value = self._length_response(
            "<think>reasoning and nothing else</think>"
        )

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("think hard")

        final = result.get("final_response") or ""
        assert final, "клиент не получил финального ответа вовсе"
        assert "/thinkon" not in final, "dead command pointer in client text"
        if lang == "ru":
            assert "размышления" in final
            assert "Thinking Budget" not in final
            assert "/reasoning" in final and "/model" in final
        else:
            assert "Thinking Budget Exhausted" in final


class TestEmptyResponseClusterAdditions:
    """RAF-221: the remaining English members of the empty/recovery cluster."""

    @staticmethod
    def _reasoning_only_response():
        msg = SimpleNamespace(
            content="", tool_calls=None, reasoning="думаю над задачей",
            reasoning_content=None, reasoning_details=None,
        )
        choice = SimpleNamespace(message=msg, finish_reason="stop")
        return SimpleNamespace(choices=[choice], model="test/model", usage=None)

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_thinking_only_prefill_status_is_localized(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)
        statuses: list = []
        agent = _make_agent(statuses)
        agent.client.chat.completions.create.return_value = (
            self._reasoning_only_response()
        )

        buffered: list = []
        orig_buffer = agent._buffer_status

        def capture_buffer(message):
            buffered.append(message)
            return orig_buffer(message)

        agent._buffer_status = capture_buffer

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.conversation_loop.jittered_backoff", return_value=0.01),
            patch("time.sleep", lambda *a, **k: None),
        ):
            agent.run_conversation("hello")

        prefill_lines = [m for m in buffered if "(1/2)" in m]
        assert prefill_lines, f"no thinking-only prefill status buffered: {buffered!r}"
        if lang == "ru":
            assert "размышления" in prefill_lines[0]
            assert "Thinking-only" not in prefill_lines[0]
        else:
            assert "Thinking-only" in prefill_lines[0]

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_reasoning_only_terminal_status_and_final_are_localized(
        self, monkeypatch, lang
    ):
        _set_lang(monkeypatch, lang)
        statuses: list = []
        agent = _make_agent(statuses)
        agent.client.chat.completions.create.return_value = (
            self._reasoning_only_response()
        )

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.conversation_loop.jittered_backoff", return_value=0.01),
            patch("time.sleep", lambda *a, **k: None),
        ):
            result = agent.run_conversation("hello")

        lifecycle_texts = _lifecycle_texts(statuses)
        joined = "\n".join(lifecycle_texts)
        final = result.get("final_response") or ""
        assert final and final != "(empty)"
        if lang == "ru":
            assert "только размышления" in joined or "размышления" in joined
            assert "Returning empty" not in joined
            assert "внутренние размышления" in final
            assert "думаю над задачей" in final  # the labeled excerpt survives
        else:
            assert "reasoning but no visible" in joined
            assert "internal reasoning" in final

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_dropped_toolcall_retry_status_is_localized(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)
        statuses: list = []
        agent = _make_agent(statuses)
        # finish_reason="tool_calls" with an empty calls array: the model
        # signalled an action but shipped none -> bounded re-prompt.
        msg = SimpleNamespace(
            content="I will do it now", tool_calls=None, reasoning=None,
            reasoning_content=None, reasoning_details=None,
        )
        choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
        agent.client.chat.completions.create.return_value = SimpleNamespace(
            choices=[choice], model="test/model", usage=None
        )

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            agent.run_conversation("do the thing")

        joined = "\n".join(_lifecycle_texts(statuses))
        if lang == "ru":
            assert "запрашиваю заново" in joined
            assert "re-prompting" not in joined
        else:
            assert "re-prompting" in joined

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_payload_too_large_final_response_is_localized(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)
        statuses: list = []
        agent = _make_agent(statuses)
        agent.compression_enabled = True
        agent.client.chat.completions.create.side_effect = _make_413_error()

        def _noop_compress(messages, system_message, **kwargs):
            return list(messages), system_message

        with (
            patch.object(agent, "_compress_context", side_effect=_noop_compress),
            patch.object(agent, "_try_strip_image_parts_from_tool_messages", return_value=False),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("time.sleep", lambda *a, **k: None),
        ):
            result = agent.run_conversation("big request")

        assert result.get("failed") is True
        final = result.get("final_response") or ""
        assert final
        if lang == "ru":
            assert "413" not in final
            assert "не удалось сжать" in final.lower()
            assert "/new" in final
        else:
            assert "413" in final

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_compression_vision_stripped_status_is_localized(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)
        statuses: list = []
        agent = _make_agent(statuses)
        agent.compression_enabled = True
        agent.client.chat.completions.create.side_effect = _make_413_error()

        def _noop_compress(messages, system_message, **kwargs):
            return list(messages), system_message

        buffered: list = []
        orig_buffer = agent._buffer_status

        def capture_buffer(message):
            buffered.append(message)
            return orig_buffer(message)

        agent._buffer_status = capture_buffer

        with (
            patch.object(agent, "_compress_context", side_effect=_noop_compress),
            patch.object(
                agent,
                "_try_strip_image_parts_from_tool_messages",
                side_effect=[True, False, False, False],
            ),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("time.sleep", lambda *a, **k: None),
        ):
            agent.run_conversation("big request with images")

        vision_lines = [m for m in buffered if "vision" in m or "изображени" in m]
        assert vision_lines, f"no vision-strip status buffered: {buffered!r}"
        if lang == "ru":
            assert "изображени" in vision_lines[0]
            assert "vision payloads" not in vision_lines[0]
        else:
            assert "vision payloads" in vision_lines[0]


# ---------------------------------------------------------------------------
# Standalone helpers (RAF-221): compaction-done edge, blocked-overflow
# warning, toolguard halt reply, offline summary — real producers called
# directly, like the manual_compression_feedback cases at the top.
# ---------------------------------------------------------------------------


class TestStandaloneHelperL10n:
    def _capture_agent(self, statuses):
        agent = _make_agent(statuses)
        return agent

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_compaction_done_status_is_localized(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)
        events: list = []
        agent = SimpleNamespace(status_callback=lambda kind, msg: events.append((kind, msg)))
        from agent.conversation_compression import _emit_compaction_done

        _emit_compaction_done(agent)
        assert events and events[0][0] == "compacted"
        text = events[0][1]
        if lang == "ru":
            assert "Контекст сжат" in text
            assert "compaction" not in text
        else:
            assert "Context compaction complete" in text

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_context_overflow_blocked_warning_is_localized(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)
        warnings_list: list = []
        agent = self._capture_agent([])
        with patch.object(
            AIAgent, "_emit_warning", lambda self, msg: warnings_list.append(msg)
        ):
            agent._warn_context_overflow_blocked("cooldown:30", 85_000, 72_000)

        assert warnings_list
        text = warnings_list[0]
        if lang == "ru":
            assert "порог сжатия" in text
            assert "cooldown" not in text
            assert "отдыхает после сбоя" in text
            assert "/new" in text and "/compress" in text
        else:
            assert "cooling down" in text

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_toolguard_halt_response_is_localized(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)
        agent = self._capture_agent([])
        decision = SimpleNamespace(tool_name="terminal", code="non_progressing", count=3)
        text = agent._toolguard_controlled_halt_response(decision)
        assert "terminal" in text
        assert "non_progressing" not in text
        if lang == "ru":
            assert "защита от зацикливания" in text
            assert "guardrail" not in text
        else:
            assert "guardrail" not in text

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_api_failure_terminal_renders_without_missing_placeholder(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)
        text = i18n.t("trix.agent.api_failed_final")
        assert "{" not in text
        if lang == "ru":
            assert "после нескольких попыток" in text
        else:
            assert "after multiple retries" in text

    @pytest.mark.parametrize("lang", ["en", "ru"])
    @pytest.mark.parametrize(
        "key",
        ("trix.agent.compression_aborted_turn", "trix.agent.codex_compaction_failed"),
    )
    def test_compression_failures_never_render_raw_error(self, monkeypatch, lang, key):
        _set_lang(monkeypatch, lang)
        raw_error = "https://backend.example/internal?account=alice"
        text = i18n.t(key, err=raw_error)
        assert raw_error not in text
        assert "{" not in text

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_stale_status_hides_operator_hint(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)
        statuses: list[str] = []
        agent = SimpleNamespace(_buffer_status=statuses.append)
        from agent.chat_completion_helpers import _report_stale_nonstream_kill

        _report_stale_nonstream_kill(
            agent,
            {"model": "gpt-5.5"},
            elapsed=120,
            stale_timeout=90,
            hint="Codex backend at chatgpt.com requires a config workaround.",
        )

        assert len(statuses) == 1
        assert "Codex backend" not in statuses[0]
        assert "chatgpt.com" not in statuses[0]
        if lang == "ru":
            assert "Завершаю эту попытку" in statuses[0]
        else:
            assert "Ending this attempt" in statuses[0]

    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_offline_summary_is_debranded_and_localized(self, monkeypatch, lang):
        _set_lang(monkeypatch, lang)

        class Nested(Exception):
            pass

        try:
            try:
                raise OSError("Temporary failure in name resolution")
            except OSError as inner:
                raise Nested("Connection error") from inner
        except Nested as exc:
            text = AIAgent._summarize_api_error(exc)

        assert "Hermes" not in text
        if lang == "ru":
            assert "связаться с провайдером" in text
            assert "offline" not in text
        else:
            assert "Can't reach the model provider" in text


# ---------------------------------------------------------------------------
# Banned words: no fork client string names the upstream brand or internal
# "agent" vocabulary on the rate-limit/fallback/overload/stall paths.
# ---------------------------------------------------------------------------


class TestNoBrandOrInternalWordsInClientText:
    _KEYS = (
        "trix.agent.rate_limited_exhausted",
        "trix.agent.api_failed_exhausted",
        "trix.agent.api_failed_final",
        "trix.agent.provider_overloaded_waiting",
        "trix.agent.provider_stale_nonstreaming",
        "trix.agent.provider_stale_streaming",
        "trix.agent.interrupted_retry",
        "trix.agent.interrupted_during_retry",
        "trix.agent.interrupted_api_error",
        "trix.agent.interrupted_empty_retry",
        "trix.agent.handoff_skip_final",
        "trix.agent.compression_vision_stripped",
        "trix.agent.payload_too_large_final",
        "trix.agent.compaction_done",
        "trix.agent.compression_aborted_turn",
        "trix.agent.compression_empty_transcript",
        "trix.agent.codex_compaction_failed",
        "trix.agent.context_overflow_blocked",
        "trix.agent.safety_refusal_final_response",
        "trix.agent.thinking_budget_exhausted",
        "trix.agent.thinking_timeout_hint",
        "trix.agent.stream_drop_hint",
        "trix.agent.stream_interrupted_partial",
        "trix.agent.thinking_only_prefill",
        "trix.agent.reasoning_only_terminal",
        "trix.agent.reasoning_only_final",
        "trix.agent.dropped_toolcall_retry",
        "trix.agent.toolguard_halted",
        "trix.agent.toolguard_halt_response",
        "trix.errors.provider.offline_summary",
        "trix.busy.stall_notice",
    )

    def test_ru_catalog_never_names_upstream_or_internal_words(self):
        import re as _re

        for key in self._KEYS:
            text = i18n.t(key, lang="ru")
            assert text != key, f"{key} missing from ru catalog"
            for banned in ("Hermes", "hermes", "redirecting", "Нos"):
                assert banned not in text, f"{banned!r} leaked into {key}: {text!r}"
            # Standalone "agent"/"agent" as a word, not inside a longer token.
            assert not _re.search(r"\b[Aa]gent\b", text), (
                f"internal word 'agent' in {key}: {text!r}"
            )
