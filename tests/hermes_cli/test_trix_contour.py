"""Tests for hermes_cli/trix_contour.py — the file-contour security module.

This is the hardened replacement for the prototype
``optional-skills/.../trix-file-contour/scripts/contour.py``. The threat
model: the request file consumed by :func:`hermes_cli.trix_contour.run_executor`
is written by an LLM agent (profile ``system_admin``) that we must assume is
prompt-injected. Every test below proves a REFUSAL against real filesystem
state (real symlinks, real profile directories, real git repos, the real
shipped ``assets/config/trix-config.yaml``) — behavior contracts, not mocks.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from hermes_cli import trix_contour as tc

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_FIXTURE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


def _shipped_terminal_extra_lines() -> str:
    """YAML lines for the three template-compared keys (`docker_extra_args`,
    `docker_forward_env`, `credential_files`), copied from whatever
    ``assets/config/trix-config.yaml`` CURRENTLY ships.

    ``_check_config_not_tampered`` compares a profile's terminal against
    the real shipped template (see ``hermes_cli/trix_contour.py`` —
    presence-based tamper detection broke on the template's own non-empty
    ``docker_extra_args``; the fix compares VALUES). Test fixtures must
    therefore carry the same values the real template ships, not an
    invented/empty baseline — behavior contract, not a pinned snapshot: if
    the shipped port range ever changes, this helper picks up the new
    value automatically instead of the test suite silently drifting out of
    sync with production.
    """
    if not REAL_FIXTURE.is_file():
        return ""
    tpl = yaml.safe_load(REAL_FIXTURE.read_text(encoding="utf-8")) or {}
    terminal = tpl.get("terminal") or {}
    lines = []
    for key in tc._TEMPLATE_COMPARED_TERMINAL_KEYS:
        if key in terminal:
            lines.append(f"  {key}: {json.dumps(terminal[key], ensure_ascii=False)}")
    return "\n".join(lines)


MINIMAL_CONFIG = f"""\
# конфиг профиля — комментарий для проверки сохранения
model:
  default: gpt-5

terminal:
  backend: docker
  docker_volumes: []
{_shipped_terminal_extra_lines()}

skills:
  external_dirs: []
"""


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    return home


def _make_profile(hermes_home: Path, name: str, config_text: str = MINIMAL_CONFIG) -> Path:
    d = hermes_home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(config_text, encoding="utf-8")
    return d


def _set_admin(hermes_home: Path, names) -> None:
    d = hermes_home / "trix_contour"
    d.mkdir(parents=True, exist_ok=True)
    (d / "admin_profiles.json").write_text(
        json.dumps({"admin_profiles": sorted(names)}), encoding="utf-8"
    )


def _profile_cfg(hermes_home: Path, name: str) -> Path:
    if name == "default":
        return hermes_home / "config.yaml"
    return hermes_home / "profiles" / name / "config.yaml"


# ---------------------------------------------------------------------------
# 1. root cannot be influenced by the request in any way
# ---------------------------------------------------------------------------


def test_request_root_key_is_ignored(hermes_home, tmp_path):
    """`root` isn't part of the grammar at all — a request that includes one
    doesn't error (unknown KEYS are not the same as unknown OPS) but it is
    never read: the resolved root stays whatever the caller passed in."""
    real_root = tmp_path / "srv" / "trix"
    evil_root = tmp_path / "evil"
    request = {
        "when": "now",
        "root": str(evil_root),  # not part of the grammar — must be ignored
        "ops": [{"op": "create_folder", "folder": "company"}],
    }
    planned, when = tc.validate_request(request, root=real_root)
    result = tc.apply_planned(planned, root=real_root)
    assert result.ops[0].ok
    assert (real_root / "company").is_dir()
    assert not evil_root.exists()


def test_public_entry_points_share_the_same_default_root():
    """Behavior contract, not a constant snapshot: every public entry point
    that defaults its `root` parameter must default it to `tc.DEFAULT_ROOT`
    — the module's single official constant for it (module docstring,
    point 1: 'root' never comes from the request, only from this one named
    default). If one of these silently drifted to a hardcoded literal
    instead of the shared constant, a command run without `--root` would
    operate on a DIFFERENT company directory than every other command —
    exactly the kind of split-brain this module exists to prevent."""
    import inspect

    for fn in (tc.validate_request, tc.run_executor, tc.status, tc.check):
        sig = inspect.signature(fn)
        assert "root" in sig.parameters, fn.__name__
        assert sig.parameters["root"].default is tc.DEFAULT_ROOT, fn.__name__


# ---------------------------------------------------------------------------
# 2. path syntax: traversal, absolute, .., over-deep, bad charset, over-long
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("folder", [
    "../etc",
    "/etc/passwd",
    "a/../../b",
    "a//b",
    "/company",
    "a/b/c/d",  # depth 4 > max 3
    "a.b",       # dot not allowed
    "a b",       # space not allowed
    "a$b",
    "",
    "company/",
    "/",
])
def test_bad_folder_paths_rejected(tmp_path, folder):
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": folder}]}
    with pytest.raises(tc.ContourError):
        tc.validate_request(request, root=tmp_path / "srv")


def test_over_long_segment_rejected(tmp_path):
    folder = "a" * 65
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": folder}]}
    with pytest.raises(tc.ContourError):
        tc.validate_request(request, root=tmp_path / "srv")


def test_cyrillic_segments_accepted(tmp_path):
    """Rule 3 explicitly requires Cyrillic to work — the company is Russian-speaking."""
    root = tmp_path / "srv"
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": "company/регламенты"}]}
    planned, _ = tc.validate_request(request, root=root)
    result = tc.apply_planned(planned, root)
    assert result.ops[0].ok
    assert (root / "company" / "регламенты").is_dir()


def test_max_depth_three_is_allowed(tmp_path):
    root = tmp_path / "srv"
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": "a/b/c"}]}
    planned, _ = tc.validate_request(request, root=root)
    assert planned[0].rel_parts == ("a", "b", "c")


# ---------------------------------------------------------------------------
# 2b. homoglyph segments (item 9): a segment must be NFKC-stable and must
# not mix scripts within itself — a Cyrillic "company" and a fullwidth
# "COMPANY" must be visually distinguishable from the real thing.
# ---------------------------------------------------------------------------


def test_fullwidth_lookalike_segment_refused(tmp_path):
    """Fullwidth Latin letters are NOT NFKC-stable — they normalize to
    plain ASCII, so a segment using them must be refused."""
    root = tmp_path / "srv"
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": "ＣＯＭＰＡＮＹ"}]}
    with pytest.raises(tc.ContourError, match="NFKC"):
        tc.validate_request(request, root=root)


def test_cyrillic_latin_mixed_script_segment_refused(tmp_path):
    """A Cyrillic "с" (U+0441) glued onto a Latin "ompany" is NFKC-stable
    (NFKC doesn't unify different scripts) but visually indistinguishable
    from "company" — refused for mixing scripts within one segment."""
    root = tmp_path / "srv"
    mixed = "сompany"  # Cyrillicес + Latin "ompany"
    assert mixed != "company"
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": mixed}]}
    with pytest.raises(tc.ContourError, match="алфавит"):
        tc.validate_request(request, root=root)


def test_pure_cyrillic_segment_with_digits_still_accepted(tmp_path):
    """Digits are script-neutral and must not trip the mixed-script check —
    a real department name like "отдел1" must keep working."""
    root = tmp_path / "srv"
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": "отдел1"}]}
    planned, _ = tc.validate_request(request, root=root)
    assert planned[0].rel_parts == ("отдел1",)


# ---------------------------------------------------------------------------
# 3. symlinks at every position are refused
# ---------------------------------------------------------------------------


def test_root_itself_symlink_refused(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    root = tmp_path / "srv_link"
    root.symlink_to(real_dir)
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": "company"}]}
    with pytest.raises(tc.ContourError, match="символическая ссылка"):
        tc.validate_request(request, root=root)


def test_mid_path_component_symlink_refused(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "dept").symlink_to(elsewhere)
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": "dept/evil"}]}
    with pytest.raises(tc.ContourError, match="символическая ссылка"):
        tc.validate_request(request, root=root)


def test_final_folder_symlink_refused(tmp_path):
    root = tmp_path / "srv"
    (root / "company").mkdir(parents=True)
    (root / "dept").mkdir()
    (root / "dept" / "sales").symlink_to(root / "company")
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": "dept/sales"}]}
    with pytest.raises(tc.ContourError, match="символическая ссылка"):
        tc.validate_request(request, root=root)


def test_symlinked_file_inside_published_skill_refused(hermes_home, tmp_path):
    src = hermes_home / "profiles" / "sales" / "skills" / "myskill"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# myskill\n", encoding="utf-8")
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    (src / "linked.txt").symlink_to(target)
    _make_profile(hermes_home, "sales")

    request = {"when": "now", "ops": [{"op": "publish_skill", "from_profile": "sales", "skill": "myskill"}]}
    with pytest.raises(tc.ContourError, match="символическая ссылка"):
        tc.validate_request(request, root=tmp_path / "srv")


# ---------------------------------------------------------------------------
# 4. dept/company binding
# ---------------------------------------------------------------------------


def test_dept_folder_cannot_be_granted_rw_to_another_department(hermes_home, tmp_path):
    _make_profile(hermes_home, "sales")
    _make_profile(hermes_home, "accounting")
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "accounting", "folder": "dept/sales", "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError, match="dept/sales"):
        tc.validate_request(request, root=tmp_path / "srv")


def test_dept_folder_rw_to_owner_allowed(hermes_home, tmp_path):
    _make_profile(hermes_home, "sales")
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    planned, _ = tc.validate_request(request, root=tmp_path / "srv")
    assert planned[0].container_path == "/dept"


def test_dept_folder_ro_requires_admin(hermes_home, tmp_path):
    _make_profile(hermes_home, "sales")
    _make_profile(hermes_home, "accounting")
    _set_admin(hermes_home, set())  # no admins configured
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "accounting", "folder": "dept/sales", "mode": "ro"},
    ]}
    with pytest.raises(tc.ContourError, match="администратору"):
        tc.validate_request(request, root=tmp_path / "srv")


def test_dept_folder_ro_allowed_for_configured_admin(hermes_home, tmp_path):
    _make_profile(hermes_home, "sales")
    _set_admin(hermes_home, {"default"})
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "default", "folder": "dept/sales", "mode": "ro"},
    ]}
    planned, _ = tc.validate_request(request, root=tmp_path / "srv")
    # Full-path derivation (item 8): an admin's read-only view of someone
    # else's department is keyed by the WHOLE path, not just the last
    # segment — so it can never collide with a different department's view
    # or with the admin's own "/dept" (see test below).
    assert planned[0].container_path == "/dept/sales"


def test_company_cannot_be_granted_rw_to_non_admin(hermes_home, tmp_path):
    _make_profile(hermes_home, "sales")
    _set_admin(hermes_home, set())
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "company", "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError, match="company"):
        tc.validate_request(request, root=tmp_path / "srv")


def test_company_ro_allowed_to_anyone(hermes_home, tmp_path):
    _make_profile(hermes_home, "sales")
    _set_admin(hermes_home, set())
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "company", "mode": "ro"},
    ]}
    planned, _ = tc.validate_request(request, root=tmp_path / "srv")
    assert planned[0].container_path == "/company"


def test_company_rw_allowed_to_admin(hermes_home, tmp_path):
    _make_profile(hermes_home, "sales")
    _set_admin(hermes_home, {"sales"})
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "company", "mode": "rw"},
    ]}
    planned, _ = tc.validate_request(request, root=tmp_path / "srv")
    assert planned[0].container_path == "/company"


def test_company_subfolder_binding_also_enforced(hermes_home, tmp_path):
    """The narrowed grammar (item 1) refuses `company/<sub>` outright — it's
    not one of the two grantable shapes at all, regardless of mode or
    admin status."""
    _make_profile(hermes_home, "sales")
    _set_admin(hermes_home, {"sales"})  # even as admin — shape is refused
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "company/regulations", "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError, match="company"):
        tc.validate_request(request, root=tmp_path / "srv")


# ---------------------------------------------------------------------------
# 4b. narrowed grammar (item 1 of the review): grant/revoke accept EXACTLY
# two folder shapes — `company` and `dept/<existing profile>` — everything
# else is refused, including the deleted third "shared" bucket, bare
# `dept`, and depth-3 paths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("folder", [
    "dept",                  # bare dept — not one of the two shapes
    "templates",             # the deleted "anything else -> /shared/*" bucket
    "dept/sales/raw",        # depth-3 — was the confirmed cross-department repro
    "company/sub",           # company with a subfolder
])
def test_grant_refuses_everything_outside_the_two_shapes(hermes_home, tmp_path, folder):
    _make_profile(hermes_home, "sales")
    _set_admin(hermes_home, {"sales"})  # admin status must not matter — shape does
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": folder, "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError):
        tc.validate_request(request, root=tmp_path / "srv")


def test_grant_dept_whole_tree_confirmed_repro_refused(hermes_home, tmp_path):
    """Confirmed repro from the review: `grant folder="dept" mode="rw"` (the
    whole dept tree) must be refused, not accepted."""
    _make_profile(hermes_home, "sales")
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept", "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError):
        tc.validate_request(request, root=tmp_path / "srv")


def test_grant_cross_department_depth_three_confirmed_repro_refused(hermes_home, tmp_path):
    """Confirmed repro from the review: `grant folder="dept/sales/raw"
    mode="rw" profile="marketing"` (cross-department write at depth 3) must
    be refused."""
    _make_profile(hermes_home, "sales")
    _make_profile(hermes_home, "marketing")
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "marketing", "folder": "dept/sales/raw", "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError):
        tc.validate_request(request, root=tmp_path / "srv")


def test_grant_dept_owner_segment_must_be_an_existing_profile(hermes_home, tmp_path):
    """`dept/<X>` is only grantable when `<X>` is an existing profile — a
    made-up department name is refused even for an admin's ro request."""
    _make_profile(hermes_home, "sales")
    _set_admin(hermes_home, {"sales"})
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/ghostdept", "mode": "ro"},
    ]}
    with pytest.raises(tc.ContourError):
        tc.validate_request(request, root=tmp_path / "srv")


