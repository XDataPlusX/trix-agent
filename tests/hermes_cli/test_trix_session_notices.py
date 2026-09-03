"""Что клиент читает, когда разговор начинается заново или бот вернулся."""

from pathlib import Path

import agent.i18n as i18n_mod
import pytest
import yaml

from hermes_cli.trix_session_notices import (
    duration_phrase,
    gateway_back_online_message,
    gateway_restarted_message,
    recovered_reply_prefix,
    session_reset_notice,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TRIX_TEMPLATE_PATH = REPO_ROOT / "assets" / "config" / "trix-config.yaml"

# Все причины, ради которых апстрим шлёт клиенту уведомление, — ровно те же
# ветки, что перечислены в ``gateway/run.py``. Пустая строка — «причина не
# названа»: апстрим отправляет такую в ветку молчания, и мы тоже.
RESET_REASONS = ("idle", "suspended", "resume_pending_expired", "daily", "")


@pytest.fixture(autouse=True)
def _russian_language(monkeypatch):
    # t() резолвит язык из env > config.yaml > "en". Тесты идут против
    # временного HERMES_HOME без config.yaml, поэтому без этой строки
    # каталог резолвился бы в "en" и русские утверждения падали бы по
    # неверной причине — из-за языка, а не из-за дефекта в модуле.
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")


def _client_texts() -> dict[str, str]:
    """Все тексты задачи, которые видит клиент, — одним отображением.

    Проверки «нет имени чужого продукта» и «нет пути к файлу» обязаны
    идти разом по всему набору: дефект этого класса чинили в ветке уже
    четырежды, и каждый раз он находился в тексте, который в прошлый раз
    просто забыли включить в проверку.
    """
    texts = {
        f"session_reset[{reason or 'без причины'}]": session_reset_notice(
            reason, idle_minutes=4320
        )
        for reason in RESET_REASONS
    }
    texts["gateway_restart.back_online"] = gateway_back_online_message()
    texts["gateway_restart.session_continues"] = gateway_restarted_message()
    texts["gateway_restart.recovered_prefix"] = recovered_reply_prefix()
    return texts


# Слово, по которому клиент узнаёт СВОЙ случай, — по одному на каждый из
# семи текстов. Таблица существует потому, что «каждый текст покрыт
# по отдельности» не ловит ПЕРЕСТАНОВКУ: ревьюер поменял местами значения
# ``restart`` и ``daily`` в каталоге, и 103 теста остались зелёными —
# оба текста на месте, оба различны, просто клиент после перезапуска
# читает про новый день. Различимость проверяется попарно: свой маркер
# обязан быть, чужие — обязаны отсутствовать.
_CASE_MARKERS = {
    "idle": "не общались",
    "suspended": "остановлен",
    "resume_pending_expired": "не успел восстановить",
    "daily": "новый день",
    "gateway_restart.back_online": "снова на связи",
    "gateway_restart.session_continues": "с того же места",
    "gateway_restart.recovered_prefix": "уже отправлял",
}


def _marked_texts() -> dict[str, str]:
    """Те же семь текстов, но под ключами таблицы маркеров."""
    texts = {
        reason: session_reset_notice(reason, idle_minutes=4320)
        for reason in RESET_REASONS
        if reason
    }
    texts["gateway_restart.back_online"] = gateway_back_online_message()
    texts["gateway_restart.session_continues"] = gateway_restarted_message()
    texts["gateway_restart.recovered_prefix"] = recovered_reply_prefix()
    return texts


def _has_cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in text)


# ---------------------------------------------------------------------------
# Срок молчания словами
# ---------------------------------------------------------------------------


