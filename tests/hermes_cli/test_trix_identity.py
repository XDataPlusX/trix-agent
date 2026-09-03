"""Identity text must present the product as Trix Agent, never as upstream."""

import re
from pathlib import Path

from agent.prompt_builder import DEFAULT_AGENT_IDENTITY
from hermes_cli.default_soul import DEFAULT_SOUL_MD

FORBIDDEN = ("Hermes", "Nous Research")

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_skills_prompt_directs_to_trix_agent_skill(monkeypatch, tmp_path):
    """The mandatory skills block must name the product Trix Agent and point
    at the `trix-agent` skill — not the removed `hermes-agent` skill, whose
    absence would make skill loading fail on exactly the questions it exists
    to answer."""
    from agent.prompt_builder import (
        build_skills_system_prompt,
        clear_skills_system_prompt_cache,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skill_dir = tmp_path / "skills" / "tools" / "some-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: some-skill\ndescription: A minimal test skill\n---\n"
    )

    clear_skills_system_prompt_cache(clear_snapshot=True)
    try:
        result = build_skills_system_prompt()
    finally:
        clear_skills_system_prompt_cache(clear_snapshot=True)

    assert "trix-agent" in result
    assert "hermes-agent" not in result
    assert "Hermes Agent" not in result


def test_default_soul_presents_trix_agent():
    assert "Trix Agent" in DEFAULT_SOUL_MD
    for word in FORBIDDEN:
        assert word not in DEFAULT_SOUL_MD


def test_install_sh_seeds_current_default_soul_md():
    """A fresh install writes DEFAULT_SOUL_MD verbatim into ~/.hermes/SOUL.md.

    scripts/install.sh carries its own copy of the persona text in a shell
    heredoc (it can't import the Python constant at install time), so it can
    silently drift from hermes_cli/default_soul.py. Parse the actual heredoc
    installers ship and assert it matches byte-for-byte -- a persona test
    that only checks the Python constant, as every prior test here did,
    cannot see this file at all.
    """
    content = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    match = re.search(
        r"cat > \"\$HERMES_HOME/SOUL\.md\" << 'SOUL_EOF'\n(.*?)\nSOUL_EOF",
        content,
        re.S,
    )
    assert match, "could not find the SOUL.md heredoc in install.sh"
    assert match.group(1) == DEFAULT_SOUL_MD


def test_install_ps1_seeds_current_default_soul_md():
    content = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    match = re.search(r'\$soulContent = @"\n(.*?)\n"@', content, re.S)
    assert match, "could not find the SOUL.md here-string in install.ps1"
    assert match.group(1) == DEFAULT_SOUL_MD


def test_docker_soul_md_matches_current_default_soul_md():
    content = (REPO_ROOT / "docker" / "SOUL.md").read_text(encoding="utf-8")
    assert content.rstrip("\n") == DEFAULT_SOUL_MD


def test_install_sh_soul_seed_is_not_legacy_template():
    """Guard against a regression that reintroduces the comment-only scaffold.

    If install.sh ever goes back to seeding the old empty scaffold instead of
    DEFAULT_SOUL_MD, config.py's self-heal would silently paper over it on
    next run -- but a real user's first `hermes` session, before that
    self-heal fires, would still see no persona at all.
    """
    from hermes_cli.default_soul import is_legacy_template_soul

    content = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    match = re.search(
        r"cat > \"\$HERMES_HOME/SOUL\.md\" << 'SOUL_EOF'\n(.*?)\nSOUL_EOF",
        content,
        re.S,
    )
    assert match
    assert not is_legacy_template_soul(match.group(1))


def test_fallback_identity_presents_trix_agent():
    assert "Trix Agent" in DEFAULT_AGENT_IDENTITY
    for word in FORBIDDEN:
        assert word not in DEFAULT_AGENT_IDENTITY


def test_help_guidance_is_trix_branded_and_offline():
    from agent.prompt_builder import TRIX_AGENT_HELP_GUIDANCE

    assert "Trix Agent" in TRIX_AGENT_HELP_GUIDANCE
    assert "skill_view(name='trix-agent')" in TRIX_AGENT_HELP_GUIDANCE
    assert "nousresearch.com" not in TRIX_AGENT_HELP_GUIDANCE
    for word in ("Hermes", "Nous Research"):
        assert word not in TRIX_AGENT_HELP_GUIDANCE


