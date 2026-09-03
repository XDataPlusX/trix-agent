"""Tool catalog view for the wizard's "изменить" block (spec §7.3, §15.2).

Renders all five of the wizard's level-2 controls (browser, search/extract,
voice, images, smart home) generically from
``hermes_cli.tools_config.TOOL_CATEGORIES`` —
the same catalog ``hermes tools`` / ``hermes setup tools`` build their own
menus from — plus whatever plugin-registered provider rows
``tools_config._visible_providers()`` would inject for that category
(FAL/OpenAI/Krea/… for image_gen, Browserbase/Firecrawl for browser, …).
Nothing here is a hand-copied snapshot of catalog data: ``_catalog()``
calls back into ``tools_config`` for the live structure so a new provider
shipped there shows up (or fails the completeness invariant, see below)
without touching this module.

The fifth level-2 control — "Поиск и извлечение страниц" (``web``) — is now
rendered the same generic way as the other four: straight from
``TOOL_CATEGORIES["web"]``'s live rows (plugin-registered search backends
included, via ``_visible_providers()`` -> ``_plugin_web_search_providers()``
same as every other category). The earlier v1 design hand-picked exactly
two backends (``ddgs``, ``firecrawl``) through a parallel, hardcoded
mechanism (``SEARCH_BACKENDS_RENDERED`` / ``EXCLUDED_SEARCH_BACKENDS``);
that mechanism is gone. Every backend the live registry carries — ddgs,
brave-free, exa, firecrawl, parallel, tavily, xai, searxng — is now a
regular catalog row, resolved by the SAME rules as browser/tts/image_gen
rows: the nous rule cuts ``requires_nous_auth``/nous-plugin rows, and a
NEW structural rule (below) cuts a "self-hosted" row with nothing
listening at its local address, so the wizard never shows a dead-end
choice. ``ddgs`` is marked ``recommended`` structurally (by
``web_backend``, not by badge text — its catalog badge says "free · no
key · search only", never the literal word "recommended"), matching the
free, no-setup default every other category's ``recommended`` row already
gets.

Structural "self-hosted" rule: a row is hidden when its badge contains
"self-hosted" AND it carries no ``post_setup`` hook AND nothing answers a
GET at its local address (``_local_service_alive()``, 1s timeout,
tolerant of any response — the point is only "is something listening
here"). Two rows in the live catalog qualify today: "SearXNG"
(``SEARXNG_URL``, no catalog default -> probed at the well-known
``http://127.0.0.1:8080``) and "Firecrawl Self-Hosted" (``FIRECRAWL_API_URL``,
probed at ``http://localhost:3002`` — see ``_KNOWN_LOCAL_URLS`` for both).
A row that passes the probe is rendered with its URL env var carrying a
``default`` equal to the address that just answered, so the page can
prefill it — same shape ``CAMOFOX_URL``'s catalog entry already uses for
its own (hardcoded) default. The probe never runs for a row that already
has ``post_setup`` (installable rows aren't gated this way — matching
every other category) or isn't tagged "self-hosted" at all (Brave/Exa/
Firecrawl-cloud/Parallel/Tavily/xAI always render). Caching: the probe
rides on whatever caches ``wizard_tool_blocks()`` itself — ``app.py``'s
``app.state.tools_cache`` for the live server; a caller that wants a
fresh probe already has to bust that cache the same way it busts every
other "installed" verdict. WITHIN one call, a local ``alive_cache`` dict
(finding 10, review 2026-08-26) memoizes by probe URL — the 2026-08-26
search/extract split means a row like "Firecrawl Self-Hosted" renders in
BOTH the "web" and "web_extract" specs, and without this it paid the
full liveness probe (and its 1s worst-case timeout) once per category
instead of once per distinct address.

Structural "OAuth-only" rule (all categories): a row is hidden when its
``post_setup`` hook names an INTERACTIVE OAuth/CLI-login flow (today, the
only member is ``"xai_grok"`` — ``hermes auth`` device-code login, run over
stdin/a browser tab the web wizard has no way to drive) AND the row carries
no ``env_vars`` at all, i.e. there is no alternative "just paste a key"
path either — the class Spotify's OAuth-redirect-must-be-localhost row
already illustrates (see ``EXCLUDED_CATEGORIES["spotify"]``), generalized
to a per-ROW rule since ``xai_grok`` shows up on rows spread across four
different categories (``tts``: "xAI TTS", ``stt``: "xAI", ``image_gen``:
"xAI Grok Imagine (image)", ``video_gen``: "xAI Grok Imagine", ``x_search``:
"xAI Grok OAuth (SuperGrok / Premium+)") rather than on one category as a
whole. A row with BOTH ``post_setup`` and real ``env_vars`` (e.g. Camofox's
``CAMOFOX_URL``) is never touched by this rule — it has a working
key/address path regardless of the interactive hook. See
``_is_oauth_only_row()``/``_INTERACTIVE_LOGIN_POST_SETUP_HOOKS`` below.
This rule is intentionally NOT the same thing as "any keyless post_setup
row" — KittenTTS/Piper (local installs, ``post_setup`` but no env_vars)
must keep rendering with their install button; only a hook actually named
in ``_INTERACTIVE_LOGIN_POST_SETUP_HOOKS`` is a login flow the web wizard
cannot drive.

"web" is the one category deliberately EXEMPT from this rule at its call
site in ``wizard_tool_blocks()``: its own "xAI Web Search (Grok)" row has
the identical shape (``post_setup="xai_grok"``, no ``env_vars``) but
already has a working, TESTED carve-out that predates this rule
(``renderSearchBlock()``'s ``row.post_setup === "xai_grok"`` branch in
page.py points the client at the provider-step xAI key instead of hiding
the row) — an intentional design, not a gap this rule should paper over.
Every other category (tts, stt, image_gen, video_gen, x_search) has no
such carve-out, so the rule applies there in full.

"OpenAI (Codex auth)" (``image_gen``) is a related-looking but structurally
DIFFERENT case: it carries neither ``env_vars`` NOR a ``post_setup`` hook at
all (only an informational ``post_setup_hint`` string the plugin schema
sets, which ``tools_config._plugin_image_gen_providers()``'s row builder
doesn't even copy into the row dict — see that function). Because the
OAuth-only rule keys on ``post_setup`` membership in the interactive-login
set, an unset ``post_setup`` can never match it — this row is NOT hidden by
that rule, structurally, not by luck. It used to be a static
``EXCLUDED_ROWS`` entry; owner ruling 2026-08-20 flipped that: it activates
via the ChatGPT/Codex OAuth login already run in step 4 ("Провайдер") —
nothing left to configure here — so it now renders with an informational
hint (``page.py``'s ``renderImageGenBlock()``) instead of being excluded.

Structural "browser activation path" rule: a "browser" row is hidden when
it has neither a ``backend_key`` (``browser_backend``, or
``browser_provider == "local"``) nor a ``CAMOFOX_URL`` env var — i.e. no
field the wizard's own ``apply_settings()`` (apply.py) actually writes
anywhere. Two live rows fail this today: "Browserbase" and "Firecrawl"
(browser-plugin cloud providers, both carrying only ``browser_provider``,
which targets ``browser.cloud_provider`` — a config.yaml key
``apply_settings()`` has no write branch for). "Local Browser", "Camofox",
and "Browser Use" all pass (see ``_browser_row_has_activation_path()`` for
the exact rule and why it is keyed on structural markers, not on either
row's display name) — owner ruling 2026-08-20: show those three, hide the
rest until a ``browser.cloud_provider`` write path exists.

Invariant (spec §15.2, enforced by
``tests/hermes_cli/test_setup_wizard_tools_view.py``): every row of every
*non-excluded* category in the live catalog must be either rendered by
``wizard_tool_blocks()`` or listed in ``EXCLUDED_ROWS`` with a reason.
Rows carrying ``requires_nous_auth`` are cut by a structural rule, not an
enumeration — spec §7.3 counts six such rows today; the live catalog
(which, unlike the spec's table, also sees plugin-injected rows) has a
seventh: an image_gen plugin's own "Nous Portal" row, whose
``get_setup_schema()`` sets ``requires_nous_auth`` but whose
``tools_config._plugin_image_gen_providers()`` builder drops that key when
assembling the row dict. ``_is_nous_plugin_row()`` catches it structurally
via the row's plugin-identity marker (``image_gen_plugin_name == "nous"``,
and its siblings for the other plugin-backed categories) instead of by its
(renameable) display name, so a title change upstream can't silently let it
back in. Likewise every category in the catalog must be in
``WIZARD_TOOL_CATEGORIES`` or ``EXCLUDED_CATEGORIES``.

Split into "Поиск в интернете" / "Чтение страниц" (2026-08-26): the
runtime has supported independent search/extract backends for a while
(``tools/web_tools.py``'s ``_get_search_backend()``/``_get_extract_backend()``
read ``web.search_backend``/``web.extract_backend`` before falling back to
the shared ``web.backend``), but this wizard used to render ONE "web"
block and write ONE ``search_backend`` field for both capabilities. Not
every provider can extract — ``tools/web_tools.py:874-892`` returns a
"search-only backend" error for a ``web_extract`` call against
ddgs/brave-free/searxng/xai — and ``ddgs`` (search-only) was the block's
own *recommended* default, so a fresh install got an agent that could
search but errored in English the first time it tried to read a page.

"web" (rendered here as key ``"web"``, still) now carries every row that
can *search* — unchanged, every registered provider defaults to
``supports_search() == True`` (``agent/web_search_provider.py``). A second,
DERIVED category, key ``"web_extract"``, carries only the rows whose
backend's ``supports_extract()`` is True — resolved structurally via
``tools_config.web_provider_capabilities(backend)`` (the exact function
the dashboard's "Capabilities" panel already uses,
``hermes_cli/web_routers/tools.py``), never a hardcoded name list. Today
that's firecrawl/tavily/exa/parallel; ddgs/brave-free/searxng/xai never
appear in ``web_extract`` — picking one there would just reproduce the
same runtime error the split exists to prevent. ``"web_extract"`` is not a
real ``tools_config.TOOL_CATEGORIES`` key — ``_catalog()`` derives it from
``catalog["web"]``'s own rows (see its own docstring) so both blocks stay
byte-identical for any row that supports both capabilities (Firecrawl,
including its self-hosted row) and there is exactly one place — the web
provider's own ``supports_extract()`` — that decides which capability a
backend gets credit for.

Owner ruling 2026-08-24 (client-VM walkthrough — Camofox/Browser Use both
require an npm/uv toolchain the machine didn't have, and the wizard gave
no honest signal why "Установить" did nothing) added four things, each
documented at its own definition:

1. ``env_vars[].auto_default`` — a known local/standard address (Camofox's
   fixed 9377 port, or a self-hosted row's own liveness-probe result) the
   client should submit without ever asking the user to type it. See
   ``_AUTO_DEFAULT_URL_SUFFIX``.
2. ``rows[].beta`` / ``beta_note_ru`` — Camofox and Browser Use flagged
   with a short Russian warning. See ``_BROWSER_BETA_NOTES_RU``.
3. ``run_tool_install()`` now returns a structured
   ``{"ok", "reason", "message"}`` verdict instead of ``None`` — see its
   own docstring and ``app.py``'s ``/api/install`` handler for how the
   message reaches the client.
4. ``rows[].voices`` / ``default_voice`` — the real Edge TTS ru-RU voice
   set, so the client can offer a choice instead of one hardcoded name.
   See ``EDGE_TTS_RU_VOICES``.
"""
from __future__ import annotations

