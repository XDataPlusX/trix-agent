"""``hermes disk clean`` и ``<root>/backups`` в contained: кнопка против отката.

В contained дефолт ``hermes backup`` — ``<root>/backups`` (иначе архив со
всем содержимым профиля, включая логины провайдеров, уезжал бы в дом ОС и
одним шагом отменял изоляцию). Ровно этот каталог стоит в списке уборки
``trix_disk`` и вычищался целиком.

Класс дефекта — тихая потеря данных по кнопке «освободить место», и именно
того артефакта, который нужен для отката. Ручной архив клиент делает один
раз, перед обновлением; уборку он жмёт, когда диск забит, — то есть сразу
после того, как архив занял место.

Контракт здесь такой: уборка сносит из ``backups`` только то, что движок
написал сам и сам же прунит по поколениям (``pre-update-*.zip``,
``pre-migration-*.zip``). Всё остальное — архив оператора, дашборда или
вручную положенный файл — остаётся. Незнакомое имя трактуется в пользу
сохранения: цена ошибки несимметрична.

В shared-host не меняется ничего: там дефолт ручного бэкапа — дом ОС, а
``<root>/backups`` держит только служебные точки отката. Это проверяется
здесь же, отдельным тестом, а не обещается.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli.trix_disk import build_report, clean, invalidate_report_cache

MANUAL = "hermes-backup-2026-09-15-000000.zip"
DASHBOARD = "hermes-backup-2026-09-14-120000-ab12cd34.zip"
AUTO_UPDATE = "pre-update-2026-09-01-000000.zip"
AUTO_MIGRATION = "pre-migration-2026-08-01-000000.zip"


def _fill(backups: Path) -> None:
    backups.mkdir(parents=True, exist_ok=True)
    (backups / MANUAL).write_bytes(b"\x00" * 4096)
    (backups / DASHBOARD).write_bytes(b"\x00" * 2048)
    (backups / AUTO_UPDATE).write_bytes(b"\x00" * 8192)
    (backups / AUTO_MIGRATION).write_bytes(b"\x00" * 1024)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "profile"
    _fill(root / "backups")
    (root / "logs").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    invalidate_report_cache()
    return root


@pytest.fixture
def shared_host_home(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "shared"
    _fill(root / "backups")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    invalidate_report_cache()
    return root


def test_clean_keeps_the_manual_backup(home: Path) -> None:
    """Архив ``hermes backup`` переживает «освободить место»."""
    clean(home)

    assert (home / "backups" / MANUAL).exists(), (
        "уборка снесла ручной бэкап — тот самый артефакт, ради которого "
        "клиент его и делал"
    )


def test_clean_keeps_the_dashboard_backup(home: Path) -> None:
    """Архив, заказанный из дашборда, — такой же артефакт оператора."""
    clean(home)

    assert (home / "backups" / DASHBOARD).exists()


def test_clean_keeps_an_unknown_file(home: Path) -> None:
    """Незнакомое имя трактуется в пользу сохранения."""
    stray = home / "backups" / "client-archive-2026.tar"
    stray.write_bytes(b"\x00" * 512)

    clean(home)

    assert stray.exists()


def test_clean_still_removes_engine_restore_points(home: Path) -> None:
    """Служебные точки отката уходят — иначе кнопка бесполезна."""
    result = clean(home)

    assert not (home / "backups" / AUTO_UPDATE).exists()
    assert not (home / "backups" / AUTO_MIGRATION).exists()
    assert result.freed_bytes >= 8192 + 1024
    assert result.errors == [], result.errors


def test_report_promises_only_what_clean_removes(home: Path) -> None:
    """Обещанный объём по метке «бэкапы» равен тому, что реально уйдёт."""
    promised = sum(
        item.bytes
        for item in build_report(home).removable
        if item.path == home / "backups"
    )

    assert promised == 8192 + 1024, promised


def test_backups_directory_itself_survives(home: Path) -> None:
    clean(home)

    assert (home / "backups").is_dir()


def test_shared_host_backups_are_cleared_as_before(shared_host_home: Path) -> None:
    """shared-host не меняется: дефолт ручного бэкапа там — дом ОС."""
    clean(shared_host_home)

    backups = shared_host_home / "backups"
    assert backups.is_dir()
    assert list(backups.iterdir()) == [], "поведение shared-host изменилось"
