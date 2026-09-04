"""The /new tip follows the curated client surface, not the upstream corpus.

``hermes_cli.tips.TIPS`` is written for someone at a terminal: it advertises
commands we disable for the client, ``cli_only`` commands that answer
"unknown command" in a chat (``/clear``, ``/paste``, ``/copy``), env vars and
``config.yaml`` edits the client cannot reach, and keybindings. Curation
closed the menu, ``/help`` and ``/commands``; the tip appended to every
``/new`` was the remaining exit, and a client did follow it into a dead end.

So: no tip while curation is on. ``curated: false`` — our debugging escape
hatch on a client machine — restores it with the rest of the built-in
surface.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key

PINNED_TIP = "pinned-upstream-tip-body"


def _make_runner():
    """A GatewayRunner with just enough wiring for _handle_reset_command."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_db = AsyncMock()
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="12345",
        chat_id="67890",
        user_name="testuser",
    )
    session_key = build_session_key(source)
    entry = SessionEntry(
        session_key=session_key,
        session_id="sess-new",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store.reset_session.return_value = entry
    runner.session_store._entries = {session_key: entry}
    runner.session_store._generate_session_key.return_value = session_key

    event = MessageEvent(text="/new", source=source)
    return runner, event


def _pin_corpus(monkeypatch, raw_config):
    """Pin the tip body and the config curation answers to fixed values."""
    monkeypatch.setattr("hermes_cli.tips.get_random_tip", lambda: PINNED_TIP)
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: raw_config)


def _curated(value):
    return {"platforms": {"telegram": {"extra": {"command_menu": {"curated": value}}}}}


@pytest.mark.asyncio
async def test_curated_surface_appends_no_tip(monkeypatch):
    """Default posture: the client's /new reply carries no upstream tip."""
    _pin_corpus(monkeypatch, {})
    runner, event = _make_runner()

    reply = str(await runner._handle_reset_command(event))

    assert PINNED_TIP not in reply


@pytest.mark.asyncio
async def test_curated_false_restores_the_upstream_tip(monkeypatch):
    """The debugging escape hatch brings the tip back with the rest of the surface."""
    _pin_corpus(monkeypatch, _curated(False))
    runner, event = _make_runner()

    reply = str(await runner._handle_reset_command(event))

    assert PINNED_TIP in reply


@pytest.mark.asyncio
async def test_unreadable_config_suppresses_the_tip(monkeypatch):
    """A config.yaml we cannot read defaults to curated — and must not raise."""
    monkeypatch.setattr("hermes_cli.tips.get_random_tip", lambda: PINNED_TIP)

    def _boom():
        raise OSError("config.yaml unreadable")

    monkeypatch.setattr("hermes_cli.config.read_raw_config", _boom)
    runner, event = _make_runner()

    reply = str(await runner._handle_reset_command(event))

    assert PINNED_TIP not in reply
    assert reply.strip()


def test_no_client_menu_command_is_cli_only():
    """The invariant the /clear tip slipped past.

    Every name on the client's menu must actually dispatch in the gateway.
    A ``cli_only`` entry here would answer "unknown command" when the client
    taps it — which is exactly what the upstream tip corpus told them to do
    with ``/clear``.
    """
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
    from hermes_cli.trix_menu import CLIENT_MENU_COMMANDS

    missing = [n for n in CLIENT_MENU_COMMANDS if n not in GATEWAY_KNOWN_COMMANDS]
    assert not missing, f"on the client menu but not dispatchable: {missing}"


def test_confirm_optout_message_names_only_the_commands_it_covers():
    """The opt-out plaque must not promise commands the gate never touches.

    ``_maybe_confirm_destructive_slash`` gates /new (+ its /reset alias) and
    /undo; /update runs through it with ``allow_always=False`` and is never
    waived. /clear is ``cli_only`` and does not exist here at all, yet both
    locales named it.
    """
    import yaml

    for locale in ("en", "ru"):
        with open(f"locales/{locale}.yaml", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        confirm = data["trix"]["cmd"]["confirm"]
        for key in ("always_saved", "always_not_saved"):
            assert "/clear" not in confirm[key], f"{locale}.{key} still names /clear"
            assert "/new" in confirm[key]
            assert "/undo" in confirm[key]
