"""Статические строки продукта не зовут клиента в консоль и не называют апстрим.

Найдено на клиентской установке 2026-09-10. Клиент выбрал модель Codex и
получил в Telegram английское сообщение с командой
``hermes config set …`` — при том, что его единственный интерфейс Telegram,
консоли у него нет, а имя апстрима в клиентском канале звучать не должно.

Починив ту строку, прогнали каталог целиком — она оказалась не одна.
``/restart`` советовал «перезапустите из консоли командой
``hermes gateway restart``», ``/update`` при ненайденном исполняемом файле —
«выполните ``hermes update`` вручную в терминале». Обе команды у клиента
есть (``CLIENT_MENU_COMMANDS``), и вторая приходит ровно тогда, когда
что-то сломалось и человеку хуже всего.

**Почему главный приёмочный тест этого не ловил.** Он стережёт системный
промпт, схемы инструментов, скиллы и подсказку про удалённый терминал.
Каталог локализации — отдельный канал доставки: ответы слэш-команд,
уведомления шлюза, сообщения о состоянии. Сторожа у него не было.

**Список перечисляет РАЗРЕШЁННОЕ, а не запрещённое** — то же решение
владельца, что и в ``hermes_cli/trix_menu.py``: список запретов проигрывает
по устройству, потому что всё, чего в нём нет, разрешено по умолчанию, и
каждое слияние с апстримом молча расширяет набор. Здесь наоборот: новая
строка с упоминанием апстрима красит сюиту и требует явного решения — либо
переписать её, либо доказать, что она консольная, и внести в
:data:`CONSOLE_ONLY_KEYS`.

Каталоги ``ru`` и ``en`` — наши; остальные пятнадцать достались от
апстрима и на клиентский путь не попадают (``display.language: ru``).
Когда продукт заговорит на третьем языке, его каталог добавится сюда.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]

# Каталоги, которые ведём мы. en — источник истины, ru — то, что читает клиент.
OUR_CATALOGS = ("en", "ru")

# Строки, которые печатает КОНСОЛЬ и только она. Здесь имя команды правдиво:
# человек уже стоит в терминале, и `hermes` — то, что он наберёт.
#
# Каждая запись — обещание, что строка на клиентский путь не попадает.
# Проверять это автоматически нечем (у каталога нет отметки канала), поэтому
# рядом стоит модуль, который её печатает: следующий читающий сверит сам.
CONSOLE_ONLY_KEYS: dict[str, str] = {
    "trix.bundles.none": "hermes_cli/slash_exec.py — наборы навыков в TUI",
    "trix.egress.next_setup": "hermes_cli/proxy_cli.py — настройка исходящего прокси",
    "trix.egress.next_start": "hermes_cli/proxy_cli.py — запуск исходящего прокси",
    "trix.cmd.write_approval.cli_only_hint": (
        "hermes_cli/write_approval_commands.py — в самом имени ключа стоит cli_only"
    ),
}

# То, что клиенту слышать нельзя: имя апстрима в любом виде — как команда
# (`hermes update`), как путь (`~/.hermes/logs/…`) или просто словом.
_FORBIDDEN = ("hermes",)


def _flatten(node, prefix: tuple = ()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _flatten(value, prefix + (str(key),))
    elif isinstance(node, str):
        yield ".".join(prefix), node


def _catalog(lang: str) -> dict:
    data = yaml.safe_load((REPO / "locales" / f"{lang}.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"каталог {lang} перестал быть отображением"
    return data


@pytest.mark.parametrize("lang", OUR_CATALOGS)
def test_no_client_facing_string_names_the_upstream(lang):
    offenders = []
    for key, text in _flatten(_catalog(lang)):
        if key in CONSOLE_ONLY_KEYS:
            continue
        lowered = text.lower()
        if any(word in lowered for word in _FORBIDDEN):
            offenders.append((key, text.strip()[:160]))

    assert not offenders, (
        f"каталог {lang}: строки называют апстрим клиенту.\n"
        + "\n".join(f"  {k}\n    {v}" for k, v in offenders)
        + "\n\nПерепишите строку — либо, если её печатает ТОЛЬКО консоль, "
        "внесите ключ в CONSOLE_ONLY_KEYS вместе с модулем, который её печатает."
    )


@pytest.mark.parametrize("lang", OUR_CATALOGS)
def test_the_console_only_allowlist_stays_honest(lang):
    """Разрешение, выданное исчезнувшему ключу, — мусор, который однажды
    прикроет собой настоящую утечку под тем же именем."""
    keys = {key for key, _ in _flatten(_catalog(lang))}
    stale = sorted(k for k in CONSOLE_ONLY_KEYS if k not in keys)
    assert not stale, (
        f"каталог {lang}: в CONSOLE_ONLY_KEYS числятся ключи, которых больше "
        f"нет: {stale}. Уберите их из списка."
    )


def test_both_catalogs_carry_the_same_keys():
    """Ключ, потерявшийся в ru, вернул бы клиенту английский текст: `t()`
    падает в en, когда перевода нет."""
    en = {key for key, _ in _flatten(_catalog("en"))}
    ru = {key for key, _ in _flatten(_catalog("ru"))}
    missing = sorted(en - ru)
    assert not missing, (
        "ключи есть в en и потерялись в ru — клиент получит их по-английски: "
        f"{missing[:20]}"
    )
