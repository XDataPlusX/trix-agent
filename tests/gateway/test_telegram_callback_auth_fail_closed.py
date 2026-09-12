"""Tests for Telegram adapter fail-closed auth fallback (#24457).

The _is_callback_user_authorized fallback must deny users by default
when TELEGRAM_ALLOWED_USERS is empty, instead of allowing everyone.
"""

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.i18n import t
from gateway.config import PlatformConfig, Platform
from hermes_cli.model_cost_guard import ExpensiveModelWarning


# -- Fake telegram modules (minimal stubs) --------------------------------

_fake_telegram_error = types.ModuleType("telegram.error")


class _TelegramError(Exception):
    pass


_fake_telegram_error.TelegramError = _TelegramError
_fake_telegram_error.BadRequest = type("BadRequest", (_TelegramError,), {})
_fake_telegram_error.NetworkError = type("NetworkError", (_TelegramError,), {})

_fake_telegram_constants = types.ModuleType("telegram.constants")
_fake_telegram_constants.ParseMode = SimpleNamespace(HTML="HTML")

_fake_telegram_request = types.ModuleType("telegram.request")
_fake_telegram_request.HTTPXRequest = type("HTTPXRequest", (), {"__init__": lambda *a, **kw: None})

_fake_telegram_ext = types.ModuleType("telegram.ext")
_fake_telegram_ext.ApplicationBuilder = type("ApplicationBuilder", (), {
    "token": lambda self, *a: self,
    "build": lambda self: None,
})

_fake_telegram = types.ModuleType("telegram")
_fake_telegram.error = _fake_telegram_error
_fake_telegram.constants = _fake_telegram_constants
_fake_telegram.ext = _fake_telegram_ext
_fake_telegram.request = _fake_telegram_request


@pytest.fixture(autouse=True)
def _inject_fake_telegram(monkeypatch):
    monkeypatch.setitem(sys.modules, "telegram", _fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.error", _fake_telegram_error)
    monkeypatch.setitem(sys.modules, "telegram.constants", _fake_telegram_constants)
    monkeypatch.setitem(sys.modules, "telegram.ext", _fake_telegram_ext)
    monkeypatch.setitem(sys.modules, "telegram.request", _fake_telegram_request)


def _make_adapter():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = object.__new__(TelegramAdapter)
    adapter.config = config
    adapter._config = config
    adapter._platform = Platform.TELEGRAM
    adapter._connected = True
    return adapter


class TestCallbackAuthFailClosed:
    """_is_callback_user_authorized fallback must be fail-closed."""

    def test_no_allowlist_no_allow_all_denies(self, monkeypatch):
        """No TELEGRAM_ALLOWED_USERS and no GATEWAY_ALLOW_ALL_USERS → deny."""
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
        adapter = _make_adapter()
        # Force the fallback path (no runner auth)
        adapter._message_handler = None
        assert adapter._is_callback_user_authorized("12345") is False


    def test_allowlist_with_matching_user_permits(self, monkeypatch):
        """TELEGRAM_ALLOWED_USERS contains the user → allow."""
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "12345,67890")
        adapter = _make_adapter()
        adapter._message_handler = None
        assert adapter._is_callback_user_authorized("12345") is True


# ---------------------------------------------------------------------------
# Task 4b: the /model picker's mm:/mc: switch branches must gate through
# _is_callback_user_authorized the same way the other five inline-window
# callback handlers already do (choice picker, exec approval, slash-command
# confirm, clarify, gmail triage). Spec 16's plan names this the sixth
# instance found while translating window A — the checked-in adapter
# comment right above the choice picker's own check says why it exists:
# "unauthorized users in a shared group must not flip session/config state
# via someone else's picker message". Before this fix the model picker had
# no such check anywhere, so the scenario was open and billed to the
# client's own key.
# ---------------------------------------------------------------------------


class _AllowRunner:
    """Fake gateway runner whose auth hook always permits the caller."""

    async def _handle_message(self, event):
        return None

    def _is_user_authorized(self, source):
        return True


class _DenyRunner:
    """Fake gateway runner whose auth hook always rejects the caller."""

    async def _handle_message(self, event):
        return None

    def _is_user_authorized(self, source):
        return False


