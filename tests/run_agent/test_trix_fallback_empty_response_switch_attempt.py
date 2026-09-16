"""agent/conversation_loop.py's empty-response-exhausted eager-fallback
branch (~7282-7311) captures ``_pre_fallback_model``/``_pre_fallback_provider``
from ``agent.model``/``agent.provider`` BEFORE calling
``agent._try_activate_fallback()``, then buffers a switch-attempt line
built from those captured (OLD) values plus the POST-swap (NEW)
``agent.model``/``agent.provider``.

Round-3 review: this capture is completely uncovered. Swapping
``_pre_fallback_model, _pre_fallback_provider`` for ``agent.model,
agent.provider`` in the buffered call (reading the state AFTER the swap
twice instead of before-and-after) renders a message that names the SAME
model as both "old" and "new" -- a switch notice that doesn't actually say
anything switched. This test drives that exact branch and asserts the
buffered message names two DIFFERENT models in the right positions.

Round-4 review: the first version of this test read ``buffered[-1]`` and
asserted against it, on the stated assumption that the call site under test
fires last. It does fire last -- but ``try_activate_fallback()`` buffers a
**byte-identical** line one step earlier, so ``buffered[-1]`` and
``buffered[-2]`` are the same string and every assertion held on the
helper's copy alone. Deleting the call site this file is named after left
the test green, as did swapping its old/new arguments.

Round-5 review: those two lines were never supposed to be identical. Before
localization, ``try_activate_fallback()`` buffered a generic
``🔄 Primary model failed — switching to fallback: X via Y`` and this branch
buffered its own ``↻ Switched to fallback: X (Y)`` -- one line about the
attempt, one confirming the swap on the specific path that provoked it.
Task 3 routed both through a single function and collapsed them into the
same sentence, so the duplication was OUR regression, not an upstream
defect. ``client_fallback_empty_response_switch_message`` restores the
split.

The assertions below therefore pin what the client actually reads on this
path, matched exactly against the rendered text each call site is supposed
to produce:

* the two switch lines must be DIFFERENT text, and each must appear exactly
  once. Deleting either call site, swapping either one's old/new arguments,
  or pointing them back at a common message all break this.
* the empty-response attempt line must appear exactly ONCE, and must be the
  text ``client_fallback_attempt_message`` renders for ``"invalid_response"``
  specifically -- that reason is passed as a bare string literal at the call
  site, so nothing else pins it to the branch the client needs.

Harness: forces ``agent._empty_content_retries`` past the retry-then-fallback
threshold and mocks ``agent.conversation_loop.jittered_backoff`` to a
near-zero delay so the (real, unmocked) retry backoff loop in
conversation_loop.py does not burn real wall-clock time -- it still walks
the genuine 3-retries-then-fallback code path, just fast.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent import i18n
from hermes_cli.trix_provider_errors import (
    client_fallback_attempt_message,
    client_fallback_empty_response_switch_message,
    client_fallback_switch_attempt_message,
)
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


def _empty_response():
    msg = SimpleNamespace(
        content="", tool_calls=None, reasoning=None,
        reasoning_content=None, reasoning_details=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="glm-5.1", usage=None)


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


def test_switch_attempt_names_the_old_and_new_model_distinctly(monkeypatch):
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        statuses: list = []
        agent = _make_agent_with_fallback(statuses)

        calls = []

        def fake_api_call(api_kwargs):
            calls.append((agent.provider, agent.model))
            # Four empty responses exhausts the 3-retry budget and lands on
            # the eager-fallback branch; the fifth call (post-swap, on the
            # fallback provider) succeeds.
            if len(calls) <= 4:
                return _empty_response()
            return _success_response("all good now")

        buffered: list = []
        orig_buffer_status = agent._buffer_status

        def capture_buffer_status(message):
            buffered.append(message)
            return orig_buffer_status(message)

        agent._buffer_status = capture_buffer_status

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
            patch("agent.conversation_loop.jittered_backoff", return_value=0.01),
        ):
            result = agent.run_conversation("hi")

        # Guard against a vacuous pass: the switch really happened.
        mock_resolve.assert_called_once()
        assert result.get("completed") is True
        assert agent.model == "glm-4.7"

        assert buffered, "ни одного буферизованного статуса не было"

        # What the client must read, rendered from the SAME l10n functions the
        # call sites use, with old and new pinned to the right slots. If a
        # call site reads agent.model/agent.provider (the POST-swap state)
        # for both the "old" and "new" slot instead of the pre-captured OLD
        # state, or has its arguments transposed, it produces a different
        # string and stops matching.
        expected_generic_switch = client_fallback_switch_attempt_message(
            "glm-5.1", "zai", "glm-4.7", "zai"
        )
        expected_empty_switch = client_fallback_empty_response_switch_message(
            "glm-5.1", "zai", "glm-4.7", "zai"
        )
        # The load-bearing assertion. Two call sites fire back to back here:
        # chat_completion_helpers.py buffers the generic line inside
        # try_activate_fallback(), and conversation_loop.py buffers this
        # path's own confirmation right after that call returns. Nothing
        # dedupes the buffer, so if both render the same sentence the client
        # reads it twice in a row on a failed turn -- and, worse, every
        # assertion about "the line from call site B" silently passes on
        # call site A's copy. That is exactly how the previous version of
        # this test went green with call site B deleted.
        assert expected_generic_switch != expected_empty_switch, (
            f"the two switch call sites render identical text, so nothing "
            f"below can tell them apart: {expected_generic_switch!r}"
        )
        assert buffered.count(expected_generic_switch) == 1, (
            f"expected exactly one generic switch line (from "
            f"try_activate_fallback), got {buffered!r}"
        )
        assert buffered.count(expected_empty_switch) == 1, (
            f"expected exactly one empty-response switch confirmation (from "
            f"conversation_loop.py's own call), got {buffered!r}"
        )

        # The reason handed to the attempt line at this call site is the bare
        # string literal "invalid_response". Nothing else keeps it on the
        # empty-response branch, so pin the rendered text of that branch.
        expected_attempt = client_fallback_attempt_message("invalid_response")
        assert buffered.count(expected_attempt) == 1, (
            f"expected the empty/malformed-response attempt line exactly once; "
            f"a different reason at the call site renders different text: "
            f"{buffered!r}"
        )
        # Order: the cause first, then the generic switch line, then this
        # path's own confirmation of the completed swap.
        assert (
            buffered.index(expected_attempt)
            < buffered.index(expected_generic_switch)
            < buffered.index(expected_empty_switch)
        ), f"the client reads these out of order: {buffered!r}"
    finally:
        i18n.reset_language_cache()
