"""Агент должен понимать, что «бот» — это он сам.

Наблюдение за живым агентом 2026-09-06: на «покажи логи, у меня бот
тупит» он искал ЧУЖОГО бота в рабочей папке, не нашёл и попросил клиента
прислать journalctl. Подсказка telegram была чисто про форматирование —
в отличие от desktop и cron, где самоидентификация есть.
"""

from agent.prompt_builder import PLATFORM_HINTS


class TestTelegramHintIdentifiesTheAgent:
    def test_says_the_bot_is_the_agent_itself(self):
        hint = PLATFORM_HINTS["telegram"].lower()
        assert "the bot" in hint
        assert "they mean you" in hint

    def test_covers_the_russian_word_the_client_actually_uses(self):
        """Клиент пишет по-русски; «бот» — то самое слово из наблюдения."""
        assert "бот" in PLATFORM_HINTS["telegram"]

    def test_forbids_hunting_for_a_third_party_bot(self):
        """Это и была наблюдаемая ошибка, а не абстрактный риск."""
        hint = PLATFORM_HINTS["telegram"].lower()
        assert "third-party bot" in hint

    def test_keeps_the_formatting_guidance_it_had(self):
        """Самоидентификация добавлена, а не подменила собой инструкцию."""
        hint = PLATFORM_HINTS["telegram"]
        assert "Markdown" in hint
        assert "MEDIA:" in hint

    def test_matches_the_shape_other_surfaces_already_use(self):
        """desktop и cron давно говорят агенту, где он находится."""
        assert "desktop app" in PLATFORM_HINTS["desktop"].lower()
        assert "cron job" in PLATFORM_HINTS["cron"].lower()
