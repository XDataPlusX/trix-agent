"""Localize the four remaining English-visible clusters in gateway/run.py:
Telegram topic mode, /update notifications, slash-command dispatch replies,
and the background-process watcher.

Each pair EXECUTES the real call site (a ``GatewayRunner`` method, the
``hermes_cli.trix_topic_notices`` module the topic cluster delegates to, or
the module-level ``_check_unavailable_skill``) so that reverting a ``t()``
call back to the old literal turns a test red -- pinning the catalog key
alone would not catch that regression.

Pattern follows ``tests/gateway/test_errors_l10n.py`` / ``test_slash_commands_
l10n.py``: ``tests/gateway/conftest.py`` pins ``HERMES_LANGUAGE=en`` via an
autouse fixture, so the ``ru`` half of every pair sets ``HERMES_LANGUAGE``
explicitly and resets the i18n cache before and after.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent import i18n
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


@pytest.fixture(autouse=True)
def _reset_i18n_after():
    yield
    i18n.reset_language_cache()


def _set_lang(monkeypatch, lang: str):
    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    i18n.reset_language_cache()


def _make_source(*, thread_id=None, chat_id="208214988", user_id="208214988") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id=user_id,
        chat_id=chat_id,
        user_name="tester",
        chat_type="dm",
        thread_id=thread_id,
    )


def _make_event(text: str, *, thread_id=None) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(thread_id=thread_id), message_id="m1")


_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


def _assert_no_cyrillic(text: str) -> None:
    assert not _CYRILLIC_RE.search(text), f"unexpected Cyrillic text: {text!r}"


def _make_bare_runner():
    """A GatewayRunner with just enough state for the topic-mode methods."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._create_dm_topic = AsyncMock(return_value="42")
    adapter._bot = MagicMock()
    adapter._bot.pin_chat_message = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._telegram_lobby_reminder_ts = {}
    runner._telegram_capability_hint_ts = {}
    return runner