def test_two_grants_colliding_on_the_same_mount_point_refuse_whole_request(hermes_home, tmp_path):
    """Item 8: two ops in the same request that would resolve to the same
    container target for the same profile must refuse the whole request,
    not silently let the second overwrite the first while both still
    report 'ok'."""
    _make_profile(hermes_home, "sales")
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "company", "mode": "ro"},
        {"op": "grant", "profile": "sales", "folder": "company", "mode": "rw"},
    ]}
    _set_admin(hermes_home, {"sales"})
    with pytest.raises(tc.ContourError, match="точку монтирования"):
        tc.validate_request(request, root=tmp_path / "srv")


# ---------------------------------------------------------------------------
# 5. shared skills directory is never grantable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_kind", ["grant", "revoke"])
def test_skills_dir_cannot_be_granted_or_revoked(hermes_home, tmp_path, op_kind):
    _make_profile(hermes_home, "sales")
    op = {"op": op_kind, "profile": "sales", "folder": "skills", "mode": "ro"}
    if op_kind == "revoke":
        op.pop("mode")
    request = {"when": "now", "ops": [op]}
    with pytest.raises(tc.ContourError, match="навыков"):
        tc.validate_request(request, root=tmp_path / "srv")


def test_skills_subpath_also_refused(hermes_home, tmp_path):
    _make_profile(hermes_home, "sales")
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "skills/foo", "mode": "ro"},
    ]}
    with pytest.raises(tc.ContourError, match="навыков"):
        tc.validate_request(request, root=tmp_path / "srv")


def test_create_folder_inside_skills_dir_also_refused(hermes_home, tmp_path):
    """Item: `_plan_create_folder` was the one writing op that skipped
    `_check_not_skills_dir` — grant/revoke always had it. An agent could
    create arbitrary directories INSIDE the shared skills folder even
    though spec 21 §9 bans writing there in any form."""
    request = {"when": "now", "ops": [
        {"op": "create_folder", "folder": "skills/backdoor"},
    ]}
    with pytest.raises(tc.ContourError, match="навыков"):
        tc.validate_request(request, root=tmp_path / "srv")

    request2 = {"when": "now", "ops": [{"op": "create_folder", "folder": "skills"}]}
    with pytest.raises(tc.ContourError, match="навыков"):
        tc.validate_request(request2, root=tmp_path / "srv")


# ---------------------------------------------------------------------------
# 5b. revoke validates the same whitelist/binding as grant (item 4 of the
# review: `_plan_revoke` used to skip binding, mount-safety, and admin
# checks entirely — an agent could strip any profile's mounts).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("folder", ["dept", "templates", "dept/sales/raw", "company/sub"])
def test_revoke_refuses_everything_outside_the_two_shapes(hermes_home, tmp_path, folder):
    _make_profile(hermes_home, "sales")
    request = {"when": "now", "ops": [{"op": "revoke", "profile": "sales", "folder": folder}]}
    with pytest.raises(tc.ContourError):
        tc.validate_request(request, root=tmp_path / "srv")


