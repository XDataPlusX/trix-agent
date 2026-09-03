"""Разбор вывода ``scripts/run_tests.sh`` для гейта релиза
(``scripts/release_trix.sh``).

Раньше вся эта логика была одной строкой grep в bash:

    grep -oE 'FAILED [^ ]+' | sed 's/^FAILED //' | sort -u

Она вынимала строки ``FAILED <node-id>`` из ЦЕЛОГО вывода прогона, не
различая, откуда они взялись. Два источника внутри этого вывода ломают
гейт, если их не отличать:

1. **Раннер повторяет упавший файл один раз** (``scripts/run_tests.sh``,
   флаг ``--file-retries``, по умолчанию 1). Если файл падает на первой
   попытке и проходит на второй, раннер сам объявляет его FLAKY и печатает
   раздел "=== ⚠ N FLAKY files ..." — с первой попыткой целиком, включая
   её строки FAILED, но только как документацию нестабильности, НЕ как
   провал. Раннер уже признал этот файл успешным, повторив его: считать ту
   же попытку падением задним числом значит сделать гейт непроходимым на
   любой занятой машине, где параллельному прогону хоть раз попался
   таймингово-чувствительный тест.
2. **Файлы из FLAKY_NEVER_RECORD** (см. ниже) recorder осознанно никогда
   не пишет в baseline. Раз их там нет, при сравнении с текущим прогоном
   они могут появиться только как "новое падение" — даже когда падают
   штатно и известно нестабильны.

Оба случая — вопрос разбора текста с реальной структурой (где начинается
и заканчивается секция FLAKY), а не однострочный grep. Поэтому разбор
живёт здесь, в питоне, а bash остаётся тонким вызывающим слоем — так же,
как hermes_cli/release_tree.py уже вынес сборку витрины из bash.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

# Файлы, которые baseline-recorder ОСОЗНАННО никогда не записывает —
# таймингово-чувствительные тесты, падающие только под нагрузкой полного
# параллельного прогона и проходящие в одиночку. Запись их в baseline
# научила бы гейт прощать НАСТОЯЩЕЕ падение в том же файле навсегда — то
# есть источник этого списка не совпадает с источником пункта 1 выше
# (тот — про ретрай раннера, этот — про сознательный отказ от учёта), но
# оба должны исключаться из "новых падений" по одной и той же причине:
# гейт сравнивает текущий прогон с тем, что было ЗАПИСАНО, а этих файлов
# в записи нет и не будет.
#
# Список живёт ЗДЕСЬ и больше нигде. scripts/release_trix.sh раньше носил
# собственную копию этого перечня (использовалась и для сравнения "новых
# падений", и для комментария в самом файле baseline) — два места означают,
# что рано или поздно они разойдутся молча. Теперь скрипт вызывает
# ``python -m hermes_cli.release_gate --never-record-list``, чтобы получить
# этот же список для заголовка baseline-файла, вместо второй копии в bash.
FLAKY_NEVER_RECORD: tuple[str, ...] = (
    "tests/agent/lsp/test_client_e2e.py",
    "tests/tui_gateway/test_server.py",
    "tests/tools/test_read_special_file_guard.py",
)

_FAILED_RE = re.compile(r"FAILED (\S+)")
# Заголовок верхнеуровневой секции вывода раннера, например
# "=== Summary: ... ===", "=== ⚠ 2 FLAKY files ... ===",
# "=== Failure output ===", "=== 3 files with test failures ... ===".
#
# Требуем РОВНО три "=" и пробел с каждой стороны ("=== "/" ==="). pytest
# сам печатает похожие на вид разделители внутри своего вывода —
# "=================================== FAILURES ===================================",
# "========================= 1 failed, 4 passed in 1.02s ==========================" —
# но у них четвёртый символ тоже "=", без пробела сразу после тройки.
# Без этого условия такая строка pytest внутри первой попытки FLAKY-файла
# ошибочно закрывала бы секцию раньше времени, и её же FAILED-строка
# снова считалась бы настоящим падением — то есть в точности тот баг,
# который этот модуль должен устранить.
_SECTION_HEADER_RE = re.compile(r"^=== \S.*\S ===$")
_FLAKY_HEADER_RE = re.compile(r"^=== \S.*FLAKY files.*===$")


@dataclass
class ParsedRun:
    """Результат разбора одного прогона ``scripts/run_tests.sh``."""

    # Настоящие падения — то, что действительно должно попасть в
    # сравнение с baseline. Отсортировано, без повторов.
    real_failures: list[str] = field(default_factory=list)
    # Node-id, встреченные ТОЛЬКО внутри секции FLAKY (первая попытка
    # файла, который затем прошёл на повторе) — отброшены как флак.
    dropped_flaky: list[str] = field(default_factory=list)
    # Node-id, отброшенные потому что их файл входит в FLAKY_NEVER_RECORD.
    dropped_never_record: list[str] = field(default_factory=list)


def _flaky_section_span(lines: list[str]) -> tuple[int, int] | None:
    """Границы секции FLAKY: (первая строка, первая строка ПОСЛЕ неё).

    Секция начинается со строки вида "=== ⚠ N FLAKY files ... ===" и
    заканчивается перед следующим верхнеуровневым заголовком "=== ... ==="
    (на практике это "=== Per-file subprocess time distribution ===" или
    "=== Failure output ==="), либо концом вывода, если такого заголовка
    нет. Внутри этой границы могут быть строки FAILED для НЕСКОЛЬКИХ
    флак-файлов сразу — все они относятся к первым попыткам, которые
    раннер сам не засчитал как падение (флак по определению — файл, для
    которого ПОЛНЫЙ повтор прошёл целиком), так что искать конкретное имя
    файла внутри секции не нужно: всё, что здесь помечено FAILED,
    относится к попытке, отменённой последующим успехом.
    """
    start = None
    for i, line in enumerate(lines):
        if _FLAKY_HEADER_RE.match(line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if _SECTION_HEADER_RE.match(lines[i]):
            end = i
            break
    return start, end


def _is_never_record(node_id: str) -> bool:
    return any(
        node_id == f or node_id.startswith(f + "::") for f in FLAKY_NEVER_RECORD
    )


def parse_run(output: str) -> ParsedRun:
    """Разобрать полный текстовый вывод ``scripts/run_tests.sh``.

    Возвращает настоящие падения отдельно от того, что было отброшено (и
    почему) — чтобы вызывающая сторона (bash-скрипт) могла честно
    сообщить человеку, сколько записей она проглотила.
    """
    lines = output.splitlines()
    span = _flaky_section_span(lines)

    real: set[str] = set()
    flaky: set[str] = set()
    for i, line in enumerate(lines):
        m = _FAILED_RE.search(line)
        if not m:
            continue
        node_id = m.group(1)
        if span is not None and span[0] <= i < span[1]:
            flaky.add(node_id)
        else:
            real.add(node_id)

    never_record = {n for n in real if _is_never_record(n)}
    real -= never_record

    return ParsedRun(
        real_failures=sorted(real),
        dropped_flaky=sorted(flaky),
        dropped_never_record=sorted(never_record),
    )


def parse_real_failures(output: str) -> list[str]:
    """Удобный short-hand: только итоговый список настоящих падений."""
    return parse_run(output).real_failures


def _cli(argv: list[str] | None = None) -> int:
    """``python3 -m hermes_cli.release_gate --parse-failures`` читает
    вывод раннера со stdin, печатает настоящие падения (по одному на
    строку) в stdout и сводку отброшенного в stderr.

    ``python3 -m hermes_cli.release_gate --never-record-list`` печатает
    FLAKY_NEVER_RECORD (по одному файлу на строку) — единственный
    источник этого списка для bash-стороны, чтобы он не дублировался
    во второй копии внутри release_trix.sh.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="hermes_cli.release_gate")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--parse-failures", action="store_true")
    group.add_argument("--never-record-list", action="store_true")
    args = parser.parse_args(argv)

    if args.never_record_list:
        for f in FLAKY_NEVER_RECORD:
            print(f)
        return 0

    output = sys.stdin.read()
    parsed = parse_run(output)
    for node_id in parsed.real_failures:
        print(node_id)
    print(
        "release_gate: отброшено как флак (прошли на повторе): "
        f"{len(parsed.dropped_flaky)}; как никогда-не-записывать: "
        f"{len(parsed.dropped_never_record)}"
        + (
            f" ({', '.join(parsed.dropped_never_record)})"
            if parsed.dropped_never_record
            else ""
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
