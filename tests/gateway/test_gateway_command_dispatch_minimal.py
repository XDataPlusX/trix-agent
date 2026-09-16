from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
        message_id="m1",
        internal=True,
    )


def _session_entry() -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry()
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._update_prompt_pending = {}
    runner._busy_input_mode = "interrupt"
    runner._draining = False
    runner._session_run_generation = {}
    runner._session_sources = {}
    runner._pending_native_image_paths_by_session = {}
    runner._background_tasks = {}
    runner._background_task_counter = 0
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._service_tier = None
    runner._fast_mode_by_session = {}
    runner._goal_state_by_session = {}
    runner._goal_runs_in_progress = set()
    runner._goal_queued_by_session = set()
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._should_send_telegram_lobby_reminder = lambda _source: False
    runner._check_slash_access = lambda _source, _command: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(key, None)
    return runner, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("command_text", ["/queue do this next", "/q do this next"])
async def test_idle_queue_sends_payload_as_next_turn(command_text):
    runner, _adapter = _make_runner()
    captured = {}

    async def fake_handle_message_with_agent(event, source, key, generation):
        captured["text"] = event.text
        captured["command"] = event.get_command()
        captured["source"] = source
        captured["key"] = key
        captured["generation"] = generation
        return {"final_response": "", "messages": []}

    runner._handle_message_with_agent = fake_handle_message_with_agent

    result = await runner._handle_message(_make_event(command_text))

    assert result == {"final_response": "", "messages": []}
    assert captured["text"] == "do this next"
    assert captured["command"] is None
    assert captured["source"] == _make_source()
    assert captured["key"] == build_session_key(_make_source())
    assert captured["generation"] == 1
    assert runner._running_agents == {}




@pytest.mark.asyncio
async def test_disk_is_answered_by_the_gateway_not_forwarded_to_the_model(
    tmp_path, monkeypatch
):
    """`/disk` must be dispatched here, not handed to the agent.

    HERMES_HOME is not mounted into the agent's sandbox and there is no
    Docker socket in there, so a `/disk` that falls through the canonical
    chain reaches a model that cannot see a single one of the files the
    client is asking about. Silent fall-through is the failure mode this
    pins (see hermes_cli/trix_disk.py's module docstring).
    """
    runner, _adapter = _make_runner()
    forwarded = {}

    async def fake_handle_message_with_agent(event, source, key, generation):
        forwarded["text"] = event.text
        return {"final_response": "", "messages": []}

    runner._handle_message_with_agent = fake_handle_message_with_agent
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    result = await runner._handle_message(_make_event("/disk"))

    assert not forwarded, "/disk ушёл в модель вместо ответа шлюза"
    assert isinstance(result, str)
    assert "%" in result


def _make_auto_reset_runner(reason: str, *, idle_minutes: int = 4320):
    """``_make_runner()`` плюс ровно то, что нужно, чтобы РЕАЛЬНЫЙ
    ``_handle_message_with_agent`` дошёл до уведомления о новом разговоре.

    Всё, что метод трогает по дороге, заглушено одной строкой на вызов;
    сам блок уведомления остаётся настоящим. Это и есть предмет теста:
    проверить надо не функцию (она покрыта в
    ``tests/hermes_cli/test_trix_session_notices.py``), а ПОДКЛЮЧЕНИЕ —
    что ``gateway/run.py`` действительно её зовёт. В этой ветке уже было:
    точку вызова вернули на английский литерал, и 85 тестов остались
    зелёными, потому что покрыта была только функция.
    """
    from gateway.config import SessionResetPolicy

    runner, adapter = _make_runner()

    entry = _session_entry()
    entry.was_auto_reset = True
    entry.auto_reset_reason = reason
    entry.reset_had_activity = True

    # ``async_session_store`` — свойство без сеттера: оно пересобирает фасад,
    # если у закешированного ``_store`` не тот самый SessionStore. Поэтому
    # подсовываем фасад в кеш и проставляем ему ``_store``, иначе свойство
    # молча заменит наш мок настоящим AsyncSessionStore.
    facade = MagicMock()
    facade._store = runner.session_store
    facade.get_or_create_session = AsyncMock(return_value=entry)
    runner._async_session_store = facade
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_telegram_topic_lane = lambda _source: False
    runner._clear_conversation_scope = lambda _key, reason=None: None
    runner._evict_cached_agent = lambda _key: None
    runner._pinned_session_context_prompt = lambda _ctx, _redact, _key: ""
    runner._adapter_for_source = lambda _source: adapter
    runner._thread_metadata_for_source = lambda _source: None
    # Блок «модель / провайдер / контекст» под уведомлением — не предмет
    # этого теста и тянул бы резолв модели с сетью. Пусто = уведомление
    # уходит клиенту как есть.
    runner._reset_notice_session_info = lambda _source: ""
    runner.session_store.config = MagicMock()
    runner.session_store.config.get_reset_policy.return_value = SessionResetPolicy(
        mode="idle", idle_minutes=idle_minutes
    )
    return runner, adapter, entry


