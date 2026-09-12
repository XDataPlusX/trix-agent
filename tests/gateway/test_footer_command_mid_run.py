"""Regression, updated for the client-command-surface layer.

Originally this file pinned that ``/footer`` reaches its real handler while
an agent is running, instead of the busy catch-all rejection
(``"Agent is running — /<cmd> can't run mid-turn"``). At the time, the
running-agent block's mid-run dispatch table listed only a couple of
commands, so a ``busy_policy="dispatch"`` command like ``/footer`` (a pure
display toggle -- see ``_handle_footer_command``) fell through to that
catch-all and was rejected, forcing a ``/stop`` just to toggle a footer.

``docs/product/plans/2026-09-01-client-command-surface.md`` (Task 1)
disables ``/footer`` for the client surface -- see
``hermes_cli.trix_menu.DISABLED_COMMANDS["footer"]``: without an argument it
silently flips a ``config.yaml`` setting, which reads as "meddling with the
product" rather than "let me check something", and Trix's client never
edits ``config.yaml`` directly. ``/status`` is the named replacement. So the
regression this file originally guarded against -- "a command declared safe
mid-run must actually dispatch mid-run, not hit the busy catch-all" -- still
matters, but pinning it on ``/footer`` is no longer correct: ``/footer`` is
now expected to answer with the disabled explanation, identically whether
the agent is idle or busy (Ruling 9 in
``docs/product/specs/2026-09-01-trix-agent-client-command-surface-design.md``
-- the disabled check is the single choke point ahead of the busy/cold
split, so neither path can reach ``_handle_footer_command`` any more).

``test_disk_dispatches_to_handler_when_agent_running`` below carries the
original invariant forward on ``/disk`` -- a client-surface command with the
same "must answer mid-run, not just when idle" shape (its own comment in
``gateway/run.py`` says why: "Free space is asked about precisely when
something has ground to a halt, so /disk must answer mid-run").
``test_footer_answers_with_disabled_explanation_when_agent_running`` pins
the new, deliberately opposite expectation for ``/footer`` itself, so this
file keeps holding a real regression instead of quietly losing coverage.

Mirrors the runner-construction pattern of ``test_steer_command.py`` so the
same proven path through ``_handle_message`` reaches the running-agent
command dispatch.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(),
        message_id="m1",
    )


def _make_runner(session_entry: SessionEntry):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner, adapter


def _session_entry() -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


_BUSY_REJECTION = "can't run"


@pytest.mark.asyncio
async def test_disk_dispatches_to_handler_when_agent_running():
    """/disk (client-surface, not disabled) mid-run reaches
    _handle_disk_command, not the busy catch-all -- the invariant this file
    originally pinned on /footer, carried forward on a command that is
    still supposed to answer mid-run."""
    runner, _adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    runner._running_agents[sk] = MagicMock()

    handler = AsyncMock(return_value="12 GB free")
    runner._handle_disk_command = handler

    result = await runner._handle_message(_make_event("/disk"))

    handler.assert_awaited_once()
    assert result == "12 GB free"
    assert _BUSY_REJECTION not in (result or ""), (
        "/disk hit the busy catch-all instead of dispatching to its handler"
    )


@pytest.mark.asyncio
async def test_footer_answers_with_disabled_explanation_when_agent_running():
    """/footer is disabled (hermes_cli.trix_menu.DISABLED_COMMANDS) -- mid-run
    it must answer with the disabled explanation, not dispatch to
    _handle_footer_command and not hit the generic busy-reject
    ("can't run mid-turn", which promises the command works once the agent
    frees up -- false for a disabled command)."""
    import hermes_cli.trix_disabled_reply as _reply_mod

    from unittest.mock import patch

    runner, _adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    runner._running_agents[sk] = MagicMock()

    handler = AsyncMock(return_value="footer toggled")
    runner._handle_footer_command = handler

    with patch.object(
        _reply_mod, "disabled_command_reply", lambda name: f"DISABLED:{name}"
    ):
        result = await runner._handle_message(_make_event("/footer"))

    handler.assert_not_called()
    assert result == "DISABLED:footer"
    assert _BUSY_REJECTION not in (result or ""), (
        "/footer's disabled reply must not read as 'try again once the agent "
        "is free' -- that promise is false for a disabled command"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
