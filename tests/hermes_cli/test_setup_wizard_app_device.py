"""POST /api/device/start + GET /api/device/status (owner requirement 2):
the wizard's own web device-code login. ``logged_in`` / ``app_env`` fixtures
live in ``tests/hermes_cli/conftest.py``.

The manager itself (``DeviceLoginManager``) is unit-tested in
``test_setup_wizard_device_login.py`` — these tests only cover the HTTP
surface: session gate, provider allow-list, and that the route delegates to
``request.app.state.device_login`` rather than re-implementing anything.
"""
from __future__ import annotations


def test_device_start_requires_session(app_env):
    client, _ = app_env
    r = client.post("/api/device/start", json={"provider": "openai-codex"})
    assert r.status_code == 401


def test_device_status_requires_session(app_env):
    client, _ = app_env
    r = client.get("/api/device/status")
    assert r.status_code == 401


def test_device_start_rejects_unknown_provider(logged_in):
    r = logged_in.post("/api/device/start", json={"provider": "anthropic"})
    assert r.status_code == 400


def test_device_start_rejects_non_device_code_api_key_provider(logged_in):
    """The whole point of the gate: an api_key provider must never reach
    DeviceLoginManager.start() (which would raise ValueError anyway, but
    the route's own allow-list must reject it BEFORE that)."""
    r = logged_in.post("/api/device/start", json={"provider": "openrouter"})
    assert r.status_code == 400


def test_device_start_delegates_to_manager_and_returns_url_and_code(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    calls = []

    class _FakeManager:
        def start(self, provider, proxy=None):
            calls.append(provider)
            return {"login_id": "abc123", "verification_url": "https://auth.openai.com/codex/device", "user_code": "ABCD-1234"}

    # app.state.device_login is per-app-instance — patch the instance the
    # `logged_in` fixture's TestClient is actually wired to, not a
    # module-level default.
    logged_in.app.state.device_login = _FakeManager()

    r = logged_in.post("/api/device/start", json={"provider": "openai-codex"})

    assert r.status_code == 200
    body = r.json()
    assert body["user_code"] == "ABCD-1234"
    assert body["verification_url"] == "https://auth.openai.com/codex/device"
    assert body["login_id"] == "abc123"
    assert calls == ["openai-codex"]


def test_device_start_forwards_the_form_proxy(logged_in):
    """Owner requirement (RU hosting): even reaching a device-code
    provider's own OAuth endpoint (auth.openai.com, MiniMax's portal) can
    need the form's proxy field from a data-center machine — it must
    reach DeviceLoginManager.start alongside the provider name."""
    calls = []

    class _FakeManager:
        def start(self, provider, proxy=None):
            calls.append((provider, proxy))
            return {"login_id": "abc123", "verification_url": "https://x/y", "user_code": "CODE-1"}

    logged_in.app.state.device_login = _FakeManager()

    r = logged_in.post(
        "/api/device/start", json={"provider": "openai-codex", "proxy": "socks5://u:p@h:1080"}
    )
    assert r.status_code == 200
    assert calls == [("openai-codex", "socks5://u:p@h:1080")]


def test_device_start_passes_none_for_empty_proxy(logged_in):
    calls = []

    class _FakeManager:
        def start(self, provider, proxy=None):
            calls.append((provider, proxy))
            return {"login_id": "abc123", "verification_url": "https://x/y", "user_code": "CODE-1"}

    logged_in.app.state.device_login = _FakeManager()

    logged_in.post("/api/device/start", json={"provider": "openai-codex"})
    assert calls == [("openai-codex", None)]


def test_device_start_failure_returns_502_with_russian_message(logged_in):
    class _FailingManager:
        def start(self, provider, proxy=None):
            raise RuntimeError("boom")

        def status(self):
            # The real DeviceLoginManager.start() stashes a specific
            # Russian message in status() before re-raising — the route
            # reads it back rather than always emitting a flat generic
            # string (review finding). error=None here on purpose: this
            # test covers the fallback-to-generic-message path.
            return {"state": "error", "error": None, "provider": "openai-codex", "login_id": None}

    logged_in.app.state.device_login = _FailingManager()

    r = logged_in.post("/api/device/start", json={"provider": "openai-codex"})

    assert r.status_code == 502
    assert "войти" in r.json()["error"].lower() or "вход" in r.json()["error"].lower()


def test_device_start_failure_surfaces_the_specific_status_message(logged_in):
    """When status() carries a specific message (e.g. the rate-limit
    text), the route must surface THAT — not the flat generic fallback."""

    class _FailingManagerWithSpecificMessage:
        def start(self, provider, proxy=None):
            raise RuntimeError("boom")

        def status(self):
            return {
                "state": "error",
                "error": "Провайдер ограничивает частоту входов — попробуйте через минуту.",
                "provider": "openai-codex",
                "login_id": None,
            }

    logged_in.app.state.device_login = _FailingManagerWithSpecificMessage()

    r = logged_in.post("/api/device/start", json={"provider": "openai-codex"})

    assert r.status_code == 502
    assert r.json()["error"] == "Провайдер ограничивает частоту входов — попробуйте через минуту."


def test_device_status_delegates_to_manager(logged_in):
    class _FakeManager:
        def status(self):
            return {"state": "ok", "error": None}

    logged_in.app.state.device_login = _FakeManager()

    r = logged_in.get("/api/device/status")

    assert r.status_code == 200
    assert r.json() == {"state": "ok", "error": None}


def test_device_status_defaults_to_idle_with_no_prior_start(logged_in):
    r = logged_in.get("/api/device/status")
    assert r.status_code == 200
    assert r.json()["state"] == "idle"


def test_device_endpoints_never_log_user_code_or_tokens(logged_in, caplog):
    class _FakeManager:
        def start(self, provider, proxy=None):
            return {
                "login_id": "abc123",
                "verification_url": "https://auth.openai.com/codex/device",
                "user_code": "SECRET-CODE-4242",
            }

        def status(self):
            return {"state": "ok", "error": None}

    logged_in.app.state.device_login = _FakeManager()

    with caplog.at_level("DEBUG"):
        logged_in.post("/api/device/start", json={"provider": "openai-codex"})
        logged_in.get("/api/device/status")

    assert "SECRET-CODE-4242" not in caplog.text
