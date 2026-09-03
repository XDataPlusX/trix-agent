"""Что клиент читает, когда разговор начинается заново или бот вернулся.

Два семейства текстов, у которых общий адресат и общая беда в апстриме:

* **Новый разговор** — сессия в теме истекла (молчание / рубеж суток),
  была остановлена ``/stop`` или не пережила перезапуск. Апстримная
  строка (``gateway/run.py``) не только английская: она говорит
  «Conversation history cleared» о разговоре, который на самом деле
  сохранён (старая строка в ``state.db`` закрывается причиной
  ``session_reset``, а не удаляется), и отправляет клиента править
  ``config.yaml`` — файл, до которого у него нет доступа: он общается с
  агентом только через Telegram.
* **Перезапуск шлюза** — три строки того же механизма
  (``_send_home_channel_startup_notifications``,
  ``_send_restart_notification``, ``RECOVERED_MARKER``). Их не выключают,
  а переводят: клиенту полезно знать, что бот вернулся, — он мог писать
  в тот момент, когда агент лежал. Слово «Gateway» ему ничего не
  говорит, а имени апстримного продукта в текстах клиенту быть не
  должно.

**Почему отдельный файл, а не ``trix_provider_errors.py``.** У соседа
другой предмет (отказ провайдера внутри хода), другая точка вызова
(``agent/conversation_loop.py``) и он уже 362 строки. Общего здесь только
приём — ``t()`` с обязательным ``default=``. Складывать в один файл две
несвязанные группы текстов ради этого приёма значит склеить их будущие
правки; сам приём переносится импортом.

**Почему не в ``gateway/run.py``.** Там 27 тысяч строк, которые мы
регулярно подтягиваем сверху, и каждая наша строка внутри оплачивается
конфликтом при обновлении. В апстримном файле остаётся только вызов.

**Почему у каждого ``t()`` есть ``default=``.** ``agent.i18n._load_catalog``
глотает любую ошибку чтения/разбора каталога и кеширует для этого
процесса ПУСТОЙ словарь. Без ``default=`` клиент на машине со сломанным
каталогом получил бы в чат путь ключа (``trix.session_reset.idle``).
Это не гипотеза — воспроизведено исполнением в первой задаче ветки.
"""

from __future__ import annotations

from typing import Optional

from agent.i18n import DEFAULT_LANGUAGE, resolve_language, t

# Апстримная нормализация: ``gateway/config.py::_validate_gateway_config``
# заменяет None и неположительное значение ровно на это число, поэтому оно
# же — единственно честный запасной вариант здесь: назвать клиенту другое
# число значило бы назвать срок, которым продукт не пользуется.
#
# Запасной вариант тут не декоративный. Валидатор правит ТОЛЬКО
# ``config.default_reset_policy``, а до нас доезжает результат
# ``GatewayConfig.get_reset_policy(platform, session_type)``, который при
# наличии переопределения отдаёт объект из ``reset_by_platform`` /
# ``reset_by_type`` — эти через валидатор не проходят вовсе. То есть
# неположительный ``idle_minutes`` сюда реально дойти может.
_FALLBACK_IDLE_MINUTES = 1440

_MINUTES_PER_HOUR = 60
_MINUTES_PER_DAY = 24 * _MINUTES_PER_HOUR

# Собирательные числительные для 2-4 суток. «Сутки» — pluralia tantum:
# «два сутки» не говорят вовсе, нужна форма «двое суток». Дальше пятого
# собирательная форма («пятеро суток») звучит архаично, поэтому с пяти
# переходим на цифры.
_COLLECTIVE_DAYS = {2: "двое суток", 3: "трое суток", 4: "четверо суток"}

# ``{duration}`` здесь безопасен ровно потому, что срок собирается на
# языке, на котором РЕАЛЬНО отрендерилась фраза (``_rendered_language``
# ниже), а не на языке, который резолвится вообще. Иначе получалась бы
# смесь «We haven't talked here for трое суток» — та же, от которой
# ``trix_status.build_heartbeat_text`` защищается тем же приёмом.
_DEFAULT_IDLE = (
    "🕓 We haven't talked here for {duration} — starting a new conversation "
    "so it doesn't drag along what is no longer relevant.\n"
    "The previous one is saved: /resume lists recent conversations and brings "
    "back the one you need. If it isn't there, just ask me — I'll search the "
    "earlier ones."
)
# Вторая дорога («попросите — поищу») есть в КАЖДОМ тексте, где назван
# ``/resume``, и это не украшение. ``_handle_resume_command``
# (``gateway/slash_commands.py``) отсеивает записи без заголовка
# (``if s.get("title")``), поэтому разговор без заголовка не появится в
# списке НИКОГДА — ни первым, ни десятым. Обещание «вернуть его: /resume»
# в одиночку необеспечено ровно того же класса, что и апстримное
# «Adjust reset timing in config.yaml», ради которого всё это правилось.
_DEFAULT_SUSPENDED = (
    "⏹️ The previous conversation was stopped — continuing with a clean "
    "slate.\n"
    "To bring the stopped one back: /resume. If it isn't there, just ask me "
    "— I'll search the earlier ones."
)
_DEFAULT_RESTART = (
    "🔌 I was restarting and didn't manage to restore the previous "
    "conversation — starting a new one.\n"
    "The previous one is saved: /resume brings it back. If it isn't there, "
    "just ask me — I'll search the earlier ones."
)
_DEFAULT_DAILY = (
    "🕓 A new day has begun — starting a new conversation.\n"
    "The previous one is saved: /resume. If it isn't there, just ask me — "
    "I'll search the earlier ones."
)

