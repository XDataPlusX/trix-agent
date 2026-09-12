"""ddgs обязан ходить через прокси, за который клиент заплатил.

ddgs — рекомендованный по умолчанию бесплатный поиск, и он ходил на
duckduckgo.com НАПРЯМУЮ, мимо прокси: под ним лежит primp (Rust), который
`HTTPS_PROXY`/`ALL_PROXY` игнорирует, а сам ddgs читает только собственную
`DDGS_PROXY`, которую продукт нигде не пишет. Для российского ДЦ это
значит, что единственный инструмент, способный починить заблокированный
поиск, к нему не подключён.

Проверяется поведение: с каким аргументом провайдер СОЗДАЁТ клиента ddgs.
"""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def fake_ddgs(monkeypatch):
    """Подставной `ddgs`, запоминающий, с чем его создали."""
    calls: dict = {}

    class _Client:
        def __init__(self, **kwargs):
            calls.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, query, max_results=5):
            return []

    module = types.ModuleType("ddgs")
    module.DDGS = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ddgs", module)
    return calls


def _run(monkeypatch, proxy):
    monkeypatch.setattr(
        "agent.process_bootstrap._get_proxy_for_base_url",
        lambda base_url: proxy,
        raising=False,
    )
    from plugins.web.ddgs import provider

    provider._run_ddgs_search("запрос", 3)


def test_configured_proxy_is_handed_to_ddgs(monkeypatch, fake_ddgs):
    _run(monkeypatch, "http://user:pass@proxy.example:3128")
    assert fake_ddgs.get("proxy") == "http://user:pass@proxy.example:3128"


def test_no_proxy_configured_means_no_proxy_argument(monkeypatch, fake_ddgs):
    """Пустой `proxy=None` в ddgs — не то же самое, что его отсутствие;
    передавать пустоту незачем."""
    _run(monkeypatch, None)
    assert "proxy" not in fake_ddgs


def test_the_request_timeout_is_still_bounded(monkeypatch, fake_ddgs):
    """Прокси не должен вытеснить ограничение по времени."""
    _run(monkeypatch, "http://proxy.example:3128")
    assert fake_ddgs.get("timeout") == 10


def test_a_broken_proxy_resolver_does_not_break_search(monkeypatch, fake_ddgs):
    """Поиск важнее прокси: если резолвер упал, ищем напрямую, а не падаем."""
    def boom(base_url):
        raise RuntimeError("резолвер сломался")

    monkeypatch.setattr(
        "agent.process_bootstrap._get_proxy_for_base_url", boom, raising=False
    )
    from plugins.web.ddgs import provider

    provider._run_ddgs_search("запрос", 3)
    assert "proxy" not in fake_ddgs
