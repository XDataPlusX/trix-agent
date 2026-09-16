"""RAF-159: ``terminal.cwd`` — адресное пространство бэкенда, а не хоста.

Поставляемый клиентский конфиг задаёт ``backend: docker`` + ``cwd: /workspace``,
и это путь ВНУТРИ контейнера. Пока движок читал его как хостовый, доктор на
каждом новом contained-клиенте печатал `⚠ рабочая папка ВНЕ корня
(/workspace)` про дыру, которой нет, — и ровно так же молчал бы, если бы дыра
была.

Обе половины правила живут в одном файле намеренно: «не шуметь на контейнерном
пути» и «не молчать про настоящий хостовый путь» — это одно решение, и
откатить его вполовину нельзя.
"""

import os
from pathlib import Path

import pytest

import hermes_constants


REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_CONFIG = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


@pytest.fixture
def contained_root(monkeypatch, tmp_path):
    """Contained-профиль на чистом окружении: ни одной унаследованной переменной."""
    profile = tmp_path / "profile"
    profile.mkdir()
    real_home = tmp_path / "realhome"
    real_home.mkdir()
    for var in (
        "HERMES_TMP_DIR",
        "TERMINAL_CWD",
        "TERMINAL_ENV",
        "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(hermes_constants, "is_container", lambda: False)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    hermes_constants.reset_isolation_warnings()
    return profile


def _doctor(monkeypatch) -> tuple[list, str]:
    """Прогнать секцию Profile Layout доктора, вернуть (issues, вывод)."""
    import hermes_cli.trix_layout_check as mod

    monkeypatch.setattr(mod, "filesystem_for", lambda *a, **k: "ext4")
    issues: list = []
    assert mod.check_layout_containment(issues, should_fix=False) == 0
    return issues, ""


# ── Сам поставляемый конфиг ──────────────────────────────────────────────

def test_shipped_config_really_pairs_docker_with_a_container_cwd():
    """Тест сторожит живой шаблон клиента, а не выдуманную комбинацию."""
    import yaml

    cfg = yaml.safe_load(SHIPPED_CONFIG.read_text(encoding="utf-8"))
    terminal = cfg["terminal"]
    assert terminal["backend"] == "docker"
    assert terminal["cwd"] == "/workspace"


def test_shipped_config_through_the_real_bridge_keeps_the_workspace_under_root(
    contained_root, monkeypatch
):
    """Сквозь настоящий мост: шаблон клиента → ``TERMINAL_*`` → резолвер.

    Подстановок нет ни одной: файл берётся поставляемый, переменные ставит
    ``apply_terminal_config_to_env``. Ровно эта цепочка и печатала клиенту
    ``⚠ рабочая папка ВНЕ корня (/workspace)``.
    """
    from hermes_cli.config import apply_terminal_config_to_env

    (contained_root / "config.yaml").write_text(
        SHIPPED_CONFIG.read_text(encoding="utf-8"), encoding="utf-8"
    )
    bridged: dict = {}
    apply_terminal_config_to_env(env=bridged)

    assert bridged["TERMINAL_ENV"] == "docker"
    assert bridged["TERMINAL_CWD"] == "/workspace"
    assert (
        hermes_constants.get_profile_workspace_dir(bridged)
        == contained_root / "workspace"
    )


# ── Половина первая: контейнерный путь не читается как хостовый ──────────

def test_container_cwd_does_not_become_a_host_branch(contained_root, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CWD", "/workspace")
    assert hermes_constants.get_profile_workspace_dir() == contained_root / "workspace"


@pytest.mark.parametrize("backend", ["docker", "singularity", "modal", "daytona", "ssh"])
def test_every_non_local_backend_keeps_the_workspace_under_root(
    contained_root, monkeypatch, backend
):
    monkeypatch.setenv("TERMINAL_ENV", backend)
    monkeypatch.setenv("TERMINAL_CWD", "/workspace")
    assert hermes_constants.get_profile_workspace_dir() == contained_root / "workspace"


def test_container_cwd_raises_no_isolation_warning(contained_root, monkeypatch, capsys):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CWD", "/workspace")
    hermes_constants.get_profile_workspace_dir()
    assert "[hermes isolation]" not in capsys.readouterr().err


def test_doctor_is_clean_on_the_shipped_contained_docker_profile(
    contained_root, monkeypatch, capsys
):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CWD", "/workspace")
    issues, _ = _doctor(monkeypatch)
    out = capsys.readouterr().out
    assert issues == [], issues
    assert "⚠" not in out, out
    assert "✗" not in out, out
    assert "рабочая папка внутри корня" in out, out


# ── Половина вторая: настоящий путь наружу по-прежнему виден ─────────────

def test_local_backend_still_honors_an_explicit_path_outside(contained_root, monkeypatch, tmp_path):
    outside = tmp_path / "operator-cwd"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(outside))
    assert hermes_constants.get_profile_workspace_dir() == outside


def test_local_backend_outside_path_still_warns(contained_root, monkeypatch, tmp_path, capsys):
    outside = tmp_path / "operator-cwd"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(outside))
    hermes_constants.get_profile_workspace_dir()
    assert "[hermes isolation]" in capsys.readouterr().err


