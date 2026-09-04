"""Тесты для hermes_cli/release_gate.py — разбор вывода scripts/run_tests.sh
для гейта релиза (scripts/release_trix.sh).

Раньше `collect_failures` в release_trix.sh вынимала регуляркой ВСЕ строки
`FAILED <node-id>` из целого вывода прогона. Это ловило два ложных
"новых падения":

1. Файл, упавший на первой попытке и прошедший на повторе (раннер
   печатает первую попытку целиком в разделе FLAKY, чтобы её видел
   человек) — раннер считает файл успешным, а старый grep всё равно
   вытаскивал FAILED-строки из этой первой попытки.
2. Файл из FLAKY_NEVER_RECORD — базовая линия сознательно никогда его не
   записывает, значит при сравнении он может появиться только как "новое
   падение", даже если падает штатно и известно нестабилен.

Эти тесты собирают собственные (не взятые из реального прогона) образцы
вывода раннера и проверяют, что модуль отбрасывает оба случая, но не
глушит настоящие падения.
"""
from __future__ import annotations

import subprocess
import sys

from hermes_cli.release_gate import FLAKY_NEVER_RECORD, parse_real_failures, parse_run


def _wrap_run(*sections: str) -> str:
    """Склеить кусочки вывода раннера в одну строку, как это делает bash
    `scripts/run_tests.sh 2>&1`."""
    return "\n".join(sections) + "\n"


SUMMARY_LINE = "=== Summary: 3 files, 10 tests passed, 3 failed, 0 skipped (100% complete) in 12.3s (4 workers) ==="

# Раздел с первой попыткой файла, который упал раз и прошёл на повторе —
# ровно то, что печатает раннер для FLAKY-файла.
FLAKY_SECTION = """=== ⚠ 1 FLAKY files (failed once, passed on retry — fix these) ===
  tests/hermes_cli/test_web_server.py
⚠ FLAKY: failed on attempt 1, passed on retry (attempt 2). Fix the flake — do not ignore this.
--- first-attempt output ---
tests/hermes_cli/test_web_server.py ..F.. [100%]
=================================== FAILURES ===================================
_ TestX.test_flaky_thing _
=========================== short test summary info ============================
FAILED tests/hermes_cli/test_web_server.py::TestX::test_flaky_thing
========================= 1 failed, 4 passed in 1.02s ==========================

--- retry output ---
tests/hermes_cli/test_web_server.py ..... [100%]
======================== 5 passed in 0.91s =========================="""

# Раздел итоговых репро-блоков для настоящих падений (боксовый вывод
# run_tests_parallel.py — строки начинаются с "  ║ ").
FAILURE_OUTPUT_HEADER = "=== Failure output ==="

REAL_FAILURE_BLOCK = """  ╔══ tests/tools/test_real_bug.py ══
  ║ =========================== short test summary info ============================
  ║ FAILED tests/tools/test_real_bug.py::test_something_broken
  ║ ========================= 1 failed, 3 passed in 0.5s ==========================
  ║
  ║  Repro: python -m pytest tests/tools/test_real_bug.py
  ╚══"""

# Файл из FLAKY_NEVER_RECORD, упавший штатно (не в разделе FLAKY — он
# падает каждый раз, повтор не спасает).
NEVER_RECORD_FAILURE_BLOCK = """  ╔══ tests/tools/test_read_special_file_guard.py ══
  ║ =========================== short test summary info ============================
  ║ FAILED tests/tools/test_read_special_file_guard.py::TestReadFileToolFifoGuard::test_fifo_read_returns_note_instantly
  ║ ========================= 1 failed, 8 passed in 12.25s =========================
  ║
  ║  Repro: python -m pytest tests/tools/test_read_special_file_guard.py
  ╚══"""

FINAL_SUMMARY = "=== 2 files with test failures (2 tests failed) ==="


def test_flaky_file_that_passed_on_retry_is_not_a_failure():
    """Файл, упавший на попытке 1 и прошедший на попытке 2, раннер уже
    признал успешным — его строки FAILED из первой попытки не считаются."""
    output = _wrap_run(SUMMARY_LINE, FLAKY_SECTION, FAILURE_OUTPUT_HEADER, FINAL_SUMMARY)
    assert parse_real_failures(output) == []


