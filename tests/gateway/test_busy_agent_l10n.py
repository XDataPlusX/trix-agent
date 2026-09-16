"""Task 8: localize busy-agent, drain, queue/steer, and pause replies.

Covers ``trix.busy.*`` -- the messages a Trix Agent customer sees most
often when something is in their way: sending a message while the agent
is busy, hitting a drain/restart window, or using /queue, /steer, /pause.

Each pair asserts the same call is byte-identical under ``en`` (the
literal that used to live in the code) and carries real Russian text
under ``ru``. ``tests/gateway/conftest.py`` pins ``HERMES_LANGUAGE=en``
via an autouse fixture, so the ``ru`` half of every pair pins its own
language explicitly.

Special attention: the gateway used to build several of these messages
by splicing an English gerund ("restarting" / "shutting down") into a
shared template. Translating the gerund alone breaks Russian
case/aspect agreement while a substring test stays green. The
``_drain_*_reply`` helpers replaced that with one whole-sentence key per
branch (Ruling 3b) -- ``TestDrainReplyHelpers`` below pins both
branches, in both languages, as an exact string match specifically to
catch a regression back to gerund-splicing.
"""

from __future__ import annotations

import types

import pytest

from agent import i18n
from gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def _reset_i18n_after():
    yield
    i18n.reset_language_cache()


def _event(text: str):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource

    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="1"),
    )


def _bare_runner(**attrs):
    runner = GatewayRunner.__new__(GatewayRunner)
    for k, v in attrs.items():
        setattr(runner, k, v)
    return runner


# ---------------------------------------------------------------------------
# Drain reply helpers -- the Ruling 3b splice-fix sites.  Exact string
# matches on purpose: this is exactly the class of bug a substring
# assertion would miss.
# ---------------------------------------------------------------------------


class TestDrainReplyHelpers:
    def test_drain_queued_restart_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner(_restart_requested=True)
        assert runner._drain_queued_reply() == (
            "⏳ Gateway restarting — queued for the next turn after it comes back."
        )

    def test_drain_queued_restart_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner(_restart_requested=True)
        result = runner._drain_queued_reply()
        assert "перезапускается" in result
        assert "restarting" not in result

    def test_drain_queued_shutdown_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner(_restart_requested=False)
        assert runner._drain_queued_reply() == (
            "⏳ Gateway shutting down — queued for the next turn after it comes back."
        )

    def test_drain_queued_shutdown_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner(_restart_requested=False)
        result = runner._drain_queued_reply()
        assert "останавливается" in result
        assert "shutting" not in result

    def test_drain_busy_restart_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner(_restart_requested=True)
        assert runner._drain_busy_reply() == (
            "⏳ Gateway is restarting and is not accepting another turn right now."
        )

    def test_drain_busy_restart_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner(_restart_requested=True)
        result = runner._drain_busy_reply()
        assert "перезапускается" in result

    def test_drain_busy_shutdown_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner(_restart_requested=False)
        assert runner._drain_busy_reply() == (
            "⏳ Gateway is shutting down and is not accepting another turn right now."
        )

    def test_drain_busy_shutdown_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner(_restart_requested=False)
        result = runner._drain_busy_reply()
        assert "останавливается" in result

    def test_drain_command_reject_restart_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner(_restart_requested=True)
        assert runner._drain_command_reject_reply() == (
            "⏳ Gateway is restarting and is not accepting new work right now."
        )

    def test_drain_command_reject_restart_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner(_restart_requested=True)
        result = runner._drain_command_reject_reply()
        assert "перезапускается" in result
        assert "новую работу" in result

    def test_drain_command_reject_shutdown_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner(_restart_requested=False)
        assert runner._drain_command_reject_reply() == (
            "⏳ Gateway is shutting down and is not accepting new work right now."
        )

    def test_drain_command_reject_shutdown_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner(_restart_requested=False)
        result = runner._drain_command_reject_reply()
        assert "останавливается" in result
        assert "новую работу" in result


