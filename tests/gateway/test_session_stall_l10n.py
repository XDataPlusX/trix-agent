"""RAF-221: the session-stall notice a Telegram client reads is Russian.

``gateway/session_stall.py::format_session_stall_notification`` used to ship
one English literal ("⚠️ Agent session appears stalled ... Try /new to
reset.") straight to chat — English, naming the internal "Agent", and
pointing at a reset the client didn't ask for. The notice now renders from
``trix.busy.stall_notice``; this module drives the real producer in both
languages, mirroring tests/gateway/test_errors_l10n.py.
"""

from __future__ import annotations

import pytest

from agent import i18n
from gateway.session_stall import format_session_stall_notification


@pytest.fixture(autouse=True)
def _reset_i18n_after():
    yield
    i18n.reset_language_cache()


def _set_lang(monkeypatch, lang: str) -> None:
    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    i18n.reset_language_cache()


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_stall_notice_is_localized(monkeypatch, lang):
    _set_lang(monkeypatch, lang)
    text = format_session_stall_notification(125)
    if lang == "ru":
        assert "зависла" in text
        assert "2 мин" in text  # 125s -> 2 minutes
    else:
        assert "stalled" in text
        assert "2 min ago" in text


def test_stall_notice_ru_names_live_commands_and_no_internal_words(monkeypatch):
    _set_lang(monkeypatch, "ru")
    text = format_session_stall_notification(900)
    assert "/stop" in text and "/new" in text
    for banned in ("Agent", "agent", "Hermes", "reset"):
        assert banned not in text, f"{banned!r} leaked into the stall notice"


def test_stall_notice_minutes_round_up_not_down(monkeypatch):
    """30s idle must read as 1 minute, not 0 (max(1, ...))."""
    _set_lang(monkeypatch, "en")
    assert "1 min" in format_session_stall_notification(30)
