"""Что клиент читает про Telegram-темы (forum topics) — режим нескольких
параллельных сессий в одном боте.

Девять функций в ``gateway/run.py`` (``_telegram_topic_root_lobby_message``,
``_telegram_topic_root_new_message``, ``_telegram_topic_new_header``,
``_ensure_telegram_system_topic``, ``_sanitize_telegram_topic_title``,
``_telegram_topic_help_text``, ``_disable_telegram_topic_mode_for_chat``,
``_telegram_topic_root_status_message``, ``_restore_telegram_topic_session``)
отвечают клиенту по-английски и двенадцать из девятнадцати строк называют
чужой продукт («Hermes chat», «Hermes session», «Hermes Chat» как имя темы
в Telegram).

**Почему отдельный модуль, а не ``t()`` прямо в ``gateway/run.py``.** Тот же
довод, что и у ``trix_session_notices.py``: это готовый, ещё не тронутый
кластер из девяти функций подряд в файле на ~29 тысяч строк, который мы
регулярно подтягиваем сверху. Здесь единый предмет — весь текст, который
Telegram-клиент видит про режим тем, — и один явный внешний контракт
(имя темы, которое реально видит Telegram: ``"System"`` / резервный
заголовок при пустом сгенерированном названии). Собрав его в одном файле,
следующая правка апстрима внутри этих девяти функций конфликтует с одним
компактным модулем, а не с рассеянными по всему ``run.py`` вызовами
``t()``.

**Почему функции возвращают готовые строки, а не только ключи.**
``_telegram_topic_root_status_message`` и ``_restore_telegram_topic_session``
строят многострочный ответ из нескольких кусков (шапка, список сессий,
инструкция). Чтобы апстримный файл остался тонким, сборка этих кусков
тоже здесь — ``gateway/run.py`` передаёт только данные (список сессий,
заголовок, текст последнего сообщения), а не строит текст сам.

**Что НЕ переведено и почему.** ``All Messages``, ``BotFather``,
``Bot Settings → Threads Settings`` — это литералы интерфейса Telegram,
которые клиент ищет глазами у себя в приложении; их перевод сделал бы
подсказку бесполезной. Discord-тема (``_sanitize_discord_thread_title``)
сюда не входит — у продукта нет Discord.
"""

from __future__ import annotations

from typing import Any, Optional

from agent.i18n import t

# ---------------------------------------------------------------------------
# Английские "default=" ниже НЕ везде дословны апстриму: где апстримная
# строка называла чужой бренд («Hermes chat/session»), бренд убран и в
# английском тексте тоже — тот же приём, что уже используется в
# trix.busy.not_paused (gateway/run.py, ~7100 строк выше по патчу): без
# этого default=, отданный при сломанном каталоге, вернул бы клиенту чужое
# имя продукта на любом языке. Комментарии у каждой такой константы это
# отмечают.
# ---------------------------------------------------------------------------

_DEFAULT_ROOT_LOBBY = (
    "This main chat is reserved for system commands.\n\n"
    "To start a new chat, open the All Messages topic at the top "
    "of this bot interface and send any message there. Telegram will "
    "create a new topic for that message; each topic works as an "
    "independent session."
)  # де-брендировано: апстрим говорил "Hermes chat" / "Hermes session"

_DEFAULT_ROOT_NEW = (
    "To start a new parallel chat, open the All Messages topic "
    "at the top of this bot interface and send any message there. "
    "Telegram will create a new topic for it.\n\n"
    "Each topic is an independent session. Use /new inside an "
    "existing topic only if you want to replace that topic's current session."
)  # де-брендировано: апстрим говорил "Hermes chat" / "Hermes session"

_DEFAULT_NEW_HEADER = (
    "Started a new session in this topic.\n\n"
    "Tip: for parallel work, open All Messages and send a message there "
    "to create a separate topic instead of using /new here. /new replaces "
    "the session attached to the current topic."
)  # де-брендировано: апстрим говорил "Hermes session"

# Реальное имя Telegram-темы, которое создаёт _ensure_telegram_system_topic.
_DEFAULT_SYSTEM_TITLE = "System"

_DEFAULT_SYSTEM_INTRO = (
    "System topic for commands and status."
)  # де-брендировано: апстрим говорил "Hermes commands and status"

# Запасное имя темы, когда сгенерированный заголовок сессии пуст
# (_sanitize_telegram_topic_title). Это заголовок Telegram-темы, а не
# сообщение в чат — короткий, без знаков препинания на конце.
_DEFAULT_TITLE_FALLBACK = "Chat"  # де-брендировано: апстрим отдавал "Hermes Chat"