class TestDurationPhrase:
    """Апстрим печатает ``72h``. По-русски это «трое суток»."""

    # 11-14 в каждой таблице — не украшение: это единственный разряд, где
    # русское согласование ломает общее правило («14 часов», а не
    # «14 часа»), и прежние таблицы проверяли только 11.
    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (1, "сутки"),
            (2, "двое суток"),
            (3, "трое суток"),
            (4, "четверо суток"),
            (5, "5 суток"),
            (11, "11 суток"),
            (12, "12 суток"),
            (13, "13 суток"),
            (14, "14 суток"),
            (21, "21 сутки"),
            (22, "22 суток"),
            (101, "101 сутки"),
            (111, "111 суток"),
            (112, "112 суток"),
            (113, "113 суток"),
            (114, "114 суток"),
        ],
    )
    def test_whole_days_are_said_in_days_with_the_right_ending(self, days, expected):
        assert duration_phrase(days * 24 * 60) == expected

    @pytest.mark.parametrize(
        ("hours", "expected"),
        [
            (1, "1 час"),
            (2, "2 часа"),
            (3, "3 часа"),
            (4, "4 часа"),
            (5, "5 часов"),
            (11, "11 часов"),
            (12, "12 часов"),
            (13, "13 часов"),
            (14, "14 часов"),
            (21, "21 час"),
            (22, "22 часа"),
            (23, "23 часа"),
        ],
    )
    def test_a_span_under_a_day_of_whole_hours_is_said_in_hours(self, hours, expected):
        assert duration_phrase(hours * 60) == expected

    @pytest.mark.parametrize(
        ("minutes", "expected"),
        [
            (1, "1 минуту"),
            (2, "2 минуты"),
            (5, "5 минут"),
            (11, "11 минут"),
            (12, "12 минут"),
            (13, "13 минут"),
            (14, "14 минут"),
            (21, "21 минуту"),
            (22, "22 минуты"),
            (45, "45 минут"),
            (59, "59 минут"),
        ],
    )
    def test_a_span_under_an_hour_is_said_in_minutes(self, minutes, expected):
        assert duration_phrase(minutes) == expected

    @pytest.mark.parametrize(
        ("minutes", "expected"),
        [
            (4320, "трое суток"),
            (4380, "трое суток 1 час"),
            (4321, "трое суток 1 минуту"),
            (4410, "трое суток 1 час 30 минут"),
            (1500, "сутки 1 час"),
            (2880 + 13 * 60, "двое суток 13 часов"),
            (90, "1 час 30 минут"),
        ],
    )
    def test_units_are_named_together_and_zero_parts_are_dropped(
        self, minutes, expected
    ):
        """Прежнее правило («кратно суткам → сутками, ИНАЧЕ часами, ИНАЧЕ
        минутами») теряло сутки целиком на любом сроке, не кратном им:
        4380 минут читались как «73 часа», а 4321 — как «4321 минуту».
        На нашем значении не встретится, но одна правка конфига — и клиент
        получает бессмыслицу."""
        assert duration_phrase(minutes) == expected

    def test_a_span_with_days_always_says_days(self):
        """Отношение, а не перечень: ЛЮБОЙ срок от суток и выше обязан
        начинаться с суток, чем бы ни был остаток."""
        for extra in (0, 1, 59, 60, 61, 719, 1439):
            rendered = duration_phrase(3 * 24 * 60 + extra)
            assert rendered.startswith("трое суток"), (extra, rendered)

    def test_days_and_hours_never_borrow_each_others_noun(self):
        """Отношение, а не снимок: одно и то же число суток и часов обязано
        давать РАЗНЫЕ существительные. Мутация «сутки → часы» именно этим и
        ловится, даже если когда-нибудь поменяются сами формулировки."""
        for n in (1, 2, 3, 4, 5, 11, 12, 13, 14, 21, 22):
            in_days = duration_phrase(n * 24 * 60)
            in_hours = duration_phrase(n * 60)
            assert in_days != in_hours, n
            assert "сут" in in_days, in_days
            assert "час" in in_hours, in_hours
            assert "сут" not in in_hours, in_hours
            assert "час" not in in_days, in_days

    def test_the_products_own_setting_reads_as_three_days(self):
        """Связь текста с шаблоном клиента: сколько стоит в конфиге, столько
        и произносится. Литерал 4320 здесь не нужен — берём из файла."""
        template = yaml.safe_load(TRIX_TEMPLATE_PATH.read_text(encoding="utf-8"))
        idle_minutes = template["session_reset"]["idle_minutes"]

        assert duration_phrase(idle_minutes) == "трое суток"

    def test_outside_russian_the_upstream_format_is_kept(self, monkeypatch):
        """Русские слова в английской фразе дают смесь («We haven't talked
        for трое суток»). Тот же приём, что в ``trix_status``."""
        monkeypatch.setenv("HERMES_LANGUAGE", "en")

        assert duration_phrase(4320) == "72h"
        assert duration_phrase(90) == "1h 30m"
        assert duration_phrase(30) == "30m"
        assert not _has_cyrillic(duration_phrase(4320))

    def test_a_missing_span_falls_back_to_the_number_the_product_uses(self):
        """``gateway/config.py::_validate_gateway_config`` заменяет None и
        неположительное значение ровно на 1440 минут. Назвать клиенту любое
        другое число значило бы назвать срок, которым продукт не пользуется.

        Запасной вариант не декоративен: валидатор правит ТОЛЬКО
        ``config.default_reset_policy``, а сюда доезжает результат
        ``get_reset_policy(platform, session_type)`` — при наличии
        переопределения это объект из ``reset_by_platform`` /
        ``reset_by_type``, который через валидатор не проходит вовсе.
        """
        from gateway.config import SessionResetPolicy

        assert duration_phrase(None) == duration_phrase(
            SessionResetPolicy().idle_minutes
        )
        assert duration_phrase(0) == duration_phrase(SessionResetPolicy().idle_minutes)


