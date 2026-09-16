"""Tests for optional-skills/autonomous-ai-agents/trix-file-contour/SKILL.md.

The security-critical logic used to live in this skill's own
``scripts/contour.py`` (a free-form-YAML prototype — path traversal, no
symlink checks, no profile binding). It has been replaced by the closed
JSON grammar in ``hermes_cli/trix_contour.py`` + the `hermes contour` CLI;
see ``tests/hermes_cli/test_trix_contour.py`` for the full security suite
(symlinks, path traversal, dept/company binding, tamper detection, git
history, token handling — all against real filesystem state).

This file only tests what belongs to the SKILL surface itself: frontmatter
compliance (HARDLINE authoring standards in CLAUDE.md), that the unsafe
free-form script is actually gone from the tree, and that the documented
grammar/CLI verbs match what the library and CLI actually expose — a
behavior contract against real imports, not a text-regex of source code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "autonomous-ai-agents"
    / "trix-file-contour"
)
SKILL_MD = SKILL_DIR / "SKILL.md"


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _frontmatter_field(name: str) -> str:
    m = re.search(rf"^{name}: (.*)$", _skill_text(), re.MULTILINE)
    assert m, f"{name} field missing from SKILL.md frontmatter"
    return m.group(1).strip().strip('"')


# ---------------------------------------------------------------------------
# HARDLINE authoring standards (CLAUDE.md, "Skill authoring standards")
# ---------------------------------------------------------------------------


def test_description_is_short_one_sentence():
    desc = _frontmatter_field("description")
    assert len(desc) <= 60, len(desc)
    assert desc.endswith(".")


def test_platforms_declared():
    assert _frontmatter_field("platforms") == "[linux]"


# ---------------------------------------------------------------------------
# The unsafe prototype is actually gone — nothing in the tree still offers
# the free-form path.
# ---------------------------------------------------------------------------


def test_unsafe_prototype_script_removed():
    assert not (SKILL_DIR / "scripts" / "contour.py").exists()
    assert not (SKILL_DIR / "templates" / "contour.example.yaml").exists()


def test_skill_md_no_longer_documents_the_old_free_form_yaml_workflow():
    """A one-line historical footnote naming the deleted script is fine
    (it explains WHY the rewrite happened); what must be gone is any
    operational instruction that still points an operator at it."""
    text = _skill_text()
    for stale in (
        "scripts/contour.py plan",
        "scripts/contour.py apply",
        "scripts/contour.py check",
        "cp templates/contour.example.yaml",
        "contour.yaml`",
        "run_as_host_user",
    ):
        assert stale not in text, stale


# ---------------------------------------------------------------------------
# Documented grammar matches the real library — behavior contract, not a
# text-regex of the .py source: import the module and compare its actual
# exposed constants/CLI surface against what SKILL.md tells the operator.
# ---------------------------------------------------------------------------


def test_skill_md_documents_every_op_in_the_real_grammar():
    from hermes_cli import trix_contour as tc

    text = _skill_text()
    for op in tc.VALID_OPS:
        assert f"`{op}`" in text, op
    # and nothing else claims to be an op — every op name mentioned in the
    # grammar table must be a real one (catches a stale/renamed op name).
    table_ops = set(re.findall(r"\| `(\w+)` \|", text))
    assert table_ops == set(tc.VALID_OPS)


def test_skill_md_documents_the_real_cli_verbs():
    import argparse

    from hermes_cli.trix_contour_cli import register_cli

    parser = argparse.ArgumentParser()
    register_cli(parser)
    subparsers_actions = [
        a for a in parser._subparsers._group_actions  # noqa: SLF001 — argparse has no public introspection API
        if isinstance(a, argparse._SubParsersAction)
    ]
    real_verbs = set(subparsers_actions[0].choices.keys())

    text = _skill_text()
    for verb in real_verbs:
        assert f"contour {verb}" in text, verb


def test_documented_night_window_matches_real_boundary_behavior():
    """Not a constant snapshot: exercises the real `_in_night_window` gate
    AT ITS BOUNDARIES (start hour included, end hour excluded) and
    cross-checks that SKILL.md's advertised window text describes exactly
    those hours. A docs/code drift here would silently give the human a
    wrong mental model of when a `when: night` request actually applies —
    just asserting the substring (the old version) would keep passing even
    if `_in_night_window` were reimplemented with off-by-one boundaries."""
    from datetime import datetime

    from hermes_cli import trix_contour as tc

    start, end = tc.NIGHT_WINDOW_START_HOUR, tc.NIGHT_WINDOW_END_HOUR
    assert tc._in_night_window(datetime(2026, 1, 1, start, 0, 0)) is True
    assert tc._in_night_window(datetime(2026, 1, 1, end - 1, 59, 59)) is True
    assert tc._in_night_window(datetime(2026, 1, 1, end, 0, 0)) is False
    before = (start - 1) % 24
    assert tc._in_night_window(datetime(2026, 1, 1, before, 59, 59)) is False

    text = _skill_text()
    window = f"{start:02d}:00–{end:02d}:00"
    assert window in text, (
        f"SKILL.md's advertised night window text does not match the real "
        f"NIGHT_WINDOW_START_HOUR/END_HOUR boundary ({window!r} not found)"
    )


@pytest.fixture
def contour_home(tmp_path, monkeypatch):
    """Just enough machine state for ``_validate_grantable_folder``'s
    ``dept/<X>``-owner-exists check to resolve real profiles — this test
    doesn't need the full ``hermes_home``/``_make_profile`` harness from
    ``tests/hermes_cli/test_trix_contour.py``, only two existing profile
    directories."""
    home = tmp_path / "hermes-home"
    (home / "profiles" / "sales").mkdir(parents=True)
    (home / "profiles" / "accounting").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


@pytest.mark.parametrize("folder", [
    "company",             # whitelisted shape 1
    "dept/sales",          # whitelisted shape 2 (existing profile)
    "dept",                # bare dept — NOT one of the two shapes
    "company/sub",         # company with a subfolder — refused
    "dept/sales/raw",      # depth-3 cross-department shape — refused
    "dept/ghostdept",      # dept owner segment is not an existing profile
    "templates",           # the deleted third "anything else" bucket
    "a/b/c",               # an arbitrary depth-3 path — legal for
                           # create_folder, but never grantable
])
def test_skill_md_documents_binding_rules_it_actually_enforces(contour_home, folder):
    """Item 10 rewrite: the previous version only exercised the two SAFE
    shapes (``dept/sales`` and ``company``), so the fixture never reached
    ``_check_binding``'s deleted "anything else" bucket or the narrowed
    grammar at all — every dangerous shape the review found (bare
    ``dept``, ``company/<sub>``, depth-3 cross-department paths) was
    UNREACHABLE by this test, even though SKILL.md's binding table already
    claimed only two shapes are grantable.

    Parametrised over every shape ``_validate_folder_rel`` itself accepts
    as syntactically valid (i.e. everything that could plausibly appear in
    a ``grant``/``revoke`` request) and asserts the SKILL.md claim end to
    end: only ``company`` and ``dept/<existing profile>`` are ever
    grantable, and every other syntactically valid path — including the
    two confirmed repros from the review — is refused."""
    from hermes_cli import trix_contour as tc

    rel_parts = tc._validate_folder_rel(folder, "`folder`")  # syntactically valid — must not raise
    if folder in ("company", "dept/sales"):
        tc._validate_grantable_folder(rel_parts)  # must NOT raise
    else:
        with pytest.raises(tc.ContourError):
            tc._validate_grantable_folder(rel_parts)


def test_check_binding_enforces_rw_ro_rules_for_the_two_whitelisted_shapes():
    """Spot-check the human-facing binding table against the enforcement
    functions themselves, without regexing their implementation: call them
    against known-bad combinations and require a refusal, exactly as
    SKILL.md claims. Runs AFTER shape validation in the real pipeline
    (``_plan_grant``/``_plan_revoke``), so it only ever sees the two
    whitelisted shapes — see the parametrised test above for the shape
    whitelist itself."""
    from hermes_cli import trix_contour as tc

    with pytest.raises(tc.ContourError):
        tc._check_binding(["dept", "sales"], "accounting", "rw", set())
    with pytest.raises(tc.ContourError):
        tc._check_binding(["dept", "sales"], "accounting", "ro", set())
    tc._check_binding(["dept", "sales"], "accounting", "ro", {"accounting"})  # admin ro: fine
    with pytest.raises(tc.ContourError):
        tc._check_binding(["company"], "sales", "rw", set())
    tc._check_binding(["company"], "sales", "ro", set())  # ro to anyone: fine
    with pytest.raises(tc.ContourError):
        tc._check_not_skills_dir(["skills"])


# ---------------------------------------------------------------------------
# references/model.md — conceptual rationale doc, unchanged by this
# rewrite, still expected to exist and still relevant.
# ---------------------------------------------------------------------------


def test_model_reference_still_present():
    model_md = SKILL_DIR / "references" / "model.md"
    assert model_md.is_file()
    assert "Монтирование и есть модель прав" in model_md.read_text(encoding="utf-8")
