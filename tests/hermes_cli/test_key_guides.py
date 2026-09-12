"""Справочник «как получить ключ» и «есть ли бесплатный тариф».

Клиент видит поле «Ключ Tavily API», ссылку на чужой англоязычный сайт —
и остаётся с ним один на один. Справочник закрывает это тремя-четырьмя
строками под полем, а заодно служит нашим собственным документом: где
бесплатный тариф есть, где его нет, где просят карту.

Главное, что здесь проверяется, — **невозможность соврать клиенту**:
утверждение о тарифе без даты проверки не должно существовать в принципе,
а незаполненная запись обязана честно сказать «мы не проверяли», а не
промолчать и не додумать.
"""

import re

import pytest

from hermes_cli.trix_key_guides import (
    CARD_NO,
    CARD_UNKNOWN,
    CARD_YES,
    FREE_NO,
    FREE_TRIAL,
    FREE_UNKNOWN,
    FREE_YES,
    GUIDES,
    KeyGuide,
    guide_for,
    guide_payload,
)


# --- нельзя соврать -----------------------------------------------------------


def test_a_tariff_claim_without_a_verification_date_is_impossible():
    """Самая важная проверка файла.

    Клиенту нельзя показывать «бесплатный тариф есть», если мы этого не
    проверяли: он потратит время на регистрацию и упрётся в карту.
    """
    for claim in (FREE_YES, FREE_TRIAL, FREE_NO):
        with pytest.raises(ValueError, match="дат"):
            KeyGuide(service="Выдуманный", free=claim)


def test_an_unchecked_entry_is_legal_and_says_so_out_loud():
    """«Не проверено» — значение, а не пробел."""
    guide = KeyGuide(service="Ещё не смотрели", steps=("шаг",))
    assert guide.free == FREE_UNKNOWN
    GUIDES["ТЕСТОВЫЙ_КЛЮЧ"] = guide
    try:
        notes = guide_payload("ТЕСТОВЫЙ_КЛЮЧ")["notes"]
    finally:
        GUIDES.pop("ТЕСТОВЫЙ_КЛЮЧ", None)
    assert any("не проверяли" in n for n in notes)


def test_a_nonsense_free_value_is_refused():
    with pytest.raises(ValueError, match="free"):
        KeyGuide(service="X", free="возможно", checked="2026-09-06")


def test_a_nonsense_card_value_is_refused():
    with pytest.raises(ValueError, match="card"):
        KeyGuide(service="X", card="наверное")


# --- формулировки -------------------------------------------------------------


def _payload(**kw):
    GUIDES["ВРЕМЕННЫЙ"] = KeyGuide(service="Сервис", **kw)
    try:
        return guide_payload("ВРЕМЕННЫЙ")
    finally:
        GUIDES.pop("ВРЕМЕННЫЙ", None)


def _everything_the_client_reads(payload) -> str:
    """Клиенту важно, что ему это сказали, а не в каком поле оно лежит."""
    return " ".join([payload["warning"], *payload["notes"]])


def test_a_paid_only_service_says_so_plainly():
    assert "платный" in _everything_the_client_reads(
        _payload(free=FREE_NO, checked="2026-09-06")
    )


def test_a_free_tier_carries_its_limit_when_we_know_it():
    notes = _payload(
        free=FREE_YES, free_note="2000 запросов в месяц", checked="2026-09-06"
    )["notes"]
    assert any("2000 запросов в месяц" in n for n in notes)


def test_trial_credits_are_not_called_a_free_tier():
    """Стартовые кредиты кончаются — обещать бесплатный тариф нельзя."""
    notes = _payload(free=FREE_TRIAL, checked="2026-09-06")["notes"]
    assert any("стартовые кредиты" in n for n in notes)
    assert not any("Бесплатный тариф есть" in n for n in notes)


def test_the_card_question_is_answered_when_we_checked_it():
    yes = _everything_the_client_reads(
        _payload(free=FREE_YES, card=CARD_YES, checked="2026-09-06")
    )
    no = _everything_the_client_reads(
        _payload(free=FREE_YES, card=CARD_NO, checked="2026-09-06")
    )
    assert "Потребуется банковская карта" in yes
    assert "Карта не потребуется" in no