import logging
import urllib.request

from hermes_cli import tools_config as _tc
from hermes_cli.config import load_config
from hermes_cli.nous_subscription import NousFeatureState, NousSubscriptionFeatures

logger = logging.getLogger(__name__)

# Categories rendered as level-2 wizard controls (spec §7.3 "Состав блока").
# "web" (Поиск и извлечение страниц) was the first to move out of
# EXCLUDED_CATEGORIES — see the module docstring for why the old parallel
# search-backend mechanism is gone and how the "self-hosted" structural
# rule replaces it. "video_gen" (Генерация видео) and "x_search" (Поиск по
# X (Twitter)) followed the same path (owner ruling 2026-08-20): both used
# to be excluded only because their sole activation path was an
# unconfigurable Nous/OAuth row — the structural OAuth-only rule (module
# docstring) now hides exactly that row and leaves a real BYOK row
# (FAL/DeepInfra for video_gen, XAI_API_KEY for x_search) behind, so there
# is something legitimate left to offer the client. "stt" (Распознавание
# речи) joined 2026-08-20: the decision it was waiting on
# (EXCLUDED_CATEGORIES used to read "ждёт решения по российским
# STT-сервисам") is resolved by the bundled Nexara STT plugin
# (plugins/stt/nexara) — a Russian-hosted, RU-reachable-without-a-proxy
# STT backend. Unlike tts/image_gen/video_gen/web/browser,
# tools_config.py has no ``_plugin_stt_providers()`` registry injection
# (upstream gap, left untouched here) — so ``_catalog()`` below unions in
# registry-driven rows itself (see ``_stt_registry_rows()``) rather than
# waiting on that upstream change.
#
# "x_search" (Поиск по X (Twitter)) and "homeassistant" (Умный дом) LEFT
# this tuple 2026-08-23 (spec A5, owner ruling): both are rare enough that
# the wizard shouldn't spend a whole slide/row on them — they're configured
# later via ``hermes tools`` (CLI) instead. This is a WIZARD-ONLY retreat:
# the underlying config/toolset write-and-clear paths
# (``apply.py``'s ``hass``/``tool_provider.x_search`` handling,
# ``_tc.TOOL_CATEGORIES["x_search"]``/``["homeassistant"]`` themselves)
# are untouched and still fully exercised by the CLI and by
# ``app.py``'s own submit-time ``null``-clear-signal tests — only this
# module stops RENDERING the two categories as wizard rows. See
# EXCLUDED_CATEGORIES below for the reason strings the completeness
# invariant (``test_every_catalog_row_resolved``) requires.
WIZARD_TOOL_CATEGORIES: tuple[str, ...] = (
    "browser",
    "web",
    "web_extract",
    "tts",
    "stt",
    "image_gen",
    "video_gen",
)

# The two web-capability categories, treated identically by every
# structural rule below (self-hosted liveness gate, OAuth-only exemption)
# — see the module docstring's "Split into Поиск в интернете / Чтение
# страниц" paragraph. "web_extract" is not a real
# tools_config.TOOL_CATEGORIES key; _catalog() derives it from "web"'s own
# rows.
_WEB_CATEGORIES: tuple[str, ...] = ("web", "web_extract")

# category key -> причина исключения. Дословные формулировки спеки §7.3
# ("Исключено из блока, с причинами") где применимо; остальные —
# датированные owner-ruling записи по мере открытия/закрытия категорий.
EXCLUDED_CATEGORIES: dict[str, str] = {
    "computer_use": "GUI-инструмент; на Linux-VPS без дисплея не работает (спека 4 уже убрала тулсет)",
    "langfuse": "операторская телеметрия, не клиентская ценность (ruling 2026-08-20)",
    "spotify": (
        "OAuth-редирект обязан указывать на localhost (auth.py:2942) — из браузера "
        "клиента редирект уйдёт на машину клиента, а не на VM; в веб-мастере поток не завершается"
    ),
    "x_search": (
        "редкая категория — убрана из мастера, настраивается позже через "
        "hermes tools (спека A5, owner ruling 2026-08-23); серверные пути "
        "записи/очистки tool_provider.x_search сохранены для CLI"
    ),
    "homeassistant": (
        "редкая категория — убрана из мастера, настраивается позже через "
        "hermes tools (спека A5, owner ruling 2026-08-23); серверные пути "
        "записи/очистки HASS_TOKEN/HASS_URL сохранены для CLI"
    ),
}