def test_help_guidance_does_not_promise_a_bundled_doc_set():
    """No Trix-branded doc set ships with the release -- install.sh clones
    the whole upstream repo onto the customer's VM, so website/docs/**
    (393 mostly-upstream-branded files) really is on disk and readable by
    the agent's own file tools. The guidance must name the trix-agent skill
    as the sole authority instead of pointing the model at "the
    documentation bundled with the release", which doesn't exist for this
    product and would send the model straight into the upstream doc tree.
    """
    from agent.prompt_builder import TRIX_AGENT_HELP_GUIDANCE

    assert "bundled with" not in TRIX_AGENT_HELP_GUIDANCE
    assert "sole authoritative reference" in TRIX_AGENT_HELP_GUIDANCE


def test_trix_agent_skill_does_not_promise_a_bundled_doc_set():
    from pathlib import Path

    skill_md = (
        REPO_ROOT
        / "skills"
        / "autonomous-ai-agents"
        / "trix-agent"
        / "SKILL.md"
    )
    content = skill_md.read_text(encoding="utf-8")
    assert "bundled with" not in content


# ---------------------------------------------------------------------------
# Four-way persona sync -- the test that matters most.
#
# The persona text lives in FOUR places: hermes_cli/default_soul.py
# (DEFAULT_SOUL_MD), agent/prompt_builder.py (DEFAULT_AGENT_IDENTITY),
# scripts/install.sh's heredoc, and hermes_cli/doctor.py's --fix path. Their
# agreement used to be held together only by a "This MUST match
# DEFAULT_SOUL_MD" code comment -- and that comment failed silently: doctor.py
# drifted to its own hardcoded, un-Russian, upstream-branded scaffold that
# hermes_cli.default_soul.is_legacy_template_soul() didn't recognize, so the
# runtime self-heal never fixed it. This test replaces the comment with a
# real assertion so a future drift fails CI instead of shipping silently.
#
# The doctor.py copy is extracted behaviorally (by actually running its
# --fix code path against a temp HERMES_HOME) rather than by regexing its
# source, per the project's "never read source code in tests" rule -- it now
# just imports DEFAULT_SOUL_MD, so a source-text extraction would trivially
# always match even if someone reintroduced a hardcoded duplicate elsewhere
# in the function. Running the real code path only passes if the function
# actually writes the shared constant.
#
# install.sh can't be imported, so its copy is extracted from the shell
# heredoc via regex -- if the heredoc marker ever changes shape, the `assert
# match` below fails the test outright rather than silently skipping it.
# ---------------------------------------------------------------------------


def _extract_install_sh_soul_text() -> str:
    content = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    match = re.search(
        r"cat > \"\$HERMES_HOME/SOUL\.md\" << 'SOUL_EOF'\n(.*?)\nSOUL_EOF",
        content,
        re.S,
    )
    assert match, (
        "could not find the SOUL.md heredoc in scripts/install.sh -- the "
        "persona sync test cannot verify this copy without it. This must "
        "FAIL, not skip: an installer that no longer seeds a recognizable "
        "SOUL.md heredoc is itself a regression."
    )
    return match.group(1)


def _extract_doctor_fix_soul_text(tmp_path) -> str:
    """Run hermes_cli.doctor's real --fix SOUL.md path and return what it wrote."""
    from hermes_cli.doctor import _check_soul_md

    hermes_home = tmp_path / "doctor_persona_sync_check"
    hermes_home.mkdir()
    fixed = _check_soul_md(hermes_home, should_fix=True)
    assert fixed == 1, "doctor's SOUL.md --fix path did not report applying a fix"
    soul_path = hermes_home / "SOUL.md"
    assert soul_path.exists(), "doctor's SOUL.md --fix path did not create the file"
    return soul_path.read_text(encoding="utf-8")


def test_all_four_persona_copies_match(tmp_path):
    install_sh_text = _extract_install_sh_soul_text()
    doctor_text = _extract_doctor_fix_soul_text(tmp_path)

    copies = {
        "hermes_cli/default_soul.py DEFAULT_SOUL_MD": DEFAULT_SOUL_MD,
        "agent/prompt_builder.py DEFAULT_AGENT_IDENTITY": DEFAULT_AGENT_IDENTITY,
        "scripts/install.sh heredoc": install_sh_text,
        "hermes_cli/doctor.py --fix output": doctor_text,
    }
    reference_name = "hermes_cli/default_soul.py DEFAULT_SOUL_MD"
    reference = copies[reference_name]
    mismatches = {
        name: text for name, text in copies.items() if text != reference
    }
    assert not mismatches, (
        f"persona text diverged from {reference_name!r}: "
        f"{sorted(mismatches.keys())}"
    )