def test_revoke_refused_for_profile_that_could_never_have_held_the_folder(hermes_home, tmp_path):
    """The confirmed escalation: an unrelated profile (neither the dept's
    owner nor an admin) could previously revoke ANY other profile's mount.
    Now it's refused — this pair could never have legitimately held
    dept/marketing in either mode."""
    _make_profile(hermes_home, "sales")
    _make_profile(hermes_home, "marketing")
    _set_admin(hermes_home, set())
    request = {"when": "now", "ops": [
        {"op": "revoke", "profile": "sales", "folder": "dept/marketing"},
    ]}
    with pytest.raises(tc.ContourError):
        tc.validate_request(request, root=tmp_path / "srv")


def test_revoke_allowed_for_owner_of_own_department(hermes_home, tmp_path):
    _make_profile(hermes_home, "sales")
    request = {"when": "now", "ops": [{"op": "revoke", "profile": "sales", "folder": "dept/sales"}]}
    planned, _ = tc.validate_request(request, root=tmp_path / "srv")
    assert planned[0].container_path == "/dept"


def test_revoke_allowed_for_admin_of_someone_elses_department_ro_view(hermes_home, tmp_path):
    _make_profile(hermes_home, "sales")
    _set_admin(hermes_home, {"default"})
    request = {"when": "now", "ops": [{"op": "revoke", "profile": "default", "folder": "dept/sales"}]}
    planned, _ = tc.validate_request(request, root=tmp_path / "srv")
    assert planned[0].container_path == "/dept/sales"


def test_revoke_company_allowed_for_any_existing_profile(hermes_home, tmp_path):
    """company ro is open to anyone, so revoke on `company` never needs
    admin status."""
    _make_profile(hermes_home, "sales")
    _set_admin(hermes_home, set())
    request = {"when": "now", "ops": [{"op": "revoke", "profile": "sales", "folder": "company"}]}
    planned, _ = tc.validate_request(request, root=tmp_path / "srv")
    assert planned[0].container_path == "/company"


def test_revoke_only_writes_skills_external_dirs_when_a_grant_also_ran(hermes_home, tmp_path):
    """Item 4's second bug: revoke used to unconditionally rewrite
    skills.external_dirs — a way to force the shared-skills mount back onto
    a profile whose administrator had removed it. A revoke-only apply must
    leave skills.external_dirs completely untouched."""
    root = tmp_path / "srv" / "trix"
    text = MINIMAL_CONFIG.replace(
        "  docker_volumes: []\n",
        f"  docker_volumes:\n    - {root}/dept/sales:/dept:rw\n",
    )
    _make_profile(hermes_home, "sales", text)
    request = {"when": "now", "ops": [{"op": "revoke", "profile": "sales", "folder": "dept/sales"}]}
    planned, _ = tc.validate_request(request, root=root)
    result = tc.apply_planned(planned, root)
    assert result.ops[0].ok, result.ops[0].detail

    new_text = _profile_cfg(hermes_home, "sales").read_text(encoding="utf-8")
    before, after = yaml.safe_load(text), yaml.safe_load(new_text)
    assert _diff_keys(before, after) == {("terminal", "docker_volumes")}
    assert after["terminal"]["docker_volumes"] == []
    # untouched, not merely "still empty" — the key must not have been
    # rewritten to an equivalent value either.
    assert after.get("skills", {}).get("external_dirs") == before.get("skills", {}).get("external_dirs")


# ---------------------------------------------------------------------------
# 6. unknown op refuses the whole request, zero mutations
# ---------------------------------------------------------------------------


def test_unknown_op_refuses_whole_request(hermes_home, tmp_path):
    root = tmp_path / "srv"
    request = {"when": "now", "ops": [
        {"op": "create_folder", "folder": "company"},
        {"op": "delete_root", "path": "/"},
    ]}
    with pytest.raises(tc.ContourError, match="неизвестная операция"):
        tc.validate_request(request, root=root)
    assert not root.exists()


def test_ops_over_the_cap_refuse_the_whole_request(hermes_home, tmp_path):
    """A request with an absurd number of ops is a cheap way to keep the
    minutely executor busy (each op is at least a stat/mkdir, plus a
    tamper check per touched profile) — must be refused outright, before a
    single mutation, rather than silently accepted."""
    root = tmp_path / "srv"
    ops = [{"op": "create_folder", "folder": f"company/note{i}"} for i in range(tc.MAX_OPS_PER_REQUEST + 1)]
    request = {"when": "now", "ops": ops}
    with pytest.raises(tc.ContourError, match=str(tc.MAX_OPS_PER_REQUEST)):
        tc.validate_request(request, root=root)
    assert not root.exists()


def test_ops_at_the_cap_are_accepted(hermes_home, tmp_path):
    root = tmp_path / "srv"
    ops = [{"op": "create_folder", "folder": f"company/note{i}"} for i in range(tc.MAX_OPS_PER_REQUEST)]
    request = {"when": "now", "ops": ops}
    planned, _ = tc.validate_request(request, root=root)  # must NOT raise
    assert len(planned) == tc.MAX_OPS_PER_REQUEST


def test_profile_must_exist(hermes_home, tmp_path):
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "ghost", "folder": "dept/ghost", "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError, match="не существует"):
        tc.validate_request(request, root=tmp_path / "srv")


def test_default_is_a_valid_profile_name(hermes_home, tmp_path):
    _set_admin(hermes_home, {"default"})
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "default", "folder": "company", "mode": "rw"},
    ]}
    planned, _ = tc.validate_request(request, root=tmp_path / "srv")
    assert planned[0].profile == "default"


# ---------------------------------------------------------------------------
# 7. derived mount points can never be /workspace, /root, or /
# ---------------------------------------------------------------------------


def test_check_derived_mount_safe_rejects_reserved():
    for bad in ("/workspace", "/workspace/sub", "/root", "/root/.trix", "/"):
        with pytest.raises(tc.ContourError):
            tc._check_derived_mount_safe(bad)


def test_derive_container_path_never_reserved(hermes_home, tmp_path):
    """Item 10 rewrite: the old version called ``_derive_container_path``
    directly with hand-picked ``rel_parts`` (including shapes like
    ``["company", "regs"]`` and ``["a", "b", "c"]`` that ``_plan_grant``
    would refuse before ever reaching derivation) — a function that
    structurally cannot return a reserved path can never fail that
    assertion, so the test could not fail.

    Rewritten as an invariant driven through the real ``_plan_grant``
    pipeline: for EVERY shape the grammar actually accepts, the derived
    target is never reserved, and two different accepted grants to the
    same profile never collide on the same mount point (item 8)."""
    _make_profile(hermes_home, "sales")
    _make_profile(hermes_home, "marketing")
    root = tmp_path / "srv"

    grants = [
        # (profile, folder, mode)
        ("sales", "company", "ro"),
        ("sales", "dept/sales", "rw"),
        ("default", "company", "rw"),
        ("default", "dept/sales", "ro"),
        ("default", "dept/marketing", "ro"),
    ]
    seen_by_profile: dict = {}
    for profile, folder, mode in grants:
        op = {"op": "grant", "profile": profile, "folder": folder, "mode": mode}
        planned = tc._plan_grant(0, op, root, {"default"})
        at = planned.container_path
        tc._check_derived_mount_safe(at)  # must not raise
        assert not at.startswith("/workspace")
        assert not at.startswith("/root")
        assert at != "/"
        key = (profile, at)
        assert key not in seen_by_profile, (
            f"{profile}: {folder!r} and {seen_by_profile.get(key)!r} both resolve to {at!r}"
        )
        seen_by_profile[key] = folder


# ---------------------------------------------------------------------------
# 8. tampered config detection
# ---------------------------------------------------------------------------


def test_config_with_docker_extra_args_extra_entry_refused_as_tampered(hermes_home, tmp_path):
    """An extra `docker run` argument beyond what we shipped (e.g. someone
    hand-added `--privileged`) must refuse, and the failure text must name
    the key and show BOTH values so a human can see the diff in one line."""
    shipped = yaml.safe_load(REAL_FIXTURE.read_text(encoding="utf-8"))["terminal"]["docker_extra_args"]
    tampered = [*shipped, "--privileged"]
    text = MINIMAL_CONFIG.replace(
        f"  docker_extra_args: {json.dumps(shipped, ensure_ascii=False)}",
        f"  docker_extra_args: {json.dumps(tampered, ensure_ascii=False)}",
    )
    assert "--privileged" in text, "fixture replace didn't take — test is broken"
    _make_profile(hermes_home, "sales", text)
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError) as excinfo:
        tc.validate_request(request, root=tmp_path / "srv")
    msg = str(excinfo.value)
    assert "docker_extra_args" in msg
    assert repr(shipped) in msg  # the shipped value is shown
    assert repr(tampered) in msg  # the found (tampered) value is shown
    # file untouched
    assert _profile_cfg(hermes_home, "sales").read_text(encoding="utf-8") == text


