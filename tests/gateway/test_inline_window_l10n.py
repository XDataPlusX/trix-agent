"""Spec 16, Task 1: the ``/model`` picker window (window A) speaks Russian.

Spec 12 translated the *text* of this window (``trix.cmd.model.picker.*``
header/labels), but left the **buttons** under that text and **every popup
tooltip** in English -- a Russian client reads a Russian screen and taps
``◀ Prev`` / ``Next ▶`` / ``✗ Cancel``. See
``docs/product/specs/2026-09-04-trix-agent-inline-windows-l10n-design.md``
section 3 for the full inventory ("window A", 27 spots).

No source-reading (Ruling R8 / this repo's hard antipattern): every check
here calls a real adapter method (``send_model_picker`` /
``_handle_model_picker_callback``) and recovers behavior from what that call
actually produced -- the ``InlineKeyboardMarkup`` rows handed to a send/edit
call, or the ``text=`` kwarg passed to ``query.answer`` / ``query.edit_message_text``.
Nothing here greps ``adapter.py``.

Not a snapshot (Ruling R7): the Russian-language assertions check the
invariant "no button label / no tooltip is pure ASCII letters" via
``_assert_ru`` (mirrors ``_assert_no_english_button`` from
``tests/gateway/test_confirm_button_labels.py``, which judges only the
letters so a bare ``✓``/``⛔`` glyph doesn't produce a false pass). The
English-language assertions pin the literal strings the catalog already
promises to preserve (Ruling R3) -- that half IS allowed to be a literal
comparison, because English must stay byte-identical to what shipped before
this task, not merely "non-empty".

Pagination trap (see the plan's "Global constraints"): the ``◀ Prev`` /
``Next ▶`` buttons only render once there are more providers than
``_PROVIDER_PAGE_SIZE`` (10) or more models than ``_MODEL_PAGE_SIZE`` (8).
Every keyboard test below seeds past those thresholds on purpose.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import i18n
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter, _update_prompt_button_labels
from tools import clarify_gateway as _clarify_gateway


# ---------------------------------------------------------------------------
# Language plumbing -- tests/gateway/conftest.py pins HERMES_LANGUAGE=en for
# the whole suite (autouse), so a test asserting "the client sees Russian"
# must set ru itself and reset the process-wide i18n cache afterwards.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_i18n_after():
    yield
    i18n.reset_language_cache()


def _set_lang(monkeypatch, lang: str) -> None:
    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    i18n.reset_language_cache()


# ---------------------------------------------------------------------------
# Keyboard capture -- the mock ``telegram`` module installed by
# tests/gateway/conftest.py doesn't preserve the ``text`` passed to
# ``InlineKeyboardButton``, so every button-label test in this repo
# monkeypatches the two names in the adapter module's own namespace to
# capturing/identity functions instead. Pattern taken verbatim from
# tests/gateway/test_confirm_button_labels.py.
# ---------------------------------------------------------------------------


@pytest.fixture
def _capture_keyboard_rows(monkeypatch):
    captured_rows: list = []
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data: text,
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
        lambda rows: captured_rows.extend([list(row) for row in rows]) or rows,
    )
    return captured_rows


def _assert_ru(text: str) -> None:
    """R7 invariant: the string has letters, and they aren't plain ASCII."""
    letters = "".join(ch for ch in text if ch.isalpha())
    assert letters, f"no letters at all: {text!r}"
    assert not letters.isascii(), f"still English: {text!r}"


def _assert_ru_is(text: str, key: str) -> None:
    """Russian AND from the branch the test claims to cover.

    ``_assert_ru`` alone cannot tell one Russian tooltip from another. Every
    /model branch below now sits behind an authorization gate whose refusal
    tooltip is *also* Russian, so a test that lost its authorized runner
    would keep passing while covering the refusal instead of its own branch
    -- exactly how ``test_mm_switch_failed_tooltip`` went silently wrong.
    Comparing against the catalog pins which key rendered, i.e. which branch
    ran, without freezing the wording itself.
    """
    from agent.i18n import t

    _assert_ru(text)
    assert text == t(key), f"expected {key!r} -> {t(key)!r}, got {text!r}"


def _make_adapter() -> TelegramAdapter:
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    adapter._app = MagicMock()
    return adapter


def _make_query(data: str):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = 12345
    query.from_user = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _providers(n: int, current_index: int = 0) -> list:
    """``n`` distinct, ungrouped provider slugs -- past PROVIDER_GROUPS so
    ``group_providers`` folds none of them and the count stays exactly ``n``.
    """
    return [
        {
            "slug": f"zzprovider{i:02d}",
            "name": f"ZZ Provider {i}",
            "total_models": 1,
            "models": [f"zzprovider{i:02d}/model-a"],
            "is_current": i == current_index,
        }
        for i in range(n)
    ]


def _models(n: int) -> list:
    return [f"provx/model-{i:02d}" for i in range(n)]


async def _seed_provider_state(adapter, providers, current_model="provx/model-00", current_provider=None):
    """Populate ``_model_picker_state`` the way ``send_model_picker`` would,
    without going through the network-send plumbing -- used by tests that
    only care about callback behavior, not the initial send.
    """
    adapter._model_picker_state["12345"] = {
        "msg_id": 1,
        "providers": providers,
        "session_key": "s1",
        "on_model_selected": AsyncMock(return_value="switched ok"),
        "current_model": current_model,
        "current_provider": current_provider or providers[0]["slug"],
        "provider_page": 0,
    }
    return adapter._model_picker_state["12345"]


