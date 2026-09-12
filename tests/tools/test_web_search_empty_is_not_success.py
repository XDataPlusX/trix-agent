"""Пустая выдача поиска не должна выдаваться модели за успех.

Самый опасный исход поиска не «поиск не работает», а «поиск вернул ноль
результатов, отрапортовав успех». Модель видит успешный ответ с пустым
списком и делает единственный доступный вывод — «в интернете этого нет» —
после чего уверенно отвечает клиенту по памяти. Отличить «ничего не
нашлось» от «поисковик сломался» она не может: ответ выглядит одинаково.

А сломанный поисковик отдаёт именно это: SearXNG с заблокированными
апстрим-движками возвращает HTTP 200 и пустой список, тем же отвечает
ddgs, когда его режут по адресу.
"""

from __future__ import annotations

from tools.web_tools import _reject_empty_search_success as reject


class TestEmptySuccessBecomesAnHonestFailure:
    def test_empty_web_list_is_no_longer_a_success(self):
        out = reject({"success": True, "data": {"web": []}}, "searxng", "погода")
        assert out["success"] is False
        assert "searxng" in out["error"]
        assert "погода" in out["error"]

    def test_the_message_says_the_two_cases_cannot_be_told_apart(self):
        """Модели нужно не «ошибка», а понимание, что вывод делать нельзя."""
        out = reject({"success": True, "data": {"web": []}}, "ddgs", "q")
        assert "отличить нельзя" in out["error"]

    def test_missing_data_key_is_treated_as_empty(self):
        out = reject({"success": True}, "ddgs", "q")
        assert out["success"] is False

    def test_unresponsive_engines_reach_the_message(self):
        """SearXNG прикладывает список не ответивших движков — это ровно
        то, по чему поломку можно опознать, и раньше оно выбрасывалось."""
        out = reject(
            {"success": True, "data": {"web": [], "unresponsive_engines": [["google", "timeout"]]}},
            "searxng",
            "q",
        )
        assert "google" in out["error"]


class TestRealResultsAndRealFailuresAreUntouched:
    """Обратная сторона: «не выдавать пустоту за успех» не должно
    превратиться в «портить нормальные ответы»."""

    def test_a_non_empty_result_passes_through_unchanged(self):
        payload = {"success": True, "data": {"web": [{"title": "t", "url": "u"}]}}
        assert reject(payload, "ddgs", "q") is payload

    def test_an_existing_failure_passes_through_unchanged(self):
        payload = {"success": False, "error": "провайдер лёг"}
        assert reject(payload, "ddgs", "q") is payload

    def test_a_non_dict_answer_is_not_rewritten(self):
        assert reject(None, "ddgs", "q") is None  # type: ignore[arg-type]


class TestTheGuardIsActuallyWiredIntoSearch:
    """Отдельно от логики — проверка ПРОВОДКИ.

    Тесты выше зовут функцию напрямую и остались бы зелёными, даже если
    убрать её вызов из `web_search_tool` — то есть если защита перестанет
    существовать для клиента. Поймано при проверке «краснеет ли тест без
    правки»: удаление точки вызова не покраснило ничего.
    """

    def test_an_empty_provider_answer_reaches_the_model_as_a_failure(self, monkeypatch):
        import json

        import tools.web_tools as wt

        class _EmptyProvider:
            name = "searxng"

            def supports_search(self):
                return True

            def search(self, query, limit):
                return {"success": True, "data": {"web": []}}

        # `get_provider` импортируется ВНУТРИ функции из реестра, поэтому
        # патчить надо реестр, а не этот модуль.
        monkeypatch.setattr(wt, "_get_search_backend", lambda: "searxng", raising=False)
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda name: _EmptyProvider(), raising=False
        )

        result = json.loads(wt.web_search_tool("погода", limit=3))
        assert result["success"] is False, result
        assert "searxng" in result["error"]

    def test_a_normal_answer_still_reaches_the_model_as_success(self, monkeypatch):
        import json

        import tools.web_tools as wt

        class _GoodProvider:
            name = "ddgs"

            def supports_search(self):
                return True

            def search(self, query, limit):
                return {"success": True, "data": {"web": [{"title": "t", "url": "u"}]}}

        monkeypatch.setattr(wt, "_get_search_backend", lambda: "ddgs", raising=False)
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda name: _GoodProvider(), raising=False
        )

        result = json.loads(wt.web_search_tool("погода", limit=3))
        assert result["success"] is True
        assert len(result["data"]["web"]) == 1