# (category, имя строки каталога) -> причина. Строки с requires_nous_auth
# (или с image_gen_plugin_name == "nous" — см. _is_nous_plugin_row() ниже)
# НЕ перечисляются здесь — см. wizard_tool_blocks(), они режутся правилом.
# Self-hosted "web" rows (SearXNG, Firecrawl Self-Hosted) are ALSO not
# listed here even though they can render as hidden — their visibility is
# a runtime liveness fact, not a permanent exclusion; see
# _is_self_hosted_row()/_local_service_alive() below. "browser" rows with
# no activation path (Browserbase, Firecrawl cloud) are a third such
# structural carve-out — see _browser_row_has_activation_path() below and
# the module docstring's "browser activation path" paragraph. OAuth-only
# rows (module docstring's "OAuth-only" paragraph — xAI TTS/STT/Grok
# Imagine/Grok OAuth) are a FOURTH structural carve-out, see
# _is_oauth_only_row() below — likewise not listed here.
#
# "OpenAI (Codex auth)" used to be a static entry here (no env_vars, no
# post_setup hook to run). Owner ruling 2026-08-20 reversed that: it
# activates through the ChatGPT/Codex OAuth login already completed in
# step 4 ("Провайдер"), so the row now renders with an informational hint
# (page.py's renderImageGenBlock()) instead of being excluded — see the
# module docstring's paragraph on this row for why the OAuth-only rule
# above structurally can't (and shouldn't) catch it either.
EXCLUDED_ROWS: dict[tuple[str, str], str] = {}

# Local addresses to probe for a "self-hosted" web row whose catalog entry
# doesn't carry a "default" on its own *_URL env var (unlike CAMOFOX_URL,
# which does) — the well-known default port/path each project documents
# for a local instance. Used ONLY to decide whether the row is worth
# showing at all; never written anywhere unless the probe succeeds (the
# row is then rendered with this address as its field's prefilled value).
_KNOWN_LOCAL_URLS: dict[str, str] = {
    "SEARXNG_URL": "http://127.0.0.1:8080",
    "FIRECRAWL_API_URL": "http://localhost:3002",
}

# Owner ruling 2026-08-24 (client-VM walkthrough, finding 1): don't ask the
# client to type in a local service's address when the machine already
# knows the answer — either it's a fixed, documented port every install of
# the tool uses (Camofox always listens on 9377 — CAMOFOX_URL's catalog
# entry carries that as a static `default`, see TOOL_CATEGORIES["browser"]
# in tools_config.py), or the wizard's own liveness probe just found
# something answering there (SearXNG/Firecrawl Self-Hosted, via
# ``_self_hosted_probe_url``/``_local_service_alive`` above). Both cases
# already converge on the SAME observable fact by the time
# ``wizard_tool_blocks()`` builds a row: the env var's ``default`` is set.
# So rather than a second hardcoded key list to keep in sync with
# ``_KNOWN_LOCAL_URLS``, the rule is structural: any ``*_URL`` env var that
# ends up carrying a ``default`` gets ``auto_default`` stamped onto it too
# (see the bottom of ``wizard_tool_blocks()``). Verified today to fire on
# exactly these three keys — HASS_URL/HERMES_LANGFUSE_BASE_URL also carry a
# static catalog ``default`` but live in categories the wizard never
# renders (homeassistant/langfuse — see EXCLUDED_CATEGORIES), so they never
# reach this code path.
#
# Contract for the client (not implemented here — see CLAUDE.md's
# "page.py NOT touch" scope note): an env var with ``auto_default`` set
# should NOT get a visible text input in the main flow — the wizard already
# knows the value and should submit it untouched. A manual override should
# stay reachable somewhere less prominent (e.g. a small "указать другой
# адрес" link that reveals the field, pre-filled with ``auto_default``) —
# see the module docstring's "Auto-default contract" paragraph below for
# the full write-up.
_AUTO_DEFAULT_URL_SUFFIX = "_URL"

TITLES_RU: dict[str, str] = {
    "browser": "Браузер",
    "web": "Поиск в интернете",
    "web_extract": "Чтение страниц",
    "tts": "Голосовые ответы",
    "stt": "Распознавание речи",
    # Kept in sync with page.py's own hardcoded headings (page.py is the
    # text source of truth — see the finding-16 fix in ``wizard_tool_blocks()``
    # below: the client now consumes ``title_ru`` instead of a second,
    # independently-hardcoded string, so a future edit here is what
    # actually reaches the page).
    "image_gen": "Генерация изображений",
    "video_gen": "Генерация видео",
    "x_search": "Поиск по X (Twitter)",
    "homeassistant": "Умный дом",
}

# env-var key -> Russian field label. tools_config.TOOL_CATEGORIES and every
# plugin's get_setup_schema() write ``env_vars[].prompt`` in English ("OpenAI
# API key", "Camofox server URL", …) — fine for `hermes tools`' English CLI,
# wrong for this Russian, brandless web wizard (finding 9). Keyed by env var
# key (not by row/category) because several rows across different
# categories share the same key with slightly different English wording
# (FAL_KEY: "FAL API key" in image_gen, "FAL.ai API key" in video_gen) — one
# Russian label per key keeps that consistent instead of forking by prose.
# Covers every env var that appears anywhere in WIZARD_TOOL_CATEGORIES's
# live catalog today (tests/hermes_cli/test_setup_wizard_tools_view.py
# enforces this as a completeness invariant, not a snapshot of the list —
# see its own docstring). CAMOFOX_URL/HASS_TOKEN/HASS_URL are included even
# though page.py currently renders those three with its own hardcoded
# Russian labels rather than reading ``prompt_ru`` — see renderBrowserBlock()/
# renderHomeAssistantBlock() — so the dict stays a complete, key-addressable
# map of the whole catalog rather than "whatever page.py happens to read
# today".
RU_ENV_PROMPTS: dict[str, str] = {
    "VOICE_TOOLS_OPENAI_KEY": "Ключ OpenAI API",
    "OPENAI_API_KEY": "Ключ OpenAI API",
    "ELEVENLABS_API_KEY": "Ключ ElevenLabs API",
    "MISTRAL_API_KEY": "Ключ Mistral API",
    "GEMINI_API_KEY": "Ключ Gemini API",
    "DEEPINFRA_API_KEY": "Ключ DeepInfra API",
    "GROQ_API_KEY": "Ключ Groq API",
    "FIRECRAWL_API_URL": "Адрес сервера Firecrawl (например, http://localhost:3002)",
    "FIRECRAWL_API_KEY": "Ключ Firecrawl API (оставьте пустым для self-hosted)",
    "XAI_API_KEY": "Ключ xAI API",
    "OPENROUTER_API_KEY": "Ключ OpenRouter API",
    "FAL_KEY": "Ключ FAL API",
    "KREA_API_KEY": "Ключ Krea API",
    "PARALLEL_API_KEY": "Ключ Parallel API",
    "EXA_API_KEY": "Ключ Exa API",
    "BRAVE_SEARCH_API_KEY": "Ключ Brave Search API (бесплатный тариф)",
    "TAVILY_API_KEY": "Ключ Tavily API",
    "SEARXNG_URL": "Адрес сервера SearXNG (например, http://localhost:8080)",
    "NEXARA_API_KEY": "Ключ Nexara API",
    "CAMOFOX_URL": "Адрес сервера Camofox",
    "HASS_TOKEN": "Токен Home Assistant",
    "HASS_URL": "Адрес Home Assistant",
}

