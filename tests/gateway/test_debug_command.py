"""Tests for the gateway /debug command.

Trix Agent never uploads client logs off the customer's VM (see
docs/product/specs/2026-08-13-trix-agent-persona-design.md). The gateway's
``/debug`` handler used to upload a summary report to a public paste
service and hand back the URL; it now only ever writes the report to a
local file and either inlines it in the chat reply (if short enough) or
tells the customer plainly it was too long to inline and points at the
saved file instead.

The inline reply must not claim the report "stays on this VM" — the reply
itself is a chat message that travels over the messaging platform
(Telegram/Discord/Slack/...), so any claim of that shape would be false.
It's fine to say the report was never uploaded to a third-party service.
"""

import stat
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/debug", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890"):
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    return runner


def _boom(*a, **k):
    raise AssertionError("network upload attempted")


class TestHandleDebugCommand:
    @pytest.mark.asyncio
    async def test_debug_sweeps_expired_pastes_but_never_uploads(
        self, tmp_path, monkeypatch
    ):
        """/debug still does a best-effort sweep of legacy pending pastes
        (cleanup of pastes a customer may have uploaded on an older build),
        but the report itself is never uploaded anywhere — it's written to
        a local file and returned inline in the chat reply instead."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        runner = _make_runner()
        event = _make_event()

        with patch(
            "hermes_cli.debug._sweep_expired_pastes", return_value=(0, 0)
        ) as mock_sweep, \
             patch("hermes_cli.debug._capture_dump", return_value="dump"), \
             patch(
                 "hermes_cli.debug.collect_debug_report", return_value="report"
             ), \
             patch("hermes_cli.debug.upload_to_pastebin", _boom), \
             patch("hermes_cli.debug._schedule_auto_delete", _boom):
            result = await runner._handle_debug_command(event)

        mock_sweep.assert_called_once()
        assert "Not uploaded to any third-party service" in result
        # The reply is a chat message that itself travels over the
        # platform — it must say so, not claim everything "stays on this
        # VM" (that claim would be false for the reply that makes it).
        assert "telegram" in result.lower()
        assert "stays on this vm" not in result.lower()
        assert "report" in result  # the report content is inlined verbatim

        saved = list((tmp_path / "debug-reports").glob("*.txt"))
        assert len(saved) == 1
        assert saved[0].read_text(encoding="utf-8") == "report"
        # Plaintext conversation content — owner read/write only.
        mode = stat.S_IMODE(saved[0].stat().st_mode)
        assert mode == 0o600

    @pytest.mark.asyncio
    async def test_long_report_is_not_inlined_but_saved_locally(
        self, tmp_path, monkeypatch
    ):
        """A report too long to fit the chat platform's message limit is
        never silently truncated — the customer is told it was too long
        and pointed at the local file instead."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        runner = _make_runner()
        event = _make_event()

        long_report = "x" * 5000  # over the 3500-char inline limit

        with patch("hermes_cli.debug._sweep_expired_pastes", return_value=(0, 0)), \
             patch("hermes_cli.debug._capture_dump", return_value="dump"), \
             patch(
                 "hermes_cli.debug.collect_debug_report", return_value=long_report
             ), \
             patch("hermes_cli.debug.upload_to_pastebin", _boom), \
             patch("hermes_cli.debug._schedule_auto_delete", _boom):
            result = await runner._handle_debug_command(event)

        assert long_report not in result
        assert "too long" in result
        assert "debug-reports" in result

        saved = list((tmp_path / "debug-reports").glob("*.txt"))
        assert len(saved) == 1
        assert saved[0].read_text(encoding="utf-8") == long_report
        assert stat.S_IMODE(saved[0].stat().st_mode) == 0o600