def test_the_card_question_is_only_raised_where_it_matters():
    """На платном сервисе вопрос «просят ли карту» не задаётся."""
    paid = _payload(free=FREE_NO, card=CARD_UNKNOWN, checked="2026-09-06")["notes"]
    assert not any("карт" in n.lower() for n in paid)

    free = _payload(free=FREE_YES, card=CARD_UNKNOWN, checked="2026-09-06")["notes"]
    assert any("не уточняли" in n for n in free)


def test_an_unknown_key_has_no_payload():
    assert guide_payload("СОВСЕМ_НЕИЗВЕСТНЫЙ_КЛЮЧ") is None
    assert guide_for("СОВСЕМ_НЕИЗВЕСТНЫЙ_КЛЮЧ") is None


# --- качество самих записей ---------------------------------------------------


@pytest.mark.parametrize("env_key", sorted(GUIDES))
def test_every_entry_is_shaped_like_instructions_a_person_can_follow(env_key):
    guide = GUIDES[env_key]
    assert guide.service.strip(), f"{env_key}: нет имени сервиса"
    if guide.steps:
        assert 2 <= len(guide.steps) <= 5, f"{env_key}: шагов {len(guide.steps)}"
        for step in guide.steps:
            assert step.strip(), f"{env_key}: пустой шаг"
            assert len(step) <= 160, f"{env_key}: шаг длиннее строки экрана"


@pytest.mark.parametrize("env_key", sorted(GUIDES))
def test_every_link_is_https(env_key):
    url = GUIDES[env_key].key_url
    if url:
        assert url.startswith("https://"), f"{env_key}: {url}"


@pytest.mark.parametrize("env_key", sorted(GUIDES))
def test_no_entry_leaks_our_internals_to_the_client(env_key):
    """Клиенту нельзя показывать пути конфига и наш жаргон."""
    text = " ".join([*GUIDES[env_key].steps, GUIDES[env_key].free_note])
    for jargon in ("config.yaml", "~/.hermes", ".env", "HERMES_", "toolset"):
        assert jargon not in text, f"{env_key}: {jargon}"


@pytest.mark.parametrize("env_key", sorted(GUIDES))
def test_a_checked_date_looks_like_a_date(env_key):
    checked = GUIDES[env_key].checked
    if checked:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked), f"{env_key}: {checked}"


@pytest.mark.parametrize("env_key", sorted(GUIDES))
def test_a_tariff_claim_names_its_source(env_key):
    """Проверять по чему-то надо: утверждение без источника не перепроверишь."""
    guide = GUIDES[env_key]
    if guide.free != FREE_UNKNOWN:
        assert guide.source.startswith("https://"), f"{env_key}: нет источника"


# --- справочник не должен отставать от каталога -------------------------------


def _key_env_vars_the_wizard_asks_for() -> set[str]:
    """Переменные-КЛЮЧИ, которые мастер просит у клиента.

    Адреса (``*_URL``) сюда не входят: это не ключ чужого сервиса, а
    адрес того, что клиент поднял у себя, и «как получить» там неуместно.
    """
    from hermes_cli.setup_wizard.providers_view import wizard_providers
    from hermes_cli.setup_wizard.tools_view import wizard_tool_blocks

    keys: set[str] = set()
    for block in wizard_tool_blocks():
        for row in block.get("rows") or []:
            for env in row.get("env_vars") or []:
                k = env.get("key") or ""
                if k and not k.endswith("_URL"):
                    keys.add(k)
    for row in wizard_providers():
        k = row.get("env_var") or ""
        if k and not k.endswith("_URL"):
            keys.add(k)
    return keys


def test_the_catalog_actually_asks_for_keys():
    """Страховка от зелёного нуля: пустой каталог обесценил бы проверку ниже."""
    assert len(_key_env_vars_the_wizard_asks_for()) >= 5


