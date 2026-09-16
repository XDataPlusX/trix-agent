"""Tests for GatewayRunner._maybe_handle_secret_capture_reply (spec 19).

Covers: (c) the reply resolves the waiting capture instead of being treated
as ordinary text, (e) every merged Telegram message id gets deleted — not
just the first, Ruling 7's "tell the client BEFORE restarting" ordering,
slash commands and unauthorized senders leaving a pending capture untouched,
and that a resolved-but-undelivered capture is never silently dropped.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_source(chat_id: str = "c1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id=chat_id,
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str, *, message_id="m1", metadata=None, cmd=None) -> MessageEvent:
    event = MessageEvent(
        text=text,
        source=_make_source(),
        message_id=message_id,
        metadata=metadata or {},
    )
    if cmd is not None:
        event.get_command = lambda: cmd  # type: ignore[method-assign]
    return event


def _make_runner(*, authorized: bool = True) -> GatewayRunner:
    """Bare GatewayRunner with only the attributes this method touches."""
    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = MagicMock(return_value=authorized)
    runner._session_key_for_source = MagicMock(return_value="session-1")

    async def _run_in_executor_with_context(func, *args):
        return func(*args)

    runner._run_in_executor_with_context = _run_in_executor_with_context
    return runner


@pytest.mark.asyncio
async def test_reply_resolves_pending_capture_and_deletes_all_merged_ids():
    """(c)/(e) — a batched reply resolves the capture and both message ids die."""
    runner = _make_runner()
    adapter = MagicMock()
    adapter.delete_message = AsyncMock(return_value=True)
    adapter.send = AsyncMock()
    runner._adapter_for_source = MagicMock(return_value=adapter)

    event = _make_event(
        "sk-tavily-abc123",
        message_id="10",
        metadata={"telegram_merged_message_ids": ["10", "11"]},
    )

    outcome = {
        "success": True,
        "stored_as": "TAVILY_API_KEY",
        "validated": None,
        "provider": False,
        "needs_restart": False,
        "message": "",
    }
    with patch("tools.secret_capture_gateway.get_pending_for_session",
               return_value=object()), \
         patch("tools.secret_capture_gateway.resolve_secret_reply",
               return_value=outcome) as resolve_mock:
        response = await runner._maybe_handle_secret_capture_reply(
            event, event.source, is_internal=False,
        )

    assert response is not None
    assert "TAVILY_API_KEY" in response
    resolve_mock.assert_called_once_with("session-1", "sk-tavily-abc123")
    assert adapter.delete_message.await_count == 2
    adapter.delete_message.assert_any_await("c1", "10")
    adapter.delete_message.assert_any_await("c1", "11")
    adapter.send.assert_not_awaited()  # no restart -> no separate pre-send


@pytest.mark.asyncio
async def test_provider_key_sends_confirmation_before_restarting():
    """Ruling 7 — client is told BEFORE the gateway restarts, not after."""
    runner = _make_runner()
    adapter = MagicMock()
    adapter.delete_message = AsyncMock(return_value=True)
    adapter.send = AsyncMock()
    runner._adapter_for_source = MagicMock(return_value=adapter)

    event = _make_event("sk-openai-abc", message_id="20")
    outcome = {
        "success": True,
        "stored_as": "OPENAI_API_KEY",
        "validated": True,
        "provider": True,
        "needs_restart": True,
        "message": "",
    }

    order: list[str] = []
    adapter.send = AsyncMock(side_effect=lambda *a, **k: order.append("sent"))

    def _fake_restart():
        order.append("restarted")

    with patch("tools.secret_capture_gateway.get_pending_for_session",
               return_value=object()), \
         patch("tools.secret_capture_gateway.resolve_secret_reply",
               return_value=outcome), \
         patch("hermes_cli.setup_wizard.gateway_ctl.restart_gateway",
               side_effect=_fake_restart) as restart_mock, \
         patch("threading.Thread") as thread_cls:
        # Run the target inline instead of spawning a real thread, so the
        # ordering assertion below is deterministic.
        def _inline_thread(target, **kwargs):
            thread = MagicMock()
            thread.start.side_effect = target
            return thread

        thread_cls.side_effect = _inline_thread

        response = await runner._maybe_handle_secret_capture_reply(
            event, event.source, is_internal=False,
        )

    assert response == ""
    restart_mock.assert_called_once()
    assert order == ["sent", "restarted"]


@pytest.mark.asyncio
async def test_slash_command_leaves_capture_pending():
    """A slash command sent while a capture is pending is not consumed as the reply."""
    runner = _make_runner()
    runner._adapter_for_source = MagicMock(return_value=MagicMock())
    event = _make_event("/status", cmd="status")

    with patch("tools.secret_capture_gateway.get_pending_for_session",
               return_value=object()), \
         patch("tools.secret_capture_gateway.resolve_secret_reply") as resolve_mock:
        response = await runner._maybe_handle_secret_capture_reply(
            event, event.source, is_internal=False,
        )

    assert response is None
    resolve_mock.assert_not_called()


@pytest.mark.asyncio
async def test_unauthorized_sender_cannot_touch_pending_capture():
    """An unauthorized sender must not be able to resolve someone else's capture."""
    runner = _make_runner(authorized=False)
    event = _make_event("sk-attacker-value")

    with patch("tools.secret_capture_gateway.get_pending_for_session",
               return_value=object()) as pending_mock, \
         patch("tools.secret_capture_gateway.resolve_secret_reply") as resolve_mock:
        response = await runner._maybe_handle_secret_capture_reply(
            event, event.source, is_internal=False,
        )

    assert response is None
    pending_mock.assert_not_called()
    resolve_mock.assert_not_called()


