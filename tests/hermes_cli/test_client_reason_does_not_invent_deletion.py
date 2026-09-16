"""Окно подтверждения говорит клиенту то, что произошло, а не то, чего боятся.

Снято с клиентской установки 2026-09-10. Агенту дали вебхук Битрикса, он
позвал его однострочником на питоне — обычный HTTP-запрос, ничего не
удаляющий, — и клиент прочитал в окне подтверждения:

    запуск однострочной программы, которая удаляет файлы

Ничего не удалялось. Правило апстрима ``script execution via -e/-c flag``
срабатывает на ЛЮБОЙ ``python -c``, не заглядывая внутрь строки (это
записано и в комментарии соседнего теста), а наш перевод приписывал ему
удаление как факт.

Тот же перекос был у всех четырёх описаний, приходящих мимо таблицы
паттернов: апстрим в каждом говорит «не смог проверить» или «может»,
а перевод — «удаляет файлы». Формулировка про последствие для клиента —
верная мысль (клиенту нечего делать со словами «parser limit»), но
последствие нельзя выдумывать.

**Сигнал безопасности при этом обязан уцелеть.** У нас есть свой разбор
содержимого (``detect_delete_in_code``), который умеет заглянуть внутрь
``-c``. Когда он находит настоящее удаление, слово «удаление» — правда, и
оно должно остаться. Тесты держат обе стороны: без находки не выдумывать,
с находкой не молчать.
"""

from __future__ import annotations

import re

import pytest

# Слова, которыми текст утверждает удаление как свершившийся факт.
#
# Корня «удал» мало: «проверить не УДАЛось» содержит его же, не говоря ни о
# каком удалении. Первая редакция теста на этом и дала два ложных падения —
# поэтому здесь перечислены формы глагола «удалить» и «стирать», а не корень.
_DELETION_CLAIM_RE = re.compile(
    r"удал(ени|ен|ён|яет|ять|ит|ил|яя)|стира(ет|ть|ни)", re.IGNORECASE
)


def _claims_deletion(text: str) -> bool:
    return bool(_DELETION_CLAIM_RE.search(text))


@pytest.fixture
def verdicts():
    """Описания, приходящие мимо таблицы паттернов удаления."""
    from tools import approval

    return {
        "inline": "script execution via -e/-c flag",
        "heredoc": "script execution via heredoc",
        "execute_code": approval._EXECUTE_CODE_DESCRIPTION,
        "parser_limit": approval._PARSER_LIMIT_DESCRIPTION,
        "malformed": approval._MALFORMED_EXEC_DESCRIPTION,
    }


class TestNoInventedDeletion:
    @pytest.mark.parametrize(
        "kind", ["inline", "heredoc", "execute_code", "parser_limit", "malformed"]
    )
    def test_verdict_does_not_assert_deletion(self, verdicts, kind):
        """Ни одно из этих правил не доказывает удаления — значит не заявляет."""
        from hermes_cli.trix_sandbox_guard import client_reason_ru

        description = verdicts[kind]
        text = client_reason_ru(description)

        assert not _claims_deletion(text), (
            f"описание {description!r} переведено как {text!r} — оно утверждает "
            "удаление, которого никто не находил"
        )

    @pytest.mark.parametrize(
        "kind", ["inline", "execute_code", "parser_limit", "malformed"]
    )
    def test_verdict_is_still_translated_and_says_something(self, verdicts, kind):
        """Убрать вымысел — не значит вернуть клиенту английский абзац."""
        from hermes_cli.trix_sandbox_guard import client_reason_ru

        description = verdicts[kind]
        text = client_reason_ru(description)

        assert text != description, "перевод пропал, клиент получит английский"
        assert any("а" <= ch <= "я" for ch in text.lower()), "текст не по-русски"


class TestRealDeletionStillSaysDeletion:
    """Обратная сторона: там, где удаление ЕСТЬ, слово обязано остаться."""

    @pytest.mark.parametrize(
        "pattern_key",
        [
            "delete in root path",
            "recursive delete",
            "find -delete",
            "git clean with force (deletes untracked files)",
        ],
    )
    def test_genuine_delete_patterns_keep_the_word(self, pattern_key):
        from hermes_cli.trix_sandbox_guard import client_reason_ru

        assert _claims_deletion(client_reason_ru(pattern_key)), (
            f"{pattern_key!r} — настоящий паттерн удаления, клиент обязан "
            "прочитать про удаление"
        )

    def test_our_own_scanner_still_finds_deletion_inside_a_one_liner(self):
        """Содержимое `-c` мы разбираем сами — сигнал не потерян."""
        from hermes_cli.trix_sandbox_guard import is_terminal_delete

        assert is_terminal_delete(
            "python -c \"import shutil; shutil.rmtree('/workspace')\""
        )

    def test_a_harmless_one_liner_is_not_a_deletion(self):
        """Ровно та команда, на которой клиент увидел неправду."""
        from hermes_cli.trix_sandbox_guard import is_terminal_delete

        assert not is_terminal_delete(
            "python -c \"import urllib.request; "
            "print(urllib.request.urlopen('https://example.com').status)\""
        )


class TestCombinedReasonKeepsBothHalves:
    def test_scanner_finding_and_delete_pattern_are_both_translated(self):
        """Склейка через '; ' переводится по частям — и половина про
        удаление остаётся правдой, а половина про однострочник перестаёт
        врать."""
        from hermes_cli.trix_sandbox_guard import client_reason_ru

        combined = "recursive delete; script execution via -e/-c flag"
        text = client_reason_ru(combined)

        assert "; " in text, "склейка развалилась"
        first, second = text.split("; ", 1)
        assert _claims_deletion(first), "настоящее удаление потеряло слово"
        assert not _claims_deletion(second), "однострочник снова врёт про удаление"
