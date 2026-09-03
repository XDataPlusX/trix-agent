"""Порог предупреждает один раз на пересечение, сводка — раз в месяц.

Клиент узнаёт о состоянии своей машины только от бота: ни консоли, ни
файлового менеджера у него нет. Поэтому проверяется не «сообщение
отправлено», а два отношения, от которых зависит польза механизма:

* предупреждение уходит РОВНО на пересечении порога вверх — сообщение раз
  в час превращается в шум, который перестают читать, и механизм окажется
  бесполезен ровно тогда, когда нужен;
* когда доставить некуда, это видно в журнале, а не проглочено молча.

Снимков текста здесь нет: проверяются отношения («в предупреждении есть
разбивка отчёта», «второй вызов ничего не вернул»).
"""

import asyncio
import json
import logging
import threading
from pathlib import Path

import pytest

from hermes_cli.trix_disk import (
    WARN_SOFT,
    WARN_URGENT,
    DiskReport,
    disk_thresholds,
    format_report,
    format_warning,
    load_state,
    partition_used_percent,
    save_state,
    should_report_monthly,
    should_warn,
    start_monthly_countdown,
    warn_level,
)

GB = 1024 ** 3
MONTH = 30 * 24 * 3600


def _report(pct: float, *, removable=None) -> DiskReport:
    """Отчёт с заданной занятостью раздела. Числа сходятся между собой."""
    total = 100 * GB
    used = int(total * pct / 100)
    return DiskReport(
        total=total,
        used=used,
        free=total - used,
        used_percent=pct,
        documents_bytes=0,
        workspace_bytes=0,
        sessions_bytes=0,
        service_bytes=0,
        other_bytes=used,
        removable=list(removable or []),
    )


# ---------------------------------------------------------------------------
# Порог
# ---------------------------------------------------------------------------

DAY = 24 * 3600


class TestThreshold:
    def test_crossing_the_threshold_warns(self):
        assert should_warn(_report(85.0), {}, warn_percent=80.0) is not None

    def test_the_warning_carries_the_full_report(self):
        """Предупреждение обязано нести разбивку, а не одну строку тревоги:
        иначе клиенту нечего решать — он не видит, чем занято место."""
        text = should_warn(_report(85.0), {}, warn_percent=80.0)
        # Тело берётся с уровнем заголовка: одно сообщение — один уровень
        # тревоги, поэтому под мягким заголовком в теле стоит мягкое
        # последствие, а не второй, срочный диагноз.
        assert format_report(_report(85.0), headline_level=WARN_SOFT) in text

    def test_staying_above_the_threshold_does_not_warn_again(self):
        """Сообщение раз в час — шум, который перестают читать."""
        state = {}
        assert should_warn(_report(85.0), state, warn_percent=80.0, now_ts=0)
        assert should_warn(_report(86.0), state, warn_percent=80.0, now_ts=3600) is None
        assert should_warn(_report(88.0), state, warn_percent=80.0, now_ts=7200) is None

    def test_below_the_threshold_never_warns(self):
        assert should_warn(_report(10.0), {}, warn_percent=80.0) is None

    def test_urgent_warns_even_after_a_soft_warning(self):
        """Мягкое предупреждение не должно съедать срочное: между 82 % и
        93 % клиенту надо сказать ещё раз, иначе он узнает о конце места
        от замолчавшего бота."""
        state = {}
        assert should_warn(
            _report(82.0), state, warn_percent=80.0, urgent_percent=90.0, now_ts=0
        )
        assert should_warn(
            _report(93.0), state, warn_percent=80.0, urgent_percent=90.0, now_ts=3600
        )

    def test_urgent_does_not_repeat_either(self):
        state = {}
        should_warn(
            _report(93.0), state, warn_percent=80.0, urgent_percent=90.0, now_ts=0
        )
        assert should_warn(
            _report(95.0), state, warn_percent=80.0, urgent_percent=90.0, now_ts=3600
        ) is None

    def test_a_soft_warning_never_follows_an_urgent_one(self):
        """Жёлтое «остаётся немного» сразу после красного «почти
        кончилось» клиент прочтёт как поломку."""
        state = {}
        first = should_warn(
            _report(95.0), state, warn_percent=80.0, urgent_percent=90.0, now_ts=0
        )
        assert first.startswith("🔴")
        # Занятость упала ниже срочного, но осталась выше мягкого.
        assert should_warn(
            _report(85.0), state, warn_percent=80.0, urgent_percent=90.0, now_ts=3600
        ) is None

    def test_urgent_and_soft_read_differently(self):
        """Клиент обязан отличить «остаётся немного» от «почти кончилось»:
        одинаковый текст на 82 % и на 95 % обесценивает оба."""
        soft = should_warn(_report(82.0), {}, warn_percent=80.0, urgent_percent=90.0)
        urgent = should_warn(_report(95.0), {}, warn_percent=80.0, urgent_percent=90.0)
        assert soft.splitlines()[0] != urgent.splitlines()[0]

    def test_first_crossing_straight_into_urgent_still_warns(self):
        """Диск может перевалить оба порога за один час между проверками."""
        assert should_warn(_report(95.0), {}, warn_percent=80.0, urgent_percent=90.0)

    def test_the_threshold_that_decided_is_the_one_the_report_uses(self):
        """Порог в предупреждении и порог в тексте отчёта — один и тот же:
        иначе клиент прочтёт тревогу без объяснения, чем она вызвана."""
        text = should_warn(_report(65.0), {}, warn_percent=60.0, urgent_percent=90.0)
        assert text is not None
        assert (
            format_report(_report(65.0), warn_percent=60.0, headline_level=WARN_SOFT)
            in text
        )

    def test_a_configured_threshold_actually_moves_the_line(self):
        assert should_warn(_report(65.0), {}, warn_percent=90.0) is None
        assert should_warn(_report(65.0), {}, warn_percent=60.0) is not None


class TestTheBoundary:
    """«Занято ровно в порог» — одно сравнение на две стороны.

    Иначе клиент прочтёт в ``/disk`` «места почти нет» в тот самый час,
    когда почасовая проверка промолчала.
    """

    def test_exactly_at_the_threshold_warns(self):
        assert should_warn(_report(80.0), {}, warn_percent=80.0) is not None

    def test_exactly_at_the_threshold_prints_the_alarm(self):
        assert "почти нет" in format_report(_report(80.0), warn_percent=80.0)

    def test_a_hair_below_the_threshold_does_neither(self):
        assert should_warn(_report(79.9), {}, warn_percent=80.0) is None
        assert "почти нет" not in format_report(_report(79.9), warn_percent=80.0)

    def test_the_two_sides_agree_all_around_the_threshold(self):
        """Инвариант, а не три точки: решение и печать обязаны совпадать
        на каждом шаге вокруг порога."""
        for tenths in range(795, 806):
            pct = tenths / 10
            decided = should_warn(_report(pct), {}, warn_percent=80.0) is not None
            printed = "почти нет" in format_report(_report(pct), warn_percent=80.0)
            assert decided == printed, f"на {pct} % решение и печать разошлись"

    def test_exactly_at_the_urgent_threshold_reads_as_urgent(self):
        text = should_warn(_report(90.0), {}, warn_percent=80.0, urgent_percent=90.0)
        assert text.startswith("🔴")


