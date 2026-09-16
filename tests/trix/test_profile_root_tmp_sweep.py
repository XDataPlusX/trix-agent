"""Фаза 5.1–5.2 плана 2026-09-14: уборка временного каталога профиля.

В contained <root>/tmp durable — его никто, кроме нас, не чистит. Тесты
исполняют уборку на настоящем дереве в tmp_path, включая злые случаи с
симлинками наружу.
"""

import os
import time
from pathlib import Path

import pytest

from hermes_cli.trix_profile_tmp import (
    MIN_AGE_SECONDS,
    sweep,
    sweep_config,
    sweep_profile_tmp_if_due,
)

DAY = 86400
NOW = 1_800_000_000.0


def _age(path: Path, seconds: float) -> None:
    stamp = NOW - seconds
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _file(path: Path, size: int = 0, age: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    _age(path, age)
    return path


@pytest.fixture
def root(tmp_path):
    (tmp_path / "tmp").mkdir()
    return tmp_path


def test_old_entries_are_removed(root):
    old = _file(root / "tmp" / "old.bin", size=10, age=30 * DAY)
    result = sweep(root, max_age_days=7, now=NOW)
    assert not old.exists()
    assert result.removed_files == 1
    assert result.freed_bytes == 10


def test_recent_entries_survive(root):
    fresh = _file(root / "tmp" / "fresh.bin", size=10, age=2 * DAY)
    sweep(root, max_age_days=7, now=NOW)
    assert fresh.exists()


def test_entries_younger_than_ten_minutes_are_never_touched(root):
    """Даже «старый» по mtime файл, тронутый минуту назад, неприкосновенен."""
    busy = _file(root / "tmp" / "busy.bin", size=10, age=60)
    sweep(root, max_age_days=0.0001, now=NOW)
    assert busy.exists()


def test_min_age_applies_under_quota_too(root):
    """Квота не имеет права отнять файл у работающей прямо сейчас операции."""
    busy = _file(root / "tmp" / "busy.bin", size=4 * 1024 * 1024, age=MIN_AGE_SECONDS / 2)
    result = sweep(root, max_age_days=0, max_size_mb=1, now=NOW)
    assert busy.exists()
    assert result.removed == 0


def test_quota_removes_oldest_first(root):
    mb = 1024 * 1024
    oldest = _file(root / "tmp" / "a.bin", size=2 * mb, age=5 * DAY)
    middle = _file(root / "tmp" / "b.bin", size=2 * mb, age=3 * DAY)
    newest = _file(root / "tmp" / "c.bin", size=2 * mb, age=1 * DAY)
    result = sweep(root, max_age_days=0, max_size_mb=5, now=NOW)
    assert not oldest.exists()
    assert middle.exists() and newest.exists()
    assert result.remaining_bytes <= 5 * mb


def test_quota_zero_disables_the_cap(root):
    big = _file(root / "tmp" / "big.bin", size=3 * 1024 * 1024, age=DAY)
    sweep(root, max_age_days=0, max_size_mb=0, now=NOW)
    assert big.exists()


def test_age_zero_disables_age_sweep(root):
    """isolation.tmp_max_age_days: 0 — аварийный выключатель без отката кода."""
    old = _file(root / "tmp" / "old.bin", size=10, age=365 * DAY)
    sweep(root, max_age_days=0, max_size_mb=0, now=NOW)
    assert old.exists()


def test_symlink_pointing_outside_is_removed_as_a_link(root, tmp_path):
    """Ссылку удаляем, цель — никогда."""
    victim = tmp_path / "precious.txt"
    victim.write_text("не трогать", encoding="utf-8")
    link = root / "tmp" / "escape"
    link.symlink_to(victim)
    _age(link, 30 * DAY)
    result = sweep(root, max_age_days=7, now=NOW)
    assert not link.is_symlink() and not link.exists()
    assert victim.exists()
    assert victim.read_text(encoding="utf-8") == "не трогать"
    assert result.removed_links == 1


def test_directory_symlink_is_not_descended(root, tmp_path):
    """В каталог по ссылке уборка не спускается и его содержимое не трогает."""
    outside = tmp_path / "outside"
    outside.mkdir()
    inner = outside / "keepme.txt"
    inner.write_text("живое", encoding="utf-8")
    _age(inner, 365 * DAY)
    link = root / "tmp" / "dirlink"
    link.symlink_to(outside, target_is_directory=True)
    _age(link, 30 * DAY)
    sweep(root, max_age_days=7, now=NOW)
    assert inner.exists()
    assert outside.is_dir()


def test_empty_old_directories_are_removed(root):
    old_dir = root / "tmp" / "stale"
    old_dir.mkdir()
    _age(old_dir, 30 * DAY)
    result = sweep(root, max_age_days=7, now=NOW)
    assert not old_dir.exists()
    assert result.removed_dirs == 1


def test_nested_tree_is_removed_bottom_up(root):
    nested = root / "tmp" / "a" / "b" / "c.bin"
    _file(nested, size=5, age=30 * DAY)
    for path in (root / "tmp" / "a" / "b", root / "tmp" / "a"):
        _age(path, 30 * DAY)
    sweep(root, max_age_days=7, now=NOW)
    assert not (root / "tmp" / "a").exists()


def test_nothing_outside_tmp_is_touched(root):
    state = root / "state.db"
    state.write_text("важное", encoding="utf-8")
    _age(state, 365 * DAY)
    home = root / "home" / ".claude"
    home.mkdir(parents=True)
    _age(home, 365 * DAY)
    sweep(root, max_age_days=1, now=NOW)
    assert state.exists()
    assert home.exists()


def test_missing_tmp_is_a_noop(tmp_path):
    result = sweep(tmp_path, max_age_days=7, now=NOW)
    assert result.removed == 0


def test_tmp_that_is_a_symlink_is_refused(tmp_path):
    """Подменённый ссылкой <root>/tmp — повод не убирать, а не убрать чужое."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / "old.bin"
    victim.write_bytes(b"x")
    _age(victim, 365 * DAY)
    root = tmp_path / "root"
    root.mkdir()
    (root / "tmp").symlink_to(elsewhere, target_is_directory=True)
    result = sweep(root, max_age_days=7, now=NOW)
    assert victim.exists()
    assert result.removed == 0


# ── ручки конфига ────────────────────────────────────────────────────

def test_sweep_config_defaults():
    assert sweep_config(None) == (7, 2048)
    assert sweep_config({}) == (7, 2048)
    assert sweep_config({"isolation": {}}) == (7, 2048)


def test_sweep_config_reads_the_section():
    assert sweep_config({"isolation": {"tmp_max_age_days": 3, "tmp_max_size_mb": 512}}) == (3, 512)


def test_sweep_config_survives_garbage():
    assert sweep_config({"isolation": {"tmp_max_age_days": "никогда"}}) == (7, 2048)
    assert sweep_config({"isolation": {"tmp_max_size_mb": -5}}) == (7, 2048)


# ── 5.2: не чаще раза в сутки ────────────────────────────────────────

@pytest.fixture
def contained(monkeypatch, tmp_path):
    import hermes_constants

    profile = tmp_path / "profile"
    (profile / "tmp").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    hermes_constants.reset_isolation_warnings()
    return profile


def test_sweep_is_skipped_in_shared_host(contained, monkeypatch):
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    assert sweep_profile_tmp_if_due({}) is None


def test_first_sweep_runs_and_stamps(contained):
    _file(contained / "tmp" / "old.bin", size=10, age=365 * DAY)
    result = sweep_profile_tmp_if_due({})
    assert result is not None
    assert (contained / "tmp" / ".last-sweep").exists()
    assert not (contained / "tmp" / "old.bin").exists()


def test_second_sweep_within_a_day_is_skipped(contained):
    sweep_profile_tmp_if_due({})
    assert sweep_profile_tmp_if_due({}) is None


def test_sweep_runs_again_after_a_day(contained):
    sweep_profile_tmp_if_due({})
    marker = contained / "tmp" / ".last-sweep"
    stamp = time.time() - 2 * DAY
    os.utime(marker, (stamp, stamp))
    assert sweep_profile_tmp_if_due({}) is not None


def test_force_ignores_the_stamp(contained):
    sweep_profile_tmp_if_due({})
    assert sweep_profile_tmp_if_due({}, force=True) is not None


def test_both_knobs_zero_disables_everything(contained):
    old = _file(contained / "tmp" / "old.bin", size=10, age=365 * DAY)
    cfg = {"isolation": {"tmp_max_age_days": 0, "tmp_max_size_mb": 0}}
    assert sweep_profile_tmp_if_due(cfg) is None
    assert old.exists()


def test_sweep_marker_itself_is_not_swept(contained):
    """Отметка лежит внутри убираемого каталога — она обязана пережить уборку."""
    sweep_profile_tmp_if_due({}, force=True)
    marker = contained / "tmp" / ".last-sweep"
    stamp = time.time() - 365 * DAY
    os.utime(marker, (stamp, stamp))
    sweep_profile_tmp_if_due({}, force=True)
    assert marker.exists()
