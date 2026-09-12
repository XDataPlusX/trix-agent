"""Web device-code login (owner requirement 2): the wizard's own
DeviceLoginManager + the request/poll/exchange helpers it drives.

No real network, no real sleeps — every ``httpx.Client`` in
``hermes_cli.setup_wizard.device_login`` is patched to a fake that returns
queued canned responses, and ``time.sleep`` inside the module is a no-op so
the codex poll loop runs to completion instantly regardless of how many
iterations it takes.
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        # hermes_cli.auth._minimax_poll_token reads `.text` (truthiness
        # gate) before calling `.json()` — a real httpx.Response always has
        # both in sync, so the fake must too, or a genuinely-populated
        # payload silently reads back as {} (a bug this default closes).
        self.text = text if text is not None else json.dumps(self._payload)
        self.reason_phrase = ""
        self.encoding = "utf-8"

    def json(self):
        return self._payload

    def read(self):
        return None

    def close(self):
        return None

    def iter_bytes(self):
        # hermes_cli.auth._minimax_response_error_text (called on a
        # non-200 MiniMax response) reads the body via this instead of
        # `.text` when the stream hasn't already been consumed.
        yield self.text.encode("utf-8")


class _FakeHTTPClient:
    """Queue-of-responses stub for ``httpx.Client(...)`` used as a context
    manager AND as a plain object (MiniMax now keeps ONE client alive
    across the start()/finish() thread boundary — see
    ``_minimax_start_device_code``'s docstring — so this fake must work
    both ``with httpx.Client() as client:`` AND a bare ``httpx.Client()``
    that gets ``.close()``d explicitly later). Supports BOTH calling
    conventions this module exercises: ``.post()`` directly (the codex
    flow, duplicated in this module) and ``.build_request()`` +
    ``.send(..., stream=True)`` (MiniMax's ``_minimax_post_form`` helper in
    ``hermes_cli.auth``, reused verbatim). Either way, each call pops the
    next queued response; running out raises ``AssertionError`` (a test
    bug, not a real failure mode) rather than silently hanging."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _next(self):
        assert self._responses, "fake HTTP client ran out of queued responses"
        return self._responses.pop(0)

    def post(self, *args, **kwargs):
        return self._next()

    def build_request(self, method, url, *, data=None, headers=None):
        return object()

    def send(self, request, stream=False):
        return self._next()

    def close(self):
        self.closed = True


