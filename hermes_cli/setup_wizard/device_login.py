"""Device-code login, driven from the browser (owner requirement 2).

Before this module, the wizard's device-code providers (``openai-codex``,
``minimax-oauth`` — ``providers_view.DEVICE_CODE_PROVIDERS``) told the owner
to "log in from the command line" instead of actually logging in. That is
gone: ``DeviceLoginManager`` runs the real device-code exchange from inside
the wizard's own FastAPI process, so the "Войти по аккаунту" button in the
browser works end to end.

Reuse vs. duplication (read before changing either provider's flow):

- **MiniMax** (``_minimax_start_device_code`` / ``_minimax_finish_device_code``
  below) is a thin wrapper around ``hermes_cli.auth``'s OWN
  client-parametrized helpers — ``_minimax_pkce_pair``,
  ``_minimax_request_user_code(client, ...)``, ``_minimax_poll_token(client,
  ...)``, ``_minimax_resolve_token_expiry_unix``, ``_minimax_save_auth_state``.
  Those already take an ``httpx.Client`` as an explicit parameter and never
  touch stdout, so there is no HTTP logic to duplicate. The two steps share
  ONE ``httpx.Client`` (matching the CLI's ``_minimax_oauth_login``, which
  opens a single ``with httpx.Client()`` around both) — the client is opened
  in ``_minimax_start_device_code()`` and handed to
  ``_minimax_finish_device_code()`` inside the info dict instead of being
  closed and reopened, since the two calls now straddle a thread boundary
  (request happens synchronously in ``start()``; poll happens later, on the
  background thread) that the CLI's single function body never had to cross.

- **Codex** (``_codex_request_device_code`` / ``_codex_poll_and_exchange``
  below) genuinely duplicates the request-usercode + poll + token-exchange
  steps of ``hermes_cli.auth._codex_device_code_login``. That function is
  written for a single-threaded CLI: its poll loop blocks on ``time.sleep``
  AND writes progress via bare ``print()`` to the process's real stdout.
  ``contextlib.redirect_stdout`` cannot fix that here — it is process-global,
  so one login's poll loop would steal stdout out from under every other
  concurrent request this multi-threaded HTTP server is handling. What IS
  reused verbatim: every constant (``CODEX_OAUTH_CLIENT_ID``,
  ``CODEX_OAUTH_TOKEN_URL``, ``DEFAULT_CODEX_BASE_URL``,
  ``CODEX_RATE_LIMITED_CODE``), the ``_parse_retry_after_seconds`` helper,
  and the persistence step (``_save_codex_tokens``) — only the HTTP
  request/poll/exchange shape (~90 lines in ``auth.py``) is copied. The
  429-backoff on step 1 is INTENTIONALLY shorter than the CLI's (see
  ``_codex_request_device_code``'s own docstring) — the CLI can afford to
  sit in a terminal for up to ~3 minutes of backoff; a web request handler
  cannot leave a button spinning that long.

Persistence is generation-gated (review finding, see ``DeviceLoginManager``'s
own docstring): ``_finish()`` never writes tokens for a login the client has
since abandoned — neither by starting a NEW login, nor by successfully
submitting the form with a DIFFERENT credential while this one was still
polling in the background (``retire()``). Device login here ONLY ever saves
tokens (``_save_codex_tokens`` / ``_minimax_save_auth_state``) — it never
touches ``model.provider``/``active_provider``/``config.yaml`` any more.
That switch is ``apply_settings``' job at submit time (it already calls
``_update_config_for_provider`` for whatever provider the client actually
submitted) — a device login by itself is not a decision to make this
provider active, just proof an account exists that CAN be submitted.

No token material, device/user code, or credential ever gets logged by this
module (see the ``logger.info``/``logger.warning`` calls below — every one
passes only ``provider`` and a static outcome string).
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from hermes_cli.auth import (
    CODEX_OAUTH_CLIENT_ID,
    CODEX_OAUTH_TOKEN_URL,
    CODEX_RATE_LIMITED_CODE,
    DEFAULT_CODEX_BASE_URL,
    MINIMAX_OAUTH_SCOPE,
    PROVIDER_REGISTRY,
    AuthError,
    _codex_access_token_is_expiring,
    _minimax_expired_in_looks_like_unix_ms,
    _minimax_pkce_pair,
    _minimax_poll_token,
    _minimax_request_user_code,
    _minimax_resolve_token_expiry_unix,
    _minimax_save_auth_state,
    _parse_retry_after_seconds,
    _read_codex_tokens,
    _save_codex_tokens,
    get_minimax_oauth_auth_status,
)
from hermes_cli.setup_wizard.providers_view import DEVICE_CODE_PROVIDERS

logger = logging.getLogger("setup_wizard")

# Same 15-minute wall-clock cap for BOTH providers (review: minimax must not
# be allowed to poll indefinitely just because the server-issued expired_in
# says so — see _minimax_capped_expired_in_ms).
_LOGIN_TIMEOUT_SECONDS = 15 * 60

_ISSUER = "https://auth.openai.com"

_GENERIC_START_ERROR = "Не удалось начать вход. Попробуйте ещё раз."
_GENERIC_LOGIN_ERROR = "Не удалось войти. Попробуйте ещё раз."
_TIMEOUT_ERROR = "Время ожидания входа истекло. Нажмите «Войти по аккаунту» ещё раз."
_RATE_LIMITED_ERROR = "Провайдер ограничивает частоту входов — попробуйте через минуту."
_DENIED_ERROR = "Вход отклонён. Попробуйте снова."
# Owner diagnosis: auth.openai.com answers device-code requests with HTTP
# 403 when they originate from a data-center/VM IP (a WAF region block —
# from an ordinary machine the same request returns 200). AuthError itself
# carries no HTTP status (see hermes_cli.auth.AuthError — message/provider/
# code/relogin_required only), so this module's OWN duplicated request loop
# (see the module docstring on why Codex's HTTP steps are duplicated here,
# not shared with hermes_cli.auth) is the only place that still has the
# raw status code in hand. It stamps a distinct `code` on the AuthError it
# raises for a 403 — never touching upstream auth.py — so _russian_error_for
# below can tell "the provider outright rejected us" apart from every other
# non-200/429 response, which still gets the generic message.
_DEVICE_CODE_REGION_BLOCKED_CODE = "device_code_region_blocked"
_REGION_BLOCKED_ERROR = (
    "Провайдер недоступен с этой машины — похоже, блокирует регион или "
    "дата-центр. Вход по аккаунту отсюда не пройдёт; используйте способ "
    "«По API-ключу» другого провайдера или настройте прокси на сервере."
)


class _LoginSuperseded(Exception):
    """Raised internally by a poll loop when it notices — via the
    ``is_current`` callback threaded through it — that it has been
    superseded (a newer ``start()``, or a ``retire()`` from a completed
    submit) mid-flight. Caught in ``DeviceLoginManager._finish`` and
    treated as a quiet no-op: no status update, no persistence, no error
    surfaced (the client isn't looking at this login any more)."""


# ---------------------------------------------------------------------------
# OpenAI Codex — duplicated HTTP steps (see module docstring for why).
# Source: hermes_cli.auth._codex_device_code_login.
# ---------------------------------------------------------------------------


def _codex_request_device_code(proxy: str | None = None) -> dict:
    """Step 1: request a user code. Synchronous — runs directly inside the
    request handler's ``asyncio.to_thread`` call, so it must return quickly.

    The CLI's own retry loop (4 attempts, exponential backoff up to 60s
    each) can burn up to ~3 minutes on repeated 429s — fine for a terminal
    session, not fine for a browser button the owner is staring at. This
    version tries at most twice, with the second attempt's delay capped at
    5s total, then gives up with a clear "try again in a minute" message
    (see ``_russian_error_for``'s ``CODEX_RATE_LIMITED_CODE`` branch)
    instead of making the click hang.

    ``proxy`` is the wizard form's own proxy field, forwarded here for the
    same reason the region-block detection above exists in the first
    place: ``auth.openai.com`` itself can be unreachable from a RU
    data-center host, not just its inference API.
    """
    max_attempts = 2
    retry_delay_cap_seconds = 5
    resp = None
    client_kwargs: dict = {"timeout": httpx.Timeout(15.0)}
    if proxy:
        client_kwargs["proxy"] = proxy
    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.post(
                    f"{_ISSUER}/api/accounts/deviceauth/usercode",
                    json={"client_id": CODEX_OAUTH_CLIENT_ID},
                    headers={"Content-Type": "application/json"},
                )
        except Exception as exc:
            raise AuthError(
                f"Failed to request device code: {exc}",
                provider="openai-codex",
                code="device_code_request_failed",
            )

        if resp.status_code != 429:
            break
        if attempt < max_attempts:
            retry_after = _parse_retry_after_seconds(getattr(resp, "headers", None))
            delay = retry_after if retry_after is not None else 2
            delay = max(1, min(int(delay), retry_delay_cap_seconds))
            time.sleep(delay)

    if resp is not None and resp.status_code == 429:
        raise AuthError(
            "OpenAI is rate-limiting Codex login requests (HTTP 429).",
            provider="openai-codex",
            code=CODEX_RATE_LIMITED_CODE,
        )
    if resp is None or resp.status_code != 200:
        status = resp.status_code if resp is not None else "unknown"
        if status == 403:
            # Not "not approved yet" (that's the poll endpoint's 403/404
            # meaning — see _codex_poll_and_exchange) — this is the very
            # first request in the flow being rejected outright, which is
            # exactly the region/WAF-block shape diagnosed above.
            raise AuthError(
                f"Device code request returned status {status}.",
                provider="openai-codex",
                code=_DEVICE_CODE_REGION_BLOCKED_CODE,
            )
        raise AuthError(
            f"Device code request returned status {status}.",
            provider="openai-codex",
            code="device_code_request_error",
        )

    device_data = resp.json()
    user_code = device_data.get("user_code", "")
    device_auth_id = device_data.get("device_auth_id", "")
    poll_interval = max(3, int(device_data.get("interval", "5")))
    if not user_code or not device_auth_id:
        raise AuthError(
            "Device code response missing required fields.",
            provider="openai-codex",
            code="device_code_incomplete",
        )
    return {
        "user_code": user_code,
        "device_auth_id": device_auth_id,
        "poll_interval": poll_interval,
        "verification_url": f"{_ISSUER}/codex/device",
    }


def _codex_poll_and_exchange(
    device_info: dict,
    *,
    timeout_seconds: float,
    is_current: Callable[[], bool] = lambda: True,
    proxy: str | None = None,
) -> dict:
    """Steps 2-3: poll until approved (or timed out), then exchange the
    authorization code for tokens. Runs on the background login thread —
    the blocking ``time.sleep``s here are the point, not a bug.

    ``is_current`` is checked before every sleep, after every sleep, and
    before the final token exchange — this is the REAL cancellation path
    (review finding): a superseded login stops polling almost immediately
    instead of running to its own natural timeout, because this function
    owns its poll loop outright (unlike the MiniMax wrapper below, which
    hands off to an upstream-owned loop it cannot interrupt mid-iteration).

    ``proxy`` (the wizard form's proxy field, threaded through from
    ``DeviceLoginManager.start``) applies to BOTH httpx.Client blocks this
    function opens — the poll loop and the token exchange — since both
    talk to ``auth.openai.com``.
    """
    user_code = device_info["user_code"]
    device_auth_id = device_info["device_auth_id"]
    poll_interval = device_info["poll_interval"]

    client_kwargs: dict = {"timeout": httpx.Timeout(15.0)}
    if proxy:
        client_kwargs["proxy"] = proxy

    start = time.monotonic()
    code_resp = None
    with httpx.Client(**client_kwargs) as client:
        while time.monotonic() - start < timeout_seconds:
            if not is_current():
                raise _LoginSuperseded()
            time.sleep(poll_interval)
            if not is_current():
                raise _LoginSuperseded()
            poll_resp = client.post(
                f"{_ISSUER}/api/accounts/deviceauth/token",
                json={"device_auth_id": device_auth_id, "user_code": user_code},
                headers={"Content-Type": "application/json"},
            )
            if poll_resp.status_code == 200:
                code_resp = poll_resp.json()
                break
            if poll_resp.status_code in {403, 404}:
                continue  # not approved yet
            raise AuthError(
                f"Device auth polling returned status {poll_resp.status_code}.",
                provider="openai-codex",
                code="device_code_poll_error",
            )

    if code_resp is None:
        raise AuthError(
            "Login timed out.",
            provider="openai-codex",
            code="device_code_timeout",
        )

    if not is_current():
        raise _LoginSuperseded()

    authorization_code = code_resp.get("authorization_code", "")
    code_verifier = code_resp.get("code_verifier", "")
    redirect_uri = f"{_ISSUER}/deviceauth/callback"
    if not authorization_code or not code_verifier:
        raise AuthError(
            "Device auth response missing authorization_code or code_verifier.",
            provider="openai-codex",
            code="device_code_incomplete_exchange",
        )

    try:
        with httpx.Client(**client_kwargs) as client:
            token_resp = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": redirect_uri,
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:
        raise AuthError(
            f"Token exchange failed: {exc}",
            provider="openai-codex",
            code="token_exchange_failed",
        )

    if token_resp.status_code == 429:
        raise AuthError(
            "OpenAI is rate-limiting Codex login requests (HTTP 429) during token exchange.",
            provider="openai-codex",
            code=CODEX_RATE_LIMITED_CODE,
        )
    if token_resp.status_code != 200:
        raise AuthError(
            f"Token exchange returned status {token_resp.status_code}.",
            provider="openai-codex",
            code="token_exchange_error",
        )

    tokens = token_resp.json()
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    if not access_token:
        raise AuthError(
            "Token exchange did not return an access_token.",
            provider="openai-codex",
            code="token_exchange_no_access_token",
        )

    if not is_current():
        raise _LoginSuperseded()

    base_url = os.getenv("HERMES_CODEX_BASE_URL", "").strip().rstrip("/") or DEFAULT_CODEX_BASE_URL
    return {
        "tokens": {"access_token": access_token, "refresh_token": refresh_token},
        "base_url": base_url,
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------------------
# MiniMax OAuth — thin wrapper over hermes_cli.auth's own client-parametrized
# helpers. Region is fixed at "global": the wizard's api-key path already
# covers China (see the "minimax-cn" catalog variant) — an OAuth region
# picker is out of scope for this pass.
# ---------------------------------------------------------------------------


def _minimax_capped_expired_in_ms(expired_in: int, *, timeout_seconds: float) -> int:
    """Clamp the server-issued device-code ``expired_in`` to at most
    ``timeout_seconds`` from now, in whichever representation (absolute
    unix-ms, or a relative seconds duration) ``_minimax_poll_token`` itself
    would have interpreted the ORIGINAL value as — reusing its own
    classifier (``_minimax_expired_in_looks_like_unix_ms``, imported, not
    modified) rather than guessing. Always returns an absolute unix-ms
    timestamp, which the classifier will always read back as "looks like
    unix ms" (any real near-future timestamp is far larger than
    ``now_ms // 2``), so the cap is unambiguous regardless of what shape
    the server originally sent.

    Review finding: without this, MiniMax's poll loop could run for
    however long the server's ``expired_in`` says (observed up to the
    codex flow's own 15-minute cap, but not GUARANTEED to be bounded by
    it) — Codex already has an explicit client-side ceiling
    (``timeout_seconds`` threaded through ``_codex_poll_and_exchange``);
    MiniMax needs the same one.
    """
    now = time.time()
    now_ms = int(now * 1000)
    raw = int(expired_in)
    if _minimax_expired_in_looks_like_unix_ms(raw, now_ms=now_ms):
        original_deadline = raw / 1000.0
    else:
        original_deadline = now + max(1, raw)
    capped_deadline = min(original_deadline, now + timeout_seconds)
    return int(capped_deadline * 1000)


def _minimax_start_device_code(proxy: str | None = None) -> dict:
    """Step 1: request a user code, and open the ONE ``httpx.Client`` this
    login's request+poll share (see module docstring). The client is
    intentionally NOT closed here — ownership passes to
    ``_minimax_finish_device_code`` via the returned dict's ``"client"``
    key, which closes it in a ``finally`` regardless of outcome.

    ``proxy``, when set, covers both steps automatically — they share this
    one client instance rather than each opening their own.
    """
    pconfig = PROVIDER_REGISTRY["minimax-oauth"]
    verifier, challenge, state = _minimax_pkce_pair()
    client_kwargs: dict = {
        "timeout": httpx.Timeout(15.0),
        "headers": {"Accept": "application/json"},
        "follow_redirects": True,
    }
    if proxy:
        client_kwargs["proxy"] = proxy
    client = httpx.Client(**client_kwargs)
    try:
        code_data = _minimax_request_user_code(
            client,
            portal_base_url=pconfig.portal_base_url,
            client_id=pconfig.client_id,
            code_challenge=challenge,
            state=state,
        )
    except Exception:
        client.close()
        raise
    interval_raw = code_data.get("interval")
    return {
        "client": client,
        "verification_url": str(code_data["verification_uri"]),
        "user_code": str(code_data["user_code"]),
        "code_verifier": verifier,
        "expired_in": int(code_data["expired_in"]),
        "interval_ms": int(interval_raw) if interval_raw is not None else None,
        "portal_base_url": pconfig.portal_base_url,
        "inference_base_url": pconfig.inference_base_url,
        "client_id": pconfig.client_id,
    }


def _minimax_finish_device_code(info: dict, *, is_current: Callable[[], bool] = lambda: True) -> dict:
    """Steps 2-3: poll for approval on the SAME client ``start()`` opened,
    then shape the auth-state dict for the caller to persist under the
    generation lock (this function itself never calls
    ``_minimax_save_auth_state`` — see ``DeviceLoginManager._finish``).

    ``is_current`` is checked before and after the poll call — NOT between
    its internal iterations, because ``_minimax_poll_token`` (upstream,
    not modified) owns that inner loop outright and offers no hook to
    interrupt it mid-iteration. A superseded MiniMax login therefore keeps
    polling in the background for up to its own capped deadline before
    this function notices — the important guarantee (review finding) is
    that its result is never written to disk once superseded, not that
    the HTTP polling stops instantly the way Codex's does.
    """
    client = info["client"]
    try:
        if not is_current():
            raise _LoginSuperseded()
        capped_expired_in = _minimax_capped_expired_in_ms(
            info["expired_in"], timeout_seconds=_LOGIN_TIMEOUT_SECONDS
        )
        token_data = _minimax_poll_token(
            client,
            portal_base_url=info["portal_base_url"],
            client_id=info["client_id"],
            user_code=info["user_code"],
            code_verifier=info["code_verifier"],
            expired_in=capped_expired_in,
            interval_ms=info["interval_ms"],
        )
        if not is_current():
            raise _LoginSuperseded()
    finally:
        client.close()

    now = datetime.now(timezone.utc)
    expires_at_unix = _minimax_resolve_token_expiry_unix(int(token_data["expired_in"]), now=now)
    expires_in_s = max(0, int(expires_at_unix - now.timestamp()))
    auth_state = {
        "provider": "minimax-oauth",
        "region": "global",
        "portal_base_url": info["portal_base_url"],
        "inference_base_url": info["inference_base_url"],
        "client_id": info["client_id"],
        "scope": MINIMAX_OAUTH_SCOPE,
        "token_type": token_data.get("token_type", "Bearer"),
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "resource_url": token_data.get("resource_url"),
        "obtained_at": now.isoformat(),
        "expires_at": datetime.fromtimestamp(expires_at_unix, tz=timezone.utc).isoformat(),
        "expires_in": expires_in_s,
    }
    return {"auth_state": auth_state}


def _russian_error_for(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code in {"device_code_timeout", "timeout"}:
        return _TIMEOUT_ERROR
    if code == CODEX_RATE_LIMITED_CODE:
        return _RATE_LIMITED_ERROR
    if code == "authorization_denied":
        return _DENIED_ERROR
    if code == _DEVICE_CODE_REGION_BLOCKED_CODE:
        return _REGION_BLOCKED_ERROR
    return _GENERIC_LOGIN_ERROR


def device_login_is_valid(provider_name: str) -> bool:
    """True when ``provider_name`` is a device-code provider that ALREADY
    has a live, refreshable credential in the Hermes auth store right now.

    The ONLY caller is ``app.py``'s submit validation (the final "did the
    account actually log in" gate before apply/restart/liveness run — see
    that module's ``_run_submit``). This is a network-capable, refresh-if-
    needed check — appropriate for a one-shot explicit submit action, but
    NOT for anything that runs on every page load (that's
    ``device_login_looks_active`` below — read its docstring for why the
    distinction matters). Never starts a login itself —
    ``DeviceLoginManager.start()`` is the only thing that does that. Does
    NOT depend on ``auth.json``'s ``active_provider`` field (verified by
    reading ``resolve_codex_runtime_credentials`` /
    ``resolve_minimax_oauth_runtime_credentials``, both of which resolve
    by the literal provider id, not "whichever provider is active") — this
    is why it still works even now that a successful device login no
    longer sets ``active_provider`` itself (that's ``apply_settings``' job
    at submit time).
    """
    if provider_name not in DEVICE_CODE_PROVIDERS:
        return False
    try:
        if provider_name == "openai-codex":
            from hermes_cli.auth import resolve_codex_runtime_credentials

            creds = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        else:  # minimax-oauth
            from hermes_cli.auth import resolve_minimax_oauth_runtime_credentials

            creds = resolve_minimax_oauth_runtime_credentials()
    except Exception:
        return False
    return bool(creds.get("api_key"))


def _codex_login_looks_active() -> bool:
    try:
        data = _read_codex_tokens()
    except Exception:
        return False
    access_token = (data.get("tokens") or {}).get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return False
    return not _codex_access_token_is_expiring(access_token, 60)


def device_login_looks_active(provider_name: str) -> bool:
    """Cheap, NETWORK-FREE check for ``/api/form``'s prefill ONLY (review
    finding) — reads the auth store directly and checks expiry via a pure
    JWT decode (Codex, ``_codex_access_token_is_expiring`` — no network) or
    the stored ``expires_at`` timestamp (MiniMax,
    ``get_minimax_oauth_auth_status`` — also a pure store read). Never
    attempts a refresh.

    Deliberately separate from ``device_login_is_valid`` above: that one
    DOES refresh over the network when a token is close to expiring, which
    is correct for an explicit one-shot submit but wrong for something
    that runs on every single ``GET /api/form`` (e.g. every page reload,
    every ``/api/form`` poll) — repeatedly hitting the refresh endpoint for
    a token that isn't actually being used yet risks tripping MiniMax's own
    quarantine-on-repeated-refresh-failure logic
    (``_minimax_oauth_quarantine_on_terminal_refresh``) for no operational
    reason at all.
    """
    if provider_name == "openai-codex":
        return _codex_login_looks_active()
    if provider_name == "minimax-oauth":
        return bool(get_minimax_oauth_auth_status().get("logged_in"))
    return False


class DeviceLoginManager:
    """One active device-code login per process (spec: "один активный на
    процесс"). Lives on ``app.state.device_login`` — a fresh instance per
    ``create_app()`` call, same isolation pattern as ``app.state.sessions``.

    ``start()`` performs step 1 (request a code) itself, synchronously —
    it is a single fast HTTP call — and returns the URL + code to show the
    client immediately. Steps 2-3 (poll + exchange, which can take up to
    the flow's own timeout) run on a daemon background thread.

    Cancellation ("отмена при новом start") is a generation counter:
    ``start()`` and ``retire()`` both bump ``self._generation`` under
    ``self._lock``, and EVERY write this module can make —
    ``_save_codex_tokens`` / ``_minimax_save_auth_state`` — happens ONLY
    inside a block that re-checks the generation under that SAME lock
    immediately before writing (review finding: the previous version only
    gated the in-memory ``status()`` result this way, not the actual disk
    writes — a stale finish could still persist tokens for a login the
    client had already moved past, minutes later, silently overwriting
    whatever the client had just submitted and applied). Two distinct
    triggers move the generation forward:

    - A NEW ``start()`` — the client picked a different provider/variant,
      or clicked "Войти по аккаунту" again. The OLD thread is not killed;
      for Codex it notices via the ``is_current`` callback threaded
      through its own poll loop and exits almost immediately (real
      cancellation — see ``_codex_poll_and_exchange``); for MiniMax it
      keeps polling inside the upstream-owned loop it cannot interrupt,
      but its eventual result is discarded once superseded either way.
    - ``retire()`` — called by ``app.py``'s ``_run_submit`` right after a
      successful ``apply_settings()``, BEFORE the gateway restart. Closes
      the exact bug this whole mechanism exists for: the client logs in,
      the login is still polling, the client switches to a different
      provider (or types an API key) and submits THAT successfully — the
      original login must never be allowed to land afterward just because
      its own device code happens to get approved a few minutes later.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._status: dict[str, Any] = {
            "state": "idle",
            "error": None,
            "provider": None,
            "login_id": None,
        }

    def start(self, provider: str, proxy: str | None = None) -> dict:
        """``proxy`` is the wizard form's own proxy field (owner
        requirement: RU-hosted machines often can't reach a provider's
        OAuth endpoint at all without one — the same field that gates
        Telegram/API-key reachability). Threaded through to both the
        synchronous step 1 below AND the background poll/exchange step
        (see ``_finish``) — every ``httpx.Client`` this login opens gets
        it.
        """
        if provider not in DEVICE_CODE_PROVIDERS:
            raise ValueError(f"unsupported device-code provider: {provider}")

        with self._lock:
            self._generation += 1
            my_generation = self._generation
            self._status = {"state": "pending", "error": None, "provider": provider, "login_id": None}

        try:
            if provider == "openai-codex":
                device_info = _codex_request_device_code(proxy)
            else:
                device_info = _minimax_start_device_code(proxy)
        except Exception as exc:
            logger.warning("device login start failed: provider=%s", provider, exc_info=True)
            message = _russian_error_for(exc) if isinstance(exc, AuthError) else _GENERIC_START_ERROR
            with self._lock:
                if my_generation == self._generation:
                    self._status = {
                        "state": "error",
                        "error": message,
                        "provider": provider,
                        "login_id": None,
                    }
            raise

        login_id = secrets.token_urlsafe(16)
        with self._lock:
            if my_generation == self._generation:
                self._status["login_id"] = login_id

        thread = threading.Thread(
            target=self._finish,
            args=(my_generation, provider, device_info, login_id, proxy),
            daemon=True,
            name=f"wizard-device-login-{provider}",
        )
        thread.start()
        logger.info("device login started: provider=%s", provider)
        return {
            "login_id": login_id,
            "verification_url": device_info["verification_url"],
            "user_code": device_info["user_code"],
        }

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def retire(self) -> None:
        """Bump the generation without starting a new login. Called from
        ``app.py``'s ``_run_submit`` right after a successful apply — see
        this class's own docstring for the exact bug this closes."""
        with self._lock:
            self._generation += 1

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _finish(
        self, generation: int, provider: str, device_info: dict, login_id: str, proxy: str | None = None
    ) -> None:
        def is_current() -> bool:
            return self._is_current(generation)

        try:
            if provider == "openai-codex":
                creds = _codex_poll_and_exchange(
                    device_info,
                    timeout_seconds=_LOGIN_TIMEOUT_SECONDS,
                    is_current=is_current,
                    proxy=proxy,
                )
            else:
                # MiniMax's client already carries the proxy from
                # _minimax_start_device_code (see that function's own
                # docstring — start() and finish() share ONE client) —
                # nothing to thread through here.
                creds = _minimax_finish_device_code(device_info, is_current=is_current)
        except _LoginSuperseded:
            logger.info("device login superseded, discarding: provider=%s", provider)
            return
        except Exception as exc:
            logger.warning("device login failed: provider=%s", provider, exc_info=True)
            with self._lock:
                if generation == self._generation:
                    self._status = {
                        "state": "error",
                        "error": _russian_error_for(exc),
                        "provider": provider,
                        "login_id": login_id,
                    }
            return

        # Persistence itself — not just the status update — is gated on the
        # generation, checked atomically under the same lock that start()/
        # retire() use to bump it. This is the fix for the review finding:
        # a stale finish must never write tokens for a login the client has
        # already moved past.
        with self._lock:
            if generation != self._generation:
                logger.info("device login result discarded (superseded): provider=%s", provider)
                return
            if provider == "openai-codex":
                _save_codex_tokens(creds["tokens"], creds.get("last_refresh"))
            else:
                _minimax_save_auth_state(creds["auth_state"])
            self._status = {"state": "ok", "error": None, "provider": provider, "login_id": login_id}

        logger.info("device login succeeded: provider=%s", provider)
