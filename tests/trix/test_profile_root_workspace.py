"""Фаза 4 плана 2026-09-14: рабочая папка профиля как дефолт cwd.

В contained плейсхолдер cwd обязан приводить в <root>/workspace, а не в
домашний каталог оператора — единственное место, где данные профиля
накапливаться не должны. В shared-host всё как было.
"""

import os

import pytest

import hermes_constants
from gateway.cwd_placeholder import placeholder_home_fallback, resolve_placeholder_terminal_cwd


@pytest.fixture
def root(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    real_home = tmp_path / "realhome"
    real_home.mkdir()
    for var in ("HERMES_ISOLATION", "TERMINAL_CWD", "MESSAGING_CWD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HOME", str(real_home))
    hermes_constants.reset_isolation_warnings()
    return profile


def test_fallback_is_the_workspace_when_contained(root, monkeypatch):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    assert placeholder_home_fallback() == str(root / "workspace")


def test_workspace_is_created_0700_on_first_use(root, monkeypatch):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    assert not (root / "workspace").exists()
    placeholder_home_fallback()
    assert (root / "workspace").is_dir()
    assert oct(os.stat(root / "workspace").st_mode & 0o777) == "0o700"


def test_fallback_is_the_real_home_in_shared_host(root, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    assert placeholder_home_fallback() == str(tmp_path / "realhome")
    assert not (root / "workspace").exists()


def test_local_placeholder_resolves_to_the_workspace(root, monkeypatch):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    resolved = resolve_placeholder_terminal_cwd(
        configured_cwd="auto",
        terminal_backend="local",
        messaging_cwd=None,
        docker_mount_cwd_to_workspace=False,
        home_fallback=placeholder_home_fallback(),
    )
    assert resolved == str(root / "workspace")


def test_explicit_cwd_still_wins(root, monkeypatch, tmp_path):
    """Оператор назвал папку — это его выбор, USER_WORKSPACE."""
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    resolved = resolve_placeholder_terminal_cwd(
        configured_cwd=str(tmp_path / "project"),
        terminal_backend="local",
        messaging_cwd=None,
        docker_mount_cwd_to_workspace=False,
        home_fallback=placeholder_home_fallback(),
    )
    assert resolved == str(tmp_path / "project")


def test_messaging_cwd_still_wins(root, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    resolved = resolve_placeholder_terminal_cwd(
        configured_cwd="",
        terminal_backend="local",
        messaging_cwd=str(tmp_path / "chat-cwd"),
        docker_mount_cwd_to_workspace=False,
        home_fallback=placeholder_home_fallback(),
    )
    assert resolved == str(tmp_path / "chat-cwd")


def test_docker_backend_is_untouched(root, monkeypatch):
    """У клиента Trix бэкенд docker и cwd явный — фаза 4 ему ничего не меняет."""
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    resolved = resolve_placeholder_terminal_cwd(
        configured_cwd="auto",
        terminal_backend="docker",
        messaging_cwd=None,
        docker_mount_cwd_to_workspace=False,
        home_fallback=placeholder_home_fallback(),
    )
    assert resolved is None


# ── 4.2: мост CLI ────────────────────────────────────────────────────

def _bridge(defaults: dict) -> str:
    """Исполнить ту же ветку выбора cwd, что и мост конфига в cli.py."""
    terminal_config = dict(defaults.get("terminal") or {})
    placeholders = (".", "auto", "cwd")
    explicit = str(terminal_config.get("cwd") or "").strip()
    has_explicit = bool(explicit) and explicit not in placeholders
    local_cwd = os.getcwd()
    if not os.environ.get("TERMINAL_CWD", "").strip():
        from hermes_constants import ensure_profile_workspace_dir, is_contained
        if is_contained():
            local_cwd = explicit if has_explicit else str(ensure_profile_workspace_dir())
    return local_cwd


def test_cli_local_uses_workspace_when_contained(root, monkeypatch):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    assert _bridge({"terminal": {"cwd": "auto", "env_type": "local"}}) == str(root / "workspace")


def test_cli_local_keeps_getcwd_in_shared_host(root, monkeypatch):
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    assert _bridge({"terminal": {"cwd": "auto", "env_type": "local"}}) == os.getcwd()


def test_cli_explicit_config_path_wins(root, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    assert _bridge({"terminal": {"cwd": str(tmp_path / "p"), "env_type": "local"}}) == str(tmp_path / "p")


def test_cli_explicit_env_var_wins(root, monkeypatch, tmp_path):
    """Аварийный выход для разработчика: TERMINAL_CWD=… hermes."""
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "here"))
    assert _bridge({"terminal": {"cwd": "auto", "env_type": "local"}}) == os.getcwd()
