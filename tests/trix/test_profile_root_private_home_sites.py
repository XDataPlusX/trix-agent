"""Фаза 3 плана 2026-09-14: приватные ``~``-сайты движка едут в корень.

Что здесь доказывается. Учётные данные провайдеров, состояние плагинов
памяти и ключи песочниц внешние CLI исторически держат под ``~``. Это
данные ОДНОГО профиля: в ``contained`` они обязаны уехать под
``<root>/home``, иначе два профиля на машине видят чужие логины. В
``shared-host`` каждый такой путь обязан остаться ровно там, где его
оставил апстрим.

Тесты исполняют резолверы, а не читают исходники, и работают на фальшивых
файлах в ``tmp_path``: содержимое настоящих учётных данных здесь не
читается и не печатается никогда.
"""

import json
from pathlib import Path

import pytest

import hermes_constants


@pytest.fixture
def homes(monkeypatch, tmp_path):
    """Настоящий дом ОС и корень профиля — разные каталоги, оба пустые."""
    real_home = tmp_path / "realhome"
    real_home.mkdir()
    root = tmp_path / "profile"
    (root / "home").mkdir(parents=True)
    monkeypatch.setattr(hermes_constants, "is_container", lambda: False)
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(root))
    for var in ("TERMINAL_HOME_MODE", "HERMES_REAL_HOME", "CODEX_HOME",
                "CUA_DRIVER_RS_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    hermes_constants.reset_isolation_warnings()
    return real_home, root


def _mode(monkeypatch, mode: str):
    monkeypatch.setenv("HERMES_ISOLATION", mode)
    hermes_constants.reset_isolation_warnings()


# ── Сайты: (имя, вызов, относительный путь под домом) ──────────────────
#
# Каждый элемент — НАСТОЯЩИЙ резолвер из продакшн-кода. Список намеренно
# плоский: когда апстрим добавит ещё один ``Path.home()``, сторож фазы 7
# укажет на файл, а сюда допишут строку.

def _claude_credentials() -> Path:
    import agent.anthropic_adapter as mod

    return Path(mod.user_home_path(".claude", ".credentials.json"))


def _qwen() -> Path:
    from hermes_cli.auth import _qwen_cli_auth_path

    return _qwen_cli_auth_path()


def _honcho_global() -> Path:
    from plugins.memory.honcho.client import resolve_global_config_path

    return resolve_global_config_path()


def _hindsight_legacy() -> Path:
    from hermes_constants import user_home_path

    return user_home_path(".hindsight", "config.json")


def _cua_home() -> Path:
    from hermes_cli.tools_config import _cua_install_home

    return _cua_install_home()


SITES = [
    ("claude credentials", _claude_credentials, (".claude", ".credentials.json")),
    ("qwen oauth", _qwen, (".qwen", "oauth_creds.json")),
    ("honcho global config", _honcho_global, (".honcho", "config.json")),
    ("hindsight legacy config", _hindsight_legacy, (".hindsight", "config.json")),
    ("cua driver home", _cua_home, (".cua-driver",)),
]


@pytest.mark.parametrize("name,resolve,rel", SITES, ids=[s[0] for s in SITES])
def test_site_lands_under_the_profile_root_in_contained(homes, monkeypatch, name, resolve, rel):
    _real_home, root = homes
    _mode(monkeypatch, "contained")
    got = resolve()
    assert got == root / "home" / Path(*rel), name
    assert got.resolve().is_relative_to(root.resolve()), name


@pytest.mark.parametrize("name,resolve,rel", SITES, ids=[s[0] for s in SITES])
def test_site_keeps_upstream_path_in_shared_host(homes, monkeypatch, name, resolve, rel):
    real_home, root = homes
    _mode(monkeypatch, "shared-host")
    got = resolve()
    assert got == real_home / Path(*rel), name
    assert not got.resolve().is_relative_to(root.resolve()), name


# ── Поведение, а не только путь ────────────────────────────────────────


def test_claude_credentials_are_read_from_the_profile_not_the_os_home(homes, monkeypatch):
    """Два дома, два разных файла — contained обязан взять профильный.

    Содержимое — выдуманное, настоящие учётные данные здесь не участвуют.
    """
    import agent.anthropic_adapter as mod

    real_home, root = homes
    for base, token in ((real_home, "токен-хоста"), (root / "home", "токен-профиля")):
        path = base / ".claude" / ".credentials.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": token,
                                          "scopes": ["user:inference"]}}),
            encoding="utf-8",
        )

    _mode(monkeypatch, "contained")
    got = mod._read_claude_code_credentials_from_file()
    assert got is not None and got["accessToken"] == "токен-профиля"

    _mode(monkeypatch, "shared-host")
    got = mod._read_claude_code_credentials_from_file()
    assert got is not None and got["accessToken"] == "токен-хоста"


