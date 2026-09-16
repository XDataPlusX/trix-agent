"""``hermes disk clean`` и ``<root>/tmp``: два контракта на один каталог.

``trix_profile_tmp`` строит уборку профильного TMPDIR вокруг одного
обещания: **записи моложе ``MIN_AGE_SECONDS`` неприкосновенны**, потому что
их почти наверняка держит работающая операция. ``trix_disk.clean()`` при
этом внесла ``tmp`` в список удаляемого и сносила его содержимое целиком,
без возрастной защиты.

Стреляет это не в теории: клиент на забитом диске жмёт «освободить место»
ровно тогда, когда что-то тяжёлое качается или распаковывается — то есть
когда в ``tmp`` лежит самый нужный ему файл. Результат — не освобождённые
мегабайты, а невоспроизводимая ошибка.

Здесь проверяется молодой файл. Остальные каталоги уборки — логи, кэши —
обязаны чиститься как прежде: у них такого контракта нет. Второе (и
единственное) исключение появилось позже и живёт в своём файле:
``<root>/backups`` в contained, где лежит артефакт отката —
``test_profile_root_disk_clean_backups.py``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli.trix_disk import clean
from hermes_cli.trix_profile_tmp import MIN_AGE_SECONDS


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "profile"
    (root / "tmp").mkdir(parents=True)
    (root / "logs").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    return root


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_clean_keeps_a_young_temp_file(home: Path) -> None:
    """Файл, которому минута, переживает «освободить место»."""
    young = home / "tmp" / "download-in-progress.bin"
    young.write_bytes(b"\x00" * 4096)
    _age(young, 60)

    clean(home)

    assert young.exists(), "уборка снесла файл, который держит работающая операция"


def test_clean_keeps_a_young_file_inside_a_young_directory(home: Path) -> None:
    """Защита не обходится вложенностью."""
    nested = home / "tmp" / "browser-unpack" / "chunk.bin"
    nested.parent.mkdir()
    nested.write_bytes(b"\x00" * 2048)
    _age(nested, 30)
    _age(nested.parent, 30)

    clean(home)

    assert nested.exists()


def test_clean_still_removes_old_temp_files(home: Path) -> None:
    """Старое по-прежнему уходит — иначе команда бесполезна."""
    old = home / "tmp" / "yesterday.bin"
    old.write_bytes(b"\x00" * 8192)
    _age(old, MIN_AGE_SECONDS * 10)

    result = clean(home)

    assert not old.exists()
    assert result.freed_bytes >= 8192


def test_clean_reports_freed_space_for_tmp(home: Path) -> None:
    """Метка ``tmp`` попадает в отчёт только когда что-то реально ушло."""
    old = home / "tmp" / "old.bin"
    old.write_bytes(b"\x00" * 16384)
    _age(old, MIN_AGE_SECONDS * 10)

    result = clean(home)

    assert result.removed_labels, "уборка ничего не сообщила об освобождённом"


def test_young_tmp_file_is_not_reported_as_an_error(home: Path) -> None:
    """Пощажённый молодой файл — не отказ уборки.

    Каталог, в котором он лежит, не опустеет — и это штатный исход, а не
    то, о чём надо сообщать клиенту.
    """
    young = home / "tmp" / "keep" / "now.bin"
    young.parent.mkdir()
    young.write_bytes(b"\x00" * 128)
    _age(young, 10)
    _age(young.parent, 10)

    result = clean(home)

    assert result.errors == [], result.errors


def test_other_dirs_are_still_cleared_completely(home: Path) -> None:
    """Возрастная защита — только у ``tmp``, и больше нигде."""
    fresh_log = home / "logs" / "gateway.log"
    fresh_log.write_text("сейчас\n", encoding="utf-8")
    _age(fresh_log, 1)

    clean(home)

    assert not fresh_log.exists(), "журналы должны чиститься как раньше"


def test_tmp_directory_itself_survives(home: Path) -> None:
    """Каталог остаётся: по нему направлен TMPDIR работающего процесса."""
    old = home / "tmp" / "old.bin"
    old.write_bytes(b"\x00" * 1024)
    _age(old, MIN_AGE_SECONDS * 10)

    clean(home)

    assert (home / "tmp").is_dir()


def test_clean_does_not_follow_a_symlink_out_of_tmp(home: Path, tmp_path: Path) -> None:
    """Ссылка наружу удаляется как ссылка, её цель остаётся."""
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("не трогать\n", encoding="utf-8")
    link = home / "tmp" / "escape"
    link.symlink_to(outside, target_is_directory=True)
    _age(link, MIN_AGE_SECONDS * 10)

    clean(home)

    assert (outside / "keep.txt").exists()