_DEFAULT_HELP_TEXT = (
    "/topic — enable multi-session DM mode (one bot, many parallel chats)\n"
    "\n"
    "Usage:\n"
    "  /topic             Enable topic mode, or show status if already on\n"
    "  /topic help        Show this message\n"
    "  /topic off         Disable topic mode and clear topic bindings\n"
    "  /topic <id>        Inside a topic: restore a previous session by ID\n"
    "\n"
    "How it works:\n"
    "1. Run /topic once in this DM — the agent checks BotFather Threads\n"
    "   Settings are enabled and flips on multi-session mode.\n"
    "2. Tap All Messages at the top of the bot and send any message.\n"
    "   Telegram creates a new topic for that message; each topic is\n"
    "   an independent session (fresh history, fresh context).\n"
    "3. The root DM becomes a system lobby — send /topic, /status,\n"
    "   /help, /usage there. Normal prompts go in a topic.\n"
    "4. /new inside a topic resets just that topic's session.\n"
    "5. /topic <id> inside a topic restores an old session into it."
)  # де-брендировано: апстрим дважды говорил "Hermes session"

_DEFAULT_CHAT_ID_UNRESOLVED = "Could not determine chat ID."

_DEFAULT_MODE_NOT_ENABLED = (
    "Multi-session topic mode is not currently enabled for this chat."
)

_DEFAULT_DISABLE_FAILED = "Failed to disable topic mode: {error}"

_DEFAULT_MODE_DISABLED = (
    "Multi-session topic mode is now OFF for this chat.\n\n"
    "Existing topics in Telegram aren't removed — they'll just stop "
    "being gated as independent sessions. The root DM works as a "
    "normal chat again. Run /topic to re-enable later."
)  # де-брендировано: апстрим говорил "normal Hermes chat"

_DEFAULT_STATUS_ENABLED = "Telegram multi-session topics are enabled."

_DEFAULT_STATUS_CREATE_HINT = (
    "To create a new chat, open All Messages at the top of this "
    "bot interface and send any message there. Telegram will create a "
    "new topic for it."
)  # де-брендировано: апстрим говорил "a new Hermes chat"

_DEFAULT_STATUS_UNLINKED_HEADER = "Previous unlinked sessions:"
_DEFAULT_STATUS_UNTITLED = "Untitled session"
_DEFAULT_STATUS_RESTORE_HEADER = "To restore one:"
_DEFAULT_STATUS_STEP_CREATE = (
    "1. Create or open a topic. To create a new one, open All Messages "
    "and send any message there."
)
_DEFAULT_STATUS_STEP_SEND = "2. Send /topic <session-id> inside that topic."
_DEFAULT_STATUS_EXAMPLE = "Example: Send /topic {session_id} inside a topic."
_DEFAULT_STATUS_NONE_FOUND = "No previous unlinked Telegram sessions found."
_DEFAULT_STATUS_RESTORE_LATER_HEADER = "To restore a previous session later:"

_DEFAULT_SESSION_NOT_FOUND = "Session not found: {session_id}"
_DEFAULT_NOT_TELEGRAM_SESSION = (
    "That session is not a Telegram session and cannot be restored into this topic."
)
_DEFAULT_SESSION_NOT_OWNED = "That session does not belong to this Telegram user."
_DEFAULT_SESSION_ALREADY_LINKED = "That session is already linked to another Telegram topic."
_DEFAULT_SESSION_RESTORED = "Session restored: {title}"
_DEFAULT_LAST_MESSAGE_PREFIX = "\n\nLast message:\n{message}"  # де-брендировано: апстрим говорил "Last Hermes message"


def topic_root_lobby_message(lang: Optional[str] = None) -> str:
    """Ответ в корневой личке, когда режим тем включён и юзер пишет не в теме."""
    return t("trix.topic.root_lobby", lang=lang, default=_DEFAULT_ROOT_LOBBY)


def topic_root_new_message(lang: Optional[str] = None) -> str:
    """Ответ на /new, отправленный из корневой личке (а не из темы)."""
    return t("trix.topic.root_new", lang=lang, default=_DEFAULT_ROOT_NEW)


def topic_new_header(lang: Optional[str] = None) -> str:
    """Заголовок, который предшествует ответу /new внутри Telegram-темы."""
    return t("trix.topic.new_header", lang=lang, default=_DEFAULT_NEW_HEADER)


def system_topic_title(lang: Optional[str] = None) -> str:
    """Имя служебной Telegram-темы, создаваемой при активации /topic."""
    return t("trix.topic.system_title", lang=lang, default=_DEFAULT_SYSTEM_TITLE)


def system_topic_intro_message(lang: Optional[str] = None) -> str:
    """Первое сообщение, закреплённое в служебной теме."""
    return t("trix.topic.system_intro", lang=lang, default=_DEFAULT_SYSTEM_INTRO)


def topic_title_fallback(lang: Optional[str] = None) -> str:
    """Запасное имя Telegram-темы, если сгенерированный заголовок пуст."""
    return t("trix.topic.title_fallback", lang=lang, default=_DEFAULT_TITLE_FALLBACK)


