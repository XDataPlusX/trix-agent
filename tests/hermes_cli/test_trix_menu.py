"""Tests for Trix's three-layer client command surface (menu curation +
service commands + disabled commands).

``hermes_cli/trix_menu.py`` is the single source of truth for which built-in
gateway command belongs to which of three layers: ``CLIENT_MENU_COMMANDS``
(shown + dispatchable), ``SERVICE_COMMANDS`` (dispatchable, not shown), and
``DISABLED_COMMANDS`` (not dispatchable, answers with an explanation). Task
1 (``docs/product/plans/2026-09-01-client-command-surface.md``) only wires
the data structures and the Telegram menu; wiring ``/help``, ``/commands``,
and the gateway dispatcher's actual refusal to run a disabled command are
later tasks in the same plan. See
``docs/product/specs/2026-09-01-trix-agent-client-command-surface-design.md``
§5.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from hermes_cli.commands import (
    COMMAND_REGISTRY,
    GATEWAY_KNOWN_COMMANDS,
    _sanitize_telegram_name,
    command_description,
    resolve_command,
    telegram_bot_commands,
    telegram_menu_commands,
    telegram_menu_max_commands,
)
from hermes_cli.trix_menu import (
    CLIENT_MENU_COMMANDS,
    DISABLED_COMMANDS,
    SERVICE_COMMANDS,
    VALID_DISABLED_HINTS,
    DisabledCommand,
    client_surface_commands,
    disabled_entry,
    filter_client_menu,
    is_disabled_in_gateway,
    menu_curation_enabled,
)


# ---------------------------------------------------------------------------
# Static relationships between CLIENT_MENU_COMMANDS / SERVICE_COMMANDS /
# DISABLED_COMMANDS and the shared registry.
# ---------------------------------------------------------------------------

class TestClientMenuCommandsResolve:
    def test_every_name_resolves_in_the_registry(self):
        """Catches a typo or an upstream rename of one of the 33 names."""
        unresolved = [name for name in CLIENT_MENU_COMMANDS if resolve_command(name) is None]
        assert not unresolved, f"names not in COMMAND_REGISTRY: {unresolved}"

    def test_no_duplicates(self):
        assert len(CLIENT_MENU_COMMANDS) == len(set(CLIENT_MENU_COMMANDS))

    def test_none_are_cli_only_without_a_gateway_gate(self):
        """A menu entry the gateway can't dispatch would dead-end the client."""
        offenders = []
        for name in CLIENT_MENU_COMMANDS:
            cmd = resolve_command(name)
            assert cmd is not None
            if cmd.cli_only and not cmd.gateway_config_gate:
                offenders.append(name)
        assert not offenders, f"cli_only without a gateway gate: {offenders}"

    def test_all_present_in_gateway_known_commands(self):
        missing = [name for name in CLIENT_MENU_COMMANDS if name not in GATEWAY_KNOWN_COMMANDS]
        assert not missing, f"missing from GATEWAY_KNOWN_COMMANDS: {missing}"


class TestServiceCommandsResolve:
    def test_every_service_name_resolves_in_the_registry(self):
        unresolved = [name for name in SERVICE_COMMANDS if resolve_command(name) is None]
        assert not unresolved, f"SERVICE_COMMANDS names not in COMMAND_REGISTRY: {unresolved}"

    def test_no_overlap_with_client_menu(self):
        overlap = set(SERVICE_COMMANDS) & set(CLIENT_MENU_COMMANDS)
        assert not overlap, f"a command cannot be both shown and service-only: {overlap}"

    def test_no_overlap_with_disabled_commands(self):
        overlap = set(SERVICE_COMMANDS) & set(DISABLED_COMMANDS)
        assert not overlap, f"a command cannot be both service and disabled: {overlap}"

    def test_every_reason_is_non_empty_russian_text(self):
        for name, reason in SERVICE_COMMANDS.items():
            assert isinstance(reason, str) and reason.strip(), name

    def test_exactly_start_and_sethome(self):
        """Ruling 1a fixes the service layer at exactly two names: /start
        (the platform's own ping) and /sethome (the home-chat escape
        hatch). Any other name showing up here is a product decision that
        needs its own review, not something that should slip in silently
        alongside these two."""
        assert set(SERVICE_COMMANDS) == {"start", "sethome"}

    def test_service_commands_remain_dispatchable(self):
        """A service command is deliberately absent from the menu, but it
        must still execute -- that's the entire point of the layer: /start
        delivers every unsolicited message (disk warnings, monthly
        summaries), /sethome is the only fix for a misconfigured home chat.
        Checked against GATEWAY_KNOWN_COMMANDS, the same source the
        dispatcher itself reads."""
        for name in SERVICE_COMMANDS:
            assert name in GATEWAY_KNOWN_COMMANDS, f"{name} dropped from gateway dispatch"