def _make_model_picker_adapter():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _make_callback_query(data: str):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = 12345
    query.from_user = MagicMock()
    query.from_user.id = "999"
    query.from_user.first_name = "Mallory"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _seed_model_picker_state(adapter, chat_id="12345", on_model_selected=None):
    if on_model_selected is None:
        on_model_selected = AsyncMock(return_value="switched")
    providers = [
        {
            "slug": "provx",
            "name": "Prov X",
            "total_models": 1,
            "models": ["provx/model-a"],
            "is_current": True,
        }
    ]
    state = {
        "msg_id": 1,
        "providers": providers,
        "session_key": "s1",
        "on_model_selected": on_model_selected,
        "current_model": "provx/model-a",
        "current_provider": "provx",
        "provider_page": 0,
        "selected_provider": "provx",
        "model_list": ["provx/model-a"],
    }
    adapter._model_picker_state[chat_id] = state
    return state


class TestModelPickerCallbackAuth:
    """/model's mm:/mc: (switch) AND its browsing branches (mp:/mg:/mpv:/
    mpg:/mb) must deny an unauthorized tap and must not touch
    ``_model_picker_state`` for the chat. Browsing isn't read-only: it
    rewrites ``model_list``/``selected_provider``/``model_page``/
    ``provider_page``, which the owner's *next* tap resolves against — an
    unauthorized browse can steer what an authorized mm:/mc: tap later
    switches to. mx: (cancel) is deliberately exempt — see
    test_mx_cancel_allowed_for_unauthorized_user.
    """

    @pytest.mark.asyncio
    async def test_mm_switch_denied_for_unauthorized_user(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.model_cost_guard.expensive_model_warning",
            MagicMock(return_value=None),
        )
        adapter = _make_model_picker_adapter()
        on_model_selected = AsyncMock(return_value="switched")
        state = _seed_model_picker_state(adapter, on_model_selected=on_model_selected)
        adapter._message_handler = _DenyRunner()._handle_message

        query = _make_callback_query("mm:0")
        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        on_model_selected.assert_not_called()
        # Guarded branch — state must survive an unauthorized tap untouched.
        assert adapter._model_picker_state.get("12345") is state
        # Behavior (on_model_selected not called, state untouched) is
        # already asserted above; this only checks the RIGHT tooltip fired,
        # via the same t() lookup the adapter itself uses -- not a literal
        # English substring, which would tie a rights check to a
        # translation's wording and only survive because the suite pins
        # HERMES_LANGUAGE=en.
        assert query.answer.call_args[1]["text"] == t("trix.cmd.shared.not_authorized_setting")

    @pytest.mark.asyncio
    async def test_mm_switch_denied_for_unauthorized_user_on_expensive_model(self, monkeypatch):
        """Task A regression: the gate must sit BEFORE the expensive-model
        lookup, not after it. Before the fix, an unauthorized tap on a model
        priced above the cost guardrail would still run the (network-bound)
        pricing lookup and redraw the client's window with a fresh "switch
        anyway" button carrying the tapper's own idx — the gate only caught
        the *second* tap. Neither must happen for a denied caller.
        """
        warning = ExpensiveModelWarning(
            model="provx/model-a",
            provider="provx",
            input_cost_per_million=None,
            output_cost_per_million=None,
            source="test",
            message="pricey",
        )
        warning_mock = MagicMock(return_value=warning)
        monkeypatch.setattr(
            "hermes_cli.model_cost_guard.expensive_model_warning",
            warning_mock,
        )
        adapter = _make_model_picker_adapter()
        on_model_selected = AsyncMock(return_value="switched")
        state = _seed_model_picker_state(adapter, on_model_selected=on_model_selected)
        adapter._message_handler = _DenyRunner()._handle_message

        query = _make_callback_query("mm:0")
        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        on_model_selected.assert_not_called()
        warning_mock.assert_not_called()
        query.edit_message_text.assert_not_called()
        assert adapter._model_picker_state.get("12345") is state
        # Behavior (on_model_selected not called, state untouched) is
        # already asserted above; this only checks the RIGHT tooltip fired,
        # via the same t() lookup the adapter itself uses -- not a literal
        # English substring, which would tie a rights check to a
        # translation's wording and only survive because the suite pins
        # HERMES_LANGUAGE=en.
        assert query.answer.call_args[1]["text"] == t("trix.cmd.shared.not_authorized_setting")

    @pytest.mark.asyncio
    async def test_mm_switch_allowed_for_authorized_user(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.model_cost_guard.expensive_model_warning",
            MagicMock(return_value=None),
        )
        adapter = _make_model_picker_adapter()
        on_model_selected = AsyncMock(return_value="switched")
        _seed_model_picker_state(adapter, on_model_selected=on_model_selected)
        adapter._message_handler = _AllowRunner()._handle_message

        query = _make_callback_query("mm:0")
        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        on_model_selected.assert_awaited_once_with("12345", "provx/model-a", "provx")
        assert "12345" not in adapter._model_picker_state

    @pytest.mark.asyncio
    async def test_mc_switch_denied_for_unauthorized_user(self):
        adapter = _make_model_picker_adapter()
        on_model_selected = AsyncMock(return_value="switched")
        state = _seed_model_picker_state(adapter, on_model_selected=on_model_selected)
        adapter._message_handler = _DenyRunner()._handle_message

        query = _make_callback_query("mc:0")
        await adapter._handle_model_picker_callback(query, "mc:0", "12345")

        on_model_selected.assert_not_called()
        assert adapter._model_picker_state.get("12345") is state
        # Behavior (on_model_selected not called, state untouched) is
        # already asserted above; this only checks the RIGHT tooltip fired,
        # via the same t() lookup the adapter itself uses -- not a literal
        # English substring, which would tie a rights check to a
        # translation's wording and only survive because the suite pins
        # HERMES_LANGUAGE=en.
        assert query.answer.call_args[1]["text"] == t("trix.cmd.shared.not_authorized_setting")

    @pytest.mark.asyncio
    async def test_mc_switch_allowed_for_authorized_user(self):
        adapter = _make_model_picker_adapter()
        on_model_selected = AsyncMock(return_value="switched")
        _seed_model_picker_state(adapter, on_model_selected=on_model_selected)
        adapter._message_handler = _AllowRunner()._handle_message

        query = _make_callback_query("mc:0")
        await adapter._handle_model_picker_callback(query, "mc:0", "12345")

        on_model_selected.assert_awaited_once_with("12345", "provx/model-a", "provx")
        assert "12345" not in adapter._model_picker_state

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data", ["mp:provx", "mg:0", "mpv:0", "mb"])
    async def test_browsing_denied_for_unauthorized_user(self, data):
        """An unauthorized tap must not be able to page through someone
        else's /model picker window: mp:/mg:/mpv:/mb rewrite
        ``model_list``/``selected_provider``/``model_page``/
        ``provider_page`` on ``_model_picker_state``, and a later
        authorized mm:/mc: tap resolves its index against whatever that
        state currently holds. A denied browse must neither redraw the
        message nor change one byte of the seeded state.
        """
        adapter = _make_model_picker_adapter()
        state = _seed_model_picker_state(adapter)
        snapshot = dict(state)
        adapter._message_handler = _DenyRunner()._handle_message

        query = _make_callback_query(data)
        await adapter._handle_model_picker_callback(query, data, "12345")

        query.edit_message_text.assert_not_called()
        assert adapter._model_picker_state.get("12345") is state
        assert state == snapshot
        # Behavior (on_model_selected not called, state untouched) is
        # already asserted above; this only checks the RIGHT tooltip fired,
        # via the same t() lookup the adapter itself uses -- not a literal
        # English substring, which would tie a rights check to a
        # translation's wording and only survive because the suite pins
        # HERMES_LANGUAGE=en.
        assert query.answer.call_args[1]["text"] == t("trix.cmd.shared.not_authorized_setting")

    @pytest.mark.asyncio
    async def test_mx_cancel_allowed_for_unauthorized_user(self):
        """Closing someone else's stale picker window costs nothing --
        unlike mm:/mc: and the browsing branches above, it doesn't rewrite
        anything a later authorized tap depends on. It DOES discard the
        picker window itself (the state entry is popped below), which is
        exactly why the "cost nothing" case for it is deliberately narrow:
        an unauthorized tap may dismiss the window, never steer it.
        """
        adapter = _make_model_picker_adapter()
        _seed_model_picker_state(adapter)
        adapter._message_handler = _DenyRunner()._handle_message

        query = _make_callback_query("mx")
        await adapter._handle_model_picker_callback(query, "mx", "12345")

        assert "12345" not in adapter._model_picker_state
        query.edit_message_text.assert_called_once()


