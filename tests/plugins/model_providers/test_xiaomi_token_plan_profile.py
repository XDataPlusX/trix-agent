"""Xiaomi MiMo Token Plan — the coding-subscription profile, separate from
the pay-as-you-go ``xiaomi`` profile.

Same bug class as Z.AI's Coding Plan (see ``test_zai_profile.py``): Xiaomi's
Token Plan subscription (tp-... keys, platform.xiaomimimo.com/token-plan)
lives on its own host and speaks the Anthropic Messages API, while the
pay-as-you-go profile (sk-... keys) is OpenAI-compatible chat_completions on
``api.xiaomimimo.com``. A Token Plan key sent to the pay-as-you-go host (or
vice versa) is rejected — before this profile existed, a Token Plan
subscriber had no address in the wizard to point at.

Tests are a contract, not a snapshot: two profiles exist, their endpoints
and key env vars don't overlap, and the pay-as-you-go profile's wire shape
(host + protocol) is untouched. No model list is pinned.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def xiaomi_profile():
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("xiaomi")
    assert profile is not None, "xiaomi provider profile must be registered"
    return profile


@pytest.fixture
def xiaomi_token_plan_profile():
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("xiaomi-token-plan")
    assert profile is not None, "xiaomi-token-plan (Token Plan subscription) profile must be registered"
    return profile


class TestXiaomiTokenPlanIsASeparateProvider:
    def test_endpoints_differ(self, xiaomi_profile, xiaomi_token_plan_profile):
        assert xiaomi_profile.base_url != xiaomi_token_plan_profile.base_url

    def test_pay_as_you_go_endpoint_is_untouched(self, xiaomi_profile):
        """The subscription sibling must not drag the pay-as-you-go profile
        onto the Token Plan host."""
        assert "token-plan" not in xiaomi_profile.base_url
        assert xiaomi_profile.base_url.rstrip("/").endswith("api.xiaomimimo.com/v1")

    def test_key_env_vars_do_not_overlap(self, xiaomi_profile, xiaomi_token_plan_profile):
        """A shared env var would mean a Token Plan key silently gets sent to
        the pay-as-you-go host — the exact failure this profile exists to
        avoid."""
        assert not set(xiaomi_profile.env_vars) & set(xiaomi_token_plan_profile.env_vars)

    def test_both_are_discoverable_by_name(self):
        import model_tools  # noqa: F401
        import providers

        for name in ("xiaomi", "xiaomi-token-plan"):
            assert providers.get_provider_profile(name) is not None, name

    def test_token_plan_speaks_anthropic_messages(self, xiaomi_profile, xiaomi_token_plan_profile):
        """Token Plan is documented as Anthropic-Messages-only; the
        pay-as-you-go profile stays on the default (OpenAI-compatible
        chat_completions). Copy-pasting one profile's api_mode onto the
        other would fail this."""
        assert xiaomi_token_plan_profile.api_mode == "anthropic_messages"
        assert xiaomi_profile.api_mode != "anthropic_messages"

    def test_token_plan_base_url_is_overridable(self, xiaomi_token_plan_profile):
        """Only the China-region host is documented; Singapore/EU
        subscribers must be able to override without a code change."""
        assert any(
            var.endswith("_BASE_URL") for var in xiaomi_token_plan_profile.env_vars
        )
