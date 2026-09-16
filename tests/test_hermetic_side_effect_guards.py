"""Regression tests for hermetic guards around local desktop side effects."""

from __future__ import annotations

import webbrowser


def test_webbrowser_open_calls_are_neutralized(monkeypatch):
    """OAuth/browser tests should never reach the real browser registry."""

    def _real_browser_lookup_reached(*_args, **_kwargs):
        raise AssertionError("test reached the real webbrowser registry")

    monkeypatch.setattr(webbrowser, "get", _real_browser_lookup_reached)

    url = "https://provider.example.invalid/oauth/authorize"

    assert webbrowser.open(url) is True
    assert webbrowser.open_new(url) is True
    assert webbrowser.open_new_tab(url) is True


def test_webbrowser_get_controller_is_neutralized(_neutralize_webbrowser):
    """Direct controller access should still stay inside the test recorder."""
    url = "https://provider.example.invalid/oauth/authorize"

    controller = webbrowser.get("hermes-test-browser")

    assert controller.open(url) is True
    assert controller.open_new(url) is True
    assert controller.open_new_tab(url) is True
    assert _neutralize_webbrowser == [url, url, url]


def _isolate_anthropic_credentials(monkeypatch):
    """Отрезать ВСЕ источники учётных данных Anthropic, а не один из них.

    Дом профиля уводит фикстура ``isolated_user_home``: файлы Claude Code
    движок ищет через ``hermes_constants.user_home_path()``, и прежний
    ``monkeypatch.setattr(aa.Path, "home", ...)`` здесь перекрывал давно не
    тот путь — сторож хермётичности читал настоящий
    ``~/.claude/.credentials.json`` и потому находил живой токен вместо
    ожидаемого ``None``.
    """
    from agent import anthropic_adapter as aa

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(aa.platform, "system", lambda: "Darwin")

    def _real_keychain_reached(*_args, **_kwargs):
        raise AssertionError("test reached the real macOS Keychain command")

    monkeypatch.setattr(aa.subprocess, "run", _real_keychain_reached)
    return aa


def test_claude_code_credential_read_does_not_touch_macos_keychain(
    monkeypatch, isolated_user_home
):
    """The real credential reader should be safe under the suite guard."""
    aa = _isolate_anthropic_credentials(monkeypatch)

    assert aa.read_claude_code_credentials() is None


def test_anthropic_token_resolution_does_not_touch_macos_keychain(
    monkeypatch, isolated_user_home
):
    """Token resolution should be safe under the same suite guard."""
    aa = _isolate_anthropic_credentials(monkeypatch)

    assert aa.resolve_anthropic_token() is None
