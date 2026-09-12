"""Task 9b: confirmation-window buttons show Russian labels, not English.

The ``ea:`` window (dangerous-command approval, ``send_exec_approval``) was
already translated by spec 9's ``_approval_button_labels()``. Two SIBLING
windows were not, and both are exercised on the ``/update`` path Task 9a
just added confirmation to:

  * ``sc:`` -- the generic slash-command confirmation window
    (``send_slash_confirm``), used by /new, /undo, and now /update.
  * ``update_prompt:`` -- the mid-update Yes/No prompt
    (``send_update_prompt``), used for stash-restore / config-migration
    questions the updater asks interactively.

Same defect shape as the ``ea:`` window before spec 9 fixed it: literal
ASCII button labels ("✅ Approve Once", "✓ Yes", ...) baked into the
adapter instead of read from the locale catalog. Same fix: read them via
``t()`` (``_slash_confirm_button_labels`` / ``_update_prompt_button_labels``
in ``plugins/platforms/telegram/adapter.py``, mirroring
``_approval_button_labels``).

``locales/ru.yaml``'s ``trix.cmd.confirm.prompt`` used to name the English
button labels by word ("Approve Once" / "Always Approve" / "Cancel")
inside its own Russian text. Translating the buttons without also fixing
that text would leave it pointing at labels that no longer exist on
screen -- covered separately by
``tests/gateway/test_run_l10n.py::TestConfirmScreen::test_new_prompt_ru``
(updated alongside this file), not duplicated here.

No source-reading: each of the three real send methods is invoked against
a fake bot, and the actual button labels are recovered from the
``InlineKeyboardMarkup`` handed to the send call -- never by grepping
``adapter.py`` as text. A fourth, undiscovered instance of this defect is
reviewer work (see the Task 9b report), not something a test can search
for without that same forbidden antipattern.

``tests/gateway/conftest.py`` installs a full mock ``telegram`` module at
import time (module-level ``_ensure_telegram_mock()``) regardless of
whether the real ``python-telegram-bot`` package is installed, so the
``InlineKeyboardButton``/``InlineKeyboardMarkup`` names imported inside
``plugins/platforms/telegram/adapter.py`` are ``MagicMock`` auto-attributes,
not the real classes -- calling them does not preserve the ``text`` that
was passed in. Every existing telegram-button test in this repo
(``tests/gateway/test_telegram_approval_buttons.py``,
``test_telegram_slash_confirm.py``) works around this by monkeypatching
those two names, in the adapter module's namespace, to identity/capturing
functions; this file follows the same pattern rather than reading
``bot.send_message``'s captured ``reply_markup`` as a real object.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig


@pytest.fixture(autouse=True)
def _use_russian_ui_language(monkeypatch):
    """This whole file is about the Russian client seeing Russian buttons
    -- ``tests/conftest.py`` pins ``HERMES_LANGUAGE=en`` for the suite by
    default, so every test here overrides it and resets the process-wide
    language cache in ``try/finally`` (pattern from
    ``tests/hermes_cli/test_trix_menu.py``'s ``TestDebugDescriptionMentionsLogs``).
    """
    from agent.i18n import reset_language_cache

    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    reset_language_cache()
    try:
        yield
    finally:
        reset_language_cache()


@pytest.fixture(autouse=True)
def _capture_keyboard_rows(monkeypatch):
    """Replace the adapter module's ``InlineKeyboardButton``/
    ``InlineKeyboardMarkup`` names with capturing identity functions, so
    the rows built by a send method are recoverable as plain button-label
    strings -- see the module docstring for why this is required here
    rather than inspecting a real ``InlineKeyboardMarkup``.

    Returns the list that gets filled with rows (each a list of label
    strings) the moment the send method builds its keyboard.
    """
    captured_rows: list = []
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data: text,
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
        lambda rows: captured_rows.extend(rows) or rows,
    )
    return captured_rows


def _make_adapter() -> TelegramAdapter:
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    adapter._app = MagicMock()
    return adapter


def _flatten(rows) -> list:
    return [label for row in rows for label in row]


def _assert_no_english_button(texts) -> None:
    assert texts, "no buttons captured"
    for text in texts:
        # Judge only the letters -- a bare "✅"/"🔒"/"❌" glyph prefix isn't
        # itself an English word, and stripping it avoids a false pass on
        # "isascii()" for the whole string when the letters are Cyrillic.
        letters = "".join(ch for ch in text if ch.isalpha())
        assert letters, f"button has no letters at all: {text!r}"
        assert not letters.isascii(), f"button label is still English: {text!r}"


# ---------------------------------------------------------------------------
# ea: -- dangerous-command approval (already fixed by spec 9). Pinned here
# too so all three confirmation windows have coverage in one file and a
# future regression is caught regardless of which spec's test survives a
# later refactor.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_approval_buttons_are_russian(_capture_keyboard_rows):
    adapter = _make_adapter()

    result = await adapter.send_exec_approval(
        chat_id="1", command="rm -rf /tmp/x", session_key="s1",
        description="dangerous deletion",
    )

    assert result.success is True
    _assert_no_english_button(_flatten(_capture_keyboard_rows))


# ---------------------------------------------------------------------------
# sc: -- generic slash-command confirmation (/new, /undo, /update).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slash_confirm_three_button_window_is_russian(_capture_keyboard_rows):
    """The default (allow_always=True) shape used by /new and /undo."""
    adapter = _make_adapter()

    result = await adapter.send_slash_confirm(
        chat_id="1", title="/new", message="Подтвердите /new",
        session_key="s1", confirm_id="c1",
    )

    assert result.success is True
    texts = _flatten(_capture_keyboard_rows)
    assert len(texts) == 3, texts  # once / always / cancel
    _assert_no_english_button(texts)


@pytest.mark.asyncio
async def test_slash_confirm_two_button_window_is_russian(_capture_keyboard_rows):
    """/update's own shape (Task 9a, allow_always=False) -- no "always"
    button, but the remaining two must still be Russian."""
    adapter = _make_adapter()

    result = await adapter.send_slash_confirm(
        chat_id="1", title="/update", message="Подтвердите /update",
        session_key="s1", confirm_id="c2", allow_always=False,
    )

    assert result.success is True
    texts = _flatten(_capture_keyboard_rows)
    assert len(texts) == 2, texts  # once / cancel only
    _assert_no_english_button(texts)


# ---------------------------------------------------------------------------
# update_prompt: -- mid-update Yes/No (stash restore, config migration).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_prompt_buttons_are_russian(_capture_keyboard_rows):
    adapter = _make_adapter()

    result = await adapter.send_update_prompt(
        chat_id="1", prompt="Restore stashed changes?",
    )

    assert result.success is True
    texts = _flatten(_capture_keyboard_rows)
    assert len(texts) == 2, texts  # yes / no
    _assert_no_english_button(texts)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
