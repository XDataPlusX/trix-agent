"""Источник списка часовых поясов для мастера (спека 11).

Настоящие импорты, никаких заглушек: список поясов — это данные, и
единственное, что стоит проверять, — отношения внутри них. Снимков вида
«в списке ровно эти одиннадцать строк» здесь нет намеренно: состав
российских поясов меняется законом, а тест-снимок краснел бы на верной
правке.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

import pytest


def test_every_offered_zone_is_a_real_iana_zone():
    """Ни одно имя из списка не должно валить ZoneInfo у клиента."""
    from hermes_cli.setup_wizard.timezones import all_zone_names

    names = all_zone_names()
    assert names, "список поясов пуст"
    for name in names:
        ZoneInfo(name)


def test_russian_block_is_a_subset_of_the_full_list():
    """Российский блок — это ярлыки к тем же поясам, а не отдельная вселенная."""
    from hermes_cli.setup_wizard.timezones import all_zone_names, russian_zones

    full = set(all_zone_names())
    for zone in russian_zones():
        assert zone["name"] in full, zone["name"]


def test_russian_block_has_a_russian_label_for_every_entry():
    from hermes_cli.setup_wizard.timezones import russian_zones

    rows = russian_zones()
    assert rows, "российский блок пуст"
    for zone in rows:
        assert zone["label"].strip(), zone
        assert any("а" <= ch.lower() <= "я" for ch in zone["label"]), zone["label"]


def test_offset_is_computed_from_the_zone_not_written_by_hand():
    """Смещение обязано совпадать с тем, что скажет ZoneInfo.

    Инвариант, а не снимок: если страна переведёт регион, подпись поедет
    вместе с данными, а разошедшаяся подпись покраснеет. Момент передаётся
    явно — иначе тест зависел бы от того, не пришёлся ли перевод часов на
    промежуток между вызовом модуля и вычислением ожидаемого.
    """
    from hermes_cli.setup_wizard.timezones import all_zones

    at = datetime(2026, 1, 15, tzinfo=timezone.utc)
    for zone in all_zones(at=at):
        offset = ZoneInfo(zone["name"]).utcoffset(at)
        assert offset is not None, zone["name"]
        minutes = int(offset.total_seconds() // 60)
        sign = "+" if minutes >= 0 else "-"
        hours, mins = divmod(abs(minutes), 60)
        expected = f"UTC{sign}{hours}" + (f":{mins:02d}" if mins else "")
        assert zone["offset"] == expected, (zone["name"], zone["offset"], expected)


def test_full_list_offers_every_zone_python_knows():
    """Владелец: «нужны все пояса, вдруг купят непонятно откуда».

    Контракт: мастер не сужает выбор относительно того, что вообще умеет
    рантайм. Сравнение с available_timezones() — не снимок: обе стороны
    берутся из разных мест (наш модуль против стандартной библиотеки).
    """
    from hermes_cli.setup_wizard.timezones import all_zone_names

    assert set(all_zone_names()) == available_timezones()


def test_groups_cover_the_full_list_exactly_once():
    """Группировка — это раскладка того же множества, без потерь и дублей."""
    from hermes_cli.setup_wizard.timezones import all_zone_names, zone_groups

    seen = []
    for group in zone_groups():
        assert group["title"].strip(), group
        for zone in group["zones"]:
            seen.append(zone["name"])
    assert sorted(seen) == sorted(all_zone_names())
    assert len(seen) == len(set(seen)), "пояс попал в две группы"


def test_russia_is_the_first_group():
    from hermes_cli.setup_wizard.timezones import zone_groups

    assert zone_groups()[0]["title"] == "Россия"


@pytest.mark.parametrize("bad", ["", "   ", "Europe/Nowhere", "Москва", "../../etc/passwd", "UTC\x00"])
def test_is_valid_rejects_anything_that_is_not_a_zone(bad):
    from hermes_cli.setup_wizard.timezones import is_valid

    assert is_valid(bad) is False


def test_is_valid_accepts_a_zone_from_our_own_list():
    from hermes_cli.setup_wizard.timezones import all_zone_names, is_valid

    assert is_valid(sorted(all_zone_names())[0]) is True


def test_summer_and_winter_labels_differ_where_the_zone_actually_shifts():
    """Момент — не украшение: пояс с переводом часов обязан показать
    разное смещение зимой и летом, иначе `at` никуда не доходит."""
    from hermes_cli.setup_wizard.timezones import all_zones

    winter = {z["name"]: z["offset"] for z in all_zones(at=datetime(2026, 1, 15, tzinfo=timezone.utc))}
    summer = {z["name"]: z["offset"] for z in all_zones(at=datetime(2026, 7, 15, tzinfo=timezone.utc))}
    assert winter["Europe/Berlin"] != summer["Europe/Berlin"]
    assert winter["Europe/Moscow"] == summer["Europe/Moscow"]
