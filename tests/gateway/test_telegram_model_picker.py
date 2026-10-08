"""Tests for Telegram model picker thread fallback."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


class _AllowRunner:
    """Fake gateway runner whose auth hook always permits the caller.

    ``mb`` (back-to-provider-list) rewrites ``_model_picker_state`` and is
    gated like the picker's other browsing branches (spec 16 review, Task
    B) -- this test is about MarkdownV2 escaping, not authorization, so it
    grants access the same way ``tests/gateway/test_inline_window_l10n.py``
    does for its own non-auth picker tests.
    """

    async def _handle_message(self, event):
        return None

    def _is_user_authorized(self, source):
        return True


@pytest.fixture
def _capture_kb(monkeypatch):
    """Capture (label, callback_data) tuples from every keyboard build.

    The mock ``telegram`` module doesn't preserve button constructor args,
    so the adapter module's own names are swapped for capturing lambdas —
    same pattern as tests/gateway/test_inline_window_l10n.py.
    """
    captured: list = []
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data: (text, callback_data),
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
        lambda rows: captured.extend([list(row) for row in rows]) or rows,
    )
    return captured


class TestTelegramModelPicker:
    @pytest.mark.asyncio
    async def test_send_model_picker_escapes_dynamic_provider_label(self):
        adapter = _make_adapter()
        sent = {}

        async def mock_send_message(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(message_id=101)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        result = await adapter.send_model_picker(
            chat_id="12345",
            providers=[
                {"slug": "provider_one", "name": "Provider One", "total_models": 1, "is_current": True}
            ],
            current_model="model_1",
            current_provider="provider_one",
            session_key="s",
            on_model_selected=AsyncMock(),
            metadata={"thread_id": "99999"},
        )

        assert result.success is True
        assert "MARKDOWN_V2" in repr(sent["parse_mode"])
        assert "provider\\_one" in sent["text"]
        assert "`model_1`" in sent["text"]

    @pytest.mark.asyncio
    async def test_send_model_picker_shows_levels_and_reset(self, _capture_kb):
        """RAF-189: levels block + reset button appear when wired."""
        adapter = _make_adapter()
        sent = {}

        async def mock_send_message(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(message_id=102)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        result = await adapter.send_model_picker(
            chat_id="12345",
            providers=[
                {"slug": "p1", "name": "P One", "total_models": 1, "is_current": True}
            ],
            current_model="model_1",
            current_provider="p1",
            session_key="s",
            on_model_selected=AsyncMock(),
            metadata=None,
            levels_text="• your default: `model_9`",
            on_set_default=AsyncMock(),
            on_reset_default=AsyncMock(),
        )

        assert result.success is True
        # Inline code spans are protected verbatim by format_message.
        assert "`model_9`" in sent["text"]
        callbacks = [cb for row in _capture_kb for _label, cb in row]
        assert "mrd:" in callbacks
        assert "mx" in callbacks

    @pytest.mark.asyncio
    async def test_switch_with_default_affordance_stages_md_state(self, _capture_kb):
        """RAF-189: mm: switch keeps an md: button + compact state."""
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        on_selected = AsyncMock(return_value="switched ok")
        on_set_default = AsyncMock(return_value="default saved")
        adapter._model_picker_state["12345"] = {
            "providers": [{"slug": "p1", "name": "P One", "total_models": 1, "is_current": True}],
            "current_model": "model_1",
            "current_provider": "p1",
            "session_key": "s",
            "on_model_selected": on_selected,
            "on_set_default": on_set_default,
            "msg_id": 42,
            "model_list": ["model_9"],
            "selected_provider": "p1",
        }

        query = AsyncMock()
        query.data = "mm:0"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        on_selected.assert_awaited_once()
        # Post-switch keyboard offers md: and close.
        callbacks = [cb for row in _capture_kb for _label, cb in row]
        assert "md:" in callbacks
        assert "mx" in callbacks
        # Browsing state popped, compact default state staged with the model.
        assert "12345" not in adapter._model_picker_state
        dstate = adapter._model_picker_default_state["12345"]
        assert dstate["model_id"] == "model_9"
        assert dstate["provider_slug"] == "p1"

        # md: tap lands on the stored callback.
        query2 = AsyncMock()
        query2.data = "md:"
        query2.message = MagicMock()
        query2.message.chat_id = 12345
        query2.from_user = MagicMock()
        query2.answer = AsyncMock()
        query2.edit_message_text = AsyncMock()
        await adapter._handle_model_picker_callback(query2, "md:", "12345")
        on_set_default.assert_awaited_once_with("12345", "model_9", "p1")
        assert "12345" not in adapter._model_picker_default_state

    @pytest.mark.asyncio
    async def test_switch_without_affordance_keeps_legacy_behavior(self):
        """No on_set_default wired → no md: button staged, no default state."""
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        on_selected = AsyncMock(return_value="switched ok")
        adapter._model_picker_state["12345"] = {
            "providers": [],
            "current_model": "model_1",
            "current_provider": "p1",
            "session_key": "s",
            "on_model_selected": on_selected,
            "msg_id": 42,
            "model_list": ["model_9"],
            "selected_provider": "p1",
        }

        query = AsyncMock()
        query.data = "mm:0"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        assert query.edit_message_text.call_args[1]["reply_markup"] is None
        assert adapter._model_picker_default_state == {}

    @pytest.mark.asyncio
    async def test_mrd_resets_default(self):
        """RAF-189: mrd: on the provider screen calls on_reset_default."""
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        on_reset = AsyncMock(return_value="default cleared")
        adapter._model_picker_state["12345"] = {
            "providers": [{"slug": "p1", "name": "P One", "total_models": 1, "is_current": True}],
            "current_model": "model_1",
            "current_provider": "p1",
            "session_key": "s",
            "on_model_selected": AsyncMock(),
            "on_reset_default": on_reset,
            "msg_id": 42,
        }

        query = AsyncMock()
        query.data = "mrd:"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mrd:", "12345")

        on_reset.assert_awaited_once_with("12345")
        assert "12345" not in adapter._model_picker_state

    @pytest.mark.asyncio
    async def test_back_button_escapes_dynamic_provider_label(self):
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        adapter._model_picker_state["12345"] = {
            "providers": [{"slug": "provider_one", "name": "Provider One", "total_models": 1, "is_current": True}],
            "current_model": "model_1",
            "current_provider": "provider_one",
            "session_key": "s",
            "on_model_selected": AsyncMock(),
            "msg_id": 42,
        }

        query = AsyncMock()
        query.data = "mb"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mb", "12345")

        edit_kwargs = query.edit_message_text.call_args[1]
        assert "MARKDOWN_V2" in repr(edit_kwargs["parse_mode"])
        assert "provider\\_one" in edit_kwargs["text"]
        assert "`model_1`" in edit_kwargs["text"]


