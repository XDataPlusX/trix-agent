"""Tests for the `hermes contour` CLI wiring (hermes_cli/trix_contour_cli.py).

These are thin — the actual security logic is tested exhaustively in
tests/hermes_cli/test_trix_contour.py. Here we only prove the CLI plumbing
(argparse wiring, --root override, exit codes) works end to end through the
real `hermes_cli.main.main()` entry point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MINIMAL_CONFIG = """\
terminal:
  backend: docker
  docker_volumes: []

skills:
  external_dirs: []
"""


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    return home


def _run_cli(argv, capsys):
    """Run `hermes <argv>` and normalize the exit code.

    ``hermes_cli.main.main()`` follows this fork's convention (see the tail
    of ``main()``): it returns ``None`` for success and only ever calls
    ``sys.exit(rc)`` for a NON-ZERO handler return — a plain ``0`` return is
    swallowed. ``argparse``'s own ``--help`` action raises ``SystemExit(0)``
    before a handler even runs. Both shapes collapse to the same
    ``(0, stdout, stderr)`` here so the tests read like exit-code checks
    instead of re-deriving this convention in every test.
    """
    import sys

    from hermes_cli.main import main

    old_argv = sys.argv
    sys.argv = ["hermes", *argv]
    try:
        try:
            rc = main()
        except SystemExit as e:
            rc = e.code
    finally:
        sys.argv = old_argv
    code = 0 if rc is None else rc
    out = capsys.readouterr()
    return code, out.out, out.err


def test_contour_help_lists_verbs(hermes_home, capsys):
    code, out, _err = _run_cli(["contour", "--help"], capsys)
    assert code == 0
    for verb in ("status", "check", "apply-request", "push", "approve-skill"):
        assert verb in out


def test_contour_status_empty(hermes_home, tmp_path, capsys):
    root = tmp_path / "srv" / "trix"
    code, out, _err = _run_cli(["contour", "--root", str(root), "status"], capsys)
    assert code == 0
    assert "ни один профиль" in out


def test_contour_apply_request_end_to_end(hermes_home, tmp_path, capsys):
    root = tmp_path / "srv" / "trix"
    request_path = tmp_path / "req.json"
    request_path.write_text(
        json.dumps({"when": "now", "ops": [{"op": "create_folder", "folder": "company"}]}),
        encoding="utf-8",
    )
    code, out, _err = _run_cli(
        ["contour", "--root", str(root), "apply-request", str(request_path)], capsys
    )
    assert code == 0
    assert "Итог: applied" in out
    assert (root / "company").is_dir()
    assert not request_path.exists()


def test_contour_push_without_remote(hermes_home, tmp_path, capsys):
    root = tmp_path / "srv" / "trix"
    from hermes_cli import trix_contour as tc

    tc.ensure_repo(root)
    code, out, _err = _run_cli(["contour", "--root", str(root), "push"], capsys)
    assert code == 0
    assert "не настроен" in out


def test_contour_approve_skill_end_to_end(hermes_home, tmp_path, capsys):
    """publish_skill lands in .pending/ (item 3) — `hermes contour
    approve-skill` is the human's only way to make it live."""
    root = tmp_path / "srv" / "trix"
    from hermes_cli import trix_contour as tc

    src = hermes_home / "profiles" / "sales" / "skills" / "greeting"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# greeting\n", encoding="utf-8")
    (hermes_home / "profiles" / "sales" / "config.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")

    request_path = tmp_path / "req.json"
    request_path.write_text(
        json.dumps({"when": "now", "ops": [
            {"op": "publish_skill", "from_profile": "sales", "skill": "greeting"},
        ]}),
        encoding="utf-8",
    )
    code, _out, _err = _run_cli(
        ["contour", "--root", str(root), "apply-request", str(request_path)], capsys
    )
    assert code == 0
    assert not (root / "skills" / "greeting").exists()
    assert (root / "skills" / ".pending" / "greeting" / "SKILL.md").is_file()

    code, out, _err = _run_cli(
        ["contour", "--root", str(root), "approve-skill", "greeting", "--yes"], capsys
    )
    assert code == 0
    assert "опубликован" in out
    assert "# greeting" in out  # blocker 3: full text is printed before promotion
    assert (root / "skills" / "greeting" / "SKILL.md").read_text(encoding="utf-8") == "# greeting\n"


def test_contour_approve_skill_stale_digest_refused(hermes_home, tmp_path, capsys):
    """Blocker 3 (TOCTOU): if the candidate changed since the digest the
    caller has in hand was computed (a second publish_skill landed in
    between), --digest must refuse rather than silently approve whatever
    is currently sitting in .pending/."""
    root = tmp_path / "srv" / "trix"
    from hermes_cli import trix_contour as tc

    src = hermes_home / "profiles" / "sales" / "skills" / "greeting"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# greeting v1\n", encoding="utf-8")
    (hermes_home / "profiles" / "sales" / "config.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")

    request_path = tmp_path / "req.json"
    request_path.write_text(
        json.dumps({"when": "now", "ops": [
            {"op": "publish_skill", "from_profile": "sales", "skill": "greeting"},
        ]}),
        encoding="utf-8",
    )
    code, _out, _err = _run_cli(
        ["contour", "--root", str(root), "apply-request", str(request_path)], capsys
    )
    assert code == 0

    # Attacker swaps the candidate before the human types approve-skill.
    (src / "SKILL.md").write_text("# greeting v2 (swapped)\n", encoding="utf-8")
    request_path.write_text(
        json.dumps({"when": "now", "ops": [
            {"op": "publish_skill", "from_profile": "sales", "skill": "greeting"},
        ]}),
        encoding="utf-8",
    )
    code, _out, _err = _run_cli(
        ["contour", "--root", str(root), "apply-request", str(request_path)], capsys
    )
    assert code == 0

    code, out, err = _run_cli(
        ["contour", "--root", str(root), "approve-skill", "greeting",
         "--digest", "0" * 64, "--yes"],
        capsys,
    )
    assert code == 1
    assert "изменился" in err
    assert not (root / "skills" / "greeting").exists()


def test_contour_approve_remote_end_to_end(hermes_home, tmp_path, capsys):
    """Blocker 2: set_git_remote only records a pending remote; push refuses
    until a human runs `hermes contour approve-remote`."""
    root = tmp_path / "srv" / "trix"
    request_path = tmp_path / "req.json"
    request_path.write_text(
        json.dumps({"when": "now", "ops": [
            {"op": "set_git_remote", "url": "https://example.invalid/org/repo.git", "token": "ghp_x"},
        ]}),
        encoding="utf-8",
    )
    code, out, _err = _run_cli(
        ["contour", "--root", str(root), "apply-request", str(request_path)], capsys
    )
    assert code == 0
    assert "ЖДЁТ ОДОБРЕНИЯ" in out

    code, out, _err = _run_cli(["contour", "--root", str(root), "status"], capsys)
    assert code == 0
    assert "approve-remote" in out

    code, out, err = _run_cli(["contour", "--root", str(root), "push"], capsys)
    assert code == 1
    assert "approve-remote" in err

    code, out, _err = _run_cli(["contour", "--root", str(root), "approve-remote"], capsys)
    assert code == 0
    assert "активирован" in out

    import subprocess as _sp
    got = _sp.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True)
    assert got.stdout.strip() == "https://example.invalid/org/repo.git"