def _make_dispatch_runner():
    """Mirrors tests/gateway/test_stacked_skill_platform_disabled.py's
    minimal runner -- enough state for ``_handle_message`` to reach the
    quick-command / hook / skill-dispatch sections without touching the
    agent loop."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )

    source = _make_source()
    session_entry = SessionEntry(
        session_key=build_session_key(source),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    from gateway.run import GatewayRunner as _GR
    runner._session_key_for_source = _GR._session_key_for_source.__get__(runner, _GR)
    return runner


# ===========================================================================
# Cluster 1 -- Telegram topic mode (hermes_cli/trix_topic_notices.py)
# ===========================================================================


class TestTopicRootMessages:
    def test_lobby_message_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        out = runner._telegram_topic_root_lobby_message()
        assert "All Messages" in out
        assert "Hermes" not in out
        _assert_no_cyrillic(out)

    def test_lobby_message_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_bare_runner()
        out = runner._telegram_topic_root_lobby_message()
        assert "All Messages" in out
        assert "Hermes" not in out
        # This message's distinctive claim ("this chat is reserved for
        # system commands") -- not merely "contains some Cyrillic" or a
        # phrase shared with the other topic-mode strings.
        assert "зарезервирован для системных команд" in out

    def test_root_new_message_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        out = runner._telegram_topic_root_new_message()
        assert "All Messages" in out
        assert "Hermes" not in out
        _assert_no_cyrillic(out)

    def test_root_new_message_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_bare_runner()
        out = runner._telegram_topic_root_new_message()
        assert "All Messages" in out
        assert "Hermes" not in out
        # Distinctive to root_new (not shared with root_lobby/new_header).
        assert "хотите заменить текущую сессию" in out

    def test_new_header_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        runner._is_telegram_topic_lane = lambda source: True
        out = runner._telegram_topic_new_header(_make_source(thread_id="5"))
        assert "All Messages" in out
        assert "Hermes" not in out
        _assert_no_cyrillic(out)

    def test_new_header_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_bare_runner()
        runner._is_telegram_topic_lane = lambda source: True
        out = runner._telegram_topic_new_header(_make_source(thread_id="5"))
        assert "All Messages" in out
        assert "Hermes" not in out
        # Distinctive to new_header (not shared with root_lobby/root_new).
        assert "В этой теме начата новая сессия" in out

    def test_new_header_none_outside_topic_lane(self):
        runner = _make_bare_runner()
        runner._is_telegram_topic_lane = lambda source: False
        assert runner._telegram_topic_new_header(_make_source()) is None


class TestSanitizeTopicTitle:
    def test_fallback_title_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        out = runner._sanitize_telegram_topic_title("   ")
        assert out == "Chat"

    def test_fallback_title_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_bare_runner()
        out = runner._sanitize_telegram_topic_title("")
        assert out == "Чат"

    def test_real_title_untouched(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        assert runner._sanitize_telegram_topic_title("My chat") == "My chat"


class TestTopicHelpText:
    def test_help_text_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        out = runner._telegram_topic_help_text()
        assert "BotFather" in out
        assert "All Messages" in out
        assert "Hermes" not in out
        _assert_no_cyrillic(out)

    def test_help_text_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_bare_runner()
        out = runner._telegram_topic_help_text()
        assert "BotFather" in out
        assert "All Messages" in out
        assert "Hermes" not in out
        assert "/topic" in out
        # Distinctive to help_text -- this exact opening clause identifies
        # the message, not just "some Russian text was present somewhere."
        assert "включить режим нескольких сессий в личке" in out


class TestEnsureSystemTopic:
    @pytest.mark.asyncio
    async def test_creates_titled_topic_and_sends_intro_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        await runner._ensure_telegram_system_topic(_make_source())
        adapter = runner.adapters[Platform.TELEGRAM]
        adapter._create_dm_topic.assert_awaited_once()
        assert adapter._create_dm_topic.await_args.args[1] == "System"
        sent_text = adapter.send.await_args.args[1]
        assert sent_text == "System topic for commands and status."

    @pytest.mark.asyncio
    async def test_creates_titled_topic_and_sends_intro_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_bare_runner()
        await runner._ensure_telegram_system_topic(_make_source())
        adapter = runner.adapters[Platform.TELEGRAM]
        assert adapter._create_dm_topic.await_args.args[1] == "Система"
        sent_text = adapter.send.await_args.args[1]
        assert sent_text == "Системная тема для команд и статуса."


class _FakeTopicSessionDB:
    def __init__(self, *, enabled=True, disable_raises=False):
        self._enabled = enabled
        self._disable_raises = disable_raises

    async def is_telegram_topic_mode_enabled(self, chat_id, user_id):
        return self._enabled

    async def disable_telegram_topic_mode(self, chat_id):
        if self._disable_raises:
            raise RuntimeError("db locked")


class TestDisableTopicMode:
    @pytest.mark.asyncio
    async def test_chat_id_unresolved_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        runner._session_db = _FakeTopicSessionDB()
        source = _make_source(chat_id="")
        out = await runner._disable_telegram_topic_mode_for_chat(source)
        assert out == "Could not determine chat ID."

    @pytest.mark.asyncio
    async def test_not_enabled_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_bare_runner()
        runner._session_db = _FakeTopicSessionDB(enabled=False)
        out = await runner._disable_telegram_topic_mode_for_chat(_make_source())
        assert "не включён" in out

    @pytest.mark.asyncio
    async def test_disable_failed_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        runner._session_db = _FakeTopicSessionDB(disable_raises=True)
        out = await runner._disable_telegram_topic_mode_for_chat(_make_source())
        assert out == "Failed to disable topic mode: db locked"

    @pytest.mark.asyncio
    async def test_disabled_ok_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_bare_runner()
        runner._session_db = _FakeTopicSessionDB()
        out = await runner._disable_telegram_topic_mode_for_chat(_make_source())
        assert "ВЫКЛЮЧЕН" in out
        assert "Hermes" not in out


class TestTopicRootStatusMessage:
    @pytest.mark.asyncio
    async def test_with_sessions_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        runner._session_db = SimpleNamespace(
            list_unlinked_telegram_sessions_for_user=AsyncMock(
                return_value=[{"id": "old-unlinked", "title": "Old research", "preview": "hi"}]
            )
        )
        out = await runner._telegram_topic_root_status_message(_make_source())
        assert "Telegram multi-session topics are enabled" in out
        assert "Previous unlinked sessions" in out
        assert "Old research" in out
        assert "Send /topic old-unlinked inside a topic" in out
        assert "Hermes" not in out

    @pytest.mark.asyncio
    async def test_no_sessions_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_bare_runner()
        runner._session_db = SimpleNamespace(
            list_unlinked_telegram_sessions_for_user=AsyncMock(return_value=[])
        )
        out = await runner._telegram_topic_root_status_message(_make_source())
        assert "не найдено" in out
        assert "/topic <id-сессии>" in out
        assert "Hermes" not in out


class _FakeRestoreSessionDB:
    def __init__(self, session=None, *, linked=False, current_binding=None,
                 raise_already_linked=False, messages=None):
        self._session = session
        self._linked = linked
        self._current_binding = current_binding
        self._raise_already_linked = raise_already_linked
        self._messages = messages or []

    async def resolve_session_id(self, raw):
        return None if self._session is None else self._session.get("id")

    async def get_session(self, session_id):
        return self._session

    async def is_telegram_session_linked_to_topic(self, session_id):
        return self._linked

    async def get_telegram_topic_binding(self, chat_id, thread_id):
        return self._current_binding

    async def bind_telegram_topic(self, **kwargs):
        if self._raise_already_linked:
            raise ValueError("session already linked to another topic")

    async def get_session_title(self, session_id):
        return self._session.get("title")

    async def get_messages(self, session_id):
        return self._messages


class TestRestoreTopicSession:
    @pytest.mark.asyncio
    async def test_not_found_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        runner._session_db = _FakeRestoreSessionDB(session=None)
        out = await runner._restore_telegram_topic_session(_make_event("/topic abc"), "abc")
        assert out == "Session not found: abc"

    @pytest.mark.asyncio
    async def test_not_found_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_bare_runner()
        runner._session_db = _FakeRestoreSessionDB(session=None)
        out = await runner._restore_telegram_topic_session(_make_event("/topic abc"), "abc")
        assert "Сессия не найдена" in out
        assert "abc" in out

    @pytest.mark.asyncio
    async def test_not_telegram_session_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        runner._session_db = _FakeRestoreSessionDB(
            session={"id": "s1", "source": "discord", "user_id": "208214988"}
        )
        out = await runner._restore_telegram_topic_session(_make_event("/topic s1"), "s1")
        assert out == "That session is not a Telegram session and cannot be restored into this topic."

    @pytest.mark.asyncio
    async def test_not_owned_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_bare_runner()
        runner._session_db = _FakeRestoreSessionDB(
            session={"id": "s1", "source": "telegram", "user_id": "someone-else"}
        )
        out = await runner._restore_telegram_topic_session(_make_event("/topic s1"), "s1")
        assert "принадлежит другому" in out

    @pytest.mark.asyncio
    async def test_already_linked_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_bare_runner()
        runner._session_db = _FakeRestoreSessionDB(
            session={"id": "s1", "source": "telegram", "user_id": "208214988"},
            linked=True,
            current_binding={"session_id": "other"},
        )
        runner._session_key_for_source = lambda source: "key"
        out = await runner._restore_telegram_topic_session(_make_event("/topic s1"), "s1")
        assert out == "That session is already linked to another Telegram topic."

    @pytest.mark.asyncio
    async def test_restored_with_last_message_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_bare_runner()
        runner._session_db = _FakeRestoreSessionDB(
            session={"id": "s1", "source": "telegram", "user_id": "208214988", "title": "My chat"},
            messages=[{"role": "assistant", "content": "hi there"}],
        )
        runner._session_key_for_source = lambda source: "key"
        out = await runner._restore_telegram_topic_session(_make_event("/topic s1"), "s1")
        assert "Сессия восстановлена: My chat" in out
        assert "Последнее сообщение:\nhi there" in out
        assert "Hermes" not in out


# ===========================================================================
# Cluster 2 -- /update notifications (trix.update.*)
# ===========================================================================


def _make_update_runner():
    """Mirrors tests/gateway/test_update_command.py's bare runner."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._update_prompt_pending = {}
    return runner


