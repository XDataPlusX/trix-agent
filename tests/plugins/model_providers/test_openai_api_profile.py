"""Unit tests for the OpenAI (direct API key) provider profile.

``openai-api`` is a plain "bring your own OpenAI API key" provider — distinct
from ``openai-codex`` (ChatGPT subscription OAuth). Before this profile
existed, the setup wizard's live provider catalog
(``providers.list_providers()``) had no entry for it at all, even though
``hermes_cli/models.py`` already knew the id ``openai-api`` in its model
catalog and provider grouping.

Code review (post-merge) on the first version of this profile found:
  1. The base ``ProviderProfile.fetch_models`` returns OpenAI's official
     host's raw ``/v1/models`` dump verbatim — 100+ non-chat entries
     (Whisper, TTS, DALL-E, embeddings, moderation) leaking into the model
     picker. ``TestOpenAIAPIFetchModelsCuration`` below pins the override
     that filters those out, mirroring the curation
     ``hermes_cli/models.py`` already does for its own OpenAI live-fetch path.
  2. ``fallback_models``/``default_aux_model`` were sourced from the legacy
     ``_PROVIDER_MODELS["openai"]`` catalog instead of the slug-specific
     ``_PROVIDER_MODELS["openai-api"]`` one.
  3. ``api_mode`` was left at the ``chat_completions`` default, contradicting
     ``hermes_cli/providers.py``'s ``HERMES_OVERLAYS["openai-api"]``
     (``transport="codex_responses"``) and the host-mandate detection in
     ``hermes_cli/runtime_provider.py`` that forces Responses API for
     OpenAI's official host family.
  4. Display metadata was Russian, but ``display_name``/``description`` are
     read by non-localized surfaces (CLI picker, desktop Settings via
     ``hermes_cli/provider_catalog.py``) — Russian belongs only in the
     wizard's ``DISPLAY_NAME_OVERRIDES`` edge layer.
  5. ``supports_vision``/``supports_prompt_cache_key`` were left at their
     (False) defaults, silently dropping OpenAI vision support and prompt
     caching for any session that resolves this profile onto the
     ``chat_completions`` transport path.

This file's classes correspond 1:1 to those five points, plus the pre-existing
identity/registration checks.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def openai_api_profile():
    """Resolve the registered profile via the provider registry.

    Importing ``model_tools`` triggers plugin discovery, which registers the
    profile. Going through ``get_provider_profile`` keeps the test honest
    about the actual registration path (name + alias resolution), not a
    private module attribute.
    """
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("openai-api")
    assert profile is not None, "openai-api provider profile must be registered"
    return profile


class TestOpenAIAPIProfile:
    def test_identity_and_endpoint(self, openai_api_profile):
        assert openai_api_profile.name == "openai-api"
        assert openai_api_profile.auth_type == "api_key"
        assert openai_api_profile.base_url == "https://api.openai.com/v1"
        assert openai_api_profile.get_hostname() == "api.openai.com"

    def test_env_vars(self, openai_api_profile):
        assert openai_api_profile.env_vars == ("OPENAI_API_KEY",)

    def test_no_bare_openai_alias(self, openai_api_profile):
        """The bare ``"openai"`` id is already claimed elsewhere.

        ``hermes_cli.providers.ALIASES`` maps ``"openai"`` to
        ``"openrouter"`` (routes through the aggregator by default). This
        profile must not also claim it as an alias in the separate
        ``providers`` plugin registry — that would make the two systems
        disagree about what "openai" resolves to.
        """
        assert "openai" not in openai_api_profile.aliases

    def test_display_metadata_is_non_empty(self, openai_api_profile):
        assert openai_api_profile.display_name
        assert openai_api_profile.description
        assert openai_api_profile.signup_url

    def test_registered_in_live_catalog(self):
        """The whole point of this plugin: the wizard's live provider
        catalog must include it, not just the internal registry."""
        import model_tools  # noqa: F401
        import providers

        names = {p.name for p in providers.list_providers()}
        assert "openai-api" in names


class TestOpenAIAPIWireProtocol:
    """Point 3: api_mode must match the host-mandated Responses API."""

    def test_api_mode_is_codex_responses(self, openai_api_profile):
        assert openai_api_profile.api_mode == "codex_responses"


class TestOpenAIAPIDisplayMetadataIsEnglish:
    """Point 4: profile metadata is read by non-localized surfaces.

    The wizard's Russian label lives in providers_view.py's
    DISPLAY_NAME_OVERRIDES instead — see
    test_setup_wizard_providers_view.py::test_openai_api_shows_russian_display_name_via_override.
    """

    def test_display_name_and_description_are_ascii(self, openai_api_profile):
        assert openai_api_profile.display_name.isascii()
        assert openai_api_profile.description.isascii()

    def test_display_name_is_openai_api(self, openai_api_profile):
        assert openai_api_profile.display_name == "OpenAI API"


class TestOpenAIAPICapabilityFlags:
    """Point 5: vision + prompt-cache-key capability flags."""

    def test_supports_vision(self, openai_api_profile):
        assert openai_api_profile.supports_vision is True

    def test_supports_prompt_cache_key(self, openai_api_profile):
        assert openai_api_profile.supports_prompt_cache_key is True


class TestOpenAIAPIFallbackModels:
    """Point 2: fallback_models/default_aux_model sourced from the
    slug-specific _PROVIDER_MODELS["openai-api"] catalog, not the legacy
    "openai" one."""

    def test_fallback_models_are_non_empty(self, openai_api_profile):
        assert openai_api_profile.fallback_models

    def test_fallback_models_exclude_codex_only_ids(self, openai_api_profile):
        # Codex-suffixed ids are Codex Responses tool-only — excluded from
        # this general picker default the same way openai-codex's own
        # catalog is a separate concern.
        for model_id in openai_api_profile.fallback_models:
            assert "codex" not in model_id.lower(), model_id

    def test_fallback_models_come_from_openai_api_catalog(self, openai_api_profile):
        from hermes_cli.models import _PROVIDER_MODELS

        catalog = _PROVIDER_MODELS["openai-api"]
        for model_id in openai_api_profile.fallback_models:
            assert model_id in catalog, (
                f"{model_id!r} is not in _PROVIDER_MODELS['openai-api'] — "
                "fallback_models must be sourced from the slug-specific "
                "catalog, not the legacy 'openai' one"
            )

    def test_fallback_models_not_from_legacy_openai_catalog_only(self, openai_api_profile):
        # gpt-5.6-* ids only exist in _PROVIDER_MODELS["openai-api"], not in
        # the legacy _PROVIDER_MODELS["openai"] list — this is the concrete
        # regression check for point 2.
        assert any(m.startswith("gpt-5.6-") for m in openai_api_profile.fallback_models)

    def test_default_aux_model_is_a_mini_from_the_same_catalog(self, openai_api_profile):
        from hermes_cli.models import _PROVIDER_MODELS

        assert openai_api_profile.default_aux_model == "gpt-5.4-mini"
        assert openai_api_profile.default_aux_model in _PROVIDER_MODELS["openai-api"]


class TestOpenAIAPIFetchModelsCuration:
    """Point 1: live fetch against the official host must be curated down
    to chat-capable models, mirroring hermes_cli/models.py's own OpenAI
    live-fetch curation (models.py ~2971-3002) without importing its
    private curated list — see the module docstring for the "rule, not a
    list" rationale.
    """

    _RAW_CATALOG = [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.4-mini",
        "o3-mini",
        "whisper-1",
        "tts-1-hd",
        "dall-e-3",
        "gpt-image-1",
        "text-embedding-3-large",
        "omni-moderation-latest",
        "davinci-002",
        "babbage-002",
    ]

    def test_official_host_curates_out_non_chat_models(self, openai_api_profile):
        with patch(
            "providers.base.ProviderProfile.fetch_models",
            return_value=list(self._RAW_CATALOG),
        ):
            result = openai_api_profile.fetch_models(
                api_key="sk-test", base_url="https://api.openai.com/v1"
            )
        assert result is not None
        for denied in (
            "whisper-1",
            "tts-1-hd",
            "dall-e-3",
            "gpt-image-1",
            "text-embedding-3-large",
            "omni-moderation-latest",
            "davinci-002",
            "babbage-002",
        ):
            assert denied not in result, f"{denied!r} must be curated out"
        for kept in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.4-mini", "o3-mini"):
            assert kept in result, f"{kept!r} must survive curation"

    def test_data_residency_host_is_also_curated(self, openai_api_profile):
        # us.api.openai.com / eu.api.openai.com serve the identical dump —
        # is_official_openai_host must treat them the same as the canonical
        # host.
        with patch(
            "providers.base.ProviderProfile.fetch_models",
            return_value=list(self._RAW_CATALOG),
        ):
            result = openai_api_profile.fetch_models(
                api_key="sk-test", base_url="https://us.api.openai.com/v1"
            )
        assert "whisper-1" not in result
        assert "gpt-5.6-sol" in result

    def test_third_party_endpoint_is_not_curated(self, openai_api_profile):
        # A user-configured proxy/gateway serves its own small catalog —
        # trust it verbatim, matching hermes_cli/models.py's own gate.
        with patch(
            "providers.base.ProviderProfile.fetch_models",
            return_value=list(self._RAW_CATALOG),
        ):
            result = openai_api_profile.fetch_models(
                api_key="sk-test", base_url="https://my-openai-proxy.example.com/v1"
            )
        assert result == self._RAW_CATALOG

    def test_empty_live_result_passes_through(self, openai_api_profile):
        with patch("providers.base.ProviderProfile.fetch_models", return_value=None):
            result = openai_api_profile.fetch_models(
                api_key="sk-test", base_url="https://api.openai.com/v1"
            )
        assert result is None

    def test_all_filtered_falls_back_to_curated_defaults(self, openai_api_profile):
        # If a future live response somehow contains nothing our rule
        # recognizes as chat-capable, degrade to our own curated defaults
        # rather than an empty picker or the (would-be) unfiltered dump.
        with patch(
            "providers.base.ProviderProfile.fetch_models",
            return_value=["whisper-1", "dall-e-3"],
        ):
            result = openai_api_profile.fetch_models(
                api_key="sk-test", base_url="https://api.openai.com/v1"
            )
        assert result == list(openai_api_profile.fallback_models)