def test_config_with_docker_extra_args_different_ports_refused_as_tampered(hermes_home, tmp_path):
    """Proves we compare VALUES, not just shapes/lengths: a same-length,
    same-flag list with different port numbers must still refuse."""
    shipped = yaml.safe_load(REAL_FIXTURE.read_text(encoding="utf-8"))["terminal"]["docker_extra_args"]
    retargeted = ["-p", "9000-9009:9000-9009"]
    assert len(retargeted) == len(shipped) and retargeted != shipped
    text = MINIMAL_CONFIG.replace(
        f"  docker_extra_args: {json.dumps(shipped, ensure_ascii=False)}",
        f"  docker_extra_args: {json.dumps(retargeted, ensure_ascii=False)}",
    )
    _make_profile(hermes_home, "sales", text)
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError) as excinfo:
        tc.validate_request(request, root=tmp_path / "srv")
    msg = str(excinfo.value)
    assert "docker_extra_args" in msg
    assert repr(shipped) in msg
    assert repr(retargeted) in msg


def test_config_with_non_docker_backend_refused(hermes_home, tmp_path):
    text = MINIMAL_CONFIG.replace("  backend: docker\n", "  backend: local\n")
    _make_profile(hermes_home, "sales", text)
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError, match="backend"):
        tc.validate_request(request, root=tmp_path / "srv")


def test_missing_terminal_block_entirely_is_treated_as_tampered(hermes_home, tmp_path):
    """Item 7: the old check returned early (fail OPEN) when `terminal` was
    not a dict at all — the LEAST trustworthy shape (someone cut the whole
    block) skipped every check, including `backend`. A config missing
    `terminal:` entirely must now be evaluated against the same rules as an
    empty dict.

    Note: with the real shipped template carrying a non-empty
    `docker_extra_args`, this particular config refuses on THAT key
    mismatch before the check ever reaches `backend` — see
    test_missing_backend_key_alone_is_treated_as_tampered below for a
    fixture that isolates the `backend` branch specifically."""
    if not REAL_FIXTURE.is_file():
        pytest.skip("assets/config/trix-config.yaml not present in this checkout")
    text = "model:\n  default: gpt-5\n\nskills:\n  external_dirs: []\n"
    assert "terminal" not in yaml.safe_load(text)
    _make_profile(hermes_home, "sales", text)
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError):
        tc.validate_request(request, root=tmp_path / "srv")


def test_missing_backend_key_alone_is_treated_as_tampered(hermes_home, tmp_path):
    """The specific branch the test above never reaches: a `terminal:`
    block that matches the shipped template on every other compared key,
    but simply omits `backend:`. The real runtime default for a missing
    `terminal.backend` is `local`
    (`DEFAULT_CONFIG["terminal"]["backend"]` in
    `hermes_cli/config_defaults.py`), NOT `docker` — the old code defaulted
    to `docker` here, so a config with the line silently deleted (or never
    written) would pass this check as "docker" and get real mounts written
    to `docker_volumes`, while the profile actually executes on the host
    where nothing mounts anything. The default here must match the
    runtime's real default, so a missing key resolves the same way the
    profile itself would resolve it — and gets refused as tampered."""
    text = MINIMAL_CONFIG.replace("  backend: docker\n", "")
    assert "backend" not in (yaml.safe_load(text).get("terminal") or {})
    _make_profile(hermes_home, "sales", text)
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError, match="backend"):
        tc.validate_request(request, root=tmp_path / "srv")


def test_credential_files_empty_list_matches_template_that_omits_the_key(hermes_home, tmp_path):
    """Item 7: `None` and `[]` must compare equal — the real shipped
    template omits `credential_files` entirely (`None`), and a profile
    that explicitly writes `credential_files: []` means the same thing and
    must not be flagged as tampered."""
    if not REAL_FIXTURE.is_file():
        pytest.skip("assets/config/trix-config.yaml not present in this checkout")
    tpl = yaml.safe_load(REAL_FIXTURE.read_text(encoding="utf-8"))
    assert "credential_files" not in (tpl.get("terminal") or {}), "fixture assumption changed — update this test"
    text = REAL_FIXTURE.read_text(encoding="utf-8").replace(
        "terminal:\n", "terminal:\n  credential_files: []\n", 1
    )
    assert "credential_files: []" in text, "fixture replace didn't take — test is broken"
    _make_profile(hermes_home, "sales", text)
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    planned, _ = tc.validate_request(request, root=tmp_path / "srv")  # must NOT raise
    assert planned[0].profile == "sales"


def test_default_shipped_config_is_accepted_as_normal(hermes_home, tmp_path):
    """This is the regression test for the tamper-rule bug: the FIRST
    version of ``_check_config_not_tampered`` refused whenever
    ``terminal.docker_extra_args``/``docker_forward_env`` were merely
    PRESENT — and the real shipped ``assets/config/trix-config.yaml``
    ships a non-empty ``docker_extra_args`` (the port-forwarding-for-demos
    feature) and an empty ``docker_forward_env`` on every fresh machine.
    That refused literally every freshly-installed profile on its first
    `grant`. The fix compares against our own curated template instead of
    against emptiness: the unmodified shipped config IS the normal case
    and must be accepted."""
    if not REAL_FIXTURE.is_file():
        pytest.skip("assets/config/trix-config.yaml not present in this checkout")
    text = REAL_FIXTURE.read_text(encoding="utf-8")
    _make_profile(hermes_home, "sales", text)
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    planned, _ = tc.validate_request(request, root=tmp_path / "srv")  # must NOT raise
    assert planned[0].profile == "sales"


def test_tampered_config_check_fails_closed_when_template_unresolvable(hermes_home, tmp_path, monkeypatch):
    """If our curated template can't be found (a stripped install), the
    check must refuse rather than silently skip — "can't compare" is not
    "assume fine" for privileged code."""
    monkeypatch.setattr(
        "hermes_cli.config_template.resolve_trix_config_template_only", lambda install_dir: None
    )
    _make_profile(hermes_home, "sales")
    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    with pytest.raises(tc.ContourError, match="шаблон"):
        tc.validate_request(request, root=tmp_path / "srv")


# ---------------------------------------------------------------------------
# 9. rewrite_config precision against the real shipped client config
# ---------------------------------------------------------------------------


def _diff_keys(before: dict, after: dict) -> set:
    changed = set()
    for top in set(before) | set(after):
        b, a = before.get(top), after.get(top)
        if isinstance(b, dict) and isinstance(a, dict):
            for sub in set(b) | set(a):
                if b.get(sub) != a.get(sub):
                    changed.add((top, sub))
        elif b != a:
            changed.add((top, None))
    return changed


def test_only_two_keys_change_in_real_fixture_comments_survive(hermes_home, tmp_path):
    if not REAL_FIXTURE.is_file():
        pytest.skip("assets/config/trix-config.yaml not present in this checkout")
    root = tmp_path / "srv" / "trix"
    text = REAL_FIXTURE.read_text(encoding="utf-8")  # unmodified — that's the whole point
    _make_profile(hermes_home, "sales", text)
    _set_admin(hermes_home, set())

    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    planned, _ = tc.validate_request(request, root=root)
    result = tc.apply_planned(planned, root)
    assert result.ops[0].ok, result.ops[0].detail

    new_text = _profile_cfg(hermes_home, "sales").read_text(encoding="utf-8")
    before, after = yaml.safe_load(text), yaml.safe_load(new_text)
    assert _diff_keys(before, after) == {("terminal", "docker_volumes"), ("skills", "external_dirs")}
    assert after["terminal"]["docker_volumes"] == [f"{root}/dept/sales:/dept:rw"]
    assert after["skills"]["external_dirs"] == [str(root / "skills")]

    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            assert line in new_text.splitlines(), line


def test_grant_refuses_to_evict_a_foreign_mount_at_the_same_point(hermes_home, tmp_path):
    """`docker_volumes` was not part of the tamper comparison at all —
    `_check_config_not_tampered` only compares
    `docker_extra_args`/`docker_forward_env`/`credential_files`. An
    operator who hand-placed their own mount on `/dept` (a host path NOT
    under contour's `root`) lost it silently on the first `grant`/`revoke`
    that happened to target the same container_path, and `apply` reported
    success. Must now detect the foreign mount and refuse instead."""
    root = tmp_path / "srv" / "trix"
    foreign_host = tmp_path / "operator-placed-elsewhere"
    foreign_host.mkdir()
    text = MINIMAL_CONFIG.replace(
        "  docker_volumes: []\n",
        f"  docker_volumes:\n    - {foreign_host}:/dept:ro\n",
    )
    _make_profile(hermes_home, "sales", text)

    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    planned, _ = tc.validate_request(request, root=root)
    result = tc.apply_planned(planned, root)
    assert not result.ops[0].ok
    assert "постор" in result.ops[0].detail or "оператор" in result.ops[0].detail

    # Config untouched — the foreign mount is still exactly where it was.
    after_text = _profile_cfg(hermes_home, "sales").read_text(encoding="utf-8")
    assert after_text == text


