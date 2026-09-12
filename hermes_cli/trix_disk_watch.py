"""Почасовая проверка места на диске клиента и отправка в домашний чат.

Вынесено из ``gateway/run.py`` (27 тысяч строк, куда мы регулярно
подтягиваем апстрим — каждая наша строка внутри оплачивается конфликтом
при обновлении). В апстримном файле остаётся только вызов из
``_start_gateway_housekeeping``.

Своего расписания модуль не заводит: тик встроен в ту же почасовую
уборку, что чистит медиакэши (см. ``DISK_WATCH_EVERY`` в
``_start_gateway_housekeeping``), а не в отдельный таск планировщика.
Одной точкой соприкосновения с апстримом меньше, и, важнее, клиент не
может случайно снять эту проверку словами, попросив агента навести
порядок в расписании cron — задачи планировщика он видит и мог бы
удалить, а эта проверка ему не показана вовсе.

Две функции:

- :func:`disk_watch_tick` — раз в час решает, пора ли предупреждать о
  занятости раздела или пора слать месячную сводку, и вызывает вторую
  функцию для доставки.
- :func:`send_to_home_channel` — общий транспорт отправки: резолвит
  Telegram-адаптер (в том числе за реле) и шлёт текст в чат, заданный
  ``/sethome``.
"""

from __future__ import annotations

import logging

from agent.async_utils import safe_schedule_threadsafe
from gateway.delivery import resolve_delivery_transport

logger = logging.getLogger(__name__)

# ``gateway.config`` (Platform, load_gateway_config) and ``hermes_cli.trix_disk``
# / ``hermes_constants`` (get_hermes_home) are deliberately imported LOCALLY,
# inside each function below, rather than up here. Tests patch those origin
# modules directly (``monkeypatch.setattr(gwconfig, "load_gateway_config",
# ...)``, ``monkeypatch.setattr(td, "partition_used_percent", ...)``) and
# expect the patched attribute to be picked up on the next call — that only
# works if the import re-runs (and re-resolves the current attribute) on
# every call, exactly as it did in the original ``gateway/run.py`` functions
# this module was extracted from. A top-level import here would freeze a
# stale reference at module-load time and silently ignore those patches.

# Сколько ждать подтверждения отправки предупреждения о месте. Тот же
# порядок, что у обновления справочника каналов ниже (fut.result(timeout=30))
# — блокируется поток уборки, а не цикл событий. Ждём намеренно: без ответа
# нельзя отличить доставленное предупреждение от потерянного, а отметка
# «уже сказали» ставится только за доставленное.
_TRIX_DISK_SEND_TIMEOUT = 30.0


def send_to_home_channel(adapters, loop, text: str) -> bool:
    """Отправить текст в домашний чат Telegram. ``False`` — если не ушло.

    Транспорт ищется общим резолвером ``resolve_delivery_transport``, а не
    ``adapters.get(Platform.TELEGRAM)``: шлюз за реле держит ОДИН адаптер
    под ``Platform.RELAY``, который фронтит несколько логических платформ,
    и прямой поиск по ключу его не нашёл бы — предупреждение о
    кончающемся диске просто не дошло бы до клиента.

    Каждый отказ пишется в журнал предупреждением. Молча терять нельзя:
    иначе никто никогда не узнает, что механизм не работал.
    """
    from gateway.config import Platform, load_gateway_config

    if loop is None:
        logger.warning(
            "Проверка диска: цикл событий шлюза недоступен — "
            "сообщение клиенту не ушло"
        )
        return False
    try:
        config = load_gateway_config()
        home = config.get_home_channel(Platform.TELEGRAM)
    except Exception as e:
        logger.warning(
            "Проверка диска: конфиг шлюза не прочитан (%s) — "
            "сообщение клиенту не ушло", e,
        )
        return False
    if not home or not home.chat_id:
        logger.warning(
            "Проверка диска: домашний чат Telegram не задан (/sethome) — "
            "сообщение клиенту не ушло"
        )
        return False

    transport = resolve_delivery_transport(Platform.TELEGRAM, config, adapters)
    if transport is None:
        logger.warning(
            "Проверка диска: Telegram не подключён — сообщение клиенту не ушло"
        )
        return False

    # Домашний чат может быть темой форума: без thread_id сообщение уедет
    # в общую ленту, где клиент его не ждёт.
    metadata = {"thread_id": home.thread_id} if home.thread_id else None
    if transport.is_relay:
        metadata = dict(metadata or {})
        if home.user_id:
            metadata["user_id"] = home.user_id
        if home.scope_id:
            metadata["scope_id"] = home.scope_id

    fut = safe_schedule_threadsafe(
        transport.send(Platform.TELEGRAM, str(home.chat_id), text, metadata=metadata),
        loop,
        logger=logger,
        log_message="Проверка диска: сообщение не удалось поставить в цикл событий",
    )
    if fut is None:
        return False
    try:
        result = fut.result(timeout=_TRIX_DISK_SEND_TIMEOUT)
    except Exception as e:
        logger.warning(
            "Проверка диска: отправка в домашний чат не удалась (%s)", e
        )
        return False
    if result is not None and getattr(result, "success", True) is False:
        logger.warning(
            "Проверка диска: отправка в домашний чат не удалась (%s)",
            getattr(result, "error", "send returned success=False"),
        )
        return False
    return True


