"""RAF-176: кнопки Telegram в мультиплекс-профиле.

Прод-баг владельца (2026-09-16): `/update` показывает две кнопки, верхняя
(«Разрешить один раз») не делает ничего, при этом обычные сообщения от того
же владельца ходят нормально.

Асимметрия объясняется не пустым `TELEGRAM_ALLOWED_USERS=` в профильном
`.env`, а тем, КАК кнопочный обработчик ищет gateway-runner:

    runner = getattr(getattr(self, "_message_handler", None), "__self__", None)

В одно-профильном шлюзе `_message_handler` — связанный метод
`GatewayRunner._handle_message`, у него есть `__self__`, и кнопка проходит
полную цепочку `_is_user_authorized`. При `multiplex_profiles: true` в адаптер
кладётся ЗАМЫКАНИЕ (`GatewayRunner._make_profile_message_handler` /
`_make_default_profile_message_handler`), у замыкания `__self__` нет — runner
не резолвится, полная цепочка молча пропускается, и кнопка сваливается в
узкий env-only fallback `_scoped_gate_env("TELEGRAM_ALLOWED_USERS")`.

Под активным мультиплексом этот fallback читает секретный скоуп профиля
АВТОРИТЕТНО (`_platform_gate_env`): ключ в скоупе есть, значение пустое →
default-deny. Обычное же сообщение идёт через `_is_user_authorized` →
`_auth_env`, а тот при пустом значении в скоупе проваливается в `os.environ`,
где лежит главный `.env` с заполненным списком — поэтому текст проходит, а
кнопка нет.

Тесты ниже — репродукция мока оператора (`sc:once` + `_slash_confirm_state`)
на реальном `_handle_callback_query`, а не монкипатч
`_is_callback_user_authorized`.
"""

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource


# -- Fake telegram modules (те же заглушки, что в соседних кнопочных тестах) --

_fake_telegram_error = types.ModuleType("telegram.error")


class _TelegramError(Exception):
    pass


_fake_telegram_error.TelegramError = _TelegramError
_fake_telegram_error.BadRequest = type("BadRequest", (_TelegramError,), {})
_fake_telegram_error.NetworkError = type("NetworkError", (_TelegramError,), {})

_fake_telegram_constants = types.ModuleType("telegram.constants")
_fake_telegram_constants.ParseMode = SimpleNamespace(HTML="HTML")

_fake_telegram_request = types.ModuleType("telegram.request")
_fake_telegram_request.HTTPXRequest = type(
    "HTTPXRequest", (), {"__init__": lambda *a, **kw: None}
)

_fake_telegram_ext = types.ModuleType("telegram.ext")
_fake_telegram_ext.ApplicationBuilder = type(
    "ApplicationBuilder", (), {"token": lambda self, *a: self, "build": lambda self: None}
)

_fake_telegram = types.ModuleType("telegram")
_fake_telegram.error = _fake_telegram_error
_fake_telegram.constants = _fake_telegram_constants
_fake_telegram.ext = _fake_telegram_ext
_fake_telegram.request = _fake_telegram_request