class TestHysteresis:
    """Одна десятая процентного пункта — двадцать мегабайт на
    стогигабайтном диске, то есть один присланный документ. Обычная работа
    агента качает занятость через порог весь день."""

    def test_the_reviewers_flapping_run_speaks_once(self):
        """79.9 → 80.0 → 79.9 → 80.1 → 79.8 → 80.0 → 79.9 → 80.2 за восемь
        часов. Четыре одинаковых жёлтых сообщения — и клиент перестанет их
        читать раньше, чем придёт красное."""
        state = {}
        spoken = [
            should_warn(
                _report(pct), state, warn_percent=80.0, urgent_percent=90.0,
                now_ts=hour * 3600,
            )
            for hour, pct in enumerate(
                (79.9, 80.0, 79.9, 80.1, 79.8, 80.0, 79.9, 80.2)
            )
        ]
        assert len([m for m in spoken if m]) == 1

    def test_flapping_across_days_still_speaks_once(self):
        """Тот же размах, но по одному замеру в сутки: суточный потолок
        здесь ни при чём, держит ровно гистерезис. Без этого теста
        проверка сценария ревьюера зеленела бы по чужой причине."""
        state = {}
        spoken = [
            should_warn(
                _report(pct), state, warn_percent=80.0, urgent_percent=90.0,
                now_ts=day * DAY,
            )
            for day, pct in enumerate(
                (79.9, 80.0, 79.9, 80.1, 79.8, 80.0, 79.9, 80.2)
            )
        ]
        assert len([m for m in spoken if m]) == 1

    def test_a_hair_below_the_threshold_does_not_re_arm(self):
        """Сутки между замерами: потолок отпустил, а взвестись заново
        нечему — падение было на волосок."""
        state = {}
        assert should_warn(_report(85.0), state, warn_percent=80.0, now_ts=0)
        assert should_warn(
            _report(79.9), state, warn_percent=80.0, now_ts=DAY + 1
        ) is None
        assert should_warn(
            _report(85.0), state, warn_percent=80.0, now_ts=2 * DAY
        ) is None

    def test_a_real_drop_re_arms(self):
        """Разгрузили по-настоящему — следующий подъём снова заслуживает
        слов, иначе предупреждение говорится один раз за всё время жизни
        машины."""
        state = {}
        assert should_warn(
            _report(85.0), state, warn_percent=80.0, rearm_percent=3.0, now_ts=0
        )
        assert should_warn(
            _report(70.0), state, warn_percent=80.0, rearm_percent=3.0, now_ts=DAY
        ) is None
        assert should_warn(
            _report(85.0), state, warn_percent=80.0, rearm_percent=3.0, now_ts=2 * DAY
        )

    def test_the_margin_is_configurable_and_actually_moves(self):
        """Падение до 78 % при пороге 80: с запасом 3 пункта это ещё не
        разгрузка, с запасом 1 — уже."""
        def _run(rearm):
            state = {}
            should_warn(_report(85.0), state, warn_percent=80.0,
                        rearm_percent=rearm, now_ts=0)
            should_warn(_report(78.0), state, warn_percent=80.0,
                        rearm_percent=rearm, now_ts=DAY)
            return should_warn(_report(85.0), state, warn_percent=80.0,
                               rearm_percent=rearm, now_ts=2 * DAY)

        assert _run(3.0) is None
        assert _run(1.0) is not None


class TestTheDailyCeiling:
    """Потолок на повторы: одно и то же предупреждение не чаще раза в
    сутки, даже если занятость честно ходит через порог с большим
    размахом."""

    def test_a_wide_swing_within_a_day_speaks_once(self):
        state = {}
        spoken = []
        for hour, pct in enumerate((85.0, 60.0, 85.0, 60.0, 85.0)):
            spoken.append(
                should_warn(
                    _report(pct), state, warn_percent=80.0, urgent_percent=90.0,
                    now_ts=hour * 3600,
                )
            )
        assert len([m for m in spoken if m]) == 1

    def test_the_same_swing_a_day_later_speaks_again(self):
        state = {}
        assert should_warn(_report(85.0), state, warn_percent=80.0, now_ts=0)
        assert should_warn(_report(60.0), state, warn_percent=80.0, now_ts=3600) is None
        assert should_warn(_report(85.0), state, warn_percent=80.0, now_ts=DAY + 7200)

    def test_the_ceiling_is_per_level(self):
        """Суточный потолок на жёлтое не имеет права заткнуть красное:
        оно про другое."""
        state = {}
        assert should_warn(
            _report(85.0), state, warn_percent=80.0, urgent_percent=90.0, now_ts=0
        )
        assert should_warn(
            _report(95.0), state, warn_percent=80.0, urgent_percent=90.0, now_ts=3600
        ).startswith("🔴")

    def test_the_window_is_configurable_and_actually_moves(self):
        def _run(hours):
            state = {}
            should_warn(_report(85.0), state, warn_percent=80.0,
                        repeat_after_hours=hours, now_ts=0)
            should_warn(_report(60.0), state, warn_percent=80.0,
                        repeat_after_hours=hours, now_ts=3600)
            return should_warn(_report(85.0), state, warn_percent=80.0,
                               repeat_after_hours=hours, now_ts=7200)

        assert _run(24.0) is None
        assert _run(1.0) is not None

    def test_a_stamp_from_the_future_does_not_gag_the_warning(self):
        """Часы ушли вперёд, потом их поправили. Отметка из будущего не
        имеет права молчать весь сдвиг — это тот же диск, о котором надо
        сказать."""
        state = {"soft_armed": True, "last_soft_ts": 1.0e12}
        assert should_warn(_report(95.0), state, warn_percent=80.0, now_ts=1.8e9)


# ---------------------------------------------------------------------------
class TestTheCheapProbe:
    """Дешёвый замер — тот, по которому принимается решение о пороге."""

    def test_it_reports_the_same_percent_the_report_does(self, tmp_path, monkeypatch):
        """Два числа об одном и том же обязаны совпадать: решение
        принимается по дешёвому, а клиент читает то, что в отчёте."""
        import hermes_cli.trix_disk as td
        from hermes_cli.trix_disk import build_report

        class _Usage:
            # used + free < total — как на настоящей файловой системе:
            # часть блоков зарезервирована и не принадлежит ни тому, ни
            # другому. Ровно поэтому «занято» нельзя считать через
            # «свободно»: два числа разойдутся, и решение о пороге
            # разъедется с тем, что клиент прочтёт в отчёте.
            total, used, free = 100 * GB, 37 * GB, 58 * GB

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)
        monkeypatch.setattr(td, "_disk_usage", lambda p: (p, _Usage()))

        # Ожидаемое — арифметика, а не второй вызов той же функции: 37 ГБ
        # из 100 ГБ это 37 %, и это верно независимо от того, что написано
        # в модуле. Сравнение двух его собственных вызовов ошибку в самой
        # формуле не поймало бы — она сдвинула бы обе стороны одинаково.
        assert partition_used_percent(home) == 37.0
        assert build_report(home).used_percent == 37.0

    def test_a_failed_probe_is_none_not_an_exception(self, tmp_path, monkeypatch):
        """Упавший замер не имеет права уронить почасовую уборку шлюза."""
        import hermes_cli.trix_disk as td

        def _boom(path):
            raise OSError("раздел исчез")

        monkeypatch.setattr(td, "_disk_usage", _boom)
        assert partition_used_percent(tmp_path) is None

    def test_an_unknown_percent_neither_warns_nor_touches_the_mark(self):
        state = {"last_warn_percent": 85.0}
        assert warn_level(None, state, warn_percent=80.0) is None
        assert state == {"last_warn_percent": 85.0}


# ---------------------------------------------------------------------------
# Месячная сводка
# ---------------------------------------------------------------------------


