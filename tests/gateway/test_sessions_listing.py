"""Task 9f (``docs/product/plans/2026-09-01-client-command-surface.md``):
``/sessions`` used to default to ``include_unnamed=False`` -- a client who
never used ``/title`` saw an empty list on ``/sessions`` even with real
conversation history, and the only way to see the rest was the undocumented
``/sessions full``. This test seeds a real ``SessionDB`` with one named and
two unnamed sessions and drives the actual gateway handler
(``GatewaySlashCommandsMixin._handle_sessions_command``), not a mock of the
formatter -- the defect was in what gets PASSED to the query, so a test that
stubs the query/formatter (like ``tests/gateway/test_slash_commands_l10n.py``
does for its title-selection tests) would stay green on the bug.

Also verifies (plan Task 9f, "проверить /resume — он резолвит цель по имени
И по номеру — убедиться исполнением, что номера из нового списка ему
подходят") whether ``/resume <number>`` — which ``/sessions <number>``
delegates to verbatim — resolves against the SAME numbering the client just
saw on ``/sessions``.
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


def _event(text, *, chat_id="chat1", user_id="u1"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm", user_id=user_id,
        ),
    )


def _make_runner(session_db, event):
    """A GatewayRunner real enough to drive both `_handle_sessions_command`
    and `_handle_resume_command` against a real on-disk SessionDB -- mirrors
    `tests/gateway/test_resume_command.py::_make_runner`, extended with the
    extra collaborators `_handle_sessions_command` needs
    (`async_session_store`, `_normalize_source_for_session_key`)."""
    from gateway.run import GatewayRunner
    from hermes_state import AsyncSessionDB

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = SimpleNamespace(platforms={})
    runner._voice_mode = {}
    runner._session_db = AsyncSessionDB(session_db)
    runner._running_agents = {}
    runner._is_user_authorized = lambda _source: True

    session_key = build_session_key(event.source)
    mock_session_entry = MagicMock()
    mock_session_entry.session_id = "current_session_not_in_list"
    mock_session_entry.session_key = session_key
    mock_store = MagicMock()
    mock_store.get_or_create_session.return_value = mock_session_entry
    mock_store.load_transcript.return_value = []
    mock_store.switch_session.return_value = mock_session_entry
    runner.session_store = mock_store
    # `async_session_store` is a real property on GatewayRunner (no setter) --
    # it lazily wraps whatever `self.session_store` is (`AsyncSessionStore`),
    # so setting `session_store` above is enough; no fake needed here.
    runner._normalize_source_for_session_key = lambda source: source
    return runner


@pytest.fixture
def seeded_db(tmp_path):
    """One named + two unnamed sessions, all in the same gateway lane, each
    with a distinct first user message (for the preview) and a distinct
    `started_at` (for the unnamed-row date label)."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    event = _event("/sessions")
    lane_key = build_session_key(event.source)
    now = time.time()

    def _set_started_at(session_id: str, started_at: float) -> None:
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?", (started_at, session_id)
        )
        db._conn.commit()

    db.create_session(
        "sess_named", "telegram", session_key=lane_key,
        user_id="u1", chat_id="chat1",
    )
    db.append_message("sess_named", "user", "Помоги составить смету на ремонт")
    db.set_session_title("sess_named", "Смета на ремонт")
    _set_started_at("sess_named", now - 300)

    db.create_session(
        "sess_unnamed_older", "telegram", session_key=lane_key,
        user_id="u1", chat_id="chat1",
    )
    db.append_message("sess_unnamed_older", "user", "Когда приедет курьер с документами?")
    _set_started_at("sess_unnamed_older", now - 7200)

    db.create_session(
        "sess_unnamed_newer", "telegram", session_key=lane_key,
        user_id="u1", chat_id="chat1",
    )
    db.append_message("sess_unnamed_newer", "user", "Сколько стоит доставка в другой город")
    _set_started_at("sess_unnamed_newer", now - 60)

    return db, event


class TestSessionsShowsEverythingByDefault:
    @pytest.mark.asyncio
    async def test_no_args_shows_all_three(self, seeded_db):
        db, event = seeded_db
        runner = _make_runner(db, event)
        result = await runner._handle_sessions_command(event)

        assert "sess_named" in result
        assert "sess_unnamed_older" in result
        assert "sess_unnamed_newer" in result
        assert "Смета на ремонт" in result

    @pytest.mark.asyncio
    async def test_unnamed_rows_show_a_date_and_the_start_of_the_first_message(self, seeded_db):
        db, event = seeded_db
        runner = _make_runner(db, event)
        result = await runner._handle_sessions_command(event)

        # Unnamed rows must not render as a bare, indistinguishable "—" —
        # each carries its own date (see _format_unnamed_session_label) and
        # the first user message's opening words (the existing `preview`
        # field list_sessions_rich already computes).
        assert "—" not in result.split("\n", 2)[-1] or "**—**" not in result
        assert "Когда приедет курьер" in result
        assert "Сколько стоит доставка" in result

    @pytest.mark.asyncio
    async def test_response_is_russian_by_default(self, seeded_db, monkeypatch):
        from agent import i18n

        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        try:
            db, event = seeded_db
            runner = _make_runner(db, event)
            result = await runner._handle_sessions_command(event)
            assert "Разговоры" in result
            assert "Sessions" not in result
            assert "Resume:" not in result and "More:" not in result
        finally:
            i18n.reset_language_cache()