# ---------------------------------------------------------------------------
# Group (a): keyboard labels, ru -- built from the real send + callback
# methods, read back from the captured InlineKeyboardMarkup rows.
# ---------------------------------------------------------------------------


class TestModelPickerKeyboardIsRussian:
    @pytest.mark.asyncio
    async def test_provider_pagination_buttons_are_russian(self, monkeypatch, _capture_keyboard_rows):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = _providers(15)  # > _PROVIDER_PAGE_SIZE (10) -> 2 pages

        result = await adapter.send_model_picker(
            chat_id="12345",
            providers=providers,
            current_model="provx/model-00",
            current_provider=providers[0]["slug"],
            session_key="s1",
            on_model_selected=AsyncMock(),
            metadata=None,
        )
        assert result.success is True

        # Page 0: nav row has only "Next" (no "Prev" on the first page);
        # last row is the lone Cancel button.
        nav_row, cancel_row = _capture_keyboard_rows[-2], _capture_keyboard_rows[-1]
        assert len(nav_row) == 2, nav_row  # page counter + Next
        assert len(cancel_row) == 1, cancel_row
        _assert_ru(nav_row[1])
        _assert_ru(cancel_row[0])

        # Navigate to page 1 -> now "Prev" shows up too.
        query = _make_query("mpv:1")
        _capture_keyboard_rows.clear()
        await adapter._handle_model_picker_callback(query, "mpv:1", "12345")
        nav_row, cancel_row = _capture_keyboard_rows[-2], _capture_keyboard_rows[-1]
        assert len(nav_row) == 2, nav_row  # Prev + page counter (page 1 is the last page)
        _assert_ru(nav_row[0])
        _assert_ru(cancel_row[0])

    @pytest.mark.asyncio
    async def test_model_pagination_and_back_buttons_are_russian(self, monkeypatch, _capture_keyboard_rows):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = [
            {
                "slug": "provx",
                "name": "Prov X",
                "total_models": 12,
                "models": _models(12),  # > _MODEL_PAGE_SIZE (8) -> 2 pages
                "is_current": True,
            }
        ]
        await _seed_provider_state(adapter, providers, current_provider="provx")

        query = _make_query("mp:provx")
        await adapter._handle_model_picker_callback(query, "mp:provx", "12345")

        # Last row is Back + Cancel; the row before it is nav (Next only, page 0).
        back_cancel_row, nav_row = _capture_keyboard_rows[-1], _capture_keyboard_rows[-2]
        assert len(back_cancel_row) == 2, back_cancel_row
        _assert_ru(back_cancel_row[0])  # Back
        _assert_ru(back_cancel_row[1])  # Cancel
        assert len(nav_row) == 2, nav_row  # page counter + Next
        _assert_ru(nav_row[1])

        # Page navigation within the model list ("mg:") -> "Prev" appears.
        _capture_keyboard_rows.clear()
        query2 = _make_query("mg:1")
        await adapter._handle_model_picker_callback(query2, "mg:1", "12345")
        nav_row2 = _capture_keyboard_rows[-2]
        _assert_ru(nav_row2[0])

    @pytest.mark.asyncio
    async def test_expensive_model_keyboard_buttons_are_russian(self, monkeypatch, _capture_keyboard_rows):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = [
            {
                "slug": "provx",
                "name": "Prov X",
                "total_models": 1,
                "models": ["provx/pricey-model"],
                "is_current": True,
            }
        ]
        state = await _seed_provider_state(adapter, providers, current_provider="provx")
        state["selected_provider"] = "provx"
        state["model_list"] = ["provx/pricey-model"]

        warning = SimpleNamespace(message="цена высокая")
        monkeypatch.setattr(
            "hermes_cli.model_cost_guard.expensive_model_warning",
            MagicMock(return_value=warning),
        )

        query = _make_query("mm:0")
        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        switch_row, back_cancel_row = _capture_keyboard_rows[0], _capture_keyboard_rows[1]
        assert len(switch_row) == 1, switch_row
        _assert_ru(switch_row[0])  # "Switch anyway"
        assert len(back_cancel_row) == 2, back_cancel_row
        _assert_ru(back_cancel_row[0])
        _assert_ru(back_cancel_row[1])

        # The tooltip that accompanies this screen must also be Russian.
        answer_text = query.answer.call_args[1]["text"]
        _assert_ru(answer_text)

        # Ruling R6: the title reuses the existing cost_warning_title key,
        # not a fresh one -- assert the edited body doesn't carry the old
        # English literal.
        edit_text = query.edit_message_text.call_args[1]["text"]
        assert "Expensive Model Warning" not in edit_text


# ---------------------------------------------------------------------------
# Group (b): tooltip text on every reject branch, ru.
# ---------------------------------------------------------------------------