class TestDisabledCommandsResolve:
    def test_every_disabled_name_resolves_in_the_registry(self):
        unresolved = [name for name in DISABLED_COMMANDS if resolve_command(name) is None]
        assert not unresolved, f"DISABLED_COMMANDS names not in COMMAND_REGISTRY: {unresolved}"

    def test_no_overlap_with_client_menu(self):
        overlap = set(DISABLED_COMMANDS) & set(CLIENT_MENU_COMMANDS)
        assert not overlap, f"a command cannot be both shown and disabled: {overlap}"

    def test_no_overlap_with_service_commands(self):
        overlap = set(DISABLED_COMMANDS) & set(SERVICE_COMMANDS)
        assert not overlap, f"a command cannot be both service and disabled: {overlap}"

    def test_every_entry_is_a_disabled_command_record(self):
        offenders = [name for name, entry in DISABLED_COMMANDS.items() if not isinstance(entry, DisabledCommand)]
        assert not offenders, f"DISABLED_COMMANDS values must be DisabledCommand records: {offenders}"

    def test_every_reason_is_non_empty_russian_text(self):
        for name, entry in DISABLED_COMMANDS.items():
            assert isinstance(entry.reason, str) and entry.reason.strip(), name

    def test_every_hint_is_a_known_value(self):
        offenders = {
            name: entry.hint for name, entry in DISABLED_COMMANDS.items()
            if entry.hint not in VALID_DISABLED_HINTS
        }
        assert not offenders, f"unknown DisabledCommand.hint value: {offenders}"

    def test_replace_hint_names_a_client_layer_replacement(self):
        """hint="replace" is a promise: the reply names a command the
        client can actually run instead. A replacement that doesn't
        resolve, or that isn't itself in the client's menu, breaks that
        promise silently."""
        offenders = [
            (name, entry.replacement)
            for name, entry in DISABLED_COMMANDS.items()
            if entry.hint == "replace"
            and (entry.replacement is None or entry.replacement not in CLIENT_MENU_COMMANDS)
        ]
        assert not offenders, (
            f"'replace' hint must name a command present in CLIENT_MENU_COMMANDS: {offenders}"
        )

    def test_non_replace_hints_carry_no_replacement(self):
        """"words" and "none" both mean "there is nothing to point the
        client at" -- a stray non-None replacement on one of them is
        either a copy-paste leftover or a hint that was never updated to
        "replace"."""
        offenders = [
            (name, entry.hint, entry.replacement)
            for name, entry in DISABLED_COMMANDS.items()
            if entry.hint != "replace" and entry.replacement is not None
        ]
        assert not offenders, f"'words'/'none' hints must not name a replacement: {offenders}"

    def test_exactly_twenty_nine_entries(self):
        """Pinned to the spec's own machine-checked total (33 + 2 + 29 =
        64, all 64 gateway commands covered, zero overlap -- spec §5). This
        is the one place in this file where a literal count is appropriate:
        it isn't guessing at implementation shape, it's citing a number the
        spec itself computed and will need a deliberate edit (and a spec
        update) to change."""
        assert len(DISABLED_COMMANDS) == 29


# ---------------------------------------------------------------------------
# is_disabled_in_gateway() / disabled_entry() -- alias resolution.
# ---------------------------------------------------------------------------

class TestIsDisabledInGatewayAliasResolution:
    def test_context_alias_ctx_is_disabled(self):
        assert is_disabled_in_gateway("ctx") is True
        assert is_disabled_in_gateway("context") is True

    def test_new_alias_reset_is_not_disabled(self):
        """/new (client menu) has the registered alias /reset -- typing the
        alias must not be a backdoor around anything, but it also must not
        be mistaken for a disabled command."""
        assert is_disabled_in_gateway("reset") is False
        assert is_disabled_in_gateway("new") is False

    def test_unknown_name_is_not_disabled(self):
        assert is_disabled_in_gateway("not-a-real-command") is False

    def test_none_and_empty_are_not_disabled(self):
        assert is_disabled_in_gateway(None) is False
        assert is_disabled_in_gateway("") is False

    def test_leading_slash_is_tolerated(self):
        assert is_disabled_in_gateway("/ctx") is True


class TestDisabledEntry:
    def test_returns_the_record_for_a_disabled_alias(self):
        entry = disabled_entry("ctx")
        assert entry is not None
        assert entry is DISABLED_COMMANDS["context"]

    def test_returns_none_for_a_client_command(self):
        assert disabled_entry("new") is None

    def test_returns_none_for_an_unknown_name(self):
        assert disabled_entry("not-a-real-command") is None

    def test_returns_none_for_none(self):
        assert disabled_entry(None) is None


