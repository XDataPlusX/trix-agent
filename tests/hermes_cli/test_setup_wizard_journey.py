"""End-to-end client journeys through the setup wizard, spec 8
(``docs/product/specs/2026-08-25-trix-agent-wizard-permanent-access-
design.md``).

Where the other ``test_setup_wizard_app_*.py`` files exercise one endpoint
or invariant at a time, this file walks the wizard the way an actual
client would — one scenario per test, story-shaped — against §15's
acceptance path:

1. Fresh machine, credentials issued to the ``primary`` slot: the door is
   shut without a header, open with the right login+password.
2. A typo along the way gets a readable Russian error at the right field
   and writes nothing.
3. The corrected submission succeeds and settings are written.
4. THE POINT OF THE SPEC: after success the wizard stays reachable — same
   address, same login/password, no self-extinguish.
5. A return visit: prefilled values, masked secrets, a proxy-only change
   persists without re-running any live probe.
6. Brute-force lockout is per-IP (§8.1) — one hammered address never
   blocks another.
7. The emergency ``temporary`` slot (§9.3) lets an admin in without
   touching — or being able to lock out — the client's own ``primary``
   credentials.

This is a full rewrite (spec §14.1 called it out explicitly): the old
version of this file was built on the model spec 8 replaces — an HTML
login form, ``POST /api/login``, a cookie session, a one-time password,
and self-extinguish after success. None of that exists any more. Fixtures
(``wizard_app``/``app_env``/``logged_in``) live in
``tests/hermes_cli/conftest.py``; the mocking shape for the live/subprocess
steps of ``_run_submit`` (``_ok_stack``) mirrors
``test_setup_wizard_app_submit.py``.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from hermes_constants import get_hermes_home

REPO_ROOT = Path(__file__).resolve().parents[2]
_TRIX_TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"

_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")

# A full, legal submission a real client would send once every field is
# filled in — same shape/values as test_setup_wizard_app_submit.py's
# GOOD_FORM (kept in sync deliberately: both exercise the same
# _SubmitBody contract).
GOOD_FORM = {
    "telegram_token": "123:abc",
    "allowed_users": "111,222",
    "proxy": "",
    "provider": {
        "name": "openrouter",
        "env_var": "OPENROUTER_API_KEY",
        "api_key": "sk-or-test",
        "base_url": "",
        "model": "z-ai/glm-5.2",
    },
    "fallback": {
        "name": "zai",
        "env_var": "GLM_API_KEY",
        "api_key": "glm-test",
        "base_url": "",
        "model": "glm-5.2",
    },
    # "ddgs" — the real live catalog value for DuckDuckGo; search_backend
    # is now validated against the live "web" catalog (same discipline
    # search_env.key already had), so a placeholder name would 422.
    "search_backend": "ddgs",
    "search_env": None,
    "browser_backend": "chromium",
    # Часовой пояс обязателен со спеки 11 — «полная законная отправка»
    # теперь включает его так же, как токен бота.
    "timezone": "Europe/Moscow",
    "tts_voice": "ru-RU-SvetlanaNeural",
    # {} (not None): a form that isn't touching Home Assistant sends the
    # no-op default, not the explicit-clear signal (see apply.py/app.py's
    # own docstrings on the finding-5/7 null contract).
    "hass": {},
}


def _ok_stack(monkeypatch, wapp):
    """Mock the live/subprocess steps of ``_run_submit`` to a clean
    success — Telegram/provider-key HTTP probes, the ``hermes gateway
    restart`` subprocess, and the liveness poll. ``apply_settings`` is
    left real so a submission's effect on disk is genuinely observable.
    """
    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"})
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "reachable": True})
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"})


# ---------------------------------------------------------------------------
# 1. First visit: nothing without credentials, everything with them.
# ---------------------------------------------------------------------------


def test_journey_first_visit_gate_blocks_without_credentials_admits_with_them(wizard_app):
    """§15.1/§15.2 + spec §14.11: a brand-new machine, credentials fresh
    from the email (the ``primary`` slot). A request with no
    ``Authorization`` header must not get a single byte of the wizard's
    own markup — proven by making sure the page renderer itself is never
    invoked, not just by checking the status code. The same address with
    the correct login+password admits the client.

    Uses its own local ``MonkeyPatch`` (not the ``monkeypatch`` fixture)
    scoped to a ``with`` block: the ``wizard_app`` fixture already used
    ``monkeypatch`` to set ``HERMES_HOME`` for this test, and sharing that
    same fixture instance here would mean ``.undo()`` below reverts the
    fixture's env var too, not just the local patch.
    """
    import pytest

    app, (login, password) = wizard_app
    from hermes_cli.setup_wizard import app as wapp

    client = TestClient(app, base_url="https://testserver")

    calls = []
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(wapp, "render_page", lambda *a, **k: calls.append(a) or "SHOULD-NOT-RENDER")
        r = client.get("/")
        assert r.status_code == 401
        assert r.headers.get("www-authenticate", "").startswith("Basic")
        assert calls == []
        assert "SHOULD-NOT-RENDER" not in r.text

    r = client.get("/", auth=(login, password))
    assert r.status_code == 200

    # A wrong password at the same address is refused, not merely ignored.
    r = client.get("/", auth=(login, "definitely-not-the-mailed-password"))
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 2 + 3 + 4. Typo -> clean failure -> corrected submit succeeds -> THE
# wizard stays reachable afterwards. This is the whole point of spec 8.
# ---------------------------------------------------------------------------


def test_journey_typo_then_fix_and_wizard_stays_open_after_success(wizard_app, monkeypatch):
    app, (login, password) = wizard_app
    from hermes_cli.setup_wizard import app as wapp
    from hermes_cli.setup_wizard.state import WizardState

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy(_TRIX_TEMPLATE, home / "config.yaml")
    (home / ".env").write_text("", encoding="utf-8")
    original_config_bytes = (home / "config.yaml").read_bytes()

    client = TestClient(app, base_url="https://testserver")
    client.auth = (login, password)

    # 2. A typo'd bot token on the way through: a readable Russian error
    # at the right field, and NOTHING written.
    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": False, "error": "Токен неверный — проверьте у @BotFather"}
    )
    form = dict(GOOD_FORM)
    form["telegram_token"] = "123:typo"
    r = client.post("/api/submit", json=form)
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert "telegram_token" in errors
    assert _CYRILLIC.search(errors["telegram_token"]), errors  # a human-readable RU message, not a code

    assert (home / ".env").read_text(encoding="utf-8") == ""
    assert (home / "config.yaml").read_bytes() == original_config_bytes

    # 3. The corrected submission succeeds and is actually written.
    _ok_stack(monkeypatch, wapp)
    r = client.post("/api/submit", json=GOOD_FORM)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["bot_username"] == "trixbot"
    assert "OPENROUTER_API_KEY=" in (home / ".env").read_text(encoding="utf-8")

    # 4. THE load-bearing assertion of this whole spec: the wizard does
    # NOT self-extinguish. Same process, same state, still open.
    assert WizardState.load().is_open() is True
    assert WizardState.load().is_disabled() is False

    # And a brand-new "browser" (a fresh TestClient, standing in for the
    # client coming back weeks later on a different machine/tab) reaches
    # the SAME address with the SAME mailed login/password and gets in —
    # not a 410, not a new password requirement.
    later_client = TestClient(app, base_url="https://testserver")
    r = later_client.get("/", auth=(login, password))
    assert r.status_code == 200

    # The gate is still a real gate, not a rubber stamp: no credentials
    # still 401s, it did not quietly turn into "anyone gets in now".
    r = later_client.get("/")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 5. Return visit: prefilled + masked, and a narrow change (proxy) saves
#    without re-running the live probes a first-run submission needs.
# ---------------------------------------------------------------------------


def test_journey_return_visit_prefills_masks_and_proxy_change_persists(wizard_app, monkeypatch):
    app, (login, password) = wizard_app
    from hermes_cli.setup_wizard import app as wapp
    from hermes_cli.setup_wizard.apply import apply_settings

    # Seed the "already configured, prox died two months later" state a
    # real returning client would have.
    seed = apply_settings(dict(GOOD_FORM))
    assert seed["ok"], seed

    client = TestClient(app, base_url="https://testserver")
    client.auth = (login, password)

    r = client.get("/api/form")
    assert r.status_code == 200
    # Masked, not blank and not the real value anywhere in the body.
    assert GOOD_FORM["telegram_token"] not in r.text
    assert GOOD_FORM["provider"]["api_key"] not in r.text
    assert GOOD_FORM["fallback"]["api_key"] not in r.text
    current = r.json()["current"]
    assert current["telegram_token"]["is_set"] is True
    assert current["provider"]["api_key"]["is_set"] is True

    # The client only wants to fix the dead proxy this time — nothing
    # else is retyped. Prove the live Telegram/provider-key probes are
    # NOT re-run for fields the client left untouched (return-mode no-op
    # contract, _SubmitBody's own docstring).
    network_calls: list[str] = []

    def _record(name):
        def _inner(*_a, **_k):
            network_calls.append(name)
            return {"ok": True, "reachable": True}

        return _inner

    monkeypatch.setattr(wapp, "check_telegram_token", _record("check_telegram_token"))
    monkeypatch.setattr(wapp, "check_provider_key", _record("check_provider_key"))
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"})

    new_proxy = "socks5://127.0.0.1:1080"
    r = client.post("/api/submit", json={"proxy": new_proxy})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert network_calls == []

    # The new proxy is actually on disk, and comes back on the next form
    # load — a real, persisted change, not just an accepted request.
    r = client.get("/api/form")
    assert r.json()["current"]["proxy"] == new_proxy


# ---------------------------------------------------------------------------
# 6. Brute force from one address never locks another.
# ---------------------------------------------------------------------------


def test_journey_bruteforce_lockout_is_per_ip(wizard_app):
    app, (login, password) = wizard_app

    attacker = TestClient(app, base_url="https://testserver", client=("203.0.113.50", 51234))
    someone_else = TestClient(app, base_url="https://testserver", client=("203.0.113.99", 51234))

    for _ in range(5):
        assert attacker.get("/", auth=(login, "wrong")).status_code == 401

    r = attacker.get("/", auth=(login, "wrong"))
    assert r.status_code == 429
    # Load-bearing per spec §8.3.6: WITH WWW-Authenticate here a browser
    # holding the correct password would just re-prompt forever, with no
    # visible reason.
    assert "www-authenticate" not in {h.lower() for h in r.headers}

    # Even the RIGHT password from the locked address is refused while
    # locked — it's an address lockout, not a "5 wrong guesses only"
    # counter.
    r = attacker.get("/", auth=(login, password))
    assert r.status_code == 429

    # A different address, same credentials, unaffected.
    r = someone_else.get("/", auth=(login, password))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 7. Emergency path: `hermes setup-wizard open`'s temporary password gets
#    an admin in without disturbing the client's own permanent login.
# ---------------------------------------------------------------------------


def test_journey_emergency_temporary_password_preserves_primary(wizard_app):
    app, (login, password) = wizard_app
    from hermes_cli.setup_wizard.cli import issue_temporary_password
    from hermes_cli.setup_wizard.state import WizardState

    temp_password = issue_temporary_password(60 * 60)

    client = TestClient(app, base_url="https://testserver")
    # The temporary slot accepts ANY login string (spec §4.2 — it's the
    # emergency path, login plays no role there).
    r = client.get("/", auth=("whatever-login", temp_password))
    assert r.status_code == 200

    # The client's own mailed credentials still work afterward — issuing
    # (and using) the emergency password did not overwrite `primary`.
    r = client.get("/", auth=(login, password))
    assert r.status_code == 200
    assert WizardState.load().primary_login() == login
