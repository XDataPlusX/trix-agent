"""Applying the wizard form (spec §10.2, §13).

The single most consequential module in this feature: it writes user
settings to disk. Everything here goes through the *existing* writers the
rest of Hermes already uses — ``save_env_value_secure`` /
``save_provider_env_credential`` for ``.env``, ``_update_config_for_provider``
for the active-provider slice of ``config.yaml`` + ``auth.json``, and
``load_config()``/``save_config()`` for the rest of ``config.yaml``. This
module does not open any file itself. A defect class the earlier
setup-wizard spec (spec 4) hit four times was "two writers of one file" —
adding a second writer here would repeat it.

``apply_settings`` never validates (that is the caller's job — task 9c runs
``hermes_cli.setup_wizard.validate`` first); it only writes what the caller
hands it, best-effort per field: one field failing to save does not stop
the rest, and every failure is collected into ``errors`` (Russian, static —
never includes the secret value) rather than raised.
"""
from __future__ import annotations

import logging

import providers
from hermes_cli.auth import _update_config_for_provider
from hermes_cli.config import load_config, load_env, remove_env_value, save_config, save_env_value_secure
from hermes_cli.credential_lifecycle import save_provider_env_credential
from hermes_cli.models import pick_silent_default_model

logger = logging.getLogger(__name__)

_SEARCH_TOOLSET = "search"
_WEB_TOOLSET = "web"

# Categories `form.tool_provider` may name — each maps 1:1 to the
# config.yaml top-level section that gets `.provider` set to the row's
# `provider_key` (tools_view.py). "x_search" is deliberately absent: its
# one live row needs no disambiguating config field at all (see
# tools_view.py's `_PROVIDER_KEY_MARKERS` docstring) — an unrecognized key
# here is simply ignored, same as any other stray form field. "stt"
# joined 2026-08-20 alongside the wizard's "Распознавание речи" category
# (tools_view.py) — every stt row (built-in and plugin-injected, e.g.
# Nexara) carries a `stt_provider` marker, same shape as tts_provider.
_TOOL_PROVIDER_CONFIG_SECTIONS = frozenset({"tts", "image_gen", "video_gen", "stt"})

# category -> the toolsets.py entry a `tool_provider.<category>: null`
# clear signal (finding 3/5/7 — see `apply_settings`' own docstring)
# removes from `platform_toolsets.telegram`, mirroring the ADD side
# (`_add_toolset`'s call sites). "stt" is deliberately absent — no
# toolsets.py entry gates speech-to-text at all (it rides on config alone,
# see `_TOOL_PROVIDER_CONFIG_SECTIONS`'s own docstring), so clearing it
# only ever touches `stt.provider`, never a toolset. "x_search" IS present
# here even though it's absent from `_TOOL_PROVIDER_CONFIG_SECTIONS` — it
# has no `.provider` config field, but it does have a toolset, and a
# client can send `tool_provider.x_search: null` purely as that toolset's
# clear signal (see `_SubmitBody`'s own docstring in app.py).
_CATEGORY_TOOLSET = {
    "tts": "tts",
    "image_gen": "image_gen",
    "video_gen": "video_gen",
    "x_search": "x_search",
}

# Hosts that stay reachable straight from an RU-hosted VM without a proxy —
# measured 2026-08-19 (api.nexara.ru added 2026-08-20: российский STT,
# доступен напрямую, замерено 2026-08-20). Data, not code: when a
# submitted proxy would otherwise swallow these too (variant A —
# HTTPS_PROXY covers every outbound HTTPS call, not just Telegram's),
# NO_PROXY must keep them routed directly. Never used to clear/replace a
# user's own NO_PROXY entries — only to guarantee these specific hosts
# are present alongside whatever the user already has (see
# _merge_no_proxy_hosts below).
_DIRECT_HOSTS = (
    "localhost,127.0.0.1,::1,api.z.ai,api.deepseek.com,"
    "generativelanguage.googleapis.com,api.search.brave.com,"
    "api.parallel.ai,api.firecrawl.dev,api.nexara.ru"
)


def _merge_no_proxy_hosts(current: str) -> str:
    """Merge ``_DIRECT_HOSTS`` into an existing ``NO_PROXY`` value.

    Order-preserving and never drops a host the user already listed —
    only appends whichever of ``_DIRECT_HOSTS`` isn't already present
    (case-insensitive; host names aren't case sensitive). A missing/empty
    ``current`` simply becomes ``_DIRECT_HOSTS`` verbatim.
    """
    existing = [h.strip() for h in (current or "").split(",") if h.strip()]
    seen = {h.lower() for h in existing}
    merged = list(existing)
    for host in _DIRECT_HOSTS.split(","):
        if host.lower() not in seen:
            merged.append(host)
            seen.add(host.lower())
    return ",".join(merged)


def _normalize_allowed_users(raw: str) -> str:
    """Light formatting only — no validation (the caller already validated)."""
    parts = [p.strip() for p in (raw or "").split(",")]
    return ",".join(p for p in parts if p)


def first_allowed_telegram_id(raw: str) -> str | None:
    """Первый числовой id из списка разрешённых — он же домашний чат.

    В личной переписке Telegram chat_id равен id пользователя, поэтому
    отдельного вопроса клиенту не нужно. Имена (@vasya) не годятся:
    отправка требует числового идентификатора, и такие записи
    пропускаются, а не возвращаются.
    """
    for part in (raw or "").split(","):
        candidate = part.strip().lstrip("@")
        if candidate.isdigit():
            return candidate
    return None


def resolve_default_model(provider_name: str) -> str:
    """Silent model default when the form leaves ``provider.model`` empty.

    Ruling 3 (spec): for ``openrouter`` — the catalog-labeled silent default
    (``pick_silent_default_model``, network-free, cache-only) applied to the
    provider's offline ``fallback_models`` list, so the wizard never
    silently escalates to the priciest flagship entry. For every other
    provider — the first entry of its ``fallback_models``. Returns ``""``
    for an unknown provider or one with no fallback models at all; the form
    is expected to require free-text input in that case rather than rely on
    this function.
    """
    try:
        profile = providers.get_provider_profile(provider_name)
    except Exception:
        logger.debug("resolve_default_model(%s): unknown provider", provider_name, exc_info=True)
        return ""
    models = list(profile.fallback_models or ())
    if not models:
        return ""
    if provider_name == "openrouter":
        return pick_silent_default_model(models, provider_name)
    return models[0]