class TestGatedDisabledCommandsIgnoreTheirConfigGate:
    """/skills and /verbose used to be the one gated special case inside
    the old single hidden-list: reachable only once their own config gate
    (``skills.write_approval`` / ``display.tool_progress_command``) was
    open. Both are now DISABLED_COMMANDS -- opening the gate must not
    resurrect them, because disablement is a product decision independent
    of that config knob.

    Scope note: this only asserts what this module promises today --
    ``is_disabled_in_gateway()`` never consults config at all, so the
    boolean is identical whether the gate is open or closed. Actually
    refusing to dispatch these at the gateway level (so a client typing
    them mid-run truly gets nothing) is Task 4's job.
    """

    def test_skills_and_verbose_are_config_gated_commands(self):
        # Sanity check on the premise: if a future registry edit removes
        # their gate, this whole scenario stops applying and the test
        # below would be vacuous.
        assert resolve_command("skills").gateway_config_gate
        assert resolve_command("verbose").gateway_config_gate

    def test_stay_disabled_with_the_gate_forced_open(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            "skills:\n  write_approval: true\n"
            "display:\n  tool_progress_command: true\n"
        )
        assert is_disabled_in_gateway("skills") is True
        assert is_disabled_in_gateway("verbose") is True


# ---------------------------------------------------------------------------
# Completeness invariant: every command the gateway could ever dispatch is
# accounted for in exactly one of the three layers.
# ---------------------------------------------------------------------------

def _canonical_gateway_known_names() -> set:
    """Canonical (non-alias) name for every command ``GATEWAY_KNOWN_COMMANDS``
    considers gateway-known: ``not cli_only OR has a gateway_config_gate``,
    independent of whether that gate is *currently* open in config.yaml.

    Deliberately built from ``GATEWAY_KNOWN_COMMANDS`` + ``resolve_command()``,
    not ``_is_gateway_available()``/``_resolve_config_gates()`` (that reads
    *current* visibility, which silently varies with the ambient
    config.yaml a test happens to run under -- exactly the gap that let
    ``/skills`` and ``/verbose`` go unlisted from the Telegram menu
    whitelist without a single test turning red: both are ``cli_only``
    with a ``gateway_config_gate`` that is closed by default, so "current
    visibility" excludes them while "gateway-known" correctly includes
    them).

    ``GATEWAY_KNOWN_COMMANDS`` flattens each command's aliases into the
    same name namespace as its canonical name (e.g. both ``codex-runtime``
    and its registered alias ``codex_runtime``); ``resolve_command()``
    collapses each back to its one canonical ``CommandDef.name`` so an
    alias is never mistaken for a separate command needing its own
    whitelist entry. ``_sanitize_telegram_name`` then normalizes the
    remaining hyphen/underscore spelling variance (``codex-runtime`` /
    ``codex_runtime``) the same way the whitelist below is normalized.
    """
    names = set()
    for raw_name in GATEWAY_KNOWN_COMMANDS:
        cmd = resolve_command(raw_name)
        if cmd is not None:
            names.add(_sanitize_telegram_name(cmd.name))
    return names


def _sanitized_layer(names) -> set:
    return {_sanitize_telegram_name(name) for name in names}


class TestThreeLayerCompleteness:
    def test_every_gateway_known_command_is_in_exactly_one_layer(self):
        """Coverage + disjointness contract between the command registry
        and Trix's three-layer command surface, mirroring
        tests/agent/test_i18n.py::test_every_gateway_command_has_a_russian_description
        (same registry, same "not cli_only or has a gate" canon, a
        different target catalog). A new upstream gateway command -- or an
        existing config-gated one like /skills or /verbose -- landing in
        none of the three layers, or in more than one, is the intended
        signal: assign it to exactly one of CLIENT_MENU_COMMANDS,
        SERVICE_COMMANDS, or DISABLED_COMMANDS.
        """
        known = _canonical_gateway_known_names()
        client = _sanitized_layer(CLIENT_MENU_COMMANDS)
        service = _sanitized_layer(SERVICE_COMMANDS)
        disabled = _sanitized_layer(DISABLED_COMMANDS)

        missing = known - (client | service | disabled)
        assert not missing, (
            "gateway-known commands missing from every layer (add to "
            f"CLIENT_MENU_COMMANDS, SERVICE_COMMANDS, or DISABLED_COMMANDS): {sorted(missing)}"
        )

        overlaps = {
            "client & service": sorted(client & service),
            "client & disabled": sorted(client & disabled),
            "service & disabled": sorted(service & disabled),
        }
        offending = {pair: names for pair, names in overlaps.items() if names}
        assert not offending, f"a command must belong to exactly one layer: {offending}"


# ---------------------------------------------------------------------------
# menu_curation_enabled()
# ---------------------------------------------------------------------------

class TestMenuCurationEnabled:
    def test_default_true_on_missing_config(self):
        assert menu_curation_enabled(None) is True
        assert menu_curation_enabled({}) is True

    def test_default_true_with_unrelated_config(self):
        assert menu_curation_enabled({"agent": {"disabled_toolsets": ["bfl"]}}) is True

    def test_explicit_false_disables_curation(self):
        cfg = {"platforms": {"telegram": {"extra": {"command_menu": {"curated": False}}}}}
        assert menu_curation_enabled(cfg) is False

    def test_explicit_true_stays_enabled(self):
        cfg = {"platforms": {"telegram": {"extra": {"command_menu": {"curated": True}}}}}
        assert menu_curation_enabled(cfg) is True

    def test_tolerates_malformed_intermediate_nodes(self):
        # "telegram" is a string, not a mapping -- must degrade to the default
        # rather than raise.
        assert menu_curation_enabled({"platforms": {"telegram": "oops"}}) is True


