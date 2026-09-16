"""Xiaomi MiMo provider profiles.

``xiaomi`` is the pay-as-you-go Open Platform key (sk-... keys, billed by
token). Xiaomi separately sells a Token Plan subscription (tp-... keys,
platform.xiaomimimo.com/token-plan) documented for third-party coding
tools at https://mimo.mi.com/docs/en-US/tokenplan/integration/claudecode —
it lives on its own host (``token-plan-cn.xiaomimimo.com``, Anthropic
Messages API) and a pay-as-you-go key does not work there and vice versa.
Same failure class as Z.AI's Coding Plan (see the ``zai`` provider module):
without a separate profile, a Token Plan subscriber has no address to
point the wizard at.

The docs only name the China-region host; regions doc mentions Singapore
and Europe endpoints exist too without naming them, so
``XIAOMI_TOKEN_PLAN_BASE_URL`` lets a non-CN subscriber override the
default the same way ``ALIBABA_CODING_PLAN_BASE_URL`` does for Alibaba.
"""

from providers import register_provider
from providers.base import ProviderProfile

xiaomi = ProviderProfile(
    name="xiaomi",
    aliases=("mimo", "xiaomi-mimo"),
    env_vars=("XIAOMI_API_KEY",),
    base_url="https://api.xiaomimimo.com/v1",
    supports_health_check=False,  # /v1/models returns 401 even with valid key
    supports_vision=True,  # mimo-v2-omni is vision-capable
    supports_vision_tool_messages=False,  # rejects list-type tool content (400 "text is not set")
)

xiaomi_token_plan = ProviderProfile(
    name="xiaomi-token-plan",
    aliases=("xiaomi-coding-plan", "mimo-token-plan", "mimo-coding-plan"),
    display_name="Xiaomi MiMo (Token Plan)",
    description="Xiaomi MiMo Token Plan (subscription tier, separate endpoint)",
    signup_url="https://platform.xiaomimimo.com/token-plan",
    env_vars=("XIAOMI_TOKEN_PLAN_API_KEY", "XIAOMI_TOKEN_PLAN_BASE_URL"),
    base_url="https://token-plan-cn.xiaomimimo.com/anthropic",
    api_mode="anthropic_messages",
    supports_vision=True,
    supports_vision_tool_messages=False,
)

register_provider(xiaomi)
register_provider(xiaomi_token_plan)
