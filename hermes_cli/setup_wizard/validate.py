"""Input validation for the setup wizard (spec §10.1).

Six independent checks the wizard runs against user-supplied form fields
before persisting them:

- ``check_proxy_syntax`` — finding 13's fix: a cheap, network-free
  scheme/host check for the ``proxy`` field, run before any of the checks
  below ever open a connection. Without this, a typo'd proxy
  (``"1.2.3.4:1080"`` with no scheme, ``"socks://…"`` with the wrong one)
  makes ``httpx.Client(proxy=...)`` raise at construction time — a failure
  every check below already caught with a blanket ``except Exception``,
  which meant it looked EXACTLY like Telegram/the provider being
  unreachable and got blamed on the wrong field (``telegram_token`` /
  ``provider.api_key``) instead of the actual culprit, ``proxy``.
- ``check_telegram_token`` — live ``getMe`` call against the Bot API,
  optionally through a proxy the user typed into the form (not yet saved
  to ``.env``, so it is passed as an argument rather than resolved via
  ``resolve_proxy_url`` the way the running adapter does — see the module
  docstring note below for why this is a deliberate divergence). Runs
  ``check_proxy_syntax`` first when a proxy is given.
- ``check_telegram_user`` — owner feedback п.4 (live VM walkthrough):
  best-effort ``getChat`` lookup for a single Telegram id, so the wizard
  can show "это <имя>" once a bot has actually exchanged a message with
  that user. Never surfaces a negative answer as an error — Telegram
  legitimately can't resolve a chat the bot has never talked to, which is
  not proof the id itself is wrong.
- ``check_allowed_users`` — normalizes/validates the comma-separated
  ``TELEGRAM_ALLOWED_USERS`` value.
- ``check_provider_key`` — thin delegate to
  :func:`hermes_cli.credential_probes.probe_provider_key`.
- ``check_reachability`` — spec A4: the redesigned "Прокси" step's
  auto-check-on-entry probe. Runs seven no-token reachability probes in
  parallel (Telegram + OpenAI/Anthropic/OpenRouter — through ``proxy`` when
  given, else direct, doubling as "do I even need a proxy" — + DeepSeek/
  GLM (Z.ai)/Gemini, which are ALWAYS probed direct, proxy or not) and
  returns a structural verdict, never per-host detail. An empty ``proxy``
  is a legal, common input — see the function's own docstring.

None of these ever put the token/key value into an error message or log
line — errors are static, translated strings.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx

from hermes_cli.credential_probes import probe_provider_key

_TIMEOUT = 10.0
_ASCII_INT_RE = re.compile(r"-?[0-9]+")

# The formats the wizard's own "Прокси" step hint promises (page.py):
# "socks5://user:pass@host:port или http://host:port". ``socks5h`` (proxy
# resolves DNS remotely — httpx accepts it) is included even though the
# hint text doesn't spell it out, since it's the same scheme family and
# rejecting it would be a needless false negative for a client who typed
# it deliberately.
_VALID_PROXY_SCHEMES = frozenset({"socks5", "socks5h", "http", "https"})
_MSG_PROXY_INVALID_FORMAT = (
    "Неверный формат прокси. Ожидается socks5://user:pass@host:port "
    "или http://host:port."
)


def check_proxy_syntax(proxy: str | None) -> dict:
    """Cheap, network-free syntax check for a proxy URL string.

    Returns ``{"ok": True}`` for an empty/missing ``proxy`` (nothing to
    check — a safe no-op on empty input) or one whose scheme is one
    of ``_VALID_PROXY_SCHEMES`` and which names a host; ``{"ok": False,
    "error": ...}`` otherwise. Never makes a network call — this exists
    specifically to run BEFORE ``httpx.Client(proxy=...)`` would, so a
    malformed proxy fails fast with a message about the proxy itself
    instead of surfacing as a generic "unreachable" from whichever live
    check happened to be holding it (see this module's own docstring).
    """
    if not proxy:
        return {"ok": True}
    try:
        parsed = urlsplit(proxy.strip())
    except ValueError:
        return {"ok": False, "error": _MSG_PROXY_INVALID_FORMAT}
    if parsed.scheme not in _VALID_PROXY_SCHEMES or not parsed.hostname:
        return {"ok": False, "error": _MSG_PROXY_INVALID_FORMAT}
    return {"ok": True}


_MSG_INVALID_TOKEN = "Токен неверный — проверьте у @BotFather"
_MSG_NETWORK = (
    "Telegram недоступен с этой машины. "
    "Если он у вас заблокирован, заполните поле «Прокси»."
)


def check_telegram_token(token: str, proxy: str | None) -> dict:
    """Live-check a Telegram bot token via ``getMe``.

    Returns ``{"ok": True, "username": ...}`` on success or
    ``{"ok": False, "error": ..., "network": bool}`` otherwise. ``proxy``
    is only passed to the client when non-empty — this call happens
    during setup, before any value the form collects is written to
    ``.env``, so it can't reuse the adapter's
    ``resolve_proxy_url("TELEGRAM_PROXY", ...)`` lookup; the caller hands
    us the proxy the user just typed instead.

    ``network`` is ``True`` exactly on the branches that couldn't reach
    (or get a sane answer from) Telegram at all — as opposed to reaching
    it and getting told the token itself is wrong. It is a structural
    signal, not a string to match: the step-2 "Проверить" button in
    page.py reads it to show a distinct, non-blocking "can't verify from
    here yet" hint instead of implying the token is bad. This function's
    own ``error`` text (``_MSG_NETWORK``) is unchanged and still what the
    submit-time validator (app.py) surfaces as the blocking field error at
    "Готово" — that call site legitimately IS the final check, so its
    copy stays as-is; only the step-2 preview button needs different
    wording (see page.py's telegram-check-btn handler).

    Finding 13's fix: a syntactically malformed ``proxy`` is checked FIRST
    (``check_proxy_syntax`` — no network call) and, on failure, returns
    ``{"ok": False, "error": ..., "proxy_invalid": True}`` — deliberately
    WITHOUT ``network: True``. A bad proxy used to make
    ``httpx.Client(proxy=...)`` raise at construction, landing in the same
    blanket ``except Exception`` as a genuinely unreachable Telegram and
    getting the identical ``network: True`` / ``_MSG_NETWORK`` treatment —
    indistinguishable from "Telegram is blocked here", even though the
    actual problem is a typo in the proxy field. ``proxy_invalid`` is its
    own structural signal (same convention as ``network``) so a caller
    (``app.py::_run_submit``) can attach the resulting 422 to the
    ``proxy`` field instead of ``telegram_token``.
    """
    proxy_syntax = check_proxy_syntax(proxy)
    if not proxy_syntax.get("ok"):
        return {"ok": False, "error": proxy_syntax["error"], "proxy_invalid": True}

    kwargs: dict = {"timeout": _TIMEOUT}
    if proxy:
        kwargs["proxy"] = proxy
    try:
        with httpx.Client(**kwargs) as client:
            resp = client.get(f"https://api.telegram.org/bot{token}/getMe")
    except Exception:
        return {"ok": False, "error": _MSG_NETWORK, "network": True}

    if resp.status_code in (401, 404):
        return {"ok": False, "error": _MSG_INVALID_TOKEN}
    if resp.status_code != 200:
        return {"ok": False, "error": _MSG_NETWORK, "network": True}

    try:
        data = resp.json()
    except Exception:
        return {"ok": False, "error": _MSG_NETWORK, "network": True}

    if not isinstance(data, dict) or not data.get("ok"):
        return {"ok": False, "error": _MSG_INVALID_TOKEN}

    username = (data.get("result") or {}).get("username")
    if not username:
        return {"ok": False, "error": _MSG_INVALID_TOKEN}
    return {"ok": True, "username": username}


def check_telegram_user(token: str, user_id: str, proxy: str | None) -> dict:
    """Best-effort ``getChat`` lookup for a single Telegram user id (owner
    feedback п.4, live VM walkthrough: "было бы круто, если бы там тоже
    высвечивалось сразу, кто это").

    Telegram's Bot API only lets a bot resolve a PRIVATE chat it has
    already exchanged a message with — exactly the same "откройте бота и
    нажмите «Старт»" precondition step 3's own note already asks for (a
    bot cannot write to, or look up, a user who has never started it). A
    user who typed their real id but hasn't pressed "Старт" yet gets a
    perfectly normal ``{"ok": false, "error_code": 400, "description":
    "Bad Request: chat not found"}`` from Telegram — that is NOT proof the
    id is wrong, so this function only ever returns a positive answer or a
    flat ``{"ok": False}``: every negative path (chat not found, malformed
    token, network failure, a malformed proxy) collapses to the exact same
    shape, on purpose. The caller (page.py) must render an ``ok`` result as
    a friendly "это <имя>" aside and render everything else as silence —
    never as an error, since absence here means "can't confirm yet", not
    "you made a mistake".
    """
    proxy_syntax = check_proxy_syntax(proxy)
    if not proxy_syntax.get("ok"):
        return {"ok": False}
    kwargs: dict = {"timeout": _TIMEOUT}
    if proxy:
        kwargs["proxy"] = proxy
    try:
        with httpx.Client(**kwargs) as client:
            resp = client.get(f"https://api.telegram.org/bot{token}/getChat", params={"chat_id": user_id})
    except Exception:
        return {"ok": False}
    if resp.status_code != 200:
        return {"ok": False}
    try:
        data = resp.json()
    except Exception:
        return {"ok": False}
    if not isinstance(data, dict) or not data.get("ok"):
        return {"ok": False}
    result = data.get("result")
    if not isinstance(result, dict):
        return {"ok": False}
    first_name = (result.get("first_name") or "").strip()
    last_name = (result.get("last_name") or "").strip()
    name = (first_name + " " + last_name).strip()
    username = result.get("username") or None
    if not name and not username:
        # Telegram answered but gave us nothing a human would recognize —
        # honest silence beats a blank "Это (@)".
        return {"ok": False}
    return {"ok": True, "name": name or None, "username": username}


def check_allowed_users(raw: str) -> dict:
    """Validate/normalize ``TELEGRAM_ALLOWED_USERS``: comma-separated ints.

    Empty input is rejected — an empty allow-list silently means "nobody
    can talk to the bot", and the wizard should surface that as an error
    rather than saving it quietly.
    """
    if not raw or not raw.strip():
        return {"ok": False, "error": "Список пользователей не может быть пустым."}

    parts = [p.strip() for p in raw.split(",")]
    ids = []
    for part in parts:
        # ``int()`` also accepts Unicode decimal digits (e.g. Arabic-Indic
        # "١٢٣") and would happily normalize them to ASCII — but that value
        # would then never match the plain-ASCII string user_id the adapter
        # compares against, so reject anything that isn't already ASCII
        # digits (with an optional leading '-' for negative group ids)
        # before parsing.
        if not _ASCII_INT_RE.fullmatch(part):
            return {
                "ok": False,
                "error": "Ожидались целые числа через запятую, например: 123456,789012",
            }
        ids.append(str(int(part)))

    return {"ok": True, "normalized": ",".join(ids)}


def check_provider_key(env_var: str | None, value: str, proxy: str | None = None) -> dict:
    """Thin delegate to :func:`probe_provider_key`.

    ``env_var=None`` means "no live probe defined for this field" — the
    wizard should treat it as passed without contacting anything.

    ``proxy`` is the form's own proxy field (not yet saved to ``.env`` at
    check/validation time, same reasoning as ``check_telegram_token``
    above) — RU-hosted deployments often can't reach OpenAI/OpenRouter/
    Anthropic directly, so the live probe must go through it when set.
    """
    if env_var is None:
        return {"ok": True, "checked": False}
    return {**probe_provider_key(env_var, value, timeout=_TIMEOUT, proxy=proxy), "checked": True}


# ---------------------------------------------------------------------------
# check_reachability (spec A4) — the redesigned "Прокси" step's
# auto-check-on-entry probe: six provider hosts + Telegram, parallel, short
# timeout, structural verdict only.
# ---------------------------------------------------------------------------

# Short on purpose — this backs an auto-check that fires the instant the
# "Прокси" step opens, not a background job; seven probes run in parallel
# (ThreadPoolExecutor below) so the wall-clock cost stays close to ONE
# probe's timeout, not their sum.
_REACHABILITY_TIMEOUT = 5.0

_REACHABILITY_TELEGRAM_URL = "https://api.telegram.org/"

# Provider slug -> bare-host URL, probed THROUGH ``proxy`` when one is
# given (else direct — see check_reachability's own docstring for why that
# is the legal "do I need a proxy" case). Closed to Russian IPs by
# geography (spec content-decisions table) — a bare-host GET proves a TLS
# handshake + HTTP round trip happened, not a real API call. Hosts are
# each provider's own live ``base_url`` (see
# ``plugins/model-providers/<name>/__init__.py``), stripped to the bare
# origin — never a path that could look like a real API call.
_REACHABLE_VIA_PROXY_TARGETS: dict[str, str] = {
    "openai-api": "https://api.openai.com/",
    "anthropic": "https://api.anthropic.com/",
    "openrouter": "https://openrouter.ai/",
}

# Provider slug -> bare-host URL, ALWAYS probed with no proxy at all,
# regardless of what ``proxy`` carries — spec's "Напрямую, мимо прокси"
# column: these three work from Russia without one.
_REACHABLE_DIRECT_TARGETS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/",
    "zai": "https://api.z.ai/",
    "gemini": "https://generativelanguage.googleapis.com/",
}


def _reachable(url: str, proxy: str | None) -> bool:
    """One bare-host GET, with or without a proxy. Never raises — any
    failure (timeout, DNS, connection refused, TLS error, or even a
    malformed ``proxy`` raising at ``httpx.Client`` construction) means
    "not reachable". A 5xx counts as unreachable (the far end is broken,
    not just "answered"); anything else (even 401/403/404 — none of these
    URLs are a real API call) counts as reachable.
    """
    kwargs: dict = {"timeout": _REACHABILITY_TIMEOUT}
    if proxy:
        kwargs["proxy"] = proxy
    try:
        with httpx.Client(**kwargs) as client:
            resp = client.get(url)
    except Exception:
        return False
    return resp.status_code < 500


def check_reachability(proxy: str | None) -> dict:
    """Spec A4: the "Прокси" step's auto-check-on-entry probe.

    Returns a structural verdict, never per-host detail::

        {"telegram": bool,
         "via_proxy": {"openai-api": bool, "anthropic": bool, "openrouter": bool},
         "direct": {"deepseek": bool, "zai": bool, "gemini": bool},
         "proxy_invalid": bool}

    - ``telegram``: reachable through ``proxy`` when given, else direct.
    - ``via_proxy``: OpenAI/Anthropic/OpenRouter — probed through ``proxy``
      when given, else direct too (SAME rule as telegram — this is what
      makes an empty ``proxy`` a legal, useful input: it's how the wizard
      answers "do I even need a proxy from this machine" the instant the
      step loads, before the client has typed anything).
    - ``direct``: DeepSeek/GLM (Z.ai)/Gemini — ALWAYS probed with no proxy
      at all, no matter what ``proxy`` carries (spec's "Напрямую, мимо
      прокси" column — these three work from Russia without one, so
      routing them through a proxy would test the wrong thing).
    - ``proxy_invalid``: mirrors ``check_telegram_token``'s own flag — a
      syntactically malformed ``proxy`` (``check_proxy_syntax``, module
      docstring) means ``telegram``/``via_proxy`` are ``False`` for the
      structural reason "this isn't a proxy URL at all", not "nothing
      answered". The client's ``renderProxyVerdict()`` reads this to show
      the format hint instead of the generic "нужен прокси" message.

    ``check_proxy_syntax`` runs first (module docstring) when ``proxy`` is
    non-empty: a syntactically malformed proxy can't be used for anything
    that WOULD route through it, so ``telegram``/``via_proxy`` short-circuit
    to ``False`` without a network call — but ``direct`` targets are
    independent of proxy validity and still get a real probe, since they
    never touch the proxy either way.

    Runs every still-needed probe in parallel (``ThreadPoolExecutor``) so
    the wall-clock cost is close to one probe's timeout, not the sum of
    all seven. Never raises.
    """
    proxy = (proxy or "").strip() or None
    proxy_invalid = False
    if proxy:
        syntax_result = check_proxy_syntax(proxy)
        proxy_invalid = not syntax_result.get("ok")

    jobs: dict[str, tuple[str, str | None]] = {}
    results: dict[str, bool] = {}

    if proxy_invalid:
        results["telegram"] = False
        for name in _REACHABLE_VIA_PROXY_TARGETS:
            results[f"via_proxy::{name}"] = False
    else:
        jobs["telegram"] = (_REACHABILITY_TELEGRAM_URL, proxy)
        for name, url in _REACHABLE_VIA_PROXY_TARGETS.items():
            jobs[f"via_proxy::{name}"] = (url, proxy)

    for name, url in _REACHABLE_DIRECT_TARGETS.items():
        jobs[f"direct::{name}"] = (url, None)

    if jobs:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {pool.submit(_reachable, url, use_proxy): key for key, (url, use_proxy) in jobs.items()}
            for future, key in futures.items():
                try:
                    results[key] = future.result()
                except Exception:
                    results[key] = False

    return {
        "telegram": results["telegram"],
        "via_proxy": {name: results[f"via_proxy::{name}"] for name in _REACHABLE_VIA_PROXY_TARGETS},
        "direct": {name: results[f"direct::{name}"] for name in _REACHABLE_DIRECT_TARGETS},
        # Finding 1 (this review pass): computed above but silently dropped
        # from the return value for a while — the client's renderProxyVerdict()
        # never got a chance to tell a malformed proxy ("1.2.3.4:1080", no
        # scheme) apart from "nothing answered" and told the client to add a
        # proxy it had already typed. Same structural flag
        # check_telegram_token already exposes for the same reason.
        "proxy_invalid": proxy_invalid,
    }


_MSG_TIMEZONE_REQUIRED = "Выберите часовой пояс — от него зависит время напоминаний."
_MSG_TIMEZONE_UNKNOWN = "Такого часового пояса нет. Выберите пояс из списка."


def check_timezone(raw: str) -> dict:
    """Проверить часовой пояс, присланный формой (спека 11).

    Ворота на сервере, а не в браузере: список поясов уезжает клиенту, но
    доказательством служит принадлежность нашему списку здесь, а не то,
    что прислал браузер.

    Пустое значение отвергается отдельным текстом — это незаполненное
    поле, а не выбор «по времени сервера». Пустой ключ `timezone`
    заставляет `hermes_time` взять системное время машины, а оно на
    сервере хостера обычно не совпадает с временем клиента.
    """
    from hermes_cli.setup_wizard.timezones import is_valid

    value = raw.strip() if isinstance(raw, str) else ""
    if not value:
        return {"ok": False, "error": _MSG_TIMEZONE_REQUIRED}
    if not is_valid(value):
        return {"ok": False, "error": _MSG_TIMEZONE_UNKNOWN}
    return {"ok": True, "normalized": value}
