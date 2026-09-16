"""The buffered "trying a fallback" lines reach the client in Russian, and
name the RIGHT reason, when the fallback attempt itself also fails.

``agent/conversation_loop.py``'s eager-fallback block (~4883-4913, the
four-branch upstream/billing/transport/rate-limit dispatch collapsed in
Task 3 into a single ``client_fallback_attempt_message(classified.reason,
upstream=...)`` call) and the auth-failover block (~4926-4940) each buffer
a status line via ``agent._buffer_status`` before calling
``agent._try_activate_fallback()``. Those buffered lines are DROPPED on a
successful recovery (see ``tests/run_agent/test_trix_fallback_success_notice.py``)
and only reach the client via ``agent._flush_status_buffer()`` when the
whole turn still ends up failing -- e.g. because the fallback provider hits
the identical wall the primary did.

Round-3 review finding: the original version of this file asserted
``"запасн" in joined`` against the newline-joined blob of EVERY lifecycle
message. That substring is present in an unrelated neighboring message too
(the buffered switch-attempt line from ``try_activate_fallback()`` itself),
so deleting the actual billing-branch buffered-status call left the test
green. Fixed by asserting against a SPECIFIC element of the lifecycle list
(matched exactly against ``client_fallback_attempt_message(...)``'s real
output), not a substring of the whole transcript.

Round-4 review, two findings:

* those exact-element assertions were written as ``lifecycle_texts[0] == ...``,
  which pins the message's POSITION as well as its content. Any status
  emitted earlier in the turn would fail them with no behaviour change at
  all. They now count occurrences of the expected text anywhere in the list.
* a third call site, conversation_loop.py's invalid-response eager fallback
  (~2943, the branch taken when the transport rejects the response envelope
  itself rather than its content), passes its reason as the bare string
  literal ``"invalid_response"`` and had no test at any level. Replacing that
  literal with a different reason changed what the client reads and nothing
  went red. ``test_invalid_response_fallback_attempt_line_names_the_empty_response_reason``
  below drives it.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent import i18n
from agent.error_classifier import FailoverReason
from hermes_cli.trix_provider_errors import client_fallback_attempt_message
from run_agent import AIAgent


def _make_402_billing_error() -> Exception:
    """See test_trix_billing_terminal_client_message.py for why this exact
    text lands on the confirmed-billing branch rather than rate_limit."""
    err = Exception("402 Payment Required: account balance is insufficient.")
    err.status_code = 402
    return err


def _make_401_auth_error() -> Exception:
    err = Exception("401 Unauthorized: invalid api key")
    err.status_code = 401
    return err


def _make_agent_with_fallback(statuses: list) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            api_mode="chat_completions",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "openai", "model": "gpt-4o-fallback"}],
            status_callback=lambda kind, message: statuses.append((kind, message)),
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _fallback_client_that_also_fails(exc: Exception) -> MagicMock:
    fb = MagicMock()
    fb.api_key = "fb-key"
    fb.base_url = "https://api.openai.com/v1"
    fb._custom_headers = None
    fb.default_headers = None
    fb.chat.completions.create.side_effect = exc
    return fb


def _invalid_envelope():
    """A response the chat-completions transport rejects outright: it has a
    ``choices`` attribute, but empty -- no message, no content, nothing to
    normalize. This is the shape that reaches conversation_loop.py's
    ``response_invalid`` branch, which is a different code path from the
    empty-*content* retry loop covered in
    tests/run_agent/test_trix_fallback_empty_response_switch_attempt.py.
    """
    return SimpleNamespace(choices=[], model="test/model", usage=None)


def _fallback_client_that_returns(response) -> MagicMock:
    fb = MagicMock()
    fb.api_key = "fb-key"
    fb.base_url = "https://api.openai.com/v1"
    fb._custom_headers = None
    fb.default_headers = None
    fb.chat.completions.create.return_value = response
    return fb


def test_billing_fallback_attempt_line_is_russian_when_fallback_also_exhausts(monkeypatch):
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        statuses: list = []
        agent = _make_agent_with_fallback(statuses)
        agent.client.chat.completions.create.side_effect = _make_402_billing_error()

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(
                    _fallback_client_that_also_fails(_make_402_billing_error()),
                    "gpt-4o-fallback",
                ),
            ) as mock_resolve,
            patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ),
        ):
            result = agent.run_conversation("do the work")

        # Guard against a vacuous pass: the fallback really was tried.
        mock_resolve.assert_called_once()
        assert result.get("failed") is True

        lifecycle_texts = [msg for kind, msg in statuses if kind == "lifecycle"]
        assert lifecycle_texts, "клиент не получил ни одного статуса"

        # The SPECIFIC message this test's docstring is about: the buffered
        # line from conversation_loop.py's collapsed four-branch dispatch
        # (~4883-4906), called with classified.reason == FailoverReason.billing.
        # Exact-element match, not "in joined" -- a mutation deleting the
        # buffer_status call at that exact site (or hardcoding the wrong
        # reason there) must make this assertion fail, not some other line
        # that happens to share a substring.
        expected_billing_attempt = client_fallback_attempt_message(FailoverReason.billing)
        # Round-4 review: this used to be ``lifecycle_texts[0] == ...``, which
        # made the test a hostage of message ORDER -- inserting any earlier
        # status would have failed it without a single behaviour change, and
        # that is the same defect as a test that catches nothing. Bind to
        # content and to how many times it appears instead.
        assert lifecycle_texts.count(expected_billing_attempt) == 1, (
            f"expected exactly one billing-branch buffered attempt line, got: "
            f"{lifecycle_texts!r}"
        )
        joined = "\n".join(lifecycle_texts)
        assert "switching to fallback" not in joined.lower()
        assert "billing or credits exhausted — switching" not in joined.lower()
    finally:
        i18n.reset_language_cache()


def test_auth_fallback_attempt_line_is_russian_when_fallback_also_exhausts(monkeypatch):
    """Covers agent/conversation_loop.py's auth-failover block (~4926-4940):
    ``client_fallback_attempt_message(classified.reason)`` where
    ``classified.reason`` is FailoverReason.auth. Same exact-element
    assertion style as the billing test above -- a hardcoded wrong reason
    at that call site (e.g. passing FailoverReason.rate_limit or
    FailoverReason.overloaded instead of classified.reason) must fail this
    test.
    """
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        statuses: list = []
        agent = _make_agent_with_fallback(statuses)
        agent.client.chat.completions.create.side_effect = _make_401_auth_error()

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(
                    _fallback_client_that_also_fails(_make_401_auth_error()),
                    "gpt-4o-fallback",
                ),
            ) as mock_resolve,
            patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ),
        ):
            result = agent.run_conversation("do the work")

        mock_resolve.assert_called_once()
        assert result.get("failed") is True

        lifecycle_texts = [msg for kind, msg in statuses if kind == "lifecycle"]
        assert lifecycle_texts, "клиент не получил ни одного статуса"

        expected_auth_attempt = client_fallback_attempt_message(FailoverReason.auth)
        # Content-bound, not position-bound -- see the billing test above.
        assert lifecycle_texts.count(expected_auth_attempt) == 1, (
            f"expected exactly one auth-failover buffered attempt line, got: "
            f"{lifecycle_texts!r}"
        )
        # Regression guard: an auth failure must not render as the
        # billing/rate-limit/empty-response texts (would mean the wrong
        # reason reached the call site, or the branch swap from round 2
        # regressed).
        assert "средства" not in expected_auth_attempt
        assert "ограничил частоту" not in expected_auth_attempt
        assert "пустой" not in expected_auth_attempt
    finally:
        i18n.reset_language_cache()


def test_invalid_response_fallback_attempt_line_names_the_empty_response_reason(monkeypatch):
    """agent/conversation_loop.py ~2943: the eager fallback taken when the
    transport rejects the response ENVELOPE (no usable ``choices``), not when
    the content came back empty.

    The reason is a bare ``"invalid_response"`` string literal at that call
    site -- no enum, no classifier, nothing that would break if it were
    changed. Swapping it for another reason silently rewrites what the client
    reads (they would be told the key ran out of funds when the provider
    actually returned a malformed envelope), so this pins the rendered text
    of that specific branch, and only that branch.
    """
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        statuses: list = []
        agent = _make_agent_with_fallback(statuses)
        agent._api_max_retries = 2
        agent.client.chat.completions.create.return_value = _invalid_envelope()

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(
                    _fallback_client_that_returns(_invalid_envelope()),
                    "gpt-4o-fallback",
                ),
            ) as mock_resolve,
            patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ),
        ):
            result = agent.run_conversation("do the work")

        # Guard against a vacuous pass: the fallback really was tried, and the
        # turn really did end up failing (which is what flushes the buffer to
        # the client).
        mock_resolve.assert_called_once()
        assert result.get("failed") is True

        lifecycle_texts = [msg for kind, msg in statuses if kind == "lifecycle"]
        assert lifecycle_texts, "клиент не получил ни одного статуса"

        expected = client_fallback_attempt_message("invalid_response")
        assert lifecycle_texts.count(expected) == 1, (
            f"expected exactly one invalid-response attempt line, got: "
            f"{lifecycle_texts!r}"
        )
        # Any other reason renders different text -- name the two the client
        # would be actively misled by if the literal were swapped.
        assert client_fallback_attempt_message(FailoverReason.billing) not in lifecycle_texts
        assert client_fallback_attempt_message(FailoverReason.rate_limit) not in lifecycle_texts
    finally:
        i18n.reset_language_cache()
