"""Проверка доктора: numpy на этой машине запускается.

Живёт в своём модуле, а в `doctor.py` уходит один вызов — по той же
причине, что `trix_support.py` и `trix_setup_service_check.py`: доктор
это ~3300 строк, которые мы регулярно тянем сверху, и каждая наша
функция внутри него оплачивается конфликтом при мёрже.

**Почему проверки в установочном хуке оказалось мало.** Восстановление
numpy стоит в `_run_post_setup` и срабатывает после установки
инструмента. Но хук не запускается, когда пакет УЖЕ стоит — а numpy к
этому моменту мог сломать кто-то другой. Ровно это и наблюдалось на
живой машине 2026-09-05: клиент выбрал Piper повторно, пакет был на
месте, хук пропустили, numpy остался сломанным, и озвучка молчала.

Состояние машины чинит доктор — это его работа. Поэтому проверка здесь:
`hermes doctor` называет поломку, `hermes doctor --fix` её устраняет, а
проход поддержки, который зовёт обе команды подряд, чинит клиенту молча
и без чьего-либо участия.

Молчим, когда numpy не установлен вовсе: это законное состояние машины,
где голосовая часть не используется, а не неполадка.
"""

from __future__ import annotations


def check_numpy_runnable(issues: list, should_fix: bool = False) -> bool:
    """Проверить (и по просьбе починить) работоспособность numpy.

    Возвращает True, если что-то было починено, — вызывающий доктор
    считает по этому свои `fixed_count`.
    """
    from hermes_cli.doctor import _section, check_fail, check_ok, check_warn
    from hermes_cli.trix_numpy_guard import _numpy_imports, _numpy_present, ensure_runnable_numpy

    if not _numpy_present():
        return False

    _section("NumPy")

    if _numpy_imports():
        check_ok("numpy импортируется", "(голосовые инструменты смогут им пользоваться)")
        return False

    if not should_fix:
        check_fail(
            "numpy установлен, но не импортируется",
            "(колёса 2.x требуют процессор x86-64-v2; без него молча "
            "отваливаются распознавание речи и локальные голоса)",
        )
        issues.append(
            "Голосовые функции не работают из-за несовместимой версии numpy — "
            "запустите 'hermes doctor --fix'"
        )
        return False

    result = ensure_runnable_numpy()
    if result.ok:
        check_ok(f"numpy починен: {result.message}")
        return True

    check_warn("numpy починить не удалось", f"({result.message})")
    issues.append(
        "Голосовые функции не работают: numpy на этой машине не запускается. "
        "Обратитесь в поддержку."
    )
    return False