def test_grant_on_a_different_point_leaves_foreign_mount_untouched(hermes_home, tmp_path):
    """Sanity check for the test above: a foreign mount at a DIFFERENT
    container_path must not block an unrelated grant."""
    root = tmp_path / "srv" / "trix"
    foreign_host = tmp_path / "operator-placed-elsewhere"
    foreign_host.mkdir()
    text = MINIMAL_CONFIG.replace(
        "  docker_volumes: []\n",
        f"  docker_volumes:\n    - {foreign_host}:/some/other/path:ro\n",
    )
    _make_profile(hermes_home, "sales", text)

    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    planned, _ = tc.validate_request(request, root=root)
    result = tc.apply_planned(planned, root)
    assert result.ops[0].ok, result.ops[0].detail
    after = yaml.safe_load(_profile_cfg(hermes_home, "sales").read_text(encoding="utf-8"))
    volumes = after["terminal"]["docker_volumes"]
    assert f"{foreign_host}:/some/other/path:ro" in volumes
    assert f"{root}/dept/sales:/dept:rw" in volumes


# ---------------------------------------------------------------------------
# Migration escape hatch: revoke_literal_mount for grants made under the
# earlier, wider grammar.
# ---------------------------------------------------------------------------


def test_revoke_literal_mount_removes_a_shape_the_closed_grammar_cannot_name(hermes_home, tmp_path):
    """A grant made under the retired wide grammar (e.g. `dept/sales/raw`,
    or the old `templates` bucket) sits in `docker_volumes` as an ordinary
    volume string. The closed grammar's own `revoke` refuses to even
    LOOK for it (`_validate_grantable_folder` only knows `company` and
    `dept/<X>`), so there was no way to remove it short of hand-editing
    YAML. `revoke_literal_mount` is the documented human-only escape
    hatch — not reachable from `validate_request` / the agent's grammar at
    all."""
    root = tmp_path / "srv" / "trix"
    text = MINIMAL_CONFIG.replace(
        "  docker_volumes: []\n",
        f"  docker_volumes:\n    - {root}/dept/sales/raw:/dept/sales/raw:rw\n",
    )
    _make_profile(hermes_home, "sales", text)

    # Confirm the ordinary agent-facing path really can't touch it.
    request = {"when": "now", "ops": [
        {"op": "revoke", "profile": "sales", "folder": "dept/sales/raw"},
    ]}
    with pytest.raises(tc.ContourError):
        tc.validate_request(request, root=root)

    result = tc.revoke_literal_mount("sales", "/dept/sales/raw")
    assert result["changed"] is True
    after = yaml.safe_load(_profile_cfg(hermes_home, "sales").read_text(encoding="utf-8"))
    assert after["terminal"]["docker_volumes"] == []


def test_revoke_literal_mount_is_a_noop_when_nothing_to_remove(hermes_home, tmp_path):
    _make_profile(hermes_home, "sales")
    result = tc.revoke_literal_mount("sales", "/dept/nonexistent")
    assert result["changed"] is False


def test_revoke_literal_mount_still_refuses_tampered_config(hermes_home, tmp_path):
    text = MINIMAL_CONFIG.replace("  backend: docker\n", "  backend: local\n")
    _make_profile(hermes_home, "sales", text)
    with pytest.raises(tc.ContourError, match="backend"):
        tc.revoke_literal_mount("sales", "/dept")


# ---------------------------------------------------------------------------
# 10. backups exist after a mid-apply failure
# ---------------------------------------------------------------------------


def test_backup_exists_after_mid_apply_failure(hermes_home, tmp_path, monkeypatch):
    root = tmp_path / "srv" / "trix"
    _make_profile(hermes_home, "sales")
    _make_profile(hermes_home, "accounting")

    request = {"when": "now", "ops": [
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
        {"op": "grant", "profile": "accounting", "folder": "dept/accounting", "mode": "rw"},
    ]}
    planned, _ = tc.validate_request(request, root=root)

    accounting_cfg = _profile_cfg(hermes_home, "accounting")
    original_write = tc.atomic_write_text

    def flaky_write(path, *a, **kw):
        # Fail only the FINAL config write for accounting (not its backup
        # write, which targets a different path) — simulating e.g. a full
        # disk hit right when the real file is about to be replaced.
        if Path(path) == accounting_cfg:
            raise OSError("simulated disk-full")
        return original_write(path, *a, **kw)

    monkeypatch.setattr(tc, "atomic_write_text", flaky_write)
    result = tc.apply_planned(planned, root)

    sales_cfg = _profile_cfg(hermes_home, "sales")
    sales_backups = list(sales_cfg.parent.glob("config.yaml.bak-*"))
    accounting_backups = list(accounting_cfg.parent.glob("config.yaml.bak-*"))
    assert sales_backups, "successful profile must have a backup of its pre-image"
    assert accounting_backups, "the failed profile's pre-image must also be preserved"

    # accounting's file itself is untouched (the flaky write never landed)
    assert accounting_cfg.read_text(encoding="utf-8") == MINIMAL_CONFIG

    by_index = {r.index: r for r in result.ops}
    assert by_index[0].ok is True
    assert by_index[1].ok is False
    assert "simulated disk-full" in by_index[1].detail


# ---------------------------------------------------------------------------
# 11. git layer
# ---------------------------------------------------------------------------


def _git_log(root: Path):
    result = subprocess.run(["git", "-C", str(root), "log", "--oneline"], capture_output=True, text=True)
    return result.stdout.strip().splitlines()


def test_first_apply_inits_repo_and_commits(hermes_home, tmp_path):
    root = tmp_path / "srv" / "trix"
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": "company"}]}
    planned, _ = tc.validate_request(request, root=root)
    result = tc.apply_planned(planned, root)
    assert result.commit
    assert (root / ".git").is_dir()
    assert len(_git_log(root)) == 1


def test_second_identical_apply_is_a_noop_commit(hermes_home, tmp_path):
    root = tmp_path / "srv" / "trix"
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": "company"}]}

    planned, _ = tc.validate_request(request, root=root)
    r1 = tc.apply_planned(planned, root)
    assert r1.commit

    planned2, _ = tc.validate_request(request, root=root)
    r2 = tc.apply_planned(planned2, root)
    assert r2.commit is None  # nothing changed — no empty commit created
    assert len(_git_log(root)) == 1


def test_dept_never_enters_git_history(hermes_home, tmp_path):
    root = tmp_path / "srv" / "trix"
    _make_profile(hermes_home, "sales")
    request = {"when": "now", "ops": [
        {"op": "create_folder", "folder": "dept/sales"},
        {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
    ]}
    planned, _ = tc.validate_request(request, root=root)
    result = tc.apply_planned(planned, root)
    assert result.commit

    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, text=True,
    ).stdout
    assert "dept/" not in tracked
    assert (root / "dept" / "sales").is_dir()  # the folder itself still exists on disk


def test_git_user_config_set_on_fresh_machine(hermes_home, tmp_path):
    """A brand-new client machine has no global git config — the first
    commit must not fail with 'Please tell me who you are'."""
    root = tmp_path / "srv" / "trix"
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": "company"}]}
    planned, _ = tc.validate_request(request, root=root)
    result = tc.apply_planned(planned, root)
    assert result.commit
    name = subprocess.run(["git", "-C", str(root), "config", "user.name"], capture_output=True, text=True).stdout.strip()
    assert name == "Trix Contour"


def test_git_not_on_path_refuses_cleanly(hermes_home, tmp_path, monkeypatch):
    root = tmp_path / "srv" / "trix"
    monkeypatch.setattr(tc, "_git_available", lambda: False)
    request = {"when": "now", "ops": [{"op": "create_folder", "folder": "company"}]}
    planned, _ = tc.validate_request(request, root=root)
    with pytest.raises(tc.ContourError, match="git"):
        tc.apply_planned(planned, root)
    assert not root.exists()


def _assert_token_nowhere_but_protected_file(tmp_path: Path, secret_token: str) -> None:
    """Item 10 rewrite: the token must appear in exactly one place on disk —
    its dedicated 0600 file outside root — and nowhere else: not the
    request archive, not the result file, not the audit log, not any
    config file. Shared across all four outcomes below (item 6/10: the
    original test covered only `applied`)."""
    token_file = tc._token_file_path()
    assert token_file.read_text(encoding="utf-8").strip() == secret_token
    assert oct(token_file.stat().st_mode)[-3:] == "600"
    for path in tmp_path.rglob("*"):
        if path.is_file() and path != token_file:
            try:
                content = path.read_bytes()
            except OSError:
                continue
            assert secret_token.encode() not in content, path


