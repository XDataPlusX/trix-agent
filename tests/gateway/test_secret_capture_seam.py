"""Шов, на котором держится спека 19, и который ломается без единого признака.

Приёмник запроса ключа регистрируется в `TurnRunner.run_sync` по
`ctx.session_key`. Диспетчер, которого зовёт инструмент `secret_request`,
ищет его по ContextVar `HERMES_SESSION_KEY`, выставленной совсем в
другом месте и в другом потоке. Разойдись эти два значения — и запрос
ключа тихо превращается в «нет приёмника», а модель, как показал живой
прогон, бросает механизм и уходит искать CLI.

Существующие тесты спеки 19 этого поймать не могут: они подставляют один
и тот же литерал по обе стороны шва (`_session_key_for_source` заменён
на `MagicMock(return_value="session-1")`), поэтому расхождение в них
невыразимо. Здесь ключ НЕ пишется руками ни разу — он выводится
настоящим `build_session_key` из настоящего источника.
"""

import asyncio

import pytest

import tools.secret_capture_gateway as scg
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, build_session_key
from gateway.session_context import get_session_env


@pytest.fixture(autouse=True)
def clean_registry():
    scg._notify_cbs.clear()
    yield
    scg._notify_cbs.clear()


@pytest.fixture
def source():
    """Настоящий источник телеграм-личка. Ключ из него выводит продукт."""
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="424242",
        chat_type="private",
        user_id="424242",
    )


class TestContextReachesTheWorkerThread:
    """Агент исполняется в потоке пула. Если ContextVar туда не доезжает,
    диспетчер увидит пустой ключ сессии и вернёт `no_session` — при
    полностью исправной регистрации."""

    def test_session_key_survives_the_executor_hop(self, source):
        from gateway import run as gateway_run
        from gateway.session_context import clear_session_vars, set_session_vars

        runner = gateway_run.GatewayRunner(GatewayConfig())
        key = build_session_key(source)
        seen = {}

        def inside_worker():
            seen["key"] = get_session_env("HERMES_SESSION_KEY", "")
            seen["platform"] = get_session_env("HERMES_SESSION_PLATFORM", "")

        async def drive():
            tokens = set_session_vars(
                platform=source.platform.value, session_key=key,
                chat_id=source.chat_id, user_id=source.user_id,
            )
            try:
                await runner._run_in_executor_with_context(inside_worker)
            finally:
                clear_session_vars(tokens)

        asyncio.run(drive())

        assert seen["key"] == key, (
            "ключ сессии не доехал в рабочий поток — spec 19 умрёт молча"
        )
        assert seen["platform"] == "telegram"


class TestRegistrationAndLookupAgree:
    """Регистрация и поиск идут через РАЗНЫЕ механизмы. Тест связывает их
    только через настоящий источник, а не через общий литерал."""

    def test_dispatcher_finds_the_notify_registered_for_the_same_source(self, source):
        key = build_session_key(source)
        delivered = []
        scg.register_notify(key, lambda entry: delivered.append(entry))

        from gateway.session_context import clear_session_vars, set_session_vars

        tokens = set_session_vars(
            platform=source.platform.value, session_key=key,
            chat_id=source.chat_id, user_id=source.user_id,
        )
        try:
            import contextvars
            import threading

            result = {}

            def ask():
                result["r"] = scg.gateway_capture_dispatcher("SOME_KEY", "зачем")

            # Поток запускается через copy_context().run — ровно так его
            # запускает шлюз (_run_in_executor_with_context). Голый
            # threading.Thread контекст НЕ наследует, и подмена этого
            # вызова на голый поток должна ронять тест: это и есть
            # проверяемый шов.
            ctx = contextvars.copy_context()
            t = threading.Thread(target=ctx.run, args=(ask,), daemon=True)
            t.start()
            for _ in range(50):
                if delivered:
                    break
                t.join(0.05)
            assert delivered, (
                "диспетчер не нашёл приёмник, зарегистрированный для этого же "
                "источника — шов разошёлся"
            )
            scg.resolve_secret_reply(key, "значение")
            t.join(timeout=5)
        finally:
            clear_session_vars(tokens)
            scg.clear_session(key)

        assert result["r"]["skipped"] is False
        assert "значение" not in str(result["r"])

    def test_a_different_chat_cannot_reach_this_capture(self, source):
        """Изоляция сессий — вторая половина того же шва."""
        key = build_session_key(source)
        scg.register_notify(key, lambda e: None)

        other = SessionSource(
            platform=Platform.TELEGRAM, chat_id="999999",
            chat_type="private", user_id="999999",
        )
        other_key = build_session_key(other)
        assert other_key != key

        from gateway.session_context import clear_session_vars, set_session_vars

        tokens = set_session_vars(
            platform="telegram", session_key=other_key,
            chat_id=other.chat_id, user_id=other.user_id,
        )
        try:
            res = scg.gateway_capture_dispatcher("SOME_KEY", "зачем")
        finally:
            clear_session_vars(tokens)

        assert res["skipped"] is True
        assert res["reason"] == "no_capture_registered"

    def test_no_session_context_at_all_is_reported_as_such(self):
        """Отдельная причина от «нет приёмника» — иначе диагностика врёт."""
        res = scg.gateway_capture_dispatcher("SOME_KEY", "зачем")
        assert res["reason"] == "no_session"
