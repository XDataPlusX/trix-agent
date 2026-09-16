"""Отказ механизма ключей обязан называть себя.

Наблюдение за живым агентом 2026-09-06: получив немое "unsupported",
модель бросала механизм и уходила искать CLI по файловой системе — пять
лишних вызовов и совет клиенту выполнить команду в шелле, которого у
него нет. Ни строки в логе при этом не появлялось.
"""

import logging

import pytest

import tools.secret_capture_gateway as scg


@pytest.fixture(autouse=True)
def clean_registry():
    scg._notify_cbs.clear()
    yield
    scg._notify_cbs.clear()


class TestRefusalIsLoud:
    def test_missing_notify_is_logged_with_both_sides(self, caplog):
        """В логе должен быть и запрошенный ключ, и то, что зарегистрировано —
        иначе расхождение ключей остаётся невидимым."""
        scg.register_notify("telegram:someone-else", lambda e: None)

        with caplog.at_level(logging.WARNING, logger=scg.logger.name):
            res = scg.request_secret("telegram:me", "SOME_KEY", "зачем")

        assert res["skipped"] is True
        text = caplog.text
        assert "telegram:me" in text
        assert "telegram:someone-else" in text

    def test_reason_names_the_actual_cause(self):
        """«unsupported» читается как «платформа не умеет» — а причина в том,
        что для ЭТОЙ сессии не зарегистрирован приёмник."""
        res = scg.request_secret("telegram:me", "SOME_KEY", "зачем")
        assert res["reason"] == "no_capture_registered"

    def test_result_tells_the_model_to_stop_not_to_improvise(self):
        """Ровно то поведение, которое наблюдалось и стоило пяти вызовов."""
        res = scg.request_secret("telegram:me", "SOME_KEY", "зачем")
        msg = (res.get("message") or "").lower()
        assert "do not" in msg or "do not search" in msg
        assert "cli" in msg

    def test_never_carries_a_value(self):
        res = scg.request_secret("telegram:me", "SOME_KEY", "зачем")
        assert "value" not in res
        assert res["stored_as"] == "SOME_KEY"


class TestSubagentCannotAskTheClientForAKey:
    def test_secret_request_is_blocked_for_delegated_children(self):
        """Ребёнок наследует ключ сессии родителя, поэтому его запрос всплыл
        бы в чате клиента из безнадзорного работника."""
        from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS

        assert "secret_request" in DELEGATE_BLOCKED_TOOLS

    def test_blocked_for_the_same_reason_as_clarify(self):
        """Обе — обращение к человеку. Если clarify запрещён, а этот нет,
        значит про него просто забыли."""
        from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS

        assert "clarify" in DELEGATE_BLOCKED_TOOLS