def test_persona_copies_include_russian_default_language_instruction():
    """Part 2.1: the language instruction must be present in every copy the
    sync test above compares, not just the reference constant."""
    marker = "Respond in Russian by default"
    assert marker in DEFAULT_SOUL_MD
    assert marker in DEFAULT_AGENT_IDENTITY
    assert marker in _extract_install_sh_soul_text()


def test_old_doctor_template_is_recognized_as_legacy_and_self_heals(tmp_path):
    """Regression for the doctor.py defect: before this fix, `hermes doctor
    --fix` wrote a short hardcoded scaffold ending in "You are Hermes, a
    helpful AI assistant." that is_legacy_template_soul() did not recognize,
    so hermes_cli.config._ensure_default_soul_md treated it as
    user-customized and never upgraded it. This is the case that matters most
    for a real customer: they already ran the broken `doctor --fix` before
    updating, and their SOUL.md is stuck on upstream identity in English. The
    recognizer must know this exact old text so the next normal `hermes` run
    (which calls ensure_hermes_home -> _ensure_default_soul_md) heals it.
    """
    import os
    from unittest.mock import patch

    from hermes_cli.config import ensure_hermes_home
    from hermes_cli.default_soul import is_legacy_template_soul

    old_broken_doctor_template = (
        "# Hermes Agent Persona\n\n"
        "<!-- Edit this file to customize how Hermes communicates. -->\n\n"
        "You are Hermes, a helpful AI assistant.\n"
    )
    assert is_legacy_template_soul(old_broken_doctor_template)

    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        soul_path = tmp_path / "SOUL.md"
        soul_path.write_text(old_broken_doctor_template, encoding="utf-8")
        ensure_hermes_home()
        assert soul_path.read_text(encoding="utf-8") == DEFAULT_SOUL_MD


def test_previous_trix_persona_self_heals_into_the_russian_default(tmp_path):
    """An install seeded before the Russian instruction must pick it up.

    The persona shipped one revision earlier is not a legacy *scaffold* --
    it reads as real content -- so without an explicit entry in
    ``_LEGACY_TEMPLATE_SOULS`` ``_ensure_default_soul_md`` would leave it
    alone forever and the agent would keep answering in whatever language
    the first message happened to arrive in. That is the whole point of the
    default, so an upgrade has to reach it.
    """
    import os
    from unittest.mock import patch

    from hermes_cli.config import ensure_hermes_home
    from hermes_cli.default_soul import DEFAULT_SOUL_MD

    previous_trix_persona = (
        "You are Trix Agent, an intelligent AI assistant provided by XDataPlus. "
        "You are helpful, knowledgeable, and direct. You assist users with a wide "
        "range of tasks including answering questions, writing and editing code, "
        "analyzing information, creative work, and executing actions via your tools. "
        "You communicate clearly, admit uncertainty when appropriate, and prioritize "
        "being genuinely useful over being verbose unless otherwise directed below. "
        "Be targeted and efficient in your exploration and investigations."
    )
    assert "Russian" not in previous_trix_persona, "fixture must predate the language sentence"

    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        soul_path = tmp_path / "SOUL.md"
        soul_path.write_text(previous_trix_persona, encoding="utf-8")
        ensure_hermes_home()
        assert soul_path.read_text(encoding="utf-8") == DEFAULT_SOUL_MD


def test_a_customer_edit_to_the_previous_persona_is_never_clobbered(tmp_path):
    """Self-healing must not become "overwrite whatever we recognise".

    Destroying a persona the customer wrote is far worse than leaving a
    stale one, so recognition is byte-exact: one added line and the file is
    theirs, untouched.
    """
    import os
    from unittest.mock import patch

    from hermes_cli.config import ensure_hermes_home

    customised = (
        "You are Trix Agent, an intelligent AI assistant provided by XDataPlus. "
        "You are helpful, knowledgeable, and direct. You assist users with a wide "
        "range of tasks including answering questions, writing and editing code, "
        "analyzing information, creative work, and executing actions via your tools. "
        "You communicate clearly, admit uncertainty when appropriate, and prioritize "
        "being genuinely useful over being verbose unless otherwise directed below. "
        "Be targeted and efficient in your exploration and investigations."
        "\n\nВсегда отвечай коротко и по делу."
    )

    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        soul_path = tmp_path / "SOUL.md"
        soul_path.write_text(customised, encoding="utf-8")
        ensure_hermes_home()
        assert soul_path.read_text(encoding="utf-8") == customised