def test_file_that_failed_on_retry_too_is_a_real_failure():
    """Файл, упавший и на повторе, никогда не попадает в раздел FLAKY —
    его падение остаётся в итоговом списке."""
    output = _wrap_run(
        SUMMARY_LINE, FAILURE_OUTPUT_HEADER, REAL_FAILURE_BLOCK, FINAL_SUMMARY
    )
    assert parse_real_failures(output) == [
        "tests/tools/test_real_bug.py::test_something_broken"
    ]


def test_never_record_file_is_dropped_even_when_it_fails_normally():
    """Файл из FLAKY_NEVER_RECORD не должен превращаться в "новое падение"
    просто потому, что baseline сознательно никогда его не записывает."""
    assert "tests/tools/test_read_special_file_guard.py" in FLAKY_NEVER_RECORD
    output = _wrap_run(
        SUMMARY_LINE, FAILURE_OUTPUT_HEADER, NEVER_RECORD_FAILURE_BLOCK, FINAL_SUMMARY
    )
    assert parse_real_failures(output) == []


def test_ordinary_failure_is_kept():
    """Обычное падение вне FLAKY и вне FLAKY_NEVER_RECORD — считается."""
    output = _wrap_run(
        SUMMARY_LINE, FAILURE_OUTPUT_HEADER, REAL_FAILURE_BLOCK, FINAL_SUMMARY
    )
    failures = parse_real_failures(output)
    assert "tests/tools/test_real_bug.py::test_something_broken" in failures


def test_empty_output_yields_empty_list():
    assert parse_real_failures("") == []
    assert parse_real_failures("\n\n") == []


def test_mixed_run_keeps_only_the_genuine_failure():
    """Комбинированный прогон: один флак-успех-на-повторе, один
    никогда-не-записываемый, одно настоящее падение — в списке должно
    остаться только настоящее."""
    output = _wrap_run(
        SUMMARY_LINE,
        FLAKY_SECTION,
        FAILURE_OUTPUT_HEADER,
        REAL_FAILURE_BLOCK,
        NEVER_RECORD_FAILURE_BLOCK,
        FINAL_SUMMARY,
    )
    assert parse_real_failures(output) == [
        "tests/tools/test_real_bug.py::test_something_broken"
    ]


def test_parse_run_reports_how_much_it_dropped_and_why():
    """scripts/release_trix.sh обязан говорить человеку, сколько записей
    оно проглотило и по какой причине — не просто молча ужать список."""
    output = _wrap_run(
        SUMMARY_LINE,
        FLAKY_SECTION,
        FAILURE_OUTPUT_HEADER,
        REAL_FAILURE_BLOCK,
        NEVER_RECORD_FAILURE_BLOCK,
        FINAL_SUMMARY,
    )
    parsed = parse_run(output)
    assert parsed.real_failures == ["tests/tools/test_real_bug.py::test_something_broken"]
    assert parsed.dropped_flaky == [
        "tests/hermes_cli/test_web_server.py::TestX::test_flaky_thing"
    ]
    assert parsed.dropped_never_record == [
        "tests/tools/test_read_special_file_guard.py"
        "::TestReadFileToolFifoGuard::test_fifo_read_returns_note_instantly"
    ]


def test_cli_parse_failures_prints_dropped_counts_to_stderr():
    """Это ровно та команда, которую зовёт scripts/release_trix.sh
    (`"$RELEASE_PY" -m hermes_cli.release_gate --parse-failures`). Гейт не
    имеет права молча проглатывать отброшенные записи — проверяем, что
    сводка реально печатается, а не только что функция её возвращает."""
    output = _wrap_run(
        SUMMARY_LINE,
        FLAKY_SECTION,
        FAILURE_OUTPUT_HEADER,
        REAL_FAILURE_BLOCK,
        NEVER_RECORD_FAILURE_BLOCK,
        FINAL_SUMMARY,
    )
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.release_gate", "--parse-failures"],
        input=output,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().splitlines() == [
        "tests/tools/test_real_bug.py::test_something_broken"
    ]
    assert "отброшено как флак" in result.stderr
    assert "1" in result.stderr  # один флак, один never-record
    assert "test_read_special_file_guard.py" in result.stderr


