"""Task 19: localize the raw English literals in gateway/slash_commands.py.

``gateway/slash_commands.py`` had ~280 calls to ``t()`` living next to ~79
raw ``return "..."``/``return f"..."`` English literals (plus multi-line
returns and ternaries) that never went through the catalog -- see
docs/product/plans/2026-08-17-trix-agent-telegram-l10n.md, "Task 19". The
canonical symptom: ``/debug`` had a Russian description in the Telegram
menu (``hermes_cli/trix_menu.py``) but replied to a ``display.language: ru``
client entirely in English.

These tests EXECUTE the call sites (not just check the catalog has a key)
so that deleting a ``t()`` call and reverting to the old literal turns a
test red -- pin the catalog key without pinning the code path and this
suite stays green while the client silently goes back to English.

Pattern follows ``tests/gateway/test_errors_l10n.py``: ``tests/gateway/
conftest.py`` pins ``HERMES_LANGUAGE=en`` via an autouse fixture, so the
``ru`` half of every pair sets ``HERMES_LANGUAGE`` explicitly and resets
the i18n cache before and after.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import i18n
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def _reset_i18n_after():
    yield
    i18n.reset_language_cache()


def _set_lang(monkeypatch, lang: str):
    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    i18n.reset_language_cache()


def _stub():
    """Minimal object exposing GatewaySlashCommandsMixin handlers."""
    from gateway.slash_commands import GatewaySlashCommandsMixin

    class _Stub(GatewaySlashCommandsMixin):
        def __init__(self):
            pass

    return _Stub()


def _event(text, *, platform=Platform.TELEGRAM, chat_id="chat1", user_id="u1"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=platform, chat_id=chat_id, chat_type="dm", user_id=user_id),
    )


class _FakeAsyncSessionStore:
    """get_or_create_session() is the only method the goal/heartbeat
    managers need before this test ever touches manager state."""

    def __init__(self, session_id="sess-1"):
        self.session_id = session_id

    async def get_or_create_session(self, source):
        return SimpleNamespace(session_id=self.session_id)


# ---------------------------------------------------------------------------
# Group A -- /debug
# ---------------------------------------------------------------------------


class TestDebug:
    def _run(self, monkeypatch, *, report_len):
        stub = _stub()
        stub._DEBUG_INLINE_REPORT_LIMIT = 3500
        report = "x" * report_len
        monkeypatch.setattr(
            "hermes_cli.debug._best_effort_sweep_expired_pastes", lambda: None
        )
        monkeypatch.setattr("hermes_cli.debug._capture_dump", lambda: "dump")
        monkeypatch.setattr(
            "hermes_cli.debug.collect_debug_report",
            lambda log_lines, dump_text: report,
        )
        monkeypatch.setattr(
            "hermes_cli.debug._save_report_locally", lambda report: "/srv/report.txt"
        )
        return asyncio.run(stub._handle_debug_command(_event("/debug")))

    def test_inline_report_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = self._run(monkeypatch, report_len=10)
        assert "Not uploaded to any third-party service" in out
        assert "/srv/report.txt" in out

    def test_inline_report_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run(monkeypatch, report_len=10)
        assert "Никуда не отправлялся" in out
        assert "/srv/report.txt" in out

    def test_too_long_report_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = self._run(monkeypatch, report_len=10000)
        assert "too long to paste inline" in out
        assert "hermes debug share" not in out
        assert "administers this machine" in out

    def test_too_long_report_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run(monkeypatch, report_len=10000)
        assert "слишком длинный" in out
        assert "hermes debug share" not in out
        assert "администрирует эту машину" in out


# ---------------------------------------------------------------------------
# Group A -- /model
# ---------------------------------------------------------------------------


def _model_stub(monkeypatch, *, session_key="sess-1"):
    from hermes_cli.model_switch import ModelSwitchResult

    stub = _stub()
    stub._session_model_overrides = {}
    stub._normalize_source_for_session_key = lambda source: source
    stub._session_key_for_source = lambda source: session_key
    stub._evict_cached_agent = lambda key: None
    stub._snapshot_session_model_override = lambda key: None

    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    def _fake_switch_model(**kwargs):
        return ModelSwitchResult(
            success=True,
            new_model="test-model",
            target_provider="openrouter",
            provider_label="OpenRouter",
        )

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", _fake_switch_model)
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length", lambda *a, **k: 0
    )
    monkeypatch.setattr(
        "hermes_cli.model_cost_guard.expensive_model_warning", lambda *a, **k: None
    )
    return stub


class TestModel:
    def test_skew_guard_drops_hermes_cli_and_still_names_the_problem_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        monkeypatch.setattr(
            "gateway.code_skew.detect_code_skew", lambda: ("abc123", "def456")
        )
        from gateway.slash_commands import _model_switch_skew_guard

        out = _model_switch_skew_guard()
        assert "abc123" in out and "def456" in out
        assert "hermes gateway restart" not in out
        assert "administers this machine" in out

    def test_skew_guard_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        monkeypatch.setattr(
            "gateway.code_skew.detect_code_skew", lambda: ("abc123", "def456")
        )
        from gateway.slash_commands import _model_switch_skew_guard

        out = _model_switch_skew_guard()
        assert "abc123" in out and "def456" in out
        assert "hermes gateway restart" not in out
        assert "администрирует эту машину" in out

    def test_once_note_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        stub = _model_stub(monkeypatch)
        out = asyncio.run(stub._handle_model_command(_event("/model test-model --once")))
        assert "next turn only" in out

    def test_once_note_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        stub = _model_stub(monkeypatch)
        out = asyncio.run(stub._handle_model_command(_event("/model test-model --once")))
        assert "только на следующий ход" in out

    def test_switch_failed_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        stub = _model_stub(monkeypatch, session_key="sess-cache")
        stub._agent_cache_lock = threading.Lock()
        raising_agent = Mock()
        raising_agent.switch_model = Mock(side_effect=RuntimeError("boom"))
        stub._agent_cache = {"sess-cache": (raising_agent, 0)}
        out = asyncio.run(stub._handle_model_command(_event("/model test-model")))
        assert "failed" in out
        assert "staying on" in out

    def test_switch_failed_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        stub = _model_stub(monkeypatch, session_key="sess-cache")
        stub._agent_cache_lock = threading.Lock()
        raising_agent = Mock()
        raising_agent.switch_model = Mock(side_effect=RuntimeError("boom"))
        stub._agent_cache = {"sess-cache": (raising_agent, 0)}
        out = asyncio.run(stub._handle_model_command(_event("/model test-model")))
        assert "Не удалось переключить модель" in out

    def _run_cost_warning(self, monkeypatch):
        stub = _model_stub(monkeypatch)
        monkeypatch.setattr(
            "hermes_cli.model_cost_guard.expensive_model_warning",
            lambda *a, **k: SimpleNamespace(message="This model is pricey."),
        )
        captured = {}

        async def _fake_request_slash_confirm(*, event, command, title, message, handler):
            captured["title"] = title
            captured["message"] = message
            captured["handler"] = handler
            return message

        stub._request_slash_confirm = _fake_request_slash_confirm
        out = asyncio.run(stub._handle_model_command(_event("/model test-model")))
        return out, captured

    def test_cost_warning_dialog_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out, captured = self._run_cost_warning(monkeypatch)
        assert captured["title"] == "Expensive Model Warning"
        assert "This model is pricey." in out
        assert "reply" in out and "approve" in out

    def test_cost_warning_dialog_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out, captured = self._run_cost_warning(monkeypatch)
        assert captured["title"] == "Предупреждение о дорогой модели"
        assert "This model is pricey." in out
        assert "ответьте" in out

    def test_cost_warning_cancel_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        _, captured = self._run_cost_warning(monkeypatch)
        out = asyncio.run(captured["handler"]("cancel"))
        assert "cancelled" in out
        assert "unchanged" in out

    def test_cost_warning_cancel_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        _, captured = self._run_cost_warning(monkeypatch)
        out = asyncio.run(captured["handler"]("cancel"))
        assert "отменено" in out


# ---------------------------------------------------------------------------
# Group A -- /memory
# ---------------------------------------------------------------------------


class TestMemory:
    def test_unknown_subcommand_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        stub = _stub()
        stub._session_key_for_source = lambda source: "sess-1"
        out = asyncio.run(stub._handle_memory_command(_event("/memory bogus")))
        assert "Unknown /memory subcommand" in out

    def test_unknown_subcommand_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        stub = _stub()
        stub._session_key_for_source = lambda source: "sess-1"
        out = asyncio.run(stub._handle_memory_command(_event("/memory bogus")))
        assert "Неизвестная подкоманда /memory" in out


# ---------------------------------------------------------------------------
# Group A -- /approvals
# ---------------------------------------------------------------------------


class TestApprovals:
    def _run(self, monkeypatch):
        stub = _stub()
        stub.config = None
        monkeypatch.setattr(
            "gateway.slash_access.policy_for_source",
            lambda config, source: SimpleNamespace(is_admin=lambda user_id: False),
        )
        return asyncio.run(stub._handle_approvals_command(_event("/approvals always")))

    def test_admin_only_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = self._run(monkeypatch)
        assert out == "Only gateway admins can change the persistent approval mode."

    def test_admin_only_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run(monkeypatch)
        assert "администратор шлюза" in out


# ---------------------------------------------------------------------------
# Group A -- /sessions
# ---------------------------------------------------------------------------


class TestSessions:
    def _stub_with_db(self):
        stub = _stub()
        stub._session_db = object()  # truthy sentinel
        stub._normalize_source_for_session_key = lambda source: source
        stub._session_key_for_source = lambda source: "sess-1"
        stub._resume_caller_is_admin = lambda source: False
        stub.async_session_store = _FakeAsyncSessionStore()
        return stub

    def test_usage_search_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        stub = self._stub_with_db()
        out = asyncio.run(stub._handle_sessions_command(_event("/sessions search")))
        assert out == "Usage: `/sessions search <query>`"

    def test_usage_search_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        stub = self._stub_with_db()
        out = asyncio.run(stub._handle_sessions_command(_event("/sessions search")))
        assert "Формат" in out

    def _run_listing(self, monkeypatch, args):
        stub = self._stub_with_db()
        monkeypatch.setattr(
            "hermes_cli.session_listing.query_session_listing", lambda *a, **k: []
        )
        monkeypatch.setattr(
            "hermes_cli.session_listing.format_gateway_session_listing",
            lambda rows, include_source, title: title,
        )
        return asyncio.run(stub._handle_sessions_command(_event(f"/sessions {args}".rstrip())))

    # Task 9f (docs/product/plans/2026-09-01-client-command-surface.md):
    # `/sessions` with no args used to default to "Named Sessions" only
    # (`include_unnamed=False`), and the only documented way to see the rest
    # was the undocumented `/sessions full`. A client who never used
    # `/title` got an empty-looking list on top of real history. The
    # gateway now forces `include_unnamed=True` on this surface regardless
    # of args (`gateway/slash_commands.py::_handle_sessions_command`), so
    # the bare-args title picks the SAME "all sessions" title `full` always
    # produced -- these two tests pin the (now correct) default;
    # `test_title_all_with_full_*` below pins that `full` still works too
    # (parse_session_listing_args itself is untouched).
    def test_title_all_default_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        assert self._run_listing(monkeypatch, "") == "Sessions"

    def test_title_all_default_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        assert self._run_listing(monkeypatch, "") == "Разговоры"

    def test_title_all_with_full_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        assert self._run_listing(monkeypatch, "full") == "Sessions"

    def test_title_all_with_full_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        assert self._run_listing(monkeypatch, "full") == "Разговоры"

    def test_title_matching_search_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        assert self._run_listing(monkeypatch, "search foo") == "Sessions matching “foo”"

    def test_title_matching_search_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run_listing(monkeypatch, "search foo")
        assert "foo" in out and "Разговоры по запросу" in out


# ---------------------------------------------------------------------------
# Group A -- /agents
# ---------------------------------------------------------------------------


class TestAgents:
    def _run(self, monkeypatch):
        stub = _stub()
        stub._session_key_for_source = lambda source: "sess-1"
        stub._running_agents = {}
        stub._running_agents_ts = {}
        stub._background_tasks = set()
        monkeypatch.setattr(
            "tools.async_delegation.list_async_delegations",
            lambda: [
                {
                    "status": "running",
                    "delegation_id": "d1",
                    "goal": "test goal",
                    "seconds_since_progress": 0,
                    "children_activity": [{"api_calls": 3}],
                }
            ],
        )
        return asyncio.run(stub._handle_agents_command(_event("/agents")))

    def test_between_turns_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = self._run(monkeypatch)
        assert "between turns" in out

    def test_between_turns_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run(monkeypatch)
        assert "между ходами" in out


# ---------------------------------------------------------------------------
# Group B -- /platform
# ---------------------------------------------------------------------------


class TestPlatform:
    def _run(self, text):
        stub = _stub()
        stub.adapters = {}
        stub._failed_platforms = {}
        event = _event(text)
        event.content = text
        return asyncio.run(stub._handle_platform_command(event))

    def test_list_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = self._run("/platform list")
        assert "**Gateway platforms**" in out
        assert "Connected: (none)" in out
        assert "Failed/paused: (none)" in out

    def test_list_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run("/platform list")
        assert "**Платформы шлюза**" in out
        assert "Подключено: (нет)" in out

    def test_pause_confirm_drops_hermes_cli_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        stub = _stub()
        stub.adapters = {}
        stub._failed_platforms = {Platform.WHATSAPP: {"attempts": 2}}
        stub._pause_failed_platform = lambda platform, reason: None
        event = _event("/platform pause whatsapp")
        event.content = "/platform pause whatsapp"
        out = asyncio.run(stub._handle_platform_command(event))
        assert "hermes gateway restart" not in out
        assert "administers this machine" in out

    def test_pause_confirm_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        stub = _stub()
        stub.adapters = {}
        stub._failed_platforms = {Platform.WHATSAPP: {"attempts": 2}}
        stub._pause_failed_platform = lambda platform, reason: None
        event = _event("/platform pause whatsapp")
        event.content = "/platform pause whatsapp"
        out = asyncio.run(stub._handle_platform_command(event))
        assert "hermes gateway restart" not in out
        assert "администрирует эту машину" in out


# ---------------------------------------------------------------------------
# Group B -- /goal
# ---------------------------------------------------------------------------


def _goal_stub():
    from hermes_cli.goals import GoalManager
    from hermes_cli.heartbeat import HeartbeatManager

    stub = _stub()
    stub.async_session_store = _FakeAsyncSessionStore()
    stub._session_key_for_source = lambda source: "sess-1"

    async def _get_goal_manager_for_event(event):
        return GoalManager(session_id="sess-1", default_max_turns=20), None

    async def _get_heartbeat_manager_for_event(event):
        return HeartbeatManager(session_id="sess-1"), None

    stub._get_goal_manager_for_event = _get_goal_manager_for_event
    stub._get_heartbeat_manager_for_event = _get_heartbeat_manager_for_event
    return stub


class TestGoal:
    def test_wait_usage_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = asyncio.run(_goal_stub()._handle_goal_command(_event("/goal wait")))
        assert out == "Usage: /goal wait <pid> [reason]"

    def test_wait_usage_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = asyncio.run(_goal_stub()._handle_goal_command(_event("/goal wait")))
        assert "Формат" in out and "/goal wait" in out

    def test_gate_usage_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = asyncio.run(_goal_stub()._handle_goal_command(_event("/goal gate bogus")))
        assert out == "Usage: /goal gate [list | add <command> | remove <N> | clear]"

    def test_gate_usage_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = asyncio.run(_goal_stub()._handle_goal_command(_event("/goal gate bogus")))
        assert "Формат" in out

    def test_draft_usage_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = asyncio.run(_goal_stub()._handle_goal_command(_event("/goal draft")))
        assert out == "Usage: /goal draft <objective in plain language>"

    def test_draft_usage_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = asyncio.run(_goal_stub()._handle_goal_command(_event("/goal draft")))
        assert "Формат" in out


# ---------------------------------------------------------------------------
# Group B -- /heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_usage_no_interval_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        stub = _goal_stub()
        out = asyncio.run(stub._handle_heartbeat_command(_event("/heartbeat bogus")))
        assert "Usage: /heartbeat every <interval> <prompt>" in out

    def test_usage_no_interval_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        stub = _goal_stub()
        out = asyncio.run(stub._handle_heartbeat_command(_event("/heartbeat bogus")))
        assert "Формат: /heartbeat every" in out


# ---------------------------------------------------------------------------
# Group B -- /refine
# ---------------------------------------------------------------------------


class TestRefine:
    def test_unavailable_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        stub = _stub()
        event = MessageEvent(text="/refine", message_type=MessageType.TEXT, source=None)
        out = asyncio.run(stub._handle_refine_command(event))
        assert out == "Refine unavailable (no session)."

    def test_unavailable_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        stub = _stub()
        event = MessageEvent(text="/refine", message_type=MessageType.TEXT, source=None)
        out = asyncio.run(stub._handle_refine_command(event))
        assert "Разбор недоступен" in out

    def test_agent_running_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        stub = _stub()
        stub._session_key_for_source = lambda source: "sess-1"
        stub._running_agents = {"sess-1": object()}
        out = asyncio.run(stub._handle_refine_command(_event("/refine")))
        assert out == "Agent is running — wait for the turn to finish, then /refine."

    def test_agent_running_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        stub = _stub()
        stub._session_key_for_source = lambda source: "sess-1"
        stub._running_agents = {"sess-1": object()}
        out = asyncio.run(stub._handle_refine_command(_event("/refine")))
        assert "Агент сейчас работает" in out


# ---------------------------------------------------------------------------
# Group B -- /subgoal
# ---------------------------------------------------------------------------


class TestSubgoal:
    def test_no_active_goal_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = asyncio.run(_goal_stub()._handle_subgoal_command(_event("/subgoal add x")))
        assert out == "No active goal. Set one with /goal <text>."

    def test_no_active_goal_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = asyncio.run(_goal_stub()._handle_subgoal_command(_event("/subgoal add x")))
        assert "Нет активной цели" in out


# ---------------------------------------------------------------------------
# Group B -- /diff
# ---------------------------------------------------------------------------


class TestDiff:
    def test_fenced_truncated_diff_note_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        from gateway.slash_commands import GatewaySlashCommandsMixin

        diff = "\n".join(f"line {i}" for i in range(100))
        out = GatewaySlashCommandsMixin._fenced_truncated_diff(diff, max_lines=10)
        assert "truncated" in out
        assert "use /diff --stat for a summary" in out

    def test_fenced_truncated_diff_note_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        from gateway.slash_commands import GatewaySlashCommandsMixin

        diff = "\n".join(f"line {i}" for i in range(100))
        out = GatewaySlashCommandsMixin._fenced_truncated_diff(diff, max_lines=10)
        assert "обрезано" in out

    def _run_untracked(self, monkeypatch):
        stub = _stub()
        monkeypatch.setenv("TERMINAL_CWD", "/tmp")
        monkeypatch.setattr(
            "tools.working_diff.collect_working_diff",
            lambda cwd, mode: {
                "success": True,
                "stat": "",
                "diff": "",
                "untracked": [f"file{i}.txt" for i in range(20)],
                "empty": False,
            },
        )
        return asyncio.run(stub._handle_diff_command(_event("/diff")))

    def test_untracked_label_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = self._run_untracked(monkeypatch)
        assert "**Untracked:**" in out
        assert "and 5 more" in out

    def test_untracked_label_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run_untracked(monkeypatch)
        assert "**Неотслеживаемые:**" in out
        assert "и ещё 5" in out


# ---------------------------------------------------------------------------
# Group B -- /codex-runtime
# ---------------------------------------------------------------------------


class TestCodexRuntime:
    def _run(self, monkeypatch):
        import hermes_cli.config as cfgmod

        monkeypatch.delattr(cfgmod, "save_config", raising=False)
        stub = _stub()
        return asyncio.run(stub._handle_codex_runtime_command(_event("/codex-runtime")))

    def test_config_load_failed_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = self._run(monkeypatch)
        assert "Could not load config" in out

    def test_config_load_failed_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run(monkeypatch)
        assert "Не удалось загрузить конфигурацию" in out


# ---------------------------------------------------------------------------
# Group B -- /bundles
# ---------------------------------------------------------------------------


class TestBundles:
    def _run(self, monkeypatch, bundles):
        from hermes_cli.slash_exec import CommandReply

        monkeypatch.setattr(
            "hermes_cli.slash_exec.execute_command",
            lambda name, ctx: CommandReply(
                text="", data={"bundles": bundles, "dir": "/srv/bundles"}
            ),
        )
        stub = _stub()
        return asyncio.run(stub._handle_bundles_command(_event("/bundles")))

    def test_none_drops_hermes_cli_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = self._run(monkeypatch, [])
        assert "hermes bundles create" not in out
        assert "administers this machine" in out

    def test_none_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run(monkeypatch, [])
        assert "hermes bundles create" not in out
        assert "администрирует эту машину" in out

    def test_header_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = self._run(
            monkeypatch, [{"slug": "demo", "description": "Demo bundle", "skills": ["a", "b"]}]
        )
        assert "**Skill Bundles** (1 installed):" in out

    def test_header_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run(
            monkeypatch, [{"slug": "demo", "description": "Demo bundle", "skills": ["a", "b"]}]
        )
        assert "**Наборы навыков** (установлено: 1):" in out


# ---------------------------------------------------------------------------
# Group B -- /topup (debranded "Nous balance" -> "Balance")
# ---------------------------------------------------------------------------


class TestTopup:
    def _run(self, monkeypatch):
        import agent.account_usage as account_usage
        from agent.account_usage import CreditsView

        monkeypatch.setattr(
            account_usage,
            "build_credits_view",
            lambda *a, **kw: CreditsView(logged_in=True, balance_lines=(), topup_url=None),
        )
        stub = _stub()
        return asyncio.run(stub._handle_topup_command(MessageEvent(
            text="/topup", message_type=MessageType.TEXT, source=None,
        )))

    def test_balance_header_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = self._run(monkeypatch)
        assert "Nous" not in out
        assert "💳 **Balance**" in out

    def test_balance_header_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run(monkeypatch)
        assert "Nous" not in out
        assert "💳 **Баланс**" in out


# ---------------------------------------------------------------------------
# Group B -- /skills (gateway mode)
# ---------------------------------------------------------------------------


class TestSkillsGateway:
    def test_approval_off_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        stub = _stub()
        stub._session_key_for_source = lambda source: "sess-1"
        out = asyncio.run(stub._handle_skills_command(_event("/skills")))
        assert out == (
            "Skill write approval is off. Enable it with /skills approval on, "
            "then review staged writes here with /skills pending."
        )
        assert "skills.write_approval" not in out

    def test_approval_off_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        stub = _stub()
        stub._session_key_for_source = lambda source: "sess-1"
        out = asyncio.run(stub._handle_skills_command(_event("/skills")))
        assert "Подтверждение записи навыков выключено" in out
        assert "skills.write_approval" not in out

    def _run_unknown(self, monkeypatch):
        stub = _stub()
        stub._session_key_for_source = lambda source: "sess-1"
        monkeypatch.setattr("tools.write_approval.write_approval_enabled", lambda subsystem: True)
        monkeypatch.setattr(
            "hermes_cli.write_approval_commands.handle_pending_subcommand",
            lambda subsystem, args, set_mode_fn=None: None,
        )
        return asyncio.run(stub._handle_skills_command(_event("/skills bogus")))

    def test_unknown_subcommand_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = self._run_unknown(monkeypatch)
        assert "CLI-only" not in out
        assert "administers this machine" in out

    def test_unknown_subcommand_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run_unknown(monkeypatch)
        assert "администрирует эту машину" in out

    def _run_diff_truncated(self, monkeypatch):
        stub = _stub()
        stub._session_key_for_source = lambda source: "sess-1"
        monkeypatch.setattr("tools.write_approval.write_approval_enabled", lambda subsystem: True)
        monkeypatch.setattr(
            "hermes_cli.write_approval_commands.handle_pending_subcommand",
            lambda subsystem, args, set_mode_fn=None: "x" * 4000,
        )
        return asyncio.run(stub._handle_skills_command(_event("/skills diff abc123")))

    def test_diff_truncated_drops_path_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        out = self._run_diff_truncated(monkeypatch)
        assert "~/.hermes/pending" not in out
        assert "administers this machine" in out

    def test_diff_truncated_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        out = self._run_diff_truncated(monkeypatch)
        assert "~/.hermes/pending" not in out
        assert "администрирует эту машину" in out
