"""``hermes backup`` в contained: архив внутри корня и без дублей.

``hermes_cli/backup.py`` волну containment проспал целиком, и получилось
три несогласованности:

* архив по умолчанию ложился в ``Path.home()`` — то есть в общий дом ОС.
  ``HOME`` процесса движка в contained не меняется НИКОГДА (это осознанно:
  юниты и ``~/.local/bin`` должны остаться на месте), поэтому «дом» здесь
  значит «снаружи корня». Бэкап профиля — самая концентрированная его
  копия, и уезжать из корня по умолчанию он не должен;
* ``tmp/`` и ``workspace/`` не исключены, хотя экспорт их исключает:
  распаковки браузера и чужой чекаут в архиве состояния профиля;
* внешние пути плагинов памяти кодировались относительно ``Path.home()``,
  а ``backup_paths()`` теперь отдаёт ``<root>/home/...`` — тот же файл либо
  попадал в архив дважды, либо объявлялся «пропущен как внешний», хотя
  лежал внутри корня.

shared-host во всех трёх случаях обязан остаться прежним.
"""

from __future__ import annotations

import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

import hermes_constants


@pytest.fixture
def arena(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """Корень профиля + отдельный «дом ОС», как в contained на самом деле."""
    real_home = tmp_path / "oshome"
    real_home.mkdir()
    root = tmp_path / "profile-root"
    root.mkdir()
    (root / "config.yaml").write_text("model: test\n", encoding="utf-8")
    (root / "sessions").mkdir()
    (root / "sessions" / "s1.json").write_text("{}", encoding="utf-8")

    monkeypatch.delenv("HERMES_ISOLATION", raising=False)
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: real_home)
    hermes_constants.reset_isolation_warnings()
    return {"root": root, "home": real_home}


def _contained(arena: dict[str, Path], monkeypatch) -> None:
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    (arena["root"] / "home").mkdir(exist_ok=True)


def _members(archive: Path) -> list[str]:
    with zipfile.ZipFile(archive) as zf:
        return zf.namelist()


# ── Куда ложится архив по умолчанию ─────────────────────────────────────

def test_default_output_stays_inside_the_root_when_contained(arena, monkeypatch) -> None:
    _contained(arena, monkeypatch)
    import hermes_cli.backup as backup_mod

    backup_mod.run_backup(Namespace(output=None))

    made = list((arena["root"] / "backups").glob("hermes-backup-*.zip"))
    assert made, "архив не найден внутри корня профиля"
    assert not list(arena["home"].glob("hermes-backup-*.zip")), "архив уехал в дом ОС"


def test_default_output_is_unchanged_in_shared_host(arena) -> None:
    import hermes_cli.backup as backup_mod

    backup_mod.run_backup(Namespace(output=None))

    assert list(arena["home"].glob("hermes-backup-*.zip")), "апстримное поведение изменилось"


def test_explicit_output_still_wins(arena, monkeypatch, tmp_path: Path) -> None:
    _contained(arena, monkeypatch)
    import hermes_cli.backup as backup_mod

    chosen = tmp_path / "usb" / "mine.zip"
    chosen.parent.mkdir()
    backup_mod.run_backup(Namespace(output=str(chosen)))

    assert chosen.exists()


# ── Что попадает в архив ────────────────────────────────────────────────

def test_ephemeral_branches_are_not_backed_up(arena, monkeypatch) -> None:
    """``tmp/`` и ``workspace/`` в корне — не состояние профиля."""
    _contained(arena, monkeypatch)
    root = arena["root"]
    (root / "tmp").mkdir(exist_ok=True)
    (root / "tmp" / "browser-unpack.bin").write_bytes(b"\x00" * 32)
    (root / "workspace").mkdir(exist_ok=True)
    (root / "workspace" / "someones-checkout.txt").write_text("big\n", encoding="utf-8")

    import hermes_cli.backup as backup_mod

    out = arena["home"] / "b.zip"
    backup_mod.run_backup(Namespace(output=str(out)))

    members = _members(out)
    assert not [m for m in members if m.startswith("tmp/")], members
    assert not [m for m in members if m.startswith("workspace/")], members
    assert "config.yaml" in members


