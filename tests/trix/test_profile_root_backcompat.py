"""Фаза 1.4 плана 2026-09-14: обычная установка на хосте не изменилась.

Это прогон Б спеки §4.12, записанный как тест-регрессия. Он охраняет главное
обещание всей работы: пока нет ни маркера, ни переменной, ни контейнера —
движок ведёт себя РОВНО как апстрим, и ни один байт поведения не уехал.

Если этот файл покраснел — значит containment протёк в shared-host, и чинить
надо не тест.
"""

from pathlib import Path

import hermes_constants


def _plain_host_install(monkeypatch, tmp_path) -> Path:
    """Обычная одиночная установка: HERMES_HOME задан, больше ничего."""
    root = tmp_path / ".hermes"
    root.mkdir()
    real_home = tmp_path / "realhome"
    real_home.mkdir()
    monkeypatch.setattr(hermes_constants, "is_container", lambda: False)
    for var in ("HERMES_ISOLATION", "HERMES_TMP_DIR", "TERMINAL_CWD",
                "TERMINAL_HOME_MODE", "HERMES_REAL_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(real_home))
    hermes_constants.reset_isolation_warnings()
    return root


def test_mode_is_shared_host(monkeypatch, tmp_path):
    _plain_host_install(monkeypatch, tmp_path)
    assert hermes_constants.get_isolation_mode() == "shared-host"


def test_subprocess_home_override_stays_absent(monkeypatch, tmp_path):
    """Апстримный контракт: на хосте HOME подпроцессу не подменяется."""
    _plain_host_install(monkeypatch, tmp_path)
    assert hermes_constants.get_subprocess_home() is None


def test_subprocess_home_stays_absent_even_with_profile_home_dir(monkeypatch, tmp_path):
    """Существующий <root>/home на хосте — по-прежнему НЕ повод менять HOME.

    Это поведение апстрима (`auto` на хосте), и contained-режим не имеет
    права включиться просто потому, что каталог оказался на диске.
    """
    root = _plain_host_install(monkeypatch, tmp_path)
    (root / "home").mkdir()
    assert hermes_constants.get_subprocess_home() is None


def test_temp_dir_follows_the_host(monkeypatch, tmp_path):
    _plain_host_install(monkeypatch, tmp_path)
    monkeypatch.setenv("TMPDIR", "/tmp")
    assert hermes_constants.get_profile_tmp_dir() == Path("/tmp")


def test_temp_dir_without_tmpdir_is_slash_tmp(monkeypatch, tmp_path):
    _plain_host_install(monkeypatch, tmp_path)
    monkeypatch.delenv("TMPDIR", raising=False)
    assert hermes_constants.get_profile_tmp_dir() == Path("/tmp")


def test_home_dir_is_the_real_os_home(monkeypatch, tmp_path):
    _plain_host_install(monkeypatch, tmp_path)
    assert hermes_constants.get_profile_home_dir() == tmp_path / "realhome"


def test_subprocess_env_is_untouched_apart_from_real_home(monkeypatch, tmp_path):
    """В shared-host сборка окружения подпроцесса ничего не переписывает.

    Единственное, что движок добавляет — HERMES_REAL_HOME; это апстримное
    поведение, оно было до этой работы и остаётся после.
    """
    _plain_host_install(monkeypatch, tmp_path)
    env = {"HOME": str(tmp_path / "realhome"), "PATH": "/usr/bin"}
    before = dict(env)
    hermes_constants.apply_subprocess_home_env(env)
    assert env["HOME"] == before["HOME"]
    assert env["PATH"] == before["PATH"]
    assert set(env) - set(before) <= {"HERMES_REAL_HOME"}


def test_nothing_was_created_on_disk(monkeypatch, tmp_path):
    """Резолверы в shared-host не создают под корнем ни одного каталога."""
    root = _plain_host_install(monkeypatch, tmp_path)
    hermes_constants.get_profile_home_dir()
    hermes_constants.get_profile_tmp_dir()
    hermes_constants.get_profile_workspace_dir()
    hermes_constants.get_subprocess_home()
    assert list(root.iterdir()) == []


def test_ensure_helpers_do_not_create_profile_home_on_host(monkeypatch, tmp_path):
    """ensure_profile_home_dir в shared-host не создаёт <root>/home.

    Иначе любой вызов включил бы профильный дом фактом наличия каталога
    (см. `_profile_home_path`) и молча поменял бы поведение установки.
    """
    root = _plain_host_install(monkeypatch, tmp_path)
    hermes_constants.ensure_profile_home_dir()
    assert not (root / "home").exists()
