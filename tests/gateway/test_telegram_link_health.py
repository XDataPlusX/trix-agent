"""Счётчик разрывов связи с Телеграмом.

Повторы отправки и осушение пула соединений адаптер переживает молча —
и правильно делает, но клиент после этого видит только «бот отвечал
долго» или «бот пропадал». Сказать ему о недоступности Телеграма через
Телеграм невозможно; посчитать — можно. Здесь проверяется, что счёт
ведётся, переживает перезапуск, не растёт бесконечно и не может уронить
отправку, ради которой заведён.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.platforms.telegram import link_health

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def home(tmp_path):
    return tmp_path


def test_nothing_recorded_reads_as_a_quiet_day(home):
    assert link_health.summary(24, home=home, now=NOW) == {
        "hours": 24, "total": 0, "by_kind": {},
    }


def test_events_are_counted_by_kind(home):
    for _ in range(3):
        link_health.record(link_health.KIND_POOL_TIMEOUT, home=home, now=NOW)
    link_health.record(link_health.KIND_SEND_FAILED, home=home, now=NOW)

    day = link_health.summary(24, home=home, now=NOW)
    assert day["total"] == 4
    assert day["by_kind"] == {
        link_health.KIND_POOL_TIMEOUT: 3,
        link_health.KIND_SEND_FAILED: 1,
    }


def test_the_count_survives_a_restart(home):
    """Счёт живёт в файле, а не в памяти процесса — иначе перезапуск шлюза
    (самый частый спутник разрыва) стирал бы ровно то, что интересно."""
    link_health.record(link_health.KIND_INIT_RETRY, home=home, now=NOW)
    assert json.loads(link_health.state_path(home).read_text(encoding="utf-8"))
    assert link_health.summary(24, home=home, now=NOW)["total"] == 1


def test_older_events_leave_the_daily_window(home):
    link_health.record(
        link_health.KIND_SEND_RETRY, home=home, now=NOW - timedelta(hours=30)
    )
    link_health.record(link_health.KIND_SEND_RETRY, home=home, now=NOW)

    assert link_health.summary(24, home=home, now=NOW)["total"] == 1
    assert link_health.summary(24 * 7, home=home, now=NOW)["total"] == 2


def test_the_file_does_not_grow_with_the_storm(home):
    """Размер файла определяется сроком хранения, а не числом событий —
    шторм разрывов не имеет права заодно засорить диск."""
    for hour in range(24 * (link_health.RETENTION_DAYS + 3)):
        for _ in range(5):
            link_health.record(
                link_health.KIND_POOL_TIMEOUT,
                home=home,
                now=NOW + timedelta(hours=hour),
            )
    buckets = json.loads(link_health.state_path(home).read_text(encoding="utf-8"))["buckets"]
    assert len(buckets) <= 24 * link_health.RETENTION_DAYS + 1


def test_unknown_kind_is_refused_rather_than_stored(home):
    assert link_health.record("сеть барахлит", home=home, now=NOW) is False
    assert link_health.summary(24, home=home, now=NOW)["total"] == 0


def test_an_unwritable_home_is_reported_not_raised(home, monkeypatch):
    """Телеметрия разрывов не имеет права сорвать отправку сообщения."""
    monkeypatch.setattr(
        link_health, "state_path", lambda *a, **k: home / "нет" / "\x00" / "f.json"
    )
    assert link_health.record(link_health.KIND_SEND_RETRY, home=home, now=NOW) is False


def test_corrupt_state_file_starts_over_instead_of_failing(home):
    path = link_health.state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{это не json", encoding="utf-8")

    assert link_health.summary(24, home=home, now=NOW)["total"] == 0
    assert link_health.record(link_health.KIND_SEND_RETRY, home=home, now=NOW) is True
    assert link_health.summary(24, home=home, now=NOW)["total"] == 1


def test_state_path_follows_the_active_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "клиент"))
    assert link_health.state_path().parent.parent == tmp_path / "profiles" / "клиент"


# --------------------------------------------------------------------------
# Проводка: настоящий путь отправки, а не вызов счётчика напрямую
# --------------------------------------------------------------------------

def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (mod.error.NetworkError,), {})
    mod.error.BadRequest = type("BadRequest", (mod.error.NetworkError,), {})
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402

_POOL_TIMEOUT_TEXT = (
    "Pool timeout: All connections in the connection pool are occupied. "
    "Request was *not* sent to Telegram. Consider adjusting the connection "
    "pool size or the pool timeout."
)


def _adapter_that_always_times_out(text):
    from telegram.error import TimedOut

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(side_effect=TimedOut(text))
    return adapter


@pytest.mark.asyncio
async def test_a_real_pool_timeout_send_is_counted(tmp_path, monkeypatch):
    """Отправка, пережившая исчерпание пула, оставляет след в счётчике.

    Проверяется исполнением настоящего цикла повторов в ``send()``, а не
    вызовом ``record()`` руками: смысл счётчика ровно в том, что он стоит
    на живом пути, а не рядом с ним.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter_that_always_times_out(_POOL_TIMEOUT_TEXT)

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await adapter.send("123", "привет")

    assert result.success is False
    day = link_health.summary(24)
    assert day["by_kind"].get(link_health.KIND_POOL_TIMEOUT) == 3
    assert day["by_kind"].get(link_health.KIND_SEND_RETRY) == 2
    assert day["by_kind"].get(link_health.KIND_SEND_FAILED) == 1


@pytest.mark.asyncio
async def test_a_successful_send_leaves_no_trace(tmp_path, monkeypatch):
    """Счётчик считает разрывы, а не сообщения — иначе «14 раз за сутки»
    ничего не значит."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    adapter.send_typing = AsyncMock()

    result = await adapter.send("123", "привет")

    assert result.success is True
    assert link_health.summary(24)["total"] == 0


@pytest.mark.asyncio
async def test_a_broken_counter_cannot_break_a_send(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    adapter.send_typing = AsyncMock()
    monkeypatch.setattr(
        link_health, "record", MagicMock(side_effect=RuntimeError("диск кончился"))
    )

    adapter._note_link_incident(link_health.KIND_SEND_RETRY)  # не должно бросить
    assert (await adapter.send("123", "привет")).success is True
