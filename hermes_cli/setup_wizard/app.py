"""Setup-wizard FastAPI application (spec 8, §8.3): HTTP Basic
authentication, the access log, the closed-state gate, and the form-data
endpoints.

Implements the permanent-access redesign
(``docs/product/specs/2026-08-25-trix-agent-wizard-permanent-access-
design.md``, §5/§8.3): the old password-form + cookie-session model is
gone entirely. The browser's own native Basic-auth dialog gates every
route (``_BasicAuthMiddleware`` below) — until it succeeds, no route
handler ever runs and no byte of this app's HTML/JSON is sent (spec
§14.11). There is no session: the browser resends ``Authorization`` on
every request, so ``WizardState.verify()`` (scrypt) would otherwise run
on every single request a loaded page makes; ``app.state.auth_cache``
closes that gap by caching a successful verification, in process memory
only, for a short TTL (§8.3.2). Every login attempt — success or
failure — is appended to the access log (§8.2, ``_log_access``).

Owner ruling 2026-08-24 ("Установка инструментов — кнопки нет" — see
docs/product/specs/2026-08-23-wizard-content-decisions.md): there used to
be a standalone ``/api/install`` route the "изменить" tool block's own
"Установить" button called. It's gone — the console wizard
(``hermes_cli/tools_config.py``) never had a manual install step either;
installing a chosen tool is just part of applying the form. ``/api/submit``
now runs the install hooks for whichever catalog rows this submission's
own choices select (see ``_pending_tool_installs``/``_run_submit`` below)
as its own stage, between ``apply_settings`` and ``restart_gateway`` — a
tool has to be on disk before the agent that will use it restarts.

Security posture (deviation from spec §12.5 of spec 6, still in force
under spec 8): instead of validating the ``Host`` header against a fixed
allowlist (the client reaches this server by bare IP; there is no other
hostname to check against), every mutating ``/api/*`` request is checked
against an Origin guard: if the client sends an ``Origin`` header, it
must parse as ``scheme://host`` and both scheme and host must match the
request's own scheme/``Host`` header, or the request is rejected with
403. An ``Origin`` header that is present but unparsable (the literal
``"null"`` browsers send for sandboxed iframes/``data:``/some redirect
chains, or anything else without a well-formed ``scheme://host`` shape) is
treated as untrusted and rejected the same way — fail closed, not fail
open. A request with **no** ``Origin`` header at all still passes (most
non-browser clients never send one). This guard is still required under
Basic auth (spec §8.3 point 4, not optional): a browser attaches
``Authorization: Basic ...`` to every request to this origin automatically,
including one a foreign page's script or form triggers — there is no
cookie attribute (no ``SameSite``) that can suppress that the way it does
for a session cookie, so the Origin check is the only thing standing
between a logged-in browser and a cross-site-forged mutating request.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import hashlib
import json
import logging
import os
import threading
import time
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

import providers
from hermes_constants import get_hermes_home, secure_parent_dir
from hermes_cli import trix_support
from hermes_cli.config import get_env_value, load_config
from hermes_cli.models import provider_group_for_slug
from hermes_cli.trix_search_chain import primary_backend as _primary_backend
from hermes_cli.setup_wizard.apply import apply_settings
from hermes_cli.setup_wizard.device_login import (
    DeviceLoginManager,
    device_login_is_valid,
    device_login_looks_active,
)
from hermes_cli.setup_wizard.gateway_ctl import (
    restart_gateway,
    wait_bot_alive,
)
from hermes_cli.setup_wizard.page import render_page
from hermes_cli.setup_wizard.providers_view import (
    DEVICE_CODE_PROVIDERS,
    fetch_live_models,
    wizard_provider_groups,
    wizard_providers,
)
from hermes_cli.setup_wizard.state import WizardState
from hermes_cli.setup_wizard.support_view import register_support_routes
from hermes_cli.setup_wizard.timezones import zone_groups
from hermes_cli.setup_wizard.tools_view import (
    run_tool_install,
    wizard_tool_blocks,
)
from hermes_cli.setup_wizard.validate import (
    check_allowed_users,
    check_provider_key,
    check_proxy_syntax,
    check_reachability,
    check_telegram_token,
    check_timezone,
    check_telegram_user,
)

logger = logging.getLogger("setup_wizard")

_CLOSED_MESSAGE = (
    "Мастер настройки отключён. Чтобы включить его снова, нужен доступ "
    "к самой машине (SSH или консоль хостинга)."
)

# HTTP methods that mutate state — the Origin guard only applies to these.
# GET/HEAD/OPTIONS never mutate, so there is nothing for a forged cross-site
# request to achieve by hitting them.
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# ---- HTTP Basic auth (spec §8.3) ------------------------------------------

# Realm string sent in WWW-Authenticate. Kept short and Latin-only (spec
# §8.3.5) — browser support for a non-ASCII realm is unreliable; the
# Russian explanation lives in the 401 response BODY instead
# (_UNAUTHORIZED_BODY below), not in this header.
_BASIC_REALM = "Trix Setup"

# In-memory cache of recently-verified Authorization header values (spec
# §8.3.2) — avoids paying the scrypt-on-16MiB cost of WizardState.verify()
# on every single request a loaded page makes (it fires off roughly a
# dozen on load, plus periodic status polling). Neither number is
# spec-mandated ("короткий TTL, ограниченный размером" — spec leaves the
# exact values to the implementation): 30s comfortably covers one page
# load's request burst without leaving a revoked/rotated credential
# usable for long, and 256 entries is far more concurrent distinct
# Authorization values than a single admin session (or a noisy scanner)
# realistically produces, while still bounding memory.
_AUTH_CACHE_TTL_SECONDS = 30.0
_AUTH_CACHE_MAX_ENTRIES = 256

# Deliberate decision, not an oversight: rotating the primary credential
# (``hermes setup-wizard set-password``, spec §7 — "письмо утекло") does
# NOT invalidate this cache. A client already holding a cached hit keeps
# working on the OLD password for up to ``_AUTH_CACHE_TTL_SECONDS`` after
# rotation, and the spec explicitly tolerates that window (§7: the whole
# point of a TTL-bounded cache is that it can be briefly stale).
#
# Why cheap invalidation isn't actually cheap here: ``set-password`` is a
# separate CLI invocation (a distinct OS process, run over SSH/console —
# spec §9.3) from the long-running ``serve`` process that owns this cache
# in its own memory (``app.state.auth_cache``). There is no existing IPC
# channel between the two, and the cache's entire reason to exist is
# skipping a ``state.json`` read on every cache HIT (see the comment
# above) — the only way to make a hit "generation-aware" would be to read
# something off disk (a mtime, a rotation counter) on every single hit
# anyway, which just re-introduces the per-request I/O this cache exists
# to avoid. Building a real invalidation channel (a signal, a socket, a
# lock file poked by set-password) is real, ongoing surface for a window
# the spec already accepts as fine — not worth it for 30 seconds.

# access.log rotation cap (§14.17: "не растёт неограниченно... ротация
# или потолок размера"). Not spec-mandated — chosen generously above what
# 8443 realistically sees; a single rotation (current file -> .1, dropping
# whatever .1 held before) is enough to bound disk use on a port that's
# open indefinitely.
_ACCESS_LOG_MAX_BYTES = 5 * 1024 * 1024

# Placeholders (spec §6/§8.3.5: "в коде это строка каталога локализации,
# не логика") — the real subject/sender are XDataPlus's to set once the
# VMmanager mail template (spec §10) is finalized; not this repo's call
# (open question §12.1). Deliberately NOT a plausible-looking made-up
# subject/sender: a client who reads this after clicking "Cancel" and then
# searches their mailbox for a fabricated string ("Доступ к панели настроек
# Trix Agent" from "noreply@xdataplus.ru") finds nothing, because no such
# email was ever promised to exist.
#
# The earlier shape of this was a bracketed placeholder ("[тема письма —
# уточняется у XDataPlus]") kept byte-identical with the `/setup` Telegram
# reply. That was honest but read to the client as an unfinished sentence.
# Spec 12 Task 7 replaced it on both surfaces with wording that names
# neither subject nor sender and points at a search that actually works —
# honest AND finished. The two surfaces still have to agree; the guard is
# tests/hermes_cli/test_setup_wizard_app_auth.py.

# Body for both "no Authorization header" and "wrong login/password" (spec
# §14.5: identical code/body/header for either — never lets a client
# distinguish "unknown login" from "wrong password" for a known one).
# Deliberately carries nothing from WizardState (§14.12) — no bot name, no
# IP, no completed/disabled flag, nothing config-derived.
_UNAUTHORIZED_BODY = (
    "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
    "<title>Trix Agent — вход</title></head><body>"
    "<h1>Настройки агента Trix</h1>"
    "<p>Это панель настройки Trix Agent на вашей машине. Браузер запросит "
    "логин и пароль для входа.</p>"
    "<p>Логин и пароль — в письме, которое пришло в день создания машины "
    "на почту, указанную при заказе. Поищите в почте по слову «Trix» — "
    "письмо приходит один раз, других копий пароля нет.</p>"
    "<p><a href=\"/\">Попробовать ещё раз</a></p>"
    "</body></html>"
)


def _unauthorized_response() -> HTMLResponse:
    return HTMLResponse(
        status_code=401,
        content=_UNAUTHORIZED_BODY,
        headers={"WWW-Authenticate": f'Basic realm="{_BASIC_REALM}"'},
    )


def _format_wait_seconds(seconds: int) -> str:
    """Russian, grammar-number-agreement-free wait text ("62 сек."/"15
    мин.") — abbreviated units sidestep having to pick the right plural
    form of "секунда"/"минута" for an arbitrary count."""
    if seconds < 60:
        return f"{seconds} сек."
    return f"{(seconds + 59) // 60} мин."


def _rate_limited_body(retry_after_seconds: int) -> str:
    """Body for a locked-out IP (§8.3.6) — no WWW-Authenticate on this
    response (see ``_BasicAuthMiddleware``): with it, a browser holding a
    correct password would just re-prompt forever with no visible reason,
    which is the exact dead-end owner ruling 2026-08-25 closed off."""
    wait_text = _format_wait_seconds(retry_after_seconds)
    return (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<title>Trix Agent — слишком много попыток</title></head><body>"
        "<h1>Слишком много неудачных попыток входа</h1>"
        f"<p>Подождите {wait_text} и попробуйте снова.</p>"
        "<p><a href=\"/\">Попробовать ещё раз</a></p>"
        "</body></html>"
    )


def _client_ip(request: Request) -> str:
    """The IP the TCP connection actually came from — **only**
    ``request.client.host`` (spec §8.1). ``X-Forwarded-For``/``X-Real-IP``
    are never read here or anywhere downstream: the wizard has no reverse
    proxy in front of it, so those headers are attacker-controlled — using
    them would let a client dodge its own lockout and pollute
    ``failures_by_ip`` with spoofed keys."""
    client = request.client
    return client.host if client is not None else "unknown"


def _parse_basic_auth(header_value: str) -> tuple[str, str] | None:
    """Parse an ``Authorization: Basic <base64>`` header value into
    ``(login, password)``, or ``None`` for anything not well-formed:
    wrong scheme, unparsable base64, undecodable UTF-8, or no ``:``
    separator in the decoded value.

    A malformed value is treated the same as a missing header (401 +
    WWW-Authenticate, no lockout accounting) rather than as a failed
    credential guess — there is no actual login/password pair to check,
    so it must not count as a "wrong password" attempt against a real
    account.
    """
    if not header_value:
        return None
    scheme, _, encoded = header_value.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, binascii.Error):
        return None
    login, sep, password = decoded.partition(":")
    if not sep:
        return None
    return login, password


def _auth_cache_key(header_value: str) -> str:
    """Cache key for the success cache (§8.3.2) — a digest of the raw
    ``Authorization`` header value, never the header itself (avoids
    holding a plaintext-recoverable copy of the credential in the cache
    dict any longer than the header itself already sits in memory for
    this request)."""
    return hashlib.sha256(header_value.encode("utf-8", errors="surrogateescape")).hexdigest()


def _auth_cache_get(app: FastAPI, key: str, now: float) -> bool:
    with app.state.auth_cache_lock:
        expiry = app.state.auth_cache.get(key)
        if expiry is None:
            return False
        if expiry <= now:
            app.state.auth_cache.pop(key, None)
            return False
        return True


def _auth_cache_put(app: FastAPI, key: str, now: float) -> None:
    with app.state.auth_cache_lock:
        cache = app.state.auth_cache
        cache[key] = now + _AUTH_CACHE_TTL_SECONDS
        if len(cache) > _AUTH_CACHE_MAX_ENTRIES:
            # Bounded size (§8.3.2) — evict whichever entry expires
            # soonest. TTLs are assigned in insertion order (a fixed
            # offset from `now`), so "expires soonest" is equivalent to
            # "oldest", without needing a separate insertion-order
            # structure.
            oldest_key = min(cache, key=cache.get)
            cache.pop(oldest_key, None)


# ---- access log (spec §8.2) ------------------------------------------------


_access_log_lock = threading.Lock()


def _access_log_path():
    return get_hermes_home() / "setup-wizard" / "access.log"


def _rotate_access_log_if_needed(path) -> None:
    """Single-backup rotation (§14.17: "ротация или потолок размера") —
    not spec-mandated in shape, just bounded. Best-effort: a failure here
    must never block writing the access line that triggered the check."""
    try:
        if not path.exists() or path.stat().st_size < _ACCESS_LOG_MAX_BYTES:
            return
        backup = path.with_name(path.name + ".1")
        try:
            if backup.exists():
                backup.unlink()
        except OSError:
            pass
        path.rename(backup)
    except OSError:
        logger.warning("access log rotation failed", exc_info=True)


def _log_access(ip: str, login: str, verdict: str, reason: str = "") -> None:
    """Append one line to ``access.log`` (§8.2): time, IP, login, verdict,
    failure reason. The password is **never** written here, in any form —
    not even hashed or truncated (§14.6). Best-effort: logging must never
    break the request it's logging (§8.2 is diagnostic, not load-bearing).
    """
    path = _access_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        secure_parent_dir(path)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Tabs and BOTH newline characters are stripped from the untrusted
        # fields (login is client-supplied, taken straight out of the
        # Basic-auth header before any credential check) so a crafted login
        # can't inject a fake extra column OR a fake extra line into the
        # log. `\n` alone used to be enough to write a genuine line break
        # into a Unix-style log file, but `\r` on its own is just as capable
        # of forging a bogus row: a login of ``real\rFAKE-LOGIN\tgranted``
        # renders as two convincing lines in a terminal or any \r\n-naive
        # viewer even though it is a single ``os.write`` of one `\n`-
        # terminated record.
        safe_login = login.replace("\t", " ").replace("\n", " ").replace("\r", " ")
        line = f"{ts}\t{ip}\t{safe_login}\t{verdict}\t{reason}\n"
        with _access_log_lock:
            _rotate_access_log_if_needed(path)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.chmod(str(path), 0o600)
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
    except OSError:
        logger.warning("access log write failed", exc_info=True)


class _ModelsBody(BaseModel):
    provider: str
    api_key: str = ""
    base_url: str = ""
    proxy: str = ""


class _DeviceStartBody(BaseModel):
    provider: str
    proxy: str = ""


class _CheckTelegramBody(BaseModel):
    token: str
    proxy: str = ""


class _CheckTelegramUserBody(BaseModel):
    token: str
    user_id: str
    proxy: str = ""


class _CheckProxyBody(BaseModel):
    proxy: str


class _CheckKeyBody(BaseModel):
    env_var: str | None = None
    value: str = ""
    proxy: str = ""


class _SubmitBody(BaseModel):
    """Mirrors ``apply_settings``'s ``form`` shape (its own docstring, and
    task-7-brief's ``FORM``): every field optional, an empty/missing value
    is a no-op rather than "clear this setting". ``provider``/``fallback``/
    ``hass`` are accepted as loose dicts rather than nested models — the
    downstream validate/apply functions already tolerate missing keys, and
    a stricter schema here would just duplicate that leniency in a second
    place.

    Finding 5/7 (owner-approved fix): a handful of fields also accept an
    explicit JSON ``null`` as a THIRD, distinct "clear this setting" signal
    — separate from "omit the field"/"send the empty default", which stays
    a no-op for backward compatibility. This only works because each of
    those fields' NO-OP default is something other than ``None`` (``""``
    for a string, ``{}`` for a dict) — ``None`` therefore can only ever
    reach ``apply_settings`` when the client deliberately sent it. See
    ``camofox_url``/``hass``/``extract_backend`` below, and — nested one
    level deeper — the
    per-category entries of ``tool_provider`` (a bare, untyped ``dict``:
    pydantic never strips a key the client actually included, so
    ``{"video_gen": null}`` and "video_gen omitted entirely" stay
    distinguishable through ``.items()`` without any extra machinery).

    ``tts_voice`` deliberately does NOT participate in this null-signal
    club (Finding 2, owner-approved fix, reversed from an earlier
    design) — see apply.py's own docstring for why deleting
    ``tts.edge.voice`` is unsafe (``DEFAULT_CONFIG``'s baseline voice is
    English). It stays a plain ``str`` no-op field: the client sends the
    literal default voice name explicitly for a deliberate return-to-
    default pick.
    """

    telegram_token: str = ""
    allowed_users: str = ""
    proxy: str = ""
    # Часовой пояс (спека 11). Обязателен на первой установке; пустое
    # значение остаётся законным ровно тогда, когда ответ уже сохранён в
    # config.yaml — тот же контракт, что у telegram_token, и по той же
    # причине: возвратный клиент, правящий один прокси, не должен
    # переотвечать на всё подряд.
    timezone: str = ""
    provider: dict = Field(default_factory=dict)
    fallback: dict | None = None
    search_backend: str = ""
    # Generalized "key of the chosen web row" — replaces the old
    # firecrawl-only ``firecrawl_key`` field. ``{"key": env_var, "value":
    # ...}`` when the client has something to write for the chosen search
    # backend (BRAVE_SEARCH_API_KEY, EXA_API_KEY, TAVILY_API_KEY,
    # PARALLEL_API_KEY, FIRECRAWL_API_KEY, SEARXNG_URL, FIRECRAWL_API_URL,
    # ...), ``None`` for "leave it alone" (return-mode no-op, same
    # contract as every other optional field here). ``key`` is validated
    # in ``_run_submit`` against the live "web" catalog's own env vars —
    # never trusted as an arbitrary env var name from the client.
    search_env: dict | None = None
    # extract_backend/extract_env are search_backend/search_env's siblings
    # for the SEPARATE "web_extract" ("Чтение страниц") wizard block — the
    # runtime resolves search and extract independently
    # (web.search_backend / web.extract_backend, see
    # tools/web_tools.py::_get_capability_backend()), and not every
    # provider can extract (search-only: ddgs, brave-free, searxng, xai —
    # see tools_view.py's module docstring on the "web" split). ``key`` is
    # validated in ``_run_submit`` against the live "web_extract"
    # catalog's own env vars — same closed-catalog discipline as
    # ``search_env``.
    #
    # Finding 1 (review 2026-08-26, owner-approved fix): declared
    # ``str | None = ""``, NOT the bare ``str = ""`` it started as —
    # ``extractBackendChoiceValue()`` on the client sends an explicit
    # JSON ``null`` as the finding-5/7 clear signal (same contract as
    # ``camofox_url`` below) the moment a backend was actually saved and
    # the client deliberately re-picks "Выключено". A bare ``str`` field
    # can never represent that: pydantic rejects a JSON ``null`` against
    # ``str`` outright (422 ``string_type``, a raw pydantic shape
    # ``errorsFromResponseBody()`` doesn't map to any field or step —
    # the client saw an address-less "Неверное значение."), and even if
    # it had been coerced through, apply.py's old ``if extract_backend:``
    # check silently no-ops on ``None`` too. ``str | None`` makes the
    # THIRD state representable the same way ``camofox_url`` already
    # does; apply_settings() (see its own docstring) now acts on it.
    extract_backend: str | None = ""
    extract_env: dict | None = None
    browser_backend: str = ""
    # ``""``/omitted: no-op. A real name: write — including the client
    # deliberately sending back the literal default voice name (Finding 2,
    # owner-approved fix) when "Голос Светлана" is picked over a saved
    # custom voice; apply.py just writes whatever non-empty string this
    # carries, same as any other plain field.
    tts_voice: str = ""
    # ``{}``/omitted: no-op (return-mode — leave whatever HASS_TOKEN/
    # HASS_URL are saved untouched). A real ``{"url": ..., "token": ...}``:
    # write. Explicit ``null``: clear signal (finding 5/7) — remove both
    # secrets and the "homeassistant" toolset. The default is a factory
    # ``dict`` (NOT ``None``) specifically so an omitted field can never be
    # confused with an explicit ``null`` — see this class's own docstring.
    hass: dict | None = Field(default_factory=dict)
    # ``""``/omitted: no-op. A real URL: write (activates Camofox — see
    # apply.py's own docstring for why CAMOFOX_URL, not browser.backend, is
    # the actual on/off switch). Explicit ``null``: clear signal — the ONE
    # way a client picking "Chromium (встроенный)" can actually turn
    # Camofox off (finding 5).
    camofox_url: str | None = ""
    # Generalized ``search_env`` for every OTHER provider-select category
    # (tts/image_gen/video_gen/x_search — see tools_view.py's module
    # docstring): a list of ``{"key": env_var, "value": ...}`` pairs, at
    # most one per category. Every ``key`` is validated in ``_run_submit``
    # against the live "изменить" catalog's own env vars — same
    # closed-catalog discipline as ``search_env``, never trusted as an
    # arbitrary env var name from the client.
    tool_env: list[dict] | None = None
    # ``{"tts": provider_key, "image_gen": provider_key, "video_gen":
    # provider_key}`` — which row is ACTIVE for a category that needs a
    # config field to disambiguate (see tools_view.py's ``provider_key``).
    # Every value is validated in ``_run_submit`` against the live
    # catalog's own rendered ``provider_key`` values for that category.
    # An entry's value of ``null`` is the finding-5/7 clear signal for
    # THAT category (including ``x_search``, which has no config field of
    # its own to disambiguate — its only use of this dict is the clear
    # signal, see apply.py's own docstring): remove
    # ``"<category>.provider"`` from config.yaml and the category's
    # toolset from ``platform_toolsets.telegram``. It deliberately does
    # NOT delete any ``.env`` key (Finding 1/4, owner-approved fix,
    # reversed from an earlier design — see apply.py's own docstring for
    # why). A category key omitted from the dict entirely is the ordinary
    # no-op — dict values are never stripped by pydantic, so
    # presence/absence survives into ``.items()`` without any extra
    # machinery (see this class's own docstring).
    tool_provider: dict | None = None


def _legal_provider_names() -> set[str]:
    """Provider ``name`` values the wizard's own catalog actually renders.

    ``wizard_providers()`` is cheap (no subprocess — it just walks
    ``providers.list_providers()``), so this is recomputed per request
    rather than cached; the point isn't performance, it's closing off
    ``EXCLUDED_PROVIDERS`` (``nous`` included — spec §2's product rule) as a
    value a form POST can name.
    """
    return {p["name"] for p in wizard_providers()}


def _reachability_providers_by_group(reachability: dict) -> dict[str, bool]:
    """Re-key ``check_reachability()``'s ``via_proxy``/``direct`` dicts
    (keyed by provider SLUG — ``openai-api``, ``gemini``, …) onto the
    wizard's own catalog GROUP id (``wizard_provider_groups()``'s own
    ``group_id``) — spec A4: "провайдер -> достижим" must be expressed in
    the SAME identity the client already renders step-4's provider list
    under, never a host name it would have to hardcode a second time.

    ``provider_group_for_slug`` is the exact resolver
    ``wizard_provider_groups()`` itself uses; a slug with no
    ``PROVIDER_GROUPS`` entry falls back to itself, matching that
    function's own "ungrouped provider IS its own group_id" contract —
    only ``gemini`` (-> ``google``) and ``openai-api`` (-> ``openai``)
    actually differ from their slug today; deepseek/zai/openrouter/
    anthropic are ungrouped, so their slug already equals their group_id.
    """
    merged = {**reachability.get("via_proxy", {}), **reachability.get("direct", {})}
    return {(provider_group_for_slug(slug) or slug): reachable for slug, reachable in merged.items()}


def _legal_check_key_env_vars(tools_rows: list[dict]) -> set[str]:
    """Env vars ``/api/check/key`` is allowed to probe.

    Union of: every provider's primary credential var
    (``wizard_providers()``, covers the model-provider block *and* the
    "запасная модель" fallback block — both reuse the same provider
    catalog) and every ``env_vars`` key that appears on any row of the
    "изменить" tool catalog (``wizard_tool_blocks()`` — covers
    ``FAL_KEY``/``FIRECRAWL_API_KEY``/``HASS_TOKEN`` among others, since
    those rows already carry them; see
    ``tests/hermes_cli/test_setup_wizard_app_form.py`` for the assertion
    that they're present without needing a hardcoded backstop). An
    arbitrary env var name from a form POST — e.g. ``ANTHROPIC_API_KEY``
    when the active provider isn't Anthropic, or something unrelated
    entirely — must never reach ``probe_provider_key``.
    """
    legal = {p["env_var"] for p in wizard_providers() if p["env_var"]}
    for block in tools_rows:
        for row in block["rows"]:
            for env in row.get("env_vars", []):
                key = env.get("key")
                if key:
                    legal.add(key)
    return legal


def _mask(value: str | None) -> dict:
    """``{"is_set"}`` for a secret field — never the raw value or any
    fragment of it.

    Finding 6 (owner-approved fix): this used to also return a ``masked``
    key (``redact_key(value)`` — the first/last 4 characters of the real
    secret). The client stopped reading that field once
    ``applySecretPlaceholderEl`` was fixed to never echo it, but the server
    kept sending it in the ``GET /api/form`` body — visible in DevTools/
    Network/HAR/any debug proxy log, which violates the project invariant
    that a saved secret never echoes, not even partially. ``is_set`` is
    the only signal the client actually needs (whether to show the
    "уже сохранено" placeholder).
    """
    return {"is_set": bool(value)}


def _current_provider_env_var(provider_name: str) -> str | None:
    if not provider_name:
        return None
    try:
        profile = providers.get_provider_profile(provider_name)
    except Exception:
        return None
    if profile is None or not profile.env_vars:
        return None
    return profile.env_vars[0]


def _current_web_capability_env(
    web_cfg: dict, tools_rows: list[dict], *, backend_field: str, category: str
) -> dict:
    """Current value of the ACTIVE web-capability backend's own env var,
    if any — shared implementation for both ``search_env`` ("web" /
    ``search_backend``) and ``extract_env`` ("web_extract" /
    ``extract_backend``), the generalized replacement for the old flat
    ``firecrawl_key`` field (apply.py's ``search_env``/``extract_env``
    mechanism covers every web row's env var, not just Firecrawl's).

    Looked up from the live ``category`` tools block (already resolved by
    ``wizard_tool_blocks()`` for this same request — see the ``/api/form``
    route) rather than re-deriving the mapping here, so this can never
    disagree with what the wizard actually renders.

    Two shapes, matching the plain-URL-vs-secret split every other
    category already draws (``HASS_URL``/``CAMOFOX_URL`` show their real
    value; ``HASS_TOKEN``/``FAL_KEY`` are masked):
      - a ``*_URL`` env var (``SEARXNG_URL``, ``FIRECRAWL_API_URL``):
        ``{"env_var": str, "url": str}`` — the real value, never masked
        (it's a local address, not a credential).
      - anything else: ``{"env_var": str|None, "is_set": bool}`` — same
        shape ``_mask()`` returns everywhere else.
    """
    backend = (web_cfg.get(backend_field) if isinstance(web_cfg, dict) else "") or ""
    env_var = None
    if backend:
        block = next((b for b in tools_rows if b.get("category") == category), None)
        for row in (block or {}).get("rows", []):
            if row.get("web_backend") == backend:
                envs = row.get("env_vars") or []
                if envs:
                    env_var = envs[0].get("key")
                break
    if env_var and env_var.endswith("_URL"):
        return {"env_var": env_var, "url": get_env_value(env_var) or ""}
    return {"env_var": env_var, **_mask(get_env_value(env_var) if env_var else None)}


def _current_search_env(web_cfg: dict, tools_rows: list[dict]) -> dict:
    return _current_web_capability_env(
        web_cfg, tools_rows, backend_field="search_backend", category="web"
    )


def _current_extract_env(web_cfg: dict, tools_rows: list[dict]) -> dict:
    return _current_web_capability_env(
        web_cfg, tools_rows, backend_field="extract_backend", category="web_extract"
    )


def _current_tool_env(tools_rows: list[dict]) -> dict:
    """Masked current value for every env var exposed by any row of the
    generic provider-select categories (tts/image_gen/video_gen/x_search)
    — the read-side counterpart of the generic ``tool_env`` write
    mechanism (see ``apply.py``). Keyed by env var name (not by row/
    category) so the front-end can look up "is this row's own key already
    saved" for WHICHEVER row is currently selected, without a per-category
    special case — same shape ``_current_search_env`` already returns for
    a single active "web" row, generalized to every row at once since,
    unlike "web", these categories can each have several plausible rows
    with independently-saved keys.
    """
    result: dict = {}
    for block in tools_rows:
        if block.get("category") not in _TOOL_ENV_CATEGORIES:
            continue
        for row in block.get("rows", []):
            for env in row.get("env_vars", []):
                key = env.get("key")
                if not key or key in result:
                    continue
                if key.endswith("_URL"):
                    result[key] = {"is_set": bool(get_env_value(key)), "url": get_env_value(key) or ""}
                else:
                    result[key] = _mask(get_env_value(key))
    return result


def _cron_job_count() -> Optional[int]:
    """Сколько задач по расписанию уже заведено. ``None`` — «не знаем».

    Три исхода, и третий существует отдельно от нуля намеренно (спека 11):

    * файла нет — задач нет, ноль. Это знание, а не догадка.
    * файл разобран — столько задач, сколько в нём записей.
    * файл есть, но не читается/не разбирается — ``None``.

    Смешать третий случай со вторым значило бы промолчать о задачах,
    которые на самом деле есть, и предупреждение о смене пояса не
    показалось бы именно тому клиенту, которому оно нужнее всего.

    Читается сам файл, а не ``cron.jobs.load_jobs()``: тот при разборе
    умеет чинить повреждённый ``jobs.json`` записью на диск, а мастеру
    здесь нужно только посчитать — молча править чужой файл на GET-запросе
    он не вправе.
    """
    path = get_hermes_home() / "cron" / "jobs.json"
    if not path.exists():
        return 0
    try:
        # utf-8-sig, как и в ``cron.jobs._parse_jobs_file``: файл, записанный
        # под Windows, несёт BOM, и обычный utf-8 споткнулся бы на нём —
        # мастер сказал бы «проверить не удалось» там, где задачи прекрасно
        # читаются самим планировщиком.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        logger.info("cron jobs count unavailable: jobs.json unreadable", exc_info=True)
        return None
    # Настоящая форма на диске — словарь ``{"jobs": [...], "updated_at": ...}``
    # (``cron/jobs.py`` пишет именно её). Голый список — форма авторемонта,
    # которую планировщик тоже принимает; принимаем и мы, иначе на такой
    # машине мастер молчал бы о существующих задачах.
    if isinstance(data, dict):
        jobs = data.get("jobs")
        if isinstance(jobs, list):
            return len(jobs)
    if isinstance(data, list):
        return len(data)
    return None


def _current_state(config: dict, tools_rows: list[dict]) -> dict:
    """Return-mode prefill (spec §12.4): non-secrets by value, secrets masked.

    Never sources secret values from ``config.yaml`` — every credential the
    wizard writes lands in ``.env`` (see ``apply.py``), so every secret
    field here reads through ``get_env_value``, not the config dict.
    """
    model_cfg = config.get("model") or {}
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    provider_name = model_cfg.get("provider") or ""
    provider_env_var = _current_provider_env_var(provider_name)
    # Device-code providers (openai-codex, minimax-oauth) have no env var —
    # their credential lives in auth.json, not .env — so their "already
    # configured" signal is a live credential-store check instead of
    # get_env_value(). device_login_looks_active() (NOT device_login_is_valid,
    # which refreshes over the network) — this runs on EVERY /api/form GET,
    # including page reloads; see that function's own docstring for why a
    # refresh-capable check here risks tripping MiniMax's quarantine logic
    # for no reason. This is what lets a returning client who already
    # authenticated in an earlier visit skip re-doing the OAuth dance.
    provider_device_login_ok = (
        device_login_looks_active(provider_name) if provider_name in DEVICE_CODE_PROVIDERS else False
    )

    web_cfg = config.get("web") or {}
    browser_cfg = config.get("browser") or {}
    tts_cfg = config.get("tts") or {}
    edge_cfg = tts_cfg.get("edge") if isinstance(tts_cfg, dict) else {}
    if not isinstance(edge_cfg, dict):
        edge_cfg = {}

    return {
        "telegram_token": _mask(get_env_value("TELEGRAM_BOT_TOKEN")),
        "allowed_users": get_env_value("TELEGRAM_ALLOWED_USERS") or "",
        "proxy": get_env_value("TELEGRAM_PROXY") or "",
        "provider": {
            "name": provider_name,
            "base_url": model_cfg.get("base_url") or "",
            "model": model_cfg.get("default") or "",
            "api_key": _mask(get_env_value(provider_env_var) if provider_env_var else None),
            "device_login_ok": provider_device_login_ok,
        },
        # Первый элемент цепочки — то, что клиент выбрал; в конфиге с
        # 2026-09-05 может лежать список (см. trix_search_chain), и отдать
        # его в поле выбора целиком значило бы потерять выбор при возврате.
        "search_backend": _primary_backend(
            web_cfg.get("search_backend") if isinstance(web_cfg, dict) else ""
        ),
        "search_env": _current_search_env(web_cfg, tools_rows),
        # Same shape as search_backend/search_env above, for the SEPARATE
        # "web_extract" ("Чтение страниц") block.
        "extract_backend": (web_cfg.get("extract_backend") if isinstance(web_cfg, dict) else "") or "",
        "extract_env": _current_extract_env(web_cfg, tools_rows),
        "browser_backend": (browser_cfg.get("backend") if isinstance(browser_cfg, dict) else "") or "",
        "tts_voice": edge_cfg.get("voice") or "",
        # Часовой пояс (спека 11) — корневой скаляр config.yaml, не секрет,
        # поэтому отдаётся значением, а не маской. Пусто = клиент ещё не
        # отвечал; форма на это опирается, чтобы отличить первый ответ от
        # смены уже выбранного пояса (у смены есть предупреждение).
        "timezone": (config.get("timezone") or "") if isinstance(config.get("timezone"), str) else "",
        # Plain config values (not secrets) — which row is ACTIVE for the
        # three provider-select categories that need a config field to
        # disambiguate (see tools_view.py's provider_key / apply.py's
        # tool_provider). "" (unset) preselects that category's "off"/
        # default option client-side, same contract search_backend/
        # browser_backend already use.
        "tts_provider": (tts_cfg.get("provider") if isinstance(tts_cfg, dict) else "") or "",
        # STT's own "which row is ACTIVE" readback (joined 2026-08-20) —
        # same shape as tts_provider above (both categories always have an
        # active default provider — "local" for stt, "edge" for tts —
        # never an "off" state, unlike image_gen/video_gen).
        "stt_provider": (
            (config.get("stt") or {}).get("provider")
            if isinstance(config.get("stt"), dict)
            else ""
        )
        or "",
        "image_gen_provider": (
            (config.get("image_gen") or {}).get("provider")
            if isinstance(config.get("image_gen"), dict)
            else ""
        )
        or "",
        "video_gen_provider": (
            (config.get("video_gen") or {}).get("provider")
            if isinstance(config.get("video_gen"), dict)
            else ""
        )
        or "",
        # Read-side counterpart of the generic tool_env write mechanism —
        # see _current_tool_env()'s own docstring.
        "tool_env": _current_tool_env(tools_rows),
        "hass": {
            "url": get_env_value("HASS_URL") or "",
            "token": _mask(get_env_value("HASS_TOKEN")),
        },
        # Non-secret, like HASS_URL above — a localhost server address,
        # not a credential. This is the wizard's read side of the real
        # Camofox on/off switch (see apply.py's own docstring: runtime
        # gates on bool(get_secret("CAMOFOX_URL")), not any config.yaml
        # key), so a returning client sees whether Camofox is already
        # pointed at a running server.
        "camofox_url": get_env_value("CAMOFOX_URL") or "",
    }


async def _cached_tool_blocks(app: FastAPI) -> list[dict]:
    """``wizard_tool_blocks()`` spawns subprocesses (browser installed-checks)
    AND reads live env/config state (``provider_readiness_status`` calls
    ``get_env_value`` per row) — its "installed" verdicts are a snapshot of
    whatever secrets/config exist *right now*, not just an expensive computation.

    Cached in ``app.state`` for the process's lifetime. ``_run_submit``
    (Task 9c's submit orchestration) is responsible for invalidating it via
    ``reset_tool_cache()`` below, twice: right after ``apply_settings``
    succeeds (a saved provider key, a new ``browser_backend``, a saved
    ``HASS_TOKEN``, etc. can each flip a row's "installed"/readiness
    verdict), and again after its own install stage runs (an install just
    changed the very verdict this cache holds). Skipping either call leaves
    a stale ``tools`` block in the very next ``/api/form`` after a
    successful submit.
    """
    if app.state.tools_cache is None:
        app.state.tools_cache = await asyncio.to_thread(wizard_tool_blocks)
    return app.state.tools_cache


def reset_tool_cache(app: FastAPI) -> None:
    """Invalidate the cached tool catalog — see ``_cached_tool_blocks`` above.

    Public (no leading underscore) on purpose: ``_run_submit`` (below) is a
    caller from a different route than the one that first populates the
    cache, and calls this itself both right after ``apply_settings``
    returns ``ok`` and again after its own install stage runs.
    """
    app.state.tools_cache = None


def _sync_cached_tool_blocks(app: FastAPI) -> list[dict]:
    """Synchronous counterpart to ``_cached_tool_blocks`` — for callers
    that already run off the event loop.

    ``_run_submit`` is itself invoked via ``asyncio.to_thread`` by its
    route, so a second ``asyncio.to_thread`` hop just to read/populate the
    same cache would be pointless indirection. Reads/writes the identical
    ``app.state.tools_cache`` the async version does, so a search-env
    legality check during submit never disagrees with what ``/api/form``
    most recently rendered.
    """
    if app.state.tools_cache is None:
        app.state.tools_cache = wizard_tool_blocks()
    return app.state.tools_cache


def _legal_web_env_vars(tools_rows: list[dict], category: str) -> set[str]:
    """Env var names ``category``'s ("web" or "web_extract") own rendered
    rows expose.

    The legal set ``form.search_env.key``/``form.extract_env.key`` (the
    generalized "key of the chosen web row" — see ``_SubmitBody``) must
    belong to, drawn from the SAME live catalog the wizard rendered (never
    a hardcoded literal) so a self-hosted row that only appears when its
    local service answers (SearXNG, Firecrawl Self-Hosted — see
    ``tools_view.py``) is legal exactly when the wizard actually showed
    it.
    """
    block = next((b for b in tools_rows if b.get("category") == category), None)
    return {
        env.get("key")
        for row in (block or {}).get("rows", [])
        for env in row.get("env_vars", [])
        if env.get("key")
    }


def _legal_search_env_vars(tools_rows: list[dict]) -> set[str]:
    return _legal_web_env_vars(tools_rows, "web")


def _legal_extract_env_vars(tools_rows: list[dict]) -> set[str]:
    return _legal_web_env_vars(tools_rows, "web_extract")


def _legal_web_backend_values(tools_rows: list[dict], category: str) -> set[str]:
    """``web_backend`` values ``category``'s own rendered rows expose —
    the legal set ``form.search_backend``/``form.extract_backend`` must
    belong to. Same closed-catalog discipline as ``_legal_web_env_vars``
    (and, generalized from it, ``_legal_tool_provider_values`` below):
    drawn from the SAME live catalog the wizard rendered, never a
    hardcoded literal.
    """
    block = next((b for b in tools_rows if b.get("category") == category), None)
    return {row.get("web_backend") for row in (block or {}).get("rows", []) if row.get("web_backend")}


# Categories `form.tool_env`/`form.tool_provider` may name — the generic
# provider-select mechanism `search_env`/`search_backend` generalize to
# (see tools_view.py's module docstring). "web" isn't here: it already has
# its own dedicated `search_env`/`search_backend` fields/validation above.
# "stt" joined 2026-08-20 alongside the "Распознавание речи" category
# (tools_view.py) — every stt row (built-in or plugin, e.g. Nexara) needs
# the same generic env-var write/read path as tts/image_gen/video_gen.
_TOOL_ENV_CATEGORIES = ("tts", "stt", "image_gen", "video_gen", "x_search")


def _legal_tool_env_vars(tools_rows: list[dict]) -> set[str]:
    """Env var names ANY row of the generic provider-select categories
    (tts/image_gen/video_gen/x_search) exposes — the legal set every
    ``form.tool_env[i].key`` must belong to. Same closed-catalog
    discipline as ``_legal_search_env_vars``: drawn from the SAME live
    catalog the wizard rendered this request, never a hardcoded literal.
    """
    legal: set[str] = set()
    for block in tools_rows:
        if block.get("category") not in _TOOL_ENV_CATEGORIES:
            continue
        for row in block.get("rows", []):
            for env in row.get("env_vars", []):
                key = env.get("key")
                if key:
                    legal.add(key)
    return legal


def _legal_tool_provider_values(tools_rows: list[dict]) -> dict[str, set[str]]:
    """category -> the set of ``provider_key`` values its rendered rows
    carry — the legal set ``form.tool_provider[category]`` must belong to
    for that category. A category absent here (no row carries a
    ``provider_key`` at all — today, "x_search") is simply never a legal
    key in ``form.tool_provider``.
    """
    result: dict[str, set[str]] = {}
    for block in tools_rows:
        values = {row.get("provider_key") for row in block.get("rows", []) if row.get("provider_key")}
        if values:
            result[block.get("category")] = values
    return result


def _selected_browser_row(form: dict, tools_rows: list[dict]) -> dict | None:
    """The "browser" catalog row this submission's own choices select, or
    ``None``.

    Mirrors page.py's own client-side ``pendingToolInstallNames()`` (same
    two-branch rule, kept in sync deliberately — see that function's own
    comment): Camofox is the selected row whenever ``form.camofox_url``
    carries a real (non-empty, non-``null``) value THIS submission — see
    ``camofoxUrlPayload()``'s own contract, CAMOFOX_URL is the actual
    Camofox on/off switch, not ``browser_backend`` (apply.py's own
    docstring). Otherwise it's whichever row's ``backend_key`` matches
    ``form.browser_backend`` — always sent as the client's live <select>
    value on every submit (never a "no-op when empty" field the way
    ``telegram_token``/``proxy`` are — see ``_SubmitBody``'s own docstring
    and page.py's ``buildPayload()`` comment on why).
    """
    browser_block = next((b for b in tools_rows if b.get("category") == "browser"), None)
    rows = (browser_block or {}).get("rows", [])
    camofox_url = form.get("camofox_url")
    if isinstance(camofox_url, str) and camofox_url:
        return next(
            (row for row in rows if any(e.get("key") == "CAMOFOX_URL" for e in row.get("env_vars", []))),
            None,
        )
    backend = (form.get("browser_backend") or "").strip()
    if not backend:
        return None
    return next((row for row in rows if row.get("backend_key") == backend), None)


def _selected_category_row(category: str, value: str, tools_rows: list[dict], key_field: str) -> dict | None:
    """The row of ``category`` whose ``key_field`` (``"web_backend"`` for
    "web", ``"provider_key"`` for the generic provider-select categories)
    equals ``value``, or ``None`` when ``value`` is empty or matches no
    rendered row (an unrecognized value never raises here — the caller
    just gets nothing to install, same fail-safe posture as
    ``_selected_browser_row`` above)."""
    if not value:
        return None
    block = next((b for b in tools_rows if b.get("category") == category), None)
    return next((row for row in (block or {}).get("rows", []) if row.get(key_field) == value), None)


def _pending_tool_installs(form: dict, tools_rows: list[dict]) -> list[dict]:
    """Catalog rows this submission's own choices select AND that are not
    already installed — the exact set ``_run_submit``'s install stage runs
    ``run_tool_install`` against.

    Owner ruling 2026-08-24 ("Установка инструментов — кнопки нет"): no new
    field from the client is needed for this — every value read here
    (``browser_backend``/``camofox_url``/``search_backend``/
    ``extract_backend``/``tool_provider``) already reaches
    ``apply_settings()`` for the SAME submission, and every one of them
    was already validated (legal
    provider/backend/env-var names — see the ``search_env``/``tool_env``/
    ``tool_provider`` validation block above) before ``_run_submit`` ever
    calls this. A row already ``installed`` is excluded — resubmitting an
    unchanged, already-configured step 5 must not re-run every install
    hook (some take minutes) on every "Готово" click.
    """
    candidates = [_selected_browser_row(form, tools_rows)]
    candidates.append(
        _selected_category_row("web", (form.get("search_backend") or "").strip(), tools_rows, "web_backend")
    )
    candidates.append(
        _selected_category_row(
            "web_extract", (form.get("extract_backend") or "").strip(), tools_rows, "web_backend"
        )
    )
    tool_provider = form.get("tool_provider") or {}
    for category in ("tts", "stt", "image_gen", "video_gen"):
        value = tool_provider.get(category)
        if isinstance(value, str) and value:
            candidates.append(_selected_category_row(category, value, tools_rows, "provider_key"))
    return [
        row
        for row in candidates
        if row is not None and row.get("post_setup") and row.get("installed") is not True
    ]


_INSTALL_TIMEOUT_SECONDS = 600.0
_MSG_INSTALL_TIMEOUT = "Установка длится слишком долго — проверьте журнал на сервере."
_MSG_CHECK_GENERIC = "Проверка не пройдена."
_MSG_STAGE_GENERIC = "Не удалось выполнить этот шаг."
_MSG_PROVIDER_UNKNOWN = "Неизвестный провайдер."
_MSG_FALLBACK_UNKNOWN = "Неизвестный резервный провайдер."
_MSG_PROVIDER_REQUIRED = "Выберите провайдера модели."
_MSG_FALLBACK_NAME_REQUIRED = "Выберите резервного провайдера."
_MSG_TOKEN_REQUIRED = "Токен обязателен."
_MSG_PROVIDER_KEY_REQUIRED = "Ключ провайдера обязателен."
_MSG_FALLBACK_KEY_REQUIRED = "Ключ резервного провайдера обязателен."
_MSG_PROVIDER_ENV_VAR_MISMATCH = "Ключ не соответствует выбранному провайдеру."
_MSG_FALLBACK_ENV_VAR_MISMATCH = "Ключ не соответствует выбранному резервному провайдеру."
_MSG_SUBMIT_IN_PROGRESS = "Настройка уже выполняется, подождите."
_MSG_DEVICE_LOGIN_REQUIRED = "Сначала выполните вход по аккаунту (кнопка в блоке провайдера)."
_MSG_TIMEZONE_REQUIRED = "Выберите часовой пояс — от него зависит время напоминаний."
_MSG_SEARCH_ENV_UNKNOWN = "Неизвестное поле поиска."
_MSG_SEARCH_BACKEND_UNKNOWN = "Неизвестный источник поиска."
_MSG_EXTRACT_ENV_UNKNOWN = "Неизвестное поле чтения страниц."
_MSG_EXTRACT_BACKEND_UNKNOWN = "Неизвестный источник чтения страниц."
_MSG_TOOL_ENV_UNKNOWN = "Неизвестное поле инструмента."
_MSG_TOOL_PROVIDER_UNKNOWN = "Неизвестный вариант инструмента."
_MSG_TOOL_INSTALL_FAILED_GENERIC = "Установка не удалась. Подробности — в логах на сервере."


def _run_tool_install_with_timeout(post_setup_key: str) -> dict:
    """Run ``run_tool_install(post_setup_key)`` under the same 600s ceiling
    the old, now-removed standalone ``/api/install`` endpoint enforced
    (``_INSTALL_TIMEOUT_SECONDS``) — this is where that protection moved
    to. ``_run_submit`` (below) already runs entirely inside a worker
    thread (the route's own ``asyncio.to_thread``), so there is no event
    loop here to hang an ``asyncio.wait_for`` off of; a second, one-shot
    thread pool plays the same role synchronously. A hung installer
    (npm/pip stuck, a stalled download) must not hang the WHOLE submission
    (restart/liveness never run) indefinitely.

    Never raises — a same-process exception from ``run_tool_install``
    itself (defensive; its own docstring says it shouldn't) is caught and
    folded into an ``ok: False`` verdict, same as a timeout. The
    under-timeout thread is not (cannot be) cancelled on a timeout —
    ``pool.shutdown(wait=False)`` just stops waiting on it; the
    subprocess it may have spawned keeps running server-side, same
    caveat the removed endpoint's own docstring already carried.
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(run_tool_install, post_setup_key)
    try:
        result = future.result(timeout=_INSTALL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        logger.warning("submit install timed out: key=%s", post_setup_key)
        pool.shutdown(wait=False)
        return {"ok": False, "message": _MSG_INSTALL_TIMEOUT}
    except Exception:
        logger.warning("submit install failed: key=%s", post_setup_key, exc_info=True)
        pool.shutdown(wait=False)
        return {"ok": False}
    pool.shutdown(wait=False)
    return result if isinstance(result, dict) else {"ok": False}


def _expected_provider_env_var(provider_name: str) -> str | None:
    """The env var ``wizard_providers()`` (the wizard's own catalog — the
    same one ``_legal_provider_names()`` draws from) associates with a
    provider name, or ``None`` when the provider isn't in the catalog, or
    is, but has no live-checkable credential var at all (e.g. a
    device-code-only provider).

    Used to reject a form that names a legal provider but pairs it with a
    DIFFERENT provider's env var — e.g. ``name="openrouter"`` sent
    alongside ``env_var="GLM_API_KEY"``. Without this check the client
    fully controls which env var a submitted API key lands under
    (``apply_settings`` just writes whatever ``env_var`` it's handed), so
    a malicious or buggy submission could overwrite an unrelated
    credential under the name of an innocuous-looking provider choice.
    """
    for row in wizard_providers():
        if row["name"] == provider_name:
            return row.get("env_var") or None
    return None


def _run_post_submit_support_pass() -> str | None:
    """Run ``trix_support``'s own check-fix-recheck pass once, right after
    a successful "Готово", and reduce it to the one client-safe sentence
    ``support_view.py``'s ``POST /api/support/run`` already returns for
    the exact same pass — see this module's own docstring section on why
    a single mechanism now backs both entry points.

    **Two hard isolation rules, both required by the brief, neither
    negotiable:**

    1. **A bad verdict must not fail the submission.** By the time this
       runs, settings are already saved, the gateway has already
       restarted, and the bot has already answered a live probe
       (``wait_bot_alive``) — there is nothing left to roll back, and
       failing the response here would throw away a genuinely successful
       setup over a machine-health finding that is, at worst, informational
       to a first-time client. Exactly the same posture ``_run_submit``
       already takes for ``tool_install_failures``/``warnings``: collect
       and report, never fail.
    2. **A crash inside the pass must not fail the submission either.**
       ``run_support_pass()`` shells out to a handful of checks (see its
       own module docstring) that are not this function's to fully trust
       under every possible host condition. Catching broadly here and
       degrading to "no extra message" is deliberate — the alternative is
       a 500 on an otherwise complete ``/api/submit`` over a pass that
       was never required for success in the first place.

    Returns one of ``trix_support.build_client_report()``'s three fixed
    Russian sentences, or ``None`` when the pass itself could not be run
    at all. Never a check id, a stage name, or a log/error string — same
    contract ``support_view.py`` already enforces for its own response
    (see that module's own docstring, "Report shape").
    """
    try:
        result = trix_support.run_support_pass()
        # Full detail goes to our own internal log first, unconditionally —
        # same ordering support_view.py's /api/support/run already uses —
        # so the log is never missing an entry just because something
        # downstream of this call later goes wrong.
        trix_support.write_internal_report(result)
        return trix_support.build_client_report(result)
    except Exception:
        logger.warning("submit post-success support pass failed", exc_info=True)
        return None


def _run_submit(form: dict, app: FastAPI) -> tuple[int, dict]:
    """Synchronous orchestration for ``POST /api/submit`` (spec §10).

    Runs entirely inside ``asyncio.to_thread`` (the caller does that) —
    every step here is blocking I/O: live Telegram/provider-key probes,
    ``.env``/``config.yaml`` writes, whatever tool-install hooks this
    submission's own choices select (each up to ``_INSTALL_TIMEOUT_SECONDS``
    — see ``_run_tool_install_with_timeout``), a ``hermes gateway restart``
    subprocess (up to ~120s), and a liveness poll (up to 90s).

    Order is fixed by the spec and MUST NOT be reordered: **validate
    (nothing written yet) -> apply (write) -> install (owner ruling
    2026-08-24 — see ``_pending_tool_installs``) -> restart -> prove.**
    Any failure before "prove" leaves the wizard open with a precise
    Russian message attached to a stage/field, and nothing past that step
    runs — in particular, a failed apply must never trigger a restart,
    and a failed restart must never trigger the liveness wait. The
    install stage is the one deliberate exception to "failure stops the
    pipeline": a tool that fails to install does NOT fail the submission
    (settings are already saved, the agent still starts) — every failure
    is collected into the success response's ``tool_install_failures``
    instead (see the Returns note below).

    There is no self-extinguish step any more (spec 8, §5): the wizard
    stays open and listening after a successful submit — ``mark_completed()``
    below only flips the "first-run form has been submitted once" hint a
    return visit uses to prefill the form, it does not touch ``disabled``
    (the only thing ``_ClosedWizardGateMiddleware`` gates on). The old
    "tear the systemd unit down a few seconds after success" mechanism
    (``_extinguish_after_delay`` / ``stop_and_disable_wizard_service``)
    is gone from this path — the wizard being permanently reachable is
    the whole point of spec 8 (a client whose Telegram proxy dies needs
    the same address+credentials to still work months later).
    ``stop_and_disable_wizard_service()`` itself is untouched and still
    lives in ``gateway_ctl.py`` — it backs ``hermes setup-wizard close``
    now, not this route.

    Validation checks run in a fixed order and STOP at the first failure
    (rather than collecting all of them): partly UX (surface the blocking
    issue nearest the top of the form), partly deliberate — it means the
    live provider-key probe is never reached once an earlier, cheaper
    check (provider-name legality, token syntax/liveness, allowed-users
    syntax) has already failed the submission.

    Return-mode no-op contract: ``_SubmitBody``'s docstring already says
    an empty/missing field is "leave this alone", not "clear it" — but
    the live/syntax checks below previously ran unconditionally, which
    meant a return-mode edit that only touches e.g. ``tts_voice`` (secret
    fields arrive masked from ``/api/form`` and are never round-tripped
    back in the clear) would 422 on an empty ``telegram_token`` even
    though the saved one is still valid. Every secret/required field here
    is therefore checked ONLY when the form actually supplied a
    non-empty value; an empty value is legal exactly when something is
    already saved for it (``get_env_value``), and illegal (422,
    "required") when nothing is. This never triggers a live network
    check for a value the user didn't type this round — see the
    "already configured" tests in
    ``tests/hermes_cli/test_setup_wizard_app_submit.py``.

    Returns ``(status_code, body)``. A validation failure is
    ``(422, {"errors": {field: message}})`` and nothing is written. Every
    later failure is ``(200, {"ok": False, "stage": "apply"|"restart"|
    "liveness", "error": message})`` — the wizard stays open either way
    (the install stage never produces one of these — see above). Success
    is ``(200, {"ok": True, "bot_username": ..., "key_checked": bool,
    "tool_install_failures": [{"name": str, "message": str}, ...],
    "warnings": [str, ...], "support_check_message": str | None})`` —
    ``support_check_message`` (spec 15) is one of
    ``trix_support.build_client_report()``'s three fixed Russian
    sentences, or ``None`` when the pass itself could not be run — see
    ``_run_post_submit_support_pass``. A bad verdict there is reported,
    never a submission failure, for the same reason the install stage
    doesn't fail one: everything up to this point already succeeded.
    ``key_checked`` is a honest §10.1 disclosure: ``True`` only when the
    provider's key was actually put through a REAL live probe this
    submission (``check_provider_key``'s ``reachable`` flag — NOT its
    ``checked`` flag, which is ``True`` for any non-empty env var even
    when the provider has no entry in ``CREDENTIAL_PROBES`` at all;
    ``reachable`` is only ``True`` when an HTTP round-trip actually
    happened). A return-mode submission that leaves an already-saved key
    untouched never re-probes it either, so ``key_checked`` stays
    ``False`` there too. ``tool_install_failures`` is always present —
    an empty list when nothing needed installing or every install that
    ran succeeded — one ``{"name", "message"}`` entry per row whose
    install hook did NOT report success; Russian, safe to show verbatim
    on the success screen (page.py's own contract).
    """
    provider = form.get("provider") or {}
    provider_name = (provider.get("name") or "").strip()
    if provider_name:
        if provider_name not in _legal_provider_names():
            logger.info("submit validation failed: field=provider.name")
            return 422, {"errors": {"provider.name": _MSG_PROVIDER_UNKNOWN}}
        expected_env_var = _expected_provider_env_var(provider_name)
        submitted_env_var = provider.get("env_var") or None
        if submitted_env_var is not None and submitted_env_var != expected_env_var:
            logger.info("submit validation failed: field=provider.env_var")
            return 422, {"errors": {"provider.env_var": _MSG_PROVIDER_ENV_VAR_MISMATCH}}
        # The catalog (wizard_providers(), the same source
        # _legal_provider_names() draws from) is the only source of truth
        # for which env var a provider name writes under — a name-only
        # submission (env_var omitted entirely) must not silently drop the
        # credential at apply time just because the client didn't echo it
        # back. A submission that DID send env_var was already checked
        # above to match, so this is a no-op for it.
        provider = {**provider, "env_var": expected_env_var}
        if provider_name in DEVICE_CODE_PROVIDERS:
            # Owner requirement 2's other half: a device-code provider
            # never carries an env_var/api_key this validator's later
            # checks would catch, so without this the submit would sail
            # straight through to apply/restart/liveness even when the
            # owner never actually clicked "Войти по аккаунту" (or the
            # login they ran expired/failed). This is the ONLY gate that
            # actually proves the account login happened — it re-reads the
            # live credential store (device_login_is_valid(), refreshing
            # if needed) rather than trusting anything the client claims.
            if not device_login_is_valid(provider_name):
                logger.info("submit validation failed: field=provider.name device_login_missing")
                return 422, {"errors": {"provider.name": _MSG_DEVICE_LOGIN_REQUIRED}}
    else:
        # An empty provider block is only a legal no-op in return mode —
        # when a provider is ALREADY active (model.provider in
        # config.yaml) AND actually configured. A first-time submission
        # with nothing selected must not silently sail through
        # apply/restart/liveness with no model configured at all (the
        # whole point of the wizard).
        cfg = load_config() or {}
        model_cfg = cfg.get("model") or {}
        active_provider = (model_cfg.get("provider") or "").strip() if isinstance(model_cfg, dict) else ""
        active_row = (
            next((row for row in wizard_providers() if row["name"] == active_provider), None)
            if active_provider
            else None
        )
        # env-var-less catalog rows (device-code auth — openai-codex,
        # minimax-oauth; "custom" no longer renders at all, see
        # EXCLUDED_PROVIDERS in providers_view.py) keep their credential
        # in auth.json, not .env. An active catalog row with no env_var is
        # therefore already "configured" by itself; requiring a saved env
        # value for it would 422 a client who genuinely authenticated via
        # device code, which is exactly the false positive this branch
        # exists to avoid.
        provider_already_configured = active_row is not None and (
            not active_row.get("env_var") or bool(get_env_value(active_row["env_var"]))
        )
        if not provider_already_configured:
            logger.info("submit validation failed: field=provider.name missing_and_unconfigured")
            return 422, {"errors": {"provider.name": _MSG_PROVIDER_REQUIRED}}

    fallback = form.get("fallback")
    fallback_name = (fallback.get("name") or "").strip() if fallback else ""
    if fallback_name:
        if fallback_name not in _legal_provider_names():
            logger.info("submit validation failed: field=fallback.name")
            return 422, {"errors": {"fallback.name": _MSG_FALLBACK_UNKNOWN}}
        expected_fallback_env_var = _expected_provider_env_var(fallback_name)
        submitted_fallback_env_var = fallback.get("env_var") or None
        if submitted_fallback_env_var is not None and submitted_fallback_env_var != expected_fallback_env_var:
            logger.info("submit validation failed: field=fallback.env_var")
            return 422, {"errors": {"fallback.env_var": _MSG_FALLBACK_ENV_VAR_MISMATCH}}
        # Same catalog-is-truth normalization as the primary provider above.
        fallback = {**fallback, "env_var": expected_fallback_env_var}
    elif fallback and fallback.get("api_key"):
        # A fallback api_key with no name is exactly the gap apply_settings
        # doesn't close on its own: apply's write condition only checks
        # env_var + api_key (never name — see apply_settings' step 1), so
        # without this gate an unnamed fallback key would sail past
        # validation (nothing here keyed on name being present) and still
        # get written under whatever env_var it carries — including,
        # worst case, the SAME env_var the primary provider just validated,
        # silently overwriting a checked key with an unchecked one.
        logger.info("submit validation failed: field=fallback.name missing_with_api_key")
        return 422, {"errors": {"fallback.name": _MSG_FALLBACK_NAME_REQUIRED}}

    telegram_token = form.get("telegram_token") or ""
    proxy = form.get("proxy") or None

    # Finding 13's fix: a syntactically malformed proxy (e.g. "1.2.3.4:1080"
    # with no scheme, "socks://..." with the wrong one) used to make
    # httpx.Client(proxy=...) raise inside check_telegram_token/
    # check_provider_key's blanket except-Exception, which is
    # indistinguishable from Telegram/the provider genuinely being
    # unreachable — the resulting 422 landed on telegram_token or
    # provider.api_key instead of the actual culprit. Checked once, up
    # front, before ANY of the live checks below (including the ones this
    # gate itself would otherwise never reach — e.g. a return-mode
    # submission that doesn't retype telegram_token/provider.api_key at
    # all still writes an untouched-but-malformed proxy into .env without
    # this). check_telegram_token also runs the same cheap check
    # internally (see its own docstring) — that copy is what protects the
    # standalone step-2 "Проверить" preview button, which never reaches
    # _run_submit at all.
    if proxy:
        proxy_syntax_result = check_proxy_syntax(proxy)
        if not proxy_syntax_result.get("ok"):
            logger.info("submit validation failed: field=proxy")
            return 422, {"errors": {"proxy": proxy_syntax_result.get("error") or _MSG_CHECK_GENERIC}}

    if telegram_token:
        telegram_result = check_telegram_token(telegram_token, proxy)
        if not telegram_result.get("ok"):
            if telegram_result.get("proxy_invalid"):
                logger.info("submit validation failed: field=proxy")
                return 422, {"errors": {"proxy": telegram_result.get("error") or _MSG_CHECK_GENERIC}}
            logger.info("submit validation failed: field=telegram_token")
            return 422, {
                "errors": {"telegram_token": telegram_result.get("error") or _MSG_CHECK_GENERIC}
            }
    elif not get_env_value("TELEGRAM_BOT_TOKEN"):
        logger.info("submit validation failed: field=telegram_token missing_and_unset")
        return 422, {"errors": {"telegram_token": _MSG_TOKEN_REQUIRED}}

    allowed_users_raw = form.get("allowed_users") or ""
    if not allowed_users_raw and get_env_value("TELEGRAM_ALLOWED_USERS"):
        # No-op: keep whatever is already saved. apply_settings() already
        # treats an empty allowed_users as "don't touch this field" — we
        # just also skip re-validating a value the user didn't retype.
        normalized_allowed_users = None
    else:
        allowed_result = check_allowed_users(allowed_users_raw)
        if not allowed_result.get("ok"):
            logger.info("submit validation failed: field=allowed_users")
            return 422, {"errors": {"allowed_users": allowed_result.get("error") or _MSG_CHECK_GENERIC}}
        normalized_allowed_users = allowed_result["normalized"]

    # Часовой пояс (спека 11) — проверяется здесь, рядом с остальными
    # полями своего шага, и ДО живых проб ключа провайдера: проверка не
    # ходит в сеть, и упереться в незаполненное поле дешевле, чем сперва
    # дождаться сетевого раунд-трипа.
    #
    # Пустое значение законно ровно тогда, когда пояс уже сохранён —
    # читается config.yaml, а не .env: ключ живёт в конфиге (`.env` у нас
    # только для секретов, а `HERMES_TIMEZONE` помечена как внутренняя и
    # вычищается из окружения дочерних процессов).
    timezone_raw = (form.get("timezone") or "").strip()
    if not timezone_raw:
        saved_cfg = load_config() or {}
        saved_timezone = saved_cfg.get("timezone")
        if not (isinstance(saved_timezone, str) and saved_timezone.strip()):
            logger.info("submit validation failed: field=timezone")
            return 422, {"errors": {"timezone": _MSG_TIMEZONE_REQUIRED}}
    else:
        timezone_result = check_timezone(timezone_raw)
        if not timezone_result.get("ok"):
            logger.info("submit validation failed: field=timezone")
            return 422, {"errors": {"timezone": timezone_result.get("error") or _MSG_CHECK_GENERIC}}

    key_checked = False
    if provider_name:
        provider_env_var = provider.get("env_var")
        provider_api_key = provider.get("api_key") or ""
        if provider_api_key:
            provider_key_result = check_provider_key(provider_env_var, provider_api_key, proxy)
            if not provider_key_result.get("ok"):
                logger.info("submit validation failed: field=provider.api_key")
                return 422, {
                    "errors": {
                        "provider.api_key": (
                            provider_key_result.get("error")
                            or provider_key_result.get("message")
                            or _MSG_CHECK_GENERIC
                        )
                    }
                }
            # NOT provider_key_result["checked"] — check_provider_key sets
            # that True for ANY non-empty env_var, including providers
            # with no entry in CREDENTIAL_PROBES (probe_provider_key's own
            # "unknown provider -> ok, don't block" contract: {"ok": True,
            # "reachable": False, "message": ""}). "checked" would claim a
            # live probe happened for e.g. Anthropic/DeepSeek keys, which
            # have no probe at all — "reachable" is the field that's
            # actually true only when a real HTTP round-trip occurred.
            key_checked = bool(provider_key_result.get("reachable"))
        elif provider_env_var and not get_env_value(provider_env_var):
            logger.info("submit validation failed: field=provider.api_key missing_and_unset")
            return 422, {"errors": {"provider.api_key": _MSG_PROVIDER_KEY_REQUIRED}}

    if fallback and fallback_name:
        # By this point fallback_name is guaranteed non-empty whenever
        # fallback.get("api_key") is truthy — the "api_key present, name
        # missing" combination was already rejected above (matching
        # apply_settings' own write condition: env_var + api_key, never
        # name — see that gate's docstring for the overwrite it closes).
        # Keeping this gate on fallback_name (not api_key) preserves the
        # "key required" branch below for a named-but-keyless fallback.
        fallback_env_var = fallback.get("env_var")
        fallback_api_key = fallback.get("api_key") or ""
        if fallback_api_key:
            fallback_key_result = check_provider_key(fallback_env_var, fallback_api_key, proxy)
            if not fallback_key_result.get("ok"):
                logger.info("submit validation failed: field=fallback.api_key")
                return 422, {
                    "errors": {
                        "fallback.api_key": (
                            fallback_key_result.get("error")
                            or fallback_key_result.get("message")
                            or _MSG_CHECK_GENERIC
                        )
                    }
                }
        elif fallback_env_var and not get_env_value(fallback_env_var):
            logger.info("submit validation failed: field=fallback.api_key missing_and_unset")
            return 422, {"errors": {"fallback.api_key": _MSG_FALLBACK_KEY_REQUIRED}}

    # Generalized "key of the chosen web row" (search_env — see
    # _SubmitBody's docstring): the ONLY thing this validates is that
    # `key` names a real env var on a row the wizard's live "web" catalog
    # actually rendered THIS request — same closed-catalog discipline as
    # `_expected_provider_env_var`/`_legal_check_key_env_vars` above,
    # never trusting an arbitrary env var name from the client. A missing
    # `search_env` (or one with no `key`) is the same no-op every other
    # optional field here already is — nothing to validate.
    search_env = form.get("search_env")
    search_env_key = (search_env.get("key") or "").strip() if search_env else ""
    if search_env_key:
        legal_search_env_vars = _legal_search_env_vars(_sync_cached_tool_blocks(app))
        if search_env_key not in legal_search_env_vars:
            logger.info("submit validation failed: field=search_env.key")
            return 422, {"errors": {"search_env.key": _MSG_SEARCH_ENV_UNKNOWN}}

    # ``form.search_backend`` (the row-select field, not its credential)
    # must name a ``web_backend`` value the live "web" catalog actually
    # rendered — same closed-catalog discipline every OTHER row-select
    # field this endpoint validates uses. A missing/empty value is the
    # ordinary no-op.
    search_backend = (form.get("search_backend") or "").strip()
    if search_backend:
        legal_search_backends = _legal_web_backend_values(_sync_cached_tool_blocks(app), "web")
        if search_backend not in legal_search_backends:
            logger.info("submit validation failed: field=search_backend")
            return 422, {"errors": {"search_backend": _MSG_SEARCH_BACKEND_UNKNOWN}}

    # extract_backend/extract_env are search_backend/search_env's siblings
    # for the SEPARATE "web_extract" ("Чтение страниц") block — validated
    # against the live "web_extract" catalog, which only ever contains
    # extract-CAPABLE backends (tools_view.py's module docstring) — so an
    # attempt to name a search-only backend here (ddgs, brave-free,
    # searxng, xai) 422s before it ever reaches apply_settings.
    extract_backend = (form.get("extract_backend") or "").strip()
    if extract_backend:
        legal_extract_backends = _legal_web_backend_values(_sync_cached_tool_blocks(app), "web_extract")
        if extract_backend not in legal_extract_backends:
            logger.info("submit validation failed: field=extract_backend")
            return 422, {"errors": {"extract_backend": _MSG_EXTRACT_BACKEND_UNKNOWN}}

    extract_env = form.get("extract_env")
    extract_env_key = (extract_env.get("key") or "").strip() if extract_env else ""
    if extract_env_key:
        legal_extract_env_vars = _legal_extract_env_vars(_sync_cached_tool_blocks(app))
        if extract_env_key not in legal_extract_env_vars:
            logger.info("submit validation failed: field=extract_env.key")
            return 422, {"errors": {"extract_env.key": _MSG_EXTRACT_ENV_UNKNOWN}}

    # Generalized ``search_env`` for the other provider-select categories
    # (tts/image_gen/video_gen/x_search — see ``_SubmitBody``'s docstring):
    # every item's ``key`` must name a real env var on a row one of THOSE
    # live catalog blocks actually rendered this request — same
    # closed-catalog discipline as ``search_env`` above. A missing/empty
    # ``tool_env`` is the same no-op every other optional field here is.
    tool_env = form.get("tool_env") or []
    if tool_env:
        legal_tool_env_vars = _legal_tool_env_vars(_sync_cached_tool_blocks(app))
        for item in tool_env:
            item_key = (item.get("key") or "").strip() if isinstance(item, dict) else ""
            if item_key and item_key not in legal_tool_env_vars:
                logger.info("submit validation failed: field=tool_env.key")
                return 422, {"errors": {"tool_env": _MSG_TOOL_ENV_UNKNOWN}}

    # ``form.tool_provider[category]`` must name a ``provider_key`` value
    # one of the SAME live catalog blocks actually rendered this request
    # for that category — never trusting an arbitrary config value from
    # the client. A missing/empty ``tool_provider`` (or an entry with an
    # empty value) is the same no-op every other optional field here is.
    #
    # Finding 8 (owner-approved fix): ``cat_key`` itself is now checked
    # against ``_TOOL_ENV_CATEGORIES`` regardless of ``value``. Before
    # this, an unrecognized ``cat_key`` paired with a non-empty ``value``
    # was already rejected as a side effect (``legal_tool_provider_values
    # .get(cat_key, set())`` is empty for an unknown category, so any
    # truthy value fails the membership check below) — but the ``null``
    # clear-signal path skipped that check entirely (``if value and
    # ...`` never even runs for ``None``), so a session-authenticated
    # client could name an arbitrary ``cat_key`` there. Harmless today
    # (apply.py's own category maps just ignore anything outside
    # ``_TOOL_PROVIDER_CONFIG_SECTIONS``/``_CATEGORY_TOOLSET``), but the
    # wizard should reject an illegal field the same way regardless of
    # which value shape carries it, not rely on a downstream no-op.
    tool_provider = form.get("tool_provider") or {}
    if tool_provider:
        legal_tool_provider_values = _legal_tool_provider_values(_sync_cached_tool_blocks(app))
        for cat_key, value in tool_provider.items():
            if cat_key not in _TOOL_ENV_CATEGORIES:
                logger.info("submit validation failed: field=tool_provider.%s unknown_category", cat_key)
                return 422, {"errors": {"tool_provider": _MSG_TOOL_PROVIDER_UNKNOWN}}
            if value and value not in legal_tool_provider_values.get(cat_key, set()):
                logger.info("submit validation failed: field=tool_provider.%s", cat_key)
                return 422, {"errors": {"tool_provider": _MSG_TOOL_PROVIDER_UNKNOWN}}

    # The token wait_bot_alive proves against: the freshly entered one, or
    # (return-mode no-op) whatever is already saved — never blank, since
    # the required-field gate above already ruled out "both empty".
    effective_token = telegram_token or get_env_value("TELEGRAM_BOT_TOKEN") or ""

    # Substitute the normalized allowed_users back into the form before it
    # reaches apply_settings — validate.check_allowed_users is the only
    # place that knows how to turn "111, 222" into the canonical
    # "111,222" the adapter compares against. When the field was a no-op
    # (normalized_allowed_users is None), leave the form's own (empty)
    # value alone so apply_settings' no-op contract keeps the saved one.
    form = dict(form)
    if normalized_allowed_users is not None:
        form["allowed_users"] = normalized_allowed_users
    # provider/fallback were normalized above (env_var filled in from the
    # catalog on a name-only submission) — without substituting the
    # normalized dicts back into form, apply_settings would see the
    # client's original (possibly env_var-less) provider/fallback and
    # silently skip writing the credential (its write condition requires
    # env_var truthy).
    form["provider"] = provider
    form["fallback"] = fallback

    # Finding 1/4 (owner-approved fix, reversed from an earlier design):
    # a ``tool_provider.<category>: null`` clear signal (see
    # ``_SubmitBody``'s docstring) used to also compute a shared-key-safe
    # subset of that category's OWN env var(s) to delete from ``.env``
    # here. That computation only ever knew about the wizard's own eight
    # categories — it had no visibility into every OTHER Hermes subsystem
    # that might depend on the same credential (``vision``'s toolset,
    # ``auxiliary`` tasks, the credential pool, a provider the client used
    # to run and might switch back to), so a client turning off e.g.
    # "Генерация изображений" could silently delete ``OPENAI_API_KEY``/
    # ``OPENROUTER_API_KEY`` out from under an active LLM provider or
    # ``vision`` — a real credential loss with no way for the client to
    # recover it (the wizard never echoes a saved secret back). Turning a
    # category off now only ever removes ``"<category>.provider"`` from
    # config.yaml and revokes the category's toolset (apply_settings()'s
    # own ``tool_provider`` handling) — it leaves ``.env`` untouched, so
    # nothing here needs to reason about who else might be using a key.
    apply_result = apply_settings(form)
    if not apply_result.get("ok"):
        logger.warning("submit apply failed: error_count=%d", len(apply_result.get("errors") or []))
        error_text = "; ".join(apply_result.get("errors") or []) or _MSG_STAGE_GENERIC
        return 200, {"ok": False, "stage": "apply", "error": error_text}

    # A device-code login that was still polling in the background when
    # this submit succeeded must never be allowed to persist afterward —
    # its own device code getting approved a few minutes from now would
    # otherwise silently write tokens (and, before this fix, model.provider
    # too — see DeviceLoginManager's own docstring) on top of whatever this
    # submission just applied. retire() bumps the manager's generation so
    # any in-flight _finish() discards its result instead of writing it.
    app.state.device_login.retire()

    # installed-verdicts in the "изменить" tool catalog depend on live
    # env/config state — a saved provider key, browser backend, HASS_TOKEN,
    # etc. can each flip one. Must run after every successful apply (see
    # reset_tool_cache's own docstring for the two call sites).
    reset_tool_cache(app)

    # Install stage (owner ruling 2026-08-24, "Установка инструментов —
    # кнопки нет"): whichever catalog rows THIS submission's own choices
    # select, and that aren't already installed, get their post_setup hook
    # run now — after settings are saved (so the choice this hook installs
    # FOR is already on disk) and before the gateway restarts (so the tool
    # is actually there the moment the freshly restarted agent looks for
    # it). A fresh, uncached catalog read (not the pre-apply cache, which
    # may be `None` right now anyway after the reset above) is the ground
    # truth for "already installed" — see _pending_tool_installs's own
    # docstring for why this needs no new field from the client.
    #
    # A failed install here does NOT fail the submission (owner ruling):
    # settings are already saved and the agent still starts — just without
    # that one tool. Every failure is collected instead, and reported
    # honestly on the success screen (see the success return below) rather
    # than silently swallowed OR treated as fatal.
    tool_install_failures: list[dict] = []
    tools_rows_for_install = _sync_cached_tool_blocks(app)
    pending_installs = _pending_tool_installs(form, tools_rows_for_install)
    if pending_installs:
        for row in pending_installs:
            post_setup_key = row.get("post_setup")
            result = _run_tool_install_with_timeout(post_setup_key)
            if not result.get("ok"):
                tool_install_failures.append(
                    {
                        "name": row.get("name") or post_setup_key,
                        "message": result.get("message") or _MSG_TOOL_INSTALL_FAILED_GENERIC,
                    }
                )
                logger.warning("submit install failed: key=%s", post_setup_key)
            else:
                logger.info("submit install succeeded: key=%s", post_setup_key)
        # Every install above just changed the very "installed" verdicts
        # this cache holds (successful or not — a failed install can still
        # have left a partial artifact behind) — the wizard stays open
        # and reachable after success (spec 8, §5), so there WILL be a
        # later /api/form read (a return visit), and a stale "still needs
        # setup" pill would be wrong there.
        reset_tool_cache(app)

    restart_result = restart_gateway()
    if not restart_result.get("ok"):
        logger.warning("submit restart failed")
        return 200, {
            "ok": False,
            "stage": "restart",
            "error": restart_result.get("message") or _MSG_STAGE_GENERIC,
        }

    alive = wait_bot_alive(
        effective_token,
        proxy,
        pre_pid=restart_result.get("pre_pid"),
        pre_platform_stamp=restart_result.get("pre_platform_stamp"),
    )
    if not alive.get("ok"):
        logger.warning("submit liveness check failed")
        return 200, {
            "ok": False,
            "stage": "liveness",
            "error": alive.get("error") or _MSG_STAGE_GENERIC,
        }

    # Fresh instance, not a WizardState held from earlier in this request
    # (there isn't one) or across the restart — mark_completed() must read
    # the current on-disk generation before mutating it (see WizardState's
    # own _reload()/mark_completed() docstrings). This is a hint only
    # (spec 8, §4.3) — it does NOT close the wizard; the wizard stays
    # open and listening (see this function's own docstring).
    WizardState.load().mark_completed()

    # Spec 15's own machine-health pass ("Проверить и починить"), run once
    # more, unconditionally, right here — same call
    # ``support_view.py``'s ``POST /api/support/run`` makes
    # (``trix_support.run_support_pass()``), one mechanism, two entry
    # points. Before this, "Готово!" only ever proved the bot answers
    # (``wait_bot_alive`` above) — it never checked the machine itself
    # (proxy health, gateway state, disk, whatever else
    # ``trix_support.SUPPORT_ACTIONS`` covers), so a client could land on
    # a success screen while something the checks would have caught (and
    # in some cases fixed) sat there unreported. See
    # ``_run_post_submit_support_pass``'s own docstring for the two
    # failure-isolation rules this must honor: a bad verdict from the
    # pass is not a submit failure, and neither is the pass itself
    # blowing up.
    support_check_message = _run_post_submit_support_pass()

    logger.info("submit succeeded")
    return 200, {
        "ok": True,
        "bot_username": alive.get("username"),
        "key_checked": key_checked,
        # Owner ruling 2026-08-24: always present (empty list when nothing
        # failed, or nothing needed installing at all) — {"name", "message"}
        # per failed row, Russian and safe to show verbatim. See
        # page.py's own success-screen rendering for the client contract.
        "tool_install_failures": tool_install_failures,
        # Finding 2 (review 2026-08-26): apply_settings()'s own non-fatal
        # notices (apply.py's docstring) — today, only "extract backend
        # picked with no usable key, so it wasn't written". Always present
        # (possibly empty), plain Russian strings safe to show verbatim —
        # same contract as tool_install_failures above.
        "warnings": apply_result.get("warnings") or [],
        # One of trix_support.build_client_report()'s three fixed Russian
        # sentences, or None when the pass itself failed to run (see
        # _run_post_submit_support_pass) — never a check id, a stage name,
        # or a log line. page.py's success screen shows this verbatim,
        # same client-safe contract support_view.py's own /api/support/run
        # response already follows.
        "support_check_message": support_check_message,
    }


def _host_from_request(request: Request) -> str | None:
    """Host-only portion of the request's ``Host`` header, or ``None``.

    Used by the ``/`` route (spec §5) to name the machine the wizard is
    running on in the page header — e.g. "...на вашей собственной
    виртуальной машине (203.0.113.7)" instead of the generic sentence.
    Strips a trailing ``:<port>`` (and unwraps an IPv6 literal in
    brackets); ``render_page()`` runs the value through ``html.escape``
    before it ever touches the HTML, since the ``Host`` header is
    client-supplied and otherwise an XSS vector.
    """
    raw = (request.headers.get("host") or "").strip()
    if not raw:
        return None
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end] if end != -1 else raw
    if ":" in raw:
        raw = raw.rsplit(":", 1)[0]
    return raw or None


def _closed_response(request: Request) -> JSONResponse | HTMLResponse:
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=410, content={"error": _CLOSED_MESSAGE})
    return HTMLResponse(status_code=410, content=f"<!doctype html><html><body>{_CLOSED_MESSAGE}</body></html>")


def _parse_origin(origin: str) -> tuple[str, str] | None:
    """Parse an ``Origin`` header into ``(scheme, host)``, or ``None``.

    ``None`` covers anything that isn't a well-formed ``scheme://host``
    origin — including the literal ``"null"`` Origin browsers send for
    sandboxed iframes / ``data:`` URLs / some redirect chains, and a
    scheme with no host (``"https://"``). Both must be treated as
    untrusted, never silently passed through.
    """
    if "://" not in origin:
        return None
    scheme, rest = origin.split("://", 1)
    host = rest.split("/", 1)[0]
    if not scheme or not host:
        return None
    return scheme, host


class _ClosedWizardGateMiddleware(BaseHTTPMiddleware):
    """410s every route once ``WizardState.is_open()`` is False."""

    async def dispatch(self, request: Request, call_next: Callable):
        if not WizardState.load().is_open():
            logger.info("request rejected: wizard closed method=%s path=%s", request.method, request.url.path)
            return _closed_response(request)
        return await call_next(request)


class _OriginGuardMiddleware(BaseHTTPMiddleware):
    """403s mutating /api/* requests whose Origin header doesn't match.

    Requests with **no** ``Origin`` header (same-origin form posts, curl,
    most non-browser clients) pass through untouched. Requests that DO
    send one are held to a strict match on both scheme and host against
    the request's own scheme/``Host`` — an unparsable or empty Origin
    (including the ``"null"`` sentinel) is rejected outright rather than
    treated as "no signal, let it through".
    """

    async def dispatch(self, request: Request, call_next: Callable):
        if request.method in _MUTATING_METHODS and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin is not None:
                parsed = _parse_origin(origin)
                request_host = request.headers.get("host", "")
                expected = (request.url.scheme, request_host)
                if parsed is None or parsed != expected:
                    logger.warning(
                        "request rejected: origin mismatch path=%s", request.url.path
                    )
                    return JSONResponse(status_code=403, content={"error": "forbidden"})
        return await call_next(request)


class _BasicAuthMiddleware(BaseHTTPMiddleware):
    """Gates every route behind HTTP Basic auth (spec §8.3).

    Replaces the old password-form + cookie ``require_session`` dependency
    entirely: there is no login route and no session — the browser resends
    ``Authorization`` on every request, and this middleware checks it on
    every request, for every route (including ``/`` and ``/api/health`` —
    spec §14.11 leaves that exception to be resolved explicitly; this
    implementation closes it uniformly with everything else rather than
    carving out a probe route, since nothing in this codebase calls it).

    Order of checks matters and is dictated by spec §8.3.6's response
    table, not by convenience:

    1. **A cached recent success** (§8.3.2) short-circuits everything else
       — no state.json read, no lockout check, no scrypt. This is the
       path almost every request on a loaded page takes.
    2. **A locked-out IP** (§8.1) always gets 429 with **no**
       ``WWW-Authenticate`` — checked before credentials are even parsed,
       so a locked-out scanner can't burn a scrypt call per request either.
       Responding with ``WWW-Authenticate`` here would make the browser
       re-prompt forever with no visible reason even for a correct
       password — the exact dead-end owner ruling 2026-08-25 closed off.
    3. **Missing or malformed** ``Authorization`` gets 401 +
       ``WWW-Authenticate`` — no lockout accounting (there's no actual
       credential guess to record).
    4. **Wrong login or wrong password** gets the byte-identical 401 +
       ``WWW-Authenticate`` response as (3) — spec §14.5: never lets a
       client distinguish "unknown login" from "wrong password for a
       known one".

    Every attempt (denied or granted) is appended to the access log
    (§8.2, ``_log_access``) — except a cache hit, which by design never
    touches ``WizardState`` at all and so has nothing new to log beyond
    the original request that populated the cache.
    """

    async def dispatch(self, request: Request, call_next: Callable):
        header_value = request.headers.get("authorization") or ""
        now = time.time()

        if header_value:
            cache_key = _auth_cache_key(header_value)
            if _auth_cache_get(request.app, cache_key, now):
                return await call_next(request)
        else:
            cache_key = None

        ip = _client_ip(request)
        state = WizardState.load()
        retry_after = state.retry_after_seconds(ip)
        if retry_after > 0:
            logger.info("auth attempt: outcome=locked ip=%s retry_after=%s", ip, retry_after)
            _log_access(ip, "", "denied", "locked")
            return HTMLResponse(status_code=429, content=_rate_limited_body(retry_after))

        credentials = _parse_basic_auth(header_value)
        if credentials is None:
            reason = "no_credentials" if not header_value else "malformed_header"
            logger.info("auth attempt: outcome=%s ip=%s", reason, ip)
            _log_access(ip, "", "denied", reason)
            return _unauthorized_response()

        login, password = credentials
        # scrypt-on-16MiB (state.verify()'s cost on every miss) plus a
        # synchronous state.json rewrite-with-fsync on failure would
        # otherwise run directly inside this coroutine — uvicorn runs one
        # process/one event loop for this app, so a burst of wrong guesses
        # (an IPv6-bucket-rotating brute force, or just several scanners at
        # once) would stall EVERY other in-flight request's turn on the
        # loop, including a legitimate client's page load, for as long as
        # the scrypt calls take to run one after another. Off-loaded to a
        # worker thread so the loop stays free to service other connections
        # while this one blocks.
        verified = await asyncio.to_thread(state.verify, login, password, ip)
        if not verified:
            logger.info("auth attempt: outcome=invalid ip=%s", ip)
            _log_access(ip, login, "denied", "invalid_credentials")
            return _unauthorized_response()

        logger.info("auth attempt: outcome=success ip=%s", ip)
        _log_access(ip, login, "granted", "")
        if cache_key is not None:
            _auth_cache_put(request.app, cache_key, now)
        return await call_next(request)


async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 handler that never echoes submitted values back to the client.

    FastAPI/pydantic's default ``RequestValidationError`` payload includes
    each error's ``input`` — for a missing top-level field that could be
    the *entire submitted body*, secrets included (``/api/submit``'s
    provider/fallback API keys). We only ever emit ``loc`` (which field)
    and ``type`` (what kind of problem) — never ``input``, never ``msg``
    (some pydantic message strings embed the offending value too).
    """
    logger.info("request validation failed: outcome=422 path=%s", request.url.path)
    sanitized = [
        {"loc": list(err.get("loc", [])), "type": err.get("type", "")}
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": sanitized})


def create_app() -> FastAPI:
    app = FastAPI(
        title="Trix Agent Setup Wizard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.tools_cache = None
    # In-memory Basic-auth success cache (§8.3.2) — per app instance (per
    # process, in production; per test/create_app() call in tests), never
    # a module-global, so each instance has its own isolated cache and a
    # test never sees a stale hit left over from a different HERMES_HOME.
    app.state.auth_cache = {}
    app.state.auth_cache_lock = threading.Lock()
    # One active device-code login per process (owner requirement 2) — see
    # DeviceLoginManager's own docstring. Fresh instance per create_app()
    # call, same isolation pattern as app.state.auth_cache above (each
    # test/process gets its own, never a module-global).
    app.state.device_login = DeviceLoginManager()
    # Guards POST /api/submit against a double-fire: a doubled "Готово"
    # click, or a browser retry of a slow request. _run_submit can take
    # up to ~4 minutes (restart_gateway's 120s timeout + wait_bot_alive's
    # 90s poll) — a second concurrent submit would restart the gateway
    # again mid-poll, pulling the rug out from under the first request's
    # liveness wait and turning a real success into a false "бот не
    # отвечает". The Lock (not just a bare bool) makes the check-and-set
    # atomic even though the submit route runs its heavy work in
    # asyncio.to_thread — two nearly-simultaneous requests could
    # otherwise both read submit_in_flight as False before either sets it.
    app.state.submit_in_flight = False
    app.state.submit_lock = threading.Lock()

    app.add_exception_handler(RequestValidationError, _validation_exception_handler)

    # Starlette applies middleware in reverse registration order (last
    # added runs first). Execution order, outermost first:
    #   _ClosedWizardGateMiddleware -> _BasicAuthMiddleware -> _OriginGuardMiddleware -> route
    # A disabled wizard 410s uniformly before the auth gate even runs (no
    # point prompting for a password the owner has switched off). Auth
    # runs before the Origin guard because the Origin guard's whole job
    # (§8.3 point 4) is protecting an AUTHENTICATED browser's
    # auto-attached Basic credentials from a cross-site-forged mutating
    # request — it has nothing to check before auth has happened anyway.
    app.add_middleware(_OriginGuardMiddleware)
    app.add_middleware(_BasicAuthMiddleware)
    app.add_middleware(_ClosedWizardGateMiddleware)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        # support_visible (spec 15): the rail's "Что-то не работает?" entry
        # only makes sense once there is something on disk for a support
        # pass to check (a saved bot token, a saved provider key) — that is
        # exactly what WizardState.is_completed() records (set once, by
        # _run_submit, right after the very first successful "Готово"; it
        # never clears again — see that method's own docstring). Read fresh
        # on every request, same as every other WizardState.load() call in
        # this file — no caching across requests.
        support_visible = WizardState.load().is_completed()
        return HTMLResponse(content=render_page(_host_from_request(request), support_visible=support_visible))

    @app.get("/api/health")
    async def health() -> dict:
        # Deliberately behind the same Basic-auth gate as every other
        # route (spec §14.11 flags this route as the one exception that
        # needs an explicit call): nothing in this codebase calls it, so
        # there is no liveness-probe use case to preserve, and leaving it
        # open would be the one crack in "not a single byte without
        # auth, on any route" for zero benefit.
        return {"ok": True}

    @app.get("/api/form")
    async def form(request: Request) -> dict:
        providers_rows, provider_groups_rows, tools_rows, config = await asyncio.gather(
            asyncio.to_thread(wizard_providers),
            asyncio.to_thread(wizard_provider_groups),
            _cached_tool_blocks(request.app),
            asyncio.to_thread(load_config),
        )
        # _current_state() makes ~8 blocking get_env_value() calls (each a
        # potential .env file read) — off the event loop like everything
        # else here, not just the network/subprocess calls. tools_rows is
        # threaded through so _current_search_env() can look up the active
        # backend's env var from the SAME live "web" block this response
        # renders — never a second, possibly-disagreeing lookup.
        current = await asyncio.to_thread(_current_state, config or {}, tools_rows)
        cron_jobs = await asyncio.to_thread(_cron_job_count)
        return {
            # Flat per-variant rows — kept for back-compat: fallback-model
            # lookups (renderAdvancedFallback in page.py) and every
            # existing consumer/test key off this shape, and
            # provider.name in a submission is always a variant slug, never
            # a group_id. provider_groups (below) is the NEW grouped view
            # the primary provider picker renders from (owner requirement
            # 1 — one top-level "OpenAI" row, not two).
            "providers": providers_rows,
            "provider_groups": provider_groups_rows,
            # "web" (Поиск и извлечение страниц) is now a regular member of
            # `tools` — the old separate `search` key (wizard_search_backends())
            # is gone; the web block IS the search/extract catalog now.
            "tools": tools_rows,
            "current": current,
            # Все пояса, какие знает рантайм, разложенные по группам с
            # Россией первой (спека 11, решение владельца: клиент может
            # быть откуда угодно). Форма рисуется отсюда — второго,
            # возможно разошедшегося списка внутри страницы нет.
            "timezones": zone_groups(),
            # Сколько задач по расписанию уже заведено — форма показывает
            # предупреждение о смене пояса только когда есть что терять.
            # ``None`` означает «проверить не удалось», и форма говорит
            # именно это, а не выдаёт незнание за отсутствие задач.
            "cron_jobs": cron_jobs,
        }

    @app.post("/api/models")
    async def models(body: _ModelsBody) -> dict:
        if body.provider not in _legal_provider_names():
            logger.info("models rejected: outcome=unknown_provider")
            raise HTTPException(status_code=400, detail="неизвестный провайдер")
        if body.provider in DEVICE_CODE_PROVIDERS:
            # Device-code providers authenticate through the Hermes auth
            # store, not a client-supplied api_key — same catalog source
            # `hermes model`'s own picker uses (provider_model_ids(), which
            # for openai-codex calls resolve_codex_runtime_credentials()
            # internally; for minimax-oauth it's the same static curated
            # list wizard_providers()'s fallback_models already carries).
            from hermes_cli.models import provider_model_ids

            result = await asyncio.to_thread(provider_model_ids, body.provider)
        else:
            # Positional, not a `proxy=` kwarg — several tests patch this
            # module's `fetch_live_models` with a bare `lambda *a: ...`.
            result = await asyncio.to_thread(
                fetch_live_models, body.provider, body.api_key, body.base_url, body.proxy or None
            )
        return {"models": result}

    @app.post("/api/device/start")
    async def device_start(request: Request, body: _DeviceStartBody) -> JSONResponse:
        if body.provider not in DEVICE_CODE_PROVIDERS:
            logger.info("device/start rejected: outcome=unknown_provider")
            raise HTTPException(status_code=400, detail="неизвестный провайдер")
        try:
            # Positional, not a `proxy=` kwarg — the test fakes for
            # app.state.device_login are plain `def start(self, provider,
            # proxy=None)` stand-ins, matching DeviceLoginManager.start()'s
            # own signature.
            info = await asyncio.to_thread(
                request.app.state.device_login.start, body.provider, body.proxy or None
            )
        except Exception:
            logger.warning("device/start failed: provider=%s", body.provider, exc_info=True)
            # start() already stashed a specific Russian message in status()
            # before re-raising (e.g. the rate-limit case — see
            # device_login._russian_error_for) — surface that instead of a
            # flat generic string whenever one is available.
            status = request.app.state.device_login.status()
            message = status.get("error") or "Не удалось начать вход. Попробуйте ещё раз."
            return JSONResponse(status_code=502, content={"error": message})
        return JSONResponse(status_code=200, content=info)

    @app.get("/api/device/status")
    async def device_status(request: Request) -> dict:
        return request.app.state.device_login.status()

    @app.post("/api/check/telegram")
    async def check_telegram(body: _CheckTelegramBody) -> dict:
        return await asyncio.to_thread(check_telegram_token, body.token, body.proxy or None)

    @app.post("/api/check/telegram_user")
    async def check_telegram_user_endpoint(body: _CheckTelegramUserBody) -> dict:
        """Owner feedback п.4: a best-effort "кто это" lookup for the id
        typed into step 3's "Ваш Telegram id" field — see
        ``validate.check_telegram_user``'s own docstring for why every
        negative outcome (chat not found, bad token, network failure)
        collapses to the same flat ``{"ok": False}`` instead of a
        distinguishable error the client could mistake for "your id is
        wrong"."""
        return await asyncio.to_thread(check_telegram_user, body.token, body.user_id, body.proxy or None)

    @app.post("/api/check/proxy")
    async def check_proxy(body: _CheckProxyBody) -> dict:
        """Spec A4: the redesigned "Прокси" step's auto-check-on-entry
        probe — fires the instant the step loads (no button any more, see
        the old commit-3 "Проверить доступность" this replaces), and again
        whenever the client wants a fresh read after editing the field.

        No tokens involved — a bare reachability probe against Telegram
        plus six provider hosts, through the proxy the client just typed
        (not yet saved) when non-empty, else direct (see
        ``validate.check_reachability``'s own docstring for exactly which
        hosts go through the proxy vs. always direct, and why an empty
        proxy is a legal, common input here rather than a no-op).

        Response shape::

            {"telegram": bool,
             "via_proxy": {"openai-api": bool, "anthropic": bool, "openrouter": bool},
             "direct": {"deepseek": bool, "zai": bool, "gemini": bool},
             "providers": {<catalog group_id>: bool, ...}}

        ``providers`` re-keys the SAME via_proxy/direct booleans by the
        wizard's own catalog ``group_id`` (``wizard_provider_groups()``)
        instead of a raw provider slug/host — spec A4: "провайдер ->
        достижим" must be expressible in the identity the client already
        renders step-4's provider list under, so it never has to hardcode
        a host name to know which group to grey out. via_proxy/direct stay
        in the response too (not replaced by ``providers``) so the "Прокси"
        step's own coarse verdict (спека's two/three outcomes) doesn't need
        reconstructing from six group ids.
        """
        result = await asyncio.to_thread(check_reachability, body.proxy or None)
        result["providers"] = _reachability_providers_by_group(result)
        return result

    @app.post("/api/check/key")
    async def check_key(
        request: Request, body: _CheckKeyBody
    ) -> dict:
        if body.env_var is not None:
            tools_rows = await _cached_tool_blocks(request.app)
            if body.env_var not in _legal_check_key_env_vars(tools_rows):
                logger.info("check/key rejected: outcome=unknown_env_var")
                raise HTTPException(status_code=400, detail="неизвестное поле")
        return await asyncio.to_thread(check_provider_key, body.env_var, body.value, body.proxy or None)

    @app.post("/api/submit")
    async def submit(
        request: Request, body: _SubmitBody
    ) -> JSONResponse:
        state = request.app.state
        with state.submit_lock:
            if state.submit_in_flight:
                logger.info("submit rejected: outcome=already_in_flight")
                return JSONResponse(status_code=409, content={"error": _MSG_SUBMIT_IN_PROGRESS})
            state.submit_in_flight = True

        try:
            form = body.model_dump()
            status_code, payload = await asyncio.to_thread(_run_submit, form, request.app)
            return JSONResponse(status_code=status_code, content=payload)
        finally:
            with state.submit_lock:
                state.submit_in_flight = False

    # Support section (spec 15) — a separate entry point (GET /support),
    # not a step of the form above, reachable the instant Basic auth
    # succeeds. Registered last so it lands on the SAME app instance,
    # after the middleware stack and app.state above are already set up —
    # see support_view.py's own module docstring for why its two mutating
    # routes need no separate Origin-guard wiring here (they live under
    # /api/, which _OriginGuardMiddleware already covers uniformly).
    register_support_routes(app)

    return app