def test_token_never_appears_anywhere_on_disk_applied(hermes_home, tmp_path):
    root = tmp_path / "srv" / "trix"
    secret_token = "ghp_SuperSecretTokenValue1234567890"
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps({
        "when": "now",
        "ops": [
            {"op": "create_folder", "folder": "company"},
            {"op": "set_git_remote", "url": "https://example.invalid/org/repo.git", "token": secret_token},
        ],
    }), encoding="utf-8")

    result = tc.run_executor(request_path, root=root)
    assert result["outcome"] == "applied", result
    assert secret_token not in json.dumps(result)
    assert not str(tc._token_file_path()).startswith(str(root))
    _assert_token_nowhere_but_protected_file(tmp_path, secret_token)


def test_token_never_appears_anywhere_on_disk_partial(hermes_home, tmp_path, monkeypatch):
    """Item 10: a request where one op fails at apply-time (I/O) but the
    set_git_remote op succeeds — outcome 'partial'. The token must be
    scrubbed and stored just as reliably as on a full success."""
    root = tmp_path / "srv" / "trix"
    _make_profile(hermes_home, "sales")
    secret_token = "ghp_PartialOutcomeToken0987654321"
    sales_cfg = _profile_cfg(hermes_home, "sales")

    original_write = tc.atomic_write_text

    def flaky_write(path, *a, **kw):
        if Path(path) == sales_cfg:
            raise OSError("simulated disk-full")
        return original_write(path, *a, **kw)

    monkeypatch.setattr(tc, "atomic_write_text", flaky_write)

    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps({
        "when": "now",
        "ops": [
            {"op": "grant", "profile": "sales", "folder": "dept/sales", "mode": "rw"},
            {"op": "set_git_remote", "url": "https://example.invalid/org/repo.git", "token": secret_token},
        ],
    }), encoding="utf-8")

    result = tc.run_executor(request_path, root=root)
    assert result["outcome"] == "partial", result
    assert secret_token not in json.dumps(result)
    _assert_token_nowhere_but_protected_file(tmp_path, secret_token)


def test_token_never_appears_anywhere_on_disk_refused(hermes_home, tmp_path):
    """Item 6/10: a request refused WHOLESALE at validation (unrelated bad
    op) used to archive the request file with the token still in
    cleartext. The token is scrubbed immediately after JSON parses, before
    the refusal is even known."""
    root = tmp_path / "srv" / "trix"
    secret_token = "ghp_RefusedOutcomeTokenAbcdef12345"
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps({
        "when": "now",
        "ops": [
            {"op": "set_git_remote", "url": "https://example.invalid/org/repo.git", "token": secret_token},
            {"op": "wipe_disk"},
        ],
    }), encoding="utf-8")

    result = tc.run_executor(request_path, root=root)
    assert result["outcome"] == "refused", result
    assert secret_token not in json.dumps(result)
    assert not request_path.exists()  # archived (renamed to .processed-*)
    _assert_token_nowhere_but_protected_file(tmp_path, secret_token)


def test_token_scrubbed_by_regex_when_json_does_not_parse_at_all(hermes_home, tmp_path):
    """Item 6: when the request isn't even valid JSON, the token can't be
    extracted structurally (there's no parsed dict) — `run_executor` falls
    back to a regex scrub on the raw text before archiving the (still
    malformed) file. Unlike the other three outcomes, the token here is
    NOT stashed in the protected file — there's no way to know it's a
    legitimate set_git_remote token from broken JSON, only that a
    `"token": "..."` field shape is present."""
    root = tmp_path / "srv" / "trix"
    secret_token = "ghp_MalformedJsonToken1122334455"
    request_path = tmp_path / "request.json"
    # Truncated/invalid JSON — a stray trailing comma — but the token field
    # is still textually present, exactly as an LLM output truncation
    # would leave it.
    request_path.write_text(
        '{"when": "now", "ops": [{"op": "set_git_remote", "url": "https://example.invalid/r.git", '
        f'"token": "{secret_token}"}},]}}',
        encoding="utf-8",
    )

    result = tc.run_executor(request_path, root=root)
    assert result["outcome"] == "refused", result
    assert secret_token not in json.dumps(result)
    assert not request_path.exists()  # archived
    processed = list(tmp_path.glob("request.json.processed-*"))
    assert len(processed) == 1
    assert secret_token not in processed[0].read_text(encoding="utf-8")
    assert tc._TOKEN_SCRUB_PLACEHOLDER in processed[0].read_text(encoding="utf-8")


def test_token_never_appears_anywhere_on_disk_pending(hermes_home, tmp_path):
    """Item 6/10: a `when: night` request outside the night window leaves
    the file at its original path awaiting the window — it used to keep
    the raw token there in the meantime. The token is scrubbed immediately
    on parse regardless of the night-window branch, with the real value
    pre-stored in the protected file so the deferred apply still works."""
    from datetime import datetime

    root = tmp_path / "srv" / "trix"
    secret_token = "ghp_PendingOutcomeToken998877665544"
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps({
        "when": "night",
        "ops": [{"op": "set_git_remote", "url": "https://example.invalid/org/repo.git", "token": secret_token}],
    }), encoding="utf-8")

    noon = datetime(2026, 1, 1, 12, 0, 0)
    result = tc.run_executor(request_path, root=root, now=noon)
    assert result["outcome"] == "pending", result
    assert request_path.exists()  # NOT consumed — still awaiting the window
    assert secret_token not in request_path.read_text(encoding="utf-8")
    assert tc._TOKEN_SCRUB_PLACEHOLDER in request_path.read_text(encoding="utf-8")
    _assert_token_nowhere_but_protected_file(tmp_path, secret_token)

    # And the deferred apply, once night comes, must use the REAL token
    # from the protected file — not the placeholder now sitting in the file.
    three_am = datetime(2026, 1, 1, 3, 0, 0)
    result2 = tc.run_executor(request_path, root=root, now=three_am)
    assert result2["outcome"] == "applied", result2

    # Blocker 2: applying set_git_remote only records a PENDING remote — the
    # real `git remote` is not touched until a human runs approve_remote().
    got = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True)
    assert got.returncode != 0 or not got.stdout.strip()
    pending = tc.read_pending_remote()
    assert pending == {"url": "https://example.invalid/org/repo.git", "requested_at": pending["requested_at"]}

    approved = tc.approve_remote(root)
    assert approved["url"] == "https://example.invalid/org/repo.git"
    got2 = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True)
    assert got2.stdout.strip() == "https://example.invalid/org/repo.git"
    assert tc.read_pending_remote() is None


def test_push_does_not_put_token_in_argv(hermes_home, tmp_path, monkeypatch):
    """Item 6: the token must travel to `git push` via the child process's
    environment, never as a literal argv element — argv is visible to any
    local user via `ps` by default, environment is not."""
    root = tmp_path / "srv" / "trix"
    tc.ensure_repo(root)
    tc.set_git_remote(root, "https://example.invalid/org/repo.git", "ghp_ArgvSafetyToken1234567890")

    captured = {}
    real_run = tc.subprocess.run

    def spy(cmd, *a, **kw):
        if isinstance(cmd, list) and "push" in cmd:
            captured["cmd"] = cmd
            captured["env"] = kw.get("env")
            result = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            return result
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(tc.subprocess, "run", spy)
    tc.push(root)

    assert "cmd" in captured, "push never called subprocess.run with a push command"
    for arg in captured["cmd"]:
        assert "ArgvSafetyToken" not in arg
    env = captured["env"] or {}
    # The token itself is never in plaintext in argv OR env — it travels as
    # a base64-encoded Basic-auth header via GIT_CONFIG_VALUE_0.
    import base64 as _b64

    header_value = env.get("GIT_CONFIG_VALUE_0", "")
    assert header_value.startswith("Authorization: Basic ")
    decoded = _b64.b64decode(header_value.split(" ")[-1]).decode()
    assert "ArgvSafetyToken" in decoded
    assert env.get("GIT_CONFIG_KEY_0") == "http.extraHeader"