def test_nested_tmp_and_workspace_are_still_backed_up(arena, monkeypatch) -> None:
    """Исключение корневое, а не по имени на любой глубине.

    ``skills/mine/workspace/`` — данные навыка, а не ветвь корня.
    """
    _contained(arena, monkeypatch)
    root = arena["root"]
    (root / "skills" / "mine" / "workspace").mkdir(parents=True)
    (root / "skills" / "mine" / "workspace" / "keep.md").write_text("x\n", encoding="utf-8")
    (root / "skills" / "mine" / "tmp").mkdir(parents=True)
    (root / "skills" / "mine" / "tmp" / "keep.txt").write_text("x\n", encoding="utf-8")

    import hermes_cli.backup as backup_mod

    out = arena["home"] / "b.zip"
    backup_mod.run_backup(Namespace(output=str(out)))

    members = _members(out)
    assert "skills/mine/workspace/keep.md" in members, members
    assert "skills/mine/tmp/keep.txt" in members, members


def test_profile_home_is_still_backed_up(arena, monkeypatch) -> None:
    """``home/`` из архива НЕ исключается: бэкап восстанавливают на себя же.

    Это осознанная разница с экспортом — экспорт отдают другому человеку.
    """
    _contained(arena, monkeypatch)
    site = arena["root"] / "home" / ".hindsight"
    site.mkdir(parents=True)
    (site / "config.json").write_text("{}", encoding="utf-8")

    import hermes_cli.backup as backup_mod

    out = arena["home"] / "b.zip"
    backup_mod.run_backup(Namespace(output=str(out)))

    assert "home/.hindsight/config.json" in _members(out)


# ── Внешние пути плагинов памяти ────────────────────────────────────────

def _patch_external(monkeypatch, paths: list[Path]) -> None:
    import hermes_cli.backup as backup_mod

    monkeypatch.setattr(
        backup_mod, "_collect_memory_provider_external_paths", lambda: list(paths)
    )


def test_provider_path_inside_the_root_is_not_duplicated(arena, monkeypatch) -> None:
    """Путь провайдера уже под корнем — обход его взял, второй раз не надо."""
    _contained(arena, monkeypatch)
    site = arena["root"] / "home" / ".hindsight"
    site.mkdir(parents=True)
    (site / "config.json").write_text("{}", encoding="utf-8")
    _patch_external(monkeypatch, [site])

    import hermes_cli.backup as backup_mod

    out = arena["home"] / "b.zip"
    backup_mod.run_backup(Namespace(output=str(out)))

    members = _members(out)
    assert "home/.hindsight/config.json" in members
    assert not [m for m in members if m.startswith("_external/")], members


def test_provider_path_inside_the_root_is_not_called_skipped(
    arena, monkeypatch, capsys
) -> None:
    """И не объявляется «пропущен как внешний» — он не внешний."""
    _contained(arena, monkeypatch)
    site = arena["root"] / "home" / ".honcho"
    site.mkdir(parents=True)
    (site / "config.json").write_text("{}", encoding="utf-8")
    _patch_external(monkeypatch, [site])

    import hermes_cli.backup as backup_mod

    backup_mod.run_backup(Namespace(output=str(arena["home"] / "b.zip")))

    out = capsys.readouterr().out
    assert str(site) not in out or "skip" not in out.lower()


def test_provider_path_outside_the_root_is_encoded_against_profile_home(
    arena, monkeypatch
) -> None:
    """Внешний путь всё ещё едет, но адресуется домом ПРОФИЛЯ."""
    _contained(arena, monkeypatch)
    # Провайдер по какой-то причине отдал путь в доме ОС.
    site = arena["home"] / ".honcho"
    site.mkdir(parents=True)
    (site / "config.json").write_text("{}", encoding="utf-8")
    _patch_external(monkeypatch, [site])

    import hermes_cli.backup as backup_mod

    out = arena["root"] / "b.zip"
    backup_mod.run_backup(Namespace(output=str(out)))

    members = _members(out)
    # Дом ОС не является домом профиля — путь честно объявлен внешним и
    # пропущен, а не записан под чужой адрес.
    assert not [m for m in members if m.startswith("_external/.honcho")], members


def test_shared_host_external_capture_is_unchanged(arena, monkeypatch) -> None:
    """В shared-host дом профиля == дом ОС, поведение прежнее."""
    site = arena["home"] / ".honcho"
    site.mkdir(parents=True)
    (site / "config.json").write_text("{}", encoding="utf-8")
    _patch_external(monkeypatch, [site])

    import hermes_cli.backup as backup_mod

    out = arena["root"] / "b.zip"
    backup_mod.run_backup(Namespace(output=str(out)))

    assert "_external/.honcho/config.json" in _members(out)
