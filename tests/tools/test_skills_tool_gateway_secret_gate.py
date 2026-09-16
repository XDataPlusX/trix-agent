"""Spec 19, Ruling 2 — the messaging-gateway secret-capture early return.

``_capture_required_environment_variables`` used to refuse ANY gateway
surface outright (short-circuiting to the "unsupported" hint) unless the
process-global ``HERMES_INTERACTIVE`` flag was set. Ruling 2 replaces that
with a check on whether the CURRENT SESSION has a capture record registered
by ``TurnRunner.run_sync`` (Ruling 1) — never a process-global flag, and
never a different session's registration.
"""

import json
import os
from unittest.mock import patch

import pytest

import tools.skills_tool as skills_tool_module
from tools.skills_tool import skill_view


def _make_skill(skills_dir, name, frontmatter_extra=""):
    """Minimal skill directory — mirrors tests/tools/test_skills_tool.py's helper."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        f"name: {name}\n"
        f"description: Description for {name}.\n"
        f"{frontmatter_extra}"
        "---\n\n"
        f"# {name}\n\nStep 1: Do the thing.\n"
    )
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir


def _skill_with_required_env(tmp_path, name="needs-key"):
    _make_skill(
        tmp_path,
        name,
        frontmatter_extra=(
            "required_environment_variables:\n"
            "  - name: TAVILY_API_KEY\n"
            "    prompt: Tavily API key\n"
        ),
    )
    return name


@pytest.fixture(autouse=True)
def _clear_secret_capture_state():
    from tools import secret_capture_gateway as scg

    with scg._lock:
        scg._entries.clear()
        scg._session_index.clear()
        scg._notify_cbs.clear()
    yield
    with scg._lock:
        scg._entries.clear()
        scg._session_index.clear()
        scg._notify_cbs.clear()


def _as_gateway_surface(monkeypatch, session_key: str = "telegram:12345"):
    """Simulate an active messaging-gateway turn bound to ``session_key``."""
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    from gateway.session_context import set_session_vars

    tokens = set_session_vars(platform="telegram", session_key=session_key)
    return tokens


class TestGatewaySurfaceWithoutCaptureRecord:
    def test_still_refuses_when_no_session_capture_is_registered(
        self, tmp_path, monkeypatch
    ):
        """Unchanged behavior: a gateway surface with no Ruling 1 record for
        the current session still gets the unsupported hint."""
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        _as_gateway_surface(monkeypatch, session_key="telegram:no-capture")
        name = _skill_with_required_env(tmp_path)

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            raw = skill_view(name)

        result = json.loads(raw)
        assert result["gateway_setup_hint"]
        assert result["setup_skipped"] is False
        assert "TAVILY_API_KEY" in result["required_environment_variables"][0]["name"]


class TestGatewaySurfaceWithCaptureRecord:
    def test_proceeds_to_capture_when_current_session_has_a_record(
        self, tmp_path, monkeypatch
    ):
        session_key = "telegram:owner-chat"
        _as_gateway_surface(monkeypatch, session_key=session_key)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        name = _skill_with_required_env(tmp_path)

        from tools import secret_capture_gateway as scg

        scg.register_notify(session_key, lambda entry: None)

        calls = []

        def fake_secret_callback(var_name, prompt, metadata=None):
            calls.append(var_name)
            os.environ[var_name] = "stored-in-test"
            return {"success": True, "stored_as": var_name, "validated": False, "skipped": False}

        monkeypatch.setattr(
            skills_tool_module, "_secret_capture_callback", fake_secret_callback,
            raising=False,
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            raw = skill_view(name)

        result = json.loads(raw)
        assert calls == ["TAVILY_API_KEY"]
        assert result["setup_skipped"] is False

    def test_a_different_sessions_record_does_not_unlock_this_one(
        self, tmp_path, monkeypatch
    ):
        """(a) Cross-session isolation from the skills_tool side: session A's
        registered capture must not open the gate for session B."""
        _as_gateway_surface(monkeypatch, session_key="telegram:session-B")
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        name = _skill_with_required_env(tmp_path)

        from tools import secret_capture_gateway as scg

        # Only session A has a registered notify — the current turn is B.
        scg.register_notify("telegram:session-A", lambda entry: None)

        calls = []
        monkeypatch.setattr(
            skills_tool_module,
            "_secret_capture_callback",
            lambda *a, **k: calls.append(a) or {"success": False},
            raising=False,
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            raw = skill_view(name)

        result = json.loads(raw)
        assert result["gateway_setup_hint"], "session B must still see the unsupported hint"
        assert calls == []  # the callback was never even reached


class TestInteractiveGatewaySurfaceUnaffected:
    def test_hermes_interactive_flag_still_works_for_desktop_tui(
        self, tmp_path, monkeypatch
    ):
        """The pre-existing desktop/TUI-in-gateway path (HERMES_INTERACTIVE)
        must keep working even with no Ruling-1 session record — Ruling 2
        adds an OR, it does not replace this."""
        monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        name = _skill_with_required_env(tmp_path)

        calls = []

        def fake_secret_callback(var_name, prompt, metadata=None):
            calls.append(var_name)
            os.environ[var_name] = "stored-in-test"
            return {"success": True, "stored_as": var_name, "validated": False, "skipped": False}

        monkeypatch.setattr(
            skills_tool_module, "_secret_capture_callback", fake_secret_callback,
            raising=False,
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            raw = skill_view(name)

        result = json.loads(raw)
        assert calls == ["TAVILY_API_KEY"]
        assert result["setup_skipped"] is False
