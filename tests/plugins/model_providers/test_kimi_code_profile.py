"""Kimi Code — the coding-*subscription* profile, separate from the Moonshot
Open Platform pay-as-you-go profiles (``kimi-coding`` / ``kimi-coding-cn``).

Same bug class as Z.AI's Coding Plan (see ``test_zai_profile.py``): the
Moonshot Open Platform key and the kimi.com/code membership key are two
different billing pools on two different hosts. Before this profile
existed, a Kimi Code subscriber had no address in the wizard to point at —
only the pay-as-you-go ``kimi-coding``/``kimi-coding-cn`` profiles, both of
which reject a membership key.

Tests are a contract, not a snapshot: two profiles exist, their endpoints
and key env vars don't overlap, and neither profile's address leaked into
the other. No model list is pinned — that's expected to change.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def kimi_profile():
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("kimi-coding")
    assert profile is not None, "kimi-coding provider profile must be registered"
    return profile


@pytest.fixture
def kimi_code_profile():
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("kimi-code")
    assert profile is not None, "kimi-code (Kimi Code subscription) profile must be registered"
    return profile


class TestKimiCodeIsASeparateProvider:
    def test_endpoints_differ(self, kimi_profile, kimi_code_profile):
        assert kimi_profile.base_url != kimi_code_profile.base_url
        assert kimi_code_profile.base_url.rstrip("/").endswith("/coding/v1")

    def test_open_platform_endpoint_is_untouched(self, kimi_profile):
        """The subscription sibling must not drag Moonshot Open Platform onto
        the kimi.com/code host."""
        assert "api.kimi.com" not in kimi_profile.base_url
        assert kimi_profile.base_url.rstrip("/").endswith("api.moonshot.ai/v1")

    def test_key_env_vars_do_not_overlap(self, kimi_profile, kimi_code_profile):
        """A shared env var would mean a Kimi Code membership key silently
        gets sent to the Open Platform endpoint — the pay-as-you-go host —
        the exact failure this profile exists to avoid."""
        assert not set(kimi_profile.env_vars) & set(kimi_code_profile.env_vars)

    def test_both_are_discoverable_by_name(self):
        import model_tools  # noqa: F401
        import providers

        for name in ("kimi-coding", "kimi-coding-cn", "kimi-code"):
            assert providers.get_provider_profile(name) is not None, name

    def test_kimi_code_reuses_the_moonshot_wire_shape(self, kimi_code_profile):
        """Still Moonshot/Kimi under the hood — thinking xor reasoning_effort
        must hold on the subscription endpoint too. A copy-paste from an
        unrelated provider would fail this."""
        extra_body, top_level = kimi_code_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"}
        )
        assert top_level == {"reasoning_effort": "high"}
        assert "thinking" not in extra_body


class TestKimiCodeModelDiscoveryIsConfirmed:
    """``_is_confirmed_kimi_coding_url`` exists specifically to recognize this
    host+path — the model-discovery K3 filter must stay OFF for it (K3 is
    excluded only on the legacy/unconfirmed Open Platform surface)."""

    def test_default_base_url_keeps_k3(self, kimi_code_profile):
        from unittest.mock import patch

        from providers.base import ProviderProfile

        with patch.object(
            ProviderProfile,
            "fetch_models",
            return_value=["k3", "kimi-k2.6"],
        ):
            models = kimi_code_profile.fetch_models(api_key="test-key")

        assert models == ["k3", "kimi-k2.6"]
