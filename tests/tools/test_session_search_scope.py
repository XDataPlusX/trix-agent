"""Conversation-scoped session_search discovery (spec 22, owner decision
2026-09-14): search the exact current conversation first, widen to the whole
profile only when that comes up short. No middle "same chat, other topics"
tier. Identity is the stable technical ``session_key`` (platform / account /
chat / thread-topic) — display names are never identity.

All scenarios run on synthetic SessionDB fixtures: seeded sessions with the
same routing columns the gateway writes at insert time (``session_key``,
``chat_id``, ``chat_type``, ``thread_id``, ``parent_session_id``), no LLM
calls, no live data.
"""

import json
import time

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import SESSION_SEARCH_SCHEMA, session_search


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


# Telegram forum-group session keys, exactly as gateway.session.build_session_key
# composes them: agent:main:telegram:group:<chat_id>:<thread_id>.
TOPIC_42_KEY = "agent:main:telegram:group:-100777:42"
TOPIC_43_KEY = "agent:main:telegram:group:-100777:43"
OTHER_GROUP_KEY = "agent:main:telegram:group:-100999:1"
DM_KEY = "agent:main:telegram:dm:555001"


def _mk_gateway_session(
    db,
    sid,
    *,
    session_key,
    chat_id,
    chat_type="group",
    thread_id=None,
    parent_session_id=None,
    end_reason=None,
    title=None,
    display_name=None,
    started_at=None,
):
    """Insert a session row the way the gateway does: routing identity at
    insert time, optional explicit end boundary (rotation)."""
    db.create_session(
        sid,
        source="telegram",
        session_key=session_key,
        chat_id=str(chat_id),
        chat_type=chat_type,
        thread_id=str(thread_id) if thread_id is not None else None,
        parent_session_id=parent_session_id,
        display_name=display_name,
    )
    if end_reason is not None:
        db.end_session(sid, end_reason)
    if started_at is not None or title is not None:
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
            (started_at if started_at is not None else time.time(), title, sid),
        )
        db._conn.commit()


def _say(db, sid, *contents):
    for c in contents:
        db.append_message(sid, role="user", content=c)
    db._conn.commit()


def _rotated_topic_world(db):
    """The UCAS scenario: one Telegram forum topic, rotated by the 72h idle
    reset; a second topic in the same group; an unrelated group; a DM."""
    now = int(time.time())
    # Oldest generation of topic 42 — closed by idle rotation.
    _mk_gateway_session(
        db, "t42_gen1",
        session_key=TOPIC_42_KEY, chat_id=-100777, thread_id=42,
        end_reason="idle", title="UCAS заявка", started_at=now - 50000,
    )
    _say(db, "t42_gen1", "UCAS заявление подано, статус waiting")
    # Second generation of topic 42 — also rotated later.
    _mk_gateway_session(
        db, "t42_gen2",
        session_key=TOPIC_42_KEY, chat_id=-100777, thread_id=42,
        parent_session_id="t42_gen1",
        end_reason="session_reset", title="UCAS приём", started_at=now - 30000,
    )
    _say(db, "t42_gen2", "UCAS статус сменился на received")
    # Current live session of topic 42 — successor of gen2.
    _mk_gateway_session(
        db, "t42_now",
        session_key=TOPIC_42_KEY, chat_id=-100777, thread_id=42,
        parent_session_id="t42_gen2", started_at=now - 100,
    )
    _say(db, "t42_now", "привет, напомни как там UCAS")
    # Another topic of the SAME group (43) with heavy lexical overlap.
    _mk_gateway_session(
        db, "t43",
        session_key=TOPIC_43_KEY, chat_id=-100777, thread_id=43,
        title="UCAS документы", started_at=now - 20000,
    )
    _say(db, "t43", "UCAS документы все собраны")
    # Unrelated group.
    _mk_gateway_session(
        db, "og",
        session_key=OTHER_GROUP_KEY, chat_id=-100999, thread_id=1,
        title="Огород", started_at=now - 10000,
    )
    _say(db, "og", " UCAS нет, зато огурцы выросли ")
    # DM with the same user, also lexically overlapping.
    _mk_gateway_session(
        db, "dm1",
        session_key=DM_KEY, chat_id=555001, chat_type="dm",
        title="Личка", started_at=now - 5000,
    )
    _say(db, "dm1", "UCAS: я отправил апелляцию в личке")


# =========================================================================
# SQL filter API (PR-1 contract: equality on stored identity columns)
# =========================================================================

