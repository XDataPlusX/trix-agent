"""Агент не переписывает собственный справочник продукта.

Две вещи по отдельности безобидны, а вместе дают тихую и постоянную
поломку:

1. системный промпт прямо велит: «нашёл в навыке изъян — чини его сразу
   через `skill_manage(action='patch')`»;
2. `hermes update` не трогает навык, который на машине изменён, — он
   остаётся «user-modified (kept)».

Значит, стоит агенту один раз поправить формулировку в `trix-agent` — и
машина навсегда перестаёт получать НАШИ правки именно к тому файлу,
который держит обещания, данные клиенту: не выдавать команд консоли, не
рассказывать про поверхности, которых у клиента нет.

Проверено на живом стенде 2026-09-09: после ручной правки одной строки
`hermes update` отрапортовал «Skills are up to date», оставив старую
копию.

Для ОСТАЛЬНЫХ навыков правка агентом — заявленная возможность продукта,
и она обязана продолжать работать: цена (навык замирает) там окупается
пользой. Здесь не окупается.
"""

import json

import pytest

from tools.skill_manager_tool import (
    PRODUCT_MANUAL_SKILL,
    _product_manual_write_refusal,
)


class TestTheManualRefusesEveryWrite:
    @pytest.mark.parametrize(
        "action", ["edit", "patch", "delete", "write_file", "remove_file"]
    )
    def test_every_write_action_is_refused(self, action):
        refusal = _product_manual_write_refusal(action, PRODUCT_MANUAL_SKILL)
        assert refusal, f"действие {action} не отбито"

    def test_the_refusal_tells_the_agent_not_to_work_around_it(self):
        """Отказ читает МОДЕЛЬ, и от его текста зависит, что будет дальше.

        Прошлый раз, получив невнятный отказ (`secret_request` вне
        шлюза), агент прочёл его как «не тот способ, попробуй иначе» и
        пошёл щупать файловую систему. Здесь обход означал бы, что он
        начнёт править файл через `terminal`.
        """
        refusal = _product_manual_write_refusal("patch", PRODUCT_MANUAL_SKILL)
        low = refusal.lower()
        assert "do not retry" in low
        assert "work around" in low
        # И говорит, что делать вместо: сказать человеку.
        assert "support" in low

    def test_the_refusal_explains_the_consequence_not_just_the_ban(self):
        refusal = _product_manual_write_refusal("edit", PRODUCT_MANUAL_SKILL)
        assert "freeze" in refusal.lower()


class TestEverythingElseKeepsWorking:
    """Запрет узкий: он про один файл, а не про возможность продукта."""

    @pytest.mark.parametrize(
        "action", ["edit", "patch", "delete", "write_file", "remove_file", "create"]
    )
    def test_other_skills_are_untouched(self, action):
        assert _product_manual_write_refusal(action, "github-pr-review") is None

    def test_creating_a_skill_with_that_name_is_not_blocked_here(self):
        """`create` не входит в запрет намеренно.

        Создание — это установка поставки на пустую машину, а не правка
        агентом уже стоящего файла. Блокировать его значило бы сломать
        первую установку ради защиты от правки.
        """
        assert _product_manual_write_refusal("create", PRODUCT_MANUAL_SKILL) is None

    def test_reads_are_not_writes(self):
        for action in ("view", "list", "search"):
            assert _product_manual_write_refusal(action, PRODUCT_MANUAL_SKILL) is None


class TestTheProhibitionIsNotAnApproval:
    """Запрет проверяется раньше обхода одобренной записи.

    Механизм согласования правок умеет «прокатить» уже одобренную
    запись мимо ворот. Если бы запрет стоял после него, достаточно было
    одного одобрения — и справочник всё-таки переписали бы.
    """

    def test_gate_refuses_even_with_the_replay_bypass_set(self):
        from tools import skill_manager_tool as smt

        token = smt._skill_gate_bypass.set(True)
        try:
            out = smt._apply_skill_write_gate("patch", PRODUCT_MANUAL_SKILL)
        finally:
            smt._skill_gate_bypass.reset(token)

        assert out is not None, "обход одобренной записи пропустил правку справочника"
        assert json.loads(out)["success"] is False

    def test_gate_lets_other_skills_through_under_bypass(self):
        from tools import skill_manager_tool as smt

        token = smt._skill_gate_bypass.set(True)
        try:
            assert smt._apply_skill_write_gate("patch", "some-other-skill") is None
        finally:
            smt._skill_gate_bypass.reset(token)
