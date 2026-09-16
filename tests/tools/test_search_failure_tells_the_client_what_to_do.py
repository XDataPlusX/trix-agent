"""Отказ поиска обязан говорить, что с ним делать.

Клиент видел техническую строку от провайдера и оставался с ней наедине,
хотя действие у него ровно одно: сменить поисковик в мастере настройки.

Отдельно важно, что отказ — не редкость. Снято на клиентской машине
2026-09-05: поднятый там же SearXNG получил от duckduckgo `CAPTCHA`, от
brave — «Suspended: too many requests», от startpage — `Suspended:
CAPTCHA`. Выбранный по умолчанию DuckDuckGo — единственный из
предлагаемых, кто скрейпит выдачу вместо официального API.
"""

import json

import pytest

from tools.trix_search_advice import advice_for, advise_on_search_failure


def test_a_successful_search_is_left_untouched():
    ok = {"success": True, "data": {"web": [{"title": "т"}]}}
    assert advise_on_search_failure(ok, "ddgs") is ok


def test_a_failure_gains_a_next_step():
    out = advise_on_search_failure({"success": False, "error": "provider timeout"}, "ddgs")
    assert "provider timeout" in out["error"]
    assert "мастере настройки" in out["error"]


def test_the_original_error_is_never_replaced():
    """Техническая причина нужна: без неё нечего показать в поддержку."""
    out = advise_on_search_failure(
        {"success": False, "error": "HTTP 429 rate limited"}, "tavily"
    )
    assert out["error"].startswith("HTTP 429 rate limited")


def test_a_scraping_engine_is_named_as_the_likely_reason():
    """DuckDuckGo упирается в защиту от роботов — про это стоит сказать."""
    text = advice_for("ddgs")
    assert "DuckDuckGo" in text
    assert "официальное API" in text


def test_an_api_engine_gets_the_plain_advice_without_ddg_noise():
    """Клиенту на Tavily незачем читать про DuckDuckGo."""
    text = advice_for("tavily")
    assert "DuckDuckGo" not in text
    assert "мастере настройки" in text


def test_the_advice_never_leaks_config_paths_to_the_client():
    """Клиенту нельзя показывать пути конфига и имена переменных."""
    for backend in ("ddgs", "tavily", ""):
        text = advice_for(backend)
        for jargon in ("config.yaml", "~/.hermes", "web.search_backend", "env", "_API_KEY"):
            assert jargon not in text, f"{jargon} просочился в совет для {backend!r}"


def test_the_advice_is_not_appended_twice():
    once = advise_on_search_failure({"success": False, "error": "нет ответа"}, "ddgs")
    twice = advise_on_search_failure(once, "ddgs")
    assert once["error"] == twice["error"]


@pytest.mark.parametrize("junk", [None, "строка", 42, [], {"success": False}])
def test_nothing_to_advise_about_is_returned_unchanged(junk):
    """Не-словарь и отказ без текста ошибки — советовать не о чем."""
    assert advise_on_search_failure(junk, "ddgs") == junk


def test_the_chain_refusal_speaks_client_language():
    """Итоговый отказ цепочки не упоминает путь в конфиге."""
    from tools.web_tools import _run_search_backend_chain

    out = _run_search_backend_chain(["совсем-несуществующий-движок"], "погода", 3)
    assert out["success"] is False
    assert "web.search_backend" not in out["error"]
    assert "погода" in out["error"]


def test_the_advice_reaches_the_tool_output(monkeypatch):
    """Сквозная проверка: совет доходит до того, что читает модель.

    Это и есть смысл правки — совет обязан быть в ответе инструмента, а
    не только в отдельной функции рядом.
    """
    import tools.web_tools as wt

    monkeypatch.setattr(wt, "_load_web_config", lambda: {"search_backend": "ddgs"})
    monkeypatch.setattr(wt, "_get_search_backend", lambda: "ddgs")
    monkeypatch.setattr(wt, "_ensure_web_plugins_loaded", lambda: None)

    class _Provider:
        name = "ddgs"

        def supports_search(self):
            return True

        def search(self, query, limit):
            return {"success": False, "error": "CAPTCHA от поисковика"}

    import agent.web_search_registry as reg

    monkeypatch.setattr(reg, "get_provider", lambda name: _Provider(), raising=False)
    monkeypatch.setattr(
        reg, "get_active_search_provider", lambda: _Provider(), raising=False
    )

    out = json.loads(wt.web_search_tool(query="погода", limit=3))
    assert out["success"] is False
    assert "CAPTCHA" in out["error"]
    assert "мастере настройки" in out["error"]