_DEFAULT_BACK_ONLINE = "♻️ I'm back online."
_DEFAULT_SESSION_CONTINUES = (
    "♻️ I restarted — the conversation continues from where it left off."
)
# Приставка к ПОВТОРНО отправленному ответу, а не самостоятельное
# сообщение: обязана заканчиваться двоеточием и пустой строкой, потому что
# сразу за ней склеивается сам ответ (``gateway/run.py`` —
# ``content = RECOVERED_MARKER + content``).
_DEFAULT_RECOVERED_PREFIX = (
    "♻️ It looks like I already sent this, but I was restarting while it "
    "went out. Sending it again so it doesn't get lost:\n\n"
)


def _russian_days(days: int) -> str:
    """«сутки» / «трое суток» / «21 сутки» / «22 суток»."""
    if days == 1:
        return "сутки"
    collective = _COLLECTIVE_DAYS.get(days)
    if collective:
        return collective
    # Дальше пятого — цифрами. У pluralia tantum «сутки» после цифры
    # выбор всего из двух форм: именительная «сутки» идёт после
    # числительных, кончающихся на 1 (кроме 11 — «11 суток»), во всех
    # остальных случаях — родительная «суток». Форма «22 сутки»
    # неграмотна: по-русски это «двадцать двое суток», а в цифровой
    # записи пишут «22 суток».
    if days % 10 == 1 and days % 100 != 11:
        return f"{days} сутки"
    return f"{days} суток"


def _russian_plural(count: int, one: str, few: str, many: str) -> str:
    """Обычное русское согласование числительного с существительным."""
    if count % 100 in (11, 12, 13, 14):
        return many
    tail = count % 10
    if tail == 1:
        return one
    if tail in (2, 3, 4):
        return few
    return many


def _rendered_language(key: str, lang: Optional[str] = None) -> str:
    """Язык, на котором ``t()`` РЕАЛЬНО отдаст этот ключ.

    Не то же самое, что ``resolve_language``, и разница — это найденный
    ревью дефект. ``t()`` идёт по трём ступеням: каталог целевого языка →
    английский каталог → ``default=``. Достаточно, чтобы ОДИН ключ
    отсутствовал в ``ru.yaml`` при целом ``en.yaml``, и клиент с русским
    языком получает английское предложение — а срок, собранный по
    ``resolve_language``, приезжает в него по-русски: «We haven't talked
    here for трое суток». Прежняя починка (убрать плейсхолдер из
    английского литерала) закрывала только третью ступень; дефект был на
    второй.

    Функция повторяет порядок ``t()`` ступень в ступень, поэтому язык
    вставки всегда совпадает с языком фразы. Обе оставшиеся ступени
    (английский каталог и ``default=``) английские, так что им отвечает
    один и тот же ``DEFAULT_LANGUAGE``.

    Приватный ``_load_catalog`` здесь взят сознательно: это тот же шов, на
    котором держится проверка «сломанный каталог» во всех тестах ветки, и
    публичного способа спросить «есть ли ключ в этом каталоге» у модуля
    нет — ``t()`` сам молча проваливается на следующую ступень.
    """
    from agent.i18n import _load_catalog

    target = resolve_language(lang)
    if target != DEFAULT_LANGUAGE:
        try:
            if key in _load_catalog(target):
                return target
        except Exception:
            pass
    return DEFAULT_LANGUAGE


def _russian_span(total: int) -> str:
    """Сутки, часы и минуты вместе; нулевые части опускаются.

    Прежнее правило («кратно суткам → сутками, ИНАЧЕ часами, ИНАЧЕ
    минутами») на любом сроке, не кратном суткам, теряло сутки целиком:
    4380 минут читались как «73 часа», а 4321 — как «4321 минуту». На
    нашем значении это не встречается, но одна правка конфига — и клиент
    получает бессмыслицу.
    """
    days, rest = divmod(total, _MINUTES_PER_DAY)
    hours, mins = divmod(rest, _MINUTES_PER_HOUR)
    parts = []
    if days:
        parts.append(_russian_days(days))
    if hours:
        parts.append(f"{hours} {_russian_plural(hours, 'час', 'часа', 'часов')}")
    if mins:
        # Винительный падеж: «мы не общались 1 минуту / 2 минуты / 5 минут».
        parts.append(f"{mins} {_russian_plural(mins, 'минуту', 'минуты', 'минут')}")
    return " ".join(parts) if parts else "0 минут"