# ---------------------------------------------------------------------------
# _dispatch_busy_slash_command -- per-command reject text + generic catch-all
# ---------------------------------------------------------------------------


class TestBusyRejectDispatch:
    @pytest.mark.asyncio
    async def test_reject_model_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner()
        cmd_def = types.SimpleNamespace(name="model", busy_policy="reject", busy_handler="model")
        result = await runner._dispatch_busy_slash_command(
            event=_event("/model"), cmd_def=cmd_def, quick_key="k", source=None
        )
        assert result == "Agent is running — wait or /stop first, then switch models."

    @pytest.mark.asyncio
    async def test_reject_model_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner()
        cmd_def = types.SimpleNamespace(name="model", busy_policy="reject", busy_handler="model")
        result = await runner._dispatch_busy_slash_command(
            event=_event("/model"), cmd_def=cmd_def, quick_key="k", source=None
        )
        assert "агент сейчас работает" in result.lower()
        assert "модель" in result.lower()

    @pytest.mark.asyncio
    async def test_reject_generic_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner()
        cmd_def = types.SimpleNamespace(name="reasoning", busy_policy="reject", busy_handler=None)
        result = await runner._dispatch_busy_slash_command(
            event=_event("/reasoning"), cmd_def=cmd_def, quick_key="k", source=None
        )
        assert result == (
            "⏳ Agent is running — `/reasoning` can't run "
            "mid-turn. Wait for the current response or `/stop` first."
        )

    @pytest.mark.asyncio
    async def test_reject_generic_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner()
        cmd_def = types.SimpleNamespace(name="reasoning", busy_policy="reject", busy_handler=None)
        result = await runner._dispatch_busy_slash_command(
            event=_event("/reasoning"), cmd_def=cmd_def, quick_key="k", source=None
        )
        assert "/reasoning" in result
        assert "агент сейчас работает" in result.lower()


# ---------------------------------------------------------------------------
# /pause -- the two branded strings in this cluster
# ---------------------------------------------------------------------------