# ---------------------------------------------------------------------------
# client_surface_commands()
# ---------------------------------------------------------------------------

class TestClientSurfaceCommands:
    def test_matches_client_menu_commands_in_order(self):
        assert client_surface_commands() == list(CLIENT_MENU_COMMANDS)

    def test_returns_a_list_not_the_shared_tuple(self):
        result = client_surface_commands()
        assert isinstance(result, list)
        result.append("mutated")
        assert "mutated" not in CLIENT_MENU_COMMANDS


# ---------------------------------------------------------------------------
# filter_client_menu()
# ---------------------------------------------------------------------------

class TestFilterClientMenu:
    def test_filters_and_reorders(self):
        commands = [("stop", "Stop"), ("help", "Help"), ("kanban", "Kanban board"), ("new", "New")]
        result = filter_client_menu(commands)
        assert [n for n, _ in result] == ["help", "new", "stop"]

    def test_missing_names_are_skipped_not_invented(self):
        result = filter_client_menu([("help", "Help")])
        assert result == [("help", "Help")]

    def test_full_registry_input_yields_exactly_client_menu_commands(self):
        result = filter_client_menu(list(telegram_bot_commands()))
        assert [n for n, _ in result] == list(CLIENT_MENU_COMMANDS)


# ---------------------------------------------------------------------------
# Integration: telegram_menu_commands()
# ---------------------------------------------------------------------------

def _write_curated_config(home_dir, curated: bool):
    Path(home_dir, "config.yaml").write_text(
        "platforms:\n"
        "  telegram:\n"
        "    extra:\n"
        "      command_menu:\n"
        f"        curated: {str(curated).lower()}\n"
    )


class TestTelegramMenuCommandsIntegration:
    def test_curated_menu_matches_client_menu_commands_in_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_curated_config(tmp_path, curated=True)
        with patch(
            "hermes_cli.commands._collect_gateway_skill_entries",
            return_value=([], 0),
        ):
            menu, _hidden = telegram_menu_commands(max_commands=telegram_menu_max_commands())
        assert [n for n, _ in menu] == list(CLIENT_MENU_COMMANDS)

    def test_default_config_is_curated(self, tmp_path, monkeypatch):
        """No command_menu block at all -- curation is the default posture."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text("agent:\n  max_iterations: 500\n")
        with patch(
            "hermes_cli.commands._collect_gateway_skill_entries",
            return_value=([], 0),
        ):
            menu, _hidden = telegram_menu_commands(max_commands=telegram_menu_max_commands())
        assert [n for n, _ in menu] == list(CLIENT_MENU_COMMANDS)

    def test_curated_false_restores_the_full_menu(self, tmp_path, monkeypatch):
        """curated=False + the real default cap -> every registry command
        is present. Deliberately uses ``telegram_menu_max_commands()`` (the
        ambient default), not an artificially inflated cap: that default is
        now a tracked invariant
        (``_DEFAULT_TELEGRAM_MENU_MAX_COMMANDS`` in ``hermes_cli/commands.py``,
        ``TestTelegramBotCommands::test_default_cap_covers_every_gateway_command``
        in ``tests/hermes_cli/test_commands.py``) rather than a coincidence,
        so this test doubles as an end-to-end check of that invariant: if
        the default cap ever drifts back below the registry size, THIS
        test (not just the unit-level one) catches it via a real missing
        name, not a silently smaller menu.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_curated_config(tmp_path, curated=False)
        full_names = {n for n, _ in telegram_bot_commands()}
        with patch(
            "hermes_cli.commands._collect_gateway_skill_entries",
            return_value=([], 0),
        ):
            menu, _hidden = telegram_menu_commands(max_commands=telegram_menu_max_commands())
        got_names = {n for n, _ in menu}
        assert got_names == full_names

    def test_curation_frees_slots_only_with_an_explicit_wide_override(self, tmp_path, monkeypatch):
        """Relation between the cap, the curated whitelist length, and the
        full registry length -- not a hardcoded slot count. Spies on the
        internal skill collector to observe the ``max_slots`` Trix's wiring
        actually hands it.

        Task 1b inverted the old assumption this test used to encode: the
        DEFAULT curated cap (no explicit ``max_commands``) now derives from
        ``len(CLIENT_MENU_COMMANDS)`` and therefore leaves ZERO room for
        skills -- that is the fix, not an incidental side effect. Room for
        skills only reappears when an operator explicitly widens
        ``max_commands`` past the curated list's length -- the respected
        debug override (spec §4). Every assertion below is a formula over
        ``len(CLIENT_MENU_COMMANDS)`` / ``len(telegram_bot_commands())``,
        not a literal, so removing a command from either list keeps this
        test meaningful instead of silently expecting a stale number.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        full_len = len(list(telegram_bot_commands()))
        wide_cap = full_len + 5  # comfortably past both list lengths

        captured = {}

        def _spy(**kwargs):
            captured["max_slots"] = kwargs["max_slots"]
            return [], 0

        # Default curated cap, no override: zero slots free for skills.
        _write_curated_config(tmp_path, curated=True)
        default_cap = telegram_menu_max_commands()
        with patch("hermes_cli.commands._collect_gateway_skill_entries", side_effect=_spy):
            telegram_menu_commands(max_commands=default_cap)
        assert captured["max_slots"] == 0, "default curated cap must leave zero room for skills"

        # Explicit override wider than the curated list: room reappears.
        Path(tmp_path, "config.yaml").write_text(
            "platforms:\n"
            "  telegram:\n"
            "    extra:\n"
            "      command_menu:\n"
            "        curated: true\n"
            f"        max_commands: {wide_cap}\n"
        )
        override_cap = telegram_menu_max_commands()
        assert override_cap == wide_cap
        with patch("hermes_cli.commands._collect_gateway_skill_entries", side_effect=_spy):
            telegram_menu_commands(max_commands=override_cap)
        assert captured["max_slots"] == max(0, wide_cap - len(CLIENT_MENU_COMMANDS))
        assert captured["max_slots"] > 0, "an explicit wide override must free room for skills"

        # Uncurated: room is relative to the full registry, not the curated list.
        _write_curated_config(tmp_path, curated=False)
        uncurated_cap = telegram_menu_max_commands()
        with patch("hermes_cli.commands._collect_gateway_skill_entries", side_effect=_spy):
            telegram_menu_commands(max_commands=uncurated_cap)
        assert captured["max_slots"] == max(0, uncurated_cap - full_len)

    def test_default_curated_cap_equals_the_client_menu_length_exactly(self, tmp_path, monkeypatch):
        """The corrected invariant (Task 1b): with curation on and no
        explicit ``max_commands``, the cap is exactly
        ``len(CLIENT_MENU_COMMANDS)`` -- not merely large enough to cover
        it. Any slack above that exact length is precisely the leak the
        client-command-surface spec closes (alphabetical skill commands
        filling the gap). See ``TestExplicitMaxCommandsOverrideStillRespected``
        for the config-level unit version of the same invariant.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _write_curated_config(tmp_path, curated=True)
        cap = telegram_menu_max_commands()
        assert cap == len(CLIENT_MENU_COMMANDS)


