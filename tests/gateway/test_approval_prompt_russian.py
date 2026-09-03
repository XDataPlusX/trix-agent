"""Окно подтверждения в Telegram — по-русски и про дело, а не про команду."""

import pytest

from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.fixture(autouse=True)
def _use_russian_ui_language(monkeypatch):
    """NOT a workaround -- this file's whole point is Russian localization,
    so it opts out of ``tests/gateway/conftest.py``'s blanket
    ``_pin_english_ui_language`` (English by default for every gateway
    test) the way that fixture's own docstring says to: monkeypatch
    ``HERMES_LANGUAGE``. Do not remove this without replacing every
    ``adapter._format_exec_approval(...)`` call below with an explicit
    ``lang=`` -- there is none, so removing the fixture silently makes
    every "is it Russian" assertion here run against English text instead.

    This only proves the OVERRIDE renders Russian. It does not prove the
    client's actual (env-var-free) configuration does -- see
    ``test_production_default_renders_russian_without_any_env_override``
    below, which deletes the env var this fixture sets.
    """
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")


@pytest.fixture
def adapter():
    return TelegramAdapter.__new__(TelegramAdapter)


def test_header_and_reason_are_russian(adapter):
    text = adapter._format_exec_approval(
        "rm -rf /workspace/проект", "recursive delete"
    )
    assert "Command Approval Required" not in text
    assert "Reason:" not in text
    assert "удаление каталога вместе со всем содержимым" in text


def test_command_is_still_shown(adapter):
    """Команду показываем — она объясняет, ЧТО именно исчезнет."""
    text = adapter._format_exec_approval("rm -rf /workspace/проект", "recursive delete")
    assert "/workspace/проект" in text


def test_unknown_reason_falls_back_to_the_upstream_text(adapter):
    text = adapter._format_exec_approval("mkfs.ext4 /dev/sda", "format filesystem")
    assert "format filesystem" in text


def test_buttons_are_russian():
    from plugins.platforms.telegram.adapter import _approval_button_labels

    labels = _approval_button_labels(lang="ru")
    assert labels["once"] == "Разрешить"
    assert labels["deny"] == "Отказать"
    for value in labels.values():
        assert value.isascii() is False, f"кнопка осталась английской: {value!r}"


def test_timeout_note_warns_before_silence(adapter):
    """Клиент должен узнать про правило тишины ДО того, как промолчит, а не
    после: молчание дольше окна ожидания = отказ, действие не выполнится."""
    text = adapter._format_exec_approval("rm -rf /workspace/проект", "recursive delete")
    assert "Если не ответить, действие не будет выполнено." in text
    # Идёт в конце текста, после причины.
    assert text.index("удаление каталога") < text.index("Если не ответить")


def test_english_path_unaffected_by_timeout_note(adapter, monkeypatch):
    """timeout_note пуст в английском каталоге -- добавление должно быть
    no-op побайтово. Причина намеренно НЕ из SANDBOX_DELETE_RU, чтобы тест
    проверял только подключение timeout_note, а не отдельный (и не
    зависящий от языка) перевод причины."""
    monkeypatch.setenv("HERMES_LANGUAGE", "en")
    text = adapter._format_exec_approval("rm -rf /", "dangerous command")
    assert text == (
        "⚠️ <b>Command Approval Required</b>\n\n"
        "<pre>rm -rf /</pre>\n\n"
        "Reason: dangerous command"
    )


def test_production_default_renders_russian_without_any_env_override(adapter, monkeypatch):
    """Боевая конфигурация: у клиента НЕТ ``HERMES_LANGUAGE`` в окружении --
    язык приходит только из ``display.language``, дефолт которого в
    ``DEFAULT_CONFIG`` равен ``"ru"``. Тест удаляет и пин из
    ``tests/gateway/conftest.py`` (English), и пин из фикстуры этого файла
    (``ru`` через env), так что резолвинг идёт по тому же пути, что и у
    настоящего клиента в свежем ``HERMES_HOME`` без ``config.yaml``
    (автоюз-фикстура ``_hermetic_environment`` из tests/conftest.py именно
    такой каталог и создаёт).
    """
    monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
    from agent.i18n import reset_language_cache

    # Сброс process-wide lru_cache: в этом файле выше уже резолвился язык при
    # HERMES_LANGUAGE, заданном явно, но кэш общий на процесс/файл-подпроцесс
    # -- без сброса можно словить чужой результат из более раннего теста.
    reset_language_cache()

    text = adapter._format_exec_approval(
        "rm -rf /workspace/проект", "recursive delete"
    )

    assert "Command Approval Required" not in text
    assert "удаление каталога вместе со всем содержимым" in text
    assert "Если не ответить, действие не будет выполнено." in text