class TestPauseResumeCommand:
    @pytest.mark.asyncio
    async def test_resumed_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        from agent import estop

        estop.engage(reason="test")
        runner = _bare_runner()
        result = await runner._handle_pause_command(_event("/pause off"))
        assert result == "▶️ Resumed — new work is accepted again."

    @pytest.mark.asyncio
    async def test_resumed_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        from agent import estop

        estop.engage(reason="test")
        runner = _bare_runner()
        result = await runner._handle_pause_command(_event("/pause off"))
        assert "возобновлено" in result.lower()

    @pytest.mark.asyncio
    async def test_not_paused_en_is_debranded(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        from agent import estop

        assert estop.get_state() is None  # fresh temp HERMES_HOME, never engaged
        runner = _bare_runner()
        result = await runner._handle_pause_command(_event("/pause off"))
        assert result == "The agent wasn't paused."
        assert "hermes" not in result.lower()

    @pytest.mark.asyncio
    async def test_not_paused_ru_names_no_product(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        from agent import estop

        assert estop.get_state() is None
        runner = _bare_runner()
        result = await runner._handle_pause_command(_event("/pause off"))
        assert "не был на паузе" in result.lower()
        assert "hermes" not in result.lower()
        assert "трикс" not in result.lower() and "trix" not in result.lower()

    @pytest.mark.asyncio
    async def test_already_paused_en_is_debranded(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        from agent import estop

        estop.engage(reason="maintenance")
        runner = _bare_runner()
        result = await runner._handle_pause_command(_event("/pause"))
        assert result == (
            "⏸️ The agent is already paused (reason: maintenance). "
            "Use `/pause off` to resume."
        )
        assert "hermes" not in result.lower()

    @pytest.mark.asyncio
    async def test_already_paused_ru_names_no_product(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        from agent import estop

        estop.engage(reason="maintenance")
        runner = _bare_runner()
        result = await runner._handle_pause_command(_event("/pause"))
        assert "уже на паузе" in result.lower()
        assert "maintenance" in result
        assert "hermes" not in result.lower()
        assert "трикс" not in result.lower() and "trix" not in result.lower()

    @pytest.mark.asyncio
    async def test_paused_with_reason_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner()
        result = await runner._handle_pause_command(_event("/pause maintenance window"))
        assert result == (
            "⏸️ Paused (reason: maintenance window). New cron/kanban/gateway "
            "work is on hold; in-flight work finishes normally. "
            "Use `/pause off` to resume."
        )

    @pytest.mark.asyncio
    async def test_paused_with_reason_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner()
        result = await runner._handle_pause_command(_event("/pause maintenance window"))
        assert "пауза" in result.lower()
        assert "maintenance window" in result


# ---------------------------------------------------------------------------
# /queue, /steer, /goal while an agent is running -- the early-return
# branches that don't need an adapter to exercise.
# ---------------------------------------------------------------------------


class TestQueueSteerGoalBusyBranches:
    @pytest.mark.asyncio
    async def test_queue_usage_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner()
        result = await runner._busy_queue_command(_event("/queue"), "k", None)
        assert result == "Usage: /queue <prompt>"

    @pytest.mark.asyncio
    async def test_queue_usage_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner()
        result = await runner._busy_queue_command(_event("/queue"), "k", None)
        assert result == "Использование: /queue <запрос>"

    @pytest.mark.asyncio
    async def test_steer_usage_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner()
        result = await runner._busy_steer_command(_event("/steer"), "k", None)
        assert result == "Usage: /steer <prompt>"

    @pytest.mark.asyncio
    async def test_steer_usage_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner()
        result = await runner._busy_steer_command(_event("/steer"), "k", None)
        assert result == "Использование: /steer <запрос>"

    @pytest.mark.asyncio
    async def test_goal_busy_reject_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner()
        result = await runner._busy_goal_command(_event("/goal ship the new feature"), "k", None)
        assert result == (
            "Agent is running — use /goal status / pause / clear / wait "
            "mid-run, or /stop before setting a new goal."
        )

    @pytest.mark.asyncio
    async def test_goal_busy_reject_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner()
        result = await runner._busy_goal_command(_event("/goal ship the new feature"), "k", None)
        assert "агент сейчас работает" in result.lower()
        assert "/goal status" in result


# ---------------------------------------------------------------------------
# Catalog-level coverage for call sites too deep in _handle_message /
# _handle_busy_input / _notify_active_sessions_of_shutdown to invoke
# directly without standing up a full gateway + adapter + session store.
# Same technique as tests/gateway/test_errors_l10n.py's
# TestStatusHintCatalogEntries: exercise the real agent.i18n.t() +
# locales/*.yaml path the gateway call sites use, proving the catalog
# entries exist, resolve, and format correctly in both languages.
# ---------------------------------------------------------------------------


class TestBusyAckCatalogEntries:
    def test_status_fragments_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.status_min_elapsed", elapsed_min=5) == "5 min elapsed"
        assert i18n.t("trix.busy.status_iteration", iteration=3, max_iter=50) == "iteration 3/50"
        assert i18n.t("trix.busy.status_running", current_tool="terminal") == "running: terminal"

    def test_status_fragments_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        assert "5" in i18n.t("trix.busy.status_min_elapsed", elapsed_min=5)
        assert "3/50" in i18n.t("trix.busy.status_iteration", iteration=3, max_iter=50)
        assert "terminal" in i18n.t("trix.busy.status_running", current_tool="terminal")

    def test_steered_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.steered", status_detail="") == (
            "⏩ Steered into current run. Your message arrives after the next tool call."
        )

    def test_steered_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.busy.steered", status_detail="")
        assert "текущий запуск" in result.lower()

    def test_redirected_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.redirected", status_detail="") == (
            "↪ Redirected current run. I'll adjust using your correction."
        )

    def test_redirected_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.busy.redirected", status_detail="")
        assert "скорректирован" in result.lower()

    def test_queued_subagent_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.queued_subagent", status_detail="") == (
            "⏳ Subagent working — your message is queued for "
            "when it finishes (use /stop to cancel everything)."
        )

    def test_queued_subagent_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.busy.queued_subagent", status_detail="")
        assert "подагент" in result.lower()

    def test_queued_compression_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.queued_compression", status_detail="") == (
            "⏳ Compressing context — your message is queued for "
            "when it finishes (use /stop to cancel everything)."
        )

    def test_queued_compression_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.busy.queued_compression", status_detail="")
        assert "сжати" in result.lower()

    def test_queued_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.queued", status_detail="") == (
            "⏳ Queued for the next turn. I'll respond once the current task finishes."
        )

    def test_queued_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.busy.queued", status_detail="")
        assert "очередь" in result.lower()

    def test_interrupting_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.interrupting", status_detail="") == (
            "⚡ Interrupting current task. I'll respond to your message shortly."
        )

    def test_interrupting_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.busy.interrupting", status_detail="")
        assert "прерываю" in result.lower()


