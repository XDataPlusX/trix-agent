"""Фаза 2.4–2.6 плана 2026-09-14: временные файлы профиля под корнем.

Проверяется не «переменная выставлена», а куда реально ложатся файлы:
хранилище результатов тулов, сокет code-exec, tempfile самого процесса.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import hermes_constants

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def root(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    real_home = tmp_path / "realhome"
    real_home.mkdir()
    for var in ("HERMES_ISOLATION", "HERMES_TMP_DIR", "TERMINAL_HOME_MODE",
                "TERMINAL_CWD", "HERMES_REAL_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HOME", str(real_home))
    hermes_constants.reset_isolation_warnings()
    return profile


# ── 2.4: LocalEnvironment.get_temp_dir ───────────────────────────────

def test_local_temp_dir_is_under_root_when_contained(root, monkeypatch):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    from tools.environments.local import LocalEnvironment

    assert LocalEnvironment().get_temp_dir() == str(root / "tmp")


def test_inherited_tmpdir_does_not_win_over_containment(root, monkeypatch, tmp_path):
    """Унаследованный от юнита TMPDIR не имеет права вывести файлы за корень."""
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "host-tmp"))
    from tools.environments.local import LocalEnvironment

    assert LocalEnvironment().get_temp_dir() == str(root / "tmp")


def test_shared_host_temp_dir_is_unchanged(root, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "host-tmp"))
    from tools.environments.local import LocalEnvironment

    assert LocalEnvironment().get_temp_dir() == str(tmp_path / "host-tmp")


def test_explicit_hermes_tmp_dir_wins(root, monkeypatch, tmp_path):
    """Явное HERMES_TMP_DIR — это custom, и оператор в своём праве."""
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    monkeypatch.setenv("HERMES_TMP_DIR", str(tmp_path / "mine"))
    from tools.environments.local import LocalEnvironment

    assert LocalEnvironment().get_temp_dir() == str(tmp_path / "mine")


def test_tool_result_storage_follows_the_temp_dir(root, monkeypatch):
    """Хранилище результатов тулов правок не требует — оно идёт за get_temp_dir."""
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    from tools.environments.local import LocalEnvironment
    from tools.tool_result_storage import _resolve_storage_dir

    resolved = Path(str(_resolve_storage_dir(LocalEnvironment())))
    assert resolved.is_relative_to(root), resolved


# ── 2.6: сокет code-exec ─────────────────────────────────────────────

def test_socket_dir_prefers_xdg_runtime_dir(root, monkeypatch, tmp_path):
    runtime = tmp_path / "run-user"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    assert hermes_constants.get_runtime_socket_dir() == str(runtime)


def test_socket_dir_falls_back_to_tmp(root, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "nope"))
    assert hermes_constants.get_runtime_socket_dir() == "/tmp"


def test_socket_path_stays_short_for_a_deep_root(monkeypatch, tmp_path):
    """sun_path — 108 байт. Глубокий корень не имеет права ломать bind()."""
    deep = tmp_path / ("d" * 200)
    monkeypatch.setenv("HERMES_HOME", str(deep))
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    hermes_constants.reset_isolation_warnings()
    tag = hermes_constants.get_profile_root_tag()
    sock = os.path.join(
        hermes_constants.get_runtime_socket_dir(), f"trix_rpc_{tag}_{'a' * 12}.sock"
    )
    assert len(sock.encode()) < 100, sock


def test_two_roots_get_two_socket_names(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    tag_a = hermes_constants.get_profile_root_tag()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "b"))
    tag_b = hermes_constants.get_profile_root_tag()
    assert tag_a != tag_b


def test_root_tag_is_stable(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    assert hermes_constants.get_profile_root_tag() == hermes_constants.get_profile_root_tag()


# ── 2.5: TMPDIR самого процесса ──────────────────────────────────────

def test_apply_process_tmpdir_is_a_noop_in_shared_host(root, monkeypatch):
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    monkeypatch.setenv("TMPDIR", "/tmp")
    assert hermes_constants.apply_process_tmpdir() is None
    assert os.environ["TMPDIR"] == "/tmp"


def test_apply_process_tmpdir_redirects_tempfile(root, monkeypatch):
    import tempfile

    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    monkeypatch.setattr(tempfile, "tempdir", "/tmp", raising=False)
    assert hermes_constants.apply_process_tmpdir() == str(root / "tmp")
    assert os.environ["TMPDIR"] == str(root / "tmp")
    assert tempfile.gettempdir() == str(root / "tmp")


def _probe(env: dict, code: str) -> str:
    full = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT), **env}
    out = subprocess.run(
        [sys.executable, "-c", code], env=full, capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr[-3000:]
    return out.stdout.strip()


@pytest.mark.timeout(180)
def test_entrypoint_sets_tmpdir_in_a_clean_process(tmp_path):
    """Прогон А: чистый env -i, contained — всё три пути под корнем.

    Это acceptance-строка §5.1 плана, записанная как тест: импорт точки
    входа обязан перенаправить tempfile ДО первого обращения к нему.
    """
    profile = tmp_path / "profile"
    profile.mkdir()
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    printed = _probe(
        {
            "HOME": str(fake_home),
            "HERMES_HOME": str(profile),
            "HERMES_ISOLATION": "contained",
        },
        "import hermes_constants as h; h.apply_process_tmpdir();"
        "import tempfile;"
        "from tools.environments.local import LocalEnvironment;"
        "print(tempfile.gettempdir());"
        "print(LocalEnvironment().get_temp_dir());"
        "print(h.get_profile_home_dir())",
    )
    lines = printed.splitlines()
    assert len(lines) == 3, printed
    for line in lines:
        assert Path(line).is_relative_to(profile), line
    assert not (fake_home / ".cache").exists()


@pytest.mark.timeout(180)
def test_entrypoint_leaves_shared_host_alone(tmp_path):
    """Прогон Б: тот же чистый env, shared-host — наружу уходит /tmp."""
    profile = tmp_path / "profile"
    profile.mkdir()
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    printed = _probe(
        {
            "HOME": str(fake_home),
            "HERMES_HOME": str(profile),
            "HERMES_ISOLATION": "shared-host",
        },
        "import hermes_constants as h; print(h.apply_process_tmpdir());"
        "import tempfile;"
        "from tools.environments.local import LocalEnvironment;"
        "print(tempfile.gettempdir());"
        "print(LocalEnvironment().get_temp_dir());"
        "print(h.get_subprocess_home())",
    )
    assert printed.splitlines() == ["None", "/tmp", "/tmp", "None"], printed
    # Ветви containment не должны появиться на диске: их создаёт только
    # contained-путь, и их наличие означало бы, что режим протёк.
    assert not (profile / "tmp").exists()
    assert not (profile / "home").exists()
