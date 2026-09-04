"""Тесты цепочки запасных поисковиков (``web.search_backend`` как список).

Контекст: раньше поиск знал ровно один бэкенд, и его падение убивало
web_search целиком — прямой ответ на вопрос владельца «сегодня Bing
работает, завтра нет». ``web.search_backend`` теперь принимает и строку
(как раньше — единственный провайдер, без изменений), и список — цепочку
попыток в заданном порядке. Первый успешный (``success: true`` и НЕПУСТОЙ
результат — см. ``_reject_empty_search_success``) выигрывает.

Реализация: ``tools/web_tools.py::_run_search_backend_chain`` — включается,
только когда ``web.search_backend`` в конфиге — реально list/tuple; обычная
строка идёт по старому, ни на бит не изменённому пути (see
``web_search_tool``).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

import tools.web_tools as wt


class FakeProvider:
    """Минимальный двойник WebSearchProvider для теста цепочки.

    Записывает каждый вызов ``search()`` в ``self.calls`` — этим и
    проверяется «недоступный провайдер пропускается БЕЗ попытки»: если его
    ``search`` не звали, список пуст.
    """

    def __init__(
        self,
        name: str,
        *,
        result: Optional[Dict[str, Any]] = None,
        raises: Optional[BaseException] = None,
        on_call=None,
        supports_search: bool = True,
    ) -> None:
        self.name = name
        self._result = result
        self._raises = raises
        self._on_call = on_call
        self._supports_search = supports_search
        self.calls: List[tuple] = []

    def supports_search(self) -> bool:
        return self._supports_search

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        self.calls.append((query, limit))
        if self._on_call is not None:
            self._on_call()
        if self._raises is not None:
            raise self._raises
        return self._result


def _ok(title: str = "t", url: str = "u") -> Dict[str, Any]:
    return {"success": True, "data": {"web": [{"title": title, "url": url}]}}


def _empty_success() -> Dict[str, Any]:
    return {"success": True, "data": {"web": []}}


def _error(msg: str) -> Dict[str, Any]:
    return {"success": False, "error": msg}


def _patch_common(monkeypatch, providers: Dict[str, FakeProvider], *, available=None) -> None:
    """Общая обвязка, повторяющаяся в каждом тесте цепочки.

    ``available`` — callable(name) -> bool; по умолчанию все доступны, так
    как большинство тестов проверяют не availability-гейт, а сам обход
    цепочки/бюджет/пометку ответившего.
    """
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
    monkeypatch.setattr(
        "agent.web_search_registry.get_provider",
        lambda name: providers.get(name),
    )
    if available is None:
        monkeypatch.setattr(wt, "_is_backend_available", lambda name: True)
    else:
        monkeypatch.setattr(wt, "_is_backend_available", available)
    monkeypatch.setattr(wt, "_ensure_web_plugins_loaded", lambda: None)


class TestStringConfigIsUnchanged:
    """Требование 1: строка в web.search_backend — обратная совместимость."""

    def test_plain_string_calls_the_single_provider_directly(self, monkeypatch):
        good = FakeProvider("tavily", result=_ok())
        monkeypatch.setattr(wt, "_load_web_config", lambda: {"search_backend": "tavily"})
        _patch_common(monkeypatch, {"tavily": good})

        result = json.loads(wt.web_search_tool("query", limit=3))

        assert result["success"] is True
        assert good.calls == [("query", 3)]
        # Одиночная строка никогда не идёт через цепочку — "answered_by"
        # это маркер fallback-пути, тут ему взяться неоткуда.
        assert "answered_by" not in result["data"]

    def test_empty_string_falls_back_exactly_like_before(self, monkeypatch):
        """web.search_backend: "" — не список, ветка цепочки не включается
        вовсе (fallback_chain остаётся []), путь идёт через старый
        _get_search_backend()/_get_backend()."""
        good = FakeProvider("firecrawl", result=_ok())
        monkeypatch.setattr(wt, "_load_web_config", lambda: {"search_backend": ""})
        monkeypatch.setattr(wt, "_get_search_backend", lambda: "firecrawl")
        _patch_common(monkeypatch, {"firecrawl": good})

        result = json.loads(wt.web_search_tool("query", limit=1))

        assert result["success"] is True
        assert good.calls == [("query", 1)]


class TestFallbackChain:
    """Требование 2 и 4: список пробуется по порядку, отказ перечисляет все попытки."""

    def test_first_raises_second_answers_and_is_marked(self, monkeypatch):
        down = FakeProvider("down", raises=RuntimeError("connection refused"))
        good = FakeProvider("good", result=_ok())
        monkeypatch.setattr(wt, "_load_web_config", lambda: {"search_backend": ["down", "good"]})
        _patch_common(monkeypatch, {"down": down, "good": good})

        result = json.loads(wt.web_search_tool("query", limit=5))

        assert result["success"] is True
        assert down.calls and good.calls
        # Ответил не первый — это должно быть видно.
        assert result["data"]["answered_by"] == "good"

    def test_first_returns_empty_success_second_answers(self, monkeypatch):
        """Самый частый вид поломки: success=true и пустой список — это
        уже не отличимо от ошибки (_reject_empty_search_success), цепочка
        обязана идти дальше ровно как при явной ошибке."""
        empty = FakeProvider("empty_one", result=_empty_success())
        good = FakeProvider("good", result=_ok())
        monkeypatch.setattr(
            wt, "_load_web_config", lambda: {"search_backend": ["empty_one", "good"]}
        )
        _patch_common(monkeypatch, {"empty_one": empty, "good": good})

        result = json.loads(wt.web_search_tool("query", limit=5))

        assert result["success"] is True
        assert empty.calls and good.calls
        assert result["data"]["answered_by"] == "good"

    def test_first_success_is_not_tagged_answered_by(self, monkeypatch):
        """Если сработал первый — цепочка не добавляет лишний шум в ответ."""
        good = FakeProvider("first", result=_ok())
        other = FakeProvider("second", result=_ok())
        monkeypatch.setattr(
            wt, "_load_web_config", lambda: {"search_backend": ["first", "second"]}
        )
        _patch_common(monkeypatch, {"first": good, "second": other})

        result = json.loads(wt.web_search_tool("query", limit=5))

        assert result["success"] is True
        assert good.calls and not other.calls
        assert "answered_by" not in result["data"]

    def test_nobody_succeeds_lists_every_attempt(self, monkeypatch):
        p1 = FakeProvider("p1", result=_error("api key missing"))
        p2 = FakeProvider("p2", result=_empty_success())
        p3 = FakeProvider("p3", raises=ValueError("boom"))
        monkeypatch.setattr(
            wt, "_load_web_config", lambda: {"search_backend": ["p1", "p2", "p3"]}
        )
        _patch_common(monkeypatch, {"p1": p1, "p2": p2, "p3": p3})

        result = json.loads(wt.web_search_tool("погода в Москве", limit=5))

        assert result["success"] is False
        assert p1.calls and p2.calls and p3.calls
        err = result["error"]
        # Все три попытки должны быть узнаваемы в тексте отказа, вместе с
        # тем, чем каждая ответила.
        assert "p1" in err and "api key missing" in err
        assert "p2" in err
        assert "p3" in err and "boom" in err
        assert "погода в Москве" in err


class TestUnavailableProviderSkippedWithoutCall:
    """Требование 2: незарегистрированный/недоступный — пропуск БЕЗ попытки."""

    def test_unavailable_provider_search_is_never_invoked(self, monkeypatch):
        down = FakeProvider("down", result=_ok())  # если бы позвали — вернул бы успех
        good = FakeProvider("good", result=_ok())
        monkeypatch.setattr(wt, "_load_web_config", lambda: {"search_backend": ["down", "good"]})
        _patch_common(
            monkeypatch,
            {"down": down, "good": good},
            available=lambda name: name != "down",
        )

        result = json.loads(wt.web_search_tool("query", limit=5))

        assert result["success"] is True
        assert down.calls == []  # ключевая проверка: search() не звали вовсе
        assert good.calls == [("query", 5)]
        assert result["data"]["answered_by"] == "good"

    def test_unregistered_provider_is_skipped_without_lookup_error(self, monkeypatch):
        """Имя вообще не зарегистрировано в реестре — get_provider вернёт
        None, цепочка должна пропустить его и продолжить, а не упасть."""
        good = FakeProvider("good", result=_ok())
        monkeypatch.setattr(
            wt, "_load_web_config", lambda: {"search_backend": ["ghost", "good"]}
        )
        _patch_common(monkeypatch, {"good": good})  # "ghost" отсутствует в словаре

        result = json.loads(wt.web_search_tool("query", limit=5))

        assert result["success"] is True
        assert good.calls == [("query", 5)]

    def test_search_only_capability_mismatch_is_skipped(self, monkeypatch):
        """Провайдер зарегистрирован, но не умеет искать (extract-only) —
        тоже пропуск без попытки, а не TypeError/AttributeError."""
        extract_only = FakeProvider("extract_only", result=_ok(), supports_search=False)
        good = FakeProvider("good", result=_ok())
        monkeypatch.setattr(
            wt, "_load_web_config", lambda: {"search_backend": ["extract_only", "good"]}
        )
        _patch_common(monkeypatch, {"extract_only": extract_only, "good": good})

        result = json.loads(wt.web_search_tool("query", limit=5))

        assert result["success"] is True
        assert extract_only.calls == []
        assert good.calls == [("query", 5)]


class TestFallbackChainTimeBudget:
    """Требование 3: общий бюджет времени цепочки не даёт уйти в бесконечность.

    Часы подделаны (никакого реального time.sleep) — медленный провайдер
    сам продвигает поддельные часы внутри своего search(), имитируя долгий
    сетевой вызов без реального ожидания в тесте.
    """

    class _FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def advance(self, secs: float) -> None:
            self.now += secs

        def monotonic(self) -> float:
            return self.now

    def test_slow_first_provider_stops_the_chain_before_the_rest(self, monkeypatch):
        clock = self._FakeClock()
        monkeypatch.setattr(wt.time, "monotonic", clock.monotonic)

        # Продвигает часы далеко за пределы SEARCH_FALLBACK_CHAIN_BUDGET_SECS
        # прямо во время своего "сетевого" вызова — как будто провайдер
        # тормозил секунд сто, прежде чем окончательно сломаться.
        slow = FakeProvider(
            "slow",
            result=_error("timed out upstream"),
            on_call=lambda: clock.advance(wt.SEARCH_FALLBACK_CHAIN_BUDGET_SECS * 5),
        )
        never = FakeProvider("never_called", result=_ok())
        monkeypatch.setattr(
            wt, "_load_web_config", lambda: {"search_backend": ["slow", "never_called"]}
        )
        _patch_common(monkeypatch, {"slow": slow, "never_called": never})

        result = json.loads(wt.web_search_tool("query", limit=5))

        assert result["success"] is False
        assert slow.calls  # первую попытку мы всё равно делаем
        assert never.calls == []  # бюджет исчерпан — вторую не начинаем
        assert "бюджет" in result["error"]
        assert "never_called" in result["error"]

    def test_budget_is_not_consulted_before_the_first_attempt(self, monkeypatch):
        """Даже если часы уже "в прошлом" относительно дедлайна (граничный
        случай — тест НЕ продвигает их вообще), первая попытка в цепочке
        обязана состояться: бюджет проверяется только перед СЛЕДУЮЩЕЙ."""
        clock = self._FakeClock()
        monkeypatch.setattr(wt.time, "monotonic", clock.monotonic)

        good = FakeProvider("only", result=_ok())
        monkeypatch.setattr(wt, "_load_web_config", lambda: {"search_backend": ["only"]})
        _patch_common(monkeypatch, {"only": good})

        result = json.loads(wt.web_search_tool("query", limit=5))

        assert result["success"] is True
        assert good.calls == [("query", 5)]