# ---------------------------------------------------------------------------
# Уведомление о новом разговоре
# ---------------------------------------------------------------------------


class TestSessionResetNotice:
    def test_every_reason_speaks_russian(self):
        for reason in RESET_REASONS:
            text = session_reset_notice(reason, idle_minutes=4320)
            assert _has_cyrillic(text), reason

    def test_no_reason_repeats_the_upstream_english_line(self):
        """Апстримная строка — не просто чужой язык: «Conversation history
        cleared» говорит о потере данных, которой не происходит."""
        upstream_fragments = (
            "Session automatically reset",
            "Conversation history cleared",
            "Use /resume to browse",
            "Adjust reset timing",
            "inactive for",
            "daily schedule",
        )
        for reason in RESET_REASONS:
            text = session_reset_notice(reason, idle_minutes=4320)
            for fragment in upstream_fragments:
                assert fragment not in text, (reason, fragment)

    def test_the_idle_notice_names_the_span_it_was_given(self):
        """Срок в тексте обязан приходить из настройки, а не быть вшит.
        Иначе клиенту с другим ``idle_minutes`` текст врал бы."""
        three_days = session_reset_notice("idle", idle_minutes=4320)
        twelve_hours = session_reset_notice("idle", idle_minutes=720)

        assert "трое суток" in three_days
        assert "12 часов" in twelve_hours
        assert three_days != twelve_hours

    def test_each_reason_reads_differently(self):
        """Ветки причин обязаны различаться попарно. Подмена одной ветки на
        другую — молчание вместо /stop, /stop вместо перезапуска — оставляет
        клиента с объяснением, которое к его случаю не относится."""
        rendered = {
            reason: session_reset_notice(reason, idle_minutes=4320)
            for reason in ("idle", "suspended", "resume_pending_expired", "daily")
        }
        assert len(set(rendered.values())) == len(rendered), rendered

    def test_an_unknown_reason_reads_as_the_silence_notice(self):
        """Тот же выбор, что в апстриме: неназванная причина уходит в самую
        частую и самую безобидную ветку, а не в пустоту."""
        assert session_reset_notice("", idle_minutes=4320) == session_reset_notice(
            "idle", idle_minutes=4320
        )
        assert session_reset_notice(None, idle_minutes=4320) == session_reset_notice(
            "idle", idle_minutes=4320
        )
        assert session_reset_notice(
            "чего-то-такого-нет", idle_minutes=4320
        ) == session_reset_notice("idle", idle_minutes=4320)

    def test_every_reason_tells_the_client_the_previous_talk_is_not_lost(self):
        """Ровно то, что было неверно в апстримной строке: разговор
        сохраняется (старая строка в state.db закрывается причиной
        ``session_reset``, а не удаляется), и вернуть его можно /resume."""
        for reason in RESET_REASONS:
            text = session_reset_notice(reason, idle_minutes=4320)
            assert "/resume" in text, reason

    # Тест «вторая дорога» переехал в TestEveryClientTextAtOnce и стал
    # общим для всех четырёх текстов: обоснование «/resume показывает лишь
    # десять последних, нужный может не попасть» оказалось неверным — ключ
    # сессии включает идентификатор темы, у каждой темы свои десять слотов.
    # Настоящая причина другая и хуже: /resume отсеивает разговоры без
    # заголовка, и такой разговор не появится там вообще, никогда.


# ---------------------------------------------------------------------------
# Сообщения о перезапуске
# ---------------------------------------------------------------------------


