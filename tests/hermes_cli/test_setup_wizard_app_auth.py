"""HTTP Basic auth gate (spec 8, §8.3): the `_BasicAuthMiddleware` in
``hermes_cli/setup_wizard/app.py``. ``wizard_app`` / ``app_env`` /
``logged_in`` fixtures live in ``tests/hermes_cli/conftest.py``.

There is no login route and no session any more (spec 8, §4.3 removes
``generation_fingerprint()``/cookie sessions entirely) — every request
either carries a valid ``Authorization: Basic`` header or it doesn't, and
the middleware decides per request.
"""
import base64
import re
import stat
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _basic_header(login: str, password: str) -> str:
    token = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def test_no_authorization_header_is_401_with_www_authenticate(app_env):
    client, _ = app_env
    r = client.get("/")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Basic")


def test_correct_credentials_are_accepted(app_env):
    client, (login, password) = app_env
    r = client.get("/", auth=(login, password))
    assert r.status_code == 200


def test_wrong_password_is_401_with_www_authenticate(app_env):
    client, (login, _) = app_env
    r = client.get("/", auth=(login, "definitely-wrong"))
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Basic")


def test_wrong_login_is_401_with_www_authenticate(app_env):
    client, (_, password) = app_env
    r = client.get("/", auth=("not-the-real-login", password))
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Basic")


def test_wrong_login_and_wrong_password_get_the_identical_response(app_env):
    """Spec §14.5: never lets a client distinguish "unknown login" from
    "wrong password for a known one" — same status, same body, same
    header for both."""
    client, (login, password) = app_env
    by_login = client.get("/", auth=("not-the-real-login", password))
    by_password = client.get("/", auth=(login, "definitely-wrong"))
    assert by_login.status_code == by_password.status_code == 401
    assert by_login.text == by_password.text
    assert by_login.headers.get("www-authenticate") == by_password.headers.get("www-authenticate")


def test_realm_is_ascii_only(app_env):
    """Spec §8.3.5: the realm must be short and Latin-only — browser
    support for a non-ASCII WWW-Authenticate realm is unreliable. The
    Russian explanation belongs in the response body, not this header."""
    client, _ = app_env
    r = client.get("/")
    header = r.headers.get("www-authenticate", "")
    assert header.startswith("Basic realm=")
    assert header.isascii()


def test_401_body_carries_the_mail_hint_and_a_retry_link(app_env):
    """Spec §6/§12: the body a browser shows after "Cancel" must explain
    where the credentials come from (the email) and offer a way back."""
    client, _ = app_env
    r = client.get("/")
    assert r.status_code == 401
    assert "письм" in r.text.lower()
    assert "trix" in r.text.lower()
    assert 'href="/"' in r.text


def test_401_body_never_carries_wizard_state(app_env, tmp_path, monkeypatch):
    """Spec §14.12: the 401 body must not leak anything from
    ``WizardState`` — no bot name, no completed/disabled flag, nothing
    config-derived. Proven by making sure the real page renderer is never
    even invoked for an unauthenticated request."""
    client, _ = app_env
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(wapp, "render_page", lambda *a, **k: calls.append(a) or "SHOULD-NOT-RENDER")

    r = client.get("/")
    assert r.status_code == 401
    assert calls == []
    assert "SHOULD-NOT-RENDER" not in r.text


def test_five_failures_from_one_ip_lock_that_ip(app_env):
    client, (login, _) = app_env
    for _ in range(5):
        assert client.get("/", auth=(login, "wrong")).status_code == 401
    r = client.get("/", auth=(login, "wrong"))
    assert r.status_code == 429


def test_locked_ip_gets_429_without_www_authenticate(app_env):
    """Spec §14.14/§8.3.6: this is the load-bearing case — WITH
    WWW-Authenticate here, a browser holding the CORRECT password would
    just re-prompt forever with no visible reason."""
    client, (login, password) = app_env
    for _ in range(6):
        client.get("/", auth=(login, "wrong"))
    r = client.get("/", auth=(login, password))
    assert r.status_code == 429
    assert "www-authenticate" not in {h.lower() for h in r.headers}