def test_malformed_set_git_remote_does_not_corrupt_a_previously_stored_good_token(hermes_home, tmp_path):
    """Blocker 2 (second half): the token used to be persisted to the
    protected store BEFORE validate_request ever ran, structurally
    extracted from the raw request regardless of whether the op's `url`
    was even well-formed. A deliberately malformed follow-up request (bad
    url shape) would overwrite a GOOD, working token with garbage a moment
    before the request got refused wholesale by validate_request — leaving
    the machine with a broken backup credential even though the request
    that caused it was rejected. Storing must require the same url shape
    check `_plan_set_git_remote` itself applies."""
    root = tmp_path / "srv" / "trix"
    good_token = "ghp_GoodWorkingToken1234567890abcd"
    good_request = tmp_path / "good.json"
    good_request.write_text(json.dumps({
        "when": "now",
        "ops": [{"op": "set_git_remote", "url": "https://example.invalid/org/repo.git", "token": good_token}],
    }), encoding="utf-8")
    result = tc.run_executor(good_request, root=root)
    assert result["outcome"] == "applied", result
    assert tc._load_token() == good_token

    # A malformed follow-up: url has embedded credentials (rejected by
    # _HTTPS_URL_RE / _plan_set_git_remote) with a DIFFERENT, garbage token.
    bad_request = tmp_path / "bad.json"
    bad_request.write_text(json.dumps({
        "when": "now",
        "ops": [{"op": "set_git_remote", "url": "https://attacker:pw@example.invalid/repo.git", "token": "garbage_token_value"}],
    }), encoding="utf-8")
    result2 = tc.run_executor(bad_request, root=root)
    assert result2["outcome"] == "refused", result2

    # The good token must still be there — untouched by the refused request.
    assert tc._load_token() == good_token
    assert not bad_request.exists()  # archived (refused requests are consumed)
    processed = list(tmp_path.glob("bad.json.processed-*"))
    assert len(processed) == 1
    assert "garbage_token_value" not in processed[0].read_text(encoding="utf-8")


def test_set_git_remote_url_must_be_https_without_credentials(hermes_home, tmp_path):
    request = {"when": "now", "ops": [
        {"op": "set_git_remote", "url": "https://user:pass@example.invalid/repo.git", "token": "x"},
    ]}
    with pytest.raises(tc.ContourError, match="url"):
        tc.validate_request(request, root=tmp_path / "srv")


def test_push_without_remote_configured_is_a_clear_noop(hermes_home, tmp_path):
    root = tmp_path / "srv" / "trix"
    tc.ensure_repo(root)
    assert "не настроен" in tc.push(root)


# ---------------------------------------------------------------------------
# 12. audit log: outside root, append-only in effect
# ---------------------------------------------------------------------------


def test_audit_log_outside_root_and_append_only(hermes_home, tmp_path):
    root = tmp_path / "srv" / "trix"
    audit_path = tc._audit_log_path()
    assert not str(audit_path).startswith(str(root))

    for i in range(2):
        request_path = tmp_path / f"req{i}.json"
        request_path.write_text(json.dumps({
            "when": "now",
            "ops": [{"op": "create_folder", "folder": f"company/note{i}"}],
        }), encoding="utf-8")
        tc.run_executor(request_path, root=root)

    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        entry = json.loads(line)
        assert {"ts", "sha256", "ops", "outcome", "commit"} <= set(entry)


def test_executor_renames_consumed_request_and_writes_result(hermes_home, tmp_path):
    root = tmp_path / "srv" / "trix"
    request_path = tmp_path / "req.json"
    request_path.write_text(json.dumps({
        "when": "now", "ops": [{"op": "create_folder", "folder": "company"}],
    }), encoding="utf-8")

    tc.run_executor(request_path, root=root)

    assert not request_path.exists()
    processed = list(tmp_path.glob("req.json.processed-*"))
    assert len(processed) == 1
    result_file = tmp_path / "req.json.result.txt"
    assert result_file.is_file()
    assert "applied" in result_file.read_text(encoding="utf-8")


def test_night_request_outside_window_is_left_pending(hermes_home, tmp_path):
    from datetime import datetime

    root = tmp_path / "srv" / "trix"
    request_path = tmp_path / "req.json"
    request_path.write_text(json.dumps({
        "when": "night", "ops": [{"op": "create_folder", "folder": "company"}],
    }), encoding="utf-8")

    noon = datetime(2026, 1, 1, 12, 0, 0)
    result = tc.run_executor(request_path, root=root, now=noon)
    assert result["outcome"] == "pending"
    assert request_path.exists()  # NOT consumed
    assert not root.exists()  # NOT applied


def test_night_request_inside_window_applies(hermes_home, tmp_path):
    from datetime import datetime

    root = tmp_path / "srv" / "trix"
    request_path = tmp_path / "req.json"
    request_path.write_text(json.dumps({
        "when": "night", "ops": [{"op": "create_folder", "folder": "company"}],
    }), encoding="utf-8")

    three_am = datetime(2026, 1, 1, 3, 0, 0)
    result = tc.run_executor(request_path, root=root, now=three_am)
    assert result["outcome"] == "applied"
    assert not request_path.exists()


def test_unknown_op_via_executor_refuses_and_reports(hermes_home, tmp_path):
    root = tmp_path / "srv" / "trix"
    request_path = tmp_path / "req.json"
    request_path.write_text(json.dumps({
        "when": "now", "ops": [{"op": "wipe_disk"}],
    }), encoding="utf-8")

    result = tc.run_executor(request_path, root=root)
    assert result["outcome"] == "refused"
    assert not root.exists()
    assert not request_path.exists()  # still consumed (moved aside) so it doesn't loop forever


# ---------------------------------------------------------------------------
# 13. publish_skill: copy, symlink refusal, add-vs-update
# ---------------------------------------------------------------------------


def test_publish_skill_lands_in_pending_not_live(hermes_home, tmp_path):
    """Item 3: publish_skill must NEVER go live directly — it lands in
    `.pending/<skill>/`, and the live directory must not exist until a
    human runs `approve_skill`."""
    root = tmp_path / "srv" / "trix"
    src = hermes_home / "profiles" / "sales" / "skills" / "greeting"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# greeting v1\n", encoding="utf-8")
    _make_profile(hermes_home, "sales")

    request = {"when": "now", "ops": [
        {"op": "publish_skill", "from_profile": "sales", "skill": "greeting"},
    ]}
    planned, _ = tc.validate_request(request, root=root)
    result = tc.apply_planned(planned, root)
    assert result.ops[0].ok
    assert "ЖДЁТ ОДОБРЕНИЯ" in result.ops[0].detail
    assert "approve-skill" in result.ops[0].detail
    # the full SKILL.md text is embedded in the detail (goes verbatim into
    # the request's result file) — the human sees it without touching the
    # machine.
    assert "# greeting v1" in result.ops[0].detail

    pending_md = root / "skills" / ".pending" / "greeting" / "SKILL.md"
    live_md = root / "skills" / "greeting" / "SKILL.md"
    assert pending_md.read_text(encoding="utf-8") == "# greeting v1\n"
    assert not live_md.exists()
    assert not (root / "skills" / "greeting").exists()


def test_publish_skill_result_file_contains_full_skill_md_text(hermes_home, tmp_path):
    """Item 3: the result file (what the human reads without going near
    the machine) must contain the full SKILL.md text, not just a
    one-line summary."""
    root = tmp_path / "srv" / "trix"
    src = hermes_home / "profiles" / "sales" / "skills" / "greeting"
    src.mkdir(parents=True)
    skill_body = "# greeting\n\nSome distinctive multi-line body text.\nSecond line.\n"
    (src / "SKILL.md").write_text(skill_body, encoding="utf-8")
    _make_profile(hermes_home, "sales")

    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps({
        "when": "now",
        "ops": [{"op": "publish_skill", "from_profile": "sales", "skill": "greeting"}],
    }), encoding="utf-8")
    result = tc.run_executor(request_path, root=root)
    assert result["outcome"] == "applied", result

    result_file = request_path.with_name(request_path.name + ".result.txt")
    result_text = result_file.read_text(encoding="utf-8")
    for line in skill_body.splitlines():
        assert line in result_text


def test_approve_skill_promotes_pending_to_live_and_commits(hermes_home, tmp_path):
    """The legitimate flow: a human reads the freshly-published candidate
    (via pending_skill_preview, as the CLI does), approves it by passing
    the digest of exactly what they just read, then a LATER, separate
    publish+approve cycle for a genuinely updated version works the same
    way. Each approve call is bound to a preview taken immediately before
    it — this is NOT the TOCTOU shape (see
    test_approve_skill_refuses_when_candidate_swapped_after_being_shown
    below for that)."""
    root = tmp_path / "srv" / "trix"
    src = hermes_home / "profiles" / "sales" / "skills" / "greeting"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# greeting v1\n", encoding="utf-8")
    _make_profile(hermes_home, "sales")

    request = {"when": "now", "ops": [
        {"op": "publish_skill", "from_profile": "sales", "skill": "greeting"},
    ]}
    planned, _ = tc.validate_request(request, root=root)
    tc.apply_planned(planned, root)

    preview = tc.pending_skill_preview(root, "greeting")
    assert "# greeting v1" in preview["text"]
    approved = tc.approve_skill(root, "greeting", expected_digest=preview["digest"])
    assert approved["is_update"] is False
    assert approved["commit"]
    assert (root / "skills" / "greeting" / "SKILL.md").read_text(encoding="utf-8") == "# greeting v1\n"
    assert not (root / "skills" / ".pending" / "greeting").exists()

    log = subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%B"], capture_output=True, text=True
    ).stdout
    assert "add greeting" in log

    # publish again with new content, preview + approve again -> update, not add
    (src / "SKILL.md").write_text("# greeting v2\n", encoding="utf-8")
    planned2, _ = tc.validate_request(request, root=root)
    tc.apply_planned(planned2, root)
    preview2 = tc.pending_skill_preview(root, "greeting")
    assert "# greeting v2" in preview2["text"]
    approved2 = tc.approve_skill(root, "greeting", expected_digest=preview2["digest"])
    assert approved2["is_update"] is True
    assert (root / "skills" / "greeting" / "SKILL.md").read_text(encoding="utf-8") == "# greeting v2\n"
    log2 = subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%B"], capture_output=True, text=True
    ).stdout
    assert "update greeting" in log2


