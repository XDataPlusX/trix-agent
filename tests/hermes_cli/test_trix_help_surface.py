"""Tasks 2 & 3 (``docs/product/plans/2026-09-01-client-command-surface.md``):
``/help`` and ``/commands`` stop reading the raw, uncurated registry and
start reading Trix's client command surface
(``hermes_cli/trix_menu.py::client_surface_commands()``) instead.

Both surfaces are exercised through ``hermes_cli.slash_exec._exec_help`` /
``_exec_commands`` directly -- every real surface (CLI REPL, gateway,
TUI slash worker) reaches the exact same functions via
``EXECUTORS["gateway_help"]`` / ``["gateway_commands"]``
(``hermes_cli/slash_exec.py``), so testing them here covers every surface
without duplicating gateway/CLI decoration plumbing.

Design spec:
``docs/product/specs/2026-09-01-trix-agent-client-command-surface-design.md``
Ruling 2 (/help) and Ruling 3 (/commands).

``hermes_cli.commands.gateway_help_lines()`` itself stays UNCURATED by
default -- five other test files call it with no argument and expect the
full registry (``tests/hermes_cli/test_approvals_command.py`` in
particular relies on ``/approvals`` being present even though Trix
disables it on the client surface). Curation is opt-in via the ``only``
parameter, and ``_exec_help`` / ``_exec_commands`` are the only callers
that opt in.
"""

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.slash_exec import CommandContext, _exec_commands, _exec_help
from hermes_cli.trix_menu import client_surface_commands


@pytest.fixture(autouse=True)
def _reset_skill_commands_cache():
    """``agent.skill_commands._skill_commands`` is a plain module-level
    cache -- ``get_skill_commands()`` only rescans when it's empty, so a
    test that seeds skills and calls ``scan_skill_commands()`` would
    otherwise leak that scan result into every later test in this file
    (subprocess-per-file isolation only resets state BETWEEN files, not
    between tests in the same file). Rescan against whatever SKILLS_DIR is
    live once the test's own patches have unwound -- each test's isolated
    ``$HERMES_HOME/skills`` is empty, so this naturally clears the cache.
    """
    yield
    from agent.skill_commands import scan_skill_commands

    scan_skill_commands()


# A line that OPENS with a slash command, e.g. "`/new [name]` -- ..." or
# "`/hermes-agent-skill-authoring` -- ...". Anchored to the start of the
# line so an alias mention mid-line ("... (псевдоним: `/reset`)") or a
# skill name mentioned in prose is never mistaken for a second command
# entry -- only genuine `/help` / `/commands` entry lines start this way.
_COMMAND_LINE_RE = re.compile(r"^`/([^\s`]+)")


