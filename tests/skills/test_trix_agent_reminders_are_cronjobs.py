"""Напоминание — это задача планировщика, а не `sleep` в песочнице.

Живой прогон 2026-09-08 (стенд trix-testing18, glm-4.6): на «напомни
завтра в 9 утра» агент верно посчитал девять утра в поясе клиента — и
завёл фоновую команду с `sleep` на одиннадцать часов, сказав клиенту «как
наступит время, напишу вам сюда».

Обещание он сдержать не может: контейнер песочницы пересоздаётся, а
машина перезагружается (проверено в тот же день — после перезагрузки
контейнеры не возвращаются). Переживает то и другое только `cronjob`:
задачи лежат файлами в HERMES_HOME.

Модель шла не мимо инструкции, а ПО ней: в справочнике durability-оговорка
разрешала фоновый терминал наравне с планировщиком. Поэтому правится
текст, а не запрет.
"""

from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / \
    "autonomous-ai-agents" / "trix-agent"


@pytest.fixture(scope="module")
def body() -> str:
    return (SKILL / "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def background_reference() -> str:
    return (SKILL / "references" / "background-systems.md").read_text(encoding="utf-8")


def _routing_row(body: str) -> str:
    """Строка таблицы про «вернуться к клиенту позже».

    Ищется по назначению, а не по первым словам: формулировку строки
    расширяли 2026-09-09, и тест, привязанный к её началу, ломался на
    правке текста, ничего не проверив по существу.
    """
    rows = [ln for ln in body.splitlines()
            if ln.startswith("|") and "cronjob" in ln]
    assert len(rows) == 1, f"ожидалась одна строка про планировщик, найдено {len(rows)}"
    return rows[0]


def test_routing_table_forbids_waiting_it_out_in_the_sandbox(body):
    row = _routing_row(body)
    assert "cronjob" in row
    assert "sleep" in row.lower() or "sleeping" in row.lower()


def test_routing_table_says_why_the_timer_fails(body):
    """Запрет без причины модель обходит — причина здесь та же, что в жизни."""
    row = _routing_row(body)
    assert "reboot" in row.lower()


def test_the_trigger_is_the_promise_not_the_word_reminder(body):
    """Прогон 2026-09-09: «запусти задачу на три часа и скажи, когда
    закончит» модель прочла как работу, а не как расписание — правило про
    напоминания не сработало, и она завела `sleep 10800`, пообещав
    написать в 16:16.

    Поэтому признак в тексте должен быть не «это напоминание?», а «я
    только что пообещал вернуться?».
    """
    row = _routing_row(body)
    assert "promised" in row.lower()


def test_reference_repeats_it_where_the_model_actually_reads(background_reference):
    """Прогон показал: модель открывает этот справочник ПЕРЕД тем, как решить."""
    assert "A wait is not a schedule" in background_reference
    assert "cronjob" in background_reference
