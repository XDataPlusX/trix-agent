"""Regression: a disabled slash command (``hermes_cli.trix_menu.DISABLED_COMMANDS``)
must never dispatch — on the cold path AND the mid-run (busy-agent) path.

Ruling 9 (``docs/product/specs/2026-09-01-trix-agent-client-command-surface-design.md``,
plan Task 4 in ``docs/product/plans/2026-09-01-client-command-surface.md``) found
that the busy dispatch path (``_dispatch_busy_slash_command``, entered from
``_handle_message`` when ``_is_session_running()`` is true) resolved the
command's canonical name through a narrower, independently-derived local
lookup, skipping quick-command alias expansion and the ``command:<name>``
plugin hook entirely — while the cold dispatch path further below did the
full resolution. A disabled-command check wired into only one of the two
would still let the command through the other, and the busy path's own
catch-all (``trix.busy.reject_generic``) implicitly *promises* the command
will work once the agent frees up — a lie for a command that never will.

``gateway.run.GatewayRunner._resolve_slash_dispatch()`` now performs alias
resolution, quick-alias expansion, access control, and the hook exactly
once, ahead of the busy/cold split; ``GatewayRunner._handle_message()``
checks ``hermes_cli.trix_menu.is_disabled_in_gateway()`` right after that
single call, before branching on session state. These tests pin that this
check is reachable and identical on both branches.
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
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    # Boundary for the "never reaches the model" assertions: any code path
    # that actually calls the provider goes through _run_agent (the thin
    # wrapper immediately around AIAgent construction / run_conversation).
    # Raising instead of only recording a call means a leak fails loudly at
    # the leak site, not several frames later at an unrelated assertion.
    runner._run_agent = AsyncMock(
        side_effect=AssertionError("slash command text leaked through to the agent")
    )
    return runner


def _mark_busy(runner) -> None:
    runner._running_agents[build_session_key(_make_source())] = MagicMock()


@pytest.fixture(autouse=True)
def _stub_disabled_reply(monkeypatch):
    """Replace the Task 5 reply builder with a fixed marker string so these
    tests assert on ROUTING — did dispatch stop here, not on the Russian
    wording that module (a parallel task) owns. Patched at its defining
    module so the lazy ``from hermes_cli.trix_disabled_reply import
    disabled_command_reply`` inside ``gateway.run`` picks it up.
    """
    import hermes_cli.trix_disabled_reply as _reply_mod

    monkeypatch.setattr(
        _reply_mod, "disabled_command_reply", lambda name: f"DISABLED:{name}"
    )


# The 8 commands Ruling 9 named as reachable through the busy dispatch
# table's ``plain`` dict before this fix (``egress`` has its own busy
# handler and is covered separately below).
DISABLED_MID_RUN_TABLE_COMMANDS = (
    "context", "pause", "kanban", "yolo", "verbose", "footer", "profile",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
@pytest.mark.parametrize("command", DISABLED_MID_RUN_TABLE_COMMANDS)
async def test_disabled_command_never_dispatches(command, busy):
    """Every command Ruling 9 named as reachable mid-run before this fix
    must not run its real handler, on either session state. This is the
    test that must fail red on ``main`` before the fix — /yolo and /footer
    mid-run previously ran their real handlers.
    """
    runner = _make_runner()
    if busy:
        _mark_busy(runner)

    handler_name = f"_handle_{command.replace('-', '_')}_command"
    handler = AsyncMock(return_value=f"REAL HANDLER RAN: {command}")
    setattr(runner, handler_name, handler)

    result = await runner._handle_message(_make_event(f"/{command}"))

    handler.assert_not_called()
    assert result == f"DISABLED:{command}"


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
async def test_egress_busy_handler_not_reached_when_disabled(busy):
    """/egress dispatches through its own busy handler
    (``_busy_egress_command`` -> ``format_status_text()``), not the ``plain``
    table -- and through a direct inline cold-path call, not a
    ``_handle_egress_command`` method. Both call sites must be unreachable
    once /egress is disabled.
    """
    runner = _make_runner()
    if busy:
        _mark_busy(runner)

    status_fn = MagicMock(return_value="Egress proxy status\nEnabled: no")
    import hermes_cli.proxy_cli as _proxy_cli

    orig = _proxy_cli.format_status_text
    _proxy_cli.format_status_text = status_fn
    try:
        result = await runner._handle_message(_make_event("/egress"))
    finally:
        _proxy_cli.format_status_text = orig

    status_fn.assert_not_called()
    assert result == "DISABLED:egress"


@pytest.mark.asyncio
async def test_disabled_command_busy_reply_matches_idle_reply():
    """A disabled command's mid-run reply must be byte-identical to its idle
    reply -- never ``trix.busy.reject_generic`` ("can't run mid-turn"),
    which promises the command will work once the agent frees up. That
    promise is false for a disabled command.
    """
    idle_runner = _make_runner()
    idle_result = await idle_runner._handle_message(_make_event("/yolo"))

    busy_runner = _make_runner()
    _mark_busy(busy_runner)
    busy_result = await busy_runner._handle_message(_make_event("/yolo"))

    assert idle_result == busy_result == "DISABLED:yolo"
    assert "can't run" not in (busy_result or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
async def test_disabled_command_alias_also_blocked(busy):
    """/ctx is a registered alias of /context (disabled) -- typing the
    alias must not dodge the disablement.
    """
    runner = _make_runner()
    if busy:
        _mark_busy(runner)
    handler = AsyncMock(return_value="REAL /context HANDLER RAN")
    runner._handle_context_command = handler

    result = await runner._handle_message(_make_event("/ctx"))

    handler.assert_not_called()
    assert result == "DISABLED:context"


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
async def test_curated_false_restores_execution(monkeypatch, busy):
    """platforms.telegram.extra.command_menu.curated: false is our own
    debugging escape hatch -- it must restore execution of an otherwise-
    disabled command, on both dispatch paths.
    """
    import hermes_cli.config as _config_mod

    monkeypatch.setattr(
        _config_mod,
        "read_raw_config",
        lambda: {
            "platforms": {"telegram": {"extra": {"command_menu": {"curated": False}}}}
        },
    )
    runner = _make_runner()
    if busy:
        _mark_busy(runner)
    handler = AsyncMock(return_value="REAL HANDLER RAN")
    runner._handle_footer_command = handler

    result = await runner._handle_message(_make_event("/footer"))

    handler.assert_awaited_once()
    assert result == "REAL HANDLER RAN"


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
async def test_curator_disabled_command_never_reaches_agent(busy):
    """/curator has NO branch at all in the cold-path if-chain (there is no
    gateway handler for the skill-maintenance subsystem) -- before the
    disabled layer existed, a client typing /curator fell all the way
    through to plain-text dispatch and its raw text reached the model (the
    defect Task 6 of the plan names). Pinned on both session states since
    _resolve_slash_dispatch + the disabled check are the single choke point
    for both.
    """
    runner = _make_runner()
    if busy:
        _mark_busy(runner)

    result = await runner._handle_message(_make_event("/curator"))

    runner._run_agent.assert_not_called()
    assert result == "DISABLED:curator"


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
async def test_whoami_always_allowed_floor_does_not_bypass_disabled(busy):
    """``_check_slash_access``'s always-allowed floor (/help, /whoami) is an
    ACCESS-CONTROL exemption ("non-admins may run these two even when
    otherwise restricted"), not an execution exemption. /whoami is disabled
    and must still answer with the disabled explanation, floor or not.
    """
    runner = _make_runner()
    if busy:
        _mark_busy(runner)
    handler = AsyncMock(return_value="REAL /whoami HANDLER RAN")
    runner._handle_whoami_command = handler

    result = await runner._handle_message(_make_event("/whoami"))

    handler.assert_not_called()
    assert result == "DISABLED:whoami"


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
async def test_quick_command_alias_to_disabled_command_is_blocked(busy):
    """The 'two insertions instead of one resolver' alternative Ruling 9
    rejected does not close this gap: a ``quick_commands`` entry of
    ``type: alias`` resolves ``canonical`` from a name that isn't in the
    gateway's built-in registry at all -- if that resolution happened
    separately on each dispatch path (or only on the cold path, as it did
    before this fix), a client could reach a disabled command by typing an
    operator-configured alias for it instead of its own name. The shared
    resolver expands the alias BEFORE the disabled check runs, on both
    paths, so this must be blocked exactly like typing /yolo directly.
    """
    runner = _make_runner()
    if busy:
        _mark_busy(runner)
    runner.config.quick_commands = {"go-yolo": {"type": "alias", "target": "/yolo"}}
    handler = AsyncMock(return_value="REAL /yolo HANDLER RAN")
    runner._handle_yolo_command = handler

    result = await runner._handle_message(_make_event("/go-yolo"))

    handler.assert_not_called()
    assert result == "DISABLED:yolo"


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
async def test_hook_rewrite_to_disabled_command_is_blocked(busy):
    """The other gap Ruling 9 named: a plugin's ``command:<name>`` hook can
    return ``{"decision": "rewrite", "command_name": ...}`` to redirect
    dispatch to a different command. Both of the old two-insertion call
    sites checked ``is_disabled_in_gateway`` (conceptually, in the fixed
    version) against canonical BEFORE this hook ran, so a rewrite landing
    on a disabled command would have slipped through on every path. The
    shared resolver runs the hook first and checks the POST-rewrite
    canonical, so this must be blocked too.
    """
    runner = _make_runner()
    if busy:
        _mark_busy(runner)

    async def _emit_collect(event_type, ctx):
        if event_type == "command:status":
            return [{"decision": "rewrite", "command_name": "yolo", "raw_args": ""}]
        return []

    runner.hooks.emit_collect = AsyncMock(side_effect=_emit_collect)
    handler = AsyncMock(return_value="REAL /yolo HANDLER RAN")
    runner._handle_yolo_command = handler
    runner._handle_status_command = AsyncMock(return_value="REAL /status HANDLER RAN")

    result = await runner._handle_message(_make_event("/status"))

    handler.assert_not_called()
    runner._handle_status_command.assert_not_called()
    assert result == "DISABLED:yolo"


@pytest.mark.asyncio
async def test_command_hook_now_fires_on_busy_path():
    """Documents the deliberate behavior change named in Ruling 9: the
    ``command:<name>`` plugin hook used to fire only on the cold path
    (a busy session never reached the code that emitted it). The shared
    resolver now runs before the busy/cold split, so a recognized,
    NON-disabled command fires the hook while the agent is busy too.
    """
    runner = _make_runner()
    _mark_busy(runner)
    calls = []

    async def _emit_collect(event_type, ctx):
        calls.append(event_type)
        return []

    runner.hooks.emit_collect = AsyncMock(side_effect=_emit_collect)
    runner._handle_status_command = AsyncMock(return_value="status ok")

    result = await runner._handle_message(_make_event("/status"))

    assert calls == ["command:status"]
    assert result == "status ok"


# ---------------------------------------------------------------------------
# Task 6 (docs/product/plans/2026-09-01-client-command-surface.md): the
# defect this test class guards has its own history independent of the
# disabled-command layer above. /curator was listed in GATEWAY_KNOWN_COMMANDS
# with no branch anywhere in _handle_message's canonical if-chain and no
# entry in DISABLED_COMMANDS at the time -- so its raw text fell all the way
# through the if-chain to the plain-text agent dispatch and reached the
# model as an ordinary user turn. Task 4's disabled-command check closed
# this as a side effect for /curator specifically (it is disabled now), but
# the defect class -- "a recognized command with no branch silently becomes
# a message to the model" -- has no test of its own and would reopen
# silently the day a fifth command is added to the registry without a
# handler branch. This section is that test, and it must survive whatever
# the plan/spec later decide about command composition.
#
# Three commands legitimately reach the agent: their branch in
# gateway/run.py deliberately rewrites `event.text` to a computed prompt
# and falls through on purpose (each site says so in a comment) --
# /queue and /steer (kept dispatchable to the client), and /blueprint
# (Task 9e: a matched catalog entry seeds the agent conversationally).
# /moa's branch does the exact same rewrite-and-fall-through, and the plan's
# own Task 6 text names {queue, steer, moa} as the set -- but /moa is listed
# in hermes_cli/trix_menu.py DISABLED_COMMANDS (decided the same day, Task
# 1/9d's sibling ruling) specifically because that real call has no
# configured provider for the client and silently burns a turn. Disabled
# commands never reach this if-chain at all (they are intercepted above it),
# so /moa is correctly "does not reach the agent" today -- not because its
# branch stopped rewriting, but because the branch is unreachable. Verified
# by running every command through the real dispatcher (not by reading
# gateway/run.py): the current, true exception set is {blueprint, queue,
# steer}, not the plan's {queue, steer, moa}. If /moa is ever re-enabled
# without also being given the plan's discipline, this test starts failing
# for /moa the moment it does -- which is exactly the protection this test
# exists to provide.
EXPECTED_AGENT_FALLTHROUGH_COMMANDS = frozenset({"blueprint", "queue", "steer"})

# /blueprint only rewrites-and-falls-through when its argument matches a
# real catalog entry (cron/blueprint_catalog.py) -- anything else returns
# a "no match" reply without touching the agent. Every other command gets a
# generic non-empty payload (empty payloads short-circuit several commands
# into their own "Usage: ..." reply before canonical dispatch even runs).
_FALLTHROUGH_PAYLOAD_OVERRIDES = {"blueprint": "meal-plan"}


@pytest.mark.asyncio
async def test_only_named_commands_convert_to_a_plain_agent_message():
    """Sweep every gateway-known command (built-in canonical names AND
    their aliases -- ``GATEWAY_KNOWN_COMMANDS``) through the idle dispatch
    path and record which ones actually invoke ``_run_agent``. Only the
    named exception set may. This must fail red the moment a command is
    added to the registry (or a disabled command's DisabledCommand entry is
    removed) without also getting a real branch in the canonical if-chain
    -- the exact shape of the original /curator defect.

    A handler crashing on this test's minimal stub state (missing runner
    attributes some real handlers need) is a different, already-covered
    concern -- handler correctness has its own tests. The only signal this
    test reads is ``_run_agent.call_count``, which AsyncMock records at
    call time regardless of what happens afterward, so a downstream crash
    can never hide a leak that already happened.
    """
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command

    reached: dict[str, str] = {}
    for name in sorted(GATEWAY_KNOWN_COMMANDS):
        cmd = resolve_command(name)
        canonical = cmd.name if cmd else name
        payload = _FALLTHROUGH_PAYLOAD_OVERRIDES.get(canonical, "test payload here")
        runner = _make_runner()
        try:
            await runner._handle_message(_make_event(f"/{name} {payload}"))
        except Exception:
            pass
        if runner._run_agent.call_count:
            reached[name] = canonical

    assert set(reached.values()) == EXPECTED_AGENT_FALLTHROUGH_COMMANDS, reached


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
