"""Плагины памяти и Codex адресуются домом ПРОФИЛЯ, а не домом ОС.

Перевод на ``user_home_path()`` шёл выборочно: honcho и hindsight перевели,
openviking, mem0 и byterover — нет. В contained это значит, что два профиля
на одной машине видят один и тот же конфиг openviking и одну и ту же базу
mem0, а «свой» профиль пишет логины наружу.

Отдельно ``mem0/_oss_providers.py`` зашивал ``~/.hermes/mem0_qdrant``
буквально и игнорировал ``HERMES_HOME`` даже в shared-host — то есть в
Docker-развёртывании база молча уезжала из тома.

Codex был раздвоен: чтение шло по ``user_home_path(".codex")``, запись —
по ``Path.home()/".codex"``. Профиль читал один файл, а правил другой.

shared-host везде обязан отвечать ровно тем же, что и до перевода.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_constants


@pytest.fixture
def arena(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    real_home = tmp_path / "oshome"
    real_home.mkdir()
    root = tmp_path / "profile-root"
    (root / "home").mkdir(parents=True)
    monkeypatch.delenv("HERMES_ISOLATION", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: real_home)
    hermes_constants.reset_isolation_warnings()
    return {"root": root, "home": real_home}


def _contained(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_ISOLATION", "contained")


# ── openviking ──────────────────────────────────────────────────────────

def test_openviking_config_follows_the_profile(arena, monkeypatch) -> None:
    _contained(monkeypatch)
    from plugins.memory.openviking import _resolve_ovcli_config_path

    path = _resolve_ovcli_config_path()

    assert path.is_relative_to(arena["root"]), path


def test_openviking_config_is_unchanged_in_shared_host(arena) -> None:
    from plugins.memory.openviking import _resolve_ovcli_config_path

    assert _resolve_ovcli_config_path() == arena["home"] / ".openviking" / "ovcli.conf"


def test_openviking_explicit_env_still_wins(arena, monkeypatch, tmp_path: Path) -> None:
    _contained(monkeypatch)
    chosen = tmp_path / "chosen.conf"
    monkeypatch.setenv("OPENVIKING_CLI_CONFIG_FILE", str(chosen))
    from plugins.memory.openviking import _resolve_ovcli_config_path

    assert _resolve_ovcli_config_path() == chosen


# ── byterover ───────────────────────────────────────────────────────────

def test_byterover_finds_a_binary_installed_in_the_profile_home(
    arena, monkeypatch
) -> None:
    """``brv``, поставленный агентом, лежит в доме профиля.

    HOME подпроцессов в contained переехал, значит и npm-установка тоже.
    Искать её обязаны там же, куда её кладут.
    """
    _contained(monkeypatch)
    binary = arena["root"] / "home" / ".brv-cli" / "bin" / "brv"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    import plugins.memory.byterover as brv

    monkeypatch.setattr(brv.shutil, "which", lambda _name: None)
    monkeypatch.setattr(brv, "_cached_brv_path", None, raising=False)

    assert brv._resolve_brv_path() == str(binary)


def test_byterover_still_finds_a_binary_in_the_real_home(arena, monkeypatch) -> None:
    """Бинарь — общий машинный ассет, дом ОС остаётся в поиске.

    Установка, сделанная оператором руками до переключения профиля, не
    должна перестать находиться.
    """
    _contained(monkeypatch)
    binary = arena["home"] / ".brv-cli" / "bin" / "brv"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    import plugins.memory.byterover as brv

    monkeypatch.setattr(brv.shutil, "which", lambda _name: None)
    monkeypatch.setattr(brv, "_cached_brv_path", None, raising=False)

    assert brv._resolve_brv_path() == str(binary)


# ── mem0 ────────────────────────────────────────────────────────────────

def test_mem0_qdrant_path_follows_hermes_home(arena) -> None:
    """Даже в shared-host: ``HERMES_HOME`` обязан решать, где база.

    Зашитый ``~/.hermes/mem0_qdrant`` в Docker-развёртывании уводил базу
    из смонтированного тома, никому об этом не сказав.
    """
    from plugins.memory.mem0._oss_providers import qdrant_default_path

    assert qdrant_default_path().is_relative_to(arena["root"])


def test_mem0_qdrant_path_follows_the_profile_when_contained(arena, monkeypatch) -> None:
    _contained(monkeypatch)
    from plugins.memory.mem0._oss_providers import qdrant_default_path

    assert qdrant_default_path().is_relative_to(arena["root"])


def test_mem0_vector_defaults_carry_the_resolved_path(arena) -> None:
    """Таблица провайдеров отдаёт путь, посчитанный сейчас, а не при импорте."""
    from plugins.memory.mem0._oss_providers import vector_default_config

    config = vector_default_config("qdrant")

    assert Path(config["path"]).is_relative_to(arena["root"])


# ── codex ───────────────────────────────────────────────────────────────

def test_codex_migration_writes_where_codex_models_reads(arena, monkeypatch) -> None:
    """Один шов: чтение и запись обязаны указывать на один каталог."""
    _contained(monkeypatch)
    from hermes_cli.codex_runtime_plugin_migration import migrate

    report = migrate({}, dry_run=True, discover_plugins=False)

    assert report.target_path is not None
    assert report.target_path.parent == hermes_constants.user_home_path(".codex")


def test_codex_migration_is_unchanged_in_shared_host(arena) -> None:
    from hermes_cli.codex_runtime_plugin_migration import migrate

    report = migrate({}, dry_run=True, discover_plugins=False)

    assert report.target_path.parent == arena["home"] / ".codex"


def test_codex_explicit_home_still_wins(arena, monkeypatch, tmp_path: Path) -> None:
    _contained(monkeypatch)
    chosen = tmp_path / "elsewhere" / ".codex"
    from hermes_cli.codex_runtime_plugin_migration import migrate

    report = migrate({}, codex_home=chosen, dry_run=True, discover_plugins=False)

    assert report.target_path.parent == chosen
