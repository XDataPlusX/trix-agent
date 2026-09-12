"""Что клиент читает про обновление — и чего он про него НЕ читает.

Разбор жалобы владельца (2026-09-12, после `/update` с боевой машины):
клиенту в чат приехала лента вывода `hermes update` — снимок, git-сброс,
`Resolved 95 packages`, `hermes-agent==0.20.0`, `Refreshing 17 active lazy
backends`. Две беды разом: ход обновления клиенту не нужен (решений по
нему он не принимает), а внутри него поимённо назван апстримный продукт,
которого в Trix быть не должно.
"""

import pytest

from hermes_cli.trix_update_notices import PARTIAL_EXIT_CODE, update_verdict_message

# Коды, которыми обновление вообще может закончиться: успех, частичный
# успех и «всё остальное — отказ». 124 — тайм-аут, который пишет шлюз.
EXIT_CODES = (0, PARTIAL_EXIT_CODE, 1, 3, 124)


@pytest.fixture
def language(monkeypatch):
    """t() резолвит язык из env; тесты сюиты пришпилены к en (conftest)."""

    def _set(lang: str) -> None:
        import agent.i18n as i18n

        monkeypatch.setenv("HERMES_LANGUAGE", lang)
        i18n.reset_language_cache()

    yield _set
    import agent.i18n as i18n

    i18n.reset_language_cache()


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("exit_code", EXIT_CODES)
def test_verdict_never_names_the_upstream_product(language, lang, exit_code):
    """Ни один исход не называет клиенту Hermes/Nous и не зовёт в терминал."""
    language(lang)
    text = update_verdict_message(exit_code)
    assert "Hermes" not in text
    assert "Nous" not in text
    assert "hermes" not in text  # ни `hermes update`, ни `hermes-agent`
    assert "gateway" not in text.lower()
    assert "```" not in text  # блок кода = сюда вклеили лог


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("exit_code", EXIT_CODES)
def test_verdict_is_a_single_short_line(language, lang, exit_code):
    """Вердикт — строка, а не лента: перенос строки означает вклеенный лог."""
    language(lang)
    text = update_verdict_message(exit_code)
    assert text.strip()
    assert "\n" not in text
    assert len(text) < 250, text


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_three_outcomes_are_distinguishable(language, lang):
    """Успех, частичный успех и отказ различимы — и значком, и текстом.

    Проверка попарная: «каждый текст на месте» не ловит перестановку
    значений в каталоге, из-за которой клиент читает про чужой исход.
    """
    language(lang)
    ok = update_verdict_message(0)
    partial = update_verdict_message(PARTIAL_EXIT_CODE)
    failed = update_verdict_message(1)

    assert ok.startswith("✅")
    assert partial.startswith("⚠️")
    assert failed.startswith("❌")
    assert len({ok, partial, failed}) == 3


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_failure_carries_the_exit_code(language, lang):
    """Код выхода — единственная зацепка, которую клиент может переслать.

    Доступа к логам и к терминалу у него нет: он общается с агентом
    только через мессенджер.
    """
    language(lang)
    assert "7" in update_verdict_message(7)


def test_partial_success_is_not_announced_as_a_failure(language):
    """Код 2 — обновлён работающий агент, не обновились компоненты,
    которых у клиента в мессенджере и нет. Объявлять это отказом значит
    назвать сломанным исправный продукт."""
    language("ru")
    text = update_verdict_message(PARTIAL_EXIT_CODE)
    assert "❌" not in text
    assert "частично" in text
