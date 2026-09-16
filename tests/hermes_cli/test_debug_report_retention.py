"""Отчёты `/debug` не копятся вечно.

Продуктовое решение 2026-09-07: держим не больше десяти последних отчётов
и ничего старше тридцати дней. Ручки в конфиге нет намеренно — окно,
которое никто не просил настраивать, это ещё одна настройка, которую
придётся объяснять.

Чистка идёт прямо на записи, а не по расписанию: отчёты создаются только
здесь, догонять нечего, а задача по расписанию была бы мертва ровно тогда,
когда машине плохо, — то есть когда отчёты и собирают.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli.debug import (
    _DEBUG_REPORT_KEEP,
    _DEBUG_REPORT_MAX_AGE_DAYS,
    _prune_old_reports,
    _save_report_locally,
)


def _make_report(dir_path: Path, name: str, *, age_days: float = 0.0) -> Path:
    path = dir_path / name
    path.write_text("report body", encoding="utf-8")
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    return path


@pytest.fixture()
def reports_dir(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    d = home / "debug-reports"
    d.mkdir()
    return d


class TestCountCap:
    def test_only_the_newest_survive(self, reports_dir):
        for i in range(_DEBUG_REPORT_KEEP + 5):
            _make_report(reports_dir, f"debug-report-2026090{i % 10}-00000{i}.txt", age_days=i)

        _prune_old_reports(reports_dir)

        left = sorted(p.name for p in reports_dir.glob("debug-report-*.txt"))
        assert len(left) == _DEBUG_REPORT_KEEP
        # Выжили именно свежие: возраст задавался равным индексу.
        ages = sorted((time.time() - p.stat().st_mtime) / 86400 for p in reports_dir.iterdir())
        assert max(ages) < _DEBUG_REPORT_KEEP

    def test_writing_a_report_prunes_the_pile(self, reports_dir):
        for i in range(_DEBUG_REPORT_KEEP + 3):
            _make_report(reports_dir, f"debug-report-old-{i:03d}.txt", age_days=i + 1)

        written = _save_report_locally("fresh report")

        assert written.exists()
        assert len(list(reports_dir.glob("debug-report-*.txt"))) == _DEBUG_REPORT_KEEP


class TestAgeCap:
    def test_old_reports_go_even_when_there_are_few(self, reports_dir):
        stale = _make_report(reports_dir, "debug-report-stale.txt",
                             age_days=_DEBUG_REPORT_MAX_AGE_DAYS + 1)
        fresh = _make_report(reports_dir, "debug-report-fresh.txt", age_days=1)

        removed = _prune_old_reports(reports_dir)

        assert stale not in list(reports_dir.iterdir())
        assert fresh.exists()
        assert removed == [stale]

    def test_a_report_just_under_the_window_stays(self, reports_dir):
        kept = _make_report(reports_dir, "debug-report-borderline.txt",
                            age_days=_DEBUG_REPORT_MAX_AGE_DAYS - 1)

        _prune_old_reports(reports_dir)

        assert kept.exists()


class TestWhatIsNeverTouched:
    def test_the_report_just_written_is_kept_even_if_it_looks_old(self, reports_dir):
        """Часы на машине могут прыгнуть; отчёт, за которым пришёл клиент,
        не должен исчезнуть между записью и отправкой."""
        just_written = _make_report(reports_dir, "debug-report-now.txt",
                                    age_days=_DEBUG_REPORT_MAX_AGE_DAYS + 5)

        _prune_old_reports(reports_dir, keep=just_written)

        assert just_written.exists()

    def test_foreign_files_are_left_alone(self, reports_dir):
        """Чистка знает только своё имя файла: всё, что человек положил в
        каталог руками, — не наше дело."""
        notes = reports_dir / "notes-from-support.txt"
        notes.write_text("клиент прислал вручную", encoding="utf-8")
        os.utime(notes, ((time.time() - 400 * 86400,) * 2))

        _prune_old_reports(reports_dir)

        assert notes.exists()

    def test_a_failing_prune_never_loses_the_report(self, reports_dir, monkeypatch):
        for i in range(_DEBUG_REPORT_KEEP + 2):
            _make_report(reports_dir, f"debug-report-old-{i:03d}.txt", age_days=i + 1)

        def _refuse(self, *a, **kw):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "unlink", _refuse)

        written = _save_report_locally("fresh report")

        assert written.exists()
        assert written.read_text(encoding="utf-8") == "fresh report"