def _swap_toolset(config: dict, old: str, new: str) -> bool:
    """Replace ``old`` with ``new`` in ``platform_toolsets.telegram``, once.

    Idempotent: a call that finds no ``old`` entry (already swapped, or
    never customized) is a no-op and returns False. Mutates ``config`` in
    place; returns True only when it actually changed something.
    """
    platform_toolsets = config.get("platform_toolsets")
    if not isinstance(platform_toolsets, dict):
        return False
    telegram = platform_toolsets.get("telegram")
    if not isinstance(telegram, list) or old not in telegram:
        return False

    inserted = new in telegram
    result: list = []
    for item in telegram:
        if item == old:
            if not inserted:
                result.append(new)
                inserted = True
            continue
        result.append(item)
    platform_toolsets["telegram"] = result
    return True


def swap_search_to_web(config: dict) -> bool:
    """Swap the ``search`` toolset for ``web`` (adds extraction) — idempotent."""
    return _swap_toolset(config, _SEARCH_TOOLSET, _WEB_TOOLSET)


def swap_web_to_search(config: dict) -> bool:
    """Swap the ``web`` toolset back to ``search`` (drops extraction) — idempotent.

    The clear-signal counterpart to ``swap_search_to_web`` above (finding 1,
    review 2026-08-26): when a client explicitly turns "Чтение страниц" off,
    the agent must actually lose the ``web_extract`` tool an earlier
    submission granted, not just the now-deleted ``web.extract_backend``
    config value — otherwise the toolset keeps advertising a capability the
    client just disabled.
    """
    return _swap_toolset(config, _WEB_TOOLSET, _SEARCH_TOOLSET)


def _add_toolset(config: dict, name: str) -> bool:
    """Idempotently append ``name`` to ``platform_toolsets.telegram``.

    Finding 3 (owner-approved fix): Trix ships an EXPLICIT
    ``platform_toolsets.telegram`` list (see ``assets/config/trix-config.yaml``).
    ``tools_config._get_platform_tools()`` only runs its auto-enable rules
    (``x_search_auto_enabled``, ``_homeassistant_credentials_present()``,
    etc.) when NO explicit list exists for the platform — with an explicit
    list, ``enabled_toolsets`` is exactly the named entries, nothing more.
    So a client who picks a video_gen/x_search provider or fills in Home
    Assistant credentials through the wizard, on a Trix install, would
    write the credential/config but the agent would never actually gain
    the matching toolset — "выбрал — не работает".

    Mutates ``config`` in place; returns True only when it actually
    changed something. A config with no ``platform_toolsets`` section at
    all, or a ``platform_toolsets.telegram`` that isn't a list, is left
    completely untouched — that's a non-Trix install, where the runtime's
    own auto-enable rules already cover this (see docstring above); this
    function must never invent the section.
    """
    platform_toolsets = config.get("platform_toolsets")
    if not isinstance(platform_toolsets, dict):
        return False
    telegram = platform_toolsets.get("telegram")
    if not isinstance(telegram, list) or name in telegram:
        return False
    platform_toolsets["telegram"] = [*telegram, name]
    return True


def _remove_toolset(config: dict, name: str) -> bool:
    """Idempotently remove ``name`` from ``platform_toolsets.telegram``.

    The symmetric counterpart to ``_add_toolset`` above (finding 5/7's
    clear signal): a client that explicitly turns a category off must be
    able to actually revoke the toolset an earlier submission granted, not
    just leave it in place. Same contract as ``_add_toolset`` — mutates
    ``config`` in place, returns True only on an actual change, and never
    invents a ``platform_toolsets`` section that isn't already there (a
    non-Trix install has nothing here for this function to touch).
    """
    platform_toolsets = config.get("platform_toolsets")
    if not isinstance(platform_toolsets, dict):
        return False
    telegram = platform_toolsets.get("telegram")
    if not isinstance(telegram, list) or name not in telegram:
        return False
    platform_toolsets["telegram"] = [item for item in telegram if item != name]
    return True


def _x_search_env_var_keys() -> frozenset:
    """Env var names the "x_search" tool category's own catalog rows
    expose — today just ``XAI_API_KEY`` (see ``hermes_cli.tools_config``'s
    ``TOOL_CATEGORIES["x_search"]``). x_search has no plugin injection
    (unlike tts/image_gen/video_gen/web — see
    ``hermes_cli.setup_wizard.tools_view``'s module docstring), so the
    static catalog dict is the complete, authoritative set; read directly
    from it (not hardcoded here as a literal) so a future catalog change
    stays in sync automatically.
    """
    from hermes_cli import tools_config as _tc

    category = _tc.TOOL_CATEGORIES.get("x_search") or {}
    keys: set = set()
    for provider in category.get("providers", []):
        for env in provider.get("env_vars", []):
            key = env.get("key")
            if key:
                keys.add(key)
    return frozenset(keys)


def _extract_capable_web_backends() -> frozenset:
    """Web-registry backend names whose ``supports_extract()`` is True —
    read live from ``agent.web_search_registry`` (never a hardcoded name
    list here) so a new extract-capable plugin is recognized automatically
    — same source ``tools_config.web_provider_capabilities()`` (and, via
    it, ``tools_view._catalog()``'s "web_extract" category) already
    consults. Falls back to the four backends known to support extraction
    today only if the registry import itself fails — defensive, matches
    the "never raise" posture every helper in this module keeps.
    """
    try:
        from agent.web_search_registry import list_providers
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        return frozenset(p.name for p in list_providers() if p.supports_extract())
    except Exception:
        logger.debug("apply_settings: could not read web_search_registry", exc_info=True)
        return frozenset({"firecrawl", "tavily", "exa", "parallel"})


def _web_backend_primary_env_var(backend: str) -> str | None:
    """The credential env var a web-registry backend's own setup schema
    names first (``get_setup_schema()["env_vars"][0]["key"]``), or
    ``None`` when the backend isn't registered or is keyless.

    Used only as a FALLBACK by the toolset auto-swap below, for a
    returning client who re-selects an already-configured extract backend
    without retyping its key this round (so neither ``search_env`` nor
    ``extract_env`` names it) — the form-supplied key always wins first.
    Does NOT correctly resolve a self-hosted row's real key (e.g.
    Firecrawl Self-Hosted's ``FIRECRAWL_API_URL`` — this returns
    Firecrawl's PRIMARY schema key, ``FIRECRAWL_API_KEY``) — a narrow,
    low-consequence gap: that specific resubmission-without-retyping case
    simply won't re-trigger the swap, which already happened the first
    time the self-hosted row was configured (the toolset grant is
    persisted, not re-derived on every visit).
    """
    try:
        from agent.web_search_registry import get_provider
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        provider = get_provider(backend)
        if provider is None:
            return None
        env_vars = (provider.get_setup_schema() or {}).get("env_vars") or []
        return env_vars[0].get("key") if env_vars else None
    except Exception:
        logger.debug("apply_settings: could not resolve schema for backend %r", backend, exc_info=True)
        return None


