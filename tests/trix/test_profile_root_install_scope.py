"""Фаза 2.9 плана 2026-09-14: скоуп установки против скоупа профиля.

Обновление трогает код, venv и кэши пакетов — общие машинные ассеты
пользователя ОС, а не данные профиля. Если бы его git/uv/npm получали
contained-окружение, предзалитые рецептом ~/.cache/uv и ~/.npm/_cacache
качались бы заново на каждой машине при каждом /update.

Тесты исполняют настоящие подпроцессы и смотрят, какой HOME они увидели.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import hermes_constants

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def scopes(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    (profile / "home").mkdir(parents=True)
    real_home = tmp_path / "realhome"
    (real_home / ".cache" / "uv").mkdir(parents=True)
    for var in ("HERMES_ISOLATION", "TERMINAL_HOME_MODE", "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    hermes_constants.reset_isolation_warnings()
    return profile, real_home


def test_restores_home_from_real_home(scopes, monkeypatch):
    profile, real_home = scopes
    monkeypatch.setenv("HOME", str(profile / "home"))
    monkeypatch.setenv("HERMES_REAL_HOME", str(real_home))
    assert hermes_constants.restore_install_scope_home() == str(real_home)
    assert os.environ["HOME"] == str(real_home)


def test_is_a_noop_when_home_is_already_right(scopes, monkeypatch):
    profile, real_home = scopes
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_REAL_HOME", str(real_home))
    assert hermes_constants.restore_install_scope_home() is None
    assert os.environ["HOME"] == str(real_home)


def test_is_a_noop_without_real_home(scopes, monkeypatch):
    profile, _ = scopes
    monkeypatch.setenv("HOME", str(profile / "home"))
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    assert hermes_constants.restore_install_scope_home() is None
    assert os.environ["HOME"] == str(profile / "home")


def test_refuses_a_real_home_that_does_not_exist(scopes, monkeypatch, tmp_path):
    """Не чинить вслепую: несуществующий дом хуже профильного."""
    profile, _ = scopes
    monkeypatch.setenv("HOME", str(profile / "home"))
    monkeypatch.setenv("HERMES_REAL_HOME", str(tmp_path / "нет-такого"))
    assert hermes_constants.restore_install_scope_home() is None
    assert os.environ["HOME"] == str(profile / "home")


def test_profile_scoped_xdg_is_dropped(scopes, monkeypatch):
    """XDG обязаны уйти вместе с домом, иначе uv сложит кэш в профиль."""
    profile, real_home = scopes
    monkeypatch.setenv("HOME", str(profile / "home"))
    monkeypatch.setenv("HERMES_REAL_HOME", str(real_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(profile / "home" / ".cache"))
    hermes_constants.restore_install_scope_home()
    assert "XDG_CACHE_HOME" not in os.environ


def test_operator_xdg_outside_the_profile_survives(scopes, monkeypatch, tmp_path):
    profile, real_home = scopes
    monkeypatch.setenv("HOME", str(profile / "home"))
    monkeypatch.setenv("HERMES_REAL_HOME", str(real_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(real_home / ".cache"))
    hermes_constants.restore_install_scope_home()
    assert os.environ["XDG_CACHE_HOME"] == str(real_home / ".cache")


def test_fake_git_and_uv_see_the_real_home(scopes, tmp_path):
    """Исполнение, а не чтение: поддельные git и uv печатают свой HOME.

    Это и есть та проверка, ради которой §2.6 плана существует: дочерний
    процесс скоупа установки обязан увидеть настоящий дом.
    """
    profile, real_home = scopes
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("git", "uv"):
        tool = bindir / name
        tool.write_text(f'#!/bin/sh\necho "{name} HOME=$HOME"\n', encoding="utf-8")
        tool.chmod(0o755)

    code = (
        "import os, subprocess, sys;"
        "sys.path.insert(0, %r);"
        "import hermes_constants as h;"
        "h.restore_install_scope_home();"
        "print(subprocess.run(['git'], capture_output=True, text=True).stdout.strip());"
        "print(subprocess.run(['uv'], capture_output=True, text=True).stdout.strip())"
    ) % str(REPO_ROOT)

    out = subprocess.run(
        [sys.executable, "-c", code],
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(profile / "home"),
            "HERMES_REAL_HOME": str(real_home),
            "HERMES_HOME": str(profile),
            "HERMES_ISOLATION": "contained",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.splitlines() == [
        f"git HOME={real_home}",
        f"uv HOME={real_home}",
    ], out.stdout


def test_update_launcher_passes_the_way_back(monkeypatch, tmp_path):
    """Шлюз обязан передать дочернему `hermes update` HERMES_REAL_HOME."""
    real_home = tmp_path / "realhome"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)

    captured: dict = {}

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    import hermes_cli.update_launch as launch

    monkeypatch.setattr(launch.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(launch, "is_running", lambda: False)
    monkeypatch.setattr(launch, "resolve_hermes_command", lambda: ["hermes"])
    monkeypatch.setattr(launch, "installed_version", lambda: "0.0.0")
    monkeypatch.setattr(launch, "output_path", lambda: tmp_path / "out.log")
    monkeypatch.setattr(launch, "exit_code_path", lambda: tmp_path / "code")
    monkeypatch.setattr(launch, "from_version_path", lambda: tmp_path / "from")
    monkeypatch.setattr(launch, "_update_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(launch, "build_launcher_argv", lambda script: ["/bin/true"])

    launch.start_detached_update(["--gateway"])
    assert captured["env"]["HERMES_REAL_HOME"] == str(real_home)


# ── Подключение: контракт докстринга против фактических точек ──────────
#
# Докстринг `restore_install_scope_home` объявлял скоуп из четырёх пунктов
# («update, gateway install, uninstall, установщик»), а подключена функция
# была ровно к одному. Три остальных пишут ровно туда, ради чего она и
# существует: юнит systemd и plist launchd живут в `~/.config/systemd/user`
# и `~/Library/LaunchAgents` — это интерфейсы ОС, класс OS_RUNTIME, и в
# корне профиля они бесполезны. Процесс, пришедший от шлюза с
# contained-окружением, положил бы их именно туда.
#
# Проверяется исполнением: HOME процесса подменяется на профильный,
# вызывается точка входа, и смотрим, куда она целится.

@pytest.fixture
def contained_process(scopes, monkeypatch):
    """Процесс, пришедший от шлюза: HOME профильный, настоящий — в запасе."""
    profile, real_home = scopes
    monkeypatch.setenv("HOME", str(profile / "home"))
    monkeypatch.setenv("HERMES_REAL_HOME", str(real_home))
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    hermes_constants.reset_isolation_warnings()
    return profile, real_home


@pytest.mark.parametrize(
    "entry",
    ["systemd_install", "systemd_uninstall", "launchd_install", "launchd_uninstall"],
)
def test_service_entrypoints_repair_the_install_scope_home(
    contained_process, monkeypatch, entry: str
):
    """Каждая точка установки/удаления службы чинит HOME до первой записи."""
    profile, real_home = contained_process
    import hermes_cli.gateway as gateway

    seen: list[str] = []

    def _stop(*_args, **_kwargs):
        seen.append(os.environ.get("HOME", ""))
        raise _Stop()

    class _Stop(Exception):
        pass

    # Точка обязана починить HOME ДО того, как начнёт делать своё дело.
    # Первый же шаг подменяется на «запомнить HOME и остановиться».
    monkeypatch.setattr(gateway, "_select_systemd_scope", _stop, raising=False)
    monkeypatch.setattr(gateway, "_require_root_for_system_service", _stop, raising=False)
    monkeypatch.setattr(gateway, "has_legacy_hermes_units", _stop, raising=False)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", _stop, raising=False)

    with pytest.raises(_Stop):
        getattr(gateway, entry)()

    assert seen == [str(real_home)], (
        f"{entry} начала работу с профильным HOME — юнит/plist уехал бы "
        f"в корень профиля: {seen}"
    )


def test_uninstall_repairs_the_install_scope_home(contained_process, monkeypatch):
    """``hermes uninstall`` — тот же скоуп установки."""
    profile, real_home = contained_process
    import hermes_cli.main as main_mod

    seen: list[str] = []

    class _Stop(Exception):
        pass

    def _stop(*_args, **_kwargs):
        seen.append(os.environ.get("HOME", ""))
        raise _Stop()

    monkeypatch.setattr(main_mod, "_require_tty", _stop, raising=False)

    from argparse import Namespace

    with pytest.raises(_Stop):
        main_mod.cmd_uninstall(Namespace(gui_summary=False, gui=False, yes=False))

    assert seen == [str(real_home)], seen


def test_docstring_scope_matches_the_wiring():
    """Контракт написан там же, где выполняется.

    Тест держит не текст ради текста: это единственное место, где заявлено,
    КУДА функция подключена. Разъехавшийся докстринг — то, с чего этот
    дефект и начался.
    """
    doc = hermes_constants.restore_install_scope_home.__doc__ or ""
    for entry in ("update", "gateway install", "uninstall"):
        assert entry in doc, f"докстринг не называет {entry!r}"
    # Установщик — bash, идёт в оболочке оператора и contained-окружения
    # не видит никогда. Это должно быть написано, а не подразумеваться.
    assert "install.sh" in doc
