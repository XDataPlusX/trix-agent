"""Gateway command help rendering tests."""

from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text: str, platform: Platform) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            chat_id="chat-1",
            user_id="user-1",
            user_name="tester",
            chat_type="dm",
        ),
    )


def _make_runner():
    from gateway.run import GatewayRunner

    return object.__new__(GatewayRunner)


def _disable_client_surface_curation(tmp_path, monkeypatch) -> None:
    """Restore Trix's uncurated escape hatch (``curated: false``) so a skill
    command actually reaches ``/help`` / ``/commands`` output.

    Since ``docs/product/plans/2026-09-01-client-command-surface.md`` Tasks
    2 & 3, both bodies are curated to the client command surface by
    default and skill commands never appear there at all (spec Ruling 3).
    These two tests exist to cover ``_telegramize_command_mentions``
    sanitizing a skill command's slash mention, which needs a skill
    command in the rendered body to sanitize -- the uncurated escape
    hatch is still a real, supported path that shows skill commands, so
    it exercises the real integration instead of degrading to a unit
    test of the sanitizer against a hand-built string.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    Path(tmp_path, "config.yaml").write_text(
        "platforms:\n"
        "  telegram:\n"
        "    extra:\n"
        "      command_menu:\n"
        "        curated: false\n"
    )


@pytest.mark.asyncio
async def test_help_sanitizes_slash_command_mentions_for_telegram(tmp_path, monkeypatch):
    """Telegram help output must not expose invalid uppercase/hyphenated slashes."""
    _disable_client_surface_curation(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {
            "/Linear": {"description": "Open Linear"},
            "/Custom-Thing": {"description": "Run a custom thing"},
        },
    )

    result = await _make_runner()._handle_help_command(
        _make_event("/help", Platform.TELEGRAM)
    )

    assert "`/linear`" in result
    assert "`/custom_thing`" in result
    assert "`/Linear`" not in result
    assert "`/Custom-Thing`" not in result


@pytest.mark.asyncio
async def test_commands_sanitizes_slash_command_mentions_for_telegram(tmp_path, monkeypatch):
    """Paginated Telegram /commands output uses Telegram-valid slash mentions."""
    _disable_client_surface_curation(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {"/Linear": {"description": "Open Linear"}},
    )

    result = await _make_runner()._handle_commands_command(
        _make_event("/commands 999", Platform.TELEGRAM)
    )

    assert "`/linear`" in result
    assert "`/Linear`" not in result