class TestMonthly:
    def test_a_fresh_state_is_not_due(self):
        """Отсчёт ещё не начат — сводке неоткуда взяться."""
        assert not should_report_monthly({}, now_ts=1.8e9)

    def test_the_countdown_starts_once_and_stays_put(self):
        state = {}
        assert start_monthly_countdown(state, now_ts=1000.0)
        assert not start_monthly_countdown(state, now_ts=2000.0)
        assert state["last_monthly_ts"] == 1000.0

    def test_nothing_is_due_right_after_the_countdown_starts(self):
        """Клиент только что настроил машину и впервые написал боту.
        Технический отчёт, которого он не просил, читается не заботой, а
        сбоем."""
        state = {}
        start_monthly_countdown(state, now_ts=1.8e9)
        assert not should_report_monthly(state, now_ts=1.8e9 + 3600)

    def test_it_is_due_a_month_after_the_countdown_started(self):
        state = {}
        start_monthly_countdown(state, now_ts=1.8e9)
        assert should_report_monthly(state, now_ts=1.8e9 + MONTH)

    def test_it_does_not_repeat_within_the_month(self):
        state = {"last_monthly_ts": MONTH}
        assert not should_report_monthly(state, now_ts=MONTH + 1)
        assert not should_report_monthly(state, now_ts=MONTH + MONTH - 1)

    def test_it_comes_back_a_month_later(self):
        state = {"last_monthly_ts": MONTH}
        assert should_report_monthly(state, now_ts=2 * MONTH)

    def test_a_mark_from_the_future_restarts_the_countdown(self):
        """Часы ушли вперёд, потом их поправили. Оставить отметку из
        будущего — запереть сводку на всю длину сдвига: при часах на год
        вперёд она не пришла бы ни через два месяца, ни через одиннадцать."""
        year_ahead = 1.8e9 + 365 * 24 * 3600
        state = {"last_monthly_ts": year_ahead}
        now = 1.8e9

        assert start_monthly_countdown(state, now_ts=now)
        assert state["last_monthly_ts"] == now
        assert not should_report_monthly(state, now_ts=now + MONTH - 1)
        assert should_report_monthly(state, now_ts=now + MONTH)

    def test_a_future_mark_left_alone_would_lock_the_summary(self):
        """Обратная сторона: без починки отсчёта сводка действительно
        заперта — этот тест держит саму починку осмысленной."""
        year_ahead = 1.8e9 + 365 * 24 * 3600
        assert not should_report_monthly(
            {"last_monthly_ts": year_ahead}, now_ts=1.8e9 + 2 * MONTH
        )

    def test_a_sane_mark_is_left_alone(self):
        state = {"last_monthly_ts": 1000.0}
        assert not start_monthly_countdown(state, now_ts=1.8e9)
        assert state["last_monthly_ts"] == 1000.0

    def test_a_broken_mark_restarts_the_countdown(self):
        """Мусор в отметке не должен ни запирать сводку навсегда, ни
        выпускать лишнюю немедленно."""
        for junk in ("вчера", None, True, [1]):
            state = {"last_monthly_ts": junk}
            assert not should_report_monthly(state, now_ts=1.8e9)
            assert start_monthly_countdown(state, now_ts=1.8e9)
            assert not should_report_monthly(state, now_ts=1.8e9 + 3600)
            assert should_report_monthly(state, now_ts=1.8e9 + MONTH)


# ---------------------------------------------------------------------------
# Состояние на диске
# ---------------------------------------------------------------------------


