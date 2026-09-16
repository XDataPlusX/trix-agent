"""The content-filter-stream-stall fallback attempt (agent/conversation_loop.py
~3357-3373) is delivered through ``_emit_status`` -- immediate, unbuffered --
not ``_buffer_status``. That's deliberate: it's one of the two call sites
(the other being the one-shot success notice) the client sees even when the
overall turn SUCCEEDS, not only on eventual failure.

Round-3 review found two silent holes here:

- **Delivery mechanism.** Swapping ``_emit_status`` for ``_buffer_status``
  at this call site is invisible to every existing test, because none of
  them drive a SUCCESSFUL turn through this branch and check the message
  survived to the client. A buffered line only reaches the client on
  eventual failure (via ``_flush_status_buffer``) or gets silently dropped
  on success (via ``_clear_status_buffer``) -- so the swap would make a
  successful content-filter fallback go silent.
- **Reason passed at the call site.** ``client_fallback_attempt_message(
  FailoverReason.content_policy_blocked)`` could be silently hardcoded to
  a different reason (e.g. ``FailoverReason.rate_limit``) with nothing
  catching it -- module-level tests exercise the function directly, never
  this call site.

This test drives a SUCCESSFUL turn (a content-filter-terminated stream on
the primary, recovered by a configured fallback that then answers cleanly)
and asserts the exact content-filter attempt message is present among the
lifecycle statuses -- which is only possible if it was delivered
immediately via ``_emit_status``, since a buffered line would have been
silently dropped by ``_clear_status_buffer()`` on this successful turn.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent import i18n
from agent.error_classifier import FailoverReason
from hermes_cli.trix_provider_errors import client_fallback_attempt_message
from run_agent import AIAgent


def _make_agent_with_fallback(statuses: list) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
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
            fallback_model=[
                {
                    "provider": "zai",
                    "model": "glm-4.7",
                    "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
                }
            ],
            status_callback=lambda kind, message: statuses.append((kind, message)),
        )
        agent.client = MagicMock()
        return agent


def _content_filter_terminated_response():
    msg = SimpleNamespace(
        content="partial answer",
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason="length")
    resp = SimpleNamespace(choices=[choice], model="glm-5.1", usage=None, id="not-a-stub")
    # The attribute conversation_loop.py actually reads to detect this case
    # (agent/chat_completion_helpers.py tags it on the response stub when
    # the provider's safety filter kills a stream mid-delivery).
    resp._content_filter_terminated = True
    return resp


def _success_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="glm-4.7", usage=None)


def _fallback_client() -> MagicMock:
    fb = MagicMock()
    fb.api_key = "primary-key-abcdef12"
    fb.base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
    fb._custom_headers = None
    fb.default_headers = None
    return fb


def test_content_filter_attempt_notice_survives_a_successful_turn(monkeypatch):
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        statuses: list = []
        agent = _make_agent_with_fallback(statuses)

        calls = []

        def fake_api_call(api_kwargs):
            calls.append((agent.provider, agent.model))
            if len(calls) == 1:
                return _content_filter_terminated_response()
            return _success_response("all good now")

        with (
            patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
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

        # Guard against a vacuous pass: the switch really happened AND the
        # turn really succeeded (this is the whole point -- a buffered
        # delivery would still show the message on a FAILED turn).
        mock_resolve.assert_called_once()
        assert result.get("completed") is True
        assert calls == [("zai", "glm-5.1"), ("zai", "glm-4.7")]

        lifecycle_texts = [msg for kind, msg in statuses if kind == "lifecycle"]
        expected = client_fallback_attempt_message(FailoverReason.content_policy_blocked)
        assert expected in lifecycle_texts, (
            f"content-filter attempt notice missing from a SUCCESSFUL turn "
            f"(would mean it was buffered instead of emitted immediately, "
            f"or the wrong reason was passed at the call site), got: "
            f"{lifecycle_texts!r}"
        )
        # It must not render as a different branch's text (rate-limited
        # default, billing, auth, ...).
        assert "ограничил частоту" not in expected
        assert "средства" not in expected
    finally:
        i18n.reset_language_cache()