class TestWatchUpdateProgressFinished:
    async def _run_finished(self, monkeypatch, tmp_path, *, exit_code: str, lang: str):
        _set_lang(monkeypatch, lang)
        runner = _make_update_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / ".update_pending.json").write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        (hermes_home / ".update_output.txt").write_text("done")
        (hermes_home / ".update_exit_code").write_text(exit_code)

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        import gateway.run as gateway_run
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        await runner._watch_update_progress(poll_interval=0.01, stream_interval=0.01, timeout=1.0)
        return mock_adapter.send.call_args_list[-1].args[1]

    @pytest.mark.asyncio
    async def test_success_en(self, monkeypatch, tmp_path):
        sent = await self._run_finished(monkeypatch, tmp_path, exit_code="0", lang="en")
        assert sent == "✅ Update finished."

    @pytest.mark.asyncio
    async def test_success_ru(self, monkeypatch, tmp_path):
        sent = await self._run_finished(monkeypatch, tmp_path, exit_code="0", lang="ru")
        assert sent == "✅ Обновление завершено."

    @pytest.mark.asyncio
    async def test_failure_en(self, monkeypatch, tmp_path):
        sent = await self._run_finished(monkeypatch, tmp_path, exit_code="3", lang="en")
        assert sent == "❌ Update failed (exit code 3)."

    @pytest.mark.asyncio
    async def test_failure_ru(self, monkeypatch, tmp_path):
        sent = await self._run_finished(monkeypatch, tmp_path, exit_code="3", lang="ru")
        assert sent == "❌ Обновление не выполнено (код выхода 3)."


class TestWatchUpdateProgressTimeout:
    @pytest.mark.asyncio
    async def test_timeout_en(self, monkeypatch, tmp_path):
        _set_lang(monkeypatch, "en")
        runner = _make_update_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / ".update_pending.json").write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}
        import gateway.run as gateway_run
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        await runner._watch_update_progress(poll_interval=0.01, stream_interval=0.01, timeout=0.03)
        sent = mock_adapter.send.call_args_list[-1].args[1]
        assert sent == "❌ Update timed out after 30 minutes."

    @pytest.mark.asyncio
    async def test_timeout_ru(self, monkeypatch, tmp_path):
        _set_lang(monkeypatch, "ru")
        runner = _make_update_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / ".update_pending.json").write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}
        import gateway.run as gateway_run
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        await runner._watch_update_progress(poll_interval=0.01, stream_interval=0.01, timeout=0.03)
        sent = mock_adapter.send.call_args_list[-1].args[1]
        assert sent == "❌ Обновление прервано по тайм-ауту (30 минут)."


class TestWatchUpdateProgressNeedsInput:
    async def _run_prompt(self, monkeypatch, tmp_path, lang):
        _set_lang(monkeypatch, lang)
        runner = _make_update_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / ".update_pending.json").write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        (hermes_home / ".update_prompt.json").write_text(json.dumps({
            "prompt": "Overwrite local changes?", "default": "yes",
        }))
        mock_adapter = AsyncMock()
        mock_adapter.typed_command_prefix = "/"
        runner.adapters = {Platform.TELEGRAM: mock_adapter}
        import gateway.run as gateway_run
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        await runner._watch_update_progress(poll_interval=0.01, stream_interval=0.01, timeout=0.03)
        # First call is the forwarded prompt; the last is the timeout notice
        # (no exit code is ever written in this scenario).
        return mock_adapter.send.call_args_list[0].args[1]

    @pytest.mark.asyncio
    async def test_prompt_en(self, monkeypatch, tmp_path):
        sent = await self._run_prompt(monkeypatch, tmp_path, "en")
        assert "Update needs your input" in sent
        assert "Overwrite local changes?" in sent
        assert "(default: yes)" in sent
        assert "`/approve`" in sent and "`/deny`" in sent

    @pytest.mark.asyncio
    async def test_prompt_ru(self, monkeypatch, tmp_path):
        sent = await self._run_prompt(monkeypatch, tmp_path, "ru")
        assert "Overwrite local changes?" in sent
        assert "(по умолчанию: yes)" in sent
        assert "`/approve`" in sent and "`/deny`" in sent


