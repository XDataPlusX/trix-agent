"""Cross-profile isolation contract for the ``session_search`` tool.

Each Trix profile is meant to be a fully isolated instance — its own state.db,
its own memory. ``session_search`` used to let any caller open ANOTHER
profile's ``state.db`` — via an embedded ``session_id="<profile>/<id>"``
value, or by finding one implicitly through scanning every profile for a bare
``session_id`` — which breaks that isolation for a host-side messaging-gateway
agent: the sales department's agent could read the accounting department's
conversations just by being asked to.

Note: the explicit ``profile=`` keyword argument tested below was never
reachable from the live agent loop in the first place (the tool-call
interceptors that would forward it never pass it through, and it's outside
``execute_code``'s tool allow-list) — it exists for direct callers such as
the desktop app resolving a dragged-in ``@session:<profile>/<id>`` link. The
gate still covers it defensively, and these tests exercise it directly since
that is this module's own calling convention, but the genuinely
agent-reachable leaks this fix closed were the embedded
``session_id="<profile>/<id>"`` form and the bare-id all-profile scan.

``session_search.allow_cross_profile`` (default ``False``, see
``DEFAULT_CONFIG`` in ``hermes_cli/config_defaults.py``) closes that. These
tests build two REAL profiles with REAL SQLite session databases (no mocking
of the DB layer) and prove isolation by actually trying to read across them,
both with the gate off (default) and explicitly turned on via a real
``config.yaml``.
"""

import json
from pathlib import Path

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import get_session_search_schema, session_search


def _seed(db_path: Path, session_id: str, text: str) -> None:
    db = SessionDB(db_path=db_path)
    db.create_session(session_id, source="cli")
    db.append_message(session_id, role="user", content=text)
    db._conn.commit()
    db.close()


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    """Two sibling profiles under one HERMES_HOME root: 'sales' (active) and
    'accounting' (the profile that must stay out of reach).

    HERMES_HOME is pointed at .../root/profiles/sales, which
    ``get_active_profile_name()`` resolves to the profile name "sales" (see
    hermes_cli/profiles.py — profile identity is HOME-anchored, derived from
    where HERMES_HOME points, not from an explicit setting).
    """
    root = tmp_path / "hermes_root"
    sales_home = root / "profiles" / "sales"
    accounting_home = root / "profiles" / "accounting"
    sales_home.mkdir(parents=True)
    accounting_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(sales_home))

    _seed(sales_home / "state.db", "s_sales", "quarterly sales pipeline notes")
    _seed(accounting_home / "state.db", "s_acct", "payroll and ledger notes")

    from hermes_cli import profiles as profiles_mod

    assert profiles_mod.get_active_profile_name() == "sales"
    assert profiles_mod.profile_exists("accounting")

    own_db = SessionDB(db_path=sales_home / "state.db")
    return {
        "root": root,
        "sales_home": sales_home,
        "accounting_home": accounting_home,
        "own_db": own_db,
    }


def _enable_cross_profile(root: Path) -> None:
    """Write a real config.yaml turning the gate on, exercising the actual
    load_config_readonly() path rather than mocking the config layer."""
    config_path = root / "profiles" / "sales" / "config.yaml"
    config_path.write_text(
        "session_search:\n  allow_cross_profile: true\n",
        encoding="utf-8",
    )