@pytest.mark.asyncio
async def test_no_pending_capture_is_a_passthrough():
    runner = _make_runner()
    event = _make_event("just a normal message")

    with patch("tools.secret_capture_gateway.get_pending_for_session",
               return_value=None):
        response = await runner._maybe_handle_secret_capture_reply(
            event, event.source, is_internal=False,
        )

    assert response is None


@pytest.mark.asyncio
async def test_pre_gateway_dispatch_hook_never_sees_a_secret_reply():
    """(d) — the hook must never receive a message carrying the secret.

    Drives the REAL ``_handle_message`` far enough to reach the hook call
    site: since the secret-capture intercept runs and returns BEFORE that
    call, a resolved secret reply must never let ``invoke_hook`` fire at
    all for that message.
    """
    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._session_key_for_source = MagicMock(return_value="session-1")

    async def _run_in_executor_with_context(func, *args):
        return func(*args)

    runner._run_in_executor_with_context = _run_in_executor_with_context
    runner._scale_to_zero_note_real_inbound = MagicMock()

    adapter = MagicMock()
    adapter.delete_message = AsyncMock(return_value=True)
    adapter.send = AsyncMock()
    runner._adapter_for_source = MagicMock(return_value=adapter)

    event = _make_event("sk-tavily-abc123", message_id="40")
    outcome = {
        "success": True,
        "stored_as": "TAVILY_API_KEY",
        "validated": None,
        "provider": False,
        "needs_restart": False,
        "message": "",
    }

    hook_mock = MagicMock(return_value=[])
    with patch("tools.secret_capture_gateway.get_pending_for_session",
               return_value=object()), \
         patch("tools.secret_capture_gateway.resolve_secret_reply",
               return_value=outcome), \
         patch("hermes_cli.lifecycle.invoke_hook", hook_mock):
        response = await runner._handle_message(event)

    assert response is not None
    hook_mock.assert_not_called()


@pytest.mark.asyncio
async def test_delete_failure_is_reported_to_the_client():
    runner = _make_runner()
    adapter = MagicMock()
    adapter.delete_message = AsyncMock(return_value=False)
    adapter.send = AsyncMock()
    runner._adapter_for_source = MagicMock(return_value=adapter)

    event = _make_event("sk-tavily-abc", message_id="30")
    outcome = {
        "success": True,
        "stored_as": "TAVILY_API_KEY",
        "validated": None,
        "provider": False,
        "needs_restart": False,
        "message": "",
    }
    with patch("tools.secret_capture_gateway.get_pending_for_session",
               return_value=object()), \
         patch("tools.secret_capture_gateway.resolve_secret_reply",
               return_value=outcome):
        response = await runner._maybe_handle_secret_capture_reply(
            event, event.source, is_internal=False,
        )

    assert response is not None
    assert "удалит" in response.lower() or "delete" in response.lower()