def duration_phrase(minutes: Optional[int], lang: Optional[str] = None) -> str:
    """Срок молчания словами.

    Апстрим печатает ``72h``. По-русски это «трое суток». Сутки, часы и
    минуты называются вместе, нулевые части опускаются («трое суток
    1 час»), а формы существительного согласуются с числом («21 сутки»,
    но «22 суток»; «21 час», но «22 часа»; «14 часов», а не «14 часа»).

    Вне русского языка отдаём апстримный формат (``72h``, ``1h 30m``,
    ``90m``): русские слова, вставленные в английскую фразу, дают смесь
    вроде «We haven't talked for трое суток».

    ``lang`` сюда обязан приходить из :func:`_rendered_language`, а не из
    ``resolve_language``: язык вставки должен совпадать с языком фразы, а
    не с языком, который резолвится вообще (см. докстроку той функции).
    """
    total = int(minutes) if minutes else 0
    if total <= 0:
        total = _FALLBACK_IDLE_MINUTES

    if resolve_language(lang) != "ru":
        hours, mins = divmod(total, _MINUTES_PER_HOUR)
        if not mins:
            return f"{hours}h"
        return f"{hours}h {mins}m" if hours else f"{mins}m"

    return _russian_span(total)


def session_reset_notice(
    reason: Optional[str],
    idle_minutes: Optional[int] = None,
    lang: Optional[str] = None,
) -> str:
    """Сообщение клиенту о том, что разговор начинается заново.

    ``reason`` — апстримный ``session_entry.auto_reset_reason``. Ветки
    ровно те же, что в ``gateway/run.py``: ``suspended`` (после
    ``/stop``), ``resume_pending_expired`` (перезапуск не успел
    восстановить ход), ``daily`` (суточный рубеж) и всё остальное —
    молчание. Неизвестная причина попадает в ветку молчания так же, как
    в апстриме: это самый частый и самый безобидный из вариантов.

    Про «Прежний сохранён»: строка старой сессии в ``state.db``
    закрывается причиной ``session_reset``
    (``promote_to_session_reset``), а не удаляется — разговор остаётся
    и находится поиском.

    Про ДВЕ дороги во второй фразе про молчание: ``/resume`` в шлюзе
    показывает не более десяти ОЗАГЛАВЛЕННЫХ разговоров
    (``_handle_resume_command``: ``limit=10`` плюс отсев записей без
    заголовка), поэтому обещать одну только эту команду было бы
    обещанием, которого продукт не всегда выполняет. Вторая дорога —
    поиск по прошлым разговорам — у клиента включена (тулсет
    ``session_search`` в ``assets/config/trix-config.yaml``).
    """
    name = (reason or "").strip()
    if name == "suspended":
        return t("trix.session_reset.suspended", lang=lang, default=_DEFAULT_SUSPENDED)
    if name == "resume_pending_expired":
        return t("trix.session_reset.restart", lang=lang, default=_DEFAULT_RESTART)
    if name == "daily":
        return t("trix.session_reset.daily", lang=lang, default=_DEFAULT_DAILY)
    # Язык вставки берём у ФРАЗЫ, а не у резолвера: если ru.yaml потерял
    # этот ключ, t() отдаст английский каталог, и русский срок в нём был бы
    # смесью двух языков (см. _rendered_language).
    return t(
        "trix.session_reset.idle",
        lang=lang,
        duration=duration_phrase(
            idle_minutes, lang=_rendered_language("trix.session_reset.idle", lang)
        ),
        default=_DEFAULT_IDLE,
    )


def gateway_back_online_message(lang: Optional[str] = None) -> str:
    """Приветствие в домашний чат после планового перезапуска шлюза."""
    return t("trix.gateway_restart.back_online", lang=lang, default=_DEFAULT_BACK_ONLINE)


def gateway_restarted_message(lang: Optional[str] = None) -> str:
    """Ответ в тот чат, из которого перезапуск был заказан."""
    return t(
        "trix.gateway_restart.session_continues",
        lang=lang,
        default=_DEFAULT_SESSION_CONTINUES,
    )


def recovered_reply_prefix(lang: Optional[str] = None) -> str:
    """Приставка к ответу, отправленному повторно после перезапуска.

    Честное «доставим хотя бы раз»: ответ уже был сформирован, а
    отправка оборвалась, поэтому дубль возможен и о нём говорят вслух.
    Заканчивается двоеточием и пустой строкой — дальше приклеивается сам
    ответ.
    """
    return t(
        "trix.gateway_restart.recovered_prefix",
        lang=lang,
        default=_DEFAULT_RECOVERED_PREFIX,
    )