class TestSendUpdateNotificationLocalized:
    async def _run(self, monkeypatch, tmp_path, *, output: str, exit_code: str, lang: str):
        _set_lang(monkeypatch, lang)
        runner = _make_update_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending = {"platform": "telegram", "chat_id": "67890", "user_id": "12345"}
        (hermes_home / ".update_pending.json").write_text(json.dumps(pending))
        if output:
            (hermes_home / ".update_output.txt").write_text(output)
        (hermes_home / ".update_exit_code").write_text(exit_code)
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}
        import gateway.run as gateway_run
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        await runner._send_update_notification()
        return mock_adapter.send.call_args[0][1]

    @pytest.mark.asyncio
    async def test_finished_with_output_en(self, monkeypatch, tmp_path):
        sent = await self._run(monkeypatch, tmp_path, output="log line", exit_code="0", lang="en")
        assert sent == "✅ Update finished.\n\n```\nlog line\n```"

    @pytest.mark.asyncio
    async def test_finished_with_output_ru(self, monkeypatch, tmp_path):
        sent = await self._run(monkeypatch, tmp_path, output="log line", exit_code="0", lang="ru")
        assert sent == "✅ Обновление завершено.\n\n```\nlog line\n```"

    @pytest.mark.asyncio
    async def test_failed_with_output_en(self, monkeypatch, tmp_path):
        sent = await self._run(monkeypatch, tmp_path, output="log line", exit_code="1", lang="en")
        assert sent == "❌ Update failed.\n\n```\nlog line\n```"

    @pytest.mark.asyncio
    async def test_failed_with_output_ru(self, monkeypatch, tmp_path):
        sent = await self._run(monkeypatch, tmp_path, output="log line", exit_code="1", lang="ru")
        assert sent == "❌ Обновление не выполнено.\n\n```\nlog line\n```"

    @pytest.mark.asyncio
    async def test_finished_no_output_en(self, monkeypatch, tmp_path):
        sent = await self._run(monkeypatch, tmp_path, output="", exit_code="0", lang="en")
        assert sent == "✅ Update finished successfully."

    @pytest.mark.asyncio
    async def test_finished_no_output_ru(self, monkeypatch, tmp_path):
        sent = await self._run(monkeypatch, tmp_path, output="", exit_code="0", lang="ru")
        assert sent == "✅ Обновление успешно завершено."

    @pytest.mark.asyncio
    async def test_failed_no_output_en(self, monkeypatch, tmp_path):
        """Meaning change: the Telegram-only client has no gateway logs and
        no terminal to run `hermes update` manually -- the notice must not
        mention either."""
        sent = await self._run(monkeypatch, tmp_path, output="", exit_code="1", lang="en")
        assert sent == "❌ Update failed. Ask whoever administers this machine to check it."
        assert "gateway logs" not in sent
        assert "hermes update" not in sent

    @pytest.mark.asyncio
    async def test_failed_no_output_ru(self, monkeypatch, tmp_path):
        sent = await self._run(monkeypatch, tmp_path, output="", exit_code="1", lang="ru")
        assert sent == "❌ Обновление не выполнено. Сообщите тому, кто администрирует эту машину."


# ===========================================================================
# Cluster 4 -- background-process watcher (trix.background.*)
# ===========================================================================


class _FakeProcessRegistry:
    """Returns pre-canned sessions in order, then None."""

    def __init__(self, sessions):
        self._sessions = list(sessions)

    def get(self, session_id):
        return self._sessions.pop(0) if self._sessions else None

    def is_completion_consumed(self, session_id):
        return False