class TestCrossProfileGateDefaultOff:
    def test_explicit_profile_arg_is_refused(self, two_profiles):
        result = json.loads(
            session_search(
                session_id="s_acct",
                profile="accounting",
                db=two_profiles["own_db"],
            )
        )
        assert result.get("success") is False
        error = result["error"].lower()
        # Actionable: names the config key so a legitimate desktop user can
        # find and flip it.
        assert "session_search.allow_cross_profile" in error
        assert "config.yaml" in error

    def test_bare_session_id_does_not_leak_across_profiles(self, two_profiles):
        # No `profile` argument — the accounting session isn't in the sales
        # db, so the tool's cross-profile fallback scan must NOT find it.
        result = json.loads(
            session_search(session_id="s_acct", db=two_profiles["own_db"])
        )
        assert result.get("success") is False
        assert "profile" not in result

    def test_own_profile_read_with_no_profile_arg_still_works(self, two_profiles):
        result = json.loads(
            session_search(session_id="s_sales", db=two_profiles["own_db"])
        )
        assert result["success"] is True
        assert result["session_id"] == "s_sales"

    def test_own_profile_read_naming_the_active_profile_explicitly_still_works(
        self, two_profiles
    ):
        """Passing profile="sales" while the agent already runs as "sales" is
        not a cross-profile read at all and must not be refused."""
        result = json.loads(
            session_search(
                session_id="s_sales",
                profile="sales",
                db=two_profiles["own_db"],
            )
        )
        assert result["success"] is True
        assert result["session_id"] == "s_sales"

    def test_schema_omits_profile_parameter(self, two_profiles):
        schema = get_session_search_schema()
        assert "profile" not in schema["parameters"]["properties"]

    def test_schema_description_drops_profile_kwarg_when_gate_closed(self, two_profiles):
        """When the gate is closed, the prose must not teach the model a
        capability the schema no longer exposes: a strict provider rejects
        an unknown-arg call, and a lenient one turns the tool's own
        `@session:` links into a guaranteed tool_error."""
        schema = get_session_search_schema()
        description = schema["description"]
        assert 'profile="work"' not in description
        assert "split the value on" not in description
        assert "@session:<id>" in description

    def test_get_active_profile_name_exception_fails_closed_not_to_default(
        self, two_profiles, monkeypatch
    ):
        """If get_active_profile_name() raises during the bare-session_id
        fallback scan, the scan must target the CURRENT HERMES_HOME (the
        profile this agent is actually running as), never a hardcoded
        "default" profile — the old code's `active = "default"` fallback
        would silently redirect the scan at a different profile's database
        (here: the root's own state.db, which is what get_profile_dir(
        "default") resolves to for this HERMES_HOME layout) instead of
        failing closed.
        """
        from hermes_cli import profiles as profiles_mod

        def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(profiles_mod, "get_active_profile_name", _boom)

        # Seed a session at exactly the path the buggy "default" fallback
        # would have scanned (get_profile_dir("default") for this
        # HERMES_HOME layout resolves to the profiles root, not sales/).
        leak_db_path = profiles_mod.get_profile_dir("default") / "state.db"
        assert leak_db_path != two_profiles["sales_home"] / "state.db"
        _seed(leak_db_path, "s_leak", "a session outside the active profile")

        result = json.loads(
            session_search(session_id="s_leak", db=two_profiles["own_db"])
        )
        assert result.get("success") is False
        assert "profile" not in result


class TestCrossProfileGateEnabled:
    def test_explicit_profile_arg_is_allowed(self, two_profiles):
        _enable_cross_profile(two_profiles["root"])
        result = json.loads(
            session_search(
                session_id="s_acct",
                profile="accounting",
                db=two_profiles["own_db"],
            )
        )
        assert result["success"] is True
        assert result["session_id"] == "s_acct"

    def test_bare_session_id_scan_reaches_other_profiles(self, two_profiles):
        _enable_cross_profile(two_profiles["root"])
        result = json.loads(
            session_search(session_id="s_acct", db=two_profiles["own_db"])
        )
        assert result["success"] is True
        assert result["profile"] == "accounting"

    def test_own_profile_read_still_works(self, two_profiles):
        _enable_cross_profile(two_profiles["root"])
        result = json.loads(
            session_search(session_id="s_sales", db=two_profiles["own_db"])
        )
        assert result["success"] is True
        assert result["session_id"] == "s_sales"

    def test_schema_includes_profile_parameter(self, two_profiles):
        _enable_cross_profile(two_profiles["root"])
        schema = get_session_search_schema()
        assert "profile" in schema["parameters"]["properties"]
