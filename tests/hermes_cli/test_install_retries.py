"""Установка инструмента повторяется, а не сдаётся с первого раза.

Мастер ставил выбранные инструменты одной попыткой. Не получилось —
неудача уходила в список, «Готово» всё равно объявляло успех, и клиент
оставался, например, без браузера, ничего для этого не сделав.

Большинство отказов тут сетевые и лечатся повтором через несколько
секунд. Но клиент ждёт ответа в браузере, поэтому у этапа обязан быть
общий потолок времени: три попытки по 600 секунд растянули бы отправку
формы на полчаса.
"""

import pytest

from hermes_cli.trix_install_retry import (
    INSTALL_STAGE_BUDGET_SECONDS,
    MAX_ATTEMPTS,
    MIN_SECONDS_FOR_ANOTHER_ATTEMPT,
    RETRY_BACKOFF_SECONDS,
    install_with_retries,
    retry_hint,
)


class _Clock:
    """Часы и сон под управлением теста — без единой настоящей секунды."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


def _runner(outcomes, clock=None, cost=0.0):
    """Установщик, отдающий заранее заданные исходы по очереди.

    Выданный потолок времени соблюдает — ровно как настоящий
    (``_run_tool_install_with_timeout`` обрезает свою попытку по нему).
    Подделка, которая потолок игнорирует, мерила бы поведение, которого в
    продукте нет, и «ловила» бы несуществующее превышение бюджета.
    """
    calls = []

    def run(key, timeout):
        calls.append({"key": key, "timeout": timeout})
        if clock is not None:
            clock.advance(min(cost, timeout))
        index = min(len(calls) - 1, len(outcomes) - 1)
        return outcomes[index]

    run.calls = calls
    return run


def test_a_success_on_the_first_try_does_not_retry():
    run = _runner([{"ok": True}])
    out = install_with_retries("browser", run, budget_left=900, sleep=lambda s: None)
    assert out["ok"] is True
    assert out["attempts"] == 1
    assert len(run.calls) == 1


def test_a_transient_failure_is_retried_and_succeeds():
    """Ровно тот случай, ради которого всё делается: сеть моргнула."""
    clock = _Clock()
    run = _runner([{"ok": False}, {"ok": True}], clock)
    out = install_with_retries(
        "browser", run, budget_left=900, sleep=clock.sleep, monotonic=clock.monotonic
    )
    assert out["ok"] is True
    assert out["attempts"] == 2
    assert clock.slept == [RETRY_BACKOFF_SECONDS[0]]


def test_a_permanent_failure_stops_after_the_last_attempt():
    clock = _Clock()
    run = _runner([{"ok": False}], clock)
    out = install_with_retries(
        "browser", run, budget_left=900, sleep=clock.sleep, monotonic=clock.monotonic
    )
    assert out["ok"] is False
    assert out["attempts"] == MAX_ATTEMPTS
    assert len(run.calls) == MAX_ATTEMPTS


def test_the_pause_grows_between_attempts():
    """Сетевая икота проходит за секунды; если не помогло — спешить некуда."""
    clock = _Clock()
    run = _runner([{"ok": False}], clock)
    install_with_retries(
        "browser", run, budget_left=900, sleep=clock.sleep, monotonic=clock.monotonic
    )
    assert clock.slept == list(RETRY_BACKOFF_SECONDS)
    assert clock.slept == sorted(clock.slept), "пауза обязана расти, а не скакать"


# --- потолок времени ----------------------------------------------------------


def test_a_hung_install_never_blows_the_stage_budget():
    """Клиент ждёт ответа в браузере — три зависания по 600 секунд его не дождутся.

    Повтор после зависания не запрещён: остатка бюджета может хватить на
    честную вторую попытку, и она полезна. Запрещено другое — вылезти за
    потолок этапа. Проверяется именно это, а не число попыток.
    """
    clock = _Clock()
    run = _runner([{"ok": False}], clock, cost=600.0)
    out = install_with_retries(
        "browser", run, budget_left=900, sleep=clock.sleep, monotonic=clock.monotonic
    )
    assert out["ok"] is False
    assert out["spent"] <= 900 + max(RETRY_BACKOFF_SECONDS), (
        "этап вылез за свой потолок: " f"{out['spent']}"
    )
    assert out["attempts"] < MAX_ATTEMPTS, "на три зависания времени быть не должно"


def test_a_second_attempt_after_a_hang_is_capped_by_what_is_left():
    """Вторая попытка не имеет права снова просить полные 600 секунд."""
    clock = _Clock()
    run = _runner([{"ok": False}], clock, cost=600.0)
    install_with_retries(
        "browser", run, budget_left=900, sleep=clock.sleep, monotonic=clock.monotonic
    )
    if len(run.calls) > 1:
        assert run.calls[1]["timeout"] < 600.0


def test_each_attempt_is_told_how_much_time_is_left():
    """Иначе одна долгая попытка съела бы бюджет всех остальных."""
    clock = _Clock()
    run = _runner([{"ok": False}, {"ok": True}], clock, cost=100.0)
    install_with_retries(
        "browser", run, budget_left=900, sleep=clock.sleep, monotonic=clock.monotonic
    )
    timeouts = [c["timeout"] for c in run.calls]
    assert timeouts[0] == pytest.approx(900.0)
    assert timeouts[1] < timeouts[0], "вторая попытка обязана знать про потраченное"


def test_no_attempt_starts_without_time_for_it():
    clock = _Clock()
    run = _runner([{"ok": False}], clock, cost=1.0)
    out = install_with_retries(
        "browser",
        run,
        budget_left=MIN_SECONDS_FOR_ANOTHER_ATTEMPT / 2,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert out["attempts"] == 1, "первую попытку делаем всегда"
    assert len(run.calls) == 1


def test_an_exhausted_budget_reports_what_it_spent():
    """Вызывающий ведёт общий бюджет этапа по этому числу."""
    clock = _Clock()
    run = _runner([{"ok": False}], clock, cost=50.0)
    out = install_with_retries(
        "browser", run, budget_left=900, sleep=clock.sleep, monotonic=clock.monotonic
    )
    assert out["spent"] >= 150.0, "три попытки по 50 секунд плюс паузы"


def test_a_broken_installer_is_not_disguised_as_a_failed_install():
    """Исключение из установщика — поломка кода, её нельзя прятать."""

    def explodes(key, timeout):
        raise RuntimeError("установщик сломан")

    with pytest.raises(RuntimeError):
        install_with_retries("browser", explodes, budget_left=900, sleep=lambda s: None)


# --- что читает клиент --------------------------------------------------------


def test_the_client_is_told_that_pressing_done_again_retries():
    """Это рабочий путь, а не отговорка: этап берёт невставшее заново."""
    assert "Готово" in retry_hint(1)
    assert "Готово" in retry_hint(3)


def test_the_client_is_told_how_many_times_we_already_tried():
    assert "3" in retry_hint(3)


def test_the_stage_budget_leaves_room_for_more_than_one_attempt():
    """Инвариант: потолок этапа не должен быть меньше пауз между попытками."""
    assert INSTALL_STAGE_BUDGET_SECONDS > sum(RETRY_BACKOFF_SECONDS) + 60