def test_every_key_the_tools_ask_for_has_an_entry():
    """Справочник ведём мы — значит он не имеет права молча отставать.

    Ограничено блоками инструментов: провайдеров моделей в каталоге 35, и
    заводить запись на каждый — отдельная работа (см.
    ``test_model_providers_without_an_entry_are_listed_not_hidden``).
    """
    from hermes_cli.setup_wizard.tools_view import wizard_tool_blocks

    missing = set()
    for block in wizard_tool_blocks():
        for row in block.get("rows") or []:
            for env in row.get("env_vars") or []:
                k = env.get("key") or ""
                if k and not k.endswith("_URL") and k not in GUIDES:
                    missing.add(k)
    assert not missing, f"мастер просит ключ, а справочника нет: {sorted(missing)}"


def test_model_providers_without_an_entry_are_listed_not_hidden():
    """Пробелы по провайдерам моделей — известны и посчитаны, а не забыты.

    Проверка не требует записи на каждого из 35: она требует, чтобы
    отсутствие записи означало отсутствие подсказки, а не пустую
    раскрывашку «Как получить ключ» без содержимого.
    """
    from hermes_cli.setup_wizard.providers_view import wizard_providers

    for row in wizard_providers():
        env_key = row.get("env_var") or ""
        guide = row.get("guide")
        if env_key in GUIDES:
            assert guide, f"{env_key}: запись есть, а до мастера не доехала"
        else:
            assert guide is None, f"{env_key}: пустая подсказка без данных"


def test_the_guide_reaches_the_tool_blocks():
    """Сквозная проверка: запись справочника доезжает до поля ввода."""
    from hermes_cli.setup_wizard.tools_view import wizard_tool_blocks

    seen = 0
    for block in wizard_tool_blocks():
        for row in block.get("rows") or []:
            for env in row.get("env_vars") or []:
                if (env.get("key") or "") in GUIDES:
                    assert env.get("guide"), f"{env['key']}: справка не доехала"
                    seen += 1
    assert seen, "ни одна запись не доехала — проверка ничего не значит"


def test_a_free_tier_that_needs_a_card_says_so_in_the_same_breath():
    """Brave называется «Free», а карту просит — клиент обязан узнать сразу.

    Это и есть та ловушка, ради которой в справочнике заведено поле про
    карту: строка каталога обещает бесплатность, а официальная
    документация сервиса требует привязать карту.
    """
    said = _everything_the_client_reads(guide_payload("BRAVE_SEARCH_API_KEY"))
    assert "карт" in said.lower()


# --- что нельзя прятать за щелчком --------------------------------------------


def test_the_card_requirement_is_visible_without_opening_anything():
    """Клиент обязан узнать про карту ДО регистрации, а не после.

    Строка каталога называется «Brave Search (Free)» — так свой тариф
    называет сам Brave, — а его же документация требует привязать карту.
    Спрятать это за раскрывашкой значит дать клиенту потратить время зря.
    """
    warning = guide_payload("BRAVE_SEARCH_API_KEY")["warning"]
    assert "карт" in warning.lower()
    # Зарубежному сервису нужна зарубежная карта — иначе клиент попробует
    # свою и упрётся; см. test_a_foreign_service_asks_for_a_foreign_card.
    assert "иностранного банка" in warning


def test_a_paid_only_service_warns_without_opening_anything():
    assert "платный" in guide_payload("KREA_API_KEY")["warning"]
    assert "платный" in guide_payload("DEEPINFRA_API_KEY")["warning"]


def test_a_service_with_no_catch_has_nothing_to_warn_about():
    """Tavily: бесплатно и без карты — пугать нечем."""
    assert guide_payload("TAVILY_API_KEY")["warning"] == ""


def test_an_unchecked_service_does_not_warn_about_what_we_did_not_check():
    """Не проверяли — не пугаем: это было бы такой же выдумкой, как обещание."""
    assert guide_payload("FAL_KEY")["warning"] == ""


def test_the_visible_warning_is_not_repeated_inside():
    """Одно и то же дважды на экране читается как две разные беды."""
    for env_key in GUIDES:
        payload = guide_payload(env_key)
        if payload["warning"]:
            assert payload["warning"] not in payload["notes"], env_key


