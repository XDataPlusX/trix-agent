"""Spec 19, Ruling 6 — the adapter-side active-session guard must also let a
secret-capture reply through while the agent is blocked waiting for it.

This is the FIRST of the two guards Ruling 6 requires. Missing it (as the
spec's first revision did) queues the client's reply in ``_pending_messages``
instead of routing it to the runner's resolver, so the agent stays blocked
until the capture times out — the exact deadlock PR #4926 already fixed for
``/approve``/``/deny`` and clarify replies.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Minimal telegram stub so importing gateway.platforms.base does not pull in
# the real python-telegram-bot dependency (mirrors
# tests/gateway/test_active_session_text_merge.py).
_tg = sys.modules.get("telegram") or types.ModuleType("telegram")
_tg.constants = sys.modules.get("telegram.constants") or types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.PRIVATE = "private"
_ct.GROUP = "group"
_ct.SUPERGROUP = "supergroup"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource, build_session_key


def _make_event(text: str, chat_id: str = "12345") -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="dm",
        user_id="u1",
        user_name=None,
    )
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id="m1")


class _DummyAdapter(BasePlatformAdapter):  # type: ignore[misc]
    async def connect(self, *, is_reconnect: bool = False):
        pass

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return None

    async def send(self, *args, **kwargs):
        return SendResult(success=True, message_id="x")


def _make_adapter() -> BasePlatformAdapter:
    adapter = object.__new__(_DummyAdapter)
    adapter.config = PlatformConfig(enabled=True, token="***")
    adapter.platform = Platform.TELEGRAM
    adapter._message_handler = AsyncMock(return_value=None)
    adapter._busy_session_handler = None
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._session_tasks = {}
    adapter._background_tasks = set()
    adapter._post_delivery_callbacks = {}
    adapter._expected_cancelled_tasks = set()
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._fatal_error_handler = None
    adapter._running = True
    adapter._busy_text_mode = "queue"
    adapter._busy_text_debounce_seconds = 0.1
    adapter._busy_text_hard_cap_seconds = 1.0
    adapter._text_debounce = {}
    adapter._auto_tts_default = False
    adapter._auto_tts_enabled_chats = set()
    adapter._auto_tts_disabled_chats = set()
    adapter._typing_paused = set()
    return adapter


@pytest.mark.asyncio
async def test_secret_reply_bypasses_active_session_guard():
    """A pending secret capture routes the reply to _message_handler inline,
    NOT into _pending_messages behind the busy agent."""
    adapter = _make_adapter()
    reply = _make_event("sk-tavily-abc123")
    session_key = build_session_key(reply.source)
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._message_handler = AsyncMock(return_value=None)

    with patch("tools.clarify_gateway.get_pending_for_session", return_value=None), \
         patch("tools.secret_capture_gateway.has_pending", return_value=True):
        await adapter.handle_message(reply)

    adapter._message_handler.assert_awaited_once_with(reply)
    assert session_key not in adapter._pending_messages
    assert session_key not in adapter._text_debounce


@pytest.mark.asyncio
async def test_ordinary_text_still_queues_when_no_capture_pending():
    """Sanity check: without a pending capture, normal busy-session queueing
    still applies (this bypass must be narrowly scoped)."""
    adapter = _make_adapter()
    adapter._busy_text_mode = ""  # direct-merge, no debounce noise
    reply = _make_event("just chatting")
    session_key = build_session_key(reply.source)
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._message_handler = AsyncMock(return_value=None)

    with patch("tools.clarify_gateway.get_pending_for_session", return_value=None), \
         patch("tools.secret_capture_gateway.has_pending", return_value=False):
        await adapter.handle_message(reply)

    adapter._message_handler.assert_not_awaited()
    assert session_key in adapter._pending_messages
