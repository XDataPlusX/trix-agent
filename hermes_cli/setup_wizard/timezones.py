"""Список часовых поясов для мастера настройки (спека 11).

Один источник для трёх потребителей: формы (`page.py` рисует `<select>`),
ответа `/api/form` (`app.py` отдаёт список браузеру) и серверной проверки
присланного значения (`validate.py`). Список из браузера не является
доказательством: то, что клиент прислал, всё равно проверяется здесь.

**Решения владельца 2026-08-31.** Предлагаются ВСЕ пояса, какие знает
рантайм, а не только российские: машину может купить клиент откуда угодно.
Российские вынесены отдельной первой группой с русскими названиями городов
— это большинство случаев, и их приятно видеть первыми. Остальной мир
сгруппирован по областям IANA с русскими заголовками, но сами города
остаются в латинском написании стандарта: придумывать русские написания
для шестисот городов означало бы выдумывать данные.

Ничего не преселектится. Пустое значение — не выбор, а незаполненное поле
(см. `hermes_time._resolve_timezone_name`: пусто означает системное время
машины, а оно на сервере хостера почти наверняка не совпадает с временем
клиента — ровно та беда, ради которой спека и появилась).

Смещение (`UTC+3`) НЕ записано в подписи руками: оно считается из самой
зоны на переданный момент. Иначе очередной перевод региона разошёлся бы с
текстом молча, а именно так и выглядит настройка, которой нельзя верить.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, available_timezones

# Одиннадцать российских поясов: имя зоны -> города, по которым клиент
# себя узнает. Города перечислены только те, что физически лежат в этой
# зоне — подпись, обещающая лишний город, хуже отсутствующей.
_RUSSIAN: tuple[tuple[str, str], ...] = (
    ("Europe/Kaliningrad", "Калининград"),
    ("Europe/Moscow", "Москва, Санкт-Петербург"),
    ("Europe/Samara", "Самара, Ижевск"),
    ("Asia/Yekaterinburg", "Екатеринбург, Пермь, Уфа"),
    ("Asia/Omsk", "Омск"),
    ("Asia/Krasnoyarsk", "Красноярск"),
    ("Asia/Irkutsk", "Иркутск, Улан-Удэ"),
    ("Asia/Yakutsk", "Якутск, Чита"),
    ("Asia/Vladivostok", "Владивосток, Хабаровск"),
    ("Asia/Magadan", "Магадан"),
    ("Asia/Kamchatka", "Петропавловск-Камчатский"),
)

_RUSSIA_TITLE = "Россия"

# Русские заголовки групп по областям IANA. Область, которой здесь нет,
# уезжает в "Прочие" — список областей задаёт база данных, а не мы, и
# новая область не должна ронять форму.
_AREA_TITLES: dict[str, str] = {
    "Europe": "Европа",
    "Asia": "Азия",
    "America": "Америка",
    "Africa": "Африка",
    "Australia": "Австралия",
    "Pacific": "Тихий океан",
    "Atlantic": "Атлантика",
    "Indian": "Индийский океан",
    "Antarctica": "Антарктида",
    "Arctic": "Арктика",
    "Brazil": "Бразилия",
    "Canada": "Канада",
    "Chile": "Чили",
    "Mexico": "Мексика",
    "US": "США",
    "Etc": "Смещения от UTC",
}

_OTHER_TITLE = "Прочие"

# Порядок групп после России. Явный, а не алфавитный: клиент, не нашедший
# себя в первой группе, вероятнее всего рядом — в Европе или Азии.
_AREA_ORDER: tuple[str, ...] = (
    "Europe",
    "Asia",
    "America",
    "Africa",
    "Australia",
    "Pacific",
    "Atlantic",
    "Indian",
    "Antarctica",
    "Arctic",
    "Brazil",
    "Canada",
    "Chile",
    "Mexico",
    "US",
    "Etc",
)


def _at(at: Optional[datetime]) -> datetime:
    return at if at is not None else datetime.now(timezone.utc)


def _offset_label(name: str, at: datetime) -> str:
    """`UTC+3` / `UTC-3:30` — из самой зоны, на переданный момент."""
    offset = ZoneInfo(name).utcoffset(at)
    if offset is None:
        return "UTC"
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{mins:02d}" if mins else "")


def all_zone_names() -> list[str]:
    """Все пояса, какие знает рантайм. Отсортированы — порядок стабилен."""
    return sorted(available_timezones())


def _row(name: str, label: str, at: datetime) -> dict[str, Any]:
    return {"name": name, "label": label, "offset": _offset_label(name, at)}


def russian_zones(at: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Российские пояса с русскими названиями городов, с запада на восток.

    Пояс, которого нет в базе рантайма, молча пропускается: список городов
    — наши данные, база зон — чужие, и расхождение не должно ронять форму.
    """
    moment = _at(at)
    known = available_timezones()
    return [_row(name, label, moment) for name, label in _RUSSIAN if name in known]


def all_zones(at: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Каждый пояс рантайма одной плоской строкой.

    Подпись российского пояса — русские города; для остальных подписью
    служит собственное имя зоны в написании стандарта.
    """
    moment = _at(at)
    russian = {name: label for name, label in _RUSSIAN}
    return [_row(name, russian.get(name, name), moment) for name in all_zone_names()]


def zone_groups(at: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Тот же список, разложенный по группам. Россия — первая.

    Раскладка полная и без дублей: каждый пояс попадает ровно в одну
    группу. Российский пояс не повторяется ниже в «Европе»/«Азии» — иначе
    клиент выбирал бы одно и то же в двух местах и не понимал, есть ли
    разница.
    """
    moment = _at(at)
    rows = russian_zones(moment)
    groups: list[dict[str, Any]] = [{"title": _RUSSIA_TITLE, "zones": rows}]

    claimed = {zone["name"] for zone in rows}
    by_area: dict[str, list[dict[str, Any]]] = {}
    for name in all_zone_names():
        if name in claimed:
            continue
        area = name.split("/")[0] if "/" in name else ""
        title = _AREA_TITLES.get(area, _OTHER_TITLE)
        by_area.setdefault(title, []).append(_row(name, name, moment))

    ordered_titles = [_AREA_TITLES[a] for a in _AREA_ORDER if _AREA_TITLES[a] in by_area]
    ordered_titles += [t for t in by_area if t not in ordered_titles]
    for title in ordered_titles:
        groups.append({"title": title, "zones": by_area[title]})
    return groups


def is_valid(name: Any) -> bool:
    """Настоящий ли это пояс. Единственные ворота для присланного значения.

    Проверяется принадлежность нашему списку, а не просто способность
    `ZoneInfo` его разобрать: `ZoneInfo` читает файлы по относительному
    пути внутри базы зон, и доверять произвольной строке из сети не стоит.
    """
    if not isinstance(name, str):
        return False
    stripped = name.strip()
    if not stripped:
        return False
    return stripped in available_timezones()