# --------------------------------------------------------------------------
# Инвариант покрытия: КАЖДОЕ описание, которое физически доезжает до окна
# подтверждения клиента, обязано иметь русскую формулировку.
#
# Прежний инвариант (``SANDBOX_DELETE_PATTERN_KEYS == set(SANDBOX_DELETE_RU)``)
# истинен по построению и упасть не может: оба множества ведёт один и тот же
# человек в одном и том же файле, а описания, приезжающие НЕ из таблицы
# паттернов, он не покрывает вовсе. Ровно поэтому мимо него проехали три
# английских абзаца на самых частых путях удаления.
#
# Достижимое множество здесь НЕ переписано руками — оно добывается
# ИСПОЛНЕНИЕМ настоящих путей продукта, причём НА ТОЙ ВЫСОТЕ, где текст
# действительно собирается перед отправкой клиенту:
#   * терминал — из ``check_all_command_guards`` на настоящих клиентских
#     командах. Это и есть точка сборки: описание клиенту не равно описанию
#     паттерна, оно склеивается через "; " из ВСЕХ сработавших предупреждений
#     (находка сканера Tirith + паттерн удаления). Добыча этажом ниже, у
#     ``detect_dangerous_command``, склейку не видит вовсе — и однажды уже
#     пропустила английский абзац сканера прямиком в окно клиента;
#   * ``execute_code`` — из настоящего ``check_execute_code_guard`` на
#     удаляющем скрипте: у него своя точка сборки и свой payload;
#   * ключи удаления — из ``SANDBOX_DELETE_PATTERN_KEYS`` (их и вправду ведёт
#     тот же файл, но их достижимость доказана отдельными тестами guard'а);
#   * синтетические вердикты «разобрать не смог» — из
#     ``uninspectable_verdicts()``, то есть из того же множества, которое
#     guard пропускает наружу.
# Переименование любого из них наверху меняет добытую строку, для неё нет
# перевода, и тест краснеет — вместо тихого возврата к английскому тексту.
# --------------------------------------------------------------------------

_DELETING_ONE_LINER = "python -c \"import shutil; shutil.rmtree('/workspace/x')\""
_DELETING_SCRIPT = "import shutil\nshutil.rmtree('/workspace/x')\n"

# Команды в том виде, в каком их пишет модель на обычную клиентскую просьбу
# прибраться. Вторая — воспроизведение дефекта: сканер срабатывает ВМЕСТЕ с
# паттерном удаления, и до клиента доезжает склейка, которой нет ни в одном
# словаре.
_CLIENT_DELETE_COMMANDS = (
    "rm -rf /workspace/старое",
    "find /workspace -name '*.log' -delete && rm -rf ~/.cache",
    "git clean -fd",
    _DELETING_ONE_LINER,
)

# Дословный вывод настоящего сканера (``tirith check --json`` на команде с
# ``find … -delete``). Сам сканер — внешний бинарь, которого в тестовом
# окружении нет: его ответ подставляется, чтобы склейка воспроизводилась на
# любой машине. Подставляется ГРАНИЦА, а не проверяемый код — точка сборки
# описания работает по-настоящему.
_TIRITH_FIND_DELETE_WARN = {
    "action": "warn",
    "findings": [
        {
            "rule_id": "blast_find_delete",
            "severity": "MEDIUM",
            "title": "find with -delete recursively removes matching files",
            "description": (
                "A `find … -delete` traverses the directory tree and unlinks "
                "every matching entry. Run `tirith preview` to see how many "
                "files this would remove before executing it."
            ),
        }
    ],
    "summary": "",
}
_TIRITH_SILENT = {"action": "allow", "findings": [], "summary": ""}