def disk_watch_tick(adapters, loop) -> None:
    """Предупреждение о месте и месячная сводка в домашний чат.

    Своего расписания не заводим: та же почасовая уборка, что чистит
    медиакэши, сверяется с отметками в ``trix_disk_state.json``. Одной
    точкой соприкосновения с апстримом меньше — и, важнее, клиент не может
    случайно снять эту проверку словами, как снял бы задачу планировщика,
    попросив агента навести порядок в расписании.

    Замер идёт синхронно и это намеренно: уборка живёт в отдельном потоке
    (``threading.Thread(target=_start_gateway_housekeeping)``), а не в
    цикле событий. Обход дерева на десятки секунд внутри цикла — молчание
    не одной команды, а всего бота: цикл один на все разговоры и все
    платформы. В цикл уходит только сама отправка.
    """
    from hermes_cli.trix_disk import (
        MONTHLY_HEAD,
        cached_report,
        disk_thresholds,
        format_report,
        format_warning,
        load_state,
        mark_warned,
        partition_free_bytes,
        partition_used_percent,
        refresh_warn_arming,
        save_state,
        should_report_monthly,
        stamp_monthly,
        stamp_monthly_attempt,
        start_monthly_countdown,
        warn_level,
    )
    from hermes_constants import get_hermes_home

    thresholds = disk_thresholds()
    home = get_hermes_home()
    state = load_state(home)
    start_monthly_countdown(state)

    # Дешёвые ворота. Занятость раздела — один системный вызов за
    # микросекунды; обход всех данных агента — секунды, а на клиентской
    # машине с раздутыми зависимостями десятки секунд. Платить за него
    # каждый час впустую нельзя: это машина, где и так мало места и мало
    # процессора.
    #
    # Ворота не могут съесть предупреждение: порог считается ровно по
    # проценту занятости, а он и есть дешёвое число. Обход нужен, только
    # чтобы ПОКАЗАТЬ разбивку, а не чтобы понять, что места мало.
    used_percent = partition_used_percent(home)
    # Тем же дешёвым системным вызовом. Процент сам по себе о запасе не
    # говорит: 80 % на диске в сто гигабайт — двадцать свободных.
    free_bytes = partition_free_bytes(home)
    # Взведение — дешёвая бухгалтерия «диск разгрузили заметно», она идёт
    # каждый час, в том числе в тот, когда обхода не будет.
    refresh_warn_arming(
        state,
        used_percent,
        warn_percent=thresholds.warn_percent,
        urgent_percent=thresholds.urgent_percent,
        rearm_percent=thresholds.rearm_percent,
    )
    level = warn_level(
        used_percent,
        state,
        warn_percent=thresholds.warn_percent,
        urgent_percent=thresholds.urgent_percent,
        repeat_after_hours=thresholds.repeat_after_hours,
        free_bytes=free_bytes,
        min_free_bytes=thresholds.min_free_bytes,
    )
    monthly_due = should_report_monthly(
        state, retry_after_hours=thresholds.repeat_after_hours
    )
    if level is None and not monthly_due:
        # Спокойный час: показывать нечего — обхода не будет.
        save_state(home, state)
        return

    report = cached_report(home)
    if level is not None:
        message = format_warning(
            level,
            report,
            warn_percent=thresholds.warn_percent,
            min_cleanup_bytes=thresholds.min_cleanup_bytes,
            min_free_bytes=thresholds.min_free_bytes,
        )
    else:
        message = MONTHLY_HEAD + "\n\n" + format_report(
            report,
            warn_percent=thresholds.warn_percent,
            min_cleanup_bytes=thresholds.min_cleanup_bytes,
            min_free_bytes=thresholds.min_free_bytes,
        )

    # Отметки ставятся ТОЛЬКО за доставленное. Иначе единственное
    # предупреждение о кончающемся диске потеряно навсегда: следующий час
    # его уже не повторит.
    if send_to_home_channel(adapters, loop, message):
        if level is not None:
            mark_warned(state, level)
        # Предупреждение несёт тот же отчёт, что и сводка. Отмечаем и
        # сводку тоже: два подряд сообщения об одном и том же клиент
        # прочтёт как поломку.
        if monthly_due:
            stamp_monthly(state)
    elif monthly_due and level is None:
        # Сводка не срочная, а её текст стоит полного обхода дерева. Пока
        # она не доставлена, «пора» остаётся истинным — без этой отметки
        # шлюз платил бы за обход каждый час. Предупреждение, в отличие от
        # неё, повторяется на следующем же часу.
        stamp_monthly_attempt(state)

    save_state(home, state)
