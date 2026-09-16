"""Tests for the gateway-side secret-capture primitive (spec 19).

Covers the module-level state machine (session isolation, wait/resolve,
timeout, session cleanup) and — the load-bearing property of the whole
feature — that the raw secret value never appears in any object this module
hands back to a caller.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch


def _clear_state():
    from tools import secret_capture_gateway as scg
    with scg._lock:
        scg._entries.clear()
        scg._session_index.clear()
        scg._notify_cbs.clear()


class TestSessionIsolation:
    """(a) A capture registered in session A must not resolve from session B."""

    def setup_method(self):
        _clear_state()

    def test_pending_lookup_is_session_scoped(self):
        from tools import secret_capture_gateway as scg

        scg.register("req-a", "session-A", "TAVILY_API_KEY", "web search")

        assert scg.get_pending_for_session("session-A") is not None
        assert scg.get_pending_for_session("session-B") is None
        assert scg.has_pending("session-A") is True
        assert scg.has_pending("session-B") is False

    def test_foreign_session_reply_cannot_resolve_another_sessions_capture(self):
        from tools import secret_capture_gateway as scg

        entry = scg.register("req-a", "session-A", "TAVILY_API_KEY", "web search")

        with patch("hermes_cli.config.save_env_value_secure") as save_mock:
            outcome = scg.resolve_secret_reply("session-B", "sk-should-not-land-here")

        assert outcome is None
        save_mock.assert_not_called()
        # session A's entry is untouched and still pending.
        assert scg.get_pending_for_session("session-A") is entry
        assert entry.result is None

    def test_two_concurrent_sessions_do_not_cross_notify(self):
        from tools import secret_capture_gateway as scg

        seen_a = []
        seen_b = []
        scg.register_notify("session-A", lambda e: seen_a.append(e.env_var))
        scg.register_notify("session-B", lambda e: seen_b.append(e.env_var))

        with patch("hermes_cli.config.save_env_value_secure"):
            def _resolve_a():
                time.sleep(0.02)
                scg.resolve_secret_reply("session-A", "value-a")

            t = threading.Thread(target=_resolve_a)
            t.start()
            result = scg.request_secret("session-A", "FOO_KEY", "purpose", timeout=5)
            t.join()

        assert result["success"] is True
        assert seen_a == ["FOO_KEY"]
        assert seen_b == []  # session B's notify was never touched


class TestValueNeverLeaks:
    """(b) The secret value must never appear in a result dict."""

    def setup_method(self):
        _clear_state()

    def test_resolve_outcome_never_carries_the_value(self):
        from tools import secret_capture_gateway as scg

        scg.register("req-1", "sess-1", "TAVILY_API_KEY", "web search")
        secret_value = "sk-super-secret-value-xyz"

        with patch("hermes_cli.config.save_env_value_secure") as save_mock:
            outcome = scg.resolve_secret_reply("sess-1", secret_value)

        # The value DID reach the save function (that's the whole point)...
        save_mock.assert_called_once_with("TAVILY_API_KEY", secret_value)
        # ...but never appears anywhere in what resolve_secret_reply returns.
        assert secret_value not in str(outcome)
        assert "value" not in outcome

    def test_request_secret_result_never_carries_the_value(self):
        from tools import secret_capture_gateway as scg

        secret_value = "sk-another-secret-abc"

        def _notify(entry):
            # Simulate the runner-side text intercept resolving immediately.
            with patch("hermes_cli.config.save_env_value_secure"):
                scg.resolve_secret_reply(entry.session_key, secret_value)

        scg.register_notify("sess-2", _notify)
        result = scg.request_secret("sess-2", "TAVILY_API_KEY", "web search", timeout=5)

        assert result["success"] is True
        assert secret_value not in str(result)

    def test_entry_object_never_retains_the_value_after_resolution(self):
        from tools import secret_capture_gateway as scg

        entry = scg.register("req-x", "sess-x", "SOME_KEY", "purpose")
        with patch("hermes_cli.config.save_env_value_secure"):
            scg.resolve_secret_reply("sess-x", "sk-do-not-keep-me")

        assert "sk-do-not-keep-me" not in str(vars(entry))
        assert "sk-do-not-keep-me" not in str(entry.result)


class TestFreeFormNames:
    """(g) Any valid env-var name is accepted, not just the wizard catalog."""

    def setup_method(self):
        _clear_state()

    def test_non_catalog_name_is_saved_like_any_other(self):
        from tools import secret_capture_gateway as scg

        scg.register("req-y", "sess-y", "BITRIX_WEBHOOK", "Bitrix24 integration")
        with patch("hermes_cli.config.save_env_value_secure") as save_mock:
            outcome = scg.resolve_secret_reply("sess-y", "https://example.bitrix24.ru/hook")

        assert outcome["success"] is True
        assert outcome["stored_as"] == "BITRIX_WEBHOOK"
        save_mock.assert_called_once_with(
            "BITRIX_WEBHOOK", "https://example.bitrix24.ru/hook"
        )


class TestProviderVsToolClassification:
    """Ruling 7 — tool keys reload in place; provider keys need a restart."""

    def setup_method(self):
        _clear_state()

    def test_tool_key_does_not_need_restart_and_reloads_env(self):
        from tools import secret_capture_gateway as scg

        scg.register("req-t", "sess-t", "TAVILY_API_KEY", "web search")
        with patch("hermes_cli.config.save_env_value_secure"), \
             patch("hermes_cli.config.reload_env") as reload_mock:
            outcome = scg.resolve_secret_reply("sess-t", "tvly-abc123")

        assert outcome["provider"] is False
        assert outcome["needs_restart"] is False
        reload_mock.assert_called_once()

    def test_provider_key_needs_restart_and_does_not_reload_env(self):
        from tools import secret_capture_gateway as scg

        scg.register("req-p", "sess-p", "OPENAI_API_KEY", "model provider")
        with patch("hermes_cli.config.save_env_value_secure"), \
             patch("hermes_cli.credential_probes.probe_provider_key",
                   return_value={"ok": True, "reachable": True, "reason": None}), \
             patch("hermes_cli.config.reload_env") as reload_mock:
            outcome = scg.resolve_secret_reply("sess-p", "sk-openai-abc")

        assert outcome["provider"] is True
        assert outcome["needs_restart"] is True
        assert outcome["validated"] is True
        reload_mock.assert_not_called()


class TestTimeoutAndCleanup:
    def setup_method(self):
        _clear_state()

    def test_wait_for_response_times_out_without_a_reply(self):
        from tools import secret_capture_gateway as scg

        scg.register("req-z", "sess-z", "FOO", "purpose")
        result = scg.wait_for_response("req-z", timeout=0.2)
        assert result is None
        # Entry is cleaned up after the wait, regardless of outcome.
        assert scg.get_pending_for_session("sess-z") is None

    def test_clear_session_cancels_pending_entries(self):
        from tools import secret_capture_gateway as scg

        scg.register("req-c", "sess-c", "FOO", "purpose")
        cancelled = scg.clear_session("sess-c")
        assert cancelled == 1
        assert scg.get_pending_for_session("sess-c") is None

    def test_request_secret_without_notify_is_skipped(self):
        from tools import secret_capture_gateway as scg

        result = scg.request_secret("no-such-session", "FOO", "purpose", timeout=1)
        assert result["success"] is False
        assert result["skipped"] is True
