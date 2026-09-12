"""Six client-reachable brand leaks found beyond the original de-brand spec
(Task 9d found 12 candidates beyond the spec's command-registry search; this
covers the five assigned plus one more found the same way while sweeping
for a sixth, each verified reachable):

1. ``agent.estop.paused_reply`` -- reached on every gateway turn while a
   global pause is engaged (``gateway/run.py:_handle_message``).
2. ``hermes_cli.active_sessions.active_session_limit_message`` -- reached
   when the gateway is at ``max_concurrent_sessions``.
3. ``hermes_cli.model_cost_guard.expensive_model_warning`` -- embedded in
   the ``/model`` expensive-model confirmation dialog.
4. ``hermes_cli.config.format_managed_message`` -- reached through
   ``/update`` on a managed install (``gateway/slash_commands.py``).
5. The Telegram handoff thread name built in
   ``gateway.run.GatewayRunner._process_handoff``.
6. The Discord auto-thread fallback title in
   ``gateway.run.GatewayRunner._sanitize_discord_thread_title`` -- used
   whenever a session has no title yet, found via a plain-text sweep for
   "Hermes"/"Nous" literals reachable by the client.

The original spec's grep only covered command *descriptions* in the
registry; these six leak through reply *bodies*, which is why they
survived that pass. Each assertion below is a behavior contract (render the
real code path, inspect the real string), not a source-text scan -- except
where noted, these exercise the function that produces the customer-visible
string, not a copy of it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent import i18n


def _has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


def _assert_debranded_and_russian(text: str, *, label: str) -> None:
    assert "Hermes" not in text, f"{label}: still mentions Hermes: {text!r}"
    assert "Nous" not in text, f"{label}: still mentions Nous: {text!r}"
    assert _has_cyrillic(text), f"{label}: no Cyrillic in the Russian render: {text!r}"


@pytest.fixture
def russian(monkeypatch):
    """Pin the resolved language to Russian for the duration of the test.

    ``tests/conftest.py`` pins ``HERMES_LANGUAGE=en`` for the whole suite
    (upstream tests assert English copy), so every test here that wants the
    customer-facing render has to opt back into Russian explicitly, per the
    pattern in ``tests/hermes_cli/test_trix_menu.py``.
    """
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        yield
    finally:
        i18n.reset_language_cache()


# ---------------------------------------------------------------------------
# 1. agent.estop.paused_reply
# ---------------------------------------------------------------------------

class TestEstopPausedReply:
    def test_with_reason_is_debranded_and_russian(self, russian, tmp_path, monkeypatch):
        from agent import estop

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        estop._reset_log_state_for_tests()
        estop.engage(reason="deploy window")
        try:
            notice = estop.paused_reply()
            assert notice is not None
            _assert_debranded_and_russian(notice, label="estop.paused_reply(reason)")
            # The client cannot run a CLI command -- the old copy told them to
            # run `hermes resume`.
            assert "hermes resume" not in notice.lower()
        finally:
            estop.disengage()

    def test_without_reason_is_debranded_and_russian(self, russian, tmp_path, monkeypatch):
        from agent import estop

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        estop._reset_log_state_for_tests()
        estop.engage()
        try:
            notice = estop.paused_reply()
            assert notice is not None
            _assert_debranded_and_russian(notice, label="estop.paused_reply()")
            assert "hermes resume" not in notice.lower()
        finally:
            estop.disengage()


# ---------------------------------------------------------------------------
# 2. hermes_cli.active_sessions.active_session_limit_message
# ---------------------------------------------------------------------------

class TestActiveSessionLimitMessage:
    def test_is_debranded_and_russian(self, russian):
        from hermes_cli.active_sessions import active_session_limit_message

        text = active_session_limit_message(3, 3, entries=[{"surface": "telegram"}])
        _assert_debranded_and_russian(text, label="active_session_limit_message")

    def test_without_holders_still_debranded(self, russian):
        from hermes_cli.active_sessions import active_session_limit_message

        text = active_session_limit_message(1, 1, entries=None)
        _assert_debranded_and_russian(text, label="active_session_limit_message (no holders)")


# ---------------------------------------------------------------------------
# 3. hermes_cli.model_cost_guard.expensive_model_warning
# ---------------------------------------------------------------------------

class TestExpensiveModelWarning:
    def test_above_threshold_line_is_debranded_and_russian(self, russian):
        from hermes_cli.model_cost_guard import expensive_model_warning
        from agent.models_dev import ModelInfo

        info = ModelInfo(
            id="pricey/model",
            name="Pricey Model",
            family="pricey",
            provider_id="pricey",
            cost_input=50.0,
            cost_output=200.0,
        )
        warning = expensive_model_warning("pricey/model", provider="pricey", model_info=info)
        assert warning is not None
        _assert_debranded_and_russian(warning.message, label="expensive_model_warning.message")


# ---------------------------------------------------------------------------
# 4. hermes_cli.config.format_managed_message
# ---------------------------------------------------------------------------

class TestFormatManagedMessage:
    def test_generic_managed_system_is_debranded_and_russian(self, russian):
        from hermes_cli.config import format_managed_message

        with patch("hermes_cli.config.get_managed_system", return_value="Homebrew"):
            text = format_managed_message("update Trix Agent")
        _assert_debranded_and_russian(text, label="format_managed_message (generic)")

    def test_nixos_managed_system_is_debranded_and_russian(self, russian):
        from hermes_cli.config import format_managed_message

        with patch("hermes_cli.config.get_managed_system", return_value="NixOS"):
            text = format_managed_message("update Trix Agent")
        _assert_debranded_and_russian(text, label="format_managed_message (NixOS)")
        # The client reading this via /update cannot edit a Nix module or run
        # a shell command -- the old copy told them to do both.
        assert "nixos-rebuild" not in text
        assert "configuration.nix" not in text


# ---------------------------------------------------------------------------
# 5. Telegram handoff thread name (gateway.run.GatewayRunner._process_handoff)
# ---------------------------------------------------------------------------

class _StopAfterThreadName(Exception):
    """Raised by the ``t()`` spy once it captures the thread name, so the
    test doesn't have to stand up the rest of the handoff pipeline (session
    store, synthetic message delivery, ...) to reach the one line under
    test."""


class TestHandoffThreadName:
    def test_thread_name_is_debranded_and_russian(self, russian, monkeypatch):
        from gateway.run import GatewayRunner

        real_t = i18n.t
        captured: dict[str, str] = {}

        def spy_t(key, *args, **kwargs):
            if key == "trix.handoff.thread_name":
                captured["value"] = real_t(key, *args, **kwargs)
                raise _StopAfterThreadName()
            return real_t(key, *args, **kwargs)

        monkeypatch.setattr("gateway.run.t", spy_t)

        runner = object.__new__(GatewayRunner)
        runner.config = MagicMock()
        runner.config.get_home_channel.return_value = MagicMock(chat_id="123", thread_id=None)
        runner.adapters = {}

        fake_transport = MagicMock(adapter=MagicMock())
        with patch("gateway.run.resolve_delivery_transport", return_value=fake_transport):
            with pytest.raises(_StopAfterThreadName):
                asyncio.run(
                    runner._process_handoff(
                        {"id": "abcdef1234", "handoff_platform": "telegram", "title": "My Session"}
                    )
                )

        thread_name = captured.get("value")
        assert thread_name, "the t() spy never observed trix.handoff.thread_name"
        assert "My Session" in thread_name
        _assert_debranded_and_russian(thread_name, label="handoff thread_name")


# ---------------------------------------------------------------------------
# 6. Discord auto-thread fallback title (found via the plain-text sweep)
# ---------------------------------------------------------------------------

class TestDiscordDefaultThreadTitle:
    def test_fallback_title_is_debranded_and_russian(self, russian):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        title = GatewayRunner._sanitize_discord_thread_title(runner, "   ")
        _assert_debranded_and_russian(title, label="discord default thread title")

    def test_non_empty_title_is_untouched(self, russian):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        title = GatewayRunner._sanitize_discord_thread_title(runner, "My Session")
        assert title == "My Session"
