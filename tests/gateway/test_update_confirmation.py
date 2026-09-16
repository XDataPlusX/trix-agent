"""``/update`` confirmation (Task 9a, Ruling 8).

Owner decision 2026-09-01: ``/restart`` returns to the same state in
seconds and stays instant. ``/update`` changes code over minutes and, on
failure, the updater prints ``git fetch origin && git reset --hard …`` --
commands the client cannot run themselves -- so ``/update`` is gated
through the same ``_maybe_confirm_destructive_slash`` primitive already
wired to ``/new`` and ``/undo``.

Review found two ways the naive "wrap /update's cold-path branch" fix
would still leak:

  1. **Busy-agent bypass.** ``/update`` has ``busy_policy="dispatch"`` and
     used to be dispatched straight to ``_handle_update_command`` from the
     mid-run ``plain`` table (``gateway/run.py``'s
     ``_dispatch_busy_slash_command``), which never reaches the cold-path
     branch at all. A confirmation wired into only the cold path is a
     no-op while an agent happens to be running -- exactly when a client
     digging through the menu is likely to hit it.
  2. **Shared opt-out key.** The confirmation gate is one config value,
     ``approvals.destructive_slash_confirm``, shared by every command
     wired through the primitive. Clicking "Always Approve" on /new (the
     single most-used menu command) silently waives it for /update too.
     /update must never offer "always" and must never even consult that
     shared key.

Every test here drives the real dispatcher (``GatewayRunner._handle_message``),
never ``_maybe_confirm_destructive_slash`` in isolation -- the busy-path
leak specifically lived in a second call site the isolated-call style of
test can't see.
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
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    """Enough of a bare ``GatewayRunner`` to drive ``_handle_message`` for
    real, on either session state -- merges the harness shapes of
    ``tests/gateway/test_disabled_commands.py`` (busy/idle dispatch) and
    ``tests/gateway/test_destructive_slash_confirm.py`` (the confirm-gate
    plumbing: ``_read_user_config``, ``_slash_confirm_counter``,
    ``_session_key_for_source``).
    """
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    # No send_slash_confirm override -> button render "fails" (MagicMock
    # attribute access still succeeds, but calling it returns a MagicMock,
    # not an awaitable with .success) -> _request_slash_confirm falls back
    # to the text path, same as test_destructive_slash_confirm.py.
    adapter.send_slash_confirm = AsyncMock(return_value=None)
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
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._thread_metadata_for_source = lambda *a, **kw: None
    runner._reply_anchor_for_event = lambda _e: None
    import itertools as _it
    runner._slash_confirm_counter = _it.count(1)
    from gateway.run import GatewayRunner as _GR
    runner._session_key_for_source = _GR._session_key_for_source.__get__(runner, _GR)
    runner._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": True}}
    # Boundary for "never reaches the model" -- unused by these tests
    # directly, but several client-surface commands swept in the table
    # test below fall through to plain-text agent dispatch, and a loud
    # failure there is more useful than a silent one.
    runner._run_agent = AsyncMock(
        side_effect=AssertionError("slash command text leaked through to the agent")
    )
    return runner


def _mark_busy(runner) -> None:
    runner._running_agents[build_session_key(_make_source())] = MagicMock()


@pytest.fixture(autouse=True)
def _clear_pending_confirms():
    """Module-level state in ``tools.slash_confirm`` -- never leak a
    pending confirm from one test into the next."""
    from tools import slash_confirm as _slash_confirm_mod
    session_key = build_session_key(_make_source())
    _slash_confirm_mod.clear(session_key)
    yield
    _slash_confirm_mod.clear(session_key)


# ---------------------------------------------------------------------------
# Step 1: confirmation actually blocks execution until resolved -- driven
# through the real dispatcher, not by calling the confirm primitive in
# isolation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_does_not_run_before_confirmation():
    from tools import slash_confirm as _slash_confirm_mod

    runner = _make_runner()
    real_update = AsyncMock(return_value="Update started.")
    runner._handle_update_command = real_update

    session_key = build_session_key(_make_source())
    await runner._handle_message(_make_event("/update"))

    real_update.assert_not_called()
    pending = _slash_confirm_mod.get_pending(session_key)
    assert pending is not None
    assert pending["command"] == "update"


@pytest.mark.asyncio
async def test_update_runs_after_once_confirmation():
    from tools import slash_confirm as _slash_confirm_mod

    runner = _make_runner()
    real_update = AsyncMock(return_value="Update started.")
    runner._handle_update_command = real_update
    session_key = build_session_key(_make_source())

    await runner._handle_message(_make_event("/update"))
    pending = _slash_confirm_mod.get_pending(session_key)
    assert pending is not None

    resolved = await _slash_confirm_mod.resolve(
        session_key, pending["confirm_id"], "once",
    )

    real_update.assert_awaited_once()
    assert resolved is not None and "Update started." in resolved


@pytest.mark.asyncio
async def test_update_never_runs_after_cancel():
    from tools import slash_confirm as _slash_confirm_mod

    runner = _make_runner()
    real_update = AsyncMock(return_value="Update started.")
    runner._handle_update_command = real_update
    session_key = build_session_key(_make_source())

    await runner._handle_message(_make_event("/update"))
    pending = _slash_confirm_mod.get_pending(session_key)
    assert pending is not None

    await _slash_confirm_mod.resolve(session_key, pending["confirm_id"], "cancel")

    real_update.assert_not_called()


# ---------------------------------------------------------------------------
# Step 2b: the confirmation must be reachable through BOTH dispatch paths --
# idle (cold canonical if-chain) AND busy (_dispatch_busy_slash_command's
# ``plain`` table, entered before the cold chain is ever reached).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
async def test_update_requires_confirmation_on_both_dispatch_paths(busy):
    from tools import slash_confirm as _slash_confirm_mod

    runner = _make_runner()
    if busy:
        _mark_busy(runner)
    real_update = AsyncMock(return_value="Update started.")
    runner._handle_update_command = real_update
    session_key = build_session_key(_make_source())

    await runner._handle_message(_make_event("/update"))

    real_update.assert_not_called()
    pending = _slash_confirm_mod.get_pending(session_key)
    assert pending is not None and pending["command"] == "update"


# ---------------------------------------------------------------------------
# Step 2c: /update must never offer "always", and must never consult the
# shared ``approvals.destructive_slash_confirm`` gate that /new's "Always
# Approve" button writes to.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_prompt_never_offers_always():
    """The choke point between the confirm primitive and the adapter is
    ``_request_slash_confirm`` -- assert it is called with
    ``allow_always=False`` for /update, and that the assembled prompt text
    never mentions an "always" option (no button, no typed fallback)."""
    runner = _make_runner()
    captured = {}

    async def _spy(*, event, command, title, message, handler, allow_always=True):
        captured["command"] = command
        captured["allow_always"] = allow_always
        captured["message"] = message
        return "stubbed"

    runner._request_slash_confirm = _spy

    await runner._handle_message(_make_event("/update"))

    assert captured["command"] == "update"
    assert captured["allow_always"] is False
    assert "always" not in captured["message"].lower()
    assert "/always" not in captured["message"]


@pytest.mark.asyncio
async def test_new_prompt_still_offers_always():
    """Control for the test above: /new (unaffected by Ruling 8) still
    offers the "always" option -- proves the assertion above discriminates
    real behavior rather than being vacuously true for every command."""
    runner = _make_runner()
    captured = {}

    async def _spy(*, event, command, title, message, handler, allow_always=True):
        captured["command"] = command
        captured["allow_always"] = allow_always
        captured["message"] = message
        return "stubbed"

    runner._request_slash_confirm = _spy

    await runner._handle_message(_make_event("/new"))

    assert captured["command"] == "new"
    assert captured["allow_always"] is True
    assert "always" in captured["message"].lower()


@pytest.mark.asyncio
async def test_update_still_confirms_after_new_always_approve(monkeypatch):
    """The obход Ruling 8 names by name: one tap of "Always Approve" on
    /new persists a SHARED config key. A fresh /update dispatch that reads
    the same (now-opted-out) config must still ask -- because /update's
    gate check ignores that key entirely (allow_always=False skips it),
    not because the key happens to still be True."""
    from tools import slash_confirm as _slash_confirm_mod

    approvals_state = {"destructive_slash_confirm": True}

    def _fake_save(path, value):
        assert path == "approvals.destructive_slash_confirm"
        approvals_state["destructive_slash_confirm"] = value
        return True

    import cli as cli_mod
    monkeypatch.setattr(cli_mod, "save_config_value", _fake_save)

    session_key = build_session_key(_make_source())

    # 1. /new -> "Always Approve": persists the shared opt-out.
    new_runner = _make_runner()
    new_runner._read_user_config = lambda: {"approvals": dict(approvals_state)}
    new_runner._handle_reset_command = AsyncMock(return_value="reset done")
    await new_runner._handle_message(_make_event("/new"))
    new_pending = _slash_confirm_mod.get_pending(session_key)
    assert new_pending is not None
    await _slash_confirm_mod.resolve(session_key, new_pending["confirm_id"], "always")
    assert approvals_state["destructive_slash_confirm"] is False

    # 2. A later /update dispatch reads the SAME (now-False) config gate.
    #    If /update consulted it the way /new does, this would skip
    #    straight to execution -- it must not.
    update_runner = _make_runner()
    update_runner._read_user_config = lambda: {"approvals": dict(approvals_state)}
    update_real = AsyncMock(return_value="Update started.")
    update_runner._handle_update_command = update_real
    await update_runner._handle_message(_make_event("/update"))

    update_real.assert_not_called()
    update_pending = _slash_confirm_mod.get_pending(session_key)
    assert update_pending is not None and update_pending["command"] == "update"


# ---------------------------------------------------------------------------
# Step 2: invariant, not a snapshot of absence. The set of commands that
# require confirmation before executing equals {new, undo, update} --
# proved by sweeping every recognized, non-disabled command through the
# real dispatcher on BOTH session states and recording which ones actually
# reached the confirm primitive. "/restart never asks" alone would go red
# on a correct refactor that adds a fourth confirmable command; this
# doesn't, because it names the full set both directions.
# ---------------------------------------------------------------------------

EXPECTED_CONFIRMATION_REQUIRED_COMMANDS = frozenset({"new", "undo", "update"})


async def _sweep_confirmation_required_commands() -> set:
    """Idle-session sweep. Busy-session confirmation reachability is its
    own, narrower claim -- ``test_update_requires_confirmation_on_both_
    dispatch_paths`` above -- deliberately not generalized to this full
    sweep: /new's busy path (``_busy_new_command``) interrupts and
    dispatches immediately by design (predates this task, #2170 --
    queuing /new as plain text mid-run fed the agent broken history), and
    /undo has no ``busy_policy`` at all so it hits the generic busy-reject
    text instead of its handler. Neither reaches the confirm primitive
    while busy, on purpose, for reasons unrelated to Ruling 8. Asserting
    the {new, undo, update} set on the busy sweep would therefore fail on
    unrelated, correct behavior -- see "Found beyond this task" in the
    Task 9a report instead of encoding it here as a red herring.
    """
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command
    from hermes_cli.trix_menu import is_disabled_in_gateway

    triggered: set = set()

    async def _spy(*, event, command, title, detail, execute, allow_always=True):
        triggered.add(command)
        return "stubbed"

    for name in sorted(GATEWAY_KNOWN_COMMANDS):
        cmd = resolve_command(name)
        canonical = cmd.name if cmd else name
        if is_disabled_in_gateway(canonical):
            # Disabled commands never reach any dispatch branch at all --
            # they cannot trigger the confirm primitive either way, and
            # their own non-execution is covered by
            # tests/gateway/test_disabled_commands.py, not here.
            continue
        runner = _make_runner()
        runner._maybe_confirm_destructive_slash = _spy
        try:
            await runner._handle_message(_make_event(f"/{name} test payload"))
        except Exception:
            pass

    return triggered


@pytest.mark.asyncio
async def test_confirmation_required_set_equals_new_undo_update():
    triggered = await _sweep_confirmation_required_commands()
    assert triggered == EXPECTED_CONFIRMATION_REQUIRED_COMMANDS, triggered


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