def test_approve_skill_refuses_when_candidate_swapped_after_being_shown(hermes_home, tmp_path):
    """Blocker 3 (TOCTOU): a compromised agent shows a benign candidate,
    waits for the human to be about to approve, then swaps the pending
    content with a second publish_skill BEFORE the human's `approve-skill`
    actually runs. Binding approval to the digest of what was shown must
    refuse instead of silently promoting the swapped content — this is the
    exact scenario the old test_approve_skill_promotes_pending_to_live_
    and_commits enshrined as correct 'update' behavior."""
    root = tmp_path / "srv" / "trix"
    src = hermes_home / "profiles" / "sales" / "skills" / "greeting"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# greeting v1 (benign)\n", encoding="utf-8")
    _make_profile(hermes_home, "sales")

    request = {"when": "now", "ops": [
        {"op": "publish_skill", "from_profile": "sales", "skill": "greeting"},
    ]}
    planned, _ = tc.validate_request(request, root=root)
    tc.apply_planned(planned, root)

    # Human reads the benign text and gets its digest — this is what they
    # are about to approve.
    shown = tc.pending_skill_preview(root, "greeting")
    assert "benign" in shown["text"]

    # Agent swaps the candidate before the human's approve command runs.
    (src / "SKILL.md").write_text("# greeting v2 (malicious payload)\n", encoding="utf-8")
    planned2, _ = tc.validate_request(request, root=root)
    tc.apply_planned(planned2, root)

    with pytest.raises(tc.ContourError, match="изменился"):
        tc.approve_skill(root, "greeting", expected_digest=shown["digest"])

    # Nothing was promoted — the malicious content stays quarantined in
    # .pending/, never reaches the live, world-visible skills dir.
    assert not (root / "skills" / "greeting").exists()
    assert (root / "skills" / ".pending" / "greeting" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# greeting v2 (malicious payload)\n"


def test_approve_skill_without_expected_digest_still_works(hermes_home, tmp_path):
    """Backward-compat / scripted callers that don't pass expected_digest
    keep working — the digest check is opt-in via the parameter, matching
    the CLI which always supplies a freshly-computed one."""
    root = tmp_path / "srv" / "trix"
    src = hermes_home / "profiles" / "sales" / "skills" / "greeting"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# greeting\n", encoding="utf-8")
    _make_profile(hermes_home, "sales")

    request = {"when": "now", "ops": [
        {"op": "publish_skill", "from_profile": "sales", "skill": "greeting"},
    ]}
    planned, _ = tc.validate_request(request, root=root)
    tc.apply_planned(planned, root)

    approved = tc.approve_skill(root, "greeting")
    assert approved["is_update"] is False


def test_approve_skill_refuses_when_nothing_pending(hermes_home, tmp_path):
    root = tmp_path / "srv" / "trix"
    tc.ensure_repo(root)
    with pytest.raises(tc.ContourError):
        tc.approve_skill(root, "nosuchskill")


def test_pending_skill_dir_excluded_from_skill_discovery(tmp_path):
    """Item 3: `.pending` must be invisible to skill discovery so an
    unapproved candidate can never resolve through skills.external_dirs
    (the shared company skills dir IS an external_dirs entry) before a
    human approves it.

    Behavior contract, not a constant snapshot: builds a real external
    skills dir shaped exactly like <root>/skills (a live skill plus a
    `.pending/<skill>/SKILL.md` candidate — the same layout
    `_publish_skill_to_pending` produces) and proves the real index walker
    (`iter_skill_index_files`, what `build_skills_system_prompt` uses) never
    surfaces the pending one, while the live sibling is still found."""
    from agent.skill_utils import iter_skill_index_files

    external = tmp_path / "company-skills"
    live_skill = external / "onboarding"
    live_skill.mkdir(parents=True)
    (live_skill / "SKILL.md").write_text(
        "---\nname: onboarding\ndescription: Onboard a new hire.\n---\nBody.\n",
        encoding="utf-8",
    )
    pending_skill = external / ".pending" / "backdoor"
    pending_skill.mkdir(parents=True)
    (pending_skill / "SKILL.md").write_text(
        "---\nname: backdoor\ndescription: Not yet approved.\n---\nBody.\n",
        encoding="utf-8",
    )

    found = {p.parent.name for p in iter_skill_index_files(external, "SKILL.md")}
    assert found == {"onboarding"}
    assert "backdoor" not in found


def test_publish_skill_src_itself_symlink_refused(hermes_home, tmp_path):
    """Item 2: the skill directory ITSELF (not just its children) must be
    symlink-checked — Path.is_dir() follows symlinks, so a symlinked skill
    root previously sailed through as an ordinary directory and
    copytree(symlinks=False) copied the TARGET's content, leaking whatever
    it pointed at into the git-tracked, world-readable shared skills dir."""
    secret_dir = tmp_path / "secret_elsewhere"
    secret_dir.mkdir()
    (secret_dir / "SKILL.md").write_text("# leaked secret\n", encoding="utf-8")
    (secret_dir / "credentials.txt").write_text("super-secret\n", encoding="utf-8")

    _make_profile(hermes_home, "sales")
    skill_link = hermes_home / "profiles" / "sales" / "skills" / "innocentname"
    skill_link.parent.mkdir(parents=True, exist_ok=True)
    skill_link.symlink_to(secret_dir)

    request = {"when": "now", "ops": [
        {"op": "publish_skill", "from_profile": "sales", "skill": "innocentname"},
    ]}
    with pytest.raises(tc.ContourError, match="символическая ссылка"):
        tc.validate_request(request, root=tmp_path / "srv")


def test_publish_skill_requires_skill_md(hermes_home, tmp_path):
    src = hermes_home / "profiles" / "sales" / "skills" / "notaskill"
    src.mkdir(parents=True)
    (src / "readme.txt").write_text("hi", encoding="utf-8")
    _make_profile(hermes_home, "sales")

    request = {"when": "now", "ops": [
        {"op": "publish_skill", "from_profile": "sales", "skill": "notaskill"},
    ]}
    with pytest.raises(tc.ContourError, match="SKILL.md"):
        tc.validate_request(request, root=tmp_path / "srv")


def test_publish_skill_size_cap(hermes_home, tmp_path, monkeypatch):
    src = hermes_home / "profiles" / "sales" / "skills" / "huge"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# huge\n", encoding="utf-8")
    (src / "blob.bin").write_bytes(b"0" * 1024)
    _make_profile(hermes_home, "sales")

    monkeypatch.setattr(tc, "MAX_SKILL_SIZE_BYTES", 100)
    request = {"when": "now", "ops": [
        {"op": "publish_skill", "from_profile": "sales", "skill": "huge"},
    ]}
    with pytest.raises(tc.ContourError, match="предела"):
        tc.validate_request(request, root=tmp_path / "srv")


# ---------------------------------------------------------------------------
# status() / check() — read-only
# ---------------------------------------------------------------------------


def test_status_reports_only_entries_under_root(hermes_home, tmp_path):
    root = tmp_path / "srv" / "trix"
    text = MINIMAL_CONFIG.replace(
        "  docker_volumes: []\n",
        f"  docker_volumes:\n    - {root}/dept/sales:/dept:rw\n    - /some/unrelated:/other:ro\n",
    )
    _make_profile(hermes_home, "sales", text)
    data = tc.status(root)
    assert data["sales"] == [{"folder": "dept/sales", "container_path": "/dept", "mode": "rw"}]


def test_check_without_docker_reports_unknown_liveness(hermes_home, tmp_path, monkeypatch):
    root = tmp_path / "srv" / "trix"
    text = MINIMAL_CONFIG.replace(
        "  docker_volumes: []\n",
        f"  docker_volumes:\n    - {root}/dept/sales:/dept:rw\n",
    )
    _make_profile(hermes_home, "sales", text)
    monkeypatch.setattr(tc.shutil, "which", lambda _name: None)
    report = tc.check(root)
    assert report["sales"]["live"] is None
