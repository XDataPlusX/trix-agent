"""Tests for GatewayRunner._maybe_restart_after_mcp_connect (spec 20, Ruling 3).

Mirrors tests/gateway/test_secret_capture_reply_intercept.py's
"response before restart" coverage, but for the post-delivery-callback seam
this hook uses instead of spec 19's synchronous send-then-restart snippet —
see tools/mcp_connect_gateway.py's module docstring for why a tool call
mid-turn can't use that snippet directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_source(chat_id: str = "c1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id=chat_id,
        user_name="tester",
        chat_type="dm",
    )


def _make_runner(session_key: str = "session-mcp") -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._session_key_for_source = MagicMock(return_value=session_key)
    return runner


def _clear_pending_restart():
    from tools import mcp_connect_gateway as mcg

    with mcg._lock:
        mcg._pending_restart.clear()


@pytest.fixture(autouse=True)
def _clean_state():
    _clear_pending_restart()
    yield
    _clear_pending_restart()


@pytest.mark.asyncio
async def test_restart_is_deferred_until_after_delivery():
    """The restart must not fire synchronously — it has to be registered as
    a post-delivery callback and only run once the adapter invokes it (i.e.
    after the turn's own response has actually gone out).
    """
    from tools import mcp_connect_gateway as mcg

    session_key = "session-mcp-defer"
    mcg.mark_pending_restart(session_key)

    runner = _make_runner(session_key=session_key)
    adapter = MagicMock()
    adapter._active_sessions = {}
    captured = {}

    def _capture(sk, callback, *, generation=None):
        captured["session_key"] = sk
        captured["callback"] = callback

    adapter.register_post_delivery_callback = MagicMock(side_effect=_capture)
    runner._adapter_for_source = MagicMock(return_value=adapter)

    with patch("hermes_cli.setup_wizard.gateway_ctl.restart_gateway") as restart_mock, \
         patch("threading.Thread") as thread_cls:
        def _inline_thread(target, **kwargs):
            thread = MagicMock()
            thread.start.side_effect = target
            return thread

        thread_cls.side_effect = _inline_thread

        await runner._maybe_restart_after_mcp_connect(
            _make_source(), "Сервер «bitrix» подключён, перезапускаю шлюз.",
        )

        # Not restarted yet — only registered for after delivery.
        restart_mock.assert_not_called()
        assert captured["session_key"] == session_key

        # Simulate the adapter delivering the response and firing the
        # post-delivery callback.
        await captured["callback"]()

    restart_mock.assert_called_once()


@pytest.mark.asyncio
async def test_no_delivery_seam_defers_instead_of_restarting_now():
    """Без шва доставки перезапускаться НЕЛЬЗЯ.

    Этот метод зовётся из _handle_message ДО того, как адаптер отправит
    ответ модели. Синхронный рестарт здесь съел бы последнее сообщение
    хода — ровно то, что Ruling 3 запрещает. Правильное поведение: вернуть
    флаг на место и дать следующему ходу, у которого шов есть, применить
    его. Запись сервера уже на диске, поэтому ничего не теряется.
    """
    from tools import mcp_connect_gateway as mcg

    session_key = "session-mcp-noadapter"
    mcg.mark_pending_restart(session_key)

    runner = _make_runner(session_key=session_key)
    adapter = MagicMock(spec=[])  # без register_post_delivery_callback
    runner._adapter_for_source = MagicMock(return_value=adapter)

    with patch("hermes_cli.setup_wizard.gateway_ctl.restart_gateway") as restart_mock, \
         patch("threading.Thread") as thread_cls:
        def _inline_thread(target, **kwargs):
            thread = MagicMock()
            thread.start.side_effect = target
            return thread

        thread_cls.side_effect = _inline_thread

        await runner._maybe_restart_after_mcp_connect(_make_source(), "Готово.")

    restart_mock.assert_not_called()
    assert mcg.pop_pending_restart(session_key) is True


@pytest.mark.asyncio
async def test_client_is_told_before_the_restart():
    """Клиент не должен уходить в минуту немоты без объяснения.

    Ответ на захват ключа (спека 19) для не-провайдерского ключа говорит
    «перезапуск не нужен» — про сам ключ это правда. Сервер же применяется
    именно перезапуском, поэтому сообщение о нём обязано уйти ДО того, как
    шлюз начнёт перезапускаться.
    """
    from tools import mcp_connect_gateway as mcg

    session_key = "session-mcp-notice"
    mcg.mark_pending_restart(session_key)

    runner = _make_runner(session_key=session_key)
    runner._thread_metadata_for_source = MagicMock(return_value=None)
    order = []

    adapter = MagicMock()
    adapter._active_sessions = {}
    captured = {}

    async def _send(chat_id, message, metadata=None):
        order.append(("send", message))
        return MagicMock(success=True)

    adapter.send = _send

    def _capture(sk, callback, *, generation=None):
        captured["callback"] = callback

    adapter.register_post_delivery_callback = MagicMock(side_effect=_capture)
    runner._adapter_for_source = MagicMock(return_value=adapter)

    with patch("hermes_cli.setup_wizard.gateway_ctl.restart_gateway") as restart_mock, \
         patch("threading.Thread") as thread_cls:
        def _inline_thread(target, **kwargs):
            thread = MagicMock()
            thread.start.side_effect = lambda: order.append(("restart", None)) or target()
            return thread

        thread_cls.side_effect = _inline_thread
        restart_mock.side_effect = lambda *a, **k: None

        await runner._maybe_restart_after_mcp_connect(_make_source(), "Готово.")
        await captured["callback"]()

    kinds = [kind for kind, _ in order]
    assert "send" in kinds, "клиенту ничего не отправили перед перезапуском"
    assert kinds.index("send") < kinds.index("restart")

    # Сверяем с каталогом, а не с текстом: прогон идёт на английской локали,
    # а на машине клиента стоит русская. Замораживать формулировку здесь
    # значит ловить правку копирайта вместо поведения.
    from agent.i18n import t as _t

    sent_text = next(text for kind, text in order if kind == "send")
    assert sent_text == _t("trix.mcp.restarting")


def test_both_catalogs_carry_the_restart_notice():
    """Ключ обязан быть в обоих каталогах.

    Умолчание в вызове написано по-русски, поэтому пропущенный ключ в
    en.yaml не проявился бы ни в одном другом тесте: клиент на английской
    локали получил бы русскую строку. Проверяем сам каталог, без умолчания.
    """
    from agent.i18n import t as _t

    for lang in ("ru", "en"):
        text = _t("trix.mcp.restarting", lang=lang)
        assert text != "trix.mcp.restarting", f"нет ключа в каталоге {lang}"
        assert text.strip()


@pytest.mark.asyncio
async def test_nothing_pending_never_touches_the_adapter():
    runner = _make_runner(session_key="session-mcp-nothing-pending")
    runner._adapter_for_source = MagicMock()

    with patch("hermes_cli.setup_wizard.gateway_ctl.restart_gateway") as restart_mock:
        await runner._maybe_restart_after_mcp_connect(_make_source(), "just a normal reply")

    restart_mock.assert_not_called()
    runner._adapter_for_source.assert_not_called()


@pytest.mark.asyncio
async def test_empty_final_response_leaves_the_flag_for_a_later_turn():
    """An interrupted/errored turn must not consume the flag it can't act
    on — the pending-restart marker survives so a later successful turn on
    the same session still applies it.
    """
    from tools import mcp_connect_gateway as mcg

    session_key = "session-mcp-empty-response"
    mcg.mark_pending_restart(session_key)

    runner = _make_runner(session_key=session_key)
    runner._adapter_for_source = MagicMock()

    with patch("hermes_cli.setup_wizard.gateway_ctl.restart_gateway") as restart_mock:
        await runner._maybe_restart_after_mcp_connect(_make_source(), "")

    restart_mock.assert_not_called()
    runner._adapter_for_source.assert_not_called()
    assert mcg.pop_pending_restart(session_key) is True
