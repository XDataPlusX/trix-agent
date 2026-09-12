"""Мастер пишет цепочку поисковиков, а не один движок.

Цепочка запасных (`tools/web_tools.py::_run_search_backend_chain`)
принимает список и при одной строке не включается вообще. Мастер писал
строку — значит в день, когда выбранный поисковик перестанет отвечать,
поиск у клиента умирал без запасного варианта.

Что это случится — не предположение: снято 2026-09-05 на клиентской
машине, где duckduckgo вернул `CAPTCHA`, brave — «too many requests»,
startpage — `CAPTCHA`.
"""

import pytest

from hermes_cli.trix_search_chain import (
    KEYLESS_FALLBACKS,
    build_search_chain,
    chain_is_meaningful,
    primary_backend,
)


def test_the_clients_choice_is_always_first():
    """Цепочка — запасной выход, а не подмена решения клиента."""
    assert build_search_chain("tavily")[0] == "tavily"
    assert build_search_chain("brave-free")[0] == "brave-free"


def test_a_keyed_engine_gets_a_keyless_safety_net():
    chain = build_search_chain("tavily")
    assert "ddgs" in chain
    assert len(chain) > 1


def test_the_tail_holds_only_engines_that_need_nothing_from_the_client():
    """Платный движок в хвосте — это трата времени цепочки на мёртвого кандидата.

    Ключа к нему у клиента может не быть, а цепочка живёт по бюджету
    времени: каждый заведомо недоступный кандидат съедает чужие секунды.
    """
    chain = build_search_chain("ddgs")
    for name in chain[1:]:
        assert name in KEYLESS_FALLBACKS


def test_the_chosen_engine_is_never_duplicated_in_the_tail():
    """Один и тот же сломанный движок не должен пробоваться дважды."""
    chain = build_search_chain("ddgs")
    assert chain.count("ddgs") == 1


def test_a_locally_hosted_engine_can_join_the_tail():
    """SearXNG, который клиент поднял сам, ключа не требует."""
    chain = build_search_chain("tavily", ["searxng"])
    assert chain == ["tavily", "ddgs", "searxng"]


@pytest.mark.parametrize("nothing", ["", "   ", None])
def test_no_choice_means_no_chain(nothing):
    """Написать цепочку из одного хвоста значит решить за клиента."""
    assert build_search_chain(nothing) == []


def test_case_and_padding_do_not_produce_a_duplicate():
    assert build_search_chain("  DDGS  ") == ["ddgs"]


def test_a_single_element_is_not_a_chain():
    """Один элемент — прежнее поведение под другим типом, не цепочка."""
    assert chain_is_meaningful(build_search_chain("ddgs")) is False
    assert chain_is_meaningful(build_search_chain("tavily")) is True


# --- возврат клиента в мастер -------------------------------------------------


def test_the_wizard_reads_the_clients_choice_back_out_of_a_chain():
    """Иначе при возврате в мастер выбор терялся бы.

    Форма оперирует одним именем, а в конфиге теперь может лежать список.
    """
    assert primary_backend(["tavily", "ddgs"]) == "tavily"


def test_a_plain_string_still_reads_back_unchanged():
    assert primary_backend("ddgs") == "ddgs"


@pytest.mark.parametrize("junk", [None, 42, [], [None, ""], {}])
def test_unreadable_values_read_back_as_no_choice(junk):
    """Мусор в конфиге не должен ронять форму — просто «не выбрано»."""
    assert primary_backend(junk) == ""


def test_a_chain_written_by_apply_is_read_back_by_the_form(tmp_path, monkeypatch):
    """Сквозная проверка круга: записали цепочку — прочитали выбор.

    Ровно этот круг и ломался бы, если бы читающая сторона осталась
    рассчитанной на строку.
    """
    chain = build_search_chain("tavily")
    stored = {"web": {"search_backend": chain}}
    assert primary_backend(stored["web"]["search_backend"]) == "tavily"


def test_the_runtime_walks_exactly_what_the_wizard_wrote():
    """Записанное мастером обязано читаться движком поиска как цепочка."""
    from tools.web_tools import _normalize_backend_value

    written = build_search_chain("tavily", ["searxng"])
    assert _normalize_backend_value(written) == ["tavily", "ddgs", "searxng"]
