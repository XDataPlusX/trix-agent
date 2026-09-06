"""Мастер не запускает установку, результат которой нечем проверить.

Требование владельца 2026-09-06: «важно, чтобы он скачался, а не просто
сказал „всё скачано" и поменял, а он не будет работать».

Успех установки определяется не кодом возврата установщика, а проверкой
«появилось ли рабочее» — предикатом из ``_POST_SETUP_READY``, который
сверяется до и после вызова хука. Хук без такого предиката неотличим:
успех и провал возвращают одно и то же.

Конкретный вред, а не чистота: единственный такой хук — ``xai_grok`` —
задаёт вопросы в консоль, а мастер работает без терминала. Клиент,
выбравший эту строку, получал зависание до потолка в 600 секунд и
«установка не удалась» на инструменте, которому установка не нужна вовсе.
"""

import pytest

from hermes_cli.tools_config import _POST_SETUP_READY


def _wizard_hooks() -> set[str]:
    from hermes_cli.setup_wizard.tools_view import wizard_tool_blocks

    hooks = set()
    for block in wizard_tool_blocks():
        for row in block.get("rows") or []:
            key = row.get("post_setup")
            if key:
                hooks.add(key)
    return hooks


def test_the_catalog_actually_has_install_hooks():
    """Страховка от зелёного нуля."""
    assert len(_wizard_hooks()) >= 3


def test_an_unverifiable_hook_is_never_scheduled_for_install():
    """Главная проверка: непроверяемый хук в очередь установки не попадает."""
    from hermes_cli.setup_wizard.app import _pending_tool_installs

    rows = [
        {"name": "Проверяемый", "post_setup": "camofox", "installed": False,
         "backend_key": "camofox"},
        {"name": "Непроверяемый", "post_setup": "нет_такой_проверки",
         "installed": False, "backend_key": "нечто"},
    ]
    picked = _pending_tool_installs(
        {"browser_backend": "camofox", "search_backend": "нечто"}, rows
    )
    names = {r["name"] for r in picked}
    assert "Непроверяемый" not in names


def test_the_known_interactive_hook_is_the_one_this_protects_against():
    """xai_grok задаёт вопросы в консоль, которой у мастера нет."""
    assert "xai_grok" not in _POST_SETUP_READY, (
        "если для xai_grok появилась проверка — этот тест пора переписать, "
        "а строку можно снова пускать в установку"
    )


def test_every_other_hook_the_wizard_offers_can_be_verified():
    """Инвариант: новый хук без проверки — это молчаливый регресс.

    Не «список из N хуков», а отношение: всё, что мастер может запустить,
    обязано иметь способ убедиться, что оно встало.
    """
    unverifiable = {h for h in _wizard_hooks() if h not in _POST_SETUP_READY}
    assert unverifiable <= {"xai_grok"}, (
        f"хуки без проверки после установки: {sorted(unverifiable)}"
    )
