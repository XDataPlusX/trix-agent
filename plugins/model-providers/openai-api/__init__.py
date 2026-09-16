"""OpenAI (direct API key) provider profile.

Distinct from ``openai-codex`` (``plugins/model-providers/openai-codex/``),
which is the ChatGPT-subscription OAuth flow (``auth_type="oauth_external"``,
no API key). This profile is the plain "bring your own OpenAI API key" path,
authenticated with ``OPENAI_API_KEY`` — the credential probe for it already
exists in ``hermes_cli/credential_probes.py``.

Wire protocol: ``api_mode="codex_responses"``, not Chat Completions. OpenAI's
official host family (``api.openai.com`` and the data-residency regional
hosts) mandates the Responses API for GPT-5.x tool-calling with reasoning —
``hermes_cli/providers.py``'s ``HERMES_OVERLAYS["openai-api"]`` already
declares ``transport="codex_responses"`` and
``hermes_cli/runtime_provider.py``'s host-mandate detection forces it for
this host family (the fix for the Coatue incident: chat_completions 400s on
tool calls against these hosts). This profile's own ``api_mode`` field is
set to match so the two declarations agree — see the ``actual`` provider
(``plugins/model-providers/actual/__init__.py``) for the same pattern on a
different host-mandated provider.

No ``"openai"`` alias here on purpose: ``hermes_cli/providers.py``'s
``ALIASES`` already maps the bare ``"openai"`` id to ``"openrouter"``
(routing through the aggregator by default), and that mapping is relied on
elsewhere. Claiming the same alias in this profile's own (separate)
registry would not change that resolution, but it would be misleading to
read next to it — so the only way to reach this provider is its full name,
``openai-api``, exactly as the upstream catalog already spells it
(``hermes_cli/models.py``'s ``PROVIDER_GROUPS["openai"]`` group and its
``_PROVIDER_MODELS["openai-api"]`` / ``ProviderEntry`` entries already use
that id).

Display metadata is deliberately English here (``display_name``/
``description`` are read verbatim by the CLI picker and the desktop
Settings tabs — see ``hermes_cli/provider_catalog.py`` — which are not
Russian-localized surfaces). The wizard-specific Russian label lives in
``hermes_cli/setup_wizard/providers_view.py``'s ``DISPLAY_NAME_OVERRIDES``
edge layer instead, exactly like every other wizard-only override.
"""

from __future__ import annotations

from providers import register_provider
from providers.base import ProviderProfile

# Model-id families OpenAI's official host serves alongside chat models on
# its generic ``/v1/models`` listing — audio transcription/TTS, image
# generation, embeddings, moderation, and legacy completion models. None of
# these accept chat/tool-calling requests, so a raw live-fetch pollutes the
# model picker with entries the agent can never actually use.
#
# This is a RULE, not a copy of hermes_cli/models.py's private curated
# ``_PROVIDER_MODELS`` dict. hermes_cli/models.py already does the
# equivalent curation for its own live-fetch path (see the
# ``normalized in ("openai", "openai-api")`` branch around line 2971) by
# intersecting with that curated list — but that dict is a module-private
# implementation detail of a different (upstream) file, reproducing it here
# would coincidentally require importing a private name across a plugin
# boundary and would drift out of sync with it silently. A capability rule
# generalizes to future non-chat model families without needing a data sync,
# and degrades safely (see ``fetch_models`` below) if it ever over-filters.
_NON_CHAT_MODEL_MARKERS: tuple[str, ...] = (
    "whisper",
    "tts",
    "dall-e",
    "dalle",
    "gpt-image",
    "text-embedding",
    "embedding",
    "omni-moderation",
    "text-moderation",
    "moderation",
    "davinci",
    "babbage",
    "curie",
    "ada",
    "transcribe",
    "realtime",
    "computer-use",
    "sora",
)


def _is_chat_model(model_id: str) -> bool:
    """True unless *model_id* names a known non-chat OpenAI model family."""
    m = (model_id or "").strip().lower()
    if not m:
        return False
    return not any(marker in m for marker in _NON_CHAT_MODEL_MARKERS)


class OpenAIAPIProfile(ProviderProfile):
    """OpenAI direct API key — curates the official host's live catalog.

    ``ProviderProfile.fetch_models`` (the base implementation) returns
    OpenAI's generic ``/v1/models`` listing verbatim. Against the official
    host that list mixes 100+ non-chat entries (Whisper, TTS, DALL-E,
    embeddings, moderation, legacy completion models) into what should be an
    agentic chat-model picker. This mirrors the curation
    ``hermes_cli/models.py`` already applies to its own OpenAI live-fetch
    path for the exact same reason — see module docstring for why this is a
    filter *rule* rather than a copy of that file's private curated list.

    Third-party / self-hosted OpenAI-compatible endpoints (a user pointing
    ``OPENAI_BASE_URL`` at a proxy or gateway) are NOT curated — their live
    catalog is whatever the operator chose to expose, and is typically
    already a small, relevant list. Curation only applies to OpenAI's own
    official host family, matching ``hermes_cli/models.py``'s own gate.
    """

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        live = super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)
        if not live:
            return live

        from hermes_cli.providers import is_official_openai_host

        effective_base = (base_url or self.base_url or "").strip()
        if not is_official_openai_host(effective_base):
            return live

        curated = [m for m in live if _is_chat_model(m)]
        if curated:
            return curated
        # Every live entry was filtered out — either an account-specific
        # catalog shape we didn't anticipate, or a transient garbage
        # response. Fall back to our own curated defaults rather than
        # either an empty picker or the unfiltered (Whisper/TTS-polluted)
        # list this override exists to avoid.
        return list(self.fallback_models) or live


openai_api = OpenAIAPIProfile(
    name="openai-api",
    api_mode="codex_responses",
    env_vars=("OPENAI_API_KEY",),
    display_name="OpenAI API",
    description="OpenAI - direct API access with your own key",
    signup_url="https://platform.openai.com/api-keys",
    base_url="https://api.openai.com/v1",
    auth_type="api_key",
    # OpenAI accepts image parts in tool-result (function/tool role) content
    # for its chat/tool-calling models, same as the other native-vision
    # surfaces already marked this way (Anthropic Messages, Gemini, MiniMax).
    supports_vision=True,
    # The legacy (flag-based) kwargs path auto-enables prompt_cache_key for
    # any OpenAI-official-host base_url (agent/transports/chat_completions.py,
    # _is_openai_api_base_url check). The profile-based path has no such
    # auto-detection — it only reads this field — so without it a session
    # running this profile under chat_completions (e.g. an explicit
    # model.api_mode: chat_completions override) would silently lose prompt
    # caching, which this repo treats as sacred.
    supports_prompt_cache_key=True,
    # First non-codex-only ids from hermes_cli/models.py's
    # _PROVIDER_MODELS["openai-api"] catalog (line ~281) — the slug-specific
    # list, not the legacy "openai" list. gpt-5.3-codex further down that
    # list is Codex Responses tool-only and excluded from this general
    # picker default the same way openai-codex's own catalog is separate.
    fallback_models=(
        "gpt-5.6-sol",
        "gpt-5.6-sol-pro",
        "gpt-5.6-terra",
        "gpt-5.6-terra-pro",
        "gpt-5.6-luna",
    ),
    # Cheap tier from the same _PROVIDER_MODELS["openai-api"] catalog.
    default_aux_model="gpt-5.4-mini",
)

register_provider(openai_api)