class TestGatewayRestartMessages:
    def test_all_three_speak_russian(self):
        for name, text in _client_texts().items():
            if not name.startswith("gateway_restart"):
                continue
            assert _has_cyrillic(text), name

    def test_none_repeats_the_upstream_english_literal(self):
        upstream_literals = (
            "Gateway online",
            "is back and ready",
            "Gateway restarted successfully",
            "Your session continues",
            "Recovered reply",
            "the gateway restarted during delivery",
            "this may be a duplicate",
        )
        for name, text in _client_texts().items():
            for literal in upstream_literals:
                assert literal not in text, (name, literal)

    def test_they_read_differently_from_each_other(self):
        assert (
            len(
                {
                    gateway_back_online_message(),
                    gateway_restarted_message(),
                    recovered_reply_prefix(),
                }
            )
            == 3
        )

    def test_the_recovered_prefix_still_introduces_the_reply_it_precedes(self):
        """Это приставка, а не отдельное сообщение: за ней сразу склеивается
        сам ответ (``content = RECOVERED_MARKER + content``). Без двоеточия
        и пустой строки на конце ответ прирастает к предупреждению."""
        prefix = recovered_reply_prefix()
        assert prefix.endswith(":\n\n"), repr(prefix)
        assert (prefix + "ответ").endswith("\n\nответ")

    def test_the_delivery_ledger_marker_is_this_very_text(self):
        """``gateway/run.py`` берёт приставку как
        ``gateway.delivery_ledger.RECOVERED_MARKER``. Проверяем сам стык, а
        не то, что обе стороны по отдельности «выглядят правильно»."""
        import gateway.delivery_ledger as delivery_ledger

        assert delivery_ledger.RECOVERED_MARKER == recovered_reply_prefix()

    def test_the_ledger_module_still_refuses_unknown_attributes(self):
        """``RECOVERED_MARKER`` отдаётся модульным ``__getattr__``. Если он
        начнёт отвечать на что угодно, опечатка в имени импорта перестанет
        быть ошибкой и станет молчаливой пустой строкой."""
        import gateway.delivery_ledger as delivery_ledger

        with pytest.raises(AttributeError):
            delivery_ledger.RECOVERED_MARKER_TYPO


# ---------------------------------------------------------------------------
# Инварианты, общие для всех текстов задачи
# ---------------------------------------------------------------------------