def test_locked_ip_body_states_how_long_to_wait(app_env):
    client, (login, _) = app_env
    for _ in range(6):
        client.get("/", auth=(login, "wrong"))
    r = client.get("/")
    assert r.status_code == 429
    assert any(word in r.text for word in ("сек", "мин"))


def test_password_never_logged(app_env, caplog):
    client, (login, password) = app_env
    with caplog.at_level("DEBUG"):
        client.get("/", auth=(login, password))
        client.get("/", auth=(login, "WRONGSECRET"))
    assert password not in caplog.text and "WRONGSECRET" not in caplog.text


def test_closed_wizard_is_410(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.state import WizardState
    st = WizardState.load()
    st.issue_primary("trix-closedtest", "somepassword0000000000000000")
    st.set_disabled(True)
    from hermes_cli.setup_wizard.app import create_app
    client = TestClient(create_app())
    assert client.get("/").status_code == 410


def test_closed_wizard_wins_over_auth(tmp_path, monkeypatch):
    """Middleware ordering: the closed-wizard gate must fire before the
    Basic-auth gate, so a disabled wizard 410s uniformly rather than
    prompting for a password nobody can use any more."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.state import WizardState
    st = WizardState.load()
    st.issue_primary("trix-closedtest", "somepassword0000000000000000")
    st.set_disabled(True)
    from hermes_cli.setup_wizard.app import create_app
    client = TestClient(create_app())
    r = client.get("/", auth=("trix-closedtest", "somepassword0000000000000000"))
    assert r.status_code == 410


def test_closed_wizard_wins_over_origin_guard(tmp_path, monkeypatch):
    """Middleware ordering: closed-wizard gate must fire before the Origin
    guard too, so a closed wizard always 410s uniformly rather than
    sometimes 403ing depending on what Origin a (still-live) attacker
    sends."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.state import WizardState
    st = WizardState.load()
    st.issue_primary("trix-closedtest", "somepassword0000000000000000")
    st.set_disabled(True)
    from hermes_cli.setup_wizard.app import create_app
    client = TestClient(create_app())
    r = client.post(
        "/api/submit",
        json={},
        auth=("trix-closedtest", "somepassword0000000000000000"),
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 410


def test_health_endpoint_also_requires_auth(app_env):
    """Owner decision recorded in app.py (spec §14.11 leaves this route's
    fate an explicit call): /api/health is closed uniformly with
    everything else — nothing in this codebase calls it as a liveness
    probe."""
    client, _ = app_env
    r = client.get("/api/health")
    assert r.status_code == 401


def test_validation_error_does_not_echo_secret_value(logged_in):
    r = logged_in.post("/api/submit", json={"provider": "not-a-dict-so-this-422s"})
    assert r.status_code == 422
    assert "not-a-dict-so-this-422s" not in r.text


def test_origin_guard_blocks_foreign_origin_on_submit(logged_in):
    r = logged_in.post(
        "/api/submit",
        json={},
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_origin_guard_allows_missing_origin(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": False, "error": "x"})
    r = logged_in.post("/api/submit", json={"telegram_token": "bad"})
    # Reaches the route (422 from validation, not 403 from the Origin
    # guard) — proves a request with no Origin header at all passes.
    assert r.status_code != 403


def test_origin_guard_rejects_null_origin(logged_in):
    """Origin: null is the sandboxed-iframe/data:/redirect CSRF vector —
    it must NOT be treated as "no signal, pass through"."""
    r = logged_in.post("/api/submit", json={}, headers={"Origin": "null"})
    assert r.status_code == 403


def test_origin_guard_rejects_unparsable_origin(logged_in):
    r = logged_in.post("/api/submit", json={}, headers={"Origin": "https://"})
    assert r.status_code == 403


def test_auth_cache_avoids_reverifying_on_every_request(app_env, monkeypatch):
    """Spec §8.3.2: a repeated request with the SAME Authorization header
    must not pay the scrypt cost again within the cache TTL."""
    client, (login, password) = app_env
    from hermes_cli.setup_wizard.state import WizardState

    calls = []
    real_verify = WizardState.verify

    def counting_verify(self, *a, **k):
        calls.append(1)
        return real_verify(self, *a, **k)

    monkeypatch.setattr(WizardState, "verify", counting_verify)

    for _ in range(5):
        assert client.get("/api/health", auth=(login, password)).status_code == 200
    assert len(calls) == 1


def test_x_forwarded_for_does_not_affect_the_lockout_bucket(app_env):
    """Spec §8.1/§14.16: the wizard has no reverse proxy in front of it —
    a spoofed X-Forwarded-For must never influence which IP bucket a
    failure is recorded against."""
    client, (login, password) = app_env
    for _ in range(6):
        client.get("/", auth=(login, "wrong"), headers={"X-Forwarded-For": "9.9.9.9"})

    from hermes_cli.setup_wizard.state import WizardState

    st = WizardState.load()
    assert st.retry_after_seconds("9.9.9.9") == 0
    # The real client's own IP (whatever TestClient reports itself as) is
    # the one that actually got locked.
    r = client.get("/", auth=(login, password))
    assert r.status_code == 429


def test_successful_verification_does_not_write_state_json(app_env, tmp_path):
    """Spec §8.3.2/§14.15: a successful check must not rewrite
    state.json — only a failure (lockout accounting) does."""
    client, (login, password) = app_env
    from hermes_cli.setup_wizard.state import state_path

    before = state_path().stat().st_mtime_ns
    r = client.get("/", auth=(login, password))
    assert r.status_code == 200
    after = state_path().stat().st_mtime_ns
    assert after == before


@pytest.mark.parametrize("bad_header", ["", "Bearer sometoken", "Basic", "Basic not-base64!!"])
def test_malformed_or_missing_authorization_is_401_not_a_500(app_env, bad_header):
    client, _ = app_env
    headers = {"Authorization": bad_header} if bad_header else {}
    r = client.get("/", headers=headers)
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Basic")


# ---------------------------------------------------------------------------
# §6/§8.3.5 — the 401 body's mail hint must not fabricate a subject/sender
# XDataPlus has not actually chosen (spec §12.1 is still open on this).
# ---------------------------------------------------------------------------


def test_401_body_mail_hint_agrees_with_the_locale_and_invents_nothing(app_env):
    """The 401 page and the `/setup` Telegram reply must tell a client the
    same thing about the credentials email.

    Earlier both carried a bracketed placeholder for the still-undecided
    subject/sender -- honest, but it read as an unfinished sentence to the
    client. Spec 12 Task 7 replaced that on both surfaces with wording that
    names neither, and points at a search that actually works.

    What must not come back is the original danger this guard was written
    for: a plausible-looking INVENTED subject or sender. A client who
    clicks "Cancel" and then searches their mailbox for a made-up string
    finds nothing, because no such email was ever promised to exist.
    """
    client, _ = app_env
    r = client.get("/")
    assert r.status_code == 401
    body = r.text

    locales_path = _REPO_ROOT / "locales" / "ru.yaml"
    catalog = yaml.safe_load(locales_path.read_text(encoding="utf-8"))
    reply = catalog["trix"]["setup_wizard"]["reply"]

    # Neither surface may carry an unfinished-looking bracketed placeholder.
    assert not re.search(r"\[[^\]]*уточняется[^\]]*\]", reply), (
        "the Telegram reply is back to a bracketed placeholder"
    )
    assert not re.search(r"\[[^\]]*уточняется[^\]]*\]", body), (
        "the 401 page is back to a bracketed placeholder"
    )

    # Neither may invent a subject line or a sender address.
    assert "@" not in body, "the 401 page names a sender address"
    for invented in ("Доступ к панели", "noreply", "no-reply"):
        assert invented not in body, f"the 401 page invents {invented!r}"
        assert invented not in reply, f"the Telegram reply invents {invented!r}"

    # Both must give the client the search that actually works.
    assert "Trix" in body and "Trix" in reply
    assert "поищите в почте" in body.lower()
    assert "поищите в почте" in reply.lower()

def test_401_body_does_not_invent_a_plausible_mail_subject_or_sender(app_env):
    """Companion to the test above: guards against reverting to a
    specific-looking but fabricated subject/sender (e.g. "Доступ к панели
    настроек Trix Agent" / "noreply@xdataplus.ru") — nobody has confirmed
    either exists (spec §12.1)."""
    client, _ = app_env
    r = client.get("/")
    assert "noreply@xdataplus.ru" not in r.text
    assert "Доступ к панели настроек Trix Agent" not in r.text


# ---------------------------------------------------------------------------
# §8.2 — the access log (real file, not the Python logging caplog fixture
# `test_password_never_logged` above already covers).
# ---------------------------------------------------------------------------


def _read_access_log_lines() -> list[str]:
    from hermes_cli.setup_wizard.app import _access_log_path

    path = _access_log_path()
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def test_access_log_file_is_created_on_first_attempt(app_env):
    from hermes_cli.setup_wizard.app import _access_log_path

    client, (login, password) = app_env
    assert not _access_log_path().exists()
    client.get("/", auth=(login, password))
    assert _access_log_path().is_file()


def test_access_log_records_time_ip_login_verdict_reason(app_env):
    """§8.2: "время, IP, логин, вердикт, причина отказа" — one line per
    attempt, granted or denied, with those five fields."""
    client, (login, password) = app_env
    client.get("/", auth=(login, password))
    client.get("/", auth=(login, "definitely-wrong"))

    lines = _read_access_log_lines()
    assert len(lines) == 2

    granted, denied = lines[0].split("\t"), lines[1].split("\t")
    assert len(granted) == 5 and len(denied) == 5
    ts, ip, logged_login, verdict, reason = granted
    assert ts and ip
    assert logged_login == login
    assert verdict == "granted"
    assert reason == ""

    ts2, ip2, logged_login2, verdict2, reason2 = denied
    assert logged_login2 == login
    assert verdict2 == "denied"
    assert reason2 == "invalid_credentials"


def test_access_log_never_contains_the_password_success_or_failure(app_env):
    """§14.6: not a single byte of the password lands in the log, in any
    form — success or failure, correct or wrong guess."""
    client, (login, password) = app_env
    wrong_guess = "totally-distinguishable-wrong-guess-000000"
    client.get("/", auth=(login, password))
    client.get("/", auth=(login, wrong_guess))
    client.get("/api/health", auth=(login, password))  # cache-hit path too

    raw = "\n".join(_read_access_log_lines())
    assert password not in raw
    assert wrong_guess not in raw


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits")
def test_access_log_file_is_0600(app_env):
    from hermes_cli.setup_wizard.app import _access_log_path

    client, (login, password) = app_env
    client.get("/", auth=(login, password))
    mode = stat.S_IMODE(_access_log_path().stat().st_mode)
    assert oct(mode)[-3:] == "600"


def test_access_log_crlf_in_login_cannot_forge_an_extra_line(tmp_path, monkeypatch):
    """A crafted login containing CR (`\\r`) must not let a single request
    render as two convincing log lines. `_log_access` already stripped
    `\\t`/`\\n` from the untrusted login — `\\r` alone is just as capable
    of forging a bogus row in a terminal or any `\\r\\n`-naive log viewer,
    and was NOT stripped before this fix. This drives `_log_access`
    directly (not through the HTTP layer) so the test is about the
    logging function's own escaping, independent of how a request reaches
    it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import app as wapp

    wapp._log_access("203.0.113.9", "real-login\rFAKE-LOGIN\tgranted\t", "denied", "invalid_credentials")
    lines = (tmp_path / "setup-wizard" / "access.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, lines
    fields = lines[0].split("\t")
    assert len(fields) == 5, fields
    assert "\r" not in lines[0]


def test_access_log_rotates_instead_of_growing_without_bound(app_env, monkeypatch):
    """§14.17: "не растёт неограниченно... ротация или потолок размера".
    Forces the rotation threshold down to a few bytes so the test doesn't
    have to write megabytes of real attempts to prove the cap exists."""
    from hermes_cli.setup_wizard import app as wapp
    from hermes_cli.setup_wizard.app import _access_log_path

    monkeypatch.setattr(wapp, "_ACCESS_LOG_MAX_BYTES", 100)

    client, (login, password) = app_env
    for _ in range(20):
        client.get("/", auth=(login, "wrong-guess-to-force-a-write-every-time"))

    backup_path = _access_log_path().with_name(_access_log_path().name + ".1")
    assert backup_path.exists(), "expected a rotated .1 backup once the (lowered) size cap was crossed"
    # The live file never grows past roughly one rotation's worth of new
    # lines past the cap — nowhere near what 20 unbounded lines would take.
    assert _access_log_path().stat().st_size < wapp._ACCESS_LOG_MAX_BYTES * 3


# ---------------------------------------------------------------------------
# §8.3.2 / event loop — WizardState.verify() (scrypt-on-16MiB) must run off
# the event loop, or a burst of failed guesses stalls every other in-flight
# request this single-process server is holding open.
# ---------------------------------------------------------------------------


def test_basic_auth_verification_does_not_block_the_event_loop(wizard_app):
    try:
        import httpx
    except ImportError:
        pytest.skip("httpx not installed")
    import asyncio
    import threading
    import unittest.mock

    app, (login, password) = wizard_app
    from hermes_cli.setup_wizard.state import WizardState

    hold_s = 1.0
    release = threading.Event()
    entered = threading.Event()
    real_verify = WizardState.verify

    def slow_verify(self, *a, **k):
        entered.set()
        release.wait(hold_s)
        return real_verify(self, *a, **k)

    async def _scenario():
        with unittest.mock.patch.object(WizardState, "verify", slow_verify):
            transport = httpx.ASGITransport(app=app)
            ticks = 0

            async def _heartbeat(stop: asyncio.Event):
                nonlocal ticks
                while not stop.is_set():
                    await asyncio.sleep(0.02)
                    ticks += 1

            stop = asyncio.Event()
            hb = asyncio.create_task(_heartbeat(stop))
            await asyncio.sleep(0)

            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
                request_task = asyncio.create_task(client.get("/", auth=(login, password)))
                # Give the request a moment to actually reach state.verify()
                # before racing the release — a fixed sleep, not a busy
                # poll, but generous enough that the thread has certainly
                # started blocking on it inside the timeout below.
                loop = asyncio.get_running_loop()
                assert await loop.run_in_executor(None, entered.wait, 5), (
                    "verify() was never even entered — test setup is broken"
                )
                resp = await request_task

            stop.set()
            release.set()
            await hb
            return resp, ticks

    resp, ticks = asyncio.run(_scenario())
    assert resp.status_code == 200
    # Pre-fix, a synchronous scrypt call directly on the coroutine blocks
    # the ONLY event loop this single-process server has for the whole
    # hold — the heartbeat gets ~0 ticks. Threshold generous for slow CI.
    assert ticks >= 10, (
        f"event loop heartbeat only ticked {ticks} time(s) while "
        "WizardState.verify() was running — Basic auth is blocking the loop again"
    )
