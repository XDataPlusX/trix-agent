"""Промпт называет провайдера так, как его назвал клиенту мастер настройки.

Найдено живым прогоном 2026-09-08 на стенде trix-testing18: на вопрос «кто
ты» агент в одном ответе из трёх выдавал клиенту `zai-coding-plan` —
внутренний идентификатор профиля, которого клиент нигде не вводил и не
видел. Правило «идентификаторы профилей не называть» в промпте уже стояло,
и всё равно не держало.

Причина не в силе правила, а в том, что рядом лежал сам факт: строка
`Provider: zai-coding-plan`. Это четвёртый случай одного и того же на
проекте — конкретика в промпте бьёт запрет в промпте. Поэтому лечится не
усилением запрета, а тем, что называть нечего: в строке стоит витринное
имя провайдера (`Z.AI (Coding Plan)`), то самое, которое клиент выбирал в
мастере.
"""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

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


def test_profile_id_never_reaches_the_prompt():
    """Клиент выбирал «Z.AI (Coding Plan)» — его и видит модель."""
    prompt = _prompt(_make_agent(provider="zai-coding-plan", model="glm-4.6",
                                 platform="telegram"))

    assert "Provider: Z.AI (Coding Plan)" in prompt
    assert "zai-coding-plan" not in prompt


def test_model_name_still_reaches_the_prompt():
    """Имя модели — факт клиента: он её выбрал и платит за неё."""
    prompt = _prompt(_make_agent(provider="zai-coding-plan", model="glm-4.6",
                                 platform="telegram"))

    assert "Model: glm-4.6" in prompt


def test_unknown_provider_keeps_its_own_name():
    """У провайдера без профиля витринного имени нет — строка не пропадает."""
    prompt = _prompt(_make_agent(provider="самодельный-relay", model="m",
                                 platform="telegram"))

    assert "Provider: самодельный-relay" in prompt


@pytest.mark.parametrize("empty", ["", None])
def test_no_provider_no_line(empty):
    prompt = _prompt(_make_agent(provider=empty, model="m", platform="telegram"))

    assert "Provider:" not in prompt
