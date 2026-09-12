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