@pytest.mark.parametrize("env_key", sorted(GUIDES))
def test_a_card_requirement_is_never_silent(env_key):
    """Инвариант: где нужна карта — там всегда есть видимое предупреждение."""
    if GUIDES[env_key].card == CARD_YES:
        assert guide_payload(env_key)["warning"], env_key


# --- плашка о тарифе ----------------------------------------------------------


def test_the_badge_says_free_only_when_it_is_free_without_a_card():
    """Ровно то, что клиент хочет увидеть, не открывая ничего."""
    from hermes_cli.trix_key_guides import badge_for

    assert badge_for("TAVILY_API_KEY") == "бесплатно, без карты"
    assert badge_for("FIRECRAWL_API_KEY") == "бесплатно, без карты"
    assert badge_for("ELEVENLABS_API_KEY") == "бесплатно, без карты"


def test_the_badge_never_hides_a_card_requirement():
    """Апстримная плашка у Brave говорит «free» — наша не имеет права."""
    from hermes_cli.trix_key_guides import badge_for

    badge = badge_for("BRAVE_SEARCH_API_KEY")
    assert "бесплатно" in badge
    assert "карт" in badge


def test_a_paid_service_is_badged_paid():
    from hermes_cli.trix_key_guides import badge_for

    assert badge_for("DEEPINFRA_API_KEY") == "платно"
    assert badge_for("KREA_API_KEY") == "платно"


def test_an_unchecked_service_gets_no_badge_at_all():
    """Догадка на плашке хуже её отсутствия: плашку запоминают как факт."""
    from hermes_cli.trix_key_guides import badge_for

    assert badge_for("FAL_KEY") == ""
    assert badge_for("СОВСЕМ_НЕИЗВЕСТНЫЙ") == ""


def test_the_badge_reaches_the_catalog_rows():
    """Сквозная проверка: плашка доезжает до строки, которую видит клиент."""
    from hermes_cli.setup_wizard.tools_view import wizard_tool_blocks

    seen = {}
    for block in wizard_tool_blocks():
        for row in block.get("rows") or []:
            if row.get("price_badge"):
                seen[row["name"]] = row["price_badge"]
    assert seen, "ни одна плашка не доехала"
    assert any("бесплатно, без карты" == v for v in seen.values())


def test_the_upstream_badge_is_not_what_the_client_reads():
    """Разъезд апстримной плашки с фактами — причина завести свою.

    У Brave апстримный badge говорит «free», хотя нужна карта; у Tavily —
    «paid», хотя бесплатный тариф есть. Проверяем, что наши строки
    основаны на справочнике, а не на нём.
    """
    from hermes_cli.setup_wizard.tools_view import wizard_tool_blocks

    for block in wizard_tool_blocks():
        for row in block.get("rows") or []:
            if row.get("name") == "Tavily":
                assert row.get("price_badge") == "бесплатно, без карты"
                return
    pytest.skip("строки Tavily нет в каталоге этой сборки")


# --- формулировка про карту ---------------------------------------------------


def test_a_foreign_service_asks_for_a_foreign_card():
    """«Банковская карта» российский клиент прочитает как «любая».

    Оплата у зарубежных сервисов идёт за границу и в долларах. Мы не
    утверждаем, что российскую карту отвергнут — этого мы не проверяли;
    мы называем ту, которой оплата пройдёт наверняка.
    """
    said = guide_payload("BRAVE_SEARCH_API_KEY")["warning"]
    assert "иностранного банка" in said


def test_a_russian_service_is_not_told_to_find_a_foreign_card():
    guide = GUIDES["NEXARA_API_KEY"]
    assert guide.russian is True
    said = " ".join(
        [guide_payload("NEXARA_API_KEY")["warning"], *guide_payload("NEXARA_API_KEY")["notes"]]
    )
    assert "иностранного банка" not in said


def test_a_paid_foreign_service_names_the_card_too():
    said = guide_payload("KREA_API_KEY")["warning"]
    assert "платный" in said and "иностранного банка" in said
