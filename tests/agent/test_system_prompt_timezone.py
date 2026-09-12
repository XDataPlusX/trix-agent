"""Часовой пояс клиента виден модели — иначе она спрашивает то, что уже задано.

Вопрос владельца 2026-09-08: «мы же ставим timezone в мастере настройки,
зачем он спрашивает про часовой пояс?». Прогон на стенде показал, что
спрашивать ему было неоткуда узнать:

* в промпте стояла только дата («Conversation started: Tuesday,
  September 08, 2026») — намеренно без времени, ради стабильности кэша, и
  без названия пояса;
* на вопрос про время промпт отправляет в терминал (`date`), а терминал —
  это docker-песочница, и её часы идут по UTC (проверено на машине:
  внутри 18:56 UTC, на хосте 23:56 +05, в конфиге Europe/Moscow).

То есть настройка клиента до модели не доезжала вовсе. Поэтому пояс
называется прямо, вместе с оговоркой про часы песочницы: иначе модель
берёт время из `date` и ставит напоминание на чужой час.
"""

import importlib.util
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent.parent


def _make_agent(**overrides):
    spec = importlib.util.spec_from_file_location(
        "_sysprompt_helper", ROOT / "tests" / "agent" / "test_system_prompt.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper._make_agent(**overrides)


def _prompt(agent) -> str:
    from agent.system_prompt import build_system_prompt_parts

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        parts = build_system_prompt_parts(agent)
    return "\n".join(
        str(v) for v in (parts.values() if isinstance(parts, dict) else [parts])
    )


def test_configured_timezone_is_named_in_the_prompt():
    with patch("hermes_time.get_timezone", return_value=ZoneInfo("Europe/Moscow")):
        prompt = _prompt(_make_agent(platform="telegram"))

    assert "Timezone: Europe/Moscow" in prompt


def test_prompt_warns_that_the_sandbox_clock_is_not_that_zone():
    """Без оговорки модель сверяется с `date` в песочнице и берёт UTC."""
    with patch("hermes_time.get_timezone", return_value=ZoneInfo("Europe/Moscow")):
        prompt = _prompt(_make_agent(platform="telegram"))

    line = next(ln for ln in prompt.splitlines() if ln.startswith("Timezone:"))
    assert "UTC" in line and "sandbox" in line.lower()


def test_no_configured_timezone_promises_nothing():
    """Пояс не задан — строки нет: обещать серверный пояс как клиентский нельзя."""
    with patch("hermes_time.get_timezone", return_value=None):
        prompt = _prompt(_make_agent(platform="telegram"))

    assert "Timezone:" not in prompt