def _disabled_feature(key: str) -> NousFeatureState:
    """A "not subscribed, nothing active" state for one managed-tool capability."""
    return NousFeatureState(
        key=key,
        label=key,
        included_by_default=False,
        available=False,
        active=False,
        managed_by_nous=False,
        direct_override=False,
        toolset_enabled=False,
        current_provider="",
        explicit_configured=False,
    )


# Deterministic "nobody is logged into Nous" stub. Passed explicitly to
# tools_config's private helpers so they never call
# get_nous_subscription_features()/get_nous_portal_account_info() — those can
# reach the network. All six (well, seven — see module docstring) managed
# rows carry `managed_nous_feature`, which _visible_providers() keeps visible
# regardless of auth state, so this stub does not hide anything the
# completeness invariant needs to see.
#
# ``features`` must carry every key NousSubscriptionFeatures.items() (and its
# per-capability properties: .web/.image_gen/.video_gen/.tts/.stt/.browser/
# .modal) can be asked for — an empty dict here would KeyError the moment any
# code path (present or future) reads one of those properties instead of
# .account_info/.nous_auth_present directly.
_NO_AUTH_FEATURES = NousSubscriptionFeatures(
    subscribed=False,
    nous_auth_present=False,
    provider_is_nous=False,
    features={
        key: _disabled_feature(key)
        for key in ("web", "image_gen", "video_gen", "tts", "stt", "browser", "modal")
    },
    account_info=None,
)


def _stt_registry_rows() -> list[dict]:
    """Registry-driven STT plugin rows, unioned into the "stt" category.

    Unlike tts/image_gen/video_gen/web/browser, ``tools_config.py`` has no
    ``_plugin_stt_providers()`` injection into ``_visible_providers()`` —
    the "stt" category's ``providers`` list there is a fully static,
    hand-written array that never reads ``agent.transcription_registry``
    (confirmed by grep: no ``_plugin_stt_providers`` symbol exists there).
    Fixing that belongs in ``tools_config.py``, which this module does not
    touch — so this is our own edge-local union: every REGISTERED
    :class:`~agent.transcription_provider.TranscriptionProvider` plugin
    (today: Nexara, ``plugins/stt/nexara``) becomes a row here, on top of
    whatever the static list already has.

    No collision is possible with a static row: ``register_provider()``
    itself refuses any name in its built-in-shadow list (``local``,
    ``local_command``, ``groq``, ``openai``, ``mistral``, ``xai``,
    ``elevenlabs``, ``deepinfra`` — see ``agent/transcription_registry.py``),
    and those are exactly the ``stt_provider`` values every static row in
    ``TOOL_CATEGORIES["stt"]["providers"]`` carries.

    Never raises — a broken plugin's ``get_setup_schema()`` is skipped
    (logged at debug), not allowed to break the whole "stt" block.
    """
    try:
        from agent.transcription_registry import list_providers
    except Exception:
        logger.debug("Could not import agent.transcription_registry", exc_info=True)
        return []
    rows: list[dict] = []
    for provider in list_providers():
        try:
            schema = provider.get_setup_schema()
        except Exception:
            logger.debug(
                "get_setup_schema() failed for STT plugin provider %r",
                provider, exc_info=True,
            )
            continue
        if not isinstance(schema, dict):
            continue
        rows.append(
            {
                "name": schema.get("name") or provider.display_name,
                "badge": schema.get("badge") or "",
                "tag": schema.get("tag") or "",
                "env_vars": schema.get("env_vars") or [],
                "post_setup": schema.get("post_setup"),
                # Same marker name the static "stt" rows carry — see
                # _PROVIDER_KEY_MARKERS — so _row_provider_key("stt", ...)
                # resolves plugin rows identically to built-in ones.
                "stt_provider": provider.name,
            }
        )
    return rows


def _catalog() -> dict:
    """Live tool catalog: ``TOOL_CATEGORIES`` with plugin rows resolved in.

    Calls back into ``tools_config._visible_providers()`` (the exact
    function ``hermes tools`` uses) per category instead of copying its
    static ``providers`` list, so plugin-registered rows (FAL, Browserbase,
    …) are visible to the completeness invariant too — not just the
    hand-written literal. "stt" additionally gets ``_stt_registry_rows()``
    unioned in — see that function's own docstring for why this category
    needs a local union instead of an upstream ``_visible_providers()``
    branch like every other provider-select category has.
    """
    catalog: dict = {}
    for key, spec in _tc.TOOL_CATEGORIES.items():
        rows = _tc._visible_providers(spec, {}, features=_NO_AUTH_FEATURES)
        if key == "stt":
            rows = list(rows) + _stt_registry_rows()
        catalog[key] = {**spec, "providers": rows}

    # "web_extract" (Чтение страниц) — derived, not a real TOOL_CATEGORIES
    # key. Every row of "web" whose OWN backend can extract pages
    # (web_provider_capabilities() consults the live web_search_registry's
    # supports_extract() flag — never a hardcoded name list here) — see
    # the module docstring's "Split into Поиск в интернете / Чтение
    # страниц" paragraph for why. A row with no "web_backend" marker at
    # all (shouldn't happen for "web" — every row there carries one, built
    # or plugin — but guarded defensively) is never included: there is no
    # backend identity to ask the registry about.
    web_spec = catalog.get("web")
    if web_spec is not None:
        extract_rows = [
            row
            for row in web_spec.get("providers", [])
            if row.get("web_backend") and "extract" in _tc.web_provider_capabilities(row["web_backend"])
        ]
        catalog["web_extract"] = {**web_spec, "providers": extract_rows}
    return catalog


# Plugin-identity markers tools_config's _plugin_*_providers() helpers stamp
# onto a row (see tools_config.py:2869-3084) to say which registry entry it
# came from. A plugin's own get_setup_schema() can set requires_nous_auth on
# itself (e.g. agent.image_gen_registry's "nous" provider), but the
# _plugin_image_gen_providers() builder does not copy that key into the row
# dict it returns — only name/badge/tag/env_vars/post_setup. Catching the
# provider identity instead of the (renameable) display name keeps the
# exclusion alive across a title change upstream; see task-5 report for the
# upstream gap this works around.
_NOUS_PLUGIN_MARKERS: tuple[str, ...] = (
    "image_gen_plugin_name",
    "video_gen_plugin_name",
    "web_search_plugin_name",
    "browser_plugin_name",
    "tts_plugin_name",
)


def _is_nous_plugin_row(provider: dict) -> bool:
    """True for a plugin-injected row whose underlying provider is "nous"."""
    return any(provider.get(marker) == "nous" for marker in _NOUS_PLUGIN_MARKERS)


# post_setup hook names that drive an INTERACTIVE OAuth/CLI-login flow the
# web wizard cannot run (no stdin, no way to open a device-code browser tab
# on the client's behalf) — see the module docstring's "OAuth-only"
# paragraph. Deliberately a narrow allowlist, not "any keyless post_setup
# row": KittenTTS/Piper/faster_whisper/camofox are also keyless-or-mixed
# post_setup hooks, but they drive a LOCAL install script, not a login —
# they must keep rendering with their install button.
_INTERACTIVE_LOGIN_POST_SETUP_HOOKS: frozenset[str] = frozenset({"xai_grok"})


def _is_oauth_only_row(provider: dict) -> bool:
    """True for a row whose only activation path is an interactive
    OAuth/CLI login this web wizard cannot drive — see
    ``_INTERACTIVE_LOGIN_POST_SETUP_HOOKS`` and the module docstring's
    "OAuth-only" paragraph. A row that ALSO carries ``env_vars`` (a
    plain key/address path) is never caught by this — only ``post_setup``
    with nothing else to fall back on.
    """
    return bool(provider.get("post_setup") in _INTERACTIVE_LOGIN_POST_SETUP_HOOKS and not provider.get("env_vars"))