# ---------------------------------------------------------------------------
# End-to-end: the REAL shipped template leaves zero menu slots for skill
# commands, however many skills are installed, AND drops none of its own
# client-layer commands off the visible menu.
#
# Before Task 1b, assets/config/trix-config.yaml pinned
# platforms.telegram.extra.command_menu.max_commands to a literal 27 --
# exactly len(CLIENT_MENU_COMMANDS) on the day that line was written. The
# list grew to 33 without the literal following it, so the last 6
# client-menu commands were silently clamped off the visible menu (see
# telegram_menu_commands()'s `all_commands[:max_commands]` in
# hermes_cli/commands.py) -- no error anywhere, just a smaller menu than
# the product intended.
#
# Task 1b (docs/product/plans/2026-09-01-client-command-surface.md) fixes
# this at the root: assets/config/trix-config.yaml no longer sets
# max_commands at all, and hermes_cli.commands._telegram_command_menu_config
# derives the cap from len(CLIENT_MENU_COMMANDS) whenever curation is on
# and no explicit override is present. The tests below install the REAL
# shipped template into a temp HERMES_HOME (not a synthetic stand-in) and
# check that derivation end-to-end: nothing from CLIENT_MENU_COMMANDS is
# trimmed, and -- separately -- no skill command ever wins a freed slot.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TRIX_TEMPLATE_PATH = _REPO_ROOT / "assets" / "config" / "trix-config.yaml"


