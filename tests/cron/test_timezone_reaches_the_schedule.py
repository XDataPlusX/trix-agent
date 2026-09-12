"""Ответ клиента о часовом поясе доезжает до времени срабатывания задач.

Ради этого и написана спека 11. Всё остальное — поле в мастере, список
поясов, запись в конфиг — имеет смысл ровно постольку, поскольку работает
эта цепочка:

    мастер записал `timezone` в config.yaml
      → `hermes_time.now()` считает время в этом поясе
        → `compute_next_run` привязывает момент запуска к нему
          → `get_due_jobs` сверяется с тем же временем

Тесты ниже проходят её настоящими вызовами: пишет — настоящий писатель
мастера (`apply_settings`), читает — настоящий планировщик. Ни одной
заглушки на этом пути, потому что заглушка здесь проверяла бы саму себя.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest


def _answer_in_the_wizard(timezone: str) -> None:
    """Пройти путь клиента: мастер сохраняет выбранный пояс."""
    import hermes_time
    from hermes_cli.setup_wizard.apply import apply_settings

    out = apply_settings({"timezone": timezone})
    assert out["ok"], out
    # Значение кэшируется на первом обращении. У клиента кэш свежий, потому
    # что мастер перезапускает шлюз после сохранения; в тесте сбрасываем
    # руками, иначе мерили бы кэш, а не запись.
    hermes_time.reset_cache()


@pytest.fixture(autouse=True)
def _clean_timezone_cache():
    import hermes_time

    hermes_time.reset_cache()
    yield
    hermes_time.reset_cache()


def test_nine_in_the_morning_means_nine_where_the_client_is():
    """«Напомни в 9 утра» — девять по поясу клиента, а не по серверу."""
    _answer_in_the_wizard("Europe/Moscow")
    from cron import jobs

    schedule = jobs.parse_schedule("0 9 * * *")
    moment = datetime.fromisoformat(jobs.compute_next_run(schedule))

    assert moment.astimezone(ZoneInfo("Europe/Moscow")).hour == 9


def test_the_same_schedule_fires_at_a_different_instant_in_a_different_zone():
    """Ровно та беда, ради которой спека и появилась.

    Одно и то же «9 утра», сохранённое при разных ответах клиента, обязано
    дать РАЗНЫЕ абсолютные моменты — иначе пояс никуда не доезжает и
    клиент из Москвы получает напоминание по Екатеринбургу.
    """
    import hermes_time
    from cron import jobs

    _answer_in_the_wizard("Europe/Moscow")
    moscow = datetime.fromisoformat(jobs.compute_next_run(jobs.parse_schedule("0 9 * * *")))

    _answer_in_the_wizard("Asia/Yekaterinburg")
    hermes_time.reset_cache()
    yekaterinburg = datetime.fromisoformat(
        jobs.compute_next_run(jobs.parse_schedule("0 9 * * *"))
    )

    assert moscow.utcoffset() != yekaterinburg.utcoffset()
    assert yekaterinburg.astimezone(ZoneInfo("Asia/Yekaterinburg")).hour == 9


def test_a_saved_job_carries_the_clients_zone_into_the_database():
    """Момент пишется в базу уже с поясом — это и делало смену задним
    числом небезопасной, и это же доказывает, что ответ доехал."""
    _answer_in_the_wizard("Asia/Vladivostok")
    from cron import jobs

    job = jobs.create_job(prompt="напомни выпить воды", schedule="0 9 * * *")
    stored = datetime.fromisoformat(job["next_run_at"])

    assert stored.astimezone(ZoneInfo("Asia/Vladivostok")).hour == 9


def test_an_already_saved_job_keeps_its_old_instant_when_the_zone_changes():
    """То, что мастер обещает клиенту в предупреждении, — правда.

    Заведённая задача хранит АБСОЛЮТНЫЙ момент, вычисленный по прежнему
    поясу. Смена пояса её не переносит: девять утра по Москве так и
    останется семью утра по Екатеринбургу. Поэтому решать надо до первой
    задачи клиента, и поэтому же при смене отвеченного пояса мастер
    считает задачи и предупреждает.

    Сверка на срабатывание (`get_due_jobs`) здесь ни при чём и в этом
    тесте не участвует намеренно: она сравнивает абсолютные моменты, а
    они одинаковы, в каком поясе их ни выражай. Тест, который «проверял»
    бы её зависимость от пояса, не мог бы покраснеть никогда.
    """
    import hermes_time
    from cron import jobs

    _answer_in_the_wizard("Europe/Moscow")
    job = jobs.create_job(prompt="напомни выпить воды", schedule="0 9 * * *")
    saved_instant = datetime.fromisoformat(job["next_run_at"])

    _answer_in_the_wizard("Asia/Yekaterinburg")
    hermes_time.reset_cache()
    reread = datetime.fromisoformat(jobs.get_job(job["id"])["next_run_at"])

    assert reread == saved_instant
    assert reread.astimezone(ZoneInfo("Asia/Yekaterinburg")).hour != 9