class _StopAfterNotice(BaseException):
    """Наследник BaseException намеренно.

    Уведомление отправляется внутри двух вложенных ``except Exception``.
    Обычное исключение из ``adapter.send`` было бы проглочено, метод пошёл
    бы дальше — строить и запускать агента, чего в этом тесте нет и быть
    не должно. ``BaseException`` проходит оба обработчика насквозь и
    обрывает ход ровно там, где уведомление уже отправлено.
    """


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["idle", "suspended", "resume_pending_expired", "daily"]
)
async def test_auto_reset_notice_is_the_russian_text_our_module_renders(
    reason, monkeypatch
):
    """Стык: уведомление о новом разговоре родилось в нашем модуле.

    Проверяем не «текст выглядит правильным», а тождество с тем, что
    отдаёт ``hermes_cli.trix_session_notices.session_reset_notice`` для
    той же причины и того же срока — отношение между двумя сторонами
    стыка, а не снимок формулировки. Возврат английского литерала в
    точку вызова красит этот тест.
    """
    from hermes_cli.trix_session_notices import session_reset_notice

    # Тесты идут против временного HERMES_HOME без config.yaml, где язык
    # резолвится в "en". Клиент Trix читает русский — закрепляем явно, иначе
    # тождество ниже держалось бы на английской паре и ничего не говорило бы
    # о том, что видит клиент.
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")

    runner, adapter, _entry = _make_auto_reset_runner(reason)
    sent = {}

    async def _capture(chat_id, content, **kwargs):
        sent["chat_id"] = chat_id
        sent["content"] = content
        raise _StopAfterNotice

    adapter.send = _capture

    with pytest.raises(_StopAfterNotice):
        await runner._handle_message_with_agent(
            _make_event("привет"), _make_source(), "quick", 1
        )

    assert sent["chat_id"] == "c1"
    assert sent["content"] == session_reset_notice(reason, idle_minutes=4320)
    assert "Session automatically reset" not in sent["content"]
    assert "Conversation history cleared" not in sent["content"]


@pytest.mark.asyncio
async def test_the_notice_names_the_span_configured_for_this_gateway(monkeypatch):
    """Срок в уведомлении приходит из политики шлюза, а не вшит в текст.

    Отдельно от предыдущего теста: тождество с функцией держалось бы и
    при захардкоженном сроке, если бы обе стороны врали одинаково. Здесь
    два РАЗНЫХ шлюза обязаны сказать клиенту разное.
    """
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    runner_three_days, adapter_a, _ = _make_auto_reset_runner("idle", idle_minutes=4320)
    runner_twelve_hours, adapter_b, _ = _make_auto_reset_runner("idle", idle_minutes=720)
    sent = {}

    def _capture(key, adapter):
        async def _send(chat_id, content, **kwargs):
            sent[key] = content
            raise _StopAfterNotice

        adapter.send = _send

    _capture("three_days", adapter_a)
    _capture("twelve_hours", adapter_b)

    for runner in (runner_three_days, runner_twelve_hours):
        with pytest.raises(_StopAfterNotice):
            await runner._handle_message_with_agent(
                _make_event("привет"), _make_source(), "quick", 1
            )

    assert "трое суток" in sent["three_days"]
    assert "12 часов" in sent["twelve_hours"]
    assert sent["three_days"] != sent["twelve_hours"]
