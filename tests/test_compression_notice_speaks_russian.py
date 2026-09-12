"""Сообщение о сдвиге порога сжатия читает КЛИЕНТ — значит по-русски и без «hermes».

Замечено владельцем на клиентской установке 2026-09-10. Клиент выбрал
модель Codex, и в Telegram пришло:

    ℹ Codex gpt-5.6-sol caps context at 272K, so auto-compaction was raised
    to 75% (from 50%) to use more of the window before summarizing.
      Opt back out: hermes config set compression.codex_gpt55_autoraise false

Два дефекта в одной строке. Английский текст в продукте, где «клиент
открывает Telegram и видит русский язык везде» (спека 3). И имя апстрима
плюс консольная команда — в канале, где у клиента консоли нет вовсе
(решение владельца 2026-09-09).

Почему это не поймал главный приёмочный тест: он стережёт системный
промпт, схемы инструментов, скиллы и подсказку про удалённый терминал.
Сообщения о состоянии, которые шлюз досылает клиенту через
``status_callback``, — отдельный канал доставки, и сторожа у него не было.
"""

from __future__ import annotations

import pytest

_AUTORAISE = {"model": "gpt-5.6-sol", "from": 0.5, "to": 0.75}


@pytest.fixture
def notice_ru(monkeypatch):
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    from agent.i18n import reset_language_cache
    from agent.agent_init import _build_codex_gpt5_autoraise_notice

    reset_language_cache()
    return _build_codex_gpt5_autoraise_notice(_AUTORAISE, context_length=272_000)


class TestTheClientReadsRussian:
    def test_no_latin_prose_reaches_the_client(self, notice_ru):
        """Имя модели и проценты латиницей — законны. Английские СЛОВА — нет."""
        forbidden = ("caps context", "auto-compaction", "summarizing", "Opt back out")
        found = [phrase for phrase in forbidden if phrase in notice_ru]
        assert not found, f"английский текст доехал до клиента: {found}"

    def test_it_actually_says_something_in_russian(self, notice_ru):
        assert "сжимается позже" in notice_ru
        assert "пересказ" in notice_ru

    def test_the_numbers_survive_translation(self, notice_ru):
        """Ради них сообщение и существует: клиент должен понять, что
        изменилось и насколько."""
        assert "gpt-5.6-sol" in notice_ru
        assert "272K" in notice_ru
        assert "75%" in notice_ru
        assert "50%" in notice_ru


class TestNeitherUpstreamNorAConsoleCommand:
    def test_the_upstream_name_is_gone(self, notice_ru):
        assert "hermes" not in notice_ru.lower()

    def test_no_shell_command_is_handed_to_the_client(self, notice_ru):
        """У клиента единственный интерфейс — Telegram. Команда, которую он
        не может выполнить, хуже молчания: она посылает его туда, где его
        нет."""
        assert "config set" not in notice_ru

    def test_the_setting_is_still_named_for_support(self, notice_ru):
        """Назвать настройку — не то же, что дать команду. Поддержке этого
        хватает, чтобы починить, клиенту не мешает."""
        assert "compression.codex_gpt55_autoraise" in notice_ru


class TestEnglishStaysAvailable:
    def test_english_catalog_carries_the_same_message(self, monkeypatch):
        """Каталог en — источник истины; ключ обязан быть и там, иначе
        падение в английский вернуло бы путь ключа вместо текста."""
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        from agent.i18n import reset_language_cache
        from agent.agent_init import _build_codex_gpt5_autoraise_notice

        reset_language_cache()
        text = _build_codex_gpt5_autoraise_notice(_AUTORAISE, context_length=272_000)

        assert "trix.compression" not in text, "ключ не нашёлся в каталоге"
        assert "gpt-5.6-sol" in text
        assert "hermes" not in text.lower()