class TestState:
    def test_state_survives_a_round_trip(self, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        save_state(home, {"last_warn_percent": 91.0, "last_monthly_ts": 5})
        loaded = load_state(home)
        assert loaded["last_warn_percent"] == 91.0
        assert loaded["last_monthly_ts"] == 5

    def test_a_missing_file_reads_as_empty(self, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        assert load_state(home) == {}

    def test_a_broken_file_reads_as_empty(self, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "trix_disk_state.json").write_text("{не json", encoding="utf-8")
        assert load_state(home) == {}

    def test_a_json_scalar_reads_as_empty(self, tmp_path):
        """Валидный JSON, но не словарь — тоже мусор: `.get` по нему упал бы."""
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "trix_disk_state.json").write_text("[1, 2]", encoding="utf-8")
        assert load_state(home) == {}

    def test_saving_never_leaves_a_half_written_file(self, tmp_path):
        """Перезапись поверх существующего состояния обязана быть целой:
        обрывок JSON стёр бы отметку и выпустил лишнюю сводку."""
        home = tmp_path / ".hermes"
        home.mkdir()
        save_state(home, {"last_monthly_ts": 1})
        save_state(home, {"last_monthly_ts": 2})
        assert json.loads((home / "trix_disk_state.json").read_text(encoding="utf-8")) == {
            "last_monthly_ts": 2
        }

    def test_saving_into_an_unwritable_home_does_not_raise(self, tmp_path):
        """Уборка шлюза не должна падать из-за файла отметок."""
        save_state(tmp_path / "нет-такого-каталога", {"soft_armed": False})

    def test_a_failed_save_says_so(self, tmp_path, caplog):
        """Условие срабатывания совпадает с тем, о чём мы предупреждаем: на
        кончившемся диске запись падает по нехватке места. Молчаливая
        неудача здесь — это спам клиенту каждый час и пустой журнал у
        владельца машины."""
        import hermes_cli.trix_disk as td

        with caplog.at_level(logging.WARNING, logger=td.logger.name):
            ok = save_state(tmp_path / "нет-такого-каталога", {"soft_armed": False})
        assert ok is False
        assert any(
            "диск" in r.getMessage().lower()
            for r in caplog.records
            if r.levelno >= logging.WARNING
        ), "запись отметок провалилась молча"

    def test_a_successful_save_says_so(self, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        assert save_state(home, {"soft_armed": False}) is True

    def test_a_failed_save_is_remembered_in_process(self, tmp_path, monkeypatch):
        """«Один раз на пересечение» обязано сохраняться хотя бы до
        перезапуска, даже когда записать на диск не вышло."""
        import hermes_cli.trix_disk as td

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setattr(td, "_state_fallback", {})

        def _no_writes(*a, **kw):
            raise OSError("No space left on device")

        monkeypatch.setattr(Path, "write_text", _no_writes)
        assert save_state(home, {"soft_armed": False, "last_soft_ts": 5.0}) is False
        assert load_state(home) == {"soft_armed": False, "last_soft_ts": 5.0}

    def test_a_later_successful_save_takes_over_again(self, tmp_path, monkeypatch):
        """Диск снова пишется — он и есть источник правды."""
        import hermes_cli.trix_disk as td

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setattr(td, "_state_fallback", {})
        real_write = Path.write_text

        def _no_writes(*a, **kw):
            raise OSError("No space left on device")

        monkeypatch.setattr(Path, "write_text", _no_writes)
        save_state(home, {"soft_armed": False})
        monkeypatch.setattr(Path, "write_text", real_write)
        assert save_state(home, {"soft_armed": True}) is True
        assert load_state(home) == {"soft_armed": True}


# ---------------------------------------------------------------------------
# Пороги из config.yaml
# ---------------------------------------------------------------------------


class TestThresholdsConfig:
    def test_defaults_when_the_config_says_nothing(self):
        t = disk_thresholds({})
        assert 0 < t.warn_percent < t.urgent_percent <= 100
        assert t.min_cleanup_bytes > 0

    def test_configured_values_win(self):
        t = disk_thresholds({"disk": {"warn_percent": 55, "urgent_percent": 70,
                                      "min_cleanup_mb": 7}})
        assert t.warn_percent == 55
        assert t.urgent_percent == 70
        assert t.min_cleanup_bytes == 7 * 1024 ** 2

    def test_the_default_config_carries_the_root(self):
        """Ключи обязаны быть в DEFAULT_CONFIG, а не только в нашем коде:
        иначе клиент не найдёт, что править, а `hermes doctor` сочтёт
        корень `disk` неизвестным."""
        from hermes_cli.config import _KNOWN_ROOT_KEYS
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert "disk" in _KNOWN_ROOT_KEYS
        assert disk_thresholds(DEFAULT_CONFIG) == disk_thresholds({})

    def test_every_key_in_the_disk_root_actually_does_something(self):
        """Ключ в config.yaml, который ничего не меняет, — ложь клиенту: он
        поправит его и будет ждать другого поведения. Заодно это ловит
        опечатку в имени ключа с любой из двух сторон."""
        import copy

        from hermes_cli.config_defaults import DEFAULT_CONFIG

        base = disk_thresholds(DEFAULT_CONFIG)
        assert DEFAULT_CONFIG["disk"], "корень disk пуст"
        for key, value in DEFAULT_CONFIG["disk"].items():
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["disk"][key] = float(value) / 2
            assert disk_thresholds(config) != base, (
                f"ключ disk.{key} в config.yaml ничего не меняет"
            )

    def test_garbage_falls_back_instead_of_raising(self):
        assert disk_thresholds({"disk": "восемьдесят"}) == disk_thresholds({})
        assert disk_thresholds({"disk": {"warn_percent": "много"}}) == disk_thresholds({})

    def test_an_urgent_threshold_below_the_soft_one_is_lifted(self):
        """Иначе «почти кончилось» звучало бы с первого же предупреждения."""
        t = disk_thresholds({"disk": {"warn_percent": 80, "urgent_percent": 40}})
        assert t.urgent_percent >= t.warn_percent

    def test_it_reads_the_live_config_by_default(self, tmp_path, monkeypatch):
        """Без аргумента пороги берутся из config.yaml профиля, а не из кода."""
        import yaml

        from hermes_cli.config import _LOAD_CONFIG_CACHE

        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            yaml.safe_dump({"disk": {"warn_percent": 42}}), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        _LOAD_CONFIG_CACHE.clear()
        try:
            assert disk_thresholds().warn_percent == 42
        finally:
            _LOAD_CONFIG_CACHE.clear()


class TestTheCommandUsesTheSameThresholds:
    """Отчёт по запросу и предупреждение по расписанию обязаны называть
    «мало места» в одной и той же точке: клиент читает их подряд, и
    расхождение он прочтёт как противоречие бота самому себе."""

    def test_the_command_honours_the_configured_warn_threshold(
        self, tmp_path, monkeypatch
    ):
        import yaml

        import hermes_cli.trix_disk as td
        from hermes_cli.config import _LOAD_CONFIG_CACHE
        from hermes_cli.slash_exec import CommandContext, execute_command

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(td, "cached_report", lambda h, **kw: _report(65.0))

        def _disk_text():
            _LOAD_CONFIG_CACHE.clear()
            return execute_command("disk", CommandContext(surface="gateway")).text

        (home / "config.yaml").write_text(
            yaml.safe_dump({"disk": {"warn_percent": 90}}), encoding="utf-8"
        )
        calm = _disk_text()
        (home / "config.yaml").write_text(
            yaml.safe_dump({"disk": {"warn_percent": 60}}), encoding="utf-8"
        )
        alarmed = _disk_text()
        _LOAD_CONFIG_CACHE.clear()

        assert "почти нет" not in calm
        assert "почти нет" in alarmed, (
            "порог из config.yaml не доехал до /disk — настройка декоративна"
        )


# ---------------------------------------------------------------------------
# Почасовая проверка в шлюзе
# ---------------------------------------------------------------------------


class _Sent:
    """Перехват доставки: что ушло в домашний чат и ушло ли вообще."""

    def __init__(self, delivered: bool = True) -> None:
        self.delivered = delivered
        self.messages: list[str] = []

    def __call__(self, adapters, loop, text):
        self.messages.append(text)
        return self.delivered


class _Watch:
    """Управляемая машина: занятость раздела отдельно от обхода дерева.

    Дешёвый замер и дорогой обход разведены нарочно — только так видно,
    какой из них на самом деле принял решение и был ли обход вообще.
    """

    def __init__(self, home: Path, percent: dict, scans: list, clock: dict) -> None:
        self.home = home
        self._percent = percent
        self.scans = scans
        self._clock = clock

    def wait_hours(self, hours: float) -> None:
        """Промотать стенные часы: отметки живут на них, а не на
        монотонных — они обязаны переживать перезапуск процесса."""
        self._clock["now"] += hours * 3600

    @property
    def percent(self) -> float:
        return self._percent["cheap"]

    @percent.setter
    def percent(self, value: float) -> None:
        self._percent["cheap"] = value
        self._percent["scanned"] = value

    def disagree(self, *, cheap: float, scanned: float) -> None:
        """Развести два числа: чем именно принято решение."""
        self._percent["cheap"] = cheap
        self._percent["scanned"] = scanned

    def make_the_monthly_due(self) -> None:
        state = load_state(self.home)
        state["last_monthly_ts"] = self._clock["now"] - MONTH - 1
        save_state(self.home, state)

    def rewind_a_month(self) -> None:
        """Отмотать УЖЕ поставленную отметку на месяц назад.

        Отличается от ``make_the_monthly_due`` намеренно: здесь отсчёт
        обязан быть начат самим тиком, иначе отматывать нечего — так
        проверяется вся цепочка «установка → месяц → сводка», а не только
        её вторая половина.
        """
        state = load_state(self.home)
        assert "last_monthly_ts" in state, "отсчёт не начат — отматывать нечего"
        state["last_monthly_ts"] = float(state["last_monthly_ts"]) - MONTH - 1
        save_state(self.home, state)


@pytest.fixture
def watch_home(tmp_path, monkeypatch):
    """HERMES_HOME с предсказуемой занятостью раздела и подсчётом обходов."""
    import hermes_cli.trix_disk as td

    home = tmp_path / ".hermes"
    (home / "cache" / "documents").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)

    percent = {"cheap": 10.0, "scanned": 10.0}
    scans: list = []
    clock = {"now": 1.8e9}

    monkeypatch.setattr(td, "partition_used_percent", lambda h: percent["cheap"])
    monkeypatch.setattr(
        td, "_wall_clock",
        lambda now_ts=None: clock["now"] if now_ts is None else float(now_ts),
    )

    def _fake_report(h, **kwargs):
        scans.append(h)
        return _report(percent["scanned"])

    monkeypatch.setattr(td, "cached_report", _fake_report)
    return _Watch(home, percent, scans, clock)


def _tick(monkeypatch, sender, adapters=None, loop=None):
    # disk_watch_tick вызывает send_to_home_channel через __globals__ СВОЕГО
    # модуля (hermes_cli.trix_disk_watch), а не через gateway.run — с тех
    # пор как тик переехал туда, monkeypatch на gw._trix_send_to_home_channel
    # больше не пересекается с тем, что реально читает disk_watch_tick.
    import hermes_cli.trix_disk_watch as tdw

    monkeypatch.setattr(tdw, "send_to_home_channel", sender)
    tdw.disk_watch_tick(adapters, loop)


class TestTheFirstHoursAfterInstall:
    """Клиент только что настроил машину. Всё, что придёт без повода,
    он прочтёт не как заботу, а как сбой."""

    def test_a_fresh_install_gets_no_unsolicited_summary(
        self, watch_home, monkeypatch
    ):
        sent = _Sent()
        _tick(monkeypatch, sent)
        _tick(monkeypatch, sent)
        _tick(monkeypatch, sent)
        assert sent.messages == []

    def test_the_countdown_starts_on_the_very_first_tick(
        self, watch_home, monkeypatch
    ):
        """Без этого сводка не придёт никогда: отметки нет — значит не
        пора, и так каждый час до конца времён."""
        sent = _Sent()
        _tick(monkeypatch, sent)
        assert sent.messages == []
        mark = load_state(watch_home.home).get("last_monthly_ts")
        assert isinstance(mark, (int, float)) and mark > 0

    def test_the_first_summary_arrives_a_month_after_install(
        self, watch_home, monkeypatch
    ):
        """Вся цепочка целиком: отсчёт начал сам тик, через месяц пришла
        сводка. Отметка руками не пишется."""
        from hermes_cli.trix_disk import MONTHLY_HEAD

        sent = _Sent()
        _tick(monkeypatch, sent)                 # установка: отсчёт пошёл
        assert sent.messages == []

        watch_home.rewind_a_month()
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1
        assert sent.messages[0].startswith(MONTHLY_HEAD)

    def test_a_full_disk_is_never_delayed_by_the_countdown(
        self, watch_home, monkeypatch
    ):
        """Отложить сводку можно, предупреждение — нет: оно по делу и
        работает с первой секунды."""
        watch_home.percent = 95.0
        sent = _Sent()
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1


class TestTheCheapGate:
    """Обход всех данных агента — секунды, а на клиентской машине с
    раздутыми зависимостями десятки секунд. Каждый час впустую его платить
    нельзя: это машина, где и так мало места и мало процессора."""

    def test_a_quiet_hour_costs_no_tree_walk(self, watch_home, monkeypatch):
        sent = _Sent()
        _tick(monkeypatch, sent)
        _tick(monkeypatch, sent)
        _tick(monkeypatch, sent)
        assert watch_home.scans == [], "спокойный час обошёлся обходом дерева"

    def test_a_crossing_pays_for_the_walk(self, watch_home, monkeypatch):
        """Ворота открываются, когда есть что показывать: разбивку клиенту
        без обхода не собрать."""
        watch_home.percent = 85.0
        _tick(monkeypatch, _Sent())
        assert len(watch_home.scans) == 1

    def test_a_due_summary_pays_for_the_walk(self, watch_home, monkeypatch):
        watch_home.make_the_monthly_due()
        _tick(monkeypatch, _Sent())
        assert len(watch_home.scans) == 1

    def test_the_crossing_is_decided_by_the_cheap_number(
        self, watch_home, monkeypatch
    ):
        """Ворота не имеют права съесть предупреждение: порог считается по
        занятости раздела, а она и есть дешёвое число. Здесь обход нарочно
        врёт, будто места полно, — предупреждение обязано уйти всё равно."""
        watch_home.disagree(cheap=95.0, scanned=10.0)
        sent = _Sent()
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1
        assert len(watch_home.scans) == 1

    def test_a_quiet_cheap_number_stays_quiet(self, watch_home, monkeypatch):
        """Обратная сторона: дорогой обход не имеет права поднять тревогу
        там, где раздел спокоен."""
        watch_home.disagree(cheap=10.0, scanned=95.0)
        sent = _Sent()
        _tick(monkeypatch, sent)
        assert sent.messages == []
        assert watch_home.scans == []

    def test_the_cheap_bookkeeping_survives_a_quiet_hour(
        self, watch_home, monkeypatch
    ):
        """Взведение «диск разгрузили» обязано идти и в дешёвый час: иначе
        оно застрянет наверху, и повторное пересечение промолчит навсегда."""
        sent = _Sent()
        watch_home.percent = 85.0
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1

        watch_home.percent = 30.0          # дешёвый час, обхода быть не должно
        walks_before = len(watch_home.scans)
        watch_home.wait_hours(1)
        _tick(monkeypatch, sent)
        assert len(watch_home.scans) == walks_before

        watch_home.percent = 85.0
        watch_home.wait_hours(25)          # суточный потолок отпустил
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 2, "после разгрузки порог снова замолчал"

    def test_a_failed_cheap_probe_stays_quiet(self, watch_home, monkeypatch):
        """Замер не удался — не повод ни будить клиента, ни платить за
        обход."""
        import hermes_cli.trix_disk as td

        monkeypatch.setattr(td, "partition_used_percent", lambda h: None)
        sent = _Sent()
        _tick(monkeypatch, sent)
        assert sent.messages == []
        assert watch_home.scans == []


class TestGatewayTick:
    def test_crossing_the_threshold_speaks_once(self, watch_home, monkeypatch):
        sent = _Sent()
        _tick(monkeypatch, sent)          # спокойный диск — тишина
        watch_home.percent = 85.0
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1
        _tick(monkeypatch, sent)
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1, "предупреждение раз в час — шум"

    def test_the_mark_survives_a_gateway_restart(self, watch_home, monkeypatch):
        """Отметка живёт в файле, а не в памяти процесса: перезапуск шлюза
        не должен снова будить клиента тем же предупреждением."""
        watch_home.percent = 85.0
        sent = _Sent()
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1
        assert (watch_home.home / "trix_disk_state.json").exists()
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1

    def test_an_undelivered_warning_is_retried_next_hour(self, watch_home, monkeypatch):
        """Если доставить не удалось, отметку ставить нельзя: единственное
        предупреждение о кончающемся диске было бы потеряно навсегда."""
        watch_home.percent = 85.0
        failed = _Sent(delivered=False)
        _tick(monkeypatch, failed)
        _tick(monkeypatch, failed)
        assert len(failed.messages) == 2

        ok = _Sent(delivered=True)
        _tick(monkeypatch, ok)
        assert len(ok.messages) == 1
        _tick(monkeypatch, ok)
        assert len(ok.messages) == 1, "доставленное предупреждение не повторяется"

    def test_an_undelivered_summary_does_not_re_walk_every_hour(
        self, watch_home, monkeypatch
    ):
        """Сводка не срочная, а её текст стоит полного обхода дерева. Пока
        она не доставлена, «пора» остаётся истинным — без паузы шлюз платил
        бы за обход каждый час на машине, где и так мало процессора."""
        watch_home.make_the_monthly_due()
        failed = _Sent(delivered=False)
        _tick(monkeypatch, failed)
        assert len(failed.messages) == 1
        assert len(watch_home.scans) == 1

        for _ in range(5):
            watch_home.wait_hours(1)
            _tick(monkeypatch, failed)
        assert len(failed.messages) == 1
        assert len(watch_home.scans) == 1

    def test_an_undelivered_summary_is_retried_the_next_day(
        self, watch_home, monkeypatch
    ):
        watch_home.make_the_monthly_due()
        failed = _Sent(delivered=False)
        _tick(monkeypatch, failed)
        watch_home.wait_hours(25)
        _tick(monkeypatch, failed)
        assert len(failed.messages) == 2

    def test_a_delivered_summary_is_not_repeated_next_hour(
        self, watch_home, monkeypatch
    ):
        """Прямая проверка: два тика — одна сводка."""
        watch_home.make_the_monthly_due()
        sent = _Sent()
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1
        watch_home.wait_hours(1)
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1

    def test_an_undelivered_warning_is_still_retried_next_hour(
        self, watch_home, monkeypatch
    ):
        """Предупреждение — срочное, на него пауза не распространяется."""
        watch_home.percent = 95.0
        failed = _Sent(delivered=False)
        _tick(monkeypatch, failed)
        watch_home.wait_hours(1)
        _tick(monkeypatch, failed)
        assert len(failed.messages) == 2

    def test_a_warning_covers_the_summary_due_the_same_hour(
        self, watch_home, monkeypatch
    ):
        """Сводка и предупреждение несут один и тот же отчёт — два подряд
        сообщения об одном и том же клиент прочтёт как поломку."""
        watch_home.make_the_monthly_due()
        watch_home.percent = 85.0
        sent = _Sent()
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1
        watch_home.percent = 86.0
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1

    def test_the_monthly_summary_comes_back_a_month_later(
        self, watch_home, monkeypatch
    ):
        import hermes_cli.trix_disk as td

        sent = _Sent()
        watch_home.make_the_monthly_due()
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1

        state = load_state(watch_home.home)
        state["last_monthly_ts"] = state["last_monthly_ts"] - td._MONTH_SECONDS - 1
        save_state(watch_home.home, state)
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 2

    def test_the_summary_says_it_is_a_summary(self, watch_home, monkeypatch):
        watch_home.make_the_monthly_due()
        sent = _Sent()
        _tick(monkeypatch, sent)
        first_line = sent.messages[0].splitlines()[0]
        assert "сводка" in first_line.lower()

    def test_a_missing_home_channel_reaches_the_log(
        self, watch_home, monkeypatch, caplog, running_loop
    ):
        """Молча терять предупреждение нельзя — иначе никто никогда не
        узнает, что механизм не работал. Настоящая производственная форма
        отказа: шлюз жив, Telegram подключён, а /sethome никто не делал."""
        import gateway.config as gwconfig
        import gateway.run as gw

        watch_home.percent = 95.0
        monkeypatch.setattr(
            gwconfig, "load_gateway_config", lambda: _gateway_config(chat_id=None)
        )
        adapters = {gwconfig.Platform.TELEGRAM: _Adapter()}
        with caplog.at_level(logging.WARNING, logger=gw.logger.name):
            gw._trix_disk_watch_tick(adapters, running_loop)
        assert any(
            "диск" in r.getMessage().lower()
            for r in caplog.records
            if r.levelno >= logging.WARNING
        ), "недоставленное предупреждение прошло молча"

    def test_an_unwritable_state_file_does_not_spam_the_client(
        self, watch_home, monkeypatch
    ):
        """Условие срабатывания совпадает с тем, о чём мы предупреждаем: на
        кончившемся диске запись отметок падает по нехватке места. Без
        запаса в памяти процесса клиент получил бы двадцать четыре «место
        почти кончилось» в сутки."""
        import hermes_cli.trix_disk as td

        monkeypatch.setattr(td, "_state_fallback", {})

        def _no_writes(*a, **kw):
            raise OSError("No space left on device")

        monkeypatch.setattr(Path, "write_text", _no_writes)
        watch_home.percent = 95.0
        sent = _Sent()
        for _ in range(5):
            _tick(monkeypatch, sent)
            watch_home.wait_hours(1)
        assert len(sent.messages) == 1

    def test_an_unwritable_state_file_reaches_the_log(
        self, watch_home, monkeypatch, caplog
    ):
        """Владелец машины обязан увидеть, что отметки не пишутся: иначе
        он не поймёт, почему бот начал повторяться после перезапуска."""
        import hermes_cli.trix_disk as td

        monkeypatch.setattr(td, "_state_fallback", {})

        def _no_writes(*a, **kw):
            raise OSError("No space left on device")

        monkeypatch.setattr(Path, "write_text", _no_writes)
        watch_home.percent = 95.0
        with caplog.at_level(logging.WARNING, logger=td.logger.name):
            _tick(monkeypatch, _Sent())
        assert [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_a_broken_state_file_does_not_break_the_tick(self, watch_home, monkeypatch):
        (watch_home.home / "trix_disk_state.json").write_text(
            "{не json", encoding="utf-8"
        )
        watch_home.percent = 85.0
        sent = _Sent()
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1

    def _pin_thresholds(self, monkeypatch, *, warn, urgent):
        import hermes_cli.trix_disk as td

        monkeypatch.setattr(
            td, "disk_thresholds",
            lambda config=None: td.DiskThresholds(
                warn_percent=warn, urgent_percent=urgent,
                min_cleanup_bytes=td._MIN_CLEANUP_BYTES,
            ),
        )

    def test_the_configured_warn_threshold_drives_the_tick(
        self, watch_home, monkeypatch
    ):
        """Порог из config.yaml обязан доехать до почасовой проверки, иначе
        настройка декоративна."""
        from hermes_cli.trix_disk import MONTHLY_HEAD

        watch_home.percent = 65.0
        self._pin_thresholds(monkeypatch, warn=60.0, urgent=90.0)

        sent = _Sent()
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1
        assert MONTHLY_HEAD not in sent.messages[0], "это сводка, а не тревога"
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 1

    def test_the_default_warn_threshold_stays_silent_at_the_same_percent(
        self, watch_home, monkeypatch
    ):
        """Обратная сторона той же проверки: при дефолтном пороге 65 % —
        это ещё не повод будить клиента."""
        watch_home.percent = 65.0
        sent = _Sent()
        _tick(monkeypatch, sent)
        assert sent.messages == []

    def test_the_configured_urgent_threshold_drives_the_tick(
        self, watch_home, monkeypatch
    ):
        """Срочный порог тоже обязан доехать: с urgent=70 занятость 75 %
        после мягкого предупреждения на 65 % заслуживает второго слова."""
        self._pin_thresholds(monkeypatch, warn=60.0, urgent=70.0)

        sent = _Sent()
        watch_home.percent = 65.0
        _tick(monkeypatch, sent)
        watch_home.percent = 75.0
        _tick(monkeypatch, sent)
        assert len(sent.messages) == 2
        assert sent.messages[0].splitlines()[0] != sent.messages[1].splitlines()[0]


# ---------------------------------------------------------------------------
# Проверка живёт в почасовой уборке шлюза и не блокирует цикл событий
# ---------------------------------------------------------------------------


class _NTickStopEvent:
    """Ровно ``n`` витков почасовой уборки без потока и без сна."""

    def __init__(self, n: int) -> None:
        self.left = n

    def is_set(self):
        return self.left <= 0

    def wait(self, timeout=None):
        self.left -= 1
        return self.left <= 0


@pytest.fixture
def quiet_housekeeping(monkeypatch):
    """Обезвредить соседние ветки почасовой уборки — проверяем только нашу."""
    import agent.curator as curator
    import gateway.platforms.base as base
    import hermes_cli.debug as debug
    import hermes_cli.mem_trim as mem_trim
    import tools.skills_sync_client as sync

    for name in ("cleanup_image_cache", "cleanup_document_cache", "cleanup_audio_cache",
                 "cleanup_video_cache", "cleanup_screenshot_cache"):
        monkeypatch.setattr(base, name, lambda **kw: 0)
    monkeypatch.setattr(debug, "_sweep_expired_pastes", lambda: (0, 0))
    monkeypatch.setattr(curator, "maybe_run_curator", lambda **kw: None)
    monkeypatch.setattr(sync, "maybe_pull_skills", lambda: None)
    monkeypatch.setattr(sync, "maybe_pull_org_skills", lambda: None)
    monkeypatch.setattr(mem_trim, "trim_memory", lambda **kw: False)


class TestHousekeepingWiring:
    def _count_ticks(self, monkeypatch, ticks: int) -> int:
        import gateway.run as gw

        calls: list = []
        monkeypatch.setattr(gw, "_trix_disk_watch_tick", lambda a, l: calls.append(1))
        gw._start_gateway_housekeeping(_NTickStopEvent(ticks), interval=0)
        return len(calls)

    def test_the_hourly_sweep_runs_the_disk_check(self, quiet_housekeeping, monkeypatch):
        """Своего расписания не заводим: проверка едет на той же почасовой
        уборке, что чистит кэши — клиент не может снять её словами, как
        снял бы задачу планировщика."""
        assert self._count_ticks(monkeypatch, 120) == 3   # первый, 60-й и 120-й

    def test_the_very_first_tick_checks_the_disk(self, quiet_housekeeping, monkeypatch):
        """Счётчик тиков локален потоку, а перезапуски бывают: обновления,
        падения, ребуты. Шлюз, перезапускающийся чаще раза в час, иначе не
        проверял бы диск НИКОГДА — ни предупреждения, ни сводки, и в
        журнале пусто."""
        assert self._count_ticks(monkeypatch, 1) == 1

    def test_it_does_not_run_every_minute(self, quiet_housekeeping, monkeypatch):
        """Между первым тиком и шестидесятым — тишина."""
        assert self._count_ticks(monkeypatch, 59) == 1

    def test_a_failing_check_does_not_kill_the_sweep(self, quiet_housekeeping, monkeypatch):
        """Уборка шлюза делит поток с чисткой кэшей и куратором: наша
        ветка не имеет права утащить их за собой."""
        import gateway.run as gw

        def _boom(adapters, loop):
            raise RuntimeError("замер упал")

        monkeypatch.setattr(gw, "_trix_disk_watch_tick", _boom)
        gw._start_gateway_housekeeping(_NTickStopEvent(120), interval=0)

    def test_a_failing_check_is_visible_at_the_usual_log_level(
        self, quiet_housekeeping, monkeypatch, caplog
    ):
        """Сломанная проверка ВЫГЛЯДИТ как спокойный диск — она просто
        молчит. Единственный способ отличить одно от другого — строка в
        журнале на обычном уровне, а не в отладочном."""
        import gateway.run as gw

        def _boom(adapters, loop):
            raise RuntimeError("замер упал")

        monkeypatch.setattr(gw, "_trix_disk_watch_tick", _boom)
        with caplog.at_level(logging.WARNING, logger=gw.logger.name):
            gw._start_gateway_housekeeping(_NTickStopEvent(1), interval=0)
        assert [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "упавшая проверка диска не видна при обычном уровне журнала"
        )

    def test_the_scan_does_not_freeze_the_event_loop(self, quiet_housekeeping, monkeypatch):
        """Цикл событий один на все разговоры и платформы. Обход дерева
        внутри него — молчание не команды, а всего бота."""
        import gateway.run as gw

        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()

        scanning = threading.Event()
        release = threading.Event()

        def _slow_tick(adapters, lp):
            scanning.set()
            release.wait(timeout=5)

        monkeypatch.setattr(gw, "_trix_disk_watch_tick", _slow_tick)
        sweep = threading.Thread(
            target=gw._start_gateway_housekeeping,
            args=(_NTickStopEvent(60),),
            kwargs={"loop": loop, "interval": 0},
            daemon=True,
        )
        try:
            sweep.start()
            assert scanning.wait(timeout=5), "почасовая уборка не дошла до проверки"

            async def _ping():
                return "жив"

            fut = asyncio.run_coroutine_threadsafe(_ping(), loop)
            assert fut.result(timeout=2) == "жив", "цикл событий встал на время замера"
        finally:
            release.set()
            sweep.join(timeout=5)
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=5)
            loop.close()


# ---------------------------------------------------------------------------
# Доставка в домашний чат
# ---------------------------------------------------------------------------


class _Adapter:
    def __init__(self, result=None, boom: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.result = result
        self.boom = boom

    async def send(self, chat_id, content, metadata=None):
        self.calls.append((chat_id, content, metadata))
        if self.boom is not None:
            raise self.boom
        return self.result


class _RelayAdapter(_Adapter):
    def __init__(self, fronts, **kw) -> None:
        super().__init__(**kw)
        self.fronts = fronts
        self.for_platform: list[tuple] = []

    def fronts_platform(self, platform):
        return platform in self.fronts

    async def send_for_platform(self, platform, chat_id, content, metadata=None):
        self.for_platform.append((platform, chat_id, content, metadata))
        return self.result


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def _gateway_config(*, chat_id="777", thread_id=None, user_id=None, scope_id=None):
    from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig

    config = GatewayConfig()
    pc = PlatformConfig(enabled=True)
    if chat_id is not None:
        pc.home_channel = HomeChannel(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            name="домашний чат",
            thread_id=thread_id,
            user_id=user_id,
            scope_id=scope_id,
        )
    config.platforms[Platform.TELEGRAM] = pc
    return config


class TestDelivery:
    def test_it_reaches_the_home_chat(self, monkeypatch, running_loop):
        import gateway.config as gwconfig
        import gateway.run as gw

        adapter = _Adapter()
        monkeypatch.setattr(gwconfig, "load_gateway_config", _gateway_config)
        assert gw._trix_send_to_home_channel(
            {gwconfig.Platform.TELEGRAM: adapter}, running_loop, "мало места"
        )
        assert [c[0] for c in adapter.calls] == ["777"]
        assert adapter.calls[0][1] == "мало места"

    def test_a_relay_fronting_telegram_is_found(self, monkeypatch, running_loop):
        """Шлюз за реле держит ОДИН адаптер под Platform.RELAY, который
        фронтит несколько логических платформ: прямой поиск по ключу
        Platform.TELEGRAM его не нашёл бы и клиент не получил бы ничего."""
        import gateway.config as gwconfig
        import gateway.run as gw

        relay = _RelayAdapter({gwconfig.Platform.TELEGRAM})
        monkeypatch.setattr(gwconfig, "load_gateway_config", _gateway_config)
        assert gw._trix_send_to_home_channel(
            {gwconfig.Platform.RELAY: relay}, running_loop, "мало места"
        )
        assert [c[0] for c in relay.for_platform] == [gwconfig.Platform.TELEGRAM]

    def test_a_relay_carries_the_logical_target_provenance(
        self, monkeypatch, running_loop
    ):
        """Ради этих полей ветка реле и существует: коннектор разрешает по
        ним логическую цель. Без них реле не знает, кому доставлять, и
        предупреждение о диске уходит в никуда."""
        import gateway.config as gwconfig
        import gateway.run as gw

        relay = _RelayAdapter({gwconfig.Platform.TELEGRAM})
        monkeypatch.setattr(
            gwconfig, "load_gateway_config",
            lambda: _gateway_config(
                thread_id="42", user_id="u-1", scope_id="s-1"
            ),
        )
        assert gw._trix_send_to_home_channel(
            {gwconfig.Platform.RELAY: relay}, running_loop, "мало места"
        )
        metadata = relay.for_platform[0][3]
        assert metadata["user_id"] == "u-1"
        assert metadata["scope_id"] == "s-1"
        assert metadata["thread_id"] == "42"

    def test_a_native_adapter_gets_no_relay_only_fields(
        self, monkeypatch, running_loop
    ):
        """Обратная сторона: у прямого адаптера провенанс логической цели
        не спрашивают — он сам и есть эта цель."""
        import gateway.config as gwconfig
        import gateway.run as gw

        adapter = _Adapter()
        monkeypatch.setattr(
            gwconfig, "load_gateway_config",
            lambda: _gateway_config(user_id="u-1", scope_id="s-1"),
        )
        assert gw._trix_send_to_home_channel(
            {gwconfig.Platform.TELEGRAM: adapter}, running_loop, "мало места"
        )
        assert adapter.calls[0][2] is None

    def test_a_relay_that_does_not_front_telegram_is_not_used(
        self, monkeypatch, running_loop, caplog
    ):
        import gateway.config as gwconfig
        import gateway.run as gw

        relay = _RelayAdapter(set())
        monkeypatch.setattr(gwconfig, "load_gateway_config", _gateway_config)
        with caplog.at_level(logging.WARNING, logger=gw.logger.name):
            assert not gw._trix_send_to_home_channel(
                {gwconfig.Platform.RELAY: relay}, running_loop, "мало места"
            )
        assert relay.for_platform == []
        assert caplog.records

    def test_no_home_channel_is_a_logged_refusal(
        self, monkeypatch, running_loop, caplog
    ):
        import gateway.config as gwconfig
        import gateway.run as gw

        adapter = _Adapter()
        monkeypatch.setattr(
            gwconfig, "load_gateway_config", lambda: _gateway_config(chat_id=None)
        )
        with caplog.at_level(logging.WARNING, logger=gw.logger.name):
            assert not gw._trix_send_to_home_channel(
                {gwconfig.Platform.TELEGRAM: adapter}, running_loop, "мало места"
            )
        assert adapter.calls == []
        assert caplog.records, "домашний чат не задан, а в журнале тихо"

    def test_a_dead_loop_is_a_logged_refusal(self, monkeypatch, caplog):
        import gateway.config as gwconfig
        import gateway.run as gw

        adapter = _Adapter()
        monkeypatch.setattr(gwconfig, "load_gateway_config", _gateway_config)
        with caplog.at_level(logging.WARNING, logger=gw.logger.name):
            assert not gw._trix_send_to_home_channel(
                {gwconfig.Platform.TELEGRAM: adapter}, None, "мало места"
            )
        assert caplog.records

    def test_a_raising_adapter_is_a_refusal_not_a_crash(
        self, monkeypatch, running_loop, caplog
    ):
        """Телеграм лежит — уборка шлюза обязана пережить это и повторить
        через час, а не считать предупреждение доставленным."""
        import gateway.config as gwconfig
        import gateway.run as gw

        adapter = _Adapter(boom=RuntimeError("телеграм недоступен"))
        monkeypatch.setattr(gwconfig, "load_gateway_config", _gateway_config)
        with caplog.at_level(logging.WARNING, logger=gw.logger.name):
            assert not gw._trix_send_to_home_channel(
                {gwconfig.Platform.TELEGRAM: adapter}, running_loop, "мало места"
            )
        assert caplog.records

    def test_a_send_that_reports_failure_is_a_refusal(self, monkeypatch, running_loop):
        import gateway.config as gwconfig
        import gateway.run as gw

        class _Failed:
            success = False
            error = "chat not found"

        adapter = _Adapter(result=_Failed())
        monkeypatch.setattr(gwconfig, "load_gateway_config", _gateway_config)
        assert not gw._trix_send_to_home_channel(
            {gwconfig.Platform.TELEGRAM: adapter}, running_loop, "мало места"
        )

    def test_a_forum_topic_home_chat_keeps_its_thread(self, monkeypatch, running_loop):
        """Домашний чат может быть темой форума: без thread_id сообщение
        уедет в общую ленту, где клиент его не ждёт."""
        import gateway.config as gwconfig
        import gateway.run as gw

        adapter = _Adapter()
        monkeypatch.setattr(
            gwconfig, "load_gateway_config", lambda: _gateway_config(thread_id="42")
        )
        assert gw._trix_send_to_home_channel(
            {gwconfig.Platform.TELEGRAM: adapter}, running_loop, "мало места"
        )
        assert adapter.calls[0][2]["thread_id"] == "42"


# ---------------------------------------------------------------------------
# Одно сообщение — один уровень тревоги
# ---------------------------------------------------------------------------


class TestOneMessageCarriesOneLevelOfAlarm:
    """Финальное ревью, §1.2: клиент получал в одном сообщении

        🟡 Места на диске остаётся немного.
        ⚠️ Места почти нет: занято 82 %.

    Инвариант «мягкое и срочное обязаны читаться по-разному» записан
    комментарием рядом с заголовками (задача 7) и нарушался строкой ниже:
    тело звало ``format_report``, который печатал свою тревогу по
    собственному порогу. На срочном уровне тело дублировало заголовок по
    смыслу: «🔴 почти кончилось» плюс «⚠️ Места почти нет».

    Задача 6 написала тело раньше, задача 7 добавила заголовки и не свела
    их.
    """

    _ALARM_MARKS = ("🟡", "🔴", "⚠️")

    def _marks(self, text: str) -> int:
        return sum(text.count(mark) for mark in self._ALARM_MARKS)

    def test_a_soft_warning_does_not_also_shout_the_urgent_thing(self):
        text = format_warning(WARN_SOFT, _report(82.0), warn_percent=80.0)
        assert "почти нет" not in text, text

    def test_the_urgent_warning_does_not_say_the_same_thing_twice(self):
        text = format_warning(WARN_URGENT, _report(95.0), warn_percent=80.0)
        # Заголовок уже сказал «почти кончилось»; тело обязано добавлять
        # последствие, а не повторять диагноз. Строка «Места почти нет:
        # занято N %» — ровно этот повтор.
        assert "почти нет" not in text, text
        assert text.count("почти кончил") == 1, text

    def test_exactly_one_alarm_mark_per_message(self):
        for level, pct in ((WARN_SOFT, 82.0), (WARN_URGENT, 95.0)):
            text = format_warning(level, _report(pct), warn_percent=80.0)
            assert self._marks(text) == 1, (level, text)

    def test_soft_and_urgent_differ_beyond_the_first_line(self):
        """Ради различимости заголовки и заведены. Если тела совпадают, то
        на 82 % и на 95 % клиент читает почти одно и то же."""
        soft = format_warning(WARN_SOFT, _report(82.0), warn_percent=80.0)
        urgent = format_warning(WARN_URGENT, _report(82.0), warn_percent=80.0)
        soft_body = soft.split("\n\n", 1)[1]
        urgent_body = urgent.split("\n\n", 1)[1]
        assert soft_body != urgent_body

    def test_the_soft_message_still_says_why_it_arrived(self):
        text = format_warning(WARN_SOFT, _report(82.0), warn_percent=80.0)
        assert "82" in text, "клиент должен видеть число, а не только тревогу"
        assert "освободить" in text.lower()

    def test_the_urgent_message_still_names_the_consequence(self):
        text = format_warning(WARN_URGENT, _report(95.0), warn_percent=80.0)
        assert "не смогу" in text

    def test_a_plain_report_names_the_alarm_itself(self):
        """У ответа на ``/disk`` заголовка нет — сказать про тревогу больше
        некому, и тело обязано сделать это само."""
        text = format_report(_report(82.0), warn_percent=80.0)
        assert "почти нет" in text
        assert self._marks(text) == 1, text

    def test_a_calm_disk_carries_no_alarm_at_all(self):
        text = format_report(_report(40.0), warn_percent=80.0)
        assert self._marks(text) == 0, text

    def test_the_cleanup_plan_survives_both_levels(self):
        """Разведение уровней не должно было отнять у клиента то, ради чего
        предупреждение и посылается, — что именно можно убрать."""
        from hermes_cli.trix_disk import RemovableItem

        removable = [
            RemovableItem(label="журналы", path=Path("/x/logs"), bytes=50 * GB)
        ]
        for level in (WARN_SOFT, WARN_URGENT):
            text = format_warning(
                level, _report(82.0, removable=removable), warn_percent=80.0
            )
            assert "/disk clean" in text, (level, text)
            assert "журналы" in text