def topic_help_text(lang: Optional[str] = None) -> str:
    """Текст /topic help."""
    return t("trix.topic.help_text", lang=lang, default=_DEFAULT_HELP_TEXT)


def chat_id_unresolved_message(lang: Optional[str] = None) -> str:
    return t("trix.topic.chat_id_unresolved", lang=lang, default=_DEFAULT_CHAT_ID_UNRESOLVED)


def topic_mode_not_enabled_message(lang: Optional[str] = None) -> str:
    return t("trix.topic.mode_not_enabled", lang=lang, default=_DEFAULT_MODE_NOT_ENABLED)


def topic_mode_disable_failed_message(error: Any, lang: Optional[str] = None) -> str:
    return t(
        "trix.topic.disable_failed", lang=lang, error=error,
        default=_DEFAULT_DISABLE_FAILED,
    )


def topic_mode_disabled_message(lang: Optional[str] = None) -> str:
    return t("trix.topic.mode_disabled", lang=lang, default=_DEFAULT_MODE_DISABLED)


def topic_root_status_message(sessions: Optional[list] = None, lang: Optional[str] = None) -> str:
    """Full /topic status reply for the root lobby, given the (possibly
    empty) list of unlinked Telegram sessions. Each entry is a dict with
    ``id`` / ``title`` / ``preview`` keys, matching
    ``SessionDB.list_unlinked_telegram_sessions_for_user``'s rows — this
    function does not touch the database itself.
    """
    lines = [
        t("trix.topic.status_enabled", lang=lang, default=_DEFAULT_STATUS_ENABLED),
        "",
        t("trix.topic.status_create_hint", lang=lang, default=_DEFAULT_STATUS_CREATE_HINT),
        "",
    ]
    sessions = sessions or []
    if sessions:
        lines.append(
            t("trix.topic.status_unlinked_header", lang=lang, default=_DEFAULT_STATUS_UNLINKED_HEADER)
        )
        untitled = t("trix.topic.status_untitled", lang=lang, default=_DEFAULT_STATUS_UNTITLED)
        for session in sessions:
            session_id = str(session.get("id") or "")
            title = str(session.get("title") or untitled)
            preview = str(session.get("preview") or "").strip()
            line = f"- {title} — `{session_id}`"
            if preview:
                line += f" — {preview}"
            lines.append(line)
        lines.extend([
            "",
            t("trix.topic.status_restore_header", lang=lang, default=_DEFAULT_STATUS_RESTORE_HEADER),
            t("trix.topic.status_step_create", lang=lang, default=_DEFAULT_STATUS_STEP_CREATE),
            t("trix.topic.status_step_send", lang=lang, default=_DEFAULT_STATUS_STEP_SEND),
            t(
                "trix.topic.status_example", lang=lang,
                session_id=str(sessions[0].get("id") or ""),
                default=_DEFAULT_STATUS_EXAMPLE,
            ),
        ])
    else:
        lines.extend([
            t("trix.topic.status_none_found", lang=lang, default=_DEFAULT_STATUS_NONE_FOUND),
            "",
            t(
                "trix.topic.status_restore_later_header", lang=lang,
                default=_DEFAULT_STATUS_RESTORE_LATER_HEADER,
            ),
            t("trix.topic.status_step_create", lang=lang, default=_DEFAULT_STATUS_STEP_CREATE),
            t("trix.topic.status_step_send", lang=lang, default=_DEFAULT_STATUS_STEP_SEND),
        ])
    return "\n".join(lines)


def session_not_found_message(raw_session_id: str, lang: Optional[str] = None) -> str:
    return t(
        "trix.topic.session_not_found", lang=lang, session_id=raw_session_id,
        default=_DEFAULT_SESSION_NOT_FOUND,
    )


def not_telegram_session_message(lang: Optional[str] = None) -> str:
    return t("trix.topic.not_telegram_session", lang=lang, default=_DEFAULT_NOT_TELEGRAM_SESSION)


def session_not_owned_message(lang: Optional[str] = None) -> str:
    return t("trix.topic.session_not_owned", lang=lang, default=_DEFAULT_SESSION_NOT_OWNED)


def session_already_linked_message(lang: Optional[str] = None) -> str:
    return t(
        "trix.topic.session_already_linked", lang=lang,
        default=_DEFAULT_SESSION_ALREADY_LINKED,
    )


def session_restored_message(
    title: str, last_assistant: Optional[str] = None, lang: Optional[str] = None,
) -> str:
    response = t(
        "trix.topic.session_restored", lang=lang, title=title,
        default=_DEFAULT_SESSION_RESTORED,
    )
    if last_assistant:
        response += t(
            "trix.topic.last_message_prefix", lang=lang, message=last_assistant,
            default=_DEFAULT_LAST_MESSAGE_PREFIX,
        )
    return response
