"""Regression tests for remaining unredacted Telegram transport-error sites.

``c3ab1424e`` added ``_redact_telegram_error_text()`` (built on
``agent.redact``'s bot-token stripping for
``api.telegram.org/bot<TOKEN>/...`` URLs) and applied it across the
send/edit transient-error paths. Four sites still built their message from
the raw exception:

- ``connect()``'s fatal-error path — the most severe: the raw text is
  passed to ``_set_fatal_error()``, which *persists* it via
  ``write_runtime_status()`` to a dashboard/admin-facing runtime status
  file, not just a log line. A transient network error during startup
  commonly embeds the request URL (``https://api.telegram.org/bot<TOKEN>/
  getMe``), so this could leak the live bot token into that surface.
- ``disconnect()``, ``send_document()``, ``send_video()`` — log-only,
  lower blast radius, but the same unredacted-exception pattern.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from plugins.platforms.telegram.adapter import (
    TelegramAdapter,
    _log_polling_error,
    _log_polling_network_error,
    _redact_proxy_url_for_log,
)

_SECRET_TOKEN = "123456789:AAFakeSecretTelegramBotTokenABCDEFGHIJ"
_SECRET_URL = f"https://api.telegram.org/bot{_SECRET_TOKEN}/getMe"


def _make_bare_adapter() -> TelegramAdapter:
    config = PlatformConfig(enabled=True, token=_SECRET_TOKEN, extra={})
    return TelegramAdapter(config)


def _make_connected_adapter() -> TelegramAdapter:
    """Adapter with a mock bot wired, past connect() — for send_* tests."""
    adapter = _make_bare_adapter()
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    adapter._bot = bot
    return adapter


@pytest.mark.asyncio
async def test_connect_failure_redacts_token_from_fatal_status(monkeypatch):
    """A connect()-time exception embedding the bot token URL must not reach
    the persisted fatal-error status or the log line unredacted."""
    adapter = _make_bare_adapter()

    def _boom(*_args, **_kwargs):
        raise RuntimeError(f"Network error connecting to {_SECRET_URL}")

    monkeypatch.setattr(adapter, "_acquire_platform_lock", _boom)
    monkeypatch.setattr(adapter, "_write_runtime_status_safe", lambda *a, **k: None)

    result = await adapter.connect()

    assert result is False
    assert adapter._fatal_error_message is not None
    assert _SECRET_TOKEN not in adapter._fatal_error_message
    assert "***" in adapter._fatal_error_message


@pytest.mark.asyncio
async def test_disconnect_failure_redacts_token_in_log(monkeypatch, caplog):
    """A disconnect()-time exception embedding the bot token URL must not
    reach the warning log unredacted."""
    adapter = _make_connected_adapter()
    adapter._app = SimpleNamespace(
        updater=SimpleNamespace(running=False),
        running=False,
        shutdown=AsyncMock(side_effect=RuntimeError(f"teardown failed: {_SECRET_URL}")),
    )
    adapter._release_platform_lock = lambda: None
    adapter._cancel_pending_delivery_tasks = AsyncMock()

    with caplog.at_level("WARNING"):
        await adapter.disconnect()

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert _SECRET_TOKEN not in logged
    assert "Error during Telegram disconnect" in logged


@pytest.mark.asyncio
async def test_send_document_failure_redacts_token_in_log(monkeypatch, caplog, tmp_path):
    """A send_document() transport exception embedding the bot token URL
    must not reach the warning log unredacted."""
    adapter = _make_connected_adapter()
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(
        adapter,
        "_send_with_dm_topic_reply_anchor_retry",
        AsyncMock(side_effect=RuntimeError(f"upload failed: {_SECRET_URL}")),
    )
    fallback = AsyncMock(return_value=SimpleNamespace(success=False, error="fallback"))
    monkeypatch.setattr(BasePlatformAdapter, "send_document", fallback)

    with caplog.at_level("WARNING"):
        await adapter.send_document("123", str(file_path))

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert _SECRET_TOKEN not in logged
    assert "Failed to send document" in logged


@pytest.mark.asyncio
async def test_send_video_failure_redacts_token_in_log(monkeypatch, caplog, tmp_path):
    """A send_video() transport exception embedding the bot token URL must
    not reach the warning log unredacted."""
    adapter = _make_connected_adapter()
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fake mp4 bytes")

    monkeypatch.setattr(
        adapter,
        "_send_with_dm_topic_reply_anchor_retry",
        AsyncMock(side_effect=RuntimeError(f"upload failed: {_SECRET_URL}")),
    )
    fallback = AsyncMock(return_value=SimpleNamespace(success=False, error="fallback"))
    monkeypatch.setattr(BasePlatformAdapter, "send_video", fallback)

    with caplog.at_level("WARNING"):
        await adapter.send_video("123", str(video_path))

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert _SECRET_TOKEN not in logged
    assert "Failed to send video" in logged


_SECRET_SEND_URL = f"https://api.telegram.org/bot{_SECRET_TOKEN}/sendMessage"


@pytest.mark.asyncio
async def test_send_update_prompt_failure_redacts_token_in_result_and_log(caplog):
    """A send_update_prompt() transport exception embedding the bot token URL
    must not reach the warning log or SendResult.error unredacted."""
    adapter = _make_connected_adapter()
    adapter._send_message_with_thread_fallback = AsyncMock(
        side_effect=RuntimeError(f"Timed out requesting {_SECRET_SEND_URL}")
    )

    with caplog.at_level("WARNING"):
        result = await adapter.send_update_prompt("123", "restart?")

    assert result.success is False
    assert _SECRET_TOKEN not in (result.error or "")
    assert "***" in (result.error or "")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert _SECRET_TOKEN not in logged


@pytest.mark.asyncio
async def test_send_clarify_failure_redacts_token_in_result_and_log(caplog):
    """A send_clarify() transport exception embedding the bot token URL must
    not reach the warning log or SendResult.error unredacted."""
    adapter = _make_connected_adapter()
    adapter._send_message_with_thread_fallback = AsyncMock(
        side_effect=RuntimeError(f"Timed out requesting {_SECRET_SEND_URL}")
    )

    with caplog.at_level("WARNING"):
        result = await adapter.send_clarify("123", "q?", ["a", "b"], "cid", "sess")

    assert result.success is False
    assert _SECRET_TOKEN not in (result.error or "")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert _SECRET_TOKEN not in logged


@pytest.mark.asyncio
async def test_delete_message_failure_redacts_token_in_log(caplog):
    """A delete_message() transport exception embedding the bot token URL
    must not reach the debug log unredacted."""
    adapter = _make_connected_adapter()
    adapter._bot.delete_message = AsyncMock(
        side_effect=RuntimeError(f"Bad Request: {_SECRET_SEND_URL}")
    )

    with caplog.at_level("DEBUG"):
        ok = await adapter.delete_message("123", "55")

    assert ok is False
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert _SECRET_TOKEN not in logged
    assert "***" in logged


# ---------------------------------------------------------------------------
# Polling error_callback log helpers + proxy URL masking
# (regression: a04fcbf779 pasted the redactor call into the format string;
# the 2026-09-12 debug report also showed the resolved proxy URL —
# credentials included — written verbatim into gateway.log / agent.log)
# ---------------------------------------------------------------------------

# Synthetic throughout. The regression this guards came from a real proxy URL
# reaching the logs, but the fixture must never BE that URL: a test that pins a
# live credential re-leaks it into git history, which is the same defect one
# layer down. Host is RFC 5737 TEST-NET-1 (192.0.2.0/24), reserved for docs.
_SECRET_PROXY_PASSWORD = "n0t-a-real-password"
_SECRET_PROXY_URL = f"http://proxyuser:{_SECRET_PROXY_PASSWORD}@192.0.2.10:42567"


def test_redact_proxy_url_for_log_masks_userinfo_password():
    """The connect()-time proxy log must keep scheme/user/host/port but mask
    the password — for every proxy scheme Hermes resolves (http, socks5)."""
    masked = _redact_proxy_url_for_log(_SECRET_PROXY_URL)
    assert _SECRET_PROXY_PASSWORD not in masked
    assert masked == "http://proxyuser:***@192.0.2.10:42567"


def test_redact_proxy_url_for_log_masks_bare_token_userinfo():
    """A bare-token proxy (``scheme://token@host``) must mask the token too."""
    masked = _redact_proxy_url_for_log("socks5://secrettoken@10.0.0.1:1080")
    assert "secrettoken" not in masked
    assert masked == "socks5://***@10.0.0.1:1080"


def test_redact_proxy_url_for_log_leaves_credential_free_urls_alone():
    """A proxy URL without userinfo must pass through unchanged (host and
    port are exactly what connectivity debugging needs)."""
    plain = "http://192.0.2.10:42567"
    assert _redact_proxy_url_for_log(plain) == plain
    assert _redact_proxy_url_for_log("") == ""
    assert _redact_proxy_url_for_log(None) == ""


def test_log_polling_network_error_redacts_token_and_proxy(caplog):
    """The polling network-error log line must redact embedded secrets AND
    must not contain the literal redactor call text (the a04fcbf779
    regression: 'Telegram network _redact_telegram_error_text(error),
    scheduling reconnect: <raw error>')."""
    err = RuntimeError(f"connect failed via {_SECRET_PROXY_URL} to {_SECRET_URL}")
    with caplog.at_level("WARNING", logger="hermes_plugins.telegram_platform.adapter"):
        _log_polling_network_error("Telegram", err)

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert _SECRET_TOKEN not in message
    assert _SECRET_PROXY_PASSWORD not in message
    assert "_redact_telegram_error_text" not in message
    assert "Telegram network error, scheduling reconnect" in message
    assert "***" in message


def test_log_polling_error_redacts_token_and_proxy(caplog):
    """The generic polling-error log line must redact embedded secrets and
    keep its exception traceback attached (exc_info)."""
    err = RuntimeError(f"polling broke: {_SECRET_PROXY_URL} {_SECRET_SEND_URL}")
    with caplog.at_level("ERROR", logger="hermes_plugins.telegram_platform.adapter"):
        _log_polling_error("Telegram", err)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    message = record.getMessage()
    assert _SECRET_TOKEN not in message
    assert _SECRET_PROXY_PASSWORD not in message
    assert "_redact_telegram_error_text" not in message
    assert "Telegram polling error" in message
    assert record.exc_info is not None