def test_cli_never_record_list_matches_module_constant():
    """bash получает список FLAKY_NEVER_RECORD ровно отсюда — единственный
    источник, никакой второй копии в scripts/release_trix.sh."""
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.release_gate", "--never-record-list"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == list(FLAKY_NEVER_RECORD)


def test_result_is_sorted_and_deduplicated():
    dup_block = """  ╔══ tests/x.py ══
  ║ FAILED tests/tools/test_real_bug.py::test_something_broken
  ║ FAILED tests/aaa/test_first.py::test_a
  ║ FAILED tests/aaa/test_first.py::test_a
  ╚══"""
    output = _wrap_run(SUMMARY_LINE, FAILURE_OUTPUT_HEADER, dup_block, FINAL_SUMMARY)
    assert parse_real_failures(output) == [
        "tests/aaa/test_first.py::test_a",
        "tests/tools/test_real_bug.py::test_something_broken",
    ]


# ---------------------------------------------------------------------------
# Файл, который не отработал ни одним тестом (parse_uncollected).
#
# Найдено 2026-09-04 на приёмочном прогоне спеки 16. Раннер сводит в одну
# секцию два разных случая — сорванный импорт и файловый таймаут, — и общее
# у них опасное: НИ ОДНОЙ строки FAILED такой файл не даёт. Значит сверка с
# базовой линией показывает совпадение, хотя целый файл тестов не
# выполнился. Замер: tests/hermes_cli/test_doctor.py прошёл за 51 секунду в
# одном полном прогоне и был убит на 600 секундах в другом; в одиночку
# проходит целиком, 63 из 63.
#
# Путь --record-baseline это уже проверял; релизный путь не проверял никто.
# ---------------------------------------------------------------------------

NO_TESTS_SECTION = """=== 2 files where no tests ran (collection/import error, timeout before collection, etc.) ===
  tests/hermes_cli/test_doctor.py
  tests/test_mcp_tool.py
"""

# Строка прогресса убитого по таймауту файла: от настоящего падения одним
# шаблоном она неотличима, поэтому разбор идёт по секции сводки.
TIMED_OUT_PROGRESS = (
    "[100.0% | 28514/~28514 | ✓32690 | ✗   40] "
    "✗ tests/hermes_cli/test_doctor.py (56 tests, 600.1s)"
)


def test_uncollected_files_are_reported():
    from hermes_cli.release_gate import parse_uncollected

    out = _wrap_run(TIMED_OUT_PROGRESS, "", NO_TESTS_SECTION, SUMMARY_LINE)
    assert parse_uncollected(out) == [
        "tests/hermes_cli/test_doctor.py",
        "tests/test_mcp_tool.py",
    ]


def test_uncollected_file_produces_no_failure_lines():
    """Суть дефекта: тот же вывод не даёт гейту ни одного падения.

    Без этого утверждения починка выглядит избыточной — «ну и что, оно же
    и так упало». Не упало: сверка с базовой линией видит пустоту.
    """
    from hermes_cli.release_gate import parse_uncollected

    out = _wrap_run(TIMED_OUT_PROGRESS, "", NO_TESTS_SECTION, SUMMARY_LINE)
    assert parse_real_failures(out) == []
    assert parse_uncollected(out) != []


def test_clean_run_reports_nothing_uncollected():
    from hermes_cli.release_gate import parse_uncollected

    assert parse_uncollected(_wrap_run(SUMMARY_LINE)) == []
    assert parse_uncollected("") == []


def test_uncollected_section_does_not_swallow_the_next_section():
    """Секция читается по границам, а не «до конца вывода»."""
    from hermes_cli.release_gate import parse_uncollected

    out = _wrap_run(
        NO_TESTS_SECTION.rstrip("\n"),
        "=== 1 file with test failures (1 test failed) ===",
        "  tests/other/test_thing.py  (1 test failed)",
        SUMMARY_LINE,
    )
    assert parse_uncollected(out) == [
        "tests/hermes_cli/test_doctor.py",
        "tests/test_mcp_tool.py",
    ]


def test_cli_parse_uncollected_matches_the_function():
    out = _wrap_run(TIMED_OUT_PROGRESS, "", NO_TESTS_SECTION, SUMMARY_LINE)
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.release_gate", "--parse-uncollected"],
        input=out,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == [
        "tests/hermes_cli/test_doctor.py",
        "tests/test_mcp_tool.py",
    ]
