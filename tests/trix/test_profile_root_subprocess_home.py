"""Фаза 2.1–2.3 плана 2026-09-14: что подпроцесс видит в contained.

Одна точка — apply_subprocess_containment_env() — обслуживает все шесть
конструкторов окружения в движке. Здесь проверяется её поведение и таблица
совмещения с TERMINAL_HOME_MODE (§2.4 плана).
"""

import os
from pathlib import Path

import pytest

import hermes_constants


@pytest.fixture
def root(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    real_home = tmp_path / "realhome"
    real_home.mkdir()
    monkeypatch.setattr(hermes_constants, "is_container", lambda: False)
    for var in ("HERMES_ISOLATION", "HERMES_TMP_DIR", "TERMINAL_HOME_MODE",
                "TERMINAL_CWD", "HERMES_REAL_HOME", "PLAYWRIGHT_BROWSERS_PATH",
                "AGENT_BROWSER_EXECUTABLE_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setattr(hermes_constants, "_iter_machine_browser_roots", lambda env=None: [])
    hermes_constants.reset_isolation_warnings()
    return profile


def _contained(monkeypatch):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")


# ── 2.1: HOME подпроцесса ────────────────────────────────────────────

def test_contained_auto_uses_profile_home(root, monkeypatch):
    _contained(monkeypatch)
    assert hermes_constants.get_subprocess_home() == str(root / "home")


def test_contained_creates_profile_home_0700(root, monkeypatch):
    """Корень без home/ — каталог обязан появиться сам.

    Иначе `_profile_home_path` вернёт None (дом включается фактом наличия
    каталога) и contained молча выродится в shared-host.
    """
    _contained(monkeypatch)
    assert not (root / "home").exists()
    hermes_constants.get_subprocess_home()
    assert (root / "home").is_dir()
    assert oct(os.stat(root / "home").st_mode & 0o777) == "0o700"


def test_contained_profile_mode_uses_profile_home(root, monkeypatch):
    _contained(monkeypatch)
    monkeypatch.setenv("TERMINAL_HOME_MODE", "profile")
    assert hermes_constants.get_subprocess_home() == str(root / "home")


def test_contained_real_mode_is_operator_override_with_warning(root, monkeypatch, tmp_path, capsys):
    """contained + home_mode=real — это custom: оператор вывел HOME за корень."""
    _contained(monkeypatch)
    monkeypatch.setenv("TERMINAL_HOME_MODE", "real")
    # Так выглядит вложенный спавн: родитель уже в contained и передал
    # дорогу назад в HERMES_REAL_HOME.
    env = {"HOME": str(root / "home"), "HERMES_REAL_HOME": str(tmp_path / "realhome")}
    assert hermes_constants.get_subprocess_home(env) == str(tmp_path / "realhome")
    assert "HOME" in capsys.readouterr().err


def test_contained_does_not_repair_back_to_real_home(root, monkeypatch):
    """Апстримный «ремонт назад» в contained выключен.

    В shared-host родитель с HOME=<root>/home чинится обратно на настоящий
    дом. В contained это ровно то состояние, которого мы добиваемся, и
    чинить его — значит выпустить данные профиля наружу.
    """
    _contained(monkeypatch)
    (root / "home").mkdir()
    env = {"HOME": str(root / "home"), "HERMES_HOME": str(root)}
    assert hermes_constants.get_subprocess_home(env) == str(root / "home")


def test_shared_host_still_repairs_back(root, monkeypatch, tmp_path):
    """А в shared-host ремонт назад работает как прежде."""
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    (root / "home").mkdir()
    env = {"HOME": str(root / "home"), "HERMES_HOME": str(root),
           "HERMES_REAL_HOME": str(tmp_path / "realhome")}
    assert hermes_constants.get_subprocess_home(env) == str(tmp_path / "realhome")


# ── 2.2: полный набор переменных ─────────────────────────────────────

def test_containment_env_sets_the_documented_set(root, monkeypatch):
    _contained(monkeypatch)
    env = {"PATH": "/usr/bin"}
    hermes_constants.apply_subprocess_containment_env(env)
    home = root / "home"
    assert env["HOME"] == str(home)
    assert env["XDG_CACHE_HOME"] == str(home / ".cache")
    assert env["XDG_CONFIG_HOME"] == str(home / ".config")
    assert env["XDG_DATA_HOME"] == str(home / ".local" / "share")
    assert env["XDG_STATE_HOME"] == str(home / ".local" / "state")
    assert env["TMPDIR"] == str(root / "tmp")
    assert env["TMP"] == env["TMPDIR"]
    assert env["TEMP"] == env["TMPDIR"]
    assert env["HERMES_OSINT_CACHE"] == str(root / "cache" / "osint")
    assert env["PATH"] == "/usr/bin"


def test_containment_env_keeps_os_runtime_untouched(root, monkeypatch):
    """XDG_RUNTIME_DIR и D-Bus — интерфейс ОС, не данные профиля."""
    _contained(monkeypatch)
    env = {"XDG_RUNTIME_DIR": "/run/user/1000",
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
    hermes_constants.apply_subprocess_containment_env(env)
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"


def test_containment_env_exposes_real_home_for_the_install_scope(root, monkeypatch, tmp_path):
    """HERMES_REAL_HOME — как скоуп установки находит дорогу назад (§2.6)."""
    _contained(monkeypatch)
    env = {}
    hermes_constants.apply_subprocess_containment_env(env)
    assert env["HERMES_REAL_HOME"] == str(tmp_path / "realhome")


def test_shared_host_env_is_unchanged_apart_from_real_home(root, monkeypatch):
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    env = {"PATH": "/usr/bin", "HOME": str(root.parent / "realhome")}
    before = dict(env)
    hermes_constants.apply_subprocess_containment_env(env)
    assert set(env) - set(before) <= {"HERMES_REAL_HOME"}
    assert env["HOME"] == before["HOME"]


def test_legacy_name_is_still_callable(root, monkeypatch):
    """Старое имя зовут шесть мест и один существующий тест."""
    _contained(monkeypatch)
    env = {}
    hermes_constants.apply_subprocess_home_env(env)
    assert env["HOME"] == str(root / "home")


def test_tmp_dir_is_created_0700(root, monkeypatch):
    _contained(monkeypatch)
    hermes_constants.apply_subprocess_containment_env({})
    assert (root / "tmp").is_dir()
    assert oct(os.stat(root / "tmp").st_mode & 0o777) == "0o700"


def test_explicit_osint_cache_is_not_overwritten(root, monkeypatch, tmp_path):
    _contained(monkeypatch)
    env = {"HERMES_OSINT_CACHE": str(tmp_path / "mine")}
    hermes_constants.apply_subprocess_containment_env(env)
    assert env["HERMES_OSINT_CACHE"] == str(tmp_path / "mine")


def test_real_home_mode_leaves_xdg_alone(root, monkeypatch, tmp_path, capsys):
    """Оператор оставил настоящий HOME — XDG обязаны следовать за ним, а не за корнем."""
    _contained(monkeypatch)
    monkeypatch.setenv("TERMINAL_HOME_MODE", "real")
    env = {"HOME": str(root / "home"), "HERMES_REAL_HOME": str(tmp_path / "realhome")}
    hermes_constants.apply_subprocess_containment_env(env)
    assert env["HOME"] == str(tmp_path / "realhome")
    assert "XDG_CACHE_HOME" not in env
    # TMPDIR всё равно под корнем: home_mode переопределяет только дом.
    assert env["TMPDIR"] == str(root / "tmp")


# ── 2.3: браузер на машине ───────────────────────────────────────────

def test_playwright_path_is_pinned_to_the_machine_cache(root, monkeypatch, tmp_path):
    """Без пина node-подпроцесс с contained HOME не найдёт Chromium."""
    _contained(monkeypatch)
    cache = tmp_path / "realhome" / ".cache" / "ms-playwright"
    (cache / "chromium-1200").mkdir(parents=True)
    monkeypatch.setattr(hermes_constants, "_iter_machine_browser_roots",
                        lambda env=None: [str(cache)])
    env = {}
    hermes_constants.apply_subprocess_containment_env(env)
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(cache)


def test_existing_playwright_path_is_not_overwritten(root, monkeypatch, tmp_path):
    _contained(monkeypatch)
    cache = tmp_path / "realhome" / ".cache" / "ms-playwright"
    (cache / "chromium-1200").mkdir(parents=True)
    monkeypatch.setattr(hermes_constants, "_iter_machine_browser_roots",
                        lambda env=None: [str(cache)])
    env = {"PLAYWRIGHT_BROWSERS_PATH": "/opt/hermes/.playwright"}
    hermes_constants.apply_subprocess_containment_env(env)
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/hermes/.playwright"


def test_agent_browser_chrome_is_pinned_by_executable(root, monkeypatch, tmp_path):
    """agent-browser 0.26 кладёт Chrome for Testing в ~/.agent-browser/browsers.

    PLAYWRIGHT_BROWSERS_PATH ему не помогает — у него своя раскладка, зато
    он уважает AGENT_BROWSER_EXECUTABLE_PATH.
    """
    _contained(monkeypatch)
    browsers = tmp_path / "realhome" / ".agent-browser" / "browsers"
    build = browsers / "chrome-152.0.7977.75"
    build.mkdir(parents=True)
    exe = build / "chrome"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setattr(hermes_constants, "_iter_machine_browser_roots",
                        lambda env=None: [str(browsers)])
    env = {}
    hermes_constants.apply_subprocess_containment_env(env)
    assert env["AGENT_BROWSER_EXECUTABLE_PATH"] == str(exe)


def test_no_browser_on_the_machine_pins_nothing(root, monkeypatch, tmp_path):
    _contained(monkeypatch)
    empty = tmp_path / "realhome" / ".cache" / "ms-playwright"
    empty.mkdir(parents=True)
    monkeypatch.setattr(hermes_constants, "_iter_machine_browser_roots",
                        lambda env=None: [str(empty)])
    env = {}
    hermes_constants.apply_subprocess_containment_env(env)
    assert "PLAYWRIGHT_BROWSERS_PATH" not in env
    assert "AGENT_BROWSER_EXECUTABLE_PATH" not in env


def test_shared_host_pins_nothing(root, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    cache = tmp_path / "realhome" / ".cache" / "ms-playwright"
    (cache / "chromium-1200").mkdir(parents=True)
    monkeypatch.setattr(hermes_constants, "_iter_machine_browser_roots",
                        lambda env=None: [str(cache)])
    env = {}
    hermes_constants.apply_subprocess_containment_env(env)
    assert "PLAYWRIGHT_BROWSERS_PATH" not in env


def test_browser_roots_are_derived_from_the_real_home(root, monkeypatch, tmp_path):
    """Перечень корней браузера — один на движок, и он про машину, не про профиль."""
    monkeypatch.undo()  # снять подмену _iter_machine_browser_roots
    monkeypatch.setenv("HOME", str(tmp_path / "realhome"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    roots = hermes_constants._iter_machine_browser_roots()
    assert str(tmp_path / "realhome" / ".cache" / "ms-playwright") in roots
    assert str(tmp_path / "realhome" / ".agent-browser" / "browsers") in roots
    for entry in roots:
        assert not Path(entry).is_relative_to(root), entry