def _patch_httpx_sequence(monkeypatch, responses):
    """Every ``httpx.Client(...)`` call in the module returns the SAME
    queue, popped across however many separate ``with httpx.Client() as
    client:`` blocks the flow opens (codex opens three; minimax now opens
    exactly ONE, shared across start()/finish() — see
    ``_FakeHTTPClient``'s own docstring). Returns the shared fake so a
    test can assert on it afterward (e.g. ``.closed``)."""
    shared = _FakeHTTPClient(responses)
    monkeypatch.setattr(
        "hermes_cli.setup_wizard.device_login.httpx.Client", lambda *a, **k: shared
    )
    return shared


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The codex poll loop calls time.sleep(poll_interval) every iteration
    — make it instant so tests never actually wait, regardless of how many
    "still pending" responses are queued."""
    monkeypatch.setattr("hermes_cli.setup_wizard.device_login.time.sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


class _SyncThread:
    """Fake ``threading.Thread`` whose ``.start()`` runs the target
    synchronously, right there — turns DeviceLoginManager.start() into a
    fully deterministic, single-threaded call for the tests that don't
    care about the cancel-on-restart race specifically."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _CapturingThread:
    """Fake ``threading.Thread`` that records the call instead of running
    it — lets a test invoke the background step manually, at a chosen
    moment, to exercise the generation-guard directly."""

    instances: list = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        _CapturingThread.instances.append(self)

    def start(self):
        pass  # deliberately does not run — the test calls run() itself

    def run(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture(autouse=True)
def _reset_capturing_thread():
    _CapturingThread.instances = []
    yield
    _CapturingThread.instances = []


# ---------------------------------------------------------------------------
# Low-level HTTP shape: _codex_request_device_code / _codex_poll_and_exchange
# ---------------------------------------------------------------------------


def test_codex_request_device_code_happy_path(monkeypatch):
    from hermes_cli.setup_wizard.device_login import _codex_request_device_code

    _patch_httpx_sequence(
        monkeypatch,
        [_FakeResponse(200, {"user_code": "ABCD-1234", "device_auth_id": "da-1", "interval": "5"})],
    )
    info = _codex_request_device_code()
    assert info["user_code"] == "ABCD-1234"
    assert info["device_auth_id"] == "da-1"
    assert info["poll_interval"] == 5
    assert info["verification_url"] == "https://auth.openai.com/codex/device"


def test_codex_request_device_code_passes_proxy_to_httpx_client(monkeypatch):
    """Owner requirement: auth.openai.com itself can be unreachable from a
    RU data-center host (the region-block diagnosis this module already
    handles) — the wizard's form proxy must reach the actual httpx.Client
    this step opens."""
    from hermes_cli.setup_wizard.device_login import _codex_request_device_code

    captured_kwargs = []

    def _client_factory(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return _FakeHTTPClient(
            [_FakeResponse(200, {"user_code": "ABCD-1234", "device_auth_id": "da-1", "interval": "5"})]
        )

    monkeypatch.setattr("hermes_cli.setup_wizard.device_login.httpx.Client", _client_factory)

    _codex_request_device_code(proxy="socks5://u:p@h:1080")
    assert captured_kwargs[0].get("proxy") == "socks5://u:p@h:1080"


def test_codex_request_device_code_omits_proxy_kwarg_when_not_given(monkeypatch):
    """No proxy configured -> no `proxy` kwarg at all (not `proxy=None`) —
    same convention as validate.check_telegram_token/probe_provider_key."""
    from hermes_cli.setup_wizard.device_login import _codex_request_device_code

    captured_kwargs = []

    def _client_factory(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return _FakeHTTPClient(
            [_FakeResponse(200, {"user_code": "ABCD-1234", "device_auth_id": "da-1", "interval": "5"})]
        )

    monkeypatch.setattr("hermes_cli.setup_wizard.device_login.httpx.Client", _client_factory)

    _codex_request_device_code()
    assert "proxy" not in captured_kwargs[0]


def test_codex_request_device_code_429_raises_rate_limited(monkeypatch):
    """Review finding: the web version must NOT burn ~3 minutes retrying
    like the CLI does — at most 2 attempts, so exactly 2 responses get
    consumed off the queue (a 3rd queued 429 below would go unused; the
    `_FakeHTTPClient` never asserts an empty queue at teardown, only that
    it doesn't run OUT, so this is a real assertion on call count, not
    just a happy accident)."""
    from hermes_cli.auth import CODEX_RATE_LIMITED_CODE, AuthError
    from hermes_cli.setup_wizard.device_login import _codex_request_device_code

    shared = _patch_httpx_sequence(
        monkeypatch, [_FakeResponse(429, {}, headers={}) for _ in range(2)]
    )
    with pytest.raises(AuthError) as exc_info:
        _codex_request_device_code()
    assert exc_info.value.code == CODEX_RATE_LIMITED_CODE
    assert shared._responses == []  # exactly 2 consumed, none left over


def test_codex_request_device_code_429_retry_delay_capped_at_five_seconds(monkeypatch):
    from hermes_cli.setup_wizard.device_login import _codex_request_device_code

    _patch_httpx_sequence(
        monkeypatch,
        [
            _FakeResponse(429, {}, headers={"retry-after": "9999"}),
            _FakeResponse(200, {"user_code": "ABCD-1234", "device_auth_id": "da-1", "interval": "5"}),
        ],
    )
    sleeps = []
    monkeypatch.setattr(
        "hermes_cli.setup_wizard.device_login.time.sleep", lambda s: sleeps.append(s)
    )
    info = _codex_request_device_code()
    assert info["user_code"] == "ABCD-1234"
    assert sleeps == [5]  # a huge Retry-After must still be clamped to 5s


def test_codex_request_device_code_403_raises_region_blocked(monkeypatch):
    """Owner diagnosis: auth.openai.com answers with HTTP 403 from a
    data-center/VM IP (a WAF region block) — a plain 403 on the usercode
    request must get its own AuthError code, distinct from every other
    non-200/429 status, so _russian_error_for can give an honest message
    instead of the generic "не удалось войти"."""
    from hermes_cli.auth import AuthError
    from hermes_cli.setup_wizard.device_login import (
        _DEVICE_CODE_REGION_BLOCKED_CODE,
        _codex_request_device_code,
    )

    _patch_httpx_sequence(monkeypatch, [_FakeResponse(403, {})])
    with pytest.raises(AuthError) as exc_info:
        _codex_request_device_code()
    assert exc_info.value.code == _DEVICE_CODE_REGION_BLOCKED_CODE


def test_codex_region_blocked_maps_to_the_specific_russian_message():
    from hermes_cli.auth import AuthError
    from hermes_cli.setup_wizard.device_login import (
        _DEVICE_CODE_REGION_BLOCKED_CODE,
        _russian_error_for,
    )

    exc = AuthError("forbidden", provider="openai-codex", code=_DEVICE_CODE_REGION_BLOCKED_CODE)
    message = _russian_error_for(exc)
    assert "блокирует регион" in message


def test_manager_start_403_surfaces_region_blocked_status(monkeypatch):
    """End-to-end through DeviceLoginManager.start(): a mocked 403 on the
    usercode request must land in status()["error"] with the honest
    region-block text, not the generic "Не удалось войти" copy."""
    import hermes_cli.setup_wizard.device_login as dl
    from hermes_cli.auth import AuthError

    shared = _patch_httpx_sequence(monkeypatch, [_FakeResponse(403, {})])

    manager = dl.DeviceLoginManager()
    with pytest.raises(AuthError):
        manager.start("openai-codex")
    status = manager.status()
    assert status["state"] == "error"
    assert "блокирует регион" in status["error"]
    assert shared._responses == []


def test_codex_rate_limited_maps_to_the_specific_russian_message():
    from hermes_cli.auth import AuthError, CODEX_RATE_LIMITED_CODE
    from hermes_cli.setup_wizard.device_login import _russian_error_for

    exc = AuthError("rate limited", provider="openai-codex", code=CODEX_RATE_LIMITED_CODE)
    message = _russian_error_for(exc)
    assert message == "Провайдер ограничивает частоту входов — попробуйте через минуту."


def test_codex_poll_and_exchange_pending_then_success(monkeypatch):
    from hermes_cli.setup_wizard.device_login import _codex_poll_and_exchange

    device_info = {"user_code": "ABCD-1234", "device_auth_id": "da-1", "poll_interval": 1}
    _patch_httpx_sequence(
        monkeypatch,
        [
            _FakeResponse(404),  # poll: not approved yet
            _FakeResponse(404),  # poll: still pending
            _FakeResponse(200, {"authorization_code": "ac-1", "code_verifier": "cv-1"}),
            _FakeResponse(200, {"access_token": "AT-1", "refresh_token": "RT-1"}),
        ],
    )
    creds = _codex_poll_and_exchange(device_info, timeout_seconds=60)
    assert creds["tokens"] == {"access_token": "AT-1", "refresh_token": "RT-1"}
    assert creds["base_url"]
    assert creds["last_refresh"]


def test_codex_poll_and_exchange_passes_proxy_to_both_http_clients(monkeypatch):
    """This function opens TWO separate httpx.Client blocks (the poll loop,
    then the token exchange) — both must carry the proxy, not just the
    first one."""
    from hermes_cli.setup_wizard.device_login import _codex_poll_and_exchange

    device_info = {"user_code": "ABCD-1234", "device_auth_id": "da-1", "poll_interval": 1}
    # ONE shared fake client instance across both httpx.Client(...) calls —
    # same reason _patch_httpx_sequence shares one, see its own docstring:
    # a fresh _FakeHTTPClient per call would re-copy the full response
    # queue instead of popping from where the previous call left off.
    shared = _FakeHTTPClient(
        [
            _FakeResponse(200, {"authorization_code": "ac-1", "code_verifier": "cv-1"}),
            _FakeResponse(200, {"access_token": "AT-1", "refresh_token": "RT-1"}),
        ]
    )
    captured_kwargs = []

    def _client_factory(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return shared

    monkeypatch.setattr("hermes_cli.setup_wizard.device_login.httpx.Client", _client_factory)

    creds = _codex_poll_and_exchange(device_info, timeout_seconds=60, proxy="http://h:8080")
    assert creds["tokens"] == {"access_token": "AT-1", "refresh_token": "RT-1"}
    assert len(captured_kwargs) == 2  # poll client + exchange client
    assert all(kw.get("proxy") == "http://h:8080" for kw in captured_kwargs)


def test_codex_poll_and_exchange_times_out_without_real_delay(monkeypatch):
    """timeout_seconds=0 makes the `while` loop's very first condition
    false — the deadline is already in the past — so this proves the
    timeout path raises without ever touching the (mocked) sleep."""
    from hermes_cli.auth import AuthError
    from hermes_cli.setup_wizard.device_login import _codex_poll_and_exchange

    device_info = {"user_code": "ABCD-1234", "device_auth_id": "da-1", "poll_interval": 1}
    _patch_httpx_sequence(monkeypatch, [])  # never reached
    with pytest.raises(AuthError) as exc_info:
        _codex_poll_and_exchange(device_info, timeout_seconds=0)
    assert exc_info.value.code == "device_code_timeout"


def test_codex_poll_and_exchange_rejects_unexpected_poll_status(monkeypatch):
    from hermes_cli.auth import AuthError
    from hermes_cli.setup_wizard.device_login import _codex_poll_and_exchange

    device_info = {"user_code": "ABCD-1234", "device_auth_id": "da-1", "poll_interval": 1}
    _patch_httpx_sequence(monkeypatch, [_FakeResponse(500)])
    with pytest.raises(AuthError) as exc_info:
        _codex_poll_and_exchange(device_info, timeout_seconds=60)
    assert exc_info.value.code == "device_code_poll_error"


def test_codex_poll_and_exchange_cancels_immediately_when_superseded(monkeypatch):
    """Review finding (real cancellation, not just a discarded result):
    Codex owns its own poll loop, so a superseded login must stop polling
    almost immediately — this proves it never even reaches the second
    queued "still pending" response once is_current() flips False, let
    alone the exchange step."""
    from hermes_cli.setup_wizard.device_login import _LoginSuperseded, _codex_poll_and_exchange

    device_info = {"user_code": "ABCD-1234", "device_auth_id": "da-1", "poll_interval": 1}
    # A whole queue of "still pending" responses that must NEVER be reached.
    _patch_httpx_sequence(monkeypatch, [_FakeResponse(404) for _ in range(50)])

    calls = {"n": 0}

    def is_current():
        calls["n"] += 1
        return calls["n"] <= 1  # current for the very first check only

    with pytest.raises(_LoginSuperseded):
        _codex_poll_and_exchange(device_info, timeout_seconds=600, is_current=is_current)


def test_codex_poll_and_exchange_superseded_after_approval_before_exchange(monkeypatch):
    """is_current() is also checked right before the token exchange —
    approval landing a moment after the client already moved on must not
    still complete the exchange (and, one layer up, must not persist).
    Queues NO token-exchange response at all — reaching that call would
    error on an empty queue, so this also proves the exchange step is
    never attempted."""
    from hermes_cli.setup_wizard.device_login import _LoginSuperseded, _codex_poll_and_exchange

    device_info = {"user_code": "ABCD-1234", "device_auth_id": "da-1", "poll_interval": 1}
    _patch_httpx_sequence(
        monkeypatch,
        [_FakeResponse(200, {"authorization_code": "ac-1", "code_verifier": "cv-1"})],
    )
    calls = {"n": 0}

    def is_current():
        calls["n"] += 1
        # True for the loop's own two checks (before/after sleep, first
        # iteration) so it actually reaches and consumes the approval
        # response; False from the third call on — the check made right
        # after the loop, before the exchange step.
        return calls["n"] <= 2

    with pytest.raises(_LoginSuperseded):
        _codex_poll_and_exchange(device_info, timeout_seconds=60, is_current=is_current)


# ---------------------------------------------------------------------------
# MiniMax: thin wrapper over hermes_cli.auth's client-parametrized helpers
# ---------------------------------------------------------------------------


def test_minimax_start_device_code_happy_path(monkeypatch):
    from hermes_cli.setup_wizard.device_login import _minimax_start_device_code

    # _minimax_request_user_code (hermes_cli.auth) validates the response's
    # "state" field against the one it generated and sent — pin
    # _minimax_pkce_pair()'s output first so the queued response below can
    # echo a state that will actually pass that check.
    monkeypatch.setattr(
        "hermes_cli.setup_wizard.device_login._minimax_pkce_pair",
        lambda: ("verifier-1", "challenge-1", "state-1"),
    )
    shared_client = _patch_httpx_sequence(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "user_code": "MX-CODE",
                    "verification_uri": "https://minimax.example/verify",
                    "expired_in": 900,
                    "interval": 2000,
                    "state": "state-1",
                },
            ),
        ],
    )

    info = _minimax_start_device_code()
    assert info["user_code"] == "MX-CODE"
    assert info["verification_url"] == "https://minimax.example/verify"
    assert info["code_verifier"] == "verifier-1"
    # Review finding: request + poll must share ONE client (matching the
    # CLI's single `with httpx.Client()` block) — start() must NOT close
    # it, ownership passes to _minimax_finish_device_code via info["client"].
    assert info["client"] is shared_client
    assert shared_client.closed is False


def test_minimax_start_device_code_passes_proxy_to_httpx_client(monkeypatch):
    """MiniMax's portal is reached through the SAME shared client
    (start()/finish() straddle a thread boundary — see the module
    docstring) — the proxy must land on that one client's kwargs."""
    from hermes_cli.setup_wizard.device_login import _minimax_start_device_code

    monkeypatch.setattr(
        "hermes_cli.setup_wizard.device_login._minimax_pkce_pair",
        lambda: ("verifier-1", "challenge-1", "state-1"),
    )
    captured_kwargs = []

    def _client_factory(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return _FakeHTTPClient(
            [
                _FakeResponse(
                    200,
                    {
                        "user_code": "MX-CODE",
                        "verification_uri": "https://minimax.example/verify",
                        "expired_in": 900,
                        "interval": 2000,
                        "state": "state-1",
                    },
                ),
            ]
        )

    monkeypatch.setattr("hermes_cli.setup_wizard.device_login.httpx.Client", _client_factory)

    info = _minimax_start_device_code(proxy="socks5://u:p@h:1080")
    assert info["user_code"] == "MX-CODE"
    assert captured_kwargs[0].get("proxy") == "socks5://u:p@h:1080"


def test_minimax_finish_device_code_success(monkeypatch):
    """_minimax_finish_device_code() itself no longer persists anything
    (review finding: persistence moved to DeviceLoginManager._finish, under
    the generation lock) — it only polls and returns the shaped auth_state
    for the caller to save."""
    from hermes_cli.setup_wizard.device_login import _minimax_finish_device_code

    shared_client = _FakeHTTPClient(
        [
            _FakeResponse(
                200,
                {
                    "status": "success",
                    "access_token": "MX-AT",
                    "refresh_token": "MX-RT",
                    "expired_in": 3600,
                },
            ),
        ]
    )
    info = {
        "client": shared_client,
        "portal_base_url": "https://minimax.example",
        "client_id": "client-1",
        "user_code": "MX-CODE",
        "code_verifier": "verifier-1",
        "expired_in": 900,
        "interval_ms": 1,
        "inference_base_url": "https://minimax.example/v1",
    }
    result = _minimax_finish_device_code(info)
    assert result["auth_state"]["access_token"] == "MX-AT"
    assert result["auth_state"]["refresh_token"] == "MX-RT"
    assert result["auth_state"]["provider"] == "minimax-oauth"
    # The shared client must be closed once polling is done, regardless of
    # outcome (see the `finally: client.close()` in the implementation).
    assert shared_client.closed is True

    from hermes_cli.auth import get_provider_auth_state

    assert get_provider_auth_state("minimax-oauth") is None


def test_minimax_finish_device_code_closes_client_even_on_failure(monkeypatch):
    from hermes_cli.auth import AuthError
    from hermes_cli.setup_wizard.device_login import _minimax_finish_device_code

    shared_client = _FakeHTTPClient([_FakeResponse(500)])
    info = {
        "client": shared_client,
        "portal_base_url": "https://minimax.example",
        "client_id": "client-1",
        "user_code": "MX-CODE",
        "code_verifier": "verifier-1",
        "expired_in": 900,
        "interval_ms": 1,
        "inference_base_url": "https://minimax.example/v1",
    }
    with pytest.raises(AuthError):
        _minimax_finish_device_code(info)
    assert shared_client.closed is True


def test_minimax_finish_device_code_superseded_before_polling(monkeypatch):
    """is_current() is checked BEFORE the poll call too — a login
    superseded between start() and its background thread actually running
    must never touch the network at all."""
    from hermes_cli.setup_wizard.device_login import (
        _LoginSuperseded,
        _minimax_finish_device_code,
    )

    shared_client = _FakeHTTPClient([])  # any HTTP call here is a test bug
    info = {
        "client": shared_client,
        "portal_base_url": "https://minimax.example",
        "client_id": "client-1",
        "user_code": "MX-CODE",
        "code_verifier": "verifier-1",
        "expired_in": 900,
        "interval_ms": 1,
        "inference_base_url": "https://minimax.example/v1",
    }
    with pytest.raises(_LoginSuperseded):
        _minimax_finish_device_code(info, is_current=lambda: False)
    assert shared_client.closed is True


def test_minimax_capped_expired_in_ms_clamps_a_far_future_deadline():
    """Review finding: MiniMax's poll deadline must be capped the same
    way Codex's is — the server's own expired_in must not be trusted to
    stay within a sane bound."""
    import time as _time

    from hermes_cli.setup_wizard.device_login import _minimax_capped_expired_in_ms

    now_ms = int(_time.time() * 1000)
    far_future_ms = now_ms + 60 * 60 * 1000  # 1 hour out — beyond the cap
    capped = _minimax_capped_expired_in_ms(far_future_ms, timeout_seconds=900)
    assert capped <= now_ms + 901_000
    assert capped > now_ms  # still a real, future deadline


def test_minimax_capped_expired_in_ms_leaves_a_near_deadline_alone():
    import time as _time

    from hermes_cli.setup_wizard.device_login import _minimax_capped_expired_in_ms

    now_ms = int(_time.time() * 1000)
    near_future_ms = now_ms + 60 * 1000  # 1 minute out — well within the cap
    capped = _minimax_capped_expired_in_ms(near_future_ms, timeout_seconds=900)
    assert abs(capped - near_future_ms) < 2000  # essentially unchanged


# ---------------------------------------------------------------------------
# DeviceLoginManager — start/status/generation, with the provider-specific
# HTTP steps mocked out (already covered above) so these tests focus purely
# on the manager's own state machine and persistence wiring.
# ---------------------------------------------------------------------------


def test_manager_start_threads_proxy_into_request_and_finish_steps(monkeypatch):
    """End-to-end through DeviceLoginManager.start(): the proxy passed to
    start() must reach BOTH the synchronous step-1 request AND the
    background poll/exchange step — not just one of the two."""
    import hermes_cli.setup_wizard.device_login as dl

    monkeypatch.setattr(dl.threading, "Thread", _SyncThread)
    captured = {}

    def _request(proxy=None):
        captured["request_proxy"] = proxy
        return {
            "user_code": "CODE-1",
            "device_auth_id": "da",
            "poll_interval": 1,
            "verification_url": "https://x/y",
        }

    def _poll(info, *, timeout_seconds, is_current=lambda: True, proxy=None):
        captured["poll_proxy"] = proxy
        return {
            "tokens": {"access_token": "AT", "refresh_token": "RT"},
            "base_url": "https://api.example",
            "last_refresh": "now",
        }

    monkeypatch.setattr(dl, "_codex_request_device_code", _request)
    monkeypatch.setattr(dl, "_codex_poll_and_exchange", _poll)

    manager = dl.DeviceLoginManager()
    manager.start("openai-codex", proxy="socks5://u:p@h:1080")

    assert captured["request_proxy"] == "socks5://u:p@h:1080"
    assert captured["poll_proxy"] == "socks5://u:p@h:1080"


def test_manager_start_returns_url_and_code_then_status_ok(monkeypatch):
    """Persistence is REAL here (only the network/poll step is mocked) —
    _save_codex_tokens actually writes to the isolated HERMES_HOME
    (_isolated_home autouse fixture), and we read it back through the
    real hermes_cli.auth store reader rather than a mock spy."""
    import hermes_cli.setup_wizard.device_login as dl

    monkeypatch.setattr(dl.threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        dl,
        "_codex_request_device_code",
        lambda proxy=None: {"user_code": "CODE-1", "device_auth_id": "da", "poll_interval": 1, "verification_url": "https://x/y"},
    )
    monkeypatch.setattr(
        dl,
        "_codex_poll_and_exchange",
        lambda info, *, timeout_seconds, is_current=lambda: True, proxy=None: {
            "tokens": {"access_token": "AT", "refresh_token": "RT"},
            "base_url": "https://api.example",
            "last_refresh": "now",
        },
    )

    manager = dl.DeviceLoginManager()
    result = manager.start("openai-codex")
    assert result["verification_url"] == "https://x/y"
    assert result["user_code"] == "CODE-1"
    assert result["login_id"]

    status = manager.status()
    assert status == {"state": "ok", "error": None, "provider": "openai-codex", "login_id": result["login_id"]}

    from hermes_cli.auth import get_provider_auth_state

    saved = get_provider_auth_state("openai-codex")
    assert saved["tokens"] == {"access_token": "AT", "refresh_token": "RT"}
    # Review finding: a device login must NOT flip config.yaml's
    # model.provider by itself any more (_update_config_for_provider is no
    # longer called from device_login.py at all) — that write is
    # apply_settings' job at submit time. NB: _save_codex_tokens ITSELF
    # still sets auth.json's active_provider as an upstream side effect of
    # _save_provider_state (unrelated to, and not fixed by, this change —
    # verified by reading that function) — config.yaml is the boundary
    # that actually matters here, and the only one this module still
    # touches.
    from hermes_cli.config import load_config

    model_cfg = (load_config() or {}).get("model") or {}
    assert (model_cfg.get("provider") if isinstance(model_cfg, dict) else None) != "openai-codex"


def test_manager_status_defaults_to_idle():
    from hermes_cli.setup_wizard.device_login import DeviceLoginManager

    manager = DeviceLoginManager()
    assert manager.status() == {"state": "idle", "error": None, "provider": None, "login_id": None}


def test_manager_start_error_path_sets_russian_message(monkeypatch):
    import hermes_cli.setup_wizard.device_login as dl
    from hermes_cli.auth import AuthError

    monkeypatch.setattr(dl.threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        dl,
        "_codex_request_device_code",
        lambda proxy=None: {"user_code": "CODE-1", "device_auth_id": "da", "poll_interval": 1, "verification_url": "https://x/y"},
    )

    def _boom(info, *, timeout_seconds, is_current=lambda: True, proxy=None):
        raise AuthError("Login timed out.", provider="openai-codex", code="device_code_timeout")

    monkeypatch.setattr(dl, "_codex_poll_and_exchange", _boom)

    manager = dl.DeviceLoginManager()
    manager.start("openai-codex")
    status = manager.status()
    assert status["state"] == "error"
    assert status["error"] == "Время ожидания входа истекло. Нажмите «Войти по аккаунту» ещё раз."
    assert status["provider"] == "openai-codex"
    # No raw English AuthError text, no code, ever reaches the client.
    assert "timed out" not in status["error"].lower()
    assert "device_code_timeout" not in status["error"]


def test_manager_start_request_failure_surfaces_specific_message_not_generic(monkeypatch):
    """Review finding: /api/device/start's 502 body must carry the
    specific message start() already computed (e.g. the rate-limit
    text), not a flat generic string — this is the manager-side half of
    that fix (app.py's route reads status()["error"] after the raise)."""
    import hermes_cli.setup_wizard.device_login as dl
    from hermes_cli.auth import CODEX_RATE_LIMITED_CODE, AuthError

    def _rate_limited(proxy=None):
        raise AuthError("rate limited", provider="openai-codex", code=CODEX_RATE_LIMITED_CODE)

    monkeypatch.setattr(dl, "_codex_request_device_code", _rate_limited)

    manager = dl.DeviceLoginManager()
    with pytest.raises(AuthError):
        manager.start("openai-codex")
    status = manager.status()
    assert status["state"] == "error"
    assert status["error"] == "Провайдер ограничивает частоту входов — попробуйте через минуту."


def test_manager_start_rejects_unsupported_provider():
    from hermes_cli.setup_wizard.device_login import DeviceLoginManager

    manager = DeviceLoginManager()
    with pytest.raises(ValueError):
        manager.start("anthropic")


def _install_two_generation_codex_flow(monkeypatch, dl):
    """Shared setup for the generation-gate tests below: two start() calls
    each get a distinct user_code AND a distinct token pair, so a test can
    tell — by reading the REAL auth store, never a mock — which
    generation's write (if any) actually landed."""
    codes = iter(["CODE-1", "CODE-2"])
    tokens = iter([
        {"access_token": "AT-GEN1", "refresh_token": "RT-GEN1"},
        {"access_token": "AT-GEN2", "refresh_token": "RT-GEN2"},
    ])
    monkeypatch.setattr(
        dl,
        "_codex_request_device_code",
        lambda proxy=None: {
            "user_code": next(codes),
            "device_auth_id": "da",
            "poll_interval": 1,
            "verification_url": "https://x/y",
        },
    )
    token_by_call = []

    def _finish(info, *, timeout_seconds, is_current=lambda: True, proxy=None):
        pair = next(tokens)
        token_by_call.append(pair)
        return {"tokens": pair, "base_url": "https://api.example", "last_refresh": "now"}

    monkeypatch.setattr(dl, "_codex_poll_and_exchange", _finish)


def test_manager_new_start_supersedes_stale_finish_no_persistence_mocking(monkeypatch):
    """Review finding: the PREVIOUS version of this test mocked
    _save_codex_tokens/_update_config_for_provider to no-ops, so it could
    never have caught the actual bug (a stale finish writing real files
    after being superseded) — it only proved the in-memory status()
    result was gated, not the disk write. This version uses the real
    persistence path against an isolated HERMES_HOME (_isolated_home
    autouse fixture) and asserts on the real auth store."""
    import hermes_cli.setup_wizard.device_login as dl

    monkeypatch.setattr(dl.threading, "Thread", _CapturingThread)
    _install_two_generation_codex_flow(monkeypatch, dl)

    manager = dl.DeviceLoginManager()
    manager.start("openai-codex")  # generation 1, thread captured but not run
    manager.start("openai-codex")  # generation 2 supersedes it, own thread captured

    assert len(_CapturingThread.instances) == 2

    from hermes_cli.auth import get_provider_auth_state

    # Run the STALE (generation-1) thread's work now, after it has been
    # superseded — nothing must land on disk.
    _CapturingThread.instances[0].run()
    assert get_provider_auth_state("openai-codex") is None
    assert manager.status()["state"] == "pending"  # still generation 2's own status

    # Now run the CURRENT (generation-2) thread's work — THIS one must
    # actually write, and with generation 2's own tokens, not generation
    # 1's (proves the write, when it does happen, is the right one).
    _CapturingThread.instances[1].run()
    saved = get_provider_auth_state("openai-codex")
    assert saved is not None
    assert saved["tokens"] == {"access_token": "AT-GEN2", "refresh_token": "RT-GEN2"}
    assert manager.status()["state"] == "ok"


def test_manager_retire_prevents_a_still_pending_login_from_persisting(monkeypatch):
    """The other trigger for the generation gate (review finding 1c):
    retire() is called by app.py's _run_submit right after a successful
    apply, BEFORE the gateway restart — a device login that was still
    polling in the background at that moment must never be allowed to
    write tokens afterward, even though it was never superseded by a
    NEW start()."""
    import hermes_cli.setup_wizard.device_login as dl

    monkeypatch.setattr(dl.threading, "Thread", _CapturingThread)
    _install_two_generation_codex_flow(monkeypatch, dl)  # only the first pair is used here

    manager = dl.DeviceLoginManager()
    manager.start("openai-codex")
    assert len(_CapturingThread.instances) == 1

    # The client successfully submitted the form with something ELSE
    # entirely (an API key for a different provider, say) while this
    # login was still in flight.
    manager.retire()

    from hermes_cli.auth import get_provider_auth_state

    _CapturingThread.instances[0].run()
    assert get_provider_auth_state("openai-codex") is None
    # retire() only touches persistence — it does not fabricate an error;
    # the in-memory status is simply stale/ignored from here on, same as
    # the superseded-by-new-start() case.


# ---------------------------------------------------------------------------
# device_login_is_valid — the ONLY caller is /api/submit's gate (network-
# capable, refreshes if needed).
# ---------------------------------------------------------------------------


def test_device_login_is_valid_true_when_credentials_resolve(monkeypatch):
    import hermes_cli.auth as auth_mod
    from hermes_cli.setup_wizard.device_login import device_login_is_valid

    monkeypatch.setattr(
        auth_mod, "resolve_codex_runtime_credentials", lambda **kw: {"api_key": "AT"}
    )
    assert device_login_is_valid("openai-codex") is True


def test_device_login_is_valid_false_on_auth_error(monkeypatch):
    import hermes_cli.auth as auth_mod
    from hermes_cli.setup_wizard.device_login import device_login_is_valid

    def _raise(**kw):
        raise auth_mod.AuthError("nope", provider="openai-codex", code="codex_auth_missing")

    monkeypatch.setattr(auth_mod, "resolve_codex_runtime_credentials", _raise)
    assert device_login_is_valid("openai-codex") is False


def test_device_login_is_valid_false_for_non_device_code_provider():
    from hermes_cli.setup_wizard.device_login import device_login_is_valid

    assert device_login_is_valid("anthropic") is False


def test_device_login_is_valid_checks_minimax_oauth(monkeypatch):
    import hermes_cli.auth as auth_mod
    from hermes_cli.setup_wizard.device_login import device_login_is_valid

    monkeypatch.setattr(
        auth_mod, "resolve_minimax_oauth_runtime_credentials", lambda **kw: {"api_key": "MX-AT"}
    )
    assert device_login_is_valid("minimax-oauth") is True


# ---------------------------------------------------------------------------
# device_login_looks_active — the /api/form prefill's cheap, NETWORK-FREE
# check (review finding: device_login_is_valid was being called on every
# GET /api/form, which refreshes over the network and risks tripping
# MiniMax's quarantine-on-repeated-refresh-failure logic on a plain page
# reload). No resolve_*_runtime_credentials mocking here on purpose — these
# tests write real store entries and assert the pure reader behaves
# correctly against them, since that's exactly the "no network" contract
# being tested.
# ---------------------------------------------------------------------------


def test_device_login_looks_active_true_for_codex_with_a_saved_token():
    from hermes_cli.auth import _save_codex_tokens
    from hermes_cli.setup_wizard.device_login import device_login_looks_active

    # A plain (non-JWT-shaped) access_token decodes to no claims at all
    # (_decode_jwt_claims returns {} for anything that isn't a 3-part JWT),
    # which _codex_access_token_is_expiring treats as "no exp claim -> not
    # expiring" — this is the same behavior a real opaque token would see.
    _save_codex_tokens({"access_token": "AT-1", "refresh_token": "RT-1"})
    assert device_login_looks_active("openai-codex") is True


def test_device_login_looks_active_false_for_codex_with_nothing_saved():
    from hermes_cli.setup_wizard.device_login import device_login_looks_active

    assert device_login_looks_active("openai-codex") is False


def test_device_login_looks_active_never_hits_the_network_for_codex(monkeypatch):
    """The actual point of this function: even if the network resolver
    would explode, the cheap check must not go anywhere near it."""
    import hermes_cli.auth as auth_mod
    from hermes_cli.setup_wizard.device_login import device_login_looks_active

    def _boom(**kw):
        raise AssertionError("device_login_looks_active must not call the network resolver")

    monkeypatch.setattr(auth_mod, "resolve_codex_runtime_credentials", _boom)
    # False (nothing saved) — but the point is _boom must never fire.
    assert device_login_looks_active("openai-codex") is False


def test_device_login_looks_active_true_for_minimax_with_future_expiry():
    from datetime import datetime, timedelta, timezone

    from hermes_cli.auth import _minimax_save_auth_state
    from hermes_cli.setup_wizard.device_login import device_login_looks_active

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    _minimax_save_auth_state(
        {
            "provider": "minimax-oauth",
            "region": "global",
            "portal_base_url": "https://minimax.example",
            "inference_base_url": "https://minimax.example/v1",
            "client_id": "client-1",
            "scope": "scope",
            "token_type": "Bearer",
            "access_token": "MX-AT",
            "refresh_token": "MX-RT",
            "resource_url": None,
            "obtained_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": future,
            "expires_in": 3600,
        }
    )
    assert device_login_looks_active("minimax-oauth") is True


def test_device_login_looks_active_false_for_minimax_with_past_expiry():
    from datetime import datetime, timedelta, timezone

    from hermes_cli.auth import _minimax_save_auth_state
    from hermes_cli.setup_wizard.device_login import device_login_looks_active

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _minimax_save_auth_state(
        {
            "provider": "minimax-oauth",
            "region": "global",
            "portal_base_url": "https://minimax.example",
            "inference_base_url": "https://minimax.example/v1",
            "client_id": "client-1",
            "scope": "scope",
            "token_type": "Bearer",
            "access_token": "MX-AT",
            "refresh_token": "MX-RT",
            "resource_url": None,
            "obtained_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": past,
            "expires_in": 0,
        }
    )
    assert device_login_looks_active("minimax-oauth") is False


def test_device_login_looks_active_false_for_non_device_code_provider():
    from hermes_cli.setup_wizard.device_login import device_login_looks_active

    assert device_login_looks_active("anthropic") is False