# category -> marker keys (checked in order) whose value is the config
# value ``apply_settings()`` must write to that category's own
# "<category>.provider" field to make a row's choice the ACTIVE one —
# mirrors ``tools_config._write_provider_config``/``_configure_provider``'s
# own field names (``tts_provider``, ``image_gen_plugin_name`` /
# ``imagegen_backend`` — see ``TOOL_CATEGORIES["image_gen"]``'s "Nous
# Subscription" row and ``_plugin_image_gen_providers()`` for the two
# sources, ``video_gen_plugin_name``), read directly off the row instead of
# re-deriving them, so this can never disagree with what
# ``tools_config.py`` itself would write for the same row.
#
# "x_search" deliberately has NO entry here: its one live row (after the
# OAuth-only rule strips "xAI Grok OAuth (SuperGrok / Premium+)") needs no
# disambiguating config key at all — activation is credential-presence-only
# (``tools_config._xai_credentials_present()``), so there is nothing for
# this function to resolve for that category; every x_search row's
# ``provider_key`` is ``None`` by construction. "stt" carries
# ``stt_provider`` on every row — both the static built-in rows
# (``TOOL_CATEGORIES["stt"]["providers"]``) and the plugin-injected ones
# ``_stt_registry_rows()`` adds — mirroring ``tts_provider``'s shape
# exactly, since STT (like TTS) always has an active default provider
# ("local"), never an "off" state.
_PROVIDER_KEY_MARKERS: dict[str, tuple[str, ...]] = {
    "tts": ("tts_provider",),
    "stt": ("stt_provider",),
    "image_gen": ("image_gen_plugin_name", "imagegen_backend"),
    "video_gen": ("video_gen_plugin_name",),
}


def _row_provider_key(cat_key: str, provider: dict) -> str | None:
    """The machine value ``form.tool_provider[cat_key]`` must carry to make
    this row the active one for its category, or ``None`` when the
    category has no such disambiguating field (see
    ``_PROVIDER_KEY_MARKERS``'s own docstring for "x_search") or the row
    itself carries none of the category's markers.
    """
    for marker in _PROVIDER_KEY_MARKERS.get(cat_key, ()):
        value = provider.get(marker)
        if value:
            return value
    return None


def _installed(provider: dict, config: dict) -> bool | None:
    """Wrap ``tools_config.provider_readiness_status`` as a tri-state bool.

    ``None`` only on an unexpected error in the readiness probe itself —
    every normal outcome (ready / needs_keys / needs_auth / needs_setup)
    resolves to a plain bool.
    """
    try:
        status = _tc.provider_readiness_status(provider, config, features=_NO_AUTH_FEATURES)
    except Exception:
        logger.debug("provider_readiness_status(%s) failed", provider.get("name"), exc_info=True)
        return None
    return status == "ready"


# ``browser.backend`` (the ONLY browser-related key ``apply_settings()``
# — hermes_cli/setup_wizard/apply.py step 3 — actually writes) has a
# narrow, specific domain: ``tools/browser_use_cli.py`` defines
# ``BACKEND_DISABLED = "off"`` (built-in browser_* tools over whatever
# Chromium the installer set up) and ``_BACKEND_KEY = "browser-use"``
# (routes through the Browser Use CLI 3.0 instead). An unset/empty value
# is a THIRD state ("auto": Browser Use if the CLI is runnable, built-in
# tools otherwise) — never submitted by the wizard, which always sends an
# explicit choice once the browser block has been touched.
#
# That domain does NOT include "local" or "camofox" — those select a
# DIFFERENT config key, ``browser.cloud_provider`` (written by
# ``tools_config._write_provider_config``'s ``browser_provider`` branch,
# not by anything ``apply_settings()`` calls). Catalog rows carry
# ``browser_provider`` (cloud_provider selection) and/or
# ``browser_backend`` (backend selection) as two independent markers —
# see hermes_cli/tools_config.py:632-668's "browser" category comment.
#
# "Local Browser" (``browser_provider == "local"``) is the one row this
# module deliberately re-maps: it has no ``browser_backend`` marker of
# its own, but it is both the wizard's recommended default AND exactly
# what ``assets/config/trix-config.yaml`` ships as
# ``browser.backend: "off"`` (see that file's own comment — "off" means
# "built-in tools over the local Chromium the installer set up", i.e.
# precisely "Local Browser"). Submitting "off" for this row is therefore
# a correct, idempotent write of the real domain value the shipped
# default already uses, NOT a guess. Every row that carries neither
# ``browser_backend`` nor the "local" special case (Camofox, any
# plugin-registered cloud provider — all of which only carry
# ``browser_provider``, a key ``apply_settings()`` has no path to write
# at all) gets ``backend_key: None`` and is rendered informationally
# only (badge/tag/install button — see ``tests/hermes_cli/
# test_setup_wizard_tools_view.py::test_browser_backend_key_domain`` for
# the enforced invariant: every non-null ``backend_key`` in the "browser"
# block is a real member of ``{BACKEND_DISABLED, browser_use_cli._BACKEND_KEY}``).
_BROWSER_PROVIDER_TO_BACKEND_KEY: dict[str, str] = {
    "local": "off",
}


def _row_backend_key(cat_key: str, provider: dict) -> str | None:
    """The machine value a row's UI control should submit for this
    category's single selectable config field, or ``None`` when the row
    has no such field (informational-only — see the "browser" comment
    block above for why most non-"Browser Use"/"Local Browser" rows land
    here).

    Only the "browser" category has one today (``browser.backend``, via
    ``form.browser_backend`` — see ``apply.py``); other categories
    (``tts``, ``image_gen``, ``homeassistant``) don't expose a
    single-choice backend field in ``apply_settings()`` at all, so every
    row in them is ``None`` here regardless of catalog markers.
    """
    if cat_key != "browser":
        return None
    if provider.get("browser_backend"):
        return provider["browser_backend"]
    browser_provider = provider.get("browser_provider")
    if browser_provider in _BROWSER_PROVIDER_TO_BACKEND_KEY:
        return _BROWSER_PROVIDER_TO_BACKEND_KEY[browser_provider]
    return None


# apply_settings() (apply.py) exposes exactly TWO write paths for the
# "browser" category — nothing else in the submitted form can move a
# browser-category choice onto disk:
#   1. form.browser_backend -> browser.backend in config.yaml, sent only
#      for a row with a real backend_key (see _row_backend_key above).
#   2. form.camofox_url -> the CAMOFOX_URL secret (apply.py's own
#      docstring calls this "the ONE real activation switch for the
#      Camofox browser row"). The row that owns this path is identified
#      structurally by carrying that literal env var key — the same key
#      apply.py's writer targets and tools/browser_camofox.py's
#      is_camofox_mode() reads back — not by its (renameable) display name.
# A row with NEITHER path only carries browser_provider, which writes
# browser.cloud_provider — a config.yaml key apply_settings() has no
# branch for at all (see the long comment above
# _BROWSER_PROVIDER_TO_BACKEND_KEY). Selecting such a row in the wizard
# today would be a UI choice with zero runtime effect: nothing ever gets
# written, so the tool never activates — "choose it, it doesn't work"
# (owner ruling, 2026-08-20). Hiding it is not a taste/curation call, it
# is the structural fact that the wizard cannot make this row do anything
# yet; it reappears on its own the moment apply_settings() grows a
# browser.cloud_provider write path, no change needed here.
def _browser_row_has_activation_path(provider: dict) -> bool:
    """True when this "browser" row has a real apply_settings() write path."""
    if _row_backend_key("browser", provider) is not None:
        return True
    return any(env.get("key") == "CAMOFOX_URL" for env in provider.get("env_vars", []))