class TestSearchMessagesScopeFilters:
    def _seed(self, db):
        _rotated_topic_world(db)

    def test_session_key_filter_matches_only_that_conversation(self, db):
        self._seed(db)
        rows = db.search_messages(query="UCAS", session_key=TOPIC_42_KEY)
        owners = {r["session_id"] for r in rows}
        assert owners == {"t42_gen1", "t42_gen2", "t42_now"}

    def test_chat_id_filter_spans_topics_of_one_chat(self, db):
        self._seed(db)
        rows = db.search_messages(query="UCAS", chat_id="-100777")
        owners = {r["session_id"] for r in rows}
        assert "t43" in owners
        assert all(o in {"t42_gen1", "t42_gen2", "t42_now", "t43"} for o in owners)

    def test_thread_id_filter_narrows_to_one_topic(self, db):
        self._seed(db)
        rows = db.search_messages(query="UCAS", chat_id="-100777", thread_id="43")
        owners = {r["session_id"] for r in rows}
        assert owners == {"t43"}

    def test_no_filters_is_profile_wide(self, db):
        self._seed(db)
        rows = db.search_messages(query="UCAS")
        owners = {r["session_id"] for r in rows}
        assert "dm1" in owners and "og" in owners

    def test_legacy_null_scope_rows_still_match_unfiltered(self, db):
        _rotated_topic_world(db)
        db.create_session("legacy_cli", source="cli")
        _say(db, "legacy_cli", "старый UCAS черновик из CLI")
        rows = db.search_messages(query="UCAS", session_key=TOPIC_42_KEY)
        assert "legacy_cli" not in {r["session_id"] for r in rows}
        rows_all = db.search_messages(query="UCAS")
        assert "legacy_cli" in {r["session_id"] for r in rows_all}


# =========================================================================
# Scope ladder: auto / conversation / global
# =========================================================================

class TestConversationTier:
    def test_same_topic_found_through_rotation_with_auto_scope(self, db):
        """Core scenario: 'какой статус?' after the 72h reset finds the old
        generations of THIS topic; response says conversation scope. The
        rotation chain dedupes into one lineage entry (by design), with the
        lineage root exposed via parent_session_id."""
        _rotated_topic_world(db)
        result = json.loads(session_search(
            query="UCAS статус", db=db, current_session_id="t42_now",
        ))
        assert result["success"] is True
        assert result["scope"] == "conversation"
        assert result["count"] >= 1
        hits = [
            r for r in result["results"]
            if r["session_id"] in {"t42_gen1", "t42_gen2"}
        ]
        assert hits, "rotation ancestors of this topic must be discoverable"
        entry = hits[0]
        assert entry.get("parent_session_id", entry["session_id"]) == "t42_gen1"
        for r in result["results"]:
            assert r["scope"] == "conversation"

    def test_other_topic_excluded_from_conversation_scope(self, db):
        _rotated_topic_world(db)
        result = json.loads(session_search(
            query="UCAS", db=db, current_session_id="t42_now",
            scope="conversation",
        ))
        assert result["scope"] == "conversation"
        sids = {r["session_id"] for r in result["results"]}
        assert "t43" not in sids
        assert "dm1" not in sids
        assert "og" not in sids

    def test_auto_stays_local_when_local_fills_the_limit(self, db):
        """Two prior generations of this topic + a third sibling lineage: with
        limit=1 the conversation tier is 'full', so no widening happens even
        though other topics match lexically."""
        _rotated_topic_world(db)
        result = json.loads(session_search(
            query="UCAS", db=db, current_session_id="t42_now", limit=1,
        ))
        assert result["scope"] == "conversation"
        assert "scope_expanded" not in result
        sids = {r["session_id"] for r in result["results"]}
        assert sids <= {"t42_gen1", "t42_gen2"}

    def test_dm_conversation_tier_through_rotation(self, db):
        now = int(time.time())
        _mk_gateway_session(
            db, "dm_old", session_key=DM_KEY, chat_id=555001, chat_type="dm",
            end_reason="idle", started_at=now - 40000,
        )
        _say(db, "dm_old", "апелляция по UCAS отправлена скан завтра")
        _mk_gateway_session(
            db, "dm_now", session_key=DM_KEY, chat_id=555001, chat_type="dm",
            parent_session_id="dm_old", started_at=now - 10,
        )
        _say(db, "dm_now", "что с апелляцией?")
        result = json.loads(session_search(
            query="апелляция UCAS", db=db, current_session_id="dm_now",
        ))
        assert result["scope"] == "conversation"
        sids = {r["session_id"] for r in result["results"]}
        assert "dm_old" in sids

    def test_same_named_groups_are_distinct_conversations(self, db):
        """Two groups both called 'Клуб' — names are display-only; the filter
        is the numeric chat identity in the session key."""
        now = int(time.time())
        key_a = "agent:main:telegram:group:-111:1"
        key_b = "agent:main:telegram:group:-222:1"
        _mk_gateway_session(
            db, "club_a", session_key=key_a, chat_id=-111, thread_id=1,
            display_name="Клуб", title="Встреча пятница", started_at=now - 9000,
        )
        _say(db, "club_a", "встреча клуба в пятницу")
        _mk_gateway_session(
            db, "club_b", session_key=key_b, chat_id=-222, thread_id=1,
            display_name="Клуб", title="Встреча суббота", started_at=now - 8000,
        )
        _say(db, "club_b", "встреча клуба в субботу")
        _mk_gateway_session(
            db, "club_a_now", session_key=key_a, chat_id=-111, thread_id=1,
            display_name="Клуб", started_at=now - 5,
        )
        _say(db, "club_a_now", "когда встреча клуба?")
        result = json.loads(session_search(
            query="встреча клуба", db=db, current_session_id="club_a_now",
            scope="conversation",
        ))
        sids = {r["session_id"] for r in result["results"]}
        assert "club_a" in sids
        assert "club_b" not in sids

    def test_platform_neutral_identity_discord_thread(self, db):
        """The conversation tier keys on session_key, not on any
        Telegram-specific column — a Discord thread works identically."""
        now = int(time.time())
        dkey = "agent:main:discord:thread:-700300400:-700300400"
        _mk_gateway_session(
            db, "d_old", session_key=dkey, chat_id=-700300400,
            chat_type="group", end_reason="idle", started_at=now - 40000,
        )
        _say(db, "d_old", "the deploy runbook lives in this thread")
        _mk_gateway_session(
            db, "d_now", session_key=dkey, chat_id=-700300400,
            chat_type="group", parent_session_id="d_old", started_at=now - 5,
        )
        _say(db, "d_now", "where is the deploy runbook?")
        result = json.loads(session_search(
            query="deploy runbook", db=db, current_session_id="d_now",
        ))
        assert result["scope"] == "conversation"
        assert {r["session_id"] for r in result["results"]} == {"d_old"}