class TestModelPickerTooltipsAreRussian:
    @pytest.mark.asyncio
    async def test_no_state_expired_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        query = _make_query("mp:anything")

        await adapter._handle_model_picker_callback(query, "mp:anything", "12345")

        _assert_ru_is(query.answer.call_args[1]["text"], "trix.cmd.model.picker.expired_use_model")

    @pytest.mark.asyncio
    async def test_mp_provider_not_found_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = _providers(1)
        await _seed_provider_state(adapter, providers)
        query = _make_query("mp:does-not-exist")

        await adapter._handle_model_picker_callback(query, "mp:does-not-exist", "12345")

        _assert_ru_is(query.answer.call_args[1]["text"], "trix.cmd.model.picker.provider_not_found")

    @pytest.mark.asyncio
    async def test_mg_invalid_page_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = _providers(1)
        await _seed_provider_state(adapter, providers)
        query = _make_query("mg:not-a-number")

        await adapter._handle_model_picker_callback(query, "mg:not-a-number", "12345")

        _assert_ru_is(query.answer.call_args[1]["text"], "trix.cmd.model.picker.invalid_page")

    @pytest.mark.asyncio
    async def test_mpv_invalid_page_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = _providers(1)
        await _seed_provider_state(adapter, providers)
        query = _make_query("mpv:not-a-number")

        await adapter._handle_model_picker_callback(query, "mpv:not-a-number", "12345")

        _assert_ru_is(query.answer.call_args[1]["text"], "trix.cmd.model.picker.invalid_page")

    @pytest.mark.asyncio
    async def test_mc_invalid_selection_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        providers = _providers(1)
        await _seed_provider_state(adapter, providers)
        query = _make_query("mc:not-a-number")

        await adapter._handle_model_picker_callback(query, "mc:not-a-number", "12345")

        _assert_ru_is(query.answer.call_args[1]["text"], "trix.cmd.model.picker.invalid_selection")

    @pytest.mark.asyncio
    async def test_mc_picker_expired_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        providers = _providers(1)
        state = await _seed_provider_state(adapter, providers)
        state["model_list"] = ["provx/model-a"]
        state["on_model_selected"] = None
        query = _make_query("mc:0")

        await adapter._handle_model_picker_callback(query, "mc:0", "12345")

        _assert_ru_is(query.answer.call_args[1]["text"], "trix.cmd.model.picker.expired")

    @pytest.mark.asyncio
    async def test_mm_invalid_model_index_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        providers = _providers(1)
        state = await _seed_provider_state(adapter, providers)
        state["model_list"] = ["provx/model-a"]
        query = _make_query("mm:999")

        await adapter._handle_model_picker_callback(query, "mm:999", "12345")

        _assert_ru_is(query.answer.call_args[1]["text"], "trix.cmd.model.picker.invalid_model_index")

    @pytest.mark.asyncio
    async def test_mx_cancel_body_is_russian(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        providers = _providers(1)
        await _seed_provider_state(adapter, providers)
        query = _make_query("mx")

        await adapter._handle_model_picker_callback(query, "mx", "12345")

        _assert_ru(query.edit_message_text.call_args[1]["text"])

    @pytest.mark.asyncio
    async def test_mpg_group_not_found_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = _providers(1)
        await _seed_provider_state(adapter, providers)
        query = _make_query("mpg:no-such-group-xyz")

        await adapter._handle_model_picker_callback(query, "mpg:no-such-group-xyz", "12345")

        _assert_ru_is(query.answer.call_args[1]["text"], "trix.cmd.model.picker.group_not_found")

    @pytest.mark.asyncio
    async def test_mm_switch_failed_tooltip(self, monkeypatch):
        """The switch itself blew up -- tooltip AND message body are Russian.

        The caller must be authorized here or this branch is never reached:
        ``mm:`` is gated (``test_telegram_callback_auth_fail_closed.py::
        test_mm_switch_denied_for_unauthorized_user``), and an unauthorized
        tap returns the denial tooltip instead. That denial is Russian too,
        so a lone "is it Russian" assertion passes either way -- the test
        would sit green while covering a different branch than its name
        claims. Pinning the failure body says which branch actually ran.
        """
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = _providers(1)
        state = await _seed_provider_state(adapter, providers)
        state["model_list"] = ["provx/model-a"]

        async def _boom(*_a, **_kw):
            raise RuntimeError("boom")

        state["on_model_selected"] = _boom
        monkeypatch.setattr(
            "hermes_cli.model_cost_guard.expensive_model_warning",
            MagicMock(return_value=None),
        )
        query = _make_query("mm:0")

        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        _assert_ru(query.answer.call_args[1]["text"])
        assert query.edit_message_text.called, (
            "the switch branch never ran -- the tap was refused before it"
        )
        body = query.edit_message_text.call_args[1]["text"]
        assert "boom" in body, body
        _assert_ru(body)

    @pytest.mark.asyncio
    async def test_mm_switch_succeeded_tooltip(self, monkeypatch):
        """The success half of the same pair -- it had no test at all."""
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = _providers(1)
        state = await _seed_provider_state(adapter, providers)
        state["model_list"] = ["provx/model-a"]
        state["on_model_selected"] = AsyncMock(return_value="Модель изменена")
        monkeypatch.setattr(
            "hermes_cli.model_cost_guard.expensive_model_warning",
            MagicMock(return_value=None),
        )
        query = _make_query("mm:0")

        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        state["on_model_selected"].assert_awaited_once()
        _assert_ru(query.answer.call_args[1]["text"])


# ---------------------------------------------------------------------------
# Group (c): the same calls, at en -- today's English literals must survive
# byte-for-byte (Ruling R3).
# ---------------------------------------------------------------------------


class TestModelPickerStaysEnglish:
    @pytest.mark.asyncio
    async def test_no_state_expired_tooltip_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        query = _make_query("mp:anything")

        await adapter._handle_model_picker_callback(query, "mp:anything", "12345")

        assert query.answer.call_args[1]["text"] == "Picker expired — use /model again."

    @pytest.mark.asyncio
    async def test_mp_provider_not_found_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = _providers(1)
        await _seed_provider_state(adapter, providers)
        query = _make_query("mp:does-not-exist")

        await adapter._handle_model_picker_callback(query, "mp:does-not-exist", "12345")

        assert query.answer.call_args[1]["text"] == "Provider not found."

    @pytest.mark.asyncio
    async def test_mg_invalid_page_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = _providers(1)
        await _seed_provider_state(adapter, providers)
        query = _make_query("mg:not-a-number")

        await adapter._handle_model_picker_callback(query, "mg:not-a-number", "12345")

        assert query.answer.call_args[1]["text"] == "Invalid page."

    @pytest.mark.asyncio
    async def test_mpv_invalid_page_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = _providers(1)
        await _seed_provider_state(adapter, providers)
        query = _make_query("mpv:not-a-number")

        await adapter._handle_model_picker_callback(query, "mpv:not-a-number", "12345")

        assert query.answer.call_args[1]["text"] == "Invalid page."

    @pytest.mark.asyncio
    async def test_mc_invalid_selection_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        providers = _providers(1)
        await _seed_provider_state(adapter, providers)
        query = _make_query("mc:not-a-number")

        await adapter._handle_model_picker_callback(query, "mc:not-a-number", "12345")

        assert query.answer.call_args[1]["text"] == "Invalid selection."

    @pytest.mark.asyncio
    async def test_mm_invalid_model_index_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        providers = _providers(1)
        state = await _seed_provider_state(adapter, providers)
        state["model_list"] = ["provx/model-a"]
        query = _make_query("mm:999")

        await adapter._handle_model_picker_callback(query, "mm:999", "12345")

        assert query.answer.call_args[1]["text"] == "Invalid model index."

    @pytest.mark.asyncio
    async def test_mx_cancel_body_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        providers = _providers(1)
        await _seed_provider_state(adapter, providers)
        query = _make_query("mx")

        await adapter._handle_model_picker_callback(query, "mx", "12345")

        assert query.edit_message_text.call_args[1]["text"] == "Model selection cancelled."

    @pytest.mark.asyncio
    async def test_mpg_group_not_found_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = _providers(1)
        await _seed_provider_state(adapter, providers)
        query = _make_query("mpg:no-such-group-xyz")

        await adapter._handle_model_picker_callback(query, "mpg:no-such-group-xyz", "12345")

        assert query.answer.call_args[1]["text"] == "Group not found."

    @pytest.mark.asyncio
    async def test_switch_outcome_tooltips_are_english(self, monkeypatch):
        """The switched / switch-failed pair -- the only strings in window A
        whose English side had no guard at all (spotted by the Opus review of
        this spec). Both outcomes come off the same ternary, so one test
        drives the branch twice rather than pinning half of it.
        """
        _set_lang(monkeypatch, "en")
        monkeypatch.setattr(
            "hermes_cli.model_cost_guard.expensive_model_warning",
            MagicMock(return_value=None),
        )

        async def _run(on_selected):
            adapter = _make_adapter()
            adapter._message_handler = _AllowRunner()._handle_message
            state = await _seed_provider_state(adapter, _providers(1))
            state["model_list"] = ["provx/model-a"]
            state["on_model_selected"] = on_selected
            query = _make_query("mm:0")
            await adapter._handle_model_picker_callback(query, "mm:0", "12345")
            return query

        async def _boom(*_a, **_kw):
            raise RuntimeError("boom")

        assert (await _run(_boom)).answer.call_args[1]["text"] == "Switch failed."
        ok = await _run(AsyncMock(return_value="switched"))
        assert ok.answer.call_args[1]["text"] == "Model switched!"

    @pytest.mark.asyncio
    async def test_expensive_model_tooltip_and_buttons_are_english(self, monkeypatch, _capture_keyboard_rows):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = [
            {
                "slug": "provx",
                "name": "Prov X",
                "total_models": 1,
                "models": ["provx/pricey-model"],
                "is_current": True,
            }
        ]
        state = await _seed_provider_state(adapter, providers, current_provider="provx")
        state["selected_provider"] = "provx"
        state["model_list"] = ["provx/pricey-model"]

        warning = SimpleNamespace(message="expensive")
        monkeypatch.setattr(
            "hermes_cli.model_cost_guard.expensive_model_warning",
            MagicMock(return_value=warning),
        )

        query = _make_query("mm:0")
        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        assert query.answer.call_args[1]["text"] == "Confirm expensive model"
        switch_row, back_cancel_row = _capture_keyboard_rows[0], _capture_keyboard_rows[1]
        assert switch_row == ["Switch anyway"]
        assert back_cancel_row == ["◀ Back", "✗ Cancel"]

    @pytest.mark.asyncio
    async def test_provider_and_model_nav_buttons_are_english(self, monkeypatch, _capture_keyboard_rows):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        providers = _providers(15)

        await adapter.send_model_picker(
            chat_id="12345",
            providers=providers,
            current_model="provx/model-00",
            current_provider=providers[0]["slug"],
            session_key="s1",
            on_model_selected=AsyncMock(),
            metadata=None,
        )

        nav_row, cancel_row = _capture_keyboard_rows[-2], _capture_keyboard_rows[-1]
        assert nav_row[-1] == "Next ▶"
        assert cancel_row == ["✗ Cancel"]

        _capture_keyboard_rows.clear()
        query = _make_query("mpv:1")
        await adapter._handle_model_picker_callback(query, "mpv:1", "12345")
        nav_row2 = _capture_keyboard_rows[-2]
        assert nav_row2[0] == "◀ Prev"



# ---------------------------------------------------------------------------
# Spec 16, Task 2: the generic finite-choice picker window (window C,
# ``/fast`` / ``/reasoning``) speaks Russian. Button labels are built by the
# *caller* and already arrive in Russian (see ``send_choice_picker`` --
# ``choice.get("label")`` is the command handler's text, not this adapter's).
# What's left in English is adapter-owned: the four tooltip reject branches
# and the "callback raised" error text, which lands in the edited message
# **body** via ``edit_message_text``, not a tooltip -- Ruling R1 covers the
# whole window, not just ``query.answer``.
#
# Same no-source-reading rule (R8): every check below drives the real
# ``_handle_choice_picker_callback`` and reads back ``query.answer`` /
# ``query.edit_message_text`` call kwargs. The "not authorized" branch is
# the one *guarded* branch in this window -- it's exercised with a fake
# ``_message_handler`` runner (the same technique
# ``tests/gateway/test_telegram_clarify_buttons.py::test_unauthorized_user_rejected``
# and ``tests/gateway/test_telegram_callback_auth_fail_closed.py`` use to
# drive the real ``_is_callback_user_authorized`` code path), never a direct
# monkeypatch of the authorization method itself.
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


_UNSET = object()


def _seed_choice_state(adapter, chat_id="12345", on_choice_selected=_UNSET):
    if on_choice_selected is _UNSET:
        on_choice_selected = AsyncMock(return_value="Готово")
    adapter._choice_picker_state[chat_id] = {
        "msg_id": 1,
        "choices": [{"value": "fast", "label": "Быстро", "is_current": False}],
        "session_key": "s1",
        "on_choice_selected": on_choice_selected,
    }
    return adapter._choice_picker_state[chat_id]


class TestChoicePickerTooltipsAreRussian:
    @pytest.mark.asyncio
    async def test_no_state_expired_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        query = _make_query("cp:0")

        await adapter._handle_choice_picker_callback(query, "cp:0", "12345")

        _assert_ru_is(query.answer.call_args[1]["text"], "trix.cmd.choice.expired_run_again")

    @pytest.mark.asyncio
    async def test_unauthorized_user_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        _seed_choice_state(adapter)
        adapter._message_handler = _DenyRunner()._handle_message
        query = _make_query("cp:0")

        await adapter._handle_choice_picker_callback(query, "cp:0", "12345")

        _assert_ru(query.answer.call_args[1]["text"])
        # Guarded branch -- confirm the state was NOT consumed/mutated.
        assert "12345" in adapter._choice_picker_state

    @pytest.mark.asyncio
    async def test_invalid_index_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        _seed_choice_state(adapter)
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_query("cp:99")

        await adapter._handle_choice_picker_callback(query, "cp:99", "12345")

        _assert_ru(query.answer.call_args[1]["text"])

    @pytest.mark.asyncio
    async def test_no_callback_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        _seed_choice_state(adapter, on_choice_selected=None)
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_query("cp:0")

        await adapter._handle_choice_picker_callback(query, "cp:0", "12345")

        _assert_ru(query.answer.call_args[1]["text"])

    @pytest.mark.asyncio
    async def test_callback_exception_lands_in_message_body_not_tooltip(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()

        async def _boom(*_a, **_kw):
            raise RuntimeError("boom")

        _seed_choice_state(adapter, on_choice_selected=_boom)
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_query("cp:0")

        await adapter._handle_choice_picker_callback(query, "cp:0", "12345")

        body = query.edit_message_text.call_args[1]["text"]
        _assert_ru(body)
        # The trailing bare `query.answer()` on the success tail carries no
        # tooltip text at all -- this is a body-only failure, not a tooltip.
        query.answer.assert_called_once_with()


class TestChoicePickerStaysEnglish:
    @pytest.mark.asyncio
    async def test_no_state_expired_tooltip_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        query = _make_query("cp:0")

        await adapter._handle_choice_picker_callback(query, "cp:0", "12345")

        assert query.answer.call_args[1]["text"] == "Picker expired — run the command again."

    @pytest.mark.asyncio
    async def test_unauthorized_user_tooltip_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        _seed_choice_state(adapter)
        adapter._message_handler = _DenyRunner()._handle_message
        query = _make_query("cp:0")

        await adapter._handle_choice_picker_callback(query, "cp:0", "12345")

        assert query.answer.call_args[1]["text"] == "⛔ You are not authorized to change this setting."

    @pytest.mark.asyncio
    async def test_invalid_index_tooltip_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        _seed_choice_state(adapter)
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_query("cp:99")

        await adapter._handle_choice_picker_callback(query, "cp:99", "12345")

        assert query.answer.call_args[1]["text"] == "Invalid selection."

    @pytest.mark.asyncio
    async def test_no_callback_tooltip_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        _seed_choice_state(adapter, on_choice_selected=None)
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_query("cp:0")

        await adapter._handle_choice_picker_callback(query, "cp:0", "12345")

        assert query.answer.call_args[1]["text"] == "Picker expired."

    @pytest.mark.asyncio
    async def test_callback_exception_body_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()

        async def _boom(*_a, **_kw):
            raise RuntimeError("boom")

        _seed_choice_state(adapter, on_choice_selected=_boom)
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_query("cp:0")

        await adapter._handle_choice_picker_callback(query, "cp:0", "12345")

        body = query.edit_message_text.call_args[1]["text"]
        assert body == "Error applying selection: boom"



# ---------------------------------------------------------------------------
# Spec 16, Task 3: the ``clarify`` window (window D) speaks Russian. This is
# not a settings screen -- it's how the agent asks the customer a question
# whenever it's missing information (``clarify_id``/``cl:`` callback prefix).
# See the plan's Task 3 for the inventory of seven English spots: an expired
# question's tooltip AND message body (``_notify_clarify_expired``), "not
# authorized", "already resolved", the "type your own answer" tooltip AND
# body, and the ``f"choice {idx + 1}"`` substitution that stands in for a
# LOST CHOICE TEXT and is read back by the customer as their own answer.
#
# "Not authorized" / "already resolved" reuse the existing
# ``trix.approval.confirm_unauthorized`` / ``confirm_already_resolved`` keys
# (Ruling R6-style key reuse) -- verified here via the real
# ``_handle_callback_query`` dispatch, not a second translation.
#
# Same no-source-reading rule (R8): every check drives the real adapter
# methods (``_notify_clarify_expired`` / ``_handle_callback_query``) and
# reads back ``query.answer`` / ``query.edit_message_text`` call kwargs.
# The auth-guarded branch is exercised with the same fake-runner technique
# as ``tests/gateway/test_telegram_clarify_buttons.py::test_unauthorized_user_rejected``
# -- never a direct monkeypatch of ``_is_callback_user_authorized``.
# ---------------------------------------------------------------------------


def _clear_clarify_state():
    with _clarify_gateway._lock:
        _clarify_gateway._entries.clear()
        _clarify_gateway._session_index.clear()
        _clarify_gateway._notify_cbs.clear()


def _make_clarify_query(data: str, text: str = "Pick one"):
    query = _make_query(data)
    query.message.text = text
    query.from_user.id = "777"
    query.from_user.first_name = "Client"
    return query


def _make_callback_update(query):
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()
    return update, context


class TestClarifyWindowIsRussian:
    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_expired_notification_tooltip_and_body_are_russian(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        query = _make_clarify_query("cl:whatever:0")

        await adapter._notify_clarify_expired(query, "Клиент")

        _assert_ru(query.answer.call_args[1]["text"])
        body = query.edit_message_text.call_args[1]["text"]
        _assert_ru(body)
        # HTML markup around the body sentence must survive translation.
        assert "<i>" in body and "</i>" in body

    @pytest.mark.asyncio
    async def test_unauthorized_tap_tooltip_is_russian(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        _clarify_gateway.register("cid-auth", "sk-auth", "Pick", ["a", "b"])
        adapter._clarify_state["cid-auth"] = "sk-auth"
        adapter._message_handler = _DenyRunner()._handle_message
        query = _make_clarify_query("cl:cid-auth:0")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        _assert_ru(query.answer.call_args[1]["text"])
        # Guarded branch -- confirm the state was NOT consumed/mutated.
        assert adapter._clarify_state["cid-auth"] == "sk-auth"

    @pytest.mark.asyncio
    async def test_already_resolved_tap_tooltip_is_russian(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        # No _clarify_state entry for this id -- already popped/resolved.
        query = _make_clarify_query("cl:cid-missing:0")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        _assert_ru(query.answer.call_args[1]["text"])

    @pytest.mark.asyncio
    async def test_own_answer_prompt_tooltip_and_body_are_russian(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        _clarify_gateway.register("cid-other", "sk-other", "Pick", ["a", "b"])
        adapter._clarify_state["cid-other"] = "sk-other"
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_clarify_query("cl:cid-other:other")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        _assert_ru(query.answer.call_args[1]["text"])
        body = query.edit_message_text.call_args[1]["text"]
        _assert_ru(body)
        assert "<i>" in body and "</i>" in body
        # Entry stays -- typed answer is still expected, not yet resolved.
        assert "cid-other" in adapter._clarify_state

    @pytest.mark.asyncio
    async def test_lost_choice_text_substitutes_russian_placeholder(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        # Deliberately do NOT register() an entry -- this reproduces the
        # race the plan calls out: the clarify entry vanished (timeout /
        # session reset) between the ask and the tap, so the
        # ``_entries.get(clarify_id)`` lookup in the callback finds
        # nothing and falls back to trix.clarify.choice_fallback.
        adapter._clarify_state["cid-race"] = "sk-race"
        adapter._message_handler = _AllowRunner()._handle_message
        monkeypatch.setattr(
            "tools.clarify_gateway.resolve_gateway_clarify",
            MagicMock(return_value=True),
        )
        query = _make_clarify_query("cl:cid-race:1")  # idx 1 -> "вариант 2"
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        tooltip = query.answer.call_args[1]["text"]
        _assert_ru(tooltip)
        assert "вариант 2" in tooltip
        body = query.edit_message_text.call_args[1]["text"]
        assert "вариант 2" in body


class TestClarifyWindowStaysEnglish:
    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_expired_notification_tooltip_and_body_are_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        query = _make_clarify_query("cl:whatever:0")

        await adapter._notify_clarify_expired(query, "Client")

        assert query.answer.call_args[1]["text"] == "⚠️ This prompt expired — please /retry."
        body = query.edit_message_text.call_args[1]["text"]
        assert body == (
            "❓ Pick one\n\n"
            "<i>⚠️ This question expired or the session reset — please /retry.</i>"
        )

    @pytest.mark.asyncio
    async def test_unauthorized_tap_tooltip_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        _clarify_gateway.register("cid-auth", "sk-auth", "Pick", ["a", "b"])
        adapter._clarify_state["cid-auth"] = "sk-auth"
        adapter._message_handler = _DenyRunner()._handle_message
        query = _make_clarify_query("cl:cid-auth:0")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        assert query.answer.call_args[1]["text"] == "⛔ You are not authorized to answer this prompt."

    @pytest.mark.asyncio
    async def test_already_resolved_tap_tooltip_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_clarify_query("cl:cid-missing:0")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        assert query.answer.call_args[1]["text"] == "This prompt has already been resolved."

    @pytest.mark.asyncio
    async def test_own_answer_prompt_tooltip_and_body_are_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        _clarify_gateway.register("cid-other", "sk-other", "Pick", ["a", "b"])
        adapter._clarify_state["cid-other"] = "sk-other"
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_clarify_query("cl:cid-other:other")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        assert query.answer.call_args[1]["text"] == "✏️ Type your answer in the chat."
        body = query.edit_message_text.call_args[1]["text"]
        assert body == "❓ Pick one\n\n<i>Awaiting typed response from Client…</i>"

    @pytest.mark.asyncio
    async def test_lost_choice_text_substitutes_english_placeholder(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        adapter._clarify_state["cid-race"] = "sk-race"
        adapter._message_handler = _AllowRunner()._handle_message
        monkeypatch.setattr(
            "tools.clarify_gateway.resolve_gateway_clarify",
            MagicMock(return_value=True),
        )
        query = _make_clarify_query("cl:cid-race:1")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        assert query.answer.call_args[1]["text"] == "✓ choice 2"
        body = query.edit_message_text.call_args[1]["text"]
        assert "choice 2" in body


# ---------------------------------------------------------------------------
# Spec 16, Task 3 addendum: a stale ``cl:`` button from a message the bot
# sent under an older callback_data format lands in the "invalid choice"
# branch after a Trix Agent update. Rare, but reachable (callback_data comes
# back from Telegram itself, from a button the bot rendered in a previous
# session/version) -- see the plan's Task 4 "довесок".
# ---------------------------------------------------------------------------


class TestClarifyInvalidChoiceTooltip:
    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_non_numeric_choice_token_tooltip_is_russian(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        _clarify_gateway.register("cid-bad", "sk-bad", "Pick", ["a", "b"])
        adapter._clarify_state["cid-bad"] = "sk-bad"
        adapter._message_handler = _AllowRunner()._handle_message
        # "notanindex" is not "other" and not parseable as int -- exactly
        # the shape a stale/foreign callback_data payload would take.
        query = _make_clarify_query("cl:cid-bad:notanindex")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        _assert_ru(query.answer.call_args[1]["text"])
        # Guarded branch -- confirm the state was NOT consumed.
        assert adapter._clarify_state["cid-bad"] == "sk-bad"

    @pytest.mark.asyncio
    async def test_non_numeric_choice_token_tooltip_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        _clarify_gateway.register("cid-bad", "sk-bad", "Pick", ["a", "b"])
        adapter._clarify_state["cid-bad"] = "sk-bad"
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_clarify_query("cl:cid-bad:notanindex")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        assert query.answer.call_args[1]["text"] == "Invalid choice."


# ---------------------------------------------------------------------------
# Spec 16, Task 4: the update-prompt response (window E) speaks Russian.
# Spec 12 already translated the Yes/No buttons themselves
# (``_update_prompt_button_labels``) -- everything AFTER the tap (the
# tooltip and the edited message body) was still English. See the plan's
# Task 4 for the inventory of four spots and the ``update_prompt:``
# callback prefix.
#
# "Not authorized" has its own ``trix.update.not_authorized`` key (review
# follow-up, Task G3): the window originally reused
# ``trix.approval.confirm_unauthorized``, which silently dropped this
# window's "update prompts" wording for the generic "this prompt" -- verified
# here via the real ``_handle_callback_query`` dispatch, not a second
# translation.
#
# The final-body label is asserted to be IDENTICAL to what the button itself
# shows (``_update_prompt_button_labels()``) -- that's the whole point of
# the fix: today the button says "✓ Yes" and the reply says plain "Yes".
# ---------------------------------------------------------------------------


def _make_update_prompt_query(data: str):
    query = _make_query(data)
    query.from_user.id = "555"
    return query


# ---------------------------------------------------------------------------
# Spec 16, Task 3: the question ABOVE the buttons (``send_update_prompt``'s
# own header) speaks Russian too. Everything after the tap was translated
# above (window E's response); the header text sent alongside the Yes/No
# buttons in the first place was still the English literal
# "Update needs your input:" -- a client would see an English question,
# Russian buttons, then a Russian reply. Verified via the real
# ``send_update_prompt`` call, reading the ``text=`` kwarg captured on the
# mocked ``adapter._bot.send_message``.
# ---------------------------------------------------------------------------


class TestUpdatePromptQuestionIsRussian:
    @pytest.mark.asyncio
    async def test_question_header_is_russian(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()

        # ASCII-only prompt text on purpose: the header this test targets
        # is a fixed literal above the caller-supplied prompt, not
        # something the prompt argument could accidentally satisfy. If the
        # header stayed English, the whole rendered text would still be
        # pure ASCII and _assert_ru would (correctly) fail.
        result = await adapter.send_update_prompt(
            chat_id="12345",
            prompt="Restart the service now?",
            default="",
        )

        assert result.success is True
        text = adapter._bot.send_message.call_args[1]["text"]
        _assert_ru(text)


class TestUpdatePromptQuestionStaysEnglish:
    @pytest.mark.asyncio
    async def test_question_header_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()

        result = await adapter.send_update_prompt(
            chat_id="12345",
            prompt="Restore stashed changes?",
            default="",
        )

        assert result.success is True
        text = adapter._bot.send_message.call_args[1]["text"]
        assert "Update needs your input:" in text


class TestUpdatePromptResponseIsRussian:
    @pytest.mark.asyncio
    async def test_unauthorized_tap_tooltip_is_russian(self, monkeypatch, tmp_path):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _DenyRunner()._handle_message
        query = _make_update_prompt_query("update_prompt:y")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        _assert_ru(query.answer.call_args[1]["text"])
        # Guarded branch -- no response file, no edited message.
        query.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_yes_tooltip_and_body_are_russian_and_match_button_label(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_update_prompt_query("update_prompt:y")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        tooltip = query.answer.call_args[1]["text"]
        _assert_ru(tooltip)
        button_labels = _update_prompt_button_labels()
        assert button_labels["yes"] in tooltip
        body = query.edit_message_text.call_args[1]["text"]
        _assert_ru(body)
        # The label in the final body must be exactly what the button said.
        assert button_labels["yes"] in body

    @pytest.mark.asyncio
    async def test_no_tooltip_and_body_are_russian_and_match_button_label(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_update_prompt_query("update_prompt:n")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        tooltip = query.answer.call_args[1]["text"]
        _assert_ru(tooltip)
        button_labels = _update_prompt_button_labels()
        assert button_labels["no"] in tooltip
        body = query.edit_message_text.call_args[1]["text"]
        _assert_ru(body)
        assert button_labels["no"] in body


class TestUpdatePromptResponseStaysEnglish:
    @pytest.mark.asyncio
    async def test_unauthorized_tap_tooltip_is_english(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        adapter._message_handler = _DenyRunner()._handle_message
        query = _make_update_prompt_query("update_prompt:y")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        assert query.answer.call_args[1]["text"] == "⛔ You are not authorized to answer update prompts."

    @pytest.mark.asyncio
    async def test_yes_tooltip_and_body_are_english_and_match_button_label(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_update_prompt_query("update_prompt:y")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        # The tooltip names the tapped button, not the raw protocol letter:
        # both catalogs now carry the same {label} placeholder. They carried
        # different ones (en {answer}, ru {label}) until the parity guard in
        # tests/agent/test_i18n.py caught it -- a catalog that disagrees with
        # English on format arguments raises KeyError the moment a caller
        # stops passing the now-unused one.
        assert query.answer.call_args[1]["text"] == "Sent '✓ Yes' to the update process."
        body = query.edit_message_text.call_args[1]["text"]
        button_labels = _update_prompt_button_labels()
        assert button_labels["yes"] == "✓ Yes"
        # format_message() renders single-asterisk emphasis as MarkdownV2
        # italics (_..._), same as the pre-fix code -- this was already
        # true before this task, we're only translating the wording.
        assert body == "⚕ Update prompt answered: _✓ Yes_"

    @pytest.mark.asyncio
    async def test_no_tooltip_and_body_are_english_and_match_button_label(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        adapter = _make_adapter()
        adapter._message_handler = _AllowRunner()._handle_message
        query = _make_update_prompt_query("update_prompt:n")
        update, context = _make_callback_update(query)

        await adapter._handle_callback_query(update, context)

        assert query.answer.call_args[1]["text"] == "Sent '✗ No' to the update process."
        body = query.edit_message_text.call_args[1]["text"]
        button_labels = _update_prompt_button_labels()
        assert button_labels["no"] == "✗ No"
        assert body == "⚕ Update prompt answered: _✗ No_"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