def _write_secret(key: str, value: str, written: list, errors: list, label: str) -> None:
    try:
        save_env_value_secure(key, value)
        written.append(key)
    except Exception as exc:
        logger.warning("apply_settings: failed to save %s", key, exc_info=True)
        errors.append(f"Не удалось сохранить {label}: {exc}")


def _delete_secret(key: str, removed: list, errors: list, label: str) -> None:
    """Finding 5/7's clear-signal counterpart to ``_write_secret`` above —
    removes ``key`` from ``.env`` instead of writing it. Never raised past
    this function: a missing key (``remove_env_value`` returns False) is
    not an error, it's already the desired state.

    Finding 9 (owner-approved fix): only append to ``removed`` when
    ``remove_env_value`` actually reports a deletion (``True``) — it
    returns ``False``, without raising, both when the key was never set
    AND when the key lives in a managed scope the wizard has no
    permission to touch (see its own docstring). Reporting a "removal"
    that didn't happen misleads the caller (and, transitively, the
    ``/api/submit`` response) into thinking a secret is gone when it is
    still sitting in ``.env`` untouched.
    """
    try:
        if remove_env_value(key):
            removed.append(key)
    except Exception as exc:
        logger.warning("apply_settings: failed to delete %s", key, exc_info=True)
        errors.append(f"Не удалось удалить {label}: {exc}")


def _write_provider_credential(env_var: str, api_key: str, written: list, errors: list, label: str) -> None:
    try:
        save_provider_env_credential(env_var, api_key)
        written.append(env_var)
    except Exception as exc:
        logger.warning("apply_settings: failed to save provider credential %s", env_var, exc_info=True)
        errors.append(f"Не удалось сохранить {label}: {exc}")


def _provider_default_base_url(provider_name: str) -> str:
    try:
        return providers.get_provider_profile(provider_name).base_url or ""
    except Exception:
        return ""


