"""Каждый инструмент клиентского набора имеет русскую формулировку.

Инвариант, а не снимок: набор инструментов берётся из того же места,
откуда его берёт продукт — шаблон конфига → ``_get_platform_tools``
(имена ТУЛСЕТОВ) → ``toolsets.resolve_toolset`` (развёрнутые имена
ОТДЕЛЬНЫХ ИНСТРУМЕНТОВ). Разворачивать обязательно: ``current_tool`` в
рантайме (``agent._current_tool``) хранит имя вызванного инструмента
(``read_file``, ``browser_snapshot``), а не имя тулсета (``file``,
``browser``), в который он входит. Проверка на уровне тулсетов ловит
только "забыли весь тулсет"; проверка на уровне инструментов дополнительно
ловит "тулсет знаком, а конкретный инструмент внутри него — нет" —
именно это раньше проходило тест молча и показывало клиенту сырое имя
вроде ``browser_snapshot``.
"""

from pathlib import Path

import yaml

from hermes_cli.tools_config import _get_platform_tools
from hermes_cli.trix_tool_names import russian_tool_action
from toolsets import resolve_toolset

REPO_ROOT = Path(__file__).resolve().parents[2]


def _client_tools():
    """Развёрнутые имена инструментов клиентского телеграм-профиля.

    ``resolve_toolset`` не принимает список — резолвим по одному имени
    тулсета за вызов и объединяем результаты.
    """
    template = yaml.safe_load(
        (REPO_ROOT / "assets" / "config" / "trix-config.yaml").read_text(encoding="utf-8")
    )
    client_toolsets = _get_platform_tools(template, "telegram")
    tool_names = set()
    for toolset_name in client_toolsets:
        tool_names.update(resolve_toolset(toolset_name))
    return tool_names


def test_every_client_tool_has_a_russian_action():
    missing = [name for name in _client_tools() if not russian_tool_action(name)]
    assert not missing, (
        f"нет русской формулировки для инструментов: {sorted(missing)} — "
        "клиент увидит сырое английское имя в строке ожидания"
    )


def test_unknown_tool_returns_none_rather_than_a_guess():
    assert russian_tool_action("some_future_tool") is None


def test_batch_tool_string_resolves_to_the_first_known_action():
    """agent._current_tool becomes ", ".join(names) for a concurrent
    tool call (agent/tool_executor.py:876,964) -- a direct dict lookup
    on the joined string never matches a single tool name, so without
    splitting it every batched turn would silently lose the deed."""
    assert russian_tool_action("read_file, web_search") == "читаю файл"
    assert russian_tool_action("web_search, read_file") == "ищу в интернете"


def test_batch_tool_string_with_no_known_names_returns_none():
    assert russian_tool_action("some_future_tool, another_future_tool") is None


def test_actions_are_lowercase_verb_phrases():
    """Строка склеивается как '⏳ 3 мин — {действие}', поэтому фраза
    начинается со строчной буквы и не содержит точки.

    Дыры в словаре (действие отсутствует) — забота соседнего теста
    ``test_every_client_tool_has_a_russian_action``, а не этого: здесь
    пропускаем ``None``, чтобы падение на реальной дыре выглядело как
    осмысленное сообщение оттуда, а не как ``TypeError:
    'NoneType' object is not subscriptable`` из этого теста.
    """
    for name in _client_tools():
        action = russian_tool_action(name)
        if action is None:
            continue
        assert action[0].islower(), f"{name}: {action!r} начинается с заглавной"
        assert not action.endswith("."), f"{name}: {action!r} оканчивается точкой"
