"""Tests for hermes_cli.trix_disabled_reply -- the client-facing text a
disabled command answers with (Task 5 of the client-command-surface plan,
``docs/product/plans/2026-09-01-client-command-surface.md``; Ruling 5 and
Ruling 6 of ``docs/product/specs/2026-09-01-trix-agent-client-command-
surface-design.md``).

``tests/conftest.py`` pins ``HERMES_LANGUAGE=en`` for the whole suite (the
fork defaults to Russian; without the pin, every upstream test asserting
English copy would break). Every assertion below that a reply *is Russian*
therefore sets ``HERMES_LANGUAGE=ru`` itself and resets ``agent.i18n``'s
catalog cache in a ``try``/``finally`` -- the same pattern
``tests/hermes_cli/test_trix_menu.py`` uses in
``TestDebugDescriptionMentionsLogs``. Without this, the Russian-text
assertions here would either always fail (client language never renders) or
never actually exercise the client's language at all.
"""

from __future__ import annotations

import re

import pytest

from agent import i18n
from hermes_cli.trix_disabled_reply import disabled_command_reply
from hermes_cli.trix_menu import CLIENT_MENU_COMMANDS, DISABLED_COMMANDS, SERVICE_COMMANDS

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{4,}")

# Every canonical command name this module might legitimately mention: a
# "replace" hint always names a CLIENT_MENU_COMMANDS entry, and the
# forbidden-word check below must not flag that name itself. Split on "-" so
# a hyphenated registry name (e.g. "reload-skills") is checked token by
# token, the same way a human reading "reload" or "skills" in isolation
# would judge it.
_KNOWN_COMMAND_TOKENS = {
    token.lower()
    for name in (*CLIENT_MENU_COMMANDS, *DISABLED_COMMANDS, *SERVICE_COMMANDS)
    for token in name.split("-")
}


@pytest.fixture(autouse=True)
def _russian_client(monkeypatch):
    """Every test in this module renders the text the client actually sees."""
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        yield
    finally:
        i18n.reset_language_cache()


class TestEveryDisabledCommandGetsAReply:
    """Ruling 5: a disabled command answers with an explanation, not
    silence and not the generic "unknown command" reply."""

    @pytest.mark.parametrize("name", sorted(DISABLED_COMMANDS))
    def test_reply_is_nonempty_and_russian(self, name):
        reply = disabled_command_reply(name)
        assert reply is not None
        assert reply.strip(), f"/{name} produced a blank reply"
        assert _CYRILLIC_RE.search(reply), f"/{name} reply has no Cyrillic text: {reply!r}"

    @pytest.mark.parametrize("name", sorted(DISABLED_COMMANDS))
    def test_reply_names_no_forbidden_surface(self, name):
        """Ruling 6 + the plan's text requirements: nothing sends the
        client to config.yaml, a home-dir path, or a terminal command --
        the "hermes <subcommand>" shape catches that class directly."""
        reply = disabled_command_reply(name)
        lowered = reply.lower()
        assert "config.yaml" not in lowered, reply
        assert "~/.hermes" not in lowered, reply
        assert not re.search(r"\bhermes\s+\w+", lowered), reply
        offenders = [
            w for w in _LATIN_WORD_RE.findall(reply)
            if w.lower() not in _KNOWN_COMMAND_TOKENS
        ]
        assert not offenders, (
            f"/{name} reply has unexplained Latin word(s) {offenders}: {reply!r}"
        )

    @pytest.mark.parametrize(
        "name",
        sorted(n for n, e in DISABLED_COMMANDS.items() if e.hint == "replace"),
    )
    def test_replace_hint_names_its_replacement(self, name):
        entry = DISABLED_COMMANDS[name]
        reply = disabled_command_reply(name)
        assert f"/{entry.replacement}" in reply, (
            f"/{name} reply doesn't name its replacement /{entry.replacement}: {reply!r}"
        )


class TestNonDisabledCommandsReturnNone:
    @pytest.mark.parametrize("name", ["status", "new"])
    def test_returns_none(self, name):
        assert name in CLIENT_MENU_COMMANDS, (
            f"test fixture assumption broken: /{name} is no longer in the client menu"
        )
        assert disabled_command_reply(name) is None


class TestAliasResolution:
    def test_alias_gets_the_same_reply_as_the_canonical_name(self):
        assert "context" in DISABLED_COMMANDS
        assert disabled_command_reply("ctx") == disabled_command_reply("context")
