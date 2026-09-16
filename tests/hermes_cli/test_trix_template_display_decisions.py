"""Решения владельца о том, что клиент видит в чате, живут в шаблоне.

Снято с клиентской машины 81.222.148.68 (2026-09-10). Клиент открыл
Telegram и увидел, что бот печатает свои размышления:

    💭 Reasoning: Requesting link and scope clarification

Раньше такого не было, и никто этого не решал. Причина — не модель и не
правка поведения, а стык двух таблиц умолчаний:

* ``hermes_cli/config_defaults.py`` (``DEFAULT_CONFIG``) обслуживает CLI,
  и там показ размышлений ВКЛЮЧЁН;
* ``gateway/display_config.py`` (``_GLOBAL_DEFAULTS``) обслуживает
  мессенджеры, и там он ВЫКЛЮЧЕН.

Пока клиентский шаблон был дельтой («только наши отклонения»), ключа в
нём не было вовсе, и на машине клиента выигрывал шлюз. Переход на полный
шаблон (коммит 5cb1e612ac) вписал в файл КАЖДЫЙ ключ ``DEFAULT_CONFIG`` —
и клиентская конфигурация впервые получила значение, рассчитанное на CLI.

Ни одно из наших тридцати кураторских решений при этом не потерялось
(проверено сверкой с дельтой до перехода). Изменилось другое: молчание
шаблона перестало означать «пусть решает шлюз».

Поэтому значения, определяющие ЧТО КЛИЕНТ ВИДИТ В ЧАТЕ, обязаны стоять в
шаблоне явно, а не наследоваться. Этот тест держит их за руку: после
слияния с апстримом, которое поменяет ``DEFAULT_CONFIG``, он покраснеет
здесь, а не на машине клиента.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

TEMPLATE = (
    Path(__file__).resolve().parents[2] / "assets" / "config" / "trix-config.yaml"
)

# Решения владельца 2026-09-10. Значение → почему клиенту именно так.
_DISPLAY_DECISIONS = {
    "show_reasoning": (False, "клиент разговаривает с ботом, а не читает его черновик"),
    "tool_progress": ("off", "чат не заполняется отчётом о каждом вызове инструмента"),
    "interim_assistant_messages": (False, "клиент ждёт ответ, а не реплики по дороге"),
    "streaming": (False, "ответ приходит целиком, а не дописывается на глазах"),
    "cleanup_progress": (True, "служебные пузыри убираются после ответа"),
}


@pytest.fixture(scope="module")
def display() -> dict:
    data = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "шаблон перестал быть отображением YAML"
    section = data.get("display")
    assert isinstance(section, dict), "в шаблоне пропала секция display"
    return section


@pytest.mark.parametrize(
    "key,expected,reason",
    [(k, v[0], v[1]) for k, v in _DISPLAY_DECISIONS.items()],
)
def test_display_decision_is_pinned_in_the_template(display, key, expected, reason):
    assert key in display, (
        f"display.{key} исчез из клиентского шаблона. Молчание шаблона больше "
        f"не означает «пусть решает шлюз»: значение подхватится из "
        f"DEFAULT_CONFIG, рассчитанного на CLI. Верните ключ — {reason}."
    )
    assert display[key] == expected, (
        f"display.{key} = {display[key]!r}, а решение владельца — {expected!r} "
        f"({reason}). Если решение изменилось, поменяйте его здесь вместе с "
        f"шаблоном; если значение приехало слиянием с апстримом — верните наше."
    )


def test_reasoning_display_stays_off_however_it_is_spelled(display):
    """Показ размышлений выключен и не включается «сбоку».

    ``reasoning_full`` управляет объёмом того же вывода. Включённый при
    выключенном ``show_reasoning`` он безвреден, но обратное сочетание —
    признак недоведённой правки, и клиент снова увидит черновик.
    """
    if display.get("show_reasoning") is False:
        assert display.get("reasoning_full") is not True, (
            "show_reasoning выключен, а reasoning_full включён — правка "
            "доведена наполовину"
        )