class TestGlobalFallback:
    def test_auto_expands_to_profile_when_local_empty(self, db):
        """Local tier empty (nothing in this topic matches) → widen to the
        profile, mark the response global+expanded, and keep the user's other
        conversations reachable."""
        _rotated_topic_world(db)
        result = json.loads(session_search(
            query="огурцы", db=db, current_session_id="t42_now",
        ))
        assert result["scope"] == "global"
        assert result.get("scope_expanded") is True
        assert "scope_note" in result
        sids = {r["session_id"] for r in result["results"]}
        assert "og" in sids
        for r in result["results"]:
            assert r["scope"] == "global"

    def test_short_but_nonempty_local_tier_is_trusted(self, db):
        """Owner contract: this topic first, the whole bot only when the topic
        has NOTHING. A single strong local match is not auto-widened — other
        topics of the same chat never leak into a default search."""
        _rotated_topic_world(db)
        # "received" matches only this topic's second generation.
        result = json.loads(session_search(
            query="received", db=db, current_session_id="t42_now", limit=3,
        ))
        assert result["scope"] == "conversation"
        assert "scope_expanded" not in result
        sids = {r["session_id"] for r in result["results"]}
        assert sids <= {"t42_gen1", "t42_gen2"}
        assert result["count"] >= 1

    def test_explicit_global_searches_everything_directly(self, db):
        _rotated_topic_world(db)
        result = json.loads(session_search(
            query="UCAS", db=db, current_session_id="t42_now", scope="global",
            limit=6,
        ))
        assert result["scope"] == "global"
        assert "scope_expanded" not in result
        assert "scope_note" in result
        sids = {r["session_id"] for r in result["results"]}
        assert "t43" in sids
        assert "dm1" in sids

    def test_explicit_global_still_finds_rotated_ancestors(self, db):
        """The rotation escape is not tier-specific: a search-everywhere
        request must not lose THIS topic's own rotated generations."""
        _rotated_topic_world(db)
        result = json.loads(session_search(
            query="UCAS статус", db=db, current_session_id="t42_now",
            scope="global",
        ))
        sids = {r["session_id"] for r in result["results"]}
        assert "t42_gen1" in sids

    def test_legacy_null_scope_rows_reachable_via_fallback(self, db):
        _rotated_topic_world(db)
        db.create_session("legacy_cli", source="cli")
        _say(db, "legacy_cli", "старый UCAS черновик из CLI")
        # Conversation tier: legacy row has no session_key — excluded.
        local = json.loads(session_search(
            query="черновик", db=db, current_session_id="t42_now",
            scope="conversation",
        ))
        assert {r["session_id"] for r in local["results"]} == set()
        # Auto: local empty → global fallback finds it.
        auto = json.loads(session_search(
            query="черновик", db=db, current_session_id="t42_now",
        ))
        assert auto["scope"] == "global"
        assert "legacy_cli" in {r["session_id"] for r in auto["results"]}

    def test_no_conversation_identity_degrades_to_global(self, db):
        """CLI/legacy current session: no session_key → no conversation tier,
        behaviour identical to pre-scoped search."""
        _rotated_topic_world(db)
        db.create_session("cli_now", source="cli")
        _say(db, "cli_now", "что было по UCAS?")
        result = json.loads(session_search(
            query="UCAS", db=db, current_session_id="cli_now",
        ))
        assert result["scope"] == "global"
        assert "scope_expanded" not in result
        assert result["count"] >= 1

    def test_conversation_scope_without_identity_falls_back_to_global(self, db):
        _rotated_topic_world(db)
        db.create_session("cli_now", source="cli")
        result = json.loads(session_search(
            query="UCAS", db=db, current_session_id="cli_now",
            scope="conversation",
        ))
        assert result["scope"] == "global"