def _is_self_hosted_row(provider: dict) -> bool:
    """True for a row whose badge advertises "self-hosted" (SearXNG,
    Firecrawl Self-Hosted today) — the only rows the structural liveness
    rule below ever gates."""
    return "self-hosted" in (provider.get("badge") or "")


def _self_hosted_probe_url(provider: dict) -> str | None:
    """The local address to GET-probe for a self-hosted row, or ``None``
    when it carries no ``*_URL`` env var at all (nothing to probe — the
    caller treats that as "not alive").

    Prefers the catalog's own ``default`` on that env var (the pattern
    ``CAMOFOX_URL`` already uses); falls back to ``_KNOWN_LOCAL_URLS`` for
    rows whose schema doesn't carry one (SearXNG, Firecrawl Self-Hosted).
    """
    for env in provider.get("env_vars", []):
        key = env.get("key", "")
        if key.endswith("_URL"):
            return env.get("default") or _KNOWN_LOCAL_URLS.get(key)
    return None


def _local_service_alive(url: str, timeout: float = 1.0) -> bool:
    """Best-effort liveness probe for a *local* service URL.

    A short timeout (1s — this runs inline in a page render, never worth
    blocking the wizard for) and tolerant of ANY response, even an HTTP
    error status: the question is only "is something listening here",
    not "is it healthy". Any failure (connection refused, timeout, DNS,
    an unparsable URL) means not alive — never raises. A separate,
    monkeypatchable seam on purpose, so tests can simulate "SearXNG is
    running" without a real local server.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except Exception:
        return False


# post_setup key -> short Russian beta warning. Owner ruling 2026-08-24
# (client-VM walkthrough, finding 2): Camofox and Browser Use both need to
# be flagged — not because either tool is unstable, but because making
# them actually WORK needs a step this wizard cannot perform on the
# client's behalf. Keyed by ``post_setup`` (a structural row marker, same
# convention as ``_row_provider_key``/``_row_backend_key`` above) rather
# than by display name, so a future rename upstream can't silently drop
# the warning.
#
# The two notes are deliberately DIFFERENT, not a shared template — the
# owner's own phrasing assumed both need Node.js, but that only holds for
# Camofox: it installs an npm package (`tools_config._run_post_setup`'s
# "camofox" branch shells out to `npm install`). Browser Use's install hook
# ("browser_use_cli" branch) shells out to `uv tool install browser-use` —
# verified against ``tools/browser_use_cli.py::install_cli()``, which never
# touches Node/npm at all. Repeating the owner's Node.js claim for Browser
# Use would be inaccurate, not just imprecise (see CLAUDE.md's "verify the
# premise" rule) — its real prerequisite is `uv`, and its real post-install
# gap is a separate auth/remote-debugging step
# (`browser-use auth login`, or enabling `chrome://inspect` on a local
# Chrome), not a server to start.
_BROWSER_BETA_NOTES_RU: dict[str, str] = {
    "camofox": "Бета. Нужен Node.js на машине; после установки сервер запускается отдельно.",
    "browser_use_cli": "Бета. Нужен uv; после установки может понадобиться отдельный вход или настройка.",
}


def _row_beta_note(cat_key: str, provider: dict) -> str | None:
    """Short Russian beta warning for this row, or ``None`` for every row
    that isn't Camofox/Browser Use (see ``_BROWSER_BETA_NOTES_RU``)."""
    if cat_key != "browser":
        return None
    return _BROWSER_BETA_NOTES_RU.get(provider.get("post_setup") or "")


# post_setup keys whose install path runs `npm install` under the hood
# (tools_config._run_post_setup's "agent_browser"/"browserbase"/"camofox"
# branches) — the only ones where "npm not found" is a coherent, honest
# reason for failure. Deliberately NOT "browser_use_cli": that hook shells
# out to `uv tool install browser-use` (tools/browser_use_cli.py::
# install_cli()) and has nothing to do with Node.js at all — see
# _BROWSER_BETA_NOTES_RU's own comment for why conflating the two would be
# inaccurate, not just imprecise. Used both by ``run_tool_install()`` below
# (to explain a real failed install) and by ``_row_install_blocked_reason``
# (to warn the client BEFORE they pick a row whose install is doomed on
# this machine right now).
_NPM_DEPENDENT_POST_SETUP_KEYS: frozenset[str] = frozenset({"agent_browser", "browserbase", "camofox"})

# The uv-flavored sibling of the set above — Browser Use's own dependency
# (see _BROWSER_BETA_NOTES_RU's comment for why it's uv, not Node/npm).
_UV_DEPENDENT_POST_SETUP_KEYS: frozenset[str] = frozenset({"browser_use_cli"})

_MSG_BLOCKED_NO_NODE_RU = "нужен Node.js на этой машине"
_MSG_BLOCKED_NO_UV_RU = "нужен uv на этой машине"


def _row_install_blocked_reason(post_setup_key: str | None, installed: bool | None) -> str | None:
    """Short Russian reason this row's install is doomed on THIS machine
    right now, or ``None`` when nothing here is known to block it.

    Owner ruling 2026-08-24 ("Установка инструментов — кнопки нет", item
    5): a row whose install is going to fail must say so BEFORE the client
    picks it, not leave them to discover it on the final "Готово" step
    (whose own install stage — ``app.py``'s ``_run_submit`` — runs
    unattended, minutes after the client already clicked away from step
    5). Deliberately narrower than "beta" (``_row_beta_note`` above,
    ``rows[].beta``): a row can be beta without being blocked (Node.js
    installed and working — nothing wrong with picking Camofox), and this
    function only fires when BOTH (a) the row still needs installing (an
    already-installed row can't be "blocked" — nothing left to fail) and
    (b) its install path structurally depends on a binary this function
    can verify is absent right now (Node.js for the npm-dependent hooks,
    uv for Browser Use). A row whose only listed dependency isn't
    checkable this way (every pip-installed hook — KittenTTS, Piper,
    faster_whisper, ddgs) never gets a reason here: their
    ``_run_post_setup`` branch installs into the SAME Python environment
    already running this wizard, which is definitionally present.
    """
    if not post_setup_key or installed:
        return None
    if post_setup_key in _NPM_DEPENDENT_POST_SETUP_KEYS:
        from hermes_constants import find_node_executable

        if not find_node_executable("npm"):
            return _MSG_BLOCKED_NO_NODE_RU
    elif post_setup_key in _UV_DEPENDENT_POST_SETUP_KEYS:
        import shutil

        from hermes_cli.managed_uv import resolve_uv

        # Managed copy FIRST, PATH second — the same two rungs `lazy_deps`
        # and `update_cmd` climb, and the same shape as the npm branch just
        # above (``find_node_executable`` is already managed-aware).
        #
        # A bare ``shutil.which`` was the bug: on a client install uv lives
        # in ``$HERMES_HOME/bin``, which is not on an arbitrary process's
        # PATH. The wizard therefore told the client "this tool cannot be
        # installed — uv is missing" on exactly the machines where uv is
        # present and the install would have succeeded. The row was blocked
        # before the client could pick it, so nothing later could correct
        # the verdict.
        if not (resolve_uv() or shutil.which("uv")):
            return _MSG_BLOCKED_NO_UV_RU
    return None


# Edge TTS's real ru-RU voice set (Microsoft Azure Neural TTS) — verified
# live against ``edge_tts.list_voices()`` on 2026-08-24: exactly two
# Russian voices exist today, "Светлана" (female) and "Дмитрий" (male).
# page.py currently hardcodes ``VOICE_DEFAULT_NAME = "ru-RU-SvetlanaNeural"``
# as the only name a client ever sees (owner's question on the client VM:
# "почему именно Светлана? Это стандарт?") — this ships the real, small
# catalog as DATA so a client can offer an actual choice instead of one
# silently-picked name. A static Python list, not a live network call: the
# wizard renders synchronously and Edge's own voice list is a stable,
# documented set, not something worth a round-trip to Microsoft mid-render.
EDGE_TTS_RU_VOICES: list[dict] = [
    {"key": "ru-RU-SvetlanaNeural", "label": "Светлана", "gender": "female"},
    {"key": "ru-RU-DmitryNeural", "label": "Дмитрий", "gender": "male"},
]
EDGE_TTS_DEFAULT_VOICE = "ru-RU-SvetlanaNeural"


