"""`/debug` отдаёт отчёт файлом, а текст остаётся запасным путём.

Почему это существенно, а не косметика: настоящий отчёт — это дамп плюс
200 строк `agent.log` плюс четыре других журнала, десятки тысяч символов.
В сообщение он не влезает никогда, и до правки 2026-09-07 клиент в
Телеграме получал только «отчёт сохранён на сервере, попросите того, кто
администрирует эту машину» — а администрирует её он сам, и шелла у него
нет. Команда была тупиком. Файл — то, что клиент может переслать в
поддержку.

Тесты держат обе половины решения: файл уходит, когда адаптер это умеет,
и текстовый ответ возвращается ровно там, где не умеет или не смог.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent
from gateway.session import SessionSource


def _make_event() -> MessageEvent:
    return MessageEvent(
        text="/debug",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="user-1",
        ),
        message_id="m1",
    )


class _FileCapableAdapter:
    """Адаптер, который умеет слать файлы (как Telegram)."""

    def __init__(self, success: bool = True, raises: bool = False):
        self.success = success
        self.raises = raises
        self.calls: list[dict] = []

    async def send_document(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise RuntimeError("upload failed")
        return SimpleNamespace(success=self.success, error=None)


class _TextOnlyAdapter(BasePlatformAdapter):
    """Адаптер без своего send_document — берёт базовую заглушку.

    Ради неё и нужна проверка «переопределён ли метод»: базовая версия
    отправляет английское «не удалось приложить файл» и возвращает
    success=True, то есть выглядела бы как удачная доставка.
    """

    def __init__(self):  # не зовём BasePlatformAdapter.__init__ — он тяжёлый
        self.sent: list[str] = []

    # Абстрактный минимум базового класса; send_document намеренно НЕ
    # переопределён — именно его наследование мы и проверяем.
    async def connect(self):  # pragma: no cover - не вызывается
        return True

    async def disconnect(self):  # pragma: no cover - не вызывается
        return None

    async def get_chat_info(self, chat_id):  # pragma: no cover - не вызывается
        return {}

    async def send(self, chat_id, content, **kwargs):
        self.sent.append(content)
        return SimpleNamespace(success=True, error=None)


def _make_runner(adapter, *, metadata_raises: bool = False):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._adapter_for_source = lambda source: adapter
    if metadata_raises:
        def _boom(source, anchor):
            raise RuntimeError("no thread metadata here")
        runner._thread_metadata_for_source = _boom
    else:
        runner._thread_metadata_for_source = lambda source, anchor: {"thread": "t1"}
    runner._reply_anchor_for_event = lambda event: None
    runner._running_agents = {}
    runner._agent_cache_lock = threading.Lock()
    return runner


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "agent.log").write_text(
        "2026-09-07 10:00:00,000 INFO agent: gateway started\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.mark.asyncio
async def test_report_is_attached_as_a_file(isolated_home):
    adapter = _FileCapableAdapter()
    runner = _make_runner(adapter)

    reply = await runner._handle_debug_command(_make_event())

    # Файл ушёл — отвечать текстом больше нечем.
    assert reply is None
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["chat_id"] == "12345"
    assert call["file_name"].startswith("debug-report-")
    assert call["caption"], "у вложения должна быть подпись, иначе файл приходит без объяснения"

    # Отправляется именно тот файл, который лежит на диске.
    sent = isolated_home / "debug-reports" / call["file_name"]
    assert sent.exists()
    assert str(sent) == call["file_path"]
    assert "--- agent.log" in sent.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_thread_metadata_failure_does_not_block_delivery(isolated_home):
    """Метаданные ветки — удобство; без них файл всё равно должен уйти."""
    adapter = _FileCapableAdapter()
    runner = _make_runner(adapter, metadata_raises=True)

    assert await runner._handle_debug_command(_make_event()) is None
    assert adapter.calls[0]["metadata"] is None


@pytest.mark.asyncio
async def test_falls_back_to_text_when_the_adapter_cannot_send_files(isolated_home):
    runner = _make_runner(_TextOnlyAdapter())

    reply = await runner._handle_debug_command(_make_event())

    assert isinstance(reply, str) and reply
    # Отчёт всё равно сохранён — клиенту называют путь.
    saved = list((isolated_home / "debug-reports").glob("debug-report-*.txt"))
    assert len(saved) == 1
    assert saved[0].name in reply or str(saved[0]) in reply


@pytest.mark.asyncio
async def test_falls_back_to_text_when_the_upload_raises(isolated_home):
    adapter = _FileCapableAdapter(raises=True)
    runner = _make_runner(adapter)

    reply = await runner._handle_debug_command(_make_event())

    assert isinstance(reply, str) and reply


@pytest.mark.asyncio
async def test_falls_back_to_text_when_the_adapter_reports_failure(isolated_home):
    adapter = _FileCapableAdapter(success=False)
    runner = _make_runner(adapter)

    reply = await runner._handle_debug_command(_make_event())

    assert isinstance(reply, str) and reply