@pytest.fixture(autouse=True)
def _inject_fake_telegram(monkeypatch):
    monkeypatch.setitem(sys.modules, "telegram", _fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.error", _fake_telegram_error)
    monkeypatch.setitem(sys.modules, "telegram.constants", _fake_telegram_constants)
    monkeypatch.setitem(sys.modules, "telegram.ext", _fake_telegram_ext)
    monkeypatch.setitem(sys.modules, "telegram.request", _fake_telegram_request)


OWNER_ID = "351788701"
OWNER_CHAT_ID = "351788701"
OWNER_THREAD_ID = "570710"


class _RecordingRunner:
    """Минимальный gateway-runner: полная авторизация = главный allowlist.

    Повторяет то, что делает настоящий `_is_user_authorized` для этого прода:
    значение берётся через `_auth_env`, который при ПУСТОМ значении в
    профильном скоупе проваливается в `os.environ` (главный `.env`).
    Запоминает каждый проверенный `SessionSource`, чтобы тест мог убедиться,
    что колбэк пришёл с проставленным профилем, а не в дефолтном контексте.
    """

    def __init__(self):
        self.seen: list[SessionSource] = []

    async def _handle_message(self, event):  # pragma: no cover - не вызывается
        return None

    def _is_user_authorized(self, source: SessionSource) -> bool:
        from gateway.authz_mixin import _auth_env

        self.seen.append(source)
        allowed = {
            uid.strip()
            for uid in _auth_env("TELEGRAM_ALLOWED_USERS").split(",")
            if uid.strip()
        }
        return bool(source.user_id) and source.user_id in allowed

    def _make_adapter_auth_check(self, platform, profile_name=None):
        """Копия формы `GatewayRunner._make_adapter_auth_check`."""

        def check(user_id, chat_type=None, chat_id=None):
            if not user_id:
                return False
            return self._is_user_authorized(
                SessionSource(
                    platform=platform,
                    chat_id=chat_id or "",
                    chat_type=chat_type or "group",
                    user_id=user_id,
                    profile=profile_name,
                )
            )

        return check


def _make_adapter():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _wire_multiplex_profile(adapter, runner, profile_name="system_admin"):
    """Подключить адаптер так, как это делает `_configure_profile_adapter`.

    Ключевая деталь прода: `_message_handler` — ЗАМЫКАНИЕ, а не связанный
    метод, поэтому `__self__` у него отсутствует.
    """

    async def _handler(event):
        if getattr(event, "source", None) is not None and not event.source.profile:
            event.source.profile = profile_name
        return await runner._handle_message(event)

    adapter.set_message_handler(_handler)
    adapter.set_authorization_check(
        runner._make_adapter_auth_check(Platform.TELEGRAM, profile_name=profile_name)
    )
    assert getattr(adapter._message_handler, "__self__", None) is None


def _make_slash_confirm_query(data="sc:once:cid-1"):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = OWNER_CHAT_ID
    query.message.message_thread_id = OWNER_THREAD_ID
    query.message.chat = MagicMock()
    query.message.chat.type = "private"
    query.from_user = MagicMock()
    query.from_user.id = OWNER_ID
    query.from_user.first_name = "Rafail"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


@pytest.fixture
def _multiplex_profile_scope(monkeypatch):
    """Активный мультиплекс + скоуп профиля с ПУСТЫМ allowlist.

    Главный `.env` (то есть `os.environ`) при этом заполнен — ровно так, как
    было на проде до ручной правки оператора.
    """
    from agent import secret_scope

    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", OWNER_ID)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    token = secret_scope.set_secret_scope({"TELEGRAM_ALLOWED_USERS": ""})
    try:
        yield
    finally:
        secret_scope.reset_secret_scope(token)


class TestButtonAuthUnderMultiplex:
    @pytest.mark.asyncio
    async def test_owner_button_resolves_in_multiplex_profile(
        self, _multiplex_profile_scope
    ):
        """Репродукция прод-бага: кнопка владельца должна резолвиться.

        До правки runner не резолвился (замыкание без `__self__`), кнопка
        уходила в env-only fallback, тот читал пустое значение из скоупа
        профиля и отвечал «нет прав». Обычные сообщения того же владельца при
        этом проходили — асимметрия, которую и видел владелец.
        """
        adapter = _make_adapter()
        runner = _RecordingRunner()
        _wire_multiplex_profile(adapter, runner)
        adapter._slash_confirm_state["cid-1"] = "session-1"

        update, query = _make_slash_confirm_query()
        await adapter._handle_callback_query(update, MagicMock())

        # Состояние подтверждения снято → кнопка отработала, а не отказала.
        assert "cid-1" not in adapter._slash_confirm_state
        assert query.answer.await_count == 1

        # Авторизация прошла через полную цепочку runner-а, и колбэк пришёл
        # с профилем владеющего адаптера, а не в дефолтном контексте.
        assert runner.seen, "полная цепочка авторизации не была вызвана"
        assert runner.seen[-1].user_id == OWNER_ID
        assert runner.seen[-1].profile == "system_admin"

    @pytest.mark.asyncio
    async def test_unauthorized_tap_is_denied_with_visible_alert(
        self, _multiplex_profile_scope
    ):
        """Чужой тап всё так же отклоняется — и отказ ВИДЕН пользователю.

        `query.answer(text=...)` без `show_alert` Telegram показывает коротким
        тостом поверх чата: его легко не заметить, и отказ читается как
        «кнопка не работает». Отказ по правам обязан быть модальным.
        """
        adapter = _make_adapter()
        runner = _RecordingRunner()
        _wire_multiplex_profile(adapter, runner)
        adapter._slash_confirm_state["cid-1"] = "session-1"

        update, query = _make_slash_confirm_query()
        query.from_user.id = "999999"
        query.from_user.first_name = "Mallory"
        await adapter._handle_callback_query(update, MagicMock())

        # Подтверждение не тронуто.
        assert adapter._slash_confirm_state.get("cid-1") == "session-1"
        assert query.answer.await_count == 1
        assert query.answer.await_args.kwargs.get("show_alert") is True
        assert query.answer.await_args.kwargs.get("text")

    @pytest.mark.asyncio
    async def test_exec_approval_button_resolves_in_multiplex_profile(
        self, _multiplex_profile_scope, monkeypatch
    ):
        """Тот же разрыв цепочки бил и по кнопкам согласования команд (`ea:`).

        Это и есть кнопка «Разрешить один раз» под `/update`: без правки она
        не доходила до `resolve_gateway_approval`.
        """
        import tools.approval as approval_mod

        resolved: list = []

        def _resolve(session_key, choice):
            resolved.append((session_key, choice))
            return 1

        monkeypatch.setattr(approval_mod, "resolve_gateway_approval", _resolve)

        adapter = _make_adapter()
        runner = _RecordingRunner()
        _wire_multiplex_profile(adapter, runner)
        adapter._approval_state[7] = "session-1"
        adapter.resume_typing_for_chat = MagicMock()

        update, query = _make_slash_confirm_query("ea:once:7")
        await adapter._handle_callback_query(update, MagicMock())

        assert resolved == [("session-1", "once")]
        assert 7 not in adapter._approval_state

    @pytest.mark.asyncio
    async def test_single_profile_runner_path_still_wins(self, monkeypatch):
        """Одно-профильный шлюз не меняется: `__self__` есть, идём через него.

        Регрессионный якорь для правки — новый fallback не должен перехватывать
        путь, который и так работал (и который монкипатчит половина кнопочных
        тестов, подменяя `_message_handler`).
        """
        from agent import secret_scope

        monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", OWNER_ID)

        adapter = _make_adapter()
        runner = _RecordingRunner()
        adapter.set_message_handler(runner._handle_message)
        # Зарегистрированной проверки НЕТ — единственный путь это runner.
        assert getattr(adapter._message_handler, "__self__", None) is runner

        adapter._slash_confirm_state["cid-1"] = "session-1"
        update, query = _make_slash_confirm_query()
        await adapter._handle_callback_query(update, MagicMock())

        assert "cid-1" not in adapter._slash_confirm_state
        assert runner.seen and runner.seen[-1].profile is None