def _row_voice_fields(cat_key: str, provider: dict) -> tuple[list[dict] | None, str | None]:
    """``(voices, default_voice)`` for a "tts" row backed by Edge TTS, or
    ``(None, None)`` for every other row — see ``EDGE_TTS_RU_VOICES``."""
    if cat_key == "tts" and provider.get("tts_provider") == "edge":
        return EDGE_TTS_RU_VOICES, EDGE_TTS_DEFAULT_VOICE
    return None, None


def wizard_tool_blocks() -> list[dict]:
    """Return one block per ``WIZARD_TOOL_CATEGORIES`` entry, rows resolved.

    Each block: ``{"category", "title_ru", "rows": [...]}``. Each row:
    ``{"name", "badge", "tag", "env_vars", "post_setup", "recommended",
    "installed", "backend_key", "web_backend", "provider_key", "beta",
    "beta_note_ru", "voices", "default_voice"}``. Rows with
    ``requires_nous_auth``, OAuth-only rows (``_is_oauth_only_row`` — module
    docstring), and rows listed in ``EXCLUDED_ROWS`` for that category are
    never rendered. ``backend_key`` is the machine value the wizard's UI
    must submit for this row (never the display ``name`` — see
    ``_row_backend_key``'s docstring); ``None`` means the row has no
    directly-submittable field and must be rendered informationally only.
    ``web_backend`` is the analogous machine value for "web" rows — the
    value ``form.search_backend`` must carry when this row is chosen
    (``apply_settings()`` writes it to ``web.search_backend`` verbatim);
    ``None`` for every non-"web" row. ``provider_key`` is the analogous
    machine value for "tts"/"image_gen"/"video_gen" rows — the value
    ``form.tool_provider[category]`` must carry when this row is chosen
    (``apply_settings()`` writes it to ``"<category>.provider"`` verbatim —
    see ``_row_provider_key``); ``None`` for every other category's rows,
    and for a row that carries none of its category's provider markers.

    "web" category rows additionally go through the self-hosted liveness
    rule (module docstring): a "self-hosted"-badged row with no
    ``post_setup`` and no live local service at its address is skipped
    entirely (not even an ``installed: False`` row — it simply isn't a
    real choice right now). A row that DOES pass the probe gets its
    matching ``*_URL`` env var's ``env_vars`` entry carrying a ``default``
    equal to the address that answered, so the caller can prefill it —
    the same shape ``CAMOFOX_URL``'s catalog entry already uses. ``ddgs``
    is marked ``recommended`` by its stable ``web_backend`` identity (its
    badge text never says "recommended" — see module docstring), not by
    the generic badge-substring rule every other category uses.

    Owner ruling 2026-08-24 (client-VM walkthrough) added four more row
    fields and one env-var field:

    - ``env_vars[].auto_default`` — stamped onto any ``*_URL`` env var that
      ends up with a ``default`` value (static catalog default, e.g.
      Camofox's 9377 port, or a fresh self-hosted liveness probe result).
      Contract for the client: don't render an input for this field in the
      main flow — submit ``auto_default`` untouched — but keep a manual
      override reachable somewhere less prominent. See
      ``_AUTO_DEFAULT_URL_SUFFIX``'s own comment for the full write-up.
    - ``beta`` / ``beta_note_ru`` — ``True`` + a short Russian warning for
      Camofox and Browser Use (see ``_BROWSER_BETA_NOTES_RU``); ``False`` /
      ``None`` for every other row. Contract for the client: show a small
      "бета" badge/note next to these two rows.
    - ``voices`` / ``default_voice`` — populated only for the "Microsoft
      Edge TTS" row (see ``_row_voice_fields``/``EDGE_TTS_RU_VOICES``);
      ``None`` for every other row. Contract for the client: offer this
      list as a voice picker instead of the hardcoded
      ``VOICE_DEFAULT_NAME`` single option page.py carries today.

    Owner ruling 2026-08-24 ("Установка инструментов — кнопки нет", item
    5) added a fifth field:

    - ``install_blocked`` / ``install_blocked_reason_ru`` — ``True`` + a
      short Russian reason (see ``_row_install_blocked_reason``) for a row
      that still needs installing AND whose install path structurally
      depends on a binary this machine doesn't have right now (Node.js for
      Camofox/Local Browser, uv for Browser Use); ``False`` / ``None`` for
      every other row, including an already-installed row (nothing left
      to fail) and a beta row whose dependency IS present. Contract for
      the client: show this reason in the row's own picker BEFORE the
      client selects it — installing now happens unattended, as part of
      the final "Готово" step (``app.py``'s ``_run_submit``), so the
      client needs to know a pick is doomed before they commit to it, not
      after.
    """
    config = load_config() or {}
    catalog = _catalog()
    blocks: list[dict] = []
    # Finding 10 (review 2026-08-26, owner-approved fix): the 2026-08-26
    # search/extract split gave "web_extract" its own pass through this
    # same self-hosted-liveness rule (below) — a row like "Firecrawl
    # Self-Hosted", which appears in BOTH the "web" and "web_extract"
    # catalog specs (it can do both), used to get `_local_service_alive()`
    # probed TWICE per `/api/form` call, once per category, each paying
    # the full 1s worst-case timeout. Request-scoped (this function is
    # re-entered fresh on every call — a longer-lived cache would risk
    # serving a stale "not alive" verdict from before the client actually
    # started the local service), keyed by probe URL so two DIFFERENT
    # self-hosted rows never share a result.
    alive_cache: dict[str, bool] = {}
    for cat_key in WIZARD_TOOL_CATEGORIES:
        spec = catalog.get(cat_key, {})
        rows: list[dict] = []
        for provider in spec.get("providers", []):
            name = provider.get("name", "")
            if provider.get("requires_nous_auth") or _is_nous_plugin_row(provider):
                continue
            # "web" is deliberately EXEMPT from the OAuth-only rule: its own
            # "xAI Web Search (Grok)" row (post_setup="xai_grok", no
            # env_vars — the same shape as the rows this rule hides
            # elsewhere) already has a working, tested carve-out
            # (renderSearchBlock()'s `row.post_setup === "xai_grok"` branch
            # in page.py — "Использует ключ провайдера xAI (Grok) —
            # настройте провайдера на шаге 4."), predating this rule. That
            # is an intentional design, not a gap the new rule should paper
            # over — hiding it here would silently regress a shipped,
            # tested feature. See _is_oauth_only_row()'s own docstring.
            if cat_key not in _WEB_CATEGORIES and _is_oauth_only_row(provider):
                continue
            if (cat_key, name) in EXCLUDED_ROWS:
                continue
            if cat_key == "browser" and not _browser_row_has_activation_path(provider):
                continue

            env_vars = [dict(env) for env in provider.get("env_vars", [])]
            # Finding 9: stamp a Russian label onto every env var by key —
            # page.py's label-building falls back to a generic "Ключ"/
            # "Адрес" when a key has no entry here, so a gap degrades to
            # generic-but-Russian, never to the raw English ``prompt``.
            for env in env_vars:
                ru_prompt = RU_ENV_PROMPTS.get(env.get("key", ""))
                if ru_prompt:
                    env["prompt_ru"] = ru_prompt
            if cat_key in _WEB_CATEGORIES and _is_self_hosted_row(provider) and not provider.get("post_setup"):
                probe_url = _self_hosted_probe_url(provider)
                if not probe_url:
                    continue
                if probe_url not in alive_cache:
                    alive_cache[probe_url] = _local_service_alive(probe_url)
                if not alive_cache[probe_url]:
                    continue
                for env in env_vars:
                    if env.get("key", "").endswith("_URL"):
                        env.setdefault("default", probe_url)

            # Owner ruling 2026-08-24, finding 1: any *_URL env var that
            # ends up with a `default` (static catalog value or a fresh
            # self-hosted probe result, both handled above) is a KNOWN
            # local/standard address — stamp `auto_default` so the client
            # knows it can submit this value without ever showing an
            # input for it. See _AUTO_DEFAULT_URL_SUFFIX's own comment.
            for env in env_vars:
                if env.get("key", "").endswith(_AUTO_DEFAULT_URL_SUFFIX) and env.get("default"):
                    env["auto_default"] = env["default"]

            badge = provider.get("badge") or ""
            recommended = "recommended" in badge or (
                cat_key == "web" and provider.get("web_backend") == "ddgs"
            )
            beta_note = _row_beta_note(cat_key, provider)
            voices, default_voice = _row_voice_fields(cat_key, provider)
            installed = _installed(provider, config)
            install_blocked_reason = _row_install_blocked_reason(provider.get("post_setup"), installed)
            rows.append(
                {
                    "name": name,
                    "badge": badge,
                    "tag": provider.get("tag", ""),
                    "env_vars": env_vars,
                    "post_setup": provider.get("post_setup"),
                    "recommended": recommended,
                    "installed": installed,
                    "backend_key": _row_backend_key(cat_key, provider),
                    # Present (non-None) only for "web" rows — the value
                    # apply_settings() writes to web.search_backend when
                    # this row is chosen (form.search_backend). None for
                    # every other category's rows, same "populated only
                    # where it means something" contract as backend_key.
                    "web_backend": provider.get("web_backend"),
                    "provider_key": _row_provider_key(cat_key, provider),
                    # Owner ruling 2026-08-24 (findings 2 and 5) — see
                    # wizard_tool_blocks()'s own docstring for the client
                    # contract on each of these four.
                    "beta": beta_note is not None,
                    "beta_note_ru": beta_note,
                    "voices": voices,
                    "default_voice": default_voice,
                    # Owner ruling 2026-08-24, item 5 — see this function's
                    # own docstring for the client contract.
                    "install_blocked": install_blocked_reason is not None,
                    "install_blocked_reason_ru": install_blocked_reason,
                }
            )
        blocks.append({"category": cat_key, "title_ru": TITLES_RU[cat_key], "rows": rows})
    return blocks