def _build_watcher_runner(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner

    (tmp_path / "config.yaml").write_text(
        "display:\n  background_process_notifications: all\n", encoding="utf-8"
    )
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(send=AsyncMock(), handle_message=AsyncMock())
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner, adapter


class TestProcessWatcherLocalized:
    async def _run(self, monkeypatch, tmp_path, lang):
        _set_lang(monkeypatch, lang)
        runner, adapter = _build_watcher_runner(monkeypatch, tmp_path)
        import tools.process_registry as pr_module
        still_running = SimpleNamespace(
            output_buffer="working...\n", exited=False, exit_code=None, command="run.sh",
        )
        finished = SimpleNamespace(
            output_buffer="working...\ndone\n", exited=True, exit_code=0, command="run.sh",
        )
        monkeypatch.setattr(pr_module, "process_registry", _FakeProcessRegistry([still_running, finished]))
        watcher = {
            "session_id": "proc1", "check_interval": 0, "platform": "telegram", "chat_id": "123",
        }
        await runner._run_process_watcher(watcher)
        calls = [c.args[1] for c in adapter.send.call_args_list]
        assert len(calls) == 2
        return calls

    @pytest.mark.asyncio
    async def test_localized_en(self, monkeypatch, tmp_path):
        running, finished = await self._run(monkeypatch, tmp_path, "en")
        assert running == "[Background process proc1 is still running. New output:\nworking...\n]"
        assert finished == (
            "[Background process proc1 finished with exit code 0. "
            "Here's the final output:\nworking...\ndone\n]"
        )
        assert "~" not in running and "~" not in finished

    @pytest.mark.asyncio
    async def test_localized_ru(self, monkeypatch, tmp_path):
        running, finished = await self._run(monkeypatch, tmp_path, "ru")
        assert running == "[Фоновый процесс proc1 всё ещё выполняется. Новый вывод:\nworking...\n]"
        assert finished == (
            "[Фоновый процесс proc1 завершён, код выхода 0. "
            "Вот итоговый вывод:\nworking...\ndone\n]"
        )
        assert "~" not in running and "~" not in finished


# ===========================================================================
# Cluster 3 -- slash-command dispatch replies (trix.cmd.*)
# ===========================================================================


def _make_command_runner():
    """Combines tests/gateway/test_stacked_skill_platform_disabled.py's
    dispatch runner with tests/gateway/test_destructive_slash_confirm.py's
    confirm-gate runner -- enough state to drive ``_handle_message`` through
    the hook/quick-command/skill/confirm/unknown-command sections."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter.send_slash_confirm = AsyncMock(return_value=None)
    adapter.typed_command_prefix = "/"
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )

    source = _make_source()
    session_entry = SessionEntry(
        session_key=build_session_key(source),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._thread_metadata_for_source = lambda *a, **kw: None
    runner._reply_anchor_for_event = lambda _e: None
    import itertools as _it
    runner._slash_confirm_counter = _it.count(1)
    from gateway.run import GatewayRunner as _GR
    runner._session_key_for_source = _GR._session_key_for_source.__get__(runner, _GR)
    return runner


class TestConfirmScreen:
    @pytest.mark.asyncio
    async def test_new_prompt_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        out = await runner._handle_message(_make_event("/new"))
        assert "Confirm /new" in out
        assert "discards the current conversation history" in out
        assert "Approve Once" in out and "Always Approve" in out and "Cancel" in out
        assert "`/approve`" in out and "`/always`" in out and "`/cancel`" in out

    @pytest.mark.asyncio
    async def test_new_prompt_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()
        out = await runner._handle_message(_make_event("/new"))
        assert "Подтвердите /new" in out
        assert "начнёт новую сессию" in out
        # Task 9b: button labels used to stay literally English here ("the
        # real Telegram buttons" the old comment pointed at) -- that was the
        # defect, not a fact about the buttons. The prompt text now names
        # the SAME (translated) labels the real Telegram keyboard renders.
        assert "Разрешить один раз" in out
        assert "Разрешать всегда" in out
        assert "Отмена" in out
        assert "Approve Once" not in out and "Always Approve" not in out
        assert "`/approve`" in out and "`/always`" in out and "`/cancel`" in out

    @pytest.mark.asyncio
    async def test_undo_one_detail_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        out = await runner._handle_message(_make_event("/undo"))
        assert "This removes the last user/assistant exchange from history." in out

    @pytest.mark.asyncio
    async def test_undo_many_detail_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()
        out = await runner._handle_message(_make_event("/undo 3"))
        assert "последние 3 пользовательских реплик" in out

    @pytest.mark.asyncio
    async def test_cancel_en(self, monkeypatch):
        from tools import slash_confirm as _slash_confirm_mod
        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        session_key = build_session_key(_make_source())
        runner._session_key_for_source = lambda src: session_key
        _slash_confirm_mod.clear(session_key)
        await runner._handle_message(_make_event("/new"))
        pending = _slash_confirm_mod.get_pending(session_key)
        out = await _slash_confirm_mod.resolve(session_key, pending["confirm_id"], "cancel")
        assert out == "🟡 /new cancelled. Conversation unchanged."

    @pytest.mark.asyncio
    async def test_cancel_ru(self, monkeypatch):
        from tools import slash_confirm as _slash_confirm_mod
        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()
        session_key = build_session_key(_make_source())
        runner._session_key_for_source = lambda src: session_key
        _slash_confirm_mod.clear(session_key)
        await runner._handle_message(_make_event("/new"))
        pending = _slash_confirm_mod.get_pending(session_key)
        out = await _slash_confirm_mod.resolve(session_key, pending["confirm_id"], "cancel")
        assert "отменено" in out

    async def _resolve_always(self, monkeypatch, lang, *, persisted):
        from tools import slash_confirm as _slash_confirm_mod
        _set_lang(monkeypatch, lang)
        runner = _make_command_runner()
        session_key = build_session_key(_make_source())
        runner._session_key_for_source = lambda src: session_key
        _slash_confirm_mod.clear(session_key)
        import cli as cli_mod
        monkeypatch.setattr(cli_mod, "save_config_value", lambda *a, **k: persisted)
        await runner._handle_message(_make_event("/new"))
        pending = _slash_confirm_mod.get_pending(session_key)
        return await _slash_confirm_mod.resolve(session_key, pending["confirm_id"], "always")

    @pytest.mark.asyncio
    async def test_always_saved_en(self, monkeypatch):
        out = await self._resolve_always(monkeypatch, "en", persisted=True)
        assert "will run without confirmation" in out
        # Meaning change: point at "whoever administers this machine",
        # not at a config.yaml key the Telegram-only client can't edit.
        assert "approvals.destructive_slash_confirm" not in out
        assert "administers this machine" in out

    @pytest.mark.asyncio
    async def test_always_saved_ru(self, monkeypatch):
        out = await self._resolve_always(monkeypatch, "ru", persisted=True)
        assert "больше не будут спрашивать подтверждения" in out
        assert "approvals.destructive_slash_confirm" not in out

    @pytest.mark.asyncio
    async def test_always_not_saved_en(self, monkeypatch):
        out = await self._resolve_always(monkeypatch, "en", persisted=False)
        assert "Could not save that preference" in out
        assert "approvals.destructive_slash_confirm" not in out
        assert "will run without confirmation" not in out

    @pytest.mark.asyncio
    async def test_always_not_saved_ru(self, monkeypatch):
        out = await self._resolve_always(monkeypatch, "ru", persisted=False)
        assert "Не удалось сохранить эту настройку" in out
        assert "approvals.destructive_slash_confirm" not in out


class TestHookBlocked:
    @pytest.mark.asyncio
    async def test_blocked_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        runner.hooks.emit_collect = AsyncMock(return_value=[{"decision": "deny"}])
        out = await runner._handle_message(_make_event("/status"))
        assert out == "Command `/status` was blocked."
        assert "hook" not in out.lower()

    @pytest.mark.asyncio
    async def test_blocked_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()
        runner.hooks.emit_collect = AsyncMock(return_value=[{"decision": "deny"}])
        out = await runner._handle_message(_make_event("/status"))
        assert out == "Команда `/status` заблокирована."


class TestUnknownCommand:
    @pytest.mark.asyncio
    async def test_unknown_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        out = await runner._handle_message(_make_event("/totallyunknownxyz"))
        assert out == (
            "Unknown command `/totallyunknownxyz`. Type /commands to see what's "
            "available, or resend without the leading slash to send as a "
            "regular message."
        )

    @pytest.mark.asyncio
    async def test_unknown_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()
        out = await runner._handle_message(_make_event("/totallyunknownxyz"))
        assert "Неизвестная команда" in out
        assert "/totallyunknownxyz" in out


class TestLearnAck:
    """/learn is now in hermes_cli.trix_menu.DISABLED_COMMANDS (client-
    command-surface plan Task 1, hint="words": the command was only ever a
    canned phrase forwarded to the agent, and the same result is reachable
    by asking in plain language, so there is no acknowledgment message left
    to localize). These tests used to pin the localized "starting to
    learn…" ack sent via adapter.send() before agent.learn_prompt ran; that
    code path -- ack included -- is unreachable now, closed off by the
    disabled-command check ahead of it. Decoupled from the exact disabled-
    reply wording (that catalog text belongs to a parallel task) by
    patching hermes_cli.trix_disabled_reply.disabled_command_reply -- see
    tests/gateway/test_disabled_commands.py for the same pattern."""

    @pytest.mark.asyncio
    async def test_disabled_en(self, monkeypatch):
        import hermes_cli.trix_disabled_reply as _reply_mod

        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        build_prompt = MagicMock(side_effect=AssertionError("build_learn_prompt must not run"))
        monkeypatch.setattr("agent.learn_prompt.build_learn_prompt", build_prompt)

        with patch.object(_reply_mod, "disabled_command_reply", lambda name: f"DISABLED:{name}"):
            out = await runner._handle_message(_make_event("/learn how to deploy"))

        assert out == "DISABLED:learn"
        build_prompt.assert_not_called()
        runner.adapters[Platform.TELEGRAM].send.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_ru(self, monkeypatch):
        import hermes_cli.trix_disabled_reply as _reply_mod

        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()

        with patch.object(_reply_mod, "disabled_command_reply", lambda name: f"DISABLED:{name}"):
            out = await runner._handle_message(_make_event("/learn"))

        assert out == "DISABLED:learn"
        runner.adapters[Platform.TELEGRAM].send.assert_not_called()


class TestInitAck:
    """/init is now in hermes_cli.trix_menu.DISABLED_COMMANDS (client-
    command-surface plan Task 1, hint="words": no confirmation before it
    scans the filesystem and writes AGENTS.md, and its prompt is English).
    Same shape as TestLearnAck above -- the old ack-then-build path is
    unreachable now."""

    @pytest.mark.asyncio
    async def test_disabled_en(self, monkeypatch):
        import hermes_cli.trix_disabled_reply as _reply_mod

        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        build_prompt = MagicMock(
            side_effect=AssertionError("build_init_prompt_for_cwd must not run")
        )
        monkeypatch.setattr("hermes_cli.init_command.build_init_prompt_for_cwd", build_prompt)

        with patch.object(_reply_mod, "disabled_command_reply", lambda name: f"DISABLED:{name}"):
            out = await runner._handle_message(_make_event("/init"))

        assert out == "DISABLED:init"
        build_prompt.assert_not_called()
        runner.adapters[Platform.TELEGRAM].send.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_ru(self, monkeypatch):
        import hermes_cli.trix_disabled_reply as _reply_mod

        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()

        with patch.object(_reply_mod, "disabled_command_reply", lambda name: f"DISABLED:{name}"):
            out = await runner._handle_message(_make_event("/init"))

        assert out == "DISABLED:init"
        runner.adapters[Platform.TELEGRAM].send.assert_not_called()


class TestMoaPrepareFailed:
    """/moa is now in hermes_cli.trix_menu.DISABLED_COMMANDS (client-
    command-surface plan Task 1, hint="replace" -> /model: the preset is
    built from providers the client has no keys for, so it silently made a
    real call and failed, burning a turn). The prepare-failure branch these
    tests pinned is unreachable now -- the disabled check answers before
    _session_state() (the mocked failure point) is ever called."""

    @pytest.mark.asyncio
    async def test_disabled_en(self, monkeypatch):
        import hermes_cli.trix_disabled_reply as _reply_mod

        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        runner._session_state = MagicMock(side_effect=RuntimeError("boom"))

        with patch.object(_reply_mod, "disabled_command_reply", lambda name: f"DISABLED:{name}"):
            out = await runner._handle_message(_make_event("/moa say hi"))

        assert out == "DISABLED:moa"
        runner._session_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_ru(self, monkeypatch):
        import hermes_cli.trix_disabled_reply as _reply_mod

        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()
        runner._session_state = MagicMock(side_effect=RuntimeError("boom"))

        with patch.object(_reply_mod, "disabled_command_reply", lambda name: f"DISABLED:{name}"):
            out = await runner._handle_message(_make_event("/moa say hi"))

        assert out == "DISABLED:moa"
        runner._session_state.assert_not_called()


class TestQuickCommands:
    async def _dispatch(self, monkeypatch, lang, qcmd):
        _set_lang(monkeypatch, lang)
        runner = _make_command_runner()
        runner.config.quick_commands = {"foo": qcmd}
        return await runner._handle_message(_make_event("/foo"))

    @pytest.mark.asyncio
    async def test_no_command_en(self, monkeypatch):
        out = await self._dispatch(monkeypatch, "en", {"type": "exec", "command": ""})
        assert out == "Quick command '/foo' has no command defined."

    @pytest.mark.asyncio
    async def test_no_command_ru(self, monkeypatch):
        out = await self._dispatch(monkeypatch, "ru", {"type": "exec", "command": ""})
        assert out == "У быстрой команды `/foo` не задана команда."

    @pytest.mark.asyncio
    async def test_no_target_en(self, monkeypatch):
        out = await self._dispatch(monkeypatch, "en", {"type": "alias", "target": ""})
        assert out == "Quick command '/foo' has no target defined."

    @pytest.mark.asyncio
    async def test_no_target_ru(self, monkeypatch):
        out = await self._dispatch(monkeypatch, "ru", {"type": "alias", "target": ""})
        assert out == "У быстрой команды `/foo` не задана цель."

    @pytest.mark.asyncio
    async def test_unsupported_type_en(self, monkeypatch):
        out = await self._dispatch(monkeypatch, "en", {"type": "weird"})
        assert out == "Quick command '/foo' has unsupported type (supported: 'exec', 'alias')."

    @pytest.mark.asyncio
    async def test_unsupported_type_ru(self, monkeypatch):
        out = await self._dispatch(monkeypatch, "ru", {"type": "weird"})
        assert "неподдерживаемый тип" in out

    @pytest.mark.asyncio
    async def test_no_output_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        runner.config.quick_commands = {"foo": {"type": "exec", "command": "true"}}
        out = await runner._handle_message(_make_event("/foo"))
        assert out == "Command returned no output."

    @pytest.mark.asyncio
    async def test_no_output_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()
        runner.config.quick_commands = {"foo": {"type": "exec", "command": "true"}}
        out = await runner._handle_message(_make_event("/foo"))
        assert out == "Команда не вернула вывод."

    @pytest.mark.asyncio
    async def test_error_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        runner.config.quick_commands = {"foo": {"type": "exec", "command": "true"}}
        monkeypatch.setattr(
            asyncio, "create_subprocess_shell",
            AsyncMock(side_effect=RuntimeError("boom")),
        )
        out = await runner._handle_message(_make_event("/foo"))
        assert out == "Quick command error: boom"

    @pytest.mark.asyncio
    async def test_error_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()
        runner.config.quick_commands = {"foo": {"type": "exec", "command": "true"}}
        monkeypatch.setattr(
            asyncio, "create_subprocess_shell",
            AsyncMock(side_effect=RuntimeError("boom")),
        )
        out = await runner._handle_message(_make_event("/foo"))
        assert out == "Ошибка быстрой команды: boom"

    @pytest.mark.asyncio
    async def test_timeout_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        runner.config.quick_commands = {"foo": {"type": "exec", "command": "sleep 5"}}
        fake_proc = AsyncMock()
        monkeypatch.setattr(
            asyncio, "create_subprocess_shell", AsyncMock(return_value=fake_proc)
        )
        monkeypatch.setattr(asyncio, "wait_for", AsyncMock(side_effect=asyncio.TimeoutError))
        out = await runner._handle_message(_make_event("/foo"))
        assert out == "Quick command timed out (30s)."

    @pytest.mark.asyncio
    async def test_timeout_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()
        runner.config.quick_commands = {"foo": {"type": "exec", "command": "sleep 5"}}
        fake_proc = AsyncMock()
        monkeypatch.setattr(
            asyncio, "create_subprocess_shell", AsyncMock(return_value=fake_proc)
        )
        monkeypatch.setattr(asyncio, "wait_for", AsyncMock(side_effect=asyncio.TimeoutError))
        out = await runner._handle_message(_make_event("/foo"))
        assert out == "Быстрая команда прервана по тайм-ауту (30 с)."


def _make_skill_file(skills_dir, name, body="content"):
    sd = skills_dir / name
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: desc {name}\n---\n\n# {name}\n\n{body}\n"
    )


@pytest.fixture
def skills_env(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    import tools.skills_tool as skills_tool_module
    monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", skills_dir)
    import agent.skill_commands as skill_commands_mod
    skill_commands_mod._skill_commands = {}
    skill_commands_mod._skill_commands_platform = None
    return skills_dir


class TestSkillDisabledForPlatform:
    @pytest.mark.asyncio
    async def test_single_skill_disabled_en(self, monkeypatch, skills_env):
        import gateway.run as gateway_run
        import agent.skill_utils as skill_utils_mod

        _make_skill_file(skills_env, "solo-skill")
        monkeypatch.setattr(
            skill_utils_mod, "get_disabled_skill_names",
            lambda platform=None: {"solo-skill"} if platform == "telegram" else set(),
        )
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        out = await runner._handle_message(_make_event("/solo-skill do something"))
        assert out == (
            "The **solo-skill** skill is disabled for telegram.\n"
            "Ask whoever administers this machine to enable it."
        )

    @pytest.mark.asyncio
    async def test_single_skill_disabled_ru(self, monkeypatch, skills_env):
        import gateway.run as gateway_run
        import agent.skill_utils as skill_utils_mod

        _make_skill_file(skills_env, "solo-skill")
        monkeypatch.setattr(
            skill_utils_mod, "get_disabled_skill_names",
            lambda platform=None: {"solo-skill"} if platform == "telegram" else set(),
        )
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()
        out = await runner._handle_message(_make_event("/solo-skill do something"))
        assert "solo-skill" in out
        assert "отключён для telegram" in out
        assert "hermes skills config" not in out

    @pytest.mark.asyncio
    async def test_stacked_skill_disabled_en(self, monkeypatch, skills_env):
        import gateway.run as gateway_run
        import agent.skill_utils as skill_utils_mod

        _make_skill_file(skills_env, "allowed-skill")
        _make_skill_file(skills_env, "disabled-skill")
        monkeypatch.setattr(
            skill_utils_mod, "get_disabled_skill_names",
            lambda platform=None: {"disabled-skill"} if platform == "telegram" else set(),
        )
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
        _set_lang(monkeypatch, "en")
        runner = _make_command_runner()
        out = await runner._handle_message(_make_event("/allowed-skill /disabled-skill do something"))
        assert "disabled-skill" in out
        assert "disabled for telegram" in out
        assert "hermes skills config" not in out
        assert "administers this machine" in out

    @pytest.mark.asyncio
    async def test_stacked_load_failed_ru(self, monkeypatch, skills_env):
        import gateway.run as gateway_run
        import agent.skill_commands as skill_commands_mod
        import agent.skill_utils as skill_utils_mod

        _make_skill_file(skills_env, "allowed-skill")
        _make_skill_file(skills_env, "second-skill")
        monkeypatch.setattr(skill_utils_mod, "get_disabled_skill_names", lambda platform=None: set())
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
        monkeypatch.setattr(
            skill_commands_mod, "build_stacked_skill_invocation_message",
            lambda *a, **k: None,
        )
        _set_lang(monkeypatch, "ru")
        runner = _make_command_runner()
        out = await runner._handle_message(_make_event("/allowed-skill /second-skill do something"))
        assert out == "Не удалось загрузить связку навыков для /allowed-skill."


class TestCheckUnavailableSkill:
    def test_installed_disabled_en(self, monkeypatch, tmp_path):
        from gateway.run import _check_unavailable_skill
        import agent.skill_utils as skill_utils_mod
        import tools.skills_tool as skills_tool_mod

        skills_dir = tmp_path / "skills"
        _make_skill_file(skills_dir, "quiet-skill")
        monkeypatch.setattr(skill_utils_mod, "get_all_skills_dirs", lambda: [skills_dir])
        monkeypatch.setattr(skill_utils_mod, "is_excluded_skill_path", lambda *a, **k: False)
        monkeypatch.setattr(skills_tool_mod, "_get_disabled_skill_names", lambda: {"quiet-skill"})
        _set_lang(monkeypatch, "en")
        out = _check_unavailable_skill("quiet-skill")
        assert out == (
            "The **quiet-skill** skill is installed but disabled.\n"
            "Ask whoever administers this machine to enable it."
        )
        assert "hermes skills config" not in out

    def test_installed_disabled_ru(self, monkeypatch, tmp_path):
        from gateway.run import _check_unavailable_skill
        import agent.skill_utils as skill_utils_mod
        import tools.skills_tool as skills_tool_mod

        skills_dir = tmp_path / "skills"
        _make_skill_file(skills_dir, "quiet-skill")
        monkeypatch.setattr(skill_utils_mod, "get_all_skills_dirs", lambda: [skills_dir])
        monkeypatch.setattr(skill_utils_mod, "is_excluded_skill_path", lambda *a, **k: False)
        monkeypatch.setattr(skills_tool_mod, "_get_disabled_skill_names", lambda: {"quiet-skill"})
        _set_lang(monkeypatch, "ru")
        out = _check_unavailable_skill("quiet-skill")
        assert "установлен, но отключён" in out
        assert "hermes skills config" not in out

    def test_not_installed_en(self, monkeypatch, tmp_path):
        from gateway.run import _check_unavailable_skill
        import agent.skill_utils as skill_utils_mod
        import hermes_constants

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        optional_dir = tmp_path / "optional"
        _make_skill_file(optional_dir / "official" / "productivity", "hidden-gem")
        monkeypatch.setattr(skill_utils_mod, "get_all_skills_dirs", lambda: [empty_dir])
        monkeypatch.setattr(skill_utils_mod, "is_excluded_skill_path", lambda *a, **k: False)
        monkeypatch.setattr(hermes_constants, "get_optional_skills_dir", lambda *a, **k: optional_dir)
        _set_lang(monkeypatch, "en")
        out = _check_unavailable_skill("hidden-gem")
        assert out == (
            "The **hidden-gem** skill is available but not installed.\n"
            "Ask whoever administers this machine to install it."
        )
        assert "hermes skills install" not in out

    def test_not_installed_ru(self, monkeypatch, tmp_path):
        from gateway.run import _check_unavailable_skill
        import agent.skill_utils as skill_utils_mod
        import hermes_constants

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        optional_dir = tmp_path / "optional"
        _make_skill_file(optional_dir / "official" / "productivity", "hidden-gem")
        monkeypatch.setattr(skill_utils_mod, "get_all_skills_dirs", lambda: [empty_dir])
        monkeypatch.setattr(skill_utils_mod, "is_excluded_skill_path", lambda *a, **k: False)
        monkeypatch.setattr(hermes_constants, "get_optional_skills_dir", lambda *a, **k: optional_dir)
        _set_lang(monkeypatch, "ru")
        out = _check_unavailable_skill("hidden-gem")
        assert "доступен, но не установлен" in out
        assert "hermes skills install" not in out


class TestHomeChannelNotice:
    """``_handle_message_with_agent``'s first-turn onboarding notice sits
    deep inside a ~1000-line method with heavy agent-loop dependencies;
    driving it end-to-end is disproportionate to this one string. The call
    site delegates to the module-level ``_home_channel_not_set_notice()``
    (same pattern as ``_gateway_provider_error_reply``), so calling that
    function here executes the real call site instead of a copy of it --
    reverting the call site back to an inline literal breaks these tests."""

    def test_not_set_en(self, monkeypatch):
        _set_lang(monkeypatch, "en")
        from gateway.run import _home_channel_not_set_notice
        out = _home_channel_not_set_notice("telegram", "/sethome")
        assert "No home channel is set for Telegram" in out
        assert "Hermes" not in out

    def test_not_set_ru(self, monkeypatch):
        _set_lang(monkeypatch, "ru")
        from gateway.run import _home_channel_not_set_notice
        out = _home_channel_not_set_notice("telegram", "/sethome")
        assert "Для Telegram не задан домашний канал" in out
        assert "/sethome" in out
        assert "Hermes" not in out