class TestEveryClientTextAtOnce:
    """Проверки идут разом по всему набору намеренно.

    Дефект «клиент читает английское / читает про файл, которого не
    видит» — пятый экземпляр одного класса в этой ветке. Каждый прошлый
    раз он находился в тексте, который забыли включить в проверку, а не в
    тексте, который проверили и пропустили.
    """

    def test_the_set_is_not_empty_and_covers_every_reason(self):
        texts = _client_texts()
        assert len(texts) == len(RESET_REASONS) + 3, sorted(texts)
        assert all(text.strip() for text in texts.values()), texts

    def test_no_text_names_the_upstream_product(self):
        """Сравнение без учёта регистра намеренно: прежняя версия перечисляла
        варианты написания руками и пропускала «hermes» и «шлюз» строчными —
        сторож, который ловит только заглавную букву, не сторож."""
        forbidden = ("hermes", "nous research", "nous", "gateway", "шлюз")
        for name, text in _client_texts().items():
            lowered = text.lower()
            for token in forbidden:
                assert token not in lowered, (name, token)

    def test_every_text_that_names_resume_offers_a_second_road(self):
        """``/resume`` отсеивает разговоры БЕЗ ЗАГОЛОВКА
        (``_handle_resume_command``: ``[s for s in sessions if s.get("title")]``),
        поэтому безымянный разговор не появится там никогда — ни первым, ни
        десятым. Значит «вернуть его: /resume» в одиночку — необеспеченное
        обещание того же класса, что и апстримное «поправьте config.yaml».

        Вторая дорога правдива, пока у клиента включён поиск по прошлым
        разговорам; проверяем связь с шаблоном, а не наличие слова.
        """
        template = yaml.safe_load(TRIX_TEMPLATE_PATH.read_text(encoding="utf-8"))
        assert "session_search" in template["platform_toolsets"]["telegram"], (
            "поиск по прошлым разговорам выключен — обещание «попросите, "
            "я поищу» перестало быть правдой во всех четырёх текстах"
        )

        named_resume = {
            name: text
            for name, text in _client_texts().items()
            if "/resume" in text
        }
        assert len(named_resume) >= 4, named_resume
        for name, text in named_resume.items():
            assert "попросите" in text.lower(), (
                f"{name}: обещан только /resume, у которого разговор без "
                "заголовка не появится вообще"
            )

    def test_no_text_carries_another_cases_marker(self):
        """Попарная различимость всех семи текстов разом.

        Перестановка значений двух ключей в каталоге оставляет оба текста
        на месте и различными, поэтому проверка «все семь попарно не равны»
        её не видит. Ловится только привязкой каждого текста к СВОЕМУ
        случаю: свой маркер обязан быть, чужие — обязаны отсутствовать.
        """
        texts = _marked_texts()
        assert set(texts) == set(_CASE_MARKERS), (sorted(texts), sorted(_CASE_MARKERS))
        for name, text in texts.items():
            lowered = text.lower()
            assert _CASE_MARKERS[name] in lowered, (
                f"{name} потерял свой маркер {_CASE_MARKERS[name]!r}: {text!r}"
            )
            for other, marker in _CASE_MARKERS.items():
                if other == name:
                    continue
                assert marker not in lowered, (
                    f"{name} несёт маркер случая {other!r} ({marker!r}) — "
                    "тексты переставлены местами"
                )

    def test_all_seven_texts_are_pairwise_distinct(self):
        texts = _marked_texts()
        assert len(set(texts.values())) == len(texts), texts

    def test_no_text_sends_the_client_to_a_file_they_cannot_open(self):
        """Клиент общается с агентом только через Telegram: ни консоли, ни
        файлового менеджера. Совет «поправьте config.yaml» для него — тупик,
        а не помощь."""
        forbidden = (
            "config.yaml",
            "config.yml",
            ".env",
            "session_reset",
            "idle_minutes",
            "~/",
            "/home/",
            "/etc/",
            ".hermes",
            "yaml",
        )
        for name, text in _client_texts().items():
            lowered = text.lower()
            for token in forbidden:
                assert token.lower() not in lowered, (name, token)

    def test_no_text_degrades_to_a_catalog_key_path(self, monkeypatch):
        """``agent.i18n._load_catalog`` глотает любую ошибку чтения каталога
        и кеширует для процесса ПУСТОЙ словарь. Без ``default=`` у вызова
        ``t()`` клиент получил бы в чат ``trix.session_reset.idle``.
        Воспроизведено исполнением в первой задаче ветки — снятие
        ``default=`` ловится именно здесь."""
        monkeypatch.setattr(i18n_mod, "_load_catalog", lambda lang: {})

        for name, text in _client_texts().items():
            assert not text.startswith("trix."), (name, text)
            assert "trix.session_reset" not in text, (name, text)
            assert "trix.gateway_restart" not in text, (name, text)
            assert len(text.split()) >= 3, (name, text)

    def test_the_reasons_stay_distinct_when_the_catalog_is_unreadable(
        self, monkeypatch
    ):
        """Запасные литералы — не заглушка «что-нибудь по-английски»: на
        сломанном каталоге клиент всё равно обязан узнать СВОЙ случай."""
        monkeypatch.setattr(i18n_mod, "_load_catalog", lambda lang: {})

        rendered = {
            reason: session_reset_notice(reason, idle_minutes=4320)
            for reason in ("idle", "suspended", "resume_pending_expired", "daily")
        }
        assert len(set(rendered.values())) == len(rendered), rendered

    @pytest.mark.parametrize("reason", ["idle", "suspended", "daily"])
    def test_a_key_missing_only_from_russian_never_mixes_two_languages(
        self, reason, monkeypatch
    ):
        """Дефект, найденный ревью: чинить надо было корень, а не случай.

        ``t()`` идёт по трём ступеням — русский каталог, английский
        каталог, ``default=``. Прошлая починка закрывала только третью
        (сломан весь каталог). Достаточно, чтобы ОДИН ключ пропал из
        ``ru.yaml`` при целом ``en.yaml``, и клиент получал английское
        предложение с русской вставкой: «We haven't talked here for трое
        суток». Здесь воспроизводится ровно вторая ступень.
        """
        real = i18n_mod._load_catalog
        key = f"trix.session_reset.{reason}"

        def _russian_catalog_lost_one_key(lang):
            catalog = dict(real(lang))
            if lang == "ru":
                catalog.pop(key, None)
            return catalog

        monkeypatch.setattr(i18n_mod, "_load_catalog", _russian_catalog_lost_one_key)

        text = session_reset_notice(reason, idle_minutes=4320)
        assert not _has_cyrillic(text), text
        if reason == "idle":
            # Английская фраза обязана нести английский срок, а не потерять
            # его: сведений клиент лишаться не должен.
            assert "72h" in text, text

    def test_the_unreadable_catalog_fallback_never_mixes_two_languages(
        self, monkeypatch
    ):
        """Найдено исполнением этого теста: первая версия модуля подставляла
        русский срок в английский запасной литерал, и клиент на сломанном
        каталоге читал «We haven't talked here for трое суток». Английский
        запасной вариант — уже деградация; смесь двух языков хуже неё."""
        monkeypatch.setattr(i18n_mod, "_load_catalog", lambda lang: {})

        for name, text in _client_texts().items():
            assert not _has_cyrillic(text), (name, text)
        assert "72h" in session_reset_notice("idle", idle_minutes=4320)