# post_setup keys whose install leaves a working artifact on disk but still
# needs a SEPARATE manual step before the tool is actually usable. Camofox
# installs its npm package but nothing in `_run_post_setup`'s own flow (nor
# anything a synchronous web request should do) starts the long-lived
# server process — `_run_post_setup("camofox")` itself prints "Start the
# Camofox server: npx @askjo/camofox-browser" as its very last step.
_NEEDS_MANUAL_START_AFTER_INSTALL: frozenset[str] = frozenset({"camofox"})

_MSG_INSTALL_ALREADY_DONE = "Уже установлено — ничего делать не нужно."
_MSG_INSTALL_OK = "Установлено."
_MSG_INSTALL_NEEDS_MANUAL_START = "Пакет установлен. Сервер нужно запустить отдельно — см. инструкцию под кнопкой."
_MSG_INSTALL_NO_NODE = (
    "На этой машине не найден Node.js — установите его "
    "(или используйте Docker-вариант из инструкции) и повторите попытку."
)
_MSG_INSTALL_FAILED = "Установка не удалась. Подробности — в логах на сервере."


def _post_setup_ready_now(post_setup_key: str) -> bool | None:
    """Best-effort "is this hook's install artifact present right now"
    check, independent of ``provider_readiness_status``'s row-shaped
    contract (which needs a whole provider dict + config, and for a
    keyless hook with no registered ``_POST_SETUP_READY`` predicate falls
    back to "is this the currently ACTIVE provider" — a config-SELECTION
    signal that stays false until the whole wizard form is submitted, not
    an install-state one; see that function's own docstring). Reuses
    ``tools_config``'s own predicate table (``_POST_SETUP_READY``) where
    one is registered — this is the SAME check
    ``provider_readiness_status`` itself would run, just callable standalone
    by key. Adds one more case tools_config has no predicate for at all:
    "browser_use_cli", checked the same way ``_run_post_setup`` itself
    checks it before deciding whether to (re)install
    (``shutil.which("browser-use")``).

    Returns ``None`` when nothing checkable is registered for this key —
    the caller must fall back to a fresh catalog reprobe for a verdict.
    """
    predicate = _tc._POST_SETUP_READY.get(post_setup_key)
    if predicate is not None:
        try:
            return bool(predicate())
        except Exception:
            return None
    if post_setup_key == "browser_use_cli":
        import shutil

        return bool(shutil.which("browser-use"))
    return None


def run_tool_install(post_setup_key: str) -> dict:
    """Run the same install hook ``hermes tools`` runs for this row and
    return a structured, Russian-language verdict.

    Delegates the actual install to ``tools_config._run_post_setup`` — no
    separate install logic lives in the wizard — but that upstream function
    (not touched here; see CLAUDE.md's plugin/core-file rule) has no return
    value at all and only prints colored progress via
    ``_print_success``/``_print_warning``: a silent early ``return`` on a
    missing dependency (e.g. no Node.js — see its "agent_browser"/"camofox"
    branches) is indistinguishable from a real success by return value
    alone, both are ``None``. Rather than parse printed terminal output
    (fragile — those helpers format for a human, not a machine), this
    wrapper independently determines the "why" from the same structural
    facts ``_run_post_setup`` itself branches on — whether npm is on PATH
    (``find_node_executable``) and whether the install actually left a
    working artifact behind (``_post_setup_ready_now``) — checked BEFORE
    and AFTER the call.

    Returns ``{"ok": bool, "reason": str, "message": str}``. ``message`` is
    Russian and safe to show verbatim. ``reason`` is a machine code
    (``"already_installed"``, ``"installed"``, ``"needs_manual_start"``,
    ``"no_node"``, ``"failed"``, or ``"unknown"`` when no local readiness
    check exists for this key at all). This function's own ``ok``/verdict
    is a best-effort local read, NOT the final word — ``app.py``'s
    ``/api/install`` handler keeps doing its own fresh, uncached
    ``wizard_tool_blocks()`` reprobe as the ground truth for the HTTP
    response's ``ok`` (a caller in a different process, or a slower
    filesystem, could disagree with a same-process check taken
    immediately after the subprocess returns) — it only borrows this
    function's ``message``/``reason`` to explain WHY, instead of the
    single generic string it used to fall back to unconditionally.
    """
    from hermes_constants import find_node_executable

    npm_present = bool(find_node_executable("npm"))
    ready_before = _post_setup_ready_now(post_setup_key)

    _tc._run_post_setup(post_setup_key)

    ready_after = _post_setup_ready_now(post_setup_key)

    if ready_after is None:
        return {"ok": True, "reason": "unknown", "message": ""}

    if ready_after:
        if ready_before:
            return {"ok": True, "reason": "already_installed", "message": _MSG_INSTALL_ALREADY_DONE}
        if post_setup_key in _NEEDS_MANUAL_START_AFTER_INSTALL:
            return {"ok": True, "reason": "needs_manual_start", "message": _MSG_INSTALL_NEEDS_MANUAL_START}
        return {"ok": True, "reason": "installed", "message": _MSG_INSTALL_OK}

    if post_setup_key in _NPM_DEPENDENT_POST_SETUP_KEYS and not npm_present:
        return {"ok": False, "reason": "no_node", "message": _MSG_INSTALL_NO_NODE}

    return {"ok": False, "reason": "failed", "message": _MSG_INSTALL_FAILED}
