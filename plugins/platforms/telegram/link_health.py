"""Счётчик разрывов связи с Телеграмом.

**Зачем.** Повторы отправки и осушение пула соединений происходят молча:
адаптер честно переживает разрыв, а клиент видит только «ответ пришёл не
сразу» или «бот пропал и вернулся». Сказать ему о недоступности Телеграма
ЧЕРЕЗ Телеграм невозможно — но можно посчитать. Строчка «за сутки связь
рвалась 14 раз» отвечает на вопрос поддержки одним числом там, где иначе
пришлось бы читать журнал шлюза.

**Форма хранения.** Почасовые вёдра, по одному счётчику на вид события::

    {"version": 1,
     "buckets": {"2026-09-03T14": {"pool_timeout": 3, "connect_retry": 1}}}

Час — достаточная точность для вопроса «сколько раз за сутки» и делает
файл ограниченным по размеру: при недельном хранении это максимум 168
записей независимо от того, сколько было разрывов. Список отдельных
событий рос бы вместе со штормом — то есть ровно тогда, когда на диске и
так плохо.

**Почему пишем на каждое событие.** События здесь — это неудачи, и путь,
на котором они возникают, и без нас спит секунды между повторами; лишняя
запись мелкого JSON на его фоне не значит ничего. Отложенная запись
теряла бы как раз последние события перед падением процесса — то есть
самые интересные.

Ошибка записи не поднимается наружу: счётчик разрывов не имеет права
уронить отправку сообщения, ради устойчивости которой он и заведён.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_STATE_FILE = "telegram_link_health.json"
_VERSION = 1

# Сколько хранить. Неделя отвечает и на «за сутки», и на «это уже было на
# прошлой неделе или началось вчера» — второй вопрос поддержке нужен не
# реже первого.
RETENTION_DAYS = 7

# Виды событий. Держим список закрытым: свободная строка из места вызова
# рано или поздно расползётся в синонимы ("pool", "pool-timeout"), и
# сводка перестанет складываться.
KIND_POOL_TIMEOUT = "pool_timeout"        # пул соединений исчерпан, запрос не ушёл
KIND_CONNECT_TIMEOUT = "connect_timeout"  # соединение не установилось
KIND_SEND_RETRY = "send_retry"            # отправка не удалась, пробуем ещё раз
KIND_SEND_FAILED = "send_failed"          # повторы кончились, сообщение не ушло
KIND_INIT_RETRY = "init_retry"            # подключение шлюза к Телеграму сорвалось

KINDS = (
    KIND_POOL_TIMEOUT,
    KIND_CONNECT_TIMEOUT,
    KIND_SEND_RETRY,
    KIND_SEND_FAILED,
    KIND_INIT_RETRY,
)


def state_path(home: Optional[Path] = None) -> Path:
    """Где лежит файл счётчиков. Профиль-зависимо, как и всё состояние."""
    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    return Path(home) / "state" / _STATE_FILE


def _bucket_key(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def _load(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": _VERSION, "buckets": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("buckets"), dict):
        return {"version": _VERSION, "buckets": {}}
    return raw


def _prune(buckets: dict, now: datetime) -> dict:
    horizon = _bucket_key(now - timedelta(days=RETENTION_DAYS))
    # Ключи — лексикографически сортируемые метки времени UTC, поэтому
    # сравнение строк здесь и есть сравнение времени.
    return {k: v for k, v in buckets.items() if isinstance(k, str) and k >= horizon}


def record(
    kind: str,
    *,
    home: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Отметить разрыв. ``False`` — записать не вышло (это не ошибка вызова).

    Неизвестный вид не пишется вовсе: молча положить в файл опечатку
    хуже, чем потерять событие, — сводка после этого врёт, а не пустует.
    """
    if kind not in KINDS:
        logger.debug("telegram link health: unknown kind %r ignored", kind)
        return False
    now = now or datetime.now(timezone.utc)
    path = state_path(home)
    try:
        state = _load(path)
        buckets = _prune(state.get("buckets", {}), now)
        bucket = buckets.setdefault(_bucket_key(now), {})
        bucket[kind] = int(bucket.get(kind, 0)) + 1
        state["version"] = _VERSION
        state["buckets"] = buckets
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as exc:
        logger.debug(
            "telegram link health: не записалось в %s (%s: %s)",
            path, type(exc).__name__, exc,
        )
        return False


def summary(
    hours: int = 24,
    *,
    home: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Сводка за последние ``hours`` часов.

    Возвращает ``{"hours": …, "total": …, "by_kind": {вид: сколько}}``.
    Виды с нулём в ``by_kind`` не попадают: «за сутки ни одного разрыва»
    выражается пустым словарём и нулевым ``total``.
    """
    hours = max(1, int(hours))
    now = now or datetime.now(timezone.utc)
    floor = _bucket_key(now - timedelta(hours=hours - 1))
    by_kind: dict = {}
    for key, bucket in _load(state_path(home)).get("buckets", {}).items():
        if not isinstance(key, str) or key < floor or not isinstance(bucket, dict):
            continue
        for kind, count in bucket.items():
            if kind in KINDS and isinstance(count, int):
                by_kind[kind] = by_kind.get(kind, 0) + count
    return {
        "hours": hours,
        "total": sum(by_kind.values()),
        "by_kind": by_kind,
    }