def apply_settings(form: dict) -> dict:
    """Persist wizard form fields through the existing single writers.

    ``form`` keys (all optional — a missing key is left untouched):
    ``telegram_token, allowed_users, proxy, timezone, provider {name,
    env_var, api_key, base_url, model}, fallback {…}|None, search_backend,
    search_env {key, value}|None, extract_backend, extract_env {key,
    value}|None, browser_backend, tts_voice, hass {url, token}|None,
    camofox_url, tool_env [{key, value}]|None, tool_provider {category:
    value|None}|None``.

    Finding 5/7 (owner-approved fix) — explicit ``None``/``null`` is a
    THIRD state for ``camofox_url``/``hass``/each individual value inside
    ``tool_provider``, distinct from "missing/empty", and it means "clear
    this setting" rather than "leave it alone" (``tool_provider``'s clear
    signal — including ``"x_search"``, which has no ``.provider`` config
    field of its own, so a ``None`` entry there is purely the
    toolset-clear signal — removes ``"<category>.provider"`` from
    config.yaml when the category has one, and revokes the matching
    toolset; it never touches ``.env`` — see Finding 1's note below).
    The caller (``app.py::_run_submit``) is responsible for only ever
    producing ``None`` there when the CLIENT explicitly sent JSON
    ``null`` — see ``_SubmitBody``'s own docstring for how that's kept
    unambiguous against "the client didn't send this field at all".

    ``tts_voice`` deliberately does NOT accept a ``None`` clear signal
    (Finding 2, owner-approved fix, reversed from an earlier design):
    ``DEFAULT_CONFIG``'s own ``tts.edge.voice`` baseline is the English
    ``en-US-AriaNeural``, so deleting the key to "return to Светлана"
    would silently switch a Russian-language product to an English voice
    the instant a client who once customized the voice picked the
    default option again. The client now sends the literal default voice
    name explicitly when that pick is deliberate — this function just
    writes whatever non-empty string ``tts_voice`` carries, same as every
    other plain string field here.

    ``tool_provider.<category>: null`` (Finding 1/4, owner-approved fix,
    reversed from an earlier design) deliberately does NOT delete any
    ``.env`` key either — only the earlier ``tool_env_clear`` mechanism
    did, and it was retired because no *server-side* set of exclusions
    can safely know every OTHER Hermes subsystem that might depend on a
    shared credential (``vision``'s toolset, ``auxiliary`` tasks, the
    credential pool, ...), only the wizard's own handful of categories.
    Turning a category off now only ever removes its ``.provider`` field
    and its toolset grant — the credential stays in ``.env``, harmlessly
    unused, so the client can always turn the category back on without
    hunting the key down again.

    ``search_env`` is the generalized "key of the chosen web SEARCH row" —
    replaces the old firecrawl-only ``firecrawl_key`` field now that the
    "web" tools category (spec §7.3, originally "Поиск и извлечение
    страниц", split 2026-08-26 into "Поиск в интернете" / "Чтение
    страниц") renders every live search-backend row, not just
    DuckDuckGo/Firecrawl (see ``tools_view.py``). ``key`` names the env var
    the CHOSEN row exposes (``BRAVE_SEARCH_API_KEY``, ``EXA_API_KEY``,
    ``TAVILY_API_KEY``, ``PARALLEL_API_KEY``, ``FIRECRAWL_API_KEY``,
    ``SEARXNG_URL``, ``FIRECRAWL_API_URL``, ...) — validated by the caller
    (``app.py::_run_submit``) against the live "web" catalog before this
    function ever sees it, so this function trusts it and just writes it
    through ``_write_secret`` like every other credential.

    ``extract_backend``/``extract_env`` are ``search_backend``/
    ``search_env``'s siblings for the SEPARATE "web_extract" ("Чтение
    страниц") block — the runtime resolves search and extract as
    independent capabilities (``web.search_backend`` / ``web.extract_backend``,
    ``tools/web_tools.py::_get_capability_backend()``), and the wizard now
    lets a client pick a DIFFERENT provider for each (Firecrawl for search,
    Tavily for extract, say). ``extract_env.key`` is validated the same way
    ``search_env.key`` is, against the live "web_extract" catalog. When the
    client picks the SAME provider for both (so ``search_env.key ==
    extract_env.key``), the credential is written only ONCE — ``search_env``
    always wins; ``extract_env`` is skipped when its key duplicates
    ``search_env``'s (see the write site below) — never a duplicate ``.env``
    write or a duplicate ``written`` entry.

    ``extract_backend`` is ``str | None`` (see ``_SubmitBody``'s own
    docstring): ``""``/omitted stays the ordinary no-op; an explicit
    ``None`` (finding 1, review 2026-08-26) is the finding-5/7 clear
    signal for a previously-saved extract backend — it deletes
    ``web.extract_backend`` from config.yaml AND swaps the
    ``platform_toolsets.telegram`` entry from ``web`` back to ``search``
    (``swap_web_to_search`` below), so a client who deliberately turns
    "Чтение страниц" off actually loses the ``web_extract`` tool an
    earlier submission granted, not just the config value it was resolved
    from.

    Every backend the "web_extract" catalog can offer (exa/firecrawl/
    parallel/tavily) needs a credential (finding 2, review 2026-08-26):
    ``web.extract_backend`` is only ever WRITTEN when the resolved
    credential (freshly typed this round, or already in ``.env`` from an
    earlier visit) is actually present — a client who picks a backend and
    never supplies its key gets a non-fatal Russian notice in ``warnings``
    instead (same "settings saved, one thing didn't fully apply" posture
    as ``tool_install_failures``, owner ruling 2026-08-24) rather than a
    config entry claiming a capability the agent doesn't have, or a 422
    that blocks the rest of the submission over one optional field.

    When the client leaves the "Чтение страниц" row untouched (finding 3,
    review 2026-08-26) — no explicit pick, no earlier extract choice
    saved — and the chosen ``search_backend`` is itself extract-capable
    with a usable credential, this function implicitly defaults
    ``extract_backend`` to that same value. This restores what the OLDER,
    pre-split combined block did for exactly this case (picking Firecrawl
    once configured both search AND extraction) without reintroducing the
    narrower ``search_backend == "firecrawl"`` trigger it replaced — the
    default only ever fires when nothing else has been chosen for extract,
    so a client who deliberately configured DIFFERENT providers per
    capability (or deliberately left extract off) is never overridden.

    The web toolset auto-swap (``search`` -> ``web`` in
    ``platform_toolsets.telegram``, which adds ``web_extract`` to the
    tools an agent gets — see ``toolsets.py``) reuses the exact same
    credential determination as the ``web.extract_backend`` write above
    (``extract_credential_present``) — a config entry the agent can't use
    must never grant the toolset that only makes sense alongside it
    either.

    ``tool_env`` is ``search_env``'s generalization to every OTHER
    provider-select category (tts/image_gen/video_gen/x_search — see
    ``tools_view.py``'s module docstring): a list of ``{"key": env_var,
    "value": ...}`` pairs, at most one per category (each category's rows
    carry at most one env var — enforced by
    ``tests/hermes_cli/test_setup_wizard_tools_view.py::
    test_every_provider_select_row_has_at_most_one_env_var``). One single
    mechanism for all four categories rather than a per-category field —
    ``app.py::_run_submit`` validates every ``key`` against the live
    "изменить" catalog's own env vars (same closed-catalog discipline as
    ``search_env``) before this function ever sees it, so each pair is
    trusted and written through ``_write_secret`` exactly like
    ``search_env``. (The earlier FAL.ai-only ``fal_key`` field this
    generalized is gone — FAL's row exposes ``FAL_KEY`` through
    ``tool_env`` like every other row now, dead reader removed.)

    ``tool_provider`` is the analogous generalization of
    ``browser_backend``/``search_backend`` for the categories that need a
    "which row is ACTIVE" config field to disambiguate when more than one
    row's credentials happen to be configured at once — ``{"tts":
    tts_provider, "image_gen": image_gen_provider, "video_gen":
    video_gen_provider, "stt": stt_provider}``, each value the row's own
    ``provider_key`` (see ``tools_view.py``). Writes
    ``"<category>.provider"`` in ``config.yaml`` for whichever keys are
    present and non-empty — mirrors
    ``tools_config._write_provider_config``'s/``_configure_provider``'s own
    field names, so a row picked here can never disagree with what
    ``hermes tools`` would have written for the same row. ``x_search`` has
    no entry (its one live row needs no disambiguating field — see
    ``tools_view.py``'s ``_PROVIDER_KEY_MARKERS`` docstring).

    ``camofox_url`` is the ONE real activation switch for the Camofox
    browser row (spec ruling, task 9d round 2): ``tools/browser_camofox.py``'s
    ``is_camofox_mode()`` gates on ``bool(get_secret("CAMOFOX_URL"))`` —
    NOT on ``browser.cloud_provider`` (a config.yaml key this function has
    no path to write in the first place; wiring Camofox through it would
    have been a config write with no runtime effect). ``CAMOFOX_URL`` is
    written through the same ``.env`` writer as every other secret here
    (``_write_secret`` — the identical pattern ``HASS_URL`` already uses
    below, even though a local server URL isn't a credential; ``.env`` is
    simply where this whole class of "point the runtime at a local
    server" values lives).

    ``proxy`` (variant A, owner-approved) writes THREE things when
    non-empty: ``TELEGRAM_PROXY`` (unchanged, Telegram-only), plus
    ``HTTPS_PROXY`` so the whole runtime — model providers, web
    search/extract, every outbound HTTPS call — routes through the same
    proxy, plus a merged ``NO_PROXY`` that keeps ``_DIRECT_HOSTS``
    (hosts reachable straight from an RU-hosted VM) routed directly
    on top of whatever the user already had there — never a wholesale
    overwrite. An empty/missing ``proxy`` is the same no-op it always
    was: none of the three are touched.

    Returns ``{"ok": bool, "written": [...], "removed": [...], "errors":
    [...]}`` — ``ok`` is ``not errors``. Order (spec §13):

    1. Secrets — ``.env`` writes via ``save_env_value_secure`` /
       ``save_provider_env_credential`` (deletes via ``remove_env_value``
       for the finding-5/7 clear signals — same step, since ``.env``
       writes/deletes are both immediate, unbuffered single-file edits).
    2. Active provider — ``_update_config_for_provider`` (writes
       ``model.{provider,base_url,default}`` + ``active_provider`` in
       ``auth.json``, clears stale endpoint credentials for the previous
       provider).
    3. Everything else in ``config.yaml`` — one ``load_config()`` →
       in-memory edits → one ``save_config(cfg)`` (``strip_defaults=True``,
       the default — never disabled, so a client's ``config.yaml`` doesn't
       balloon with schema defaults on every apply).

    An **empty or missing** value for any optional ``form`` field is a
    no-op — a field the caller leaves blank keeps whatever was previously
    saved. An explicit ``None``/``null`` on the specific fields called out
    above IS a field-level delete (finding 5/7's clear signal) — the one
    exception to "the form isn't the whole config"; everything else still
    has no way to be cleared through this function.

    ``written``/``removed`` only ever list fields that are actually
    confirmed on disk: every step-3 config.yaml edit (write OR removal) is
    buffered locally (``pending``/``pending_removed``) and only merged
    into ``written``/``removed`` after ``save_config(cfg)`` returns
    without raising, so a failed save can't leave the caller believing
    settings landed — or were cleared — that didn't.

    Fallback-chain dedup (step 3) keys on ``(provider, model)`` only — a
    narrower identity than the runtime resolver's
    (``hermes_cli.fallback_config._entry_identity``: ``(provider, model,
    base_url)``). Submitting the same provider/model with a different
    ``base_url`` through the wizard twice replaces the earlier entry in
    place rather than adding a second route; this is deliberate for a
    single-fallback-slot wizard form, but callers building a multi-entry
    UI on top of ``fallback_providers`` should be aware the two dedup
    rules don't match.
    """
    written: list = []
    removed: list = []
    errors: list = []
    # Non-fatal, Russian, safe-to-show-verbatim notices — distinct from
    # ``errors`` (which flips ``ok`` False and fails the whole submission
    # at ``app.py``'s "stage": "apply" branch). Same "settings are saved,
    # just not everything worked" posture as ``tool_install_failures``
    # (owner ruling 2026-08-24) — see the extract-backend-without-key
    # note below for the one producer today.
    warnings: list = []

    provider = form.get("provider") or {}
    provider_name = provider.get("name")
    fallback = form.get("fallback")
    # Generalized "key of the chosen web SEARCH row" (replaces the old
    # firecrawl-only `firecrawl_key` field — see this function's own
    # docstring). `app.py::_run_submit` already validated `key` against
    # the live "web" catalog's own env vars before this is ever reached.
    search_env = form.get("search_env") or {}
    search_env_key = search_env.get("key")
    search_env_value = search_env.get("value")
    # Its sibling for the SEPARATE "web_extract" block — validated the same
    # way against the live "web_extract" catalog.
    extract_env = form.get("extract_env") or {}
    extract_env_key = extract_env.get("key")
    extract_env_value = extract_env.get("value")

    # ---- Step 1: secrets --------------------------------------------------
    telegram_token = form.get("telegram_token")
    if telegram_token:
        _write_secret("TELEGRAM_BOT_TOKEN", telegram_token, written, errors, "токен Telegram")

    allowed_users = form.get("allowed_users")
    if allowed_users:
        _write_secret(
            "TELEGRAM_ALLOWED_USERS",
            _normalize_allowed_users(allowed_users),
            written,
            errors,
            "список пользователей Telegram",
        )
        # Домашний чат: адрес, по которому бот пишет клиенту САМ —
        # предупреждение о заканчивающемся месте, ежемесячная сводка
        # (hermes_cli/trix_disk_watch.py::send_to_home_channel). Без него
        # такие сообщения отправлять некуда, и они молча уходят в лог.
        # Записывается на каждом применении вместе со списком: клиент,
        # вернувшийся в мастер и сменивший список, не должен остаться с
        # адресом прежнего человека.
        #
        # Домашний чат — ВСЕГДА личка клиента, и только она. Продукт живёт
        # в группе с темами, но непрошеные сообщения адресованы человеку, а
        # не рабочей теме: в теме они были бы шумом для всех участников и
        # потерялись бы среди работы.
        #
        # Поэтому идентификатор темы обнуляется здесь же, а не оставляется
        # как есть. Шлюз читает адрес и тему из ДВУХ переменных, причём
        # переменные окружения перекрывают yaml
        # (gateway/config.py::_apply_env_overrides). Клиент, сделавший
        # /sethome в теме рабочей группы, оставлял в .env
        # TELEGRAM_HOME_CHANNEL_THREAD_ID=47; следующее сохранение настроек
        # переписывало адрес на личный, тему не трогало — и сообщение
        # уходило в личку с идентификатором чужой темы, то есть не доходило
        # вовсе, оставляя в журнале одну строку.
        #
        # /sethome у клиента остаётся: это его прямое действие, и оно
        # срабатывает. Просто следующее сохранение настроек возвращает
        # домашний чат в личку — оба значения переписываются вместе, а не
        # одно из двух.
        home_id = first_allowed_telegram_id(allowed_users)
        if home_id:
            _write_secret(
                "TELEGRAM_HOME_CHANNEL",
                home_id,
                written,
                errors,
                "домашний чат Telegram",
            )
            _write_secret(
                "TELEGRAM_HOME_CHANNEL_THREAD_ID",
                "",
                written,
                errors,
                "тему домашнего чата Telegram",
            )

    proxy = form.get("proxy")
    if proxy:
        _write_secret("TELEGRAM_PROXY", proxy, written, errors, "прокси Telegram")
        # Variant A (owner-approved): route the whole runtime through the
        # proxy, not just Telegram — every outbound HTTPS call (model
        # providers, web search/extract, ...) reads HTTPS_PROXY the same
        # way Telegram reads TELEGRAM_PROXY. NO_PROXY is never overwritten
        # wholesale — only guaranteed to still carry the hosts that are
        # reachable directly from an RU-hosted VM (see _DIRECT_HOSTS), on
        # top of whatever the user already has there.
        _write_secret("HTTPS_PROXY", proxy, written, errors, "HTTPS-прокси")
        current_no_proxy = load_env().get("NO_PROXY", "")
        _write_secret(
            "NO_PROXY",
            _merge_no_proxy_hosts(current_no_proxy),
            written,
            errors,
            "список прямых хостов (NO_PROXY)",
        )

    if provider_name and provider.get("env_var") and provider.get("api_key"):
        _write_provider_credential(
            provider["env_var"], provider["api_key"], written, errors, "ключ провайдера"
        )

    if fallback and fallback.get("env_var") and fallback.get("api_key"):
        _write_provider_credential(
            fallback["env_var"], fallback["api_key"], written, errors, "ключ резервного провайдера"
        )

    if search_env_key and search_env_value:
        _write_secret(search_env_key, search_env_value, written, errors, "ключ/адрес источника поиска")

    # Its "web_extract" sibling — skipped when its key duplicates
    # search_env's (the client picked the SAME provider for both search
    # and extract): search_env already wrote that credential above, so
    # writing it again here would be a harmless but pointless duplicate
    # .env write and a duplicate `written` entry (see this function's own
    # docstring's note on this).
    if extract_env_key and extract_env_value and extract_env_key != search_env_key:
        _write_secret(extract_env_key, extract_env_value, written, errors, "ключ/адрес источника чтения страниц")

    # Generalized "keys of the chosen provider-select rows" (tts/image_gen/
    # video_gen/x_search — see this function's own docstring). Each item's
    # `key` was already validated by `app.py::_run_submit` against the live
    # "изменить" catalog; a malformed item (not a dict, empty key/value) is
    # silently skipped rather than raised — same return-mode no-op contract
    # every other optional field here follows.
    #
    # x_search has no `tool_provider` entry to disambiguate (see this
    # function's own docstring / `_TOOL_PROVIDER_CONFIG_SECTIONS`) — a
    # non-empty x_search env var submitted here IS the category's whole
    # "the client configured this" signal, tracked so step 3 can grant the
    # matching toolset (finding 3 — see `_add_toolset`'s own docstring).
    x_search_env_var_keys = _x_search_env_var_keys()
    x_search_key_written = False
    for item in form.get("tool_env") or []:
        if not isinstance(item, dict):
            continue
        env_key = (item.get("key") or "").strip()
        env_value = item.get("value") or ""
        if env_key and env_value:
            _write_secret(env_key, env_value, written, errors, f"ключ {env_key}")
            if env_key in x_search_env_var_keys:
                x_search_key_written = True

    # ``hass`` defaults to ``{}`` at the app.py/pydantic layer (see
    # `_SubmitBody` — NOT ``None``, specifically so an explicit ``null`` is
    # unambiguous there): a real ``{"url": ..., "token": ...}`` writes; an
    # empty/missing dict is the ordinary no-op; ``None`` is the finding-5/7
    # clear signal — remove both secrets and, further down, the
    # "homeassistant" toolset. The ``"hass" in form`` guard covers the
    # OTHER caller shape this function must also stay backward-compatible
    # with: a bare dict built directly for this function (every existing
    # ``apply_settings()`` unit test) that simply doesn't have a "hass" key
    # at all — ``.get("hass")`` alone can't tell that apart from an
    # explicit ``None``, only membership can.
    # `hass_activated` (tracked for step 3's toolset grant) is true only
    # when BOTH halves of the credential were actually submitted this
    # round — a token with no URL (or vice versa) leaves nothing the
    # runtime can actually reach.
    hass_raw = form.get("hass") if "hass" in form else {}
    hass_clear = hass_raw is None
    hass = hass_raw or {}
    hass_token = hass.get("token")
    hass_url = hass.get("url")
    if hass_clear:
        _delete_secret("HASS_TOKEN", removed, errors, "токен Home Assistant")
        _delete_secret("HASS_URL", removed, errors, "адрес Home Assistant")
    else:
        if hass_token:
            _write_secret("HASS_TOKEN", hass_token, written, errors, "токен Home Assistant")
        if hass_url:
            _write_secret("HASS_URL", hass_url, written, errors, "адрес Home Assistant")
    hass_activated = bool(hass_token) and bool(hass_url)

    # Same pattern as HASS_URL above — see this function's own docstring
    # for why CAMOFOX_URL (not browser.cloud_provider) is the real Camofox
    # on/off switch. Empty/missing is a no-op (leaves whatever is already
    # saved untouched). ``None`` is the finding-5's clear signal — the ONE
    # way a client picking "Chromium (встроенный)" can actually turn
    # Camofox off; ``browser_backend`` (step 3, below) still writes
    # normally and independently of this. Same ``"camofox_url" in form``
    # membership guard as ``hass`` above, and for the identical reason: a
    # bare dict built directly for this function without a "camofox_url"
    # key at all must stay a no-op, not read as an explicit clear.
    camofox_url = form.get("camofox_url") if "camofox_url" in form else ""
    if camofox_url:
        _write_secret("CAMOFOX_URL", camofox_url, written, errors, "адрес Camofox")
    elif camofox_url is None:
        _delete_secret("CAMOFOX_URL", removed, errors, "адрес Camofox")

    # ---- Step 2: active provider -------------------------------------------
    # Tracked separately from `provider_name` truthiness: step 3's forced
    # model.default write (below) must never land if THIS step failed —
    # otherwise config.yaml could end up with the new provider's model name
    # but the OLD provider still active (model.provider untouched), a
    # mismatched pair (e.g. an OpenRouter-formatted model id sent to a
    # direct-API provider).
    provider_step_ok = False
    if provider_name:
        try:
            base_url = (provider.get("base_url") or "").strip() or _provider_default_base_url(provider_name)
            model = provider.get("model") or resolve_default_model(provider_name)
            _update_config_for_provider(provider_name, base_url, default_model=model or None)
            written.append("model.provider")
            provider_step_ok = True
        except Exception as exc:
            logger.warning("apply_settings: failed to apply provider", exc_info=True)
            errors.append(f"Не удалось применить провайдера: {exc}")

    # ---- Step 3: the rest of config.yaml, one load + one save -------------
    try:
        cfg = load_config()
        # Buffered separately from `written`: these edits only happened in
        # memory so far. Merging them into `written` now (before
        # save_config() below has actually run) would let a save failure
        # leave the caller believing settings landed on disk that didn't —
        # extend written with this list only after save_config() succeeds.
        pending: list = []
        # `pending`'s counterpart for config.yaml keys this call REMOVES
        # (finding 5/7's clear signal) rather than writes — buffered the
        # same way, only merged into `removed` (never `written`) after
        # `save_config()` succeeds. The `tool_provider.<category>: null`
        # branch further down is what actually appends to this today
        # (tts_voice no longer has a clear branch — see Finding 2's note
        # on this function's own docstring).
        pending_removed: list = []

        if fallback and fallback.get("name"):
            fb_name = fallback["name"]
            fb_model = fallback.get("model") or resolve_default_model(fb_name)
            if fb_model:
                entry: dict = {"provider": fb_name, "model": fb_model}
                fb_base_url = (fallback.get("base_url") or "").strip()
                if fb_base_url:
                    entry["base_url"] = fb_base_url.rstrip("/")

                chain = cfg.get("fallback_providers")
                if not isinstance(chain, list):
                    chain = []
                dedup_key = (fb_name, fb_model)
                for i, existing in enumerate(chain):
                    if isinstance(existing, dict) and (
                        existing.get("provider"),
                        existing.get("model"),
                    ) == dedup_key:
                        chain[i] = {**existing, **entry}
                        break
                else:
                    chain.append(entry)
                cfg["fallback_providers"] = chain
                pending.append("fallback_providers")

        search_backend = form.get("search_backend")
        if search_backend:
            cfg.setdefault("web", {})["search_backend"] = search_backend
            pending.append("web.search_backend")

        # Finding 1 (review 2026-08-26, owner-approved fix): explicit
        # ``None`` is the finding-5/7 clear signal for a previously-saved
        # extract backend (see ``_SubmitBody``'s own docstring on
        # ``extract_backend: str | None``) — same "<field> in form"
        # membership guard as ``camofox_url``/``hass`` above, and for the
        # identical reason: a bare dict built directly for this function
        # with no "extract_backend" key at all must stay a no-op, never
        # read as an explicit clear.
        #
        # Truthiness, not membership, gates the actual delete+report below
        # (Finding 9's own lesson, applied here): unlike ``camofox_url``
        # (a bare ``.env`` value with no config.yaml presence at all),
        # ``DEFAULT_CONFIG``'s own ``web`` section always carries an
        # ``extract_backend: ""`` placeholder key — ``load_config()``'s
        # deep-merge means ``"extract_backend" in web_section`` is True
        # even when the client never configured anything. Checking the
        # VALUE, not just the key, keeps a clear-on-nothing-saved
        # submission from claiming a removal that didn't happen.
        extract_backend = form.get("extract_backend") if "extract_backend" in form else ""
        extract_backend_cleared = extract_backend is None
        if extract_backend_cleared:
            web_section = cfg.get("web")
            if isinstance(web_section, dict) and web_section.get("extract_backend"):
                del web_section["extract_backend"]
                pending_removed.append("web.extract_backend")
            extract_backend = ""
        elif not extract_backend:
            # Finding 3 (review 2026-08-26, owner-approved fix): search and
            # extract are independent picks (this function's own
            # docstring), but a client who configures search with an
            # extract-CAPABLE backend and never opens the separate "Чтение
            # страниц" row must not silently lose the extraction the old
            # combined block used to give them for the exact same choice —
            # this is the ONLY place that implicitly defaults extract from
            # search, and only when nothing is already saved for extract
            # (a client who deliberately picked a DIFFERENT — or the same
            # — extract backend earlier keeps it: ``renderExtractBlock()``
            # preselects from ``current.extract_backend`` whenever it's
            # non-empty, so an already-saved choice never reaches this
            # branch as "" again) and the search credential is actually
            # usable (freshly written above, or already in ``.env``).
            already_saved_extract = (
                (cfg.get("web") or {}).get("extract_backend") if isinstance(cfg.get("web"), dict) else None
            )
            if (
                not already_saved_extract
                and search_backend
                and search_backend in _extract_capable_web_backends()
                and bool(load_env().get(_web_backend_primary_env_var(search_backend) or ""))
            ):
                extract_backend = search_backend

        # Finding 2 (review 2026-08-26, owner-approved fix): every backend
        # the "web_extract" catalog can offer (exa/firecrawl/parallel/
        # tavily) needs a credential — writing ``web.extract_backend``
        # without one would make config.yaml/the summary screen claim a
        # capability the agent doesn't actually have (the toolset auto-swap
        # further down gates on this SAME credential check, so it wouldn't
        # fire either — the client would see "Tavily" everywhere with no
        # ``web_extract`` tool ever granted). Skip the write instead, and
        # surface it as a non-fatal warning — same "settings saved, one
        # thing didn't fully apply" posture as ``tool_install_failures``
        # (owner ruling 2026-08-24) — rather than failing the whole
        # submission over one optional field.
        extract_credential_present = False
        if extract_backend:
            expected_extract_key = (
                extract_env_key
                or (search_env_key if search_backend == extract_backend else None)
                or _web_backend_primary_env_var(extract_backend)
            )
            extract_credential_present = bool(expected_extract_key) and bool(
                load_env().get(expected_extract_key)
            )
            if extract_credential_present:
                cfg.setdefault("web", {})["extract_backend"] = extract_backend
                pending.append("web.extract_backend")
            else:
                warnings.append(
                    "Чтение страниц: источник выбран, но подходящего ключа нет — "
                    "настройка не сохранена. Поиск в интернете продолжит работать как раньше."
                )

        browser_backend = form.get("browser_backend")
        if browser_backend:
            cfg.setdefault("browser", {})["backend"] = browser_backend
            pending.append("browser.backend")

        # Finding 2 (owner-approved fix): the "return to default" pick
        # ("Голос Светлана (по умолчанию, рекомендуется)") is no longer a
        # clear signal — it never was safe to delete the key, since
        # DEFAULT_CONFIG's own ``tts.edge.voice`` baseline is the English
        # ``en-US-AriaNeural``, not the Russian voice the label promises
        # (Trix's own template ships the Russian voice explicitly — see
        # ``assets/config/trix-config.yaml`` — but ``load_config()`` falls
        # back to upstream Hermes's ``DEFAULT_CONFIG`` the instant the key
        # is absent). The client now sends the literal constant name
        # (``page.py``'s ``VOICE_DEFAULT_NAME``) when the pick is a
        # deliberate return-to-default over a saved custom voice — this
        # function just writes whatever non-empty string it's handed, the
        # same "dumb writer" contract as every other secret/config field
        # here. Empty/missing stays the ordinary no-op (leave whatever is
        # already saved untouched).
        tts_voice = form.get("tts_voice") or ""
        if tts_voice:
            cfg.setdefault("tts", {}).setdefault("edge", {})["voice"] = tts_voice
            pending.append("tts.edge.voice")

        # Generalized "which row is ACTIVE" field for the three
        # provider-select categories that need one to disambiguate
        # (tts/image_gen/video_gen — see this function's own docstring).
        # Each value is the chosen row's own `provider_key`
        # (tools_view.py) — writing "<category>.provider" verbatim mirrors
        # tools_config._write_provider_config's own field names, so a row
        # picked here can never disagree with what `hermes tools` would
        # have written for the same row. Empty/missing is the same no-op
        # contract as every other optional field here.
        #
        # A single `platform_toolsets.telegram` mutation flag covers every
        # toolset add/remove/swap below (video_gen here, x_search/
        # homeassistant/firecrawl further down) — see `_add_toolset`'s own
        # docstring for why an explicit list needs this at all; only one
        # `pending` entry is appended regardless of how many of these fire
        # together. `pending_removed` (declared above, next to `pending`)
        # is `pending`'s counterpart for config.yaml keys this call REMOVES
        # (finding 5/7's clear signal) rather than writes — buffered the
        # same way, only merged into `removed` (never `written`) after
        # `save_config()` succeeds.
        platform_toolsets_changed = False

        tool_provider = form.get("tool_provider") or {}
        for cat_key, value in tool_provider.items():
            if value is None:
                # Finding 5/7's clear signal for this category (see
                # `_SubmitBody`'s own docstring in app.py): remove
                # "<category>.provider" from config.yaml — when that
                # category even has one, "x_search" doesn't — and revoke
                # the matching toolset grant, symmetric to the ADD branch
                # below.
                if cat_key in _TOOL_PROVIDER_CONFIG_SECTIONS:
                    section = cfg.get(cat_key)
                    if isinstance(section, dict) and "provider" in section:
                        del section["provider"]
                        pending_removed.append(f"{cat_key}.provider")
                toolset_name = _CATEGORY_TOOLSET.get(cat_key)
                if toolset_name and _remove_toolset(cfg, toolset_name):
                    platform_toolsets_changed = True
                continue
            if cat_key not in _TOOL_PROVIDER_CONFIG_SECTIONS or not value:
                continue
            cfg.setdefault(cat_key, {})["provider"] = value
            pending.append(f"{cat_key}.provider")
            # Finding 3 (owner-approved fix): symmetric with the REMOVE
            # branch above — every category that has a toolsets.py entry
            # (`_CATEGORY_TOOLSET`) gets it re-granted here, not just
            # "video_gen". Trix ships tts/image_gen already inside the
            # baseline `platform_toolsets.telegram`, so for a first-time
            # pick this is a same-value no-op (`_add_toolset` is
            # idempotent) — but it becomes load-bearing the moment an
            # EARLIER submission cleared that category via the null signal
            # above (`_remove_toolset`) and the client re-enables it: without
            # this, the category could be turned off but never back on.
            toolset_name = _CATEGORY_TOOLSET.get(cat_key)
            if toolset_name and _add_toolset(cfg, toolset_name):
                platform_toolsets_changed = True

        if x_search_key_written and _add_toolset(cfg, "x_search"):
            platform_toolsets_changed = True

        if hass_activated and _add_toolset(cfg, "homeassistant"):
            platform_toolsets_changed = True

        if hass_clear and _remove_toolset(cfg, "homeassistant"):
            platform_toolsets_changed = True

        # Web toolset auto-swap (search -> web, adds web_extract) — see
        # this function's own docstring for why this is keyed on EXTRACT
        # activation and not on `search_backend` alone (the old
        # firecrawl-only trigger this replaces — restored, generalized,
        # by the implicit-default branch above for the plain "picked
        # Firecrawl for search, never opened the extract row" case).
        # Two conditions, both required: `extract_backend` must actually
        # be able to extract (`_extract_capable_web_backends()` — this
        # function stays a "dumb writer" that never re-validates a
        # bogus/illegal backend name at the WRITE site above, since
        # catalog legality is app.py's job, but the toolset swap is NOT
        # optional here — granting `web_extract` for a backend that can't
        # extract would be a lie no matter how it got here), AND
        # `extract_credential_present` (computed above, at the SAME write
        # site that decides whether `web.extract_backend` itself gets
        # persisted, finding 2) — a config entry the agent can't actually
        # use must never grant the toolset that only makes sense alongside
        # it either, so this reuses that one determination rather than
        # recomputing a second, potentially-diverging copy of the same
        # "real credential" check.
        if (
            extract_backend in _extract_capable_web_backends()
            and extract_credential_present
            and swap_search_to_web(cfg)
        ):
            platform_toolsets_changed = True

        # Finding 1's clear-signal counterpart: turning "Чтение страниц"
        # off must actually revoke the `web_extract` tool an earlier
        # submission granted, not just remove the now-orphaned
        # `web.extract_backend` config value above — otherwise the agent
        # keeps advertising a capability the client just disabled.
        if extract_backend_cleared and swap_web_to_search(cfg):
            platform_toolsets_changed = True

        if platform_toolsets_changed:
            pending.append("platform_toolsets.telegram")

        # _update_config_for_provider (step 2) only overwrites an existing
        # model.default when it's empty or slash-formatted (its own
        # "don't clobber a hand-picked model" guard — see auth.py) — so a
        # form that explicitly names a model for a provider that already had
        # a plain (non-slash) default would otherwise be silently ignored.
        # When the form explicitly named a model, force it here so the
        # wizard's explicit intent always wins over that guard.
        explicit_model = (provider.get("model") or "").strip() if provider_name else ""
        if explicit_model and provider_step_ok:
            cfg.setdefault("model", {})["default"] = explicit_model
            pending.append("model.default")

        # Часовой пояс (спека 11). Корневой скаляр, а не секция, и НЕ
        # `.env`: переменная `HERMES_TIMEZONE` помечена в коде как
        # внутренняя и вычищается из окружения дочерних процессов, а по
        # общему правилу проекта `.env` держит только секреты.
        #
        # Пустое/отсутствующее значение — по общему контракту `_SubmitBody`
        # «не трогай», а не «сотри»: возвратный клиент, правящий один
        # прокси, не должен терять пояс. Обязательность поля обеспечивается
        # на входе (`_run_submit`), а не тем, что здесь затирается.
        # Легальность значения проверена там же; здесь — обычный писатель.
        timezone_value = (form.get("timezone") or "").strip()
        if timezone_value:
            cfg["timezone"] = timezone_value
            pending.append("timezone")

        save_config(cfg)
        written.extend(pending)
        removed.extend(pending_removed)
    except Exception as exc:
        logger.warning("apply_settings: failed to save config.yaml", exc_info=True)
        errors.append(f"Не удалось сохранить config.yaml: {exc}")

    return {"ok": not errors, "written": written, "removed": removed, "errors": errors, "warnings": warnings}