class TestRealSkillCommandsDoNotReachTheCuratedMenu:
    def test_shipped_template_derives_a_cap_that_fits_every_client_menu_command(
        self, tmp_path, monkeypatch
    ):
        """Nothing from the client layer is silently trimmed. Installs the
        REAL trix-config.yaml (the file no longer carries a max_commands
        literal at all -- see the module comment above) into a temp
        HERMES_HOME and asserts every CLIENT_MENU_COMMANDS name survives
        into the rendered Telegram menu. Formula over CLIENT_MENU_COMMANDS,
        not a literal count, so it keeps meaning the same thing after every
        future menu edit -- and it fails loudly (naming the missing
        command(s)) instead of quietly shipping a shorter menu, which is
        exactly the failure mode Task 1b closes.
        """
        real_home = Path(os.path.realpath(str(tmp_path)))
        monkeypatch.setenv("HERMES_HOME", str(real_home))
        (real_home / "config.yaml").write_text(
            _TRIX_TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        with patch(
            "hermes_cli.commands._collect_gateway_skill_entries",
            return_value=([], 0),
        ):
            menu, _hidden = telegram_menu_commands(max_commands=telegram_menu_max_commands())
        names = [n for n, _ in menu]
        missing = [n for n in CLIENT_MENU_COMMANDS if n not in names]
        assert not missing, (
            "shipped trix-config.yaml's derived cap drops client-menu "
            f"command(s) from the visible menu: {missing}"
        )

    def test_real_skill_md_files_do_not_reach_the_curated_menu(self, tmp_path, monkeypatch):
        """Installs the REAL trix-config.yaml exactly as a fresh install
        would (verbatim copy to $HERMES_HOME/config.yaml -- the same file
        hermes_cli.config_template.resolve_config_template() picks),
        seeds two real on-disk SKILL.md files, scans them with the real
        scanner (agent.skill_commands.scan_skill_commands()), and asserts
        the final Telegram menu contains ONLY CLIENT_MENU_COMMANDS names.

        Wraps the skills dir in os.path.realpath: on macOS, tempfile
        creates paths under /var/..., which is a symlink to
        /private/var/...; _collect_gateway_skill_entries's path-prefix
        filter compares against SKILLS_DIR.resolve(), so an unresolved
        tmp_path here silently fails the prefix match and the skill
        vanishes from every menu for a reason unrelated to curation --
        which would make this test pass for the wrong reason.

        This test does NOT depend on the exact derived cap value (unlike
        the class-level test above, which checks that the cap fits every
        client-menu command): whatever the derived cap comes out to, zero
        menu slots are free for skills as long as CLIENT_MENU_COMMANDS
        alone meets or exceeds it, so no skill command can ever win a slot.
        Mutation proof (Task 1b step 3): pin an explicit max_commands: 71
        under platforms.telegram.extra.command_menu in the real template
        this test loads, and this assertion goes red -- the derived-cap
        default's own headroom is what keeps skills out; a stale override
        reopens the door.
        """
        real_home = Path(os.path.realpath(str(tmp_path)))
        monkeypatch.setenv("HERMES_HOME", str(real_home))

        template_text = _TRIX_TEMPLATE_PATH.read_text(encoding="utf-8")
        (real_home / "config.yaml").write_text(template_text, encoding="utf-8")

        skills_dir = real_home / "skills"
        skill_specs = [
            ("trix-onboarding", "Trix Onboarding", "Walks a new client through their first session."),
            ("trix-billing-faq", "Trix Billing FAQ", "Answers common billing questions."),
        ]
        for _slug, title, description in skill_specs:
            skill_dir = skills_dir / _slug
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {title}\ndescription: {description}\n---\n\nBody.\n"
            )

        with patch("tools.skills_tool.SKILLS_DIR", skills_dir):
            from agent.skill_commands import scan_skill_commands

            scan_skill_commands()
            menu, _hidden = telegram_menu_commands(max_commands=telegram_menu_max_commands())

        names = {n for n, _ in menu}
        assert names <= set(CLIENT_MENU_COMMANDS), (
            "curated menu on a real install must contain ONLY "
            f"CLIENT_MENU_COMMANDS names, got extras: "
            f"{names - set(CLIENT_MENU_COMMANDS)}"
        )
        assert "trix_onboarding" not in names
        assert "trix_billing_faq" not in names


# ---------------------------------------------------------------------------
# max_commands stays a respected override -- Task 1b only changes what the
# DEFAULT is when the key is absent, never what happens when it's present.
# This is deliberate (spec §4): it's the product's own way to debug the
# menu cap on a client machine without shipping a code change.
# ---------------------------------------------------------------------------