class TestProfileBoundary:
    def test_global_scope_stays_inside_the_profile_db(self, tmp_path, monkeypatch):
        """Hard tenant boundary: scope=global means 'all sessions of THIS
        profile', never a hop into another profile's state.db."""
        root = tmp_path / "hermes_root"
        sales_home = root / "profiles" / "sales"
        accounting_home = root / "profiles" / "accounting"
        sales_home.mkdir(parents=True)
        accounting_home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(sales_home))

        sales_db = SessionDB(sales_home / "state.db")
        acct_db = SessionDB(accounting_home / "state.db")
        now = int(time.time())
        for db, sid in ((sales_db, "s_sales"), (acct_db, "s_acct")):
            db.create_session(
                sid, source="telegram",
                session_key="agent:main:telegram:group:-100777:42",
                chat_id="-100777", chat_type="group", thread_id="42",
            )
        _say(sales_db, "s_sales", "sales UCAS notes in topic 42")
        _say(acct_db, "s_acct", "accounting UCAS notes in topic 42")

        result = json.loads(session_search(
            query="UCAS", db=sales_db, current_session_id="s_sales",
            scope="global",
        ))
        assert result["success"] is True
        assert "s_acct" not in {r["session_id"] for r in result["results"]}
        sales_db.close()
        acct_db.close()


class TestOtherShapesUnaffected:
    """scope is a discovery-shape parameter only: scroll / read / browse must
    behave exactly as before."""

    def test_scroll_ignores_scope(self, db):
        _rotated_topic_world(db)
        msgs = db.get_messages("t42_gen1")
        anchor = msgs[0]["id"]
        result = json.loads(session_search(
            db=db, session_id="t42_gen1", around_message_id=anchor,
            window=5, scope="global",
        ))
        assert result["success"] is True
        assert result["mode"] == "scroll"
        assert "scope" not in result

    def test_read_ignores_scope(self, db):
        _rotated_topic_world(db)
        result = json.loads(session_search(
            db=db, session_id="t42_gen1", scope="conversation",
        ))
        assert result["success"] is True
        assert result["mode"] == "read"

    def test_browse_ignores_scope(self, db):
        _rotated_topic_world(db)
        result = json.loads(session_search(db=db, scope="global"))
        assert result["success"] is True
        assert result["mode"] == "browse"
        assert "scope" not in result


class TestSchemaContract:
    def test_scope_param_is_a_three_value_enum(self):
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        assert params["scope"]["enum"] == ["auto", "conversation", "global"]
        assert params["scope"]["default"] == "auto"

    def test_registry_handler_forwards_scope(self, db, monkeypatch):
        """The registered dispatch path (model_tools → registry handler)
        forwards the scope kwarg into the tool."""
        from tools.registry import registry

        seen = {}

        def _capture(**kw):
            seen.update(kw)
            return json.dumps({"success": True, "mode": "discover", "stub": True})

        monkeypatch.setattr("tools.session_search_tool.session_search", _capture)
        entry = registry.get_entry("session_search")
        assert entry is not None
        entry.handler({"query": "UCAS", "scope": "conversation"}, db=db)
        assert seen.get("scope") == "conversation"