def test_codex_tokens_are_read_from_the_profile_not_the_os_home(homes, monkeypatch):
    from hermes_cli.auth import _import_codex_cli_tokens

    real_home, root = homes
    for base, token in ((real_home, "хост"), (root / "home", "профиль")):
        path = base / ".codex" / "auth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"tokens": {"access_token": token, "refresh_token": f"r-{token}",
                                   "account_id": "acc"}}),
            encoding="utf-8",
        )

    _mode(monkeypatch, "contained")
    assert (_import_codex_cli_tokens() or {}).get("access_token") == "профиль"

    _mode(monkeypatch, "shared-host")
    assert (_import_codex_cli_tokens() or {}).get("access_token") == "хост"


def test_explicit_codex_home_still_wins_in_both_modes(homes, monkeypatch):
    """``CODEX_HOME`` — решение оператора; корень его не отменяет."""
    from hermes_cli.auth import _import_codex_cli_tokens

    _real_home, _root = homes
    elsewhere = _root.parent / "явный-codex"
    elsewhere.mkdir()
    (elsewhere / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "явный", "refresh_token": "r",
                               "account_id": "acc"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(elsewhere))
    for mode in ("contained", "shared-host"):
        _mode(monkeypatch, mode)
        assert (_import_codex_cli_tokens() or {}).get("access_token") == "явный", mode


def test_modal_credentials_are_looked_up_per_profile(homes, monkeypatch):
    from tools.tool_backend_helpers import has_direct_modal_credentials

    real_home, root = homes
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    (real_home / ".modal.toml").write_text("# фальшивка\n", encoding="utf-8")

    _mode(monkeypatch, "contained")
    assert has_direct_modal_credentials() is False, "профиль не должен видеть ключ хоста"

    (root / "home" / ".modal.toml").write_text("# фальшивка\n", encoding="utf-8")
    assert has_direct_modal_credentials() is True

    _mode(monkeypatch, "shared-host")
    assert has_direct_modal_credentials() is True


def test_downloads_fall_back_to_the_workspace_in_contained(homes, monkeypatch):
    """``~/Downloads`` нет → в contained приземляемся в workspace, не в cwd."""
    import tools.flux3_video_tool as mod

    _real_home, root = homes
    # Не платформенная доставка: ветка кэша видео здесь не при чём.
    monkeypatch.setattr(mod, "_delivers_as_an_attachment", lambda: False)

    _mode(monkeypatch, "contained")
    assert mod._default_directory() == root / "workspace"

    (root / "home" / "Downloads").mkdir()
    assert mod._default_directory() == root / "home" / "Downloads"


def test_credential_fingerprint_follows_the_profile(homes, monkeypatch):
    """Ключ кэша обязан протухать от СВОИХ логинов, а не от чужих."""
    from hermes_cli.models import _credential_fingerprint

    real_home, root = homes
    _mode(monkeypatch, "contained")
    before = _credential_fingerprint("anthropic")

    host_cred = real_home / ".claude" / ".credentials.json"
    host_cred.parent.mkdir(parents=True, exist_ok=True)
    host_cred.write_text("{}", encoding="utf-8")
    assert _credential_fingerprint("anthropic") == before, "чужой логин не трогает ключ"

    mine = root / "home" / ".claude" / ".credentials.json"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("{}", encoding="utf-8")
    assert _credential_fingerprint("anthropic") != before, "свой логин обязан протухнуть"


# ── Граница: интерфейсы ОС НЕ переезжают ───────────────────────────────


def test_os_runtime_sites_stay_on_the_real_home(homes, monkeypatch):
    """Реестр команд ОС — не данные профиля; contained его не двигает."""
    from hermes_cli.profiles import _get_wrapper_dir

    real_home, root = homes
    _mode(monkeypatch, "contained")
    assert _get_wrapper_dir() == real_home / ".local" / "bin"
    assert not _get_wrapper_dir().is_relative_to(root)


def test_the_allowlist_is_the_only_written_answer(homes):
    """Оба класса непусты и не пересекаются — сторож фазы 7 читает их отсюда."""
    shared = {p for p, _ in hermes_constants.PROFILE_ROOT_SHARED_IMMUTABLE}
    runtime = {p for p, _ in hermes_constants.PROFILE_ROOT_OS_RUNTIME}
    assert shared and runtime
    assert not (shared & runtime)
    assert all(reason.strip() for _, reason in
               hermes_constants.PROFILE_ROOT_SHARED_IMMUTABLE
               + hermes_constants.PROFILE_ROOT_OS_RUNTIME)