class TestExplicitMaxCommandsOverrideStillRespected:
    def test_explicit_value_wins_over_the_derived_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        Path(tmp_path, "config.yaml").write_text(
            "platforms:\n"
            "  telegram:\n"
            "    extra:\n"
            "      command_menu:\n"
            "        curated: true\n"
            "        max_commands: 5\n"
        )
        assert telegram_menu_max_commands() == 5

    def test_absence_of_the_key_derives_from_client_menu_length_when_curated(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        Path(tmp_path, "config.yaml").write_text(
            "platforms:\n"
            "  telegram:\n"
            "    extra:\n"
            "      command_menu:\n"
            "        curated: true\n"
        )
        assert telegram_menu_max_commands() == len(CLIENT_MENU_COMMANDS)

    def test_absence_of_the_key_does_not_derive_from_client_menu_when_uncurated(
        self, tmp_path, monkeypatch
    ):
        """Uncurated (curated: false) intentionally keeps the old,
        registry-sized default -- deriving from CLIENT_MENU_COMMANDS would
        make the escape-hatch full menu clamp itself right back down to the
        curated size, defeating the point of turning curation off."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        Path(tmp_path, "config.yaml").write_text(
            "platforms:\n"
            "  telegram:\n"
            "    extra:\n"
            "      command_menu:\n"
            "        curated: false\n"
        )
        assert telegram_menu_max_commands() >= len(list(telegram_bot_commands()))


# ---------------------------------------------------------------------------
# /topup -- now DISABLED, not merely hidden.
#
# The original review pass (spec 2026-08-17) believed /topup's no-account
# reply always said "log into Nous Portal" and considered removing it from
# the gateway entirely. That premise did not hold by execution:
# gateway.credits.not_logged_in was already rewritten (commit 2342d2308) to
# point the client at their own provider's console and never names Nous --
# tests/agent/test_credits_view.py::test_gateway_topup_never_names_the_upstream_vendor
# pins exactly that, so /topup itself is not broken.
#
# The client-command-surface spec (2026-09-01) disables it anyway, on a
# different ground (Ruling 4.3, "duplicates a client-layer command"): its
# whole purpose is showing spend, and /usage already covers that -- see
# DISABLED_COMMANDS["topup"] above.
# ---------------------------------------------------------------------------


class TestTopupNowDisabled:
    def test_not_in_client_menu(self):
        assert "topup" not in CLIENT_MENU_COMMANDS

    def test_not_in_service_commands(self):
        assert "topup" not in SERVICE_COMMANDS

    def test_in_disabled_commands_with_a_usage_replacement(self):
        assert "topup" in DISABLED_COMMANDS
        entry = DISABLED_COMMANDS["topup"]
        assert entry.hint == "replace"
        assert entry.replacement == "usage"

    def test_is_disabled_in_gateway(self):
        assert is_disabled_in_gateway("topup") is True

    def test_still_resolves_in_the_registry(self):
        """Being disabled is a Trix-layer decision, not a registry fact --
        /topup keeps existing as a CommandDef (the upstream command still
        works correctly, per the docstring above); only Trix's dispatcher
        (Task 4) is meant to refuse to run it."""
        cmd = resolve_command("topup")
        assert cmd is not None
        assert cmd.cli_only is False
        assert cmd.gateway_only is False


# ---------------------------------------------------------------------------
# Task 6 + description review -- Russian menu-description text quality.
#
# The review pass that produced this module's original HIDDEN_REASONS also
# read the menu through a non-technical Telegram client's eyes and found
# descriptions that either misdescribed real gateway behavior (verified by
# executing the handlers, not by reading them) or leaked internal jargon /
# a competitor's product name. These are invariants over the *surviving*
# CLIENT_MENU_COMMANDS text -- not a snapshot of any one description -- so
# a future edit reintroducing the same class of defect (a menu string that
# says "шлюз", "идентификатор", or names a competitor's product) turns this
# red without pinning exact wording.
# ---------------------------------------------------------------------------

# Words a non-technical Telegram client should never see in a menu
# description: internal architecture vocabulary ("шлюз" -- the gateway
# process; "идентификатор" -- session id) that CLAUDE.md's own "known
# pitfalls" and this review pass called out by name.
#
# The "filesystem checkpoints" jargon is NOT a single fixed phrase: Russian
# inflects "контрольные точки" (nominative, the pre-fix wording in
# rollback's own description -- see `git show e279870e8:locales/ru.yaml`)
# differently from "контрольных точек" (genitive, e.g. inside "N контрольных
# точек"), while "файловой систем-ы/-е" only ever changes its ending. A
# single exact-phrase string here previously matched neither inflection that
# actually shipped and could not go red on the real old text -- caught by a
# review that mutated with the literal ``git show`` string instead of a
# hand-typed guess. Represented as a tuple of independently-inflecting
# STEMS that must ALL appear (AND, not a phrase match): "контрольн" catches
# every case/number of "контрольная/-ые/-ых точка/-и/-ек", "файловой систем"
# catches every case of "файловая система" via its invariant stem.
_BANNED_RU_JARGON = (
    "шлюз",
    "идентификатор",
    ("контрольн", "файловой систем"),
)

# Competitor / unrelated third-party product names that must never appear in
# a description shown to a client who has no account with them (spec
# 2026-08-17-trix-agent-standard-build-design.md, "чужой бренд" throughout).
_BANNED_PRODUCT_NAMES = ("Codex", "Nous")

# Internal-architecture words in the CANONICAL ENGLISH (COMMAND_REGISTRY)
# text that a client should not see either -- this is the fallback every
# locale (including a locale-less ``en``) renders when no translation
# exists, so an English-only regression (e.g. /restart's canonical
# description keeping "gateway" while only its ru.yaml translation got
# fixed) must turn red too, not just a ru.yaml-side check.
_BANNED_EN_JARGON = ("gateway",)


def _client_menu_ru_descriptions() -> dict:
    """Russian ``commands.<name>.description`` for every CLIENT_MENU_COMMANDS
    name, requiring the key to actually be present.

    A silent ``.get(name, "")`` fallback would let a command with NO ru.yaml
    key at all pass every jargon/product-name check vacuously -- the same
    class of false-green the mutation review caught elsewhere in this file.
    ``test_every_gateway_command_has_a_russian_description``
    (tests/agent/test_i18n.py) already guarantees every gateway-visible
    command has a key; this asserts that guarantee explicitly before relying
    on it, so a coverage regression fails HERE with a clear message instead
    of silently voiding the jargon checks below.
    """
    locales_dir = Path(__file__).resolve().parents[2] / "locales"
    with (locales_dir / "ru.yaml").open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    commands = raw.get("commands", {}) or {}
    missing = [
        name for name in CLIENT_MENU_COMMANDS
        if not (isinstance(commands.get(name), dict) and "description" in commands[name])
    ]
    assert not missing, f"CLIENT_MENU_COMMANDS names missing a ru.yaml commands.*.description: {missing}"
    return {name: commands[name]["description"] for name in CLIENT_MENU_COMMANDS}


def _contains_banned(text: str, banned) -> bool:
    """True if *text* contains a banned single word, or ALL stems of a
    banned AND-tuple (see ``_BANNED_RU_JARGON``)."""
    if isinstance(banned, tuple):
        return all(stem in text for stem in banned)
    return banned in text


class TestClientMenuDescriptionsAvoidJargonAndProductNames:
    def test_no_banned_jargon_in_ru_descriptions(self):
        ru_descriptions = _client_menu_ru_descriptions()
        offenders = [
            (name, word)
            for name in CLIENT_MENU_COMMANDS
            for word in _BANNED_RU_JARGON
            if _contains_banned(ru_descriptions[name], word)
        ]
        assert not offenders, f"jargon leaked into the client menu: {offenders}"

    def test_no_competitor_product_names_in_ru_descriptions(self):
        ru_descriptions = _client_menu_ru_descriptions()
        offenders = [
            (name, word)
            for name in CLIENT_MENU_COMMANDS
            for word in _BANNED_PRODUCT_NAMES
            if word in ru_descriptions[name]
        ]
        assert not offenders, f"competitor product name leaked into the client menu: {offenders}"

    def test_no_competitor_product_names_in_canonical_english(self):
        """The English text in COMMAND_REGISTRY is the fallback every locale
        (including a locale-less en) renders when no translation exists, so
        it must be clean too, not just the ru.yaml translation."""
        offenders = [
            (cmd.name, word)
            for cmd in COMMAND_REGISTRY
            if cmd.name in CLIENT_MENU_COMMANDS
            for word in _BANNED_PRODUCT_NAMES
            if word in cmd.description
        ]
        assert not offenders, f"competitor product name in canonical English: {offenders}"

    def test_no_banned_jargon_in_canonical_english(self):
        """Mirrors the ru.yaml jargon check for the canonical English text
        (see module docstring above _BANNED_EN_JARGON for why this must be
        checked independently of the ru.yaml translation)."""
        offenders = [
            (cmd.name, word)
            for cmd in COMMAND_REGISTRY
            if cmd.name in CLIENT_MENU_COMMANDS
            for word in _BANNED_EN_JARGON
            if word in cmd.description.lower()
        ]
        assert not offenders, f"jargon leaked into the canonical English: {offenders}"


# ---------------------------------------------------------------------------
# Task 6 -- /debug's description must tell the client that the report
# includes log tails which may contain fragments of their OWN CONVERSATION
# (spec §8.4's actual requirement -- hermes_cli/debug.py's own docstring on
# _save_report_locally: agent.log holds "plaintext conversation content").
# Checked in both languages Trix actually ships -- ru (default) and en
# (registry canonical / fallback) -- per spec 3's "test written as a pair"
# rule.
#
# A bare "лог"/"log" substring check is not enough: "Собрать локальный отчёт
# для диагностики: сведения о системе и логи" (the description BEFORE this
# task's fix) already contained "логи" and would have passed such a check
# while saying nothing about conversation content -- exactly the gap §8.4
# exists to close. Requires BOTH the log mention AND a conversation/chat-
# content mention, not exact wording, since ru.yaml prose is free to evolve.
# ---------------------------------------------------------------------------

_RU_CONVERSATION_MARKERS = ("переписк", "разговор", "сообщени")
_EN_CONVERSATION_MARKERS = ("conversation", "chat", "message")


class TestDebugDescriptionMentionsLogs:
    def test_mentions_logs_and_conversation_content_in_russian(self, monkeypatch):
        from agent import i18n

        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        try:
            cmd = resolve_command("debug")
            text = command_description(cmd).lower()
            assert "лог" in text, f"ru /debug description doesn't mention logs: {text!r}"
            assert any(m in text for m in _RU_CONVERSATION_MARKERS), (
                f"ru /debug description doesn't warn about conversation content "
                f"(spec §8.4): {text!r}"
            )
        finally:
            i18n.reset_language_cache()

    def test_mentions_logs_and_conversation_content_in_english(self, monkeypatch):
        from agent import i18n

        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        try:
            cmd = resolve_command("debug")
            text = command_description(cmd).lower()
            assert "log" in text, f"en /debug description doesn't mention logs: {text!r}"
            assert any(m in text for m in _EN_CONVERSATION_MARKERS), (
                f"en /debug description doesn't warn about conversation content "
                f"(spec §8.4): {text!r}"
            )
        finally:
            i18n.reset_language_cache()
