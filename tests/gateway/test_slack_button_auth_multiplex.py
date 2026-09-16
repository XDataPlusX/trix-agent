"""RAF-176 (Slack-зеркало): кнопки Block Kit в мультиплекс-профиле.

Разбор цепочки в ``test_telegram_button_auth_multiplex`` — здесь тот же дефект
в ``SlackAdapter._is_interactive_user_authorized``: он ищет gateway-runner
через ``_message_handler.__self__``, а при ``gateway.multiplex_profiles``
в адаптер кладётся замыкание (``_make_profile_message_handler``), у которого
``__self__`` нет. Runner не резолвится, полная цепочка ``_is_user_authorized``
молча выпадает, и любое нажатие кнопки судится по узкому env-only fallback —
который под мультиплексом читает секретный скоуп профиля авторитетно.

Баг нашли на Telegram; Slack ломался ровно так же и молча.
"""

from unittest.mock import MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource


OWNER_ID = "U0OWNER"
CHANNEL_ID = "D0OWNERDM"


class _RecordingRunner:
    """Полная авторизация = главный allowlist (см. телеграмный аналог)."""

    def __init__(self):
        self.seen: list[SessionSource] = []
        # Гранты, которых нет ни в одном env-списке — так выглядит запись в
        # pairing-store или ``allow_from`` в config.yaml.
        self.extra_authorized: set[str] = set()

    async def _handle_message(self, event):  # pragma: no cover - не вызывается
        return None

    def _is_user_authorized(self, source: SessionSource) -> bool:
        from gateway.authz_mixin import _auth_env

        self.seen.append(source)
        if source.user_id in self.extra_authorized:
            return True
        allowed = {
            uid.strip()
            for uid in _auth_env("SLACK_ALLOWED_USERS").split(",")
            if uid.strip()
        }
        return bool(source.user_id) and source.user_id in allowed

    def _make_adapter_auth_check(self, platform, profile_name=None):
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
    from plugins.platforms.slack.adapter import SlackAdapter

    adapter = object.__new__(SlackAdapter)
    adapter.config = PlatformConfig(enabled=True, token="xoxb-test", extra={})
    adapter._platform = Platform.SLACK
    return adapter


def _wire_multiplex_profile(adapter, runner, profile_name="system_admin"):
    async def _handler(event):  # замыкание, а не связанный метод
        return await runner._handle_message(event)

    adapter._message_handler = _handler
    adapter._authorization_check = runner._make_adapter_auth_check(
        Platform.SLACK, profile_name=profile_name
    )
    assert getattr(adapter._message_handler, "__self__", None) is None


@pytest.fixture
def _multiplex_profile_scope(monkeypatch):
    """Активный мультиплекс + скоуп профиля с пустым allowlist; главный
    ``.env`` (``os.environ``) при этом заполнен."""
    from agent import secret_scope

    monkeypatch.setenv("SLACK_ALLOWED_USERS", OWNER_ID)
    monkeypatch.delenv("SLACK_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    token = secret_scope.set_secret_scope({"SLACK_ALLOWED_USERS": ""})
    try:
        yield
    finally:
        secret_scope.reset_secret_scope(token)


def test_owner_button_authorized_in_multiplex_profile(_multiplex_profile_scope):
    adapter = _make_adapter()
    runner = _RecordingRunner()
    _wire_multiplex_profile(adapter, runner)

    assert (
        adapter._is_interactive_user_authorized(OWNER_ID, channel_id=CHANNEL_ID)
        is True
    )
    assert runner.seen, "полная цепочка авторизации не была вызвана"
    assert runner.seen[-1].profile == "system_admin"
    assert runner.seen[-1].chat_type == "dm"


def test_stranger_button_still_denied_in_multiplex_profile(_multiplex_profile_scope):
    adapter = _make_adapter()
    runner = _RecordingRunner()
    _wire_multiplex_profile(adapter, runner)

    assert (
        adapter._is_interactive_user_authorized("U0MALLORY", channel_id="C0SHARED")
        is False
    )
    assert runner.seen[-1].chat_type == "group"


def test_env_only_fallback_survives_without_a_registered_check(monkeypatch):
    """Голый адаптер (тесты, ранний старт) обязан дойти до env-fallback."""
    from agent import secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setenv("SLACK_ALLOWED_USERS", OWNER_ID)
    monkeypatch.delenv("SLACK_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)

    adapter = _make_adapter()
    adapter._message_handler = None
    # Ни runner-а, ни зарегистрированной проверки — атрибута нет вовсе,
    # как у адаптера, собранного через object.__new__.
    assert not hasattr(adapter, "_authorization_check")

    assert adapter._is_interactive_user_authorized(OWNER_ID, channel_id=CHANNEL_ID) is True
    assert adapter._is_interactive_user_authorized("U0MALLORY", channel_id=CHANNEL_ID) is False


def test_grant_that_only_the_full_chain_knows_about_is_honored(
    _multiplex_profile_scope,
):
    """Якорь на сам дефект — и на то, чем он отличается от телеграмного.

    Slack-овский env-fallback читает значения через ``get_secret`` с
    проваливанием в ``os.environ``, поэтому владельца он НЕ отклонял: там,
    где Telegram отвечал «нет прав», Slack молча судил кнопку по чужому
    (главному) списку. Настоящая цена одна и та же — полная цепочка
    выпадала целиком, вместе со всем, чего в env-списках нет: pairing-store,
    ``allow_from`` адаптера, групповые правила.

    Здесь это проверяется пользователем, которого авторизует ТОЛЬКО runner.
    """
    paired_user = "U0PAIRED"

    adapter = _make_adapter()
    runner = _RecordingRunner()
    runner.extra_authorized.add(paired_user)
    _wire_multiplex_profile(adapter, runner)

    # С делегатом — грант виден.
    assert (
        adapter._is_interactive_user_authorized(paired_user, channel_id=CHANNEL_ID)
        is True
    )

    # Без него (поведение до правки) — цепочку не спрашивают вовсе, и грант
    # теряется: в env-списках этого пользователя нет.
    runner.seen.clear()
    adapter._authorization_check = None
    assert (
        adapter._is_interactive_user_authorized(paired_user, channel_id=CHANNEL_ID)
        is False
    )
    assert not runner.seen