@pytest.fixture
def manual_approvals(tmp_path, monkeypatch):
    """Клиентский режим подтверждений: спрашивает человек, не вспомогательная
    модель. Пишем настоящий ``config.yaml`` в изолированный HERMES_HOME, а не
    мокаем ``_get_approval_mode`` — путь резолвинга тогда тот же, что у
    клиента."""
    from hermes_cli import config as config_mod

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("approvals:\n  mode: manual\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(config_mod, "_config_cache", None, raising=False)
    monkeypatch.setattr(config_mod, "_config_cache_path", None, raising=False)
    yield home
    monkeypatch.setattr(config_mod, "_config_cache", None, raising=False)


def _reachable_reason_descriptions(monkeypatch) -> set:
    """Описания, которые доезжают до ``send_exec_approval``, добытые из кода."""
    from tools import tirith_security
    from tools.approval import check_all_command_guards, check_execute_code_guard
    from hermes_cli.trix_sandbox_guard import (
        SANDBOX_DELETE_PATTERN_KEYS,
        uninspectable_verdicts,
    )

    reachable = set(SANDBOX_DELETE_PATTERN_KEYS) | set(uninspectable_verdicts())

    # Без notify-колбэка шлюза guard возвращает тот же payload как
    # ``pending_approval`` — то есть ровно ту строку, которая ушла бы в
    # ``send_exec_approval``.
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")

    def _harvest_terminal(scan: dict) -> None:
        """Терминал: точка сборки — check_all_command_guards, не детектор."""
        monkeypatch.setattr(
            tirith_security, "check_command_security", lambda command: dict(scan)
        )
        for command in _CLIENT_DELETE_COMMANDS:
            result = check_all_command_guards(command, "docker", has_host_access=False)
            assert result.get("status") == "pending_approval", (
                f"guard больше не спрашивает про {command!r} — тест ниже проверял "
                f"бы перевод описания, которого больше нет: {result}"
            )
            reachable.add(result["description"])

    _harvest_terminal(_TIRITH_SILENT)      # одно описание паттерна
    _harvest_terminal(_TIRITH_FIND_DELETE_WARN)  # склейка: сканер + паттерн

    # execute_code: настоящий guard на удаляющем скрипте.
    result = check_execute_code_guard(code=_DELETING_SCRIPT, env_type="docker")
    assert result.get("status") == "pending_approval", (
        f"execute_code guard больше не спрашивает про удаление: {result}"
    )
    reachable.add(result["description"])
    return reachable


def test_every_reachable_reason_has_a_russian_wording(adapter, monkeypatch, manual_approvals):
    reachable = _reachable_reason_descriptions(monkeypatch)
    untranslated = sorted(
        description
        for description in reachable
        if adapter._ea_reason_text(description) == description
    )
    assert not untranslated, (
        "до клиента доезжает английское описание без русской формулировки: "
        f"{untranslated}"
    )


def test_every_reachable_reason_renders_in_cyrillic(adapter, monkeypatch, manual_approvals):
    """Мало отличаться от оригинала — формулировка обязана быть русской."""
    reachable = _reachable_reason_descriptions(monkeypatch)
    not_russian = sorted(
        description
        for description in reachable
        if not any("а" <= ch.lower() <= "я" for ch in adapter._ea_reason_text(description))
    )
    assert not not_russian, f"формулировка не по-русски: {not_russian}"


# --------------------------------------------------------------------------
# Итог подтверждения (что клиент видит ПОСЛЕ нажатия) — тоже по-русски.
# --------------------------------------------------------------------------

_OUTCOME_KEYS = (
    "resolved_once", "resolved_session", "resolved_always", "resolved_deny",
    "resolved_fallback", "expired_label", "expired_text",
    "callback_invalid_data", "callback_unauthorized", "callback_already_resolved",
    "confirm_once", "confirm_always", "confirm_cancel",
    "confirm_unauthorized", "confirm_already_resolved",
)

# Дословные литералы, стоявшие в адаптере до локализации. Английский путь
# оператора не имеет права сдвинуться ни на байт — поэтому сверяем не
# «примерно то же», а точное равенство.
_HISTORICAL_ENGLISH = {
    "resolved_once": "✅ Approved once",
    "resolved_session": "✅ Approved for session",
    "resolved_always": "✅ Approved permanently",
    "resolved_deny": "❌ Denied",
    "resolved_fallback": "Resolved",
    "expired_label": "⌛ Approval expired",
    "callback_invalid_data": "Invalid approval data.",
    "callback_unauthorized": "⛔ You are not authorized to approve commands.",
    "callback_already_resolved": "This approval has already been resolved.",
    "confirm_once": "✅ Approved once",
    "confirm_always": "🔒 Always approve",
    "confirm_cancel": "❌ Cancelled",
    "confirm_unauthorized": "⛔ You are not authorized to answer this prompt.",
    "confirm_already_resolved": "This prompt has already been resolved.",
}


@pytest.mark.parametrize("key,literal", sorted(_HISTORICAL_ENGLISH.items()))
def test_english_outcome_literals_are_unchanged(key, literal):
    from plugins.platforms.telegram.adapter import _approval_outcome_text

    assert _approval_outcome_text(f"trix.approval.{key}", lang="en") == literal


def test_english_composed_outcome_lines_are_unchanged():
    """Составные строки собирались f-строками — склейка обязана дать те же байты."""
    from plugins.platforms.telegram.adapter import _approval_outcome_text

    label = _approval_outcome_text("trix.approval.resolved_once", lang="en")
    assert _approval_outcome_text(
        "trix.approval.resolved_by", lang="en", label=label, user="Ivan"
    ) == "✅ Approved once by Ivan"

    expired = _approval_outcome_text("trix.approval.expired_label", lang="en")
    assert _approval_outcome_text(
        "trix.approval.expired_text", lang="en", label=expired
    ) == (
        "⌛ Approval expired — no command was waiting. "
        "It already timed out (and was denied) or was resolved elsewhere."
    )


@pytest.mark.parametrize("key", _OUTCOME_KEYS)
def test_outcome_is_russian(key):
    """Клиент жмёт русскую кнопку — ответ обязан быть на том же языке."""
    from plugins.platforms.telegram.adapter import _approval_outcome_text

    text = _approval_outcome_text(f"trix.approval.{key}", lang="ru", label="", user="")
    assert any("а" <= ch.lower() <= "я" for ch in text), f"итог остался английским: {text!r}"


def test_russian_resolved_line_names_the_person():
    from plugins.platforms.telegram.adapter import _approval_outcome_text

    label = _approval_outcome_text("trix.approval.resolved_once", lang="ru")
    line = _approval_outcome_text(
        "trix.approval.resolved_by", lang="ru", label=label, user="Иван"
    )
    assert "Иван" in line and label in line
    assert "by" not in line
