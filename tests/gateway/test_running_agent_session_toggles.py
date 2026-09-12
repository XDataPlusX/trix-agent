"""Regression tests, updated for the client-command-surface layer.

Originally this file pinned that ``/yolo`` and ``/verbose`` dispatch to
their real handlers while an agent is running, instead of hitting the
running-agent guard's catch-all rejection ("Agent is running —
/{cmd} can't run mid-turn", PR #12334). At the time, a small allowlist in
that guard bypassed the catch-all for these two specifically.

``docs/product/plans/2026-09-01-client-command-surface.md`` (Task 1) now
disables both for the client surface -- see
``hermes_cli.trix_menu.DISABLED_COMMANDS["yolo"]`` and ``["verbose"]``:
``/yolo`` removes dangerous-command approval with no second question (a
protection spec 9 put there on purpose); ``/verbose`` without an argument
silently flips a ``config.yaml`` setting the config-gate closes in this
build anyway. Ruling 9 in
``docs/product/specs/2026-09-01-trix-agent-client-command-surface-design.md``
names these two explicitly: they were the reason the mid-run disabled-
command gap mattered in the first place -- ``/yolo`` and ``/footer`` (see
``test_footer_command_mid_run.py``) are the commands the disabled layer
exists to close, and both used to dispatch for real if typed while the
agent was busy.

These tests now lock in the opposite of what they originally guarded:
neither command's real handler runs mid-run, and both answer with the
disabled explanation -- identically to the idle-session reply, never the
generic busy-reject ("can't run mid-turn"), which would falsely promise the
command works once the agent frees up.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    """Minimal GatewayRunner with an active running agent for this session."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._service_tier = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()

    # Simulate agent actively running for this session so the guard fires.
    # Note: the stale-eviction branch calls agent.get_activity_summary() and
    # compares seconds_since_activity against HERMES_AGENT_TIMEOUT. Return a
    # dict with recent activity so the eviction path doesn't clear our
    # fake running agent before the toggle guard runs.
    import time
    sk = build_session_key(_make_source())
    agent_mock = MagicMock()
    agent_mock.get_activity_summary.return_value = {
        "seconds_since_activity": 0.0,
        "last_activity_desc": "api_call",
        "api_call_count": 1,
        "max_iterations": 60,
    }
    runner._running_agents[sk] = agent_mock
    runner._running_agents_ts[sk] = time.time()
    return runner


@pytest.mark.asyncio
async def test_yolo_answers_disabled_explanation_mid_run():
    """/yolo is disabled -- mid-run it must answer with the disabled
    explanation, not dispatch to _handle_yolo_command and not hit the
    generic busy-reject."""
    import hermes_cli.trix_disabled_reply as _reply_mod

    runner = _make_runner()
    runner._handle_yolo_command = AsyncMock(
        return_value="⚡ YOLO mode **ON** for this session"
    )

    with patch.object(
        _reply_mod, "disabled_command_reply", lambda name: f"DISABLED:{name}"
    ):
        result = await runner._handle_message(_make_event("/yolo"))

    runner._handle_yolo_command.assert_not_called()
    assert result == "DISABLED:yolo"
    assert "can't run mid-turn" not in (result or "")


@pytest.mark.asyncio
async def test_verbose_answers_disabled_explanation_mid_run():
    """/verbose is disabled -- mid-run it must answer with the disabled
    explanation, not dispatch to _handle_verbose_command and not hit the
    generic busy-reject."""
    import hermes_cli.trix_disabled_reply as _reply_mod

    runner = _make_runner()
    runner._handle_verbose_command = AsyncMock(return_value="tool progress: new")

    with patch.object(
        _reply_mod, "disabled_command_reply", lambda name: f"DISABLED:{name}"
    ):
        result = await runner._handle_message(_make_event("/verbose"))

    runner._handle_verbose_command.assert_not_called()
    assert result == "DISABLED:verbose"
    assert "can't run mid-turn" not in (result or "")