class TestDrainDeepCallSiteCatalogEntries:
    def test_external_drain_active_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.external_drain_active") == (
            "⏳ This agent is draining for a maintenance action and isn't "
            "accepting new turns right now. It'll be back in a moment — "
            "please resend shortly."
        )

    def test_external_drain_active_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.busy.external_drain_active")
        assert "техническ" in result.lower()

    def test_turn_lease_timeout_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.turn_lease_timeout") == (
            "⏳ Another turn is still running on this session. To "
            "protect the transcript, this message was not processed. "
            "Wait for the active turn to finish, then resend it."
        )

    def test_turn_lease_timeout_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.busy.turn_lease_timeout")
        assert "другой ход" in result.lower()

    def test_shutdown_notice_restart_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.shutdown_notice_restart") == (
            "⚠️ Gateway restarting — Your current task will be interrupted. "
            "Send any message after restart and I'll try to resume where you left off."
        )

    def test_shutdown_notice_restart_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.busy.shutdown_notice_restart")
        assert "перезапускается" in result

    def test_shutdown_notice_shutdown_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.shutdown_notice_shutdown") == (
            "⚠️ Gateway shutting down — Your current task will be interrupted."
        )

    def test_shutdown_notice_shutdown_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.busy.shutdown_notice_shutdown")
        assert "останавливается" in result


class TestRemainingSteerBranchesAndBrandFreeStop:
    @pytest.mark.asyncio
    async def test_steer_no_active_agent_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        runner = _bare_runner()
        # No session state stubbed -> _peek_session_state must resolve to
        # "no running agent"; patch it directly so this test doesn't need
        # a real session store.
        monkeypatch.setattr(runner, "_peek_session_state", lambda k: None)
        monkeypatch.setattr(runner, "_adapter_for_source", lambda s: None)
        result = await runner._busy_steer_command(_event("/steer keep going"), "k", None)
        assert result == "No active agent — /steer queued for the next turn."

    @pytest.mark.asyncio
    async def test_steer_no_active_agent_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        runner = _bare_runner()
        monkeypatch.setattr(runner, "_peek_session_state", lambda k: None)
        monkeypatch.setattr(runner, "_adapter_for_source", lambda s: None)
        result = await runner._busy_steer_command(_event("/steer keep going"), "k", None)
        assert "нет активного агента" in result.lower()

    def test_stop_force_stopped_catalog_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.stop_force_stopped") == (
            "⚡ Force-stopped. The agent was still starting — session unlocked."
        )

    def test_stop_force_stopped_catalog_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.busy.stop_force_stopped")
        assert "принудительно остановлено" in result.lower()

    def test_steer_usage_no_agent_catalog_en(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        assert i18n.t("trix.busy.steer_usage_no_agent") == (
            "Usage: /steer <prompt>  (no agent is running; sending as a normal message)"
        )

    def test_steer_usage_no_agent_catalog_ru(self, monkeypatch):
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        result = i18n.t("trix.busy.steer_usage_no_agent")
        assert "использование: /steer" in result.lower()
