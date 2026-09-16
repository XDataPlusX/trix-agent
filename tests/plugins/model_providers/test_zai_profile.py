"""Unit tests for the Z.AI / GLM provider profile's reasoning wiring.

Z.AI's GLM-4.5-and-later chat models default to thinking-mode ON when the
request omits ``thinking``.  Before the profile emitted the parameter,
``reasoning_config = {"enabled": False}`` was a silent no-op on the direct
Z.AI route — users who turned thinking off kept burning thinking tokens on
every turn (the desktop "thinking reverts to medium" report).

GLM-5.2 additionally exposes a native ``reasoning_effort`` knob with two
enabled levels (high / max) on the OpenAI-compatible ``/api/paas/v4``
endpoint; the Hermes effort scale is collapsed onto those.

These tests pin the profile's wire-shape contract so Z.AI requests stay
correctly shaped without going live.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def zai_profile():
    """Resolve the registered Z.AI profile through the real discovery path."""
    # ``model_tools`` triggers plugin discovery on import, which is what
    # registers the Z.AI profile in the global provider registry.
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("zai")
    assert profile is not None, "zai provider profile must be registered"
    return profile


class TestZaiThinkingWireShape:
    """``build_api_kwargs_extras`` produces Z.AI's exact wire format."""

    def test_no_preference_omits_thinking(self, zai_profile):
        """No reasoning_config → omit ``thinking`` so the server default
        applies (matches prior behavior for users with no preference)."""
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config=None, model="glm-5"
        )
        assert extra_body == {}
        assert top_level == {}

    def test_enabled_sends_enabled_marker(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"}, model="glm-5"
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {}

    def test_explicitly_disabled_sends_disabled_marker(self, zai_profile):
        """``reasoning_config.enabled=False`` → ``thinking.type=disabled``.

        The crucial bit is that the parameter is *sent* at all — GLM defaults
        to thinking-on when ``thinking`` is absent, so an unsent disable
        burns thinking tokens forever.
        """
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model="glm-5"
        )
        assert extra_body == {"thinking": {"type": "disabled"}}
        assert top_level == {}


class TestZaiGLM52ReasoningEffort:
    """GLM-5.2's native ``reasoning_effort`` knob (two enabled levels)."""

    def test_high_maps_to_high(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {"reasoning_effort": "high"}

    @pytest.mark.parametrize("effort", ["low", "medium", "minimal"])
    def test_lower_efforts_clamp_up_to_high(self, zai_profile, effort):
        """GLM-5.2's minimum thinking level is high — lower Hermes levels
        clamp onto it."""
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {"reasoning_effort": "high"}

    @pytest.mark.parametrize("effort", ["xhigh", "max"])
    def test_strong_efforts_map_to_max(self, zai_profile, effort):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {"reasoning_effort": "max"}

    def test_disabled_sends_no_effort(self, zai_profile):
        """Disabled reasoning still sends the thinking-off marker but never
        an effort level."""
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": "high"},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "disabled"}}
        assert top_level == {}


    @pytest.mark.parametrize(
        "model",
        [
            "z-ai/glm-5.2",
            "glm-5-2",
            "glm-5p2",
            "accounts/fireworks/models/glm-5p2",
            "zai-org-glm-5-2",
        ],
    )
    def test_alias_spellings_recognized(self, zai_profile, model):
        _, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "max"},
            model=model,
        )
        assert top_level == {"reasoning_effort": "max"}

    @pytest.mark.parametrize(
        "model",
        ["glm-5.1", "glm-5", "glm-4.7", "glm-4-9b", "", None],
    )
    def test_non_glm_5_2_models_get_no_effort(self, zai_profile, model):
        _, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model=model,
        )
        assert top_level == {}


class TestZaiModelGating:
    """GLM 4.5+ get thinking; earlier GLM models are left untouched."""

    @pytest.mark.parametrize(
        "model",
        [
            "glm-4.5",
            "glm-4.5-air",
            "glm-4.5-flash",
            "glm-4.6",
            "glm-5",
            "glm-5.2",
            "GLM-5",  # case-insensitive
        ],
    )
    def test_thinking_capable_models_emit_thinking(self, zai_profile, model):
        extra_body, _ = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model=model
        )
        assert extra_body == {"thinking": {"type": "disabled"}}


class TestZaiFullKwargsIntegration:
    """End-to-end: the transport's full kwargs carry the reasoning wiring."""


    def test_glm_5_2_effort_reaches_top_level(self, zai_profile):
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="glm-5.2",
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=zai_profile,
            reasoning_config={"enabled": True, "effort": "max"},
            base_url="https://api.z.ai/api/paas/v4",
            provider_name="zai",
        )
        assert kwargs["reasoning_effort"] == "max"
        assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}


# ---------------------------------------------------------------------------
# Coding Plan — отдельный профиль, потому что у подписки отдельный адрес.
#
# Ключ подписки на обычном платном адресе получает
# "Insufficient balance or no resource package" (проверено живым запросом
# 2026-09-04), а на адресе подписки отвечает нормально. Мастер настройки
# своего адреса ввести не даёт, поэтому без отдельного профиля клиент с
# подпиской Z.AI не мог пройти настройку вовсе.
#
# Тесты — про контракт, а не про снимок каталога: проверяется, что два
# профиля существуют, что они РАЗНЫЕ по адресу и по переменной ключа, и что
# ни один не унаследовал чужой адрес. Список моделей не фиксируется — он
# обязан меняться.
# ---------------------------------------------------------------------------


@pytest.fixture
def zai_coding_profile():
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("zai-coding-plan")
    assert profile is not None, "профиль подписки Z.AI должен быть зарегистрирован"
    return profile


class TestZaiCodingPlanIsASeparateProvider:
    def test_endpoints_differ(self, zai_profile, zai_coding_profile):
        assert zai_profile.base_url != zai_coding_profile.base_url
        assert zai_coding_profile.base_url.rstrip("/").endswith("/coding/paas/v4")

    def test_pay_as_you_go_endpoint_is_untouched(self, zai_profile):
        """Соседний профиль не должен увести обычный Z.AI на адрес подписки."""
        assert "/coding/" not in zai_profile.base_url

    def test_key_env_vars_do_not_overlap(self, zai_profile, zai_coding_profile):
        """Разные тарифы — разные ключи. Общая переменная означала бы, что
        ключ подписки молча уедет на платный адрес, то есть ровно тот отказ,
        ради которого профиль и заведён."""
        assert not set(zai_profile.env_vars) & set(zai_coding_profile.env_vars)

    def test_both_are_discoverable_by_name(self):
        import model_tools  # noqa: F401
        import providers

        for name in ("zai", "zai-coding-plan"):
            assert providers.get_provider_profile(name) is not None, name

    def test_coding_plan_reuses_the_glm_wire_shape(self, zai_coding_profile):
        """Подписка — тот же GLM, значит те же правила thinking. Если профиль
        завели копипастой от чужого провайдера, это утверждение упадёт."""
        extra, _top = zai_coding_profile.build_api_kwargs_extras(
            model="glm-4.6", reasoning_config={"enabled": False}
        )
        assert extra["thinking"] == {"type": "disabled"}