class TestResumeNumberingAgainstTheNewSessionsList:
    """docs/product/plans/2026-09-01-client-command-surface.md Task 9f:
    "проверить /resume — убедиться исполнением, что номера из нового
    списка ему подходят". `/sessions` lists ALL three sessions (named +
    unnamed) by default; `/sessions <n>` delegates verbatim to `/resume <n>`
    (`_handle_sessions_command`'s `if target:` branch).

    `_handle_resume_command`'s numeric resolver now shares its query with
    `/sessions`' own listing (`_numbered_session_rows` in
    `gateway/slash_commands.py`) instead of the separate, titled-only
    `_list_titled_sessions()` it still uses for the bare (name-less)
    `/resume` listing. Before that fix, row 1 below (an UNNAMED session)
    resolved via `/resume 1` to `sess_named` instead -- a different
    session than the one the client just read as row 1. This test now
    asserts the fixed invariant: every number `/sessions` renders resolves,
    via `/resume <n>`, to that exact same session id.
    """

    @pytest.mark.asyncio
    async def test_sessions_listing_order_matches_resume_numeric_resolution(self, seeded_db):
        db, event = seeded_db
        runner = _make_runner(db, event)

        listing = await runner._handle_sessions_command(event)
        # `/sessions` (all three, newest first): 1=sess_unnamed_newer,
        # 2=sess_named, 3=sess_unnamed_older.
        lines = [l for l in listing.split("\n") if l and l[0].isdigit()]
        assert len(lines) == 3
        assert lines[0].startswith("1.")
        assert "sess_unnamed_newer" in lines[0]
        assert lines[1].startswith("2.")
        assert "sess_named" in lines[1]
        assert lines[2].startswith("3.")
        assert "sess_unnamed_older" in lines[2]

        # Each row's session id is backtick-quoted by
        # `format_gateway_session_listing` -- pull it out so the assertion
        # below checks the real routing target, not just substring presence
        # of the id somewhere in the line.
        row_ids = [re.search(r"`([^`]+)`", line).group(1) for line in lines]
        assert row_ids == ["sess_unnamed_newer", "sess_named", "sess_unnamed_older"]

        # The numbering fix: every number `/sessions` just rendered must
        # resolve, via `/resume <n>`, to that SAME session -- named or not.
        for idx, expected_id in enumerate(row_ids, start=1):
            runner.session_store.switch_session.reset_mock()
            resumed = await runner._handle_resume_command(_event(f"/resume {idx}"))
            assert runner.session_store.switch_session.call_args is not None, (idx, resumed)
            actual_id = runner.session_store.switch_session.call_args[0][1]
            assert actual_id == expected_id, (idx, expected_id, actual_id, resumed)


class TestNumericResumeStaysScopedToCaller:
    """`_numbered_session_rows` (the query `/resume <n>` now shares with
    `/sessions`) must not let a numeric guess reach another user's/chat's
    session -- the enumeration+IDOR half of the pre-existing `/resume`
    scoping guard, now exercised through the NEW shared query path rather
    than the old titled-only one."""

    @pytest.mark.asyncio
    async def test_numeric_resume_never_resolves_a_foreign_session(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        event = _event("/sessions")
        lane_key = build_session_key(event.source)

        # Caller's own session -- unnamed, so it only shows up at all
        # because of the Task 9f "show everything" default.
        db.create_session(
            "mine_unnamed", "telegram", session_key=lane_key,
            user_id="u1", chat_id="chat1",
        )
        db.append_message("mine_unnamed", "user", "Мой собственный разговор")

        # A different user's chat, created AFTER the caller's session so it
        # would sort ahead of it if scoping ever leaked (most-recent-first).
        db.create_session(
            "foreign_newer", "telegram",
            session_key="agent:main:telegram:dm:foreign-user",
            user_id="foreign-user", chat_id="foreign-chat",
        )
        db.append_message("foreign_newer", "user", "Чужой разговор, новее")
        db.set_session_title("foreign_newer", "Foreign Work")

        runner = _make_runner(db, event)

        resumed = await runner._handle_resume_command(_event("/resume 1"))
        assert runner.session_store.switch_session.call_args is not None
        assert runner.session_store.switch_session.call_args[0][1] == "mine_unnamed"
        assert "Foreign Work" not in resumed
        assert "foreign_newer" not in resumed

        # Only one session is visible to this caller -- a second guess must
        # fail closed rather than ever reaching the foreign session.
        runner.session_store.switch_session.reset_mock()
        out_of_range = await runner._handle_resume_command(_event("/resume 2"))
        assert runner.session_store.switch_session.call_args is None
        assert "Foreign Work" not in out_of_range
        assert "foreign_newer" not in out_of_range
        db.close()


class TestNumericResumeAppliesRowVisibilityCheck:
    """Proves `_resume_row_visible` is not just present in the numeric
    resolver's source but actually has effect: with it stubbed to reject,
    the caller's own (in-lane, would-otherwise-be-visible) session must
    disappear from `/resume <n>`'s candidate set instead of resolving."""

    @pytest.mark.asyncio
    async def test_row_visible_false_blocks_numeric_resolution(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        event = _event("/resume 1")
        lane_key = build_session_key(event.source)
        db.create_session(
            "would_be_row_1", "telegram", session_key=lane_key,
            user_id="u1", chat_id="chat1",
        )
        db.append_message("would_be_row_1", "user", "Разговор в своей ветке")

        from unittest.mock import AsyncMock

        runner = _make_runner(db, event)
        # Same call signature `_resume_row_visible` is invoked with in
        # `_numbered_session_rows` -- reject every row unconditionally.
        runner._resume_row_visible = AsyncMock(return_value=False)

        result = await runner._handle_resume_command(event)

        assert runner.session_store.switch_session.call_args is None
        assert "out of range" in result.lower()
        db.close()
