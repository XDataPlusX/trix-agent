"""Tests for optional-skills/autonomous-ai-agents/trix-business-rollout/SKILL.md.

This skill is an operator runbook, not an agent-facing skill (see its own
"When to Use" — it explicitly documents that it's for us, not for
`system_admin` or client profiles). It still lives under the same
authoring-standards regime as every other SKILL.md, so this file checks the
same two things ``tests/skills/test_trix_file_contour_skill.py`` checks for
its neighbor: the frontmatter hardline rules, and that every command/flag/
refusal the document quotes is real — driven through actual imports and
argparse trees, never a text-regex of ``.py`` source.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

import pytest
import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "autonomous-ai-agents"
    / "trix-business-rollout"
)
SKILL_MD = SKILL_DIR / "SKILL.md"
REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _normalized_skill_text() -> str:
    """Collapse whitespace/newlines — quoted refusal strings are wrapped
    across Markdown lines, but the real exception message is a single
    line; comparing after normalizing whitespace lets a substring check
    survive re-wrapping without pretending the doc is byte-identical to
    the source string."""
    return re.sub(r"\s+", " ", _skill_text())


def _in_skill(fixed_substring: str) -> bool:
    return re.sub(r"\s+", " ", fixed_substring) in _normalized_skill_text()


def _frontmatter_field(name: str) -> str:
    m = re.search(rf"^{name}: (.*)$", _skill_text(), re.MULTILINE)
    assert m, f"{name} field missing from SKILL.md frontmatter"
    return m.group(1).strip().strip('"')


# ---------------------------------------------------------------------------
# HARDLINE authoring standards (CLAUDE.md, "Skill authoring standards") —
# tests/skills/test_authoring_standards.py already enforces this generically
# across every skill; these two are kept here too (matching the sibling
# trix-file-contour test file's own explicit checks) since this skill's
# description is the thing most likely to drift as the runbook grows.
# ---------------------------------------------------------------------------


def test_description_is_short_one_sentence():
    desc = _frontmatter_field("description")
    assert len(desc) <= 60, len(desc)
    assert desc.endswith(".")


def test_platforms_declared():
    assert _frontmatter_field("platforms") == "[linux]"


def test_related_skills_are_the_two_agent_facing_skills():
    text = _skill_text()
    assert "trix-file-contour" in text
    assert "trix-system-admin" in text


def test_states_it_is_not_for_client_profiles_or_system_admin():
    """The brief for this skill is explicit: it is for us, not for client
    profiles and not for the system_admin agent. Assert the doc actually
    says so, rather than trusting it was written in."""
    text = _skill_text()
    assert "Не для" in text
    assert "system_admin" in text


# ---------------------------------------------------------------------------
# Documented CLI surface matches the real argparse trees — behavior
# contract, not a text-regex of the .py source.
# ---------------------------------------------------------------------------


def _business_setup_parser():
    from hermes_cli.trix_business import register_cli

    parser = argparse.ArgumentParser()
    register_cli(parser)
    subparsers_actions = [
        a for a in parser._subparsers._group_actions  # noqa: SLF001 — argparse has no public introspection API
        if isinstance(a, argparse._SubParsersAction)
    ]
    return subparsers_actions[0].choices["setup"]


def test_skill_documents_the_real_business_setup_flags():
    setup_parser = _business_setup_parser()
    option_strings: set = set()
    for action in setup_parser._actions:  # noqa: SLF001
        option_strings.update(action.option_strings)

    documented_flags = {
        "--company-root",
        "--profile-name",
        "--admin-telegram-id",
        "--group-chat-id",
        "--admin-thread-id",
    }
    assert documented_flags <= option_strings  # every flag we test IS real
    text = _skill_text()
    for flag in documented_flags:
        assert flag in text, flag


def test_skill_documents_the_real_contour_cli_verbs_it_names():
    """This skill points administrators at `hermes contour status`/`check`
    for verification — assert those verbs are real (not the full verb set;
    trix-file-contour's own test already covers that library end to end)."""
    from hermes_cli.trix_contour_cli import register_cli

    parser = argparse.ArgumentParser()
    register_cli(parser)
    subparsers_actions = [
        a for a in parser._subparsers._group_actions  # noqa: SLF001
        if isinstance(a, argparse._SubParsersAction)
    ]
    real_verbs = set(subparsers_actions[0].choices.keys())

    text = _skill_text()
    for verb in ("status", "check"):
        assert verb in real_verbs
        assert f"hermes contour {verb}" in text


# ---------------------------------------------------------------------------
# Quoted refusals match what run_setup() (and its validators) actually
# raise — trigger the real BusinessSetupError, then check the fixed
# (non-interpolated) portion of its message is really quoted in the doc.
# ---------------------------------------------------------------------------


@pytest.fixture()
def biz_env(tmp_path, monkeypatch):
    """Mirrors tests/hermes_cli/test_trix_business.py's own fixture: an
    already-installed machine with the real client config template and an
    admin already recorded in TELEGRAM_ALLOWED_USERS."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    (default_home / "config.yaml").write_text(
        TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (default_home / ".env").write_text("TELEGRAM_ALLOWED_USERS=111222333\n", encoding="utf-8")
    return default_home


def _company_root(default_home: Path) -> str:
    return str(default_home.parent / "srv" / "trix")


def test_missing_telegram_allowed_users_refusal_matches(tmp_path, monkeypatch):
    from hermes_cli import trix_business as tb

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    (default_home / "config.yaml").write_text(
        TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (default_home / ".env").write_text("", encoding="utf-8")

    with pytest.raises(tb.BusinessSetupError) as exc:
        tb.run_setup(company_root=_company_root(default_home), install_skills=False)

    msg = str(exc.value)
    assert "TELEGRAM_ALLOWED_USERS" in msg
    assert _in_skill(msg)  # this message has no interpolated parts — exact quote


def test_admin_id_not_numeric_refusal_matches():
    from hermes_cli import trix_business as tb

    with pytest.raises(tb.BusinessSetupError) as exc:
        tb._validate_admin_telegram_ids(
            ["not-a-number"], allowed_ids=["111"], raw_allowed="111", is_group_path=False,
        )
    assert _in_skill("--admin-telegram-id должен быть числовым Telegram id")
    assert "не число" not in str(exc.value)  # sanity: real message, not a stub


def test_admin_id_not_in_allowlist_refusal_matches():
    from hermes_cli import trix_business as tb

    with pytest.raises(tb.BusinessSetupError) as exc:
        tb._validate_admin_telegram_ids(
            ["999"], allowed_ids=["111"], raw_allowed="111", is_group_path=False,
        )
    msg = str(exc.value)
    fixed = (
        "бот игнорирует сообщения от того, кого нет в этом списке, так что тема "
        "администрирования оказалась бы в чате, куда никто не сможет написать"
    )
    assert fixed in msg
    assert _in_skill(fixed)


def test_multiple_admin_ids_with_group_path_refusal_matches():
    from hermes_cli import trix_business as tb

    with pytest.raises(tb.BusinessSetupError) as exc:
        tb._validate_admin_telegram_ids(
            ["1", "2"], allowed_ids=["1", "2"], raw_allowed="1,2", is_group_path=True,
        )
    fixed = "--admin-telegram-id указан несколько раз вместе с --group-chat-id/--admin-thread-id"
    assert fixed in str(exc.value)
    assert _in_skill(fixed)


def test_multiplex_profiles_false_refusal_matches(tmp_path, monkeypatch):
    from hermes_cli import trix_business as tb

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    (default_home / "config.yaml").write_text(
        "gateway:\n  multiplex_profiles: false\n", encoding="utf-8"
    )
    (default_home / ".env").write_text("TELEGRAM_ALLOWED_USERS=111222333\n", encoding="utf-8")

    with pytest.raises(tb.BusinessSetupError) as exc:
        tb.run_setup(company_root=_company_root(default_home), install_skills=False)

    msg = str(exc.value)
    assert "gateway.multiplex_profiles явно выставлен в false" in msg
    assert _in_skill(msg)  # no interpolated parts — exact quote


def test_company_root_not_absolute_refusal_matches():
    from hermes_cli import trix_business as tb

    with pytest.raises(tb.BusinessSetupError) as exc:
        tb._validate_company_root("relative/path")
    fixed = "--company-root должен быть абсолютным путём"
    assert fixed in str(exc.value)
    assert _in_skill(fixed)


def test_profile_name_default_refusal_matches(tmp_path, monkeypatch):
    from hermes_cli import trix_business as tb

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    with pytest.raises(tb.BusinessSetupError) as exc:
        tb.run_setup(company_root=str(tmp_path / "srv" / "trix"), profile_name="default")

    fixed = "именем администраторского профиля не может быть 'default'"
    assert fixed in str(exc.value)
    assert _in_skill(fixed)


def test_admin_config_missing_refusal_matches(biz_env):
    """A profile directory that exists but was never fully created (no
    config.yaml) — run_setup refuses rather than repairing it. Constructed
    directly (mkdir, no config.yaml) since profile_exists() only checks
    directory presence (hermes_cli/profiles.py)."""
    from hermes_cli import trix_business as tb

    (biz_env / "profiles" / "halfdone").mkdir(parents=True)

    with pytest.raises(tb.BusinessSetupError) as exc:
        tb.run_setup(
            company_root=_company_root(biz_env),
            profile_name="halfdone",
            admin_telegram_id=["111222333"],
            install_skills=False,
        )

    msg = str(exc.value)
    assert "нет config.yaml" in msg
    assert _in_skill("нет config.yaml")


# ---------------------------------------------------------------------------
# Constants the skill quotes verbatim (paths, topic name, toolset list) —
# cross-checked against the real module attributes, not retyped by hand.
# ---------------------------------------------------------------------------


def test_admin_dm_topic_name_matches():
    from hermes_cli import trix_business as tb

    assert tb.ADMIN_DM_TOPIC_NAME in _skill_text()


def test_dm_topic_marker_relative_path_matches():
    from hermes_cli import trix_business as tb

    # Path("business_setup") / "pending_dm_topic.json" — compare as
    # POSIX-style text, which is how the doc necessarily renders it.
    assert tb.DM_TOPIC_MARKER_RELATIVE.as_posix() in _skill_text()


def test_admin_allowlist_relative_path_matches():
    # trix_contour._admin_allowlist_path() is <admin_home>/trix_contour/admin_profiles.json
    assert "trix_contour/admin_profiles.json" in _skill_text()


def test_system_admin_toolsets_all_named():
    from hermes_cli import trix_business as tb

    text = _skill_text()
    for toolset in tb.SYSTEM_ADMIN_TOOLSETS:
        assert f"`{toolset}`" in text, toolset


# ---------------------------------------------------------------------------
# The live-check log anchor (spec 21 §13) — behavioral: actually trigger
# TelegramAdapter._create_dm_topic() against a fake bot and read the real
# logged line back via caplog, rather than grepping the adapter's source.
# ---------------------------------------------------------------------------


def test_create_dm_topic_log_anchor_matches_what_the_skill_tells_operators_to_grep(caplog):
    from gateway.config import Platform
    from plugins.platforms.telegram.adapter import TelegramAdapter

    class _FakeTopic:
        message_thread_id = 4242

    class _FakeBot:
        async def create_forum_topic(self, **kwargs):
            return _FakeTopic()

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM  # adapter.name is derived from this (read-only property)
    adapter._bot = _FakeBot()

    with caplog.at_level("INFO", logger="plugins.platforms.telegram.adapter"):
        thread_id = asyncio.run(adapter._create_dm_topic(555, "Администрирование"))

    assert thread_id == 4242
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Created DM topic 'Администрирование' in chat 555" in logged
    assert "thread_id=4242" in logged

    # The skill tells operators to grep for this anchor in `hermes logs
    # --follow` — assert the fixed (non-interpolated) shape it names is a
    # real substring of what actually gets logged.
    anchor = "Created DM topic 'Администрирование' in chat ... -> thread_id=..."
    assert _in_skill(anchor)