def _command_names_in(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        m = _COMMAND_LINE_RE.match(line)
        if m:
            names.add(m.group(1))
    return names


def _all_command_names_across_pages(page_size: int = 5) -> set[str]:
    """Drive ``/commands`` across EVERY page it renders, collecting every
    command-shaped line's name.

    Stops once bumping the page number no longer changes the rendered
    body: ``_exec_commands`` clamps a too-high page to the last valid one
    (see its ``page != requested_page`` handling), so two identical
    consecutive renders means the previous page was the last real one.
    This walks pagination via that documented clamping behavior rather
    than parsing a localized page-count number out of the header text.
    """
    names: set[str] = set()
    prev_text = None
    page = 1
    while page <= 200:  # generous ceiling -- a runaway loop is a bug to see, not hang on
        reply = _exec_commands(
            CommandContext(surface="gateway", args=str(page), options={"page_size": page_size})
        )
        if reply.text == prev_text:
            break
        names |= _command_names_in(reply.text)
        prev_text = reply.text
        page += 1
    return names


def _write_curated_config(home_dir, curated: bool) -> None:
    Path(home_dir, "config.yaml").write_text(
        "platforms:\n"
        "  telegram:\n"
        "    extra:\n"
        "      command_menu:\n"
        f"        curated: {str(curated).lower()}\n"
    )


# ---------------------------------------------------------------------------
# Task 2 -- /help
# ---------------------------------------------------------------------------

class TestHelpBodyIsExactlyTheClientSurface:
    """Step 1: whitelist, not blacklist. "No service/disabled commands"
    would NOT catch the actual leak -- /hermes-agent-skill-authoring is
    neither a service command nor a disabled command, it's a skill. The
    only assertion that catches it: every command-shaped line in /help's
    body names a client-surface command, and there are no others.
    """

    def test_every_command_line_belongs_to_the_client_surface(self):
        reply = _exec_help(CommandContext(surface="gateway"))
        found = _command_names_in(reply.text)
        assert found, "expected at least one command line in /help"
        unexpected = found - set(client_surface_commands())
        assert not unexpected, (
            f"/help names commands outside the client surface: {sorted(unexpected)}"
        )

    def test_installed_skills_never_change_the_curated_help_body(self, tmp_path, monkeypatch):
        """Step 4: the curated body must not sample skill commands or
        promise more of them behind /commands -- Task 3 makes /commands
        stop listing skills entirely under curation, so the old "...and N
        more, see /commands" pointer would dangle on a page that no
        longer has them. Proven by seeding more skills than the old
        "first 10" cutoff and asserting the curated body is byte-identical
        with and without them -- the pre-Task-2 code would have grown a
        skill_header block plus a /commands pointer the moment more than
        zero skills existed, so this is mutation-provable, not merely a
        substring check against today's wording.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # No config.yaml at all -- curation is the default posture.
        before = _exec_help(CommandContext(surface="gateway")).text

        real_skills_dir = Path(os.path.realpath(str(tmp_path / "skills")))
        for i in range(12):
            skill_dir = real_skills_dir / f"seeded-help-skill-{i}"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: seeded-help-skill-{i}\n"
                f"description: Seeded test skill number {i}.\n---\n\nBody.\n"
            )

        with patch("tools.skills_tool.SKILLS_DIR", real_skills_dir):
            from agent.skill_commands import scan_skill_commands

            scanned = scan_skill_commands()
            assert len(scanned) == 12, "seeding assumption broken -- scan found a different count"
            after = _exec_help(CommandContext(surface="gateway")).text

        assert after == before, (
            "installing skills must not change the curated /help body at all"
        )


class TestHelpBodyFitsOneTelegramMessage:
    """Step 2 + 3: the body must fit Telegram's real single-message limit,
    measured the way the adapter actually measures it (UTF-16 code units,
    not Python len()), and measured in the language the client actually
    sees (Russian) rather than the English the rest of the suite is
    pinned to by default."""

    def test_curated_help_body_fits_in_one_message_in_russian(self, monkeypatch):
        from agent import i18n
        from gateway.platforms.base import utf16_len
        from plugins.platforms.telegram.adapter import TelegramAdapter

        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        try:
            reply = _exec_help(CommandContext(surface="gateway"))
            length = utf16_len(reply.text)
            assert length <= TelegramAdapter.MAX_MESSAGE_LENGTH, (
                f"/help body is {length} UTF-16 units in Russian -- the "
                f"language the client actually sees -- over Telegram's "
                f"{TelegramAdapter.MAX_MESSAGE_LENGTH}-unit single-message limit"
            )
        finally:
            i18n.reset_language_cache()


# ---------------------------------------------------------------------------
# Task 3 -- /commands
# ---------------------------------------------------------------------------

class TestCommandsBodyIsExactlyTheClientSurfaceAcrossAllPages:
    """Step 1 + 2: the invariant is a set equality over ALL pages, not a
    hardcoded brand blocklist (`hermes|claude|codex|apple` would let a
    new skill like /gemini-cli straight through) -- and it is checked by
    actually walking every page, because the first page alone already has
    enough built-in commands to look clean while a later page leaks."""

    def test_every_page_only_names_client_surface_commands(self):
        # Small page_size forces several pages regardless of how many
        # commands CLIENT_MENU_COMMANDS holds on any given day.
        names = _all_command_names_across_pages(page_size=5)
        assert names == set(client_surface_commands())


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_AUTHORING_MD = (
    _REPO_ROOT
    / "skills"
    / "software-development"
    / "hermes-agent-skill-authoring"
    / "SKILL.md"
)


class TestRealSkillsDoNotLeakIntoCommands:
    """Step 3: seeding a made-up SKILL.md would only prove the filter
    works in the abstract. The mutation this guards against is concrete
    -- /commands used to append every scanned skill command -- and the
    spec ties the regression check to a skill that actually ships in this
    repo: /hermes-agent-skill-authoring
    (skills/software-development/hermes-agent-skill-authoring/SKILL.md).
    /hermes-agent and /hermes-desktop-plugins, the other two names the
    spec measured, only exist under a developer's personal
    ~/.hermes/skills/ and are absent from a clean checkout -- a test tied
    to either of those would stay green even with the leak wide open.
    """

    def test_hermes_agent_skill_authoring_does_not_appear_anywhere(self, tmp_path, monkeypatch):
        assert _SKILL_AUTHORING_MD.is_file(), (
            "fixture assumption broken: skills/software-development/"
            "hermes-agent-skill-authoring/SKILL.md no longer ships in this repo -- "
            "repoint this test at another bundled skill"
        )

        # Wrap in os.path.realpath: on macOS, tempfile creates paths under
        # /var/..., a symlink to /private/var/...; _collect_gateway_skill_entries
        # (and the skill scanner) compare against SKILLS_DIR.resolve(), so an
        # unresolved tmp_path here would silently fail the prefix match and
        # the skill would vanish for a reason unrelated to curation --
        # making this test pass for the wrong reason.
        real_skills_dir = Path(os.path.realpath(str(tmp_path / "skills")))
        skill_dir = real_skills_dir / "hermes-agent-skill-authoring"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            _SKILL_AUTHORING_MD.read_text(encoding="utf-8"), encoding="utf-8"
        )

        with patch("tools.skills_tool.SKILLS_DIR", real_skills_dir):
            from agent.skill_commands import scan_skill_commands

            scan_skill_commands()

            commands_names = _all_command_names_across_pages(page_size=5)
            help_names = _command_names_in(_exec_help(CommandContext(surface="gateway")).text)

        assert "hermes-agent-skill-authoring" not in commands_names
        assert "hermes-agent-skill-authoring" not in help_names
        assert commands_names == set(client_surface_commands())


# ---------------------------------------------------------------------------
# Global constraint: curation stays opt-out-able via
# platforms.telegram.extra.command_menu.curated: false -- our own
# debugging escape hatch on a client machine. Any task adding a filter
# must respect this key and prove it here.
# ---------------------------------------------------------------------------

class TestCurationEscapeHatchStillWorks:
    def test_curated_false_restores_the_full_help_body(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_curated_config(tmp_path, curated=False)

        from hermes_cli.commands import gateway_help_lines

        expected = _command_names_in("\n".join(gateway_help_lines()))
        found = _command_names_in(_exec_help(CommandContext(surface="gateway")).text)
        assert found == expected
        # Proves the escape hatch genuinely reaches beyond the curated
        # list -- if curated: false were silently ignored, `found` would
        # equal the curated set and this difference would be empty.
        assert found - set(client_surface_commands())

    def test_curated_false_restores_full_commands_across_pages(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_curated_config(tmp_path, curated=False)

        from hermes_cli.commands import gateway_help_lines

        expected = _command_names_in("\n".join(gateway_help_lines()))
        got = _all_command_names_across_pages(page_size=5)
        assert got == expected
        assert got - set(client_surface_commands())
