"""Task 3 headline requirement: on a SUCCESSFUL fallback recovery, the
client sees the switch explained in Russian, naming the model they're now
paying for.

The one-shot notice mechanism (``agent._pending_fallback_notice`` set by
``agent.chat_completion_helpers.try_activate_fallback``, surfaced exactly
once via ``AIAgent._emit_pending_fallback_notice`` on the first successful
content after a switch — see run_agent.py) is untouched by this task; only
the text changed, from a hardcoded English literal to
``hermes_cli.trix_provider_errors.client_fallback_message(...)``.

Module-level tests (``tests/agent/test_trix_fallback_notice.py``,
``tests/hermes_cli/test_trix_provider_errors.py``) already prove the
function itself renders correct Russian text. This file proves the ACTUAL
call site still wires into it end to end — the same category of gap two
mutations found in the prior task (gluing extra text onto the client
message, and reverting a call site back to its English literal both
survived the full test suite untouched).

Round-4 review: "names the new model" was still too weak an assertion --
transposing the old/new arguments at the call site
(``chat_completion_helpers.py``, where ``_pending_fallback_notice`` is set)
leaves the new model's name in the text, moved into the clause that names
the model that just died, and survived. The Russian test below now matches
the notice against the full rendered line with both slots pinned.
Harness lifted from
``tests/run_agent/test_32646_fallback_429_after_timeout.py`` and
``tests/run_agent/test_trix_billing_terminal_client_message.py``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent import i18n
from hermes_cli.trix_provider_errors import client_fallback_message
from run_agent import AIAgent


def _make_tool_defs():
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _make_agent_with_fallback(fb_chain, statuses):
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key-abcdef12",
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            provider="zai",
            model="glm-5.1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fb_chain,
            status_callback=lambda kind, message: statuses.append((kind, message)),
        )
        agent.client = MagicMock()
        return agent


def _mock_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="fallback/model", usage=None)


class RateLimitError(Exception):
    status_code = 429

    def __init__(self):
        super().__init__("Error code: 429 - rate limit exceeded")
        self.response = SimpleNamespace(headers={})
        self.body = {"error": {"message": "rate limit exceeded"}}


def _fallback_client():
    fb = MagicMock()
    fb.api_key = "primary-key-abcdef12"
    fb.base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
    fb._custom_headers = None
    fb.default_headers = None
    return fb


def test_successful_fallback_recovery_notifies_the_client_in_russian(monkeypatch):
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        fb_chain = [
            {
                "provider": "zai",
                "model": "glm-4.7",
                "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
            }
        ]
        statuses: list = []
        agent = _make_agent_with_fallback(fb_chain, statuses)
        agent._api_max_retries = 2

        calls = []

        def fake_api_call(api_kwargs):
            calls.append((agent.provider, agent.model))
            if len(calls) == 1:
                raise RateLimitError()
            return _mock_response("all good now")

        with (
            patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch("agent.agent_runtime_helpers.time.sleep"),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(_fallback_client(), "glm-4.7"),
            ) as mock_resolve,
            patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ),
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
        ):
            result = agent.run_conversation("hello")

        # Guard against a vacuous pass: the switch actually happened.
        mock_resolve.assert_called_once()
        assert result["completed"] is True
        assert calls == [("zai", "glm-5.1"), ("zai", "glm-4.7")]
        assert agent.model == "glm-4.7"

        lifecycle_texts = [msg for kind, msg in statuses if kind == "lifecycle"]
        assert lifecycle_texts, (
            "no status_callback('lifecycle', ...) call at all -- the client "
            "was never told the model switched"
        )
        joined = "\n".join(lifecycle_texts)

        # The headline requirement: the client sees WHICH model they're now
        # paying for, in Russian.
        assert "glm-4.7" in joined, f"new model name missing from: {joined!r}"
        assert "Switched" not in joined
        assert "switching to fallback" not in joined.lower()
        assert any("а" <= ch.lower() <= "я" for ch in joined), (
            f"expected Russian text somewhere in the switch notice, got: {joined!r}"
        )

        # Round-4 review: the assertions above are all satisfied by a notice
        # whose old/new arguments are transposed -- "glm-4.7" is still in the
        # text, just in the sentence that names the model that FAILED. On a
        # successful switch this is the only line the client ever sees, so
        # getting the two slots backwards tells them the dead model is the one
        # they are now paying for. Match the whole line against what the call
        # site is supposed to render, with old and new pinned to their slots.
        expected_notice = client_fallback_message("glm-5.1", "zai", "glm-4.7", "zai")
        assert lifecycle_texts.count(expected_notice) == 1, (
            f"expected exactly one success notice naming glm-5.1 as the old "
            f"model and glm-4.7 as the new one, got: {lifecycle_texts!r}"
        )
    finally:
        i18n.reset_language_cache()


def test_successful_fallback_recovery_in_english_locale_has_no_stray_russian(monkeypatch):
    """Sanity check the other direction: with the English catalog active,
    the notice must not splice in Russian text (mirrors the existing
    ``test_unknown_upstream_does_not_splice_a_russian_word_into_english``
    guard in tests/hermes_cli/test_trix_provider_errors.py, at the real
    call site instead of the bare function)."""
    monkeypatch.setenv("HERMES_LANGUAGE", "en")
    i18n.reset_language_cache()
    try:
        fb_chain = [
            {
                "provider": "zai",
                "model": "glm-4.7",
                "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
            }
        ]
        statuses: list = []
        agent = _make_agent_with_fallback(fb_chain, statuses)
        agent._api_max_retries = 2

        calls = []

        def fake_api_call(api_kwargs):
            calls.append((agent.provider, agent.model))
            if len(calls) == 1:
                raise RateLimitError()
            return _mock_response("all good now")

        with (
            patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch("agent.agent_runtime_helpers.time.sleep"),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(_fallback_client(), "glm-4.7"),
            ),
            patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ),
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
        ):
            result = agent.run_conversation("hello")

        assert result["completed"] is True
        lifecycle_texts = [msg for kind, msg in statuses if kind == "lifecycle"]
        joined = "\n".join(lifecycle_texts)
        assert "glm-4.7" in joined
        assert not any("а" <= ch.lower() <= "я" for ch in joined), (
            f"Russian text leaked into the English-locale notice: {joined!r}"
        )
    finally:
        i18n.reset_language_cache()
