"""RAF-158: MCP-подпроцесс обязан идти через ту же точку containment-env.

План 2026-09-14 §2.5 и docstring `apply_subprocess_containment_env` называют
её «единственной точкой, через которую движок собирает окружение дочерних
процессов», и перечисляют шесть конструкторов. Пользовательские MCP-серверы
в этот перечень не входили: stdio-спавн собирает окружение отдельной функцией
`_build_safe_env`, которая копирует `HOME`/`TMPDIR`/`XDG_*` из окружения
самого движка. А дом движка в contained намеренно НЕ переезжает — значит
MCP-сервер получал настоящий дом ОС и раскладывал состояние мимо корня
профиля.

Тесты ниже фиксируют обе стороны: contained — внутри корня, shared-host —
без изменений, и явный `env:` оператора по-прежнему сильнее пина.
"""

import os

import pytest

import hermes_constants
from tools.mcp_tool import _build_safe_env


@pytest.fixture
def root(monkeypatch, tmp_path):
    """Окружение движка в момент спавна MCP: дом ОС снаружи корня профиля."""
    profile = tmp_path / "profile"
    profile.mkdir()
    real_home = tmp_path / "realhome"
    real_home.mkdir()
    monkeypatch.setattr(hermes_constants, "is_container", lambda: False)
    monkeypatch.setattr(hermes_constants, "_iter_machine_browser_roots", lambda env=None: [])
    for var in ("HERMES_ISOLATION", "HERMES_TMP_DIR", "TERMINAL_HOME_MODE",
                "TERMINAL_CWD", "HERMES_REAL_HOME", "PLAYWRIGHT_BROWSERS_PATH",
                "AGENT_BROWSER_EXECUTABLE_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("XDG_CACHE_HOME", str(real_home / ".cache"))
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    hermes_constants.reset_isolation_warnings()
    return profile


def _contained(monkeypatch):
    monkeypatch.setenv("HERMES_ISOLATION", "contained")


def test_contained_mcp_home_is_inside_profile_root(root, monkeypatch):
    """Главное: сервер не должен писать в настоящий дом ОС."""
    _contained(monkeypatch)
    env = _build_safe_env(None)
    assert env["HOME"] == str(root / "home")


def test_contained_mcp_tmp_and_xdg_follow_the_root(root, monkeypatch):
    """TMPDIR и XDG обязаны ехать за HOME, иначе состояние разъедется надвое."""
    _contained(monkeypatch)
    env = _build_safe_env(None)
    home = root / "home"
    assert env["TMPDIR"] == str(root / "tmp")
    assert env["XDG_CACHE_HOME"] == str(home / ".cache")
    assert env["XDG_CONFIG_HOME"] == str(home / ".config")
    assert env["XDG_DATA_HOME"] == str(home / ".local" / "share")
    assert env["XDG_STATE_HOME"] == str(home / ".local" / "state")


def test_contained_mcp_gets_the_way_back(root, monkeypatch, tmp_path):
    """HERMES_REAL_HOME — тот же контракт, что у остальных подпроцессов."""
    _contained(monkeypatch)
    env = _build_safe_env(None)
    assert env["HERMES_REAL_HOME"] == str(tmp_path / "realhome")


def test_contained_mcp_matches_the_browser_subprocess(root, monkeypatch):
    """Оба подпроцесса обязаны видеть один и тот же дом.

    Браузер ходит через `hermes_subprocess_env`, MCP — через `_build_safe_env`.
    Расхождение между ними и было дефектом.
    """
    _contained(monkeypatch)
    from tools.environments.local import hermes_subprocess_env

    browser = hermes_subprocess_env(inherit_credentials=False)
    mcp = _build_safe_env(None)
    assert mcp["HOME"] == browser["HOME"]
    assert mcp["TMPDIR"] == browser["TMPDIR"]


def test_operator_env_block_still_wins(root, monkeypatch, tmp_path):
    """Явный `env:` в конфиге сервера сильнее пина — как и до правки."""
    _contained(monkeypatch)
    custom = str(tmp_path / "chosen-home")
    env = _build_safe_env({"HOME": custom})
    assert env["HOME"] == custom


def test_shared_host_home_unchanged(root, monkeypatch, tmp_path):
    """В shared-host дом подпроцесса остаётся настоящим домом ОС."""
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    env = _build_safe_env(None)
    assert env["HOME"] == str(tmp_path / "realhome")
    assert env["TMPDIR"] == "/tmp"


def test_a_broken_pin_stops_the_spawn_instead_of_leaking(root, monkeypatch):
    """Fail-closed: сбой контракта не должен давать сервер с утёкшим домом.

    Охрана вокруг вызова означала бы, что при любой поломке контракта сервер
    стартует ровно с тем дефектом, который правка чинит. Остальные шесть
    конструкторов зовут контракт без охраны — MCP не исключение.
    """
    _contained(monkeypatch)

    def _boom(env):
        raise RuntimeError("контракт сломан")

    monkeypatch.setattr(
        hermes_constants, "apply_subprocess_containment_env", _boom
    )
    with pytest.raises(RuntimeError):
        _build_safe_env(None)


def test_secrets_are_still_not_leaked(root, monkeypatch):
    """Пин не должен расширять перечень: ключи в MCP-подпроцесс не уезжают."""
    _contained(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-travel")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_travel")
    env = _build_safe_env(None)
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
