"""Фаза 1.2 плана 2026-09-14: четыре ветви профиля выводятся из одного корня.

Резолверы — чистые функции: каталогов не создают, на диск не пишут.
"""

import os
from pathlib import Path

import hermes_constants


def _contained(monkeypatch, root: Path):
    monkeypatch.setattr(hermes_constants, "is_container", lambda: False)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    hermes_constants.reset_isolation_warnings()


def _shared(monkeypatch, root: Path):
    monkeypatch.setattr(hermes_constants, "is_container", lambda: False)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    hermes_constants.reset_isolation_warnings()


def test_profile_root_is_hermes_home(monkeypatch, tmp_path):
    _contained(monkeypatch, tmp_path / "profile")
    assert hermes_constants.get_profile_root() == tmp_path / "profile"
    assert hermes_constants.get_profile_root() == hermes_constants.get_hermes_home()


def test_defaults_derive_from_single_root(monkeypatch, tmp_path):
    root = tmp_path / "profile"
    _contained(monkeypatch, root)
    assert hermes_constants.get_profile_home_dir() == root / "home"
    assert hermes_constants.get_profile_tmp_dir() == root / "tmp"
    assert hermes_constants.get_profile_workspace_dir() == root / "workspace"


def test_resolvers_create_nothing(monkeypatch, tmp_path):
    root = tmp_path / "profile"
    root.mkdir()
    _contained(monkeypatch, root)
    hermes_constants.get_profile_home_dir()
    hermes_constants.get_profile_tmp_dir()
    hermes_constants.get_profile_workspace_dir()
    assert sorted(p.name for p in root.iterdir()) == []


def test_all_branches_stay_under_root(monkeypatch, tmp_path):
    root = tmp_path / "profile"
    _contained(monkeypatch, root)
    resolved_root = root.resolve()
    for branch in (
        hermes_constants.get_profile_home_dir(),
        hermes_constants.get_profile_tmp_dir(),
        hermes_constants.get_profile_workspace_dir(),
    ):
        assert Path(os.path.abspath(branch)).is_relative_to(resolved_root), branch


def test_symlinked_root_does_not_leak_outside(monkeypatch, tmp_path):
    """Корень может быть симлинком; ветви обязаны остаться внутри реального корня."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    _contained(monkeypatch, link)
    for branch in (
        hermes_constants.get_profile_home_dir(),
        hermes_constants.get_profile_tmp_dir(),
        hermes_constants.get_profile_workspace_dir(),
    ):
        assert branch.resolve().is_relative_to(real.resolve()), branch


def test_hermes_tmp_dir_override_is_honored(monkeypatch, tmp_path):
    root = tmp_path / "profile"
    _contained(monkeypatch, root)
    monkeypatch.setenv("HERMES_TMP_DIR", str(tmp_path / "elsewhere"))
    assert hermes_constants.get_profile_tmp_dir() == tmp_path / "elsewhere"


def test_shared_host_tmp_follows_external_tmpdir(monkeypatch, tmp_path):
    _shared(monkeypatch, tmp_path / "profile")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "host-tmp"))
    assert hermes_constants.get_profile_tmp_dir() == tmp_path / "host-tmp"


def test_contained_ignores_external_tmpdir(monkeypatch, tmp_path):
    """Внешний TMPDIR в contained не уводит временные файлы за корень."""
    root = tmp_path / "profile"
    _contained(monkeypatch, root)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "host-tmp"))
    assert hermes_constants.get_profile_tmp_dir() == root / "tmp"


def test_shared_host_home_dir_is_real_home(monkeypatch, tmp_path):
    _shared(monkeypatch, tmp_path / "profile")
    monkeypatch.setenv("HOME", str(tmp_path / "realhome"))
    assert hermes_constants.get_profile_home_dir() == tmp_path / "realhome"


def test_workspace_override_is_honored(monkeypatch, tmp_path):
    root = tmp_path / "profile"
    _contained(monkeypatch, root)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "operator-cwd"))
    assert hermes_constants.get_profile_workspace_dir() == tmp_path / "operator-cwd"


def test_workspace_placeholder_is_not_an_override(monkeypatch, tmp_path):
    """`.`/`auto`/`cwd` — плейсхолдеры, а не путь; они не отменяют корень."""
    root = tmp_path / "profile"
    _contained(monkeypatch, root)
    for placeholder in (".", "auto", "cwd", ""):
        monkeypatch.setenv("TERMINAL_CWD", placeholder)
        assert hermes_constants.get_profile_workspace_dir() == root / "workspace"


def test_explicit_override_outside_root_warns_once(monkeypatch, tmp_path, capsys):
    root = tmp_path / "profile"
    _contained(monkeypatch, root)
    monkeypatch.setenv("HERMES_TMP_DIR", str(tmp_path / "outside"))
    hermes_constants.get_profile_tmp_dir()
    hermes_constants.get_profile_tmp_dir()
    err = capsys.readouterr().err
    assert err.count("HERMES_TMP_DIR") == 1, err