def test_doctor_still_reports_a_real_branch_outside(contained_root, monkeypatch, tmp_path, capsys):
    outside = tmp_path / "operator-cwd"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(outside))
    issues, _ = _doctor(monkeypatch)
    out = capsys.readouterr().out
    assert any("вне его корня" in i for i in issues), issues
    assert "рабочая папка ВНЕ корня" in out, out


def test_unset_backend_is_read_as_local(contained_root, monkeypatch, tmp_path):
    """Без ``TERMINAL_ENV`` бэкенд — ``local``: поведение апстрима не трогаем."""
    outside = tmp_path / "operator-cwd"
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setenv("TERMINAL_CWD", str(outside))
    assert hermes_constants.get_profile_workspace_dir() == outside


def test_mounted_docker_cwd_is_a_host_path_and_is_reported(contained_root, monkeypatch, tmp_path, capsys):
    """docker + монтирование cwd: путь снова хостовый — доктор обязан не молчать.

    ``terminal_tool`` при ``docker_mount_cwd_to_workspace`` берёт хостовый
    каталог, монтирует его и подменяет рабочий каталог на ``/workspace``.
    Агент правда пишет наружу корня, и это тот самый случай, ради которого
    предупреждение существует.
    """
    outside = tmp_path / "mounted-host-dir"
    outside.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true")
    monkeypatch.setenv("TERMINAL_CWD", str(outside))
    assert hermes_constants.get_profile_workspace_dir() == outside
    issues, _ = _doctor(monkeypatch)
    assert any("вне его корня" in i for i in issues), issues


def test_mounted_docker_relative_cwd_is_a_host_path_and_is_reported(
    contained_root, monkeypatch, tmp_path
):
    """Относительный ``cwd`` при монтировании — тоже хостовый каталог.

    ``terminal_tool`` раскрывает кандидата через ``os.path.abspath``, то есть
    относительный путь превращается в каталог ХОСТА и монтируется. Если
    резолвер профиля ограничится ``expanduser``, такой путь не пройдёт проверку
    на абсолютность — и доктор промолчит о ветви, которую терминал уже вынес
    наружу корня. Два экземпляра одного вопроса обязаны отвечать одинаково.
    """
    outside = tmp_path / "relative-host-dir"
    outside.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true")
    monkeypatch.setenv("TERMINAL_CWD", "relative-host-dir")

    resolved = hermes_constants.get_profile_workspace_dir()
    assert Path(os.path.abspath(resolved)) == outside.resolve()
    issues, _ = _doctor(monkeypatch)
    assert any("вне его корня" in i for i in issues), issues


def test_both_copies_of_the_host_path_question_answer_alike(contained_root, monkeypatch, tmp_path):
    """Сторож против тихого расхождения двух предикатов «это путь хоста?».

    ``terminal_tool._get_env_config`` решает, монтировать ли каталог, а
    ``hermes_constants`` — считать ли его ветвью профиля наружу. Разойдись они,
    и доктор начнёт отчитываться не про ту машину.
    """
    import tools.terminal_tool as tt

    existing = tmp_path / "exists"
    existing.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true")

    for value in (str(existing), "exists", "/workspace", "/root", "/nope/missing"):
        monkeypatch.setenv("TERMINAL_CWD", value)
        cfg = tt._get_env_config()
        mounted = cfg.get("host_cwd") is not None
        assert hermes_constants._terminal_cwd_is_host_path(value) == mounted, value


def test_mounted_docker_keeps_container_paths_container(contained_root, monkeypatch):
    """Даже при включённом монтировании ``/workspace`` остаётся контейнерным."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true")
    monkeypatch.setenv("TERMINAL_CWD", "/workspace")
    assert hermes_constants.get_profile_workspace_dir() == contained_root / "workspace"


# ── Потребители хостового значения ───────────────────────────────────────

def test_ensure_does_not_create_a_container_path_on_the_host(contained_root, monkeypatch):
    """``ensure_profile_workspace_dir`` не должен пытаться сделать mkdir /workspace."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CWD", "/workspace")
    created = hermes_constants.ensure_profile_workspace_dir()
    assert created == contained_root / "workspace"
    assert created.is_dir()
    assert Path(os.path.abspath(created)).is_relative_to(contained_root.resolve())
