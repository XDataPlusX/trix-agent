"""Kimi / Moonshot provider profiles.

Kimi has dual endpoints:
  - Kimi Code subscription keys (kimi.com/code, "sk-..." tp-style keys) →
    api.kimi.com/coding/v1 — separate monthly/yearly membership billing,
    documented at https://www.kimi.com/code/docs/en/
  - Moonshot Open Platform keys (legacy, pay-as-you-go) →
    api.moonshot.ai/v1 (international) / api.moonshot.cn/v1 (China)

Despite the "-coding" suffix, ``kimi-coding`` / ``kimi-coding-cn`` are the
Moonshot Open Platform (pay-as-you-go) profiles — that name predates the
Kimi Code subscription product. ``kimi-code`` below is the actual coding
*subscription* profile; a client with a Kimi Code membership key gets
"unauthorized"/model-not-found errors on the Open Platform endpoint because
it is a different billing pool entirely, the same class of failure fixed
for Z.AI's Coding Plan (see the ``zai`` provider module).
"""

from typing import Any
from urllib.parse import urlparse

from hermes_cli import __version__ as _HERMES_VERSION
from providers import register_provider
from providers.base import OMIT_TEMPERATURE, ProviderProfile


def _is_confirmed_kimi_coding_url(base_url: str) -> bool:
    """Return True only for Kimi Code's canonical HTTPS API surfaces."""
    try:
        parsed = urlparse(base_url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "api.kimi.com"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") in {"/coding", "/coding/v1"}
        and not parsed.query
        and not parsed.fragment
    )


class KimiProfile(ProviderProfile):
    """Kimi/Moonshot — temperature omitted, thinking xor reasoning_effort."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Use Kimi Code's OpenAI-compatible surface for model discovery."""
        effective_base = (base_url or self.base_url or "").rstrip("/")
        confirmed_coding_endpoint = _is_confirmed_kimi_coding_url(effective_base)
        if confirmed_coding_endpoint and urlparse(effective_base).path.rstrip("/") == "/coding":
            effective_base += "/v1"
        models = super().fetch_models(
            api_key=api_key,
            base_url=effective_base or None,
            timeout=timeout,
        )
        if models is None or confirmed_coding_endpoint:
            return models
        return [model for model in models if model.strip().lower() != "k3"]

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, **context
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Kimi reasoning controls.

        Moonshot's wire shape treats ``extra_body.thinking`` (a binary toggle)
        and a top-level ``reasoning_effort`` as mutually exclusive — sending
        both is at best redundant and risks "cannot specify both 'thinking' and
        'reasoning_effort'" (HTTP 400). This mirrors the kimi-k2 handling on the
        opencode-go relay: send effort when one is requested, otherwise fall
        back to ``extra_body.thinking`` — never both.
        """
        extra_body = {}
        top_level = {}

        if not reasoning_config or not isinstance(reasoning_config, dict):
            # No config → thinking enabled, let the server pick the depth.
            # (Previously also sent reasoning_effort="medium", which paired
            # thinking + effort on every default call.)
            extra_body["thinking"] = {"type": "enabled"}
            return extra_body, top_level

        enabled = reasoning_config.get("enabled", True)
        if enabled is False:
            extra_body["thinking"] = {"type": "disabled"}
            return extra_body, top_level

        # Enabled: prefer an explicit effort; only fall back to extra_body
        # thinking when no recognized effort is requested.
        effort = (reasoning_config.get("effort") or "").strip().lower()
        if effort in {"low", "medium", "high"}:
            top_level["reasoning_effort"] = effort
        else:
            extra_body["thinking"] = {"type": "enabled"}

        return extra_body, top_level


kimi = KimiProfile(
    name="kimi-coding",
    aliases=("kimi", "moonshot", "kimi-for-coding"),
    env_vars=("KIMI_API_KEY", "KIMI_CODING_API_KEY"),
    base_url="https://api.moonshot.ai/v1",
    fixed_temperature=OMIT_TEMPERATURE,
    default_max_tokens=32000,
    default_headers={
        "HTTP-Referer": "https://hermes-agent.nousresearch.com",
        "X-Title": "Hermes Agent",
        "User-Agent": f"HermesAgent/{_HERMES_VERSION}",
    },
    default_aux_model="kimi-k2-turbo-preview",
)

kimi_cn = KimiProfile(
    name="kimi-coding-cn",
    aliases=("kimi-cn", "moonshot-cn"),
    env_vars=("KIMI_CN_API_KEY",),
    base_url="https://api.moonshot.cn/v1",
    fixed_temperature=OMIT_TEMPERATURE,
    default_max_tokens=32000,
    default_headers={
        "HTTP-Referer": "https://hermes-agent.nousresearch.com",
        "X-Title": "Hermes Agent",
        "User-Agent": f"HermesAgent/{_HERMES_VERSION}",
    },
    default_aux_model="kimi-k2-turbo-preview",
)

# Kimi Code — the actual subscription/membership product (kimi.com/code),
# separate from the Moonshot Open Platform pay-as-you-go keys above. The
# ``_is_confirmed_kimi_coding_url`` helper above already special-cases this
# host+path for model discovery; this profile was the missing piece that
# put it on the default catalog so a subscriber can pick it in the wizard
# instead of hitting the Open Platform endpoint with a membership key.
kimi_code = KimiProfile(
    name="kimi-code",
    aliases=("kimi-coding-plan", "kimi-code-plan", "kimi.com-code"),
    env_vars=("KIMI_CODE_API_KEY", "KIMI_CODE_PLAN_API_KEY"),
    display_name="Kimi Code (Coding Plan)",
    description="Kimi Code (kimi.com membership, separate endpoint)",
    signup_url="https://www.kimi.com/code",
    base_url="https://api.kimi.com/coding/v1",
    fixed_temperature=OMIT_TEMPERATURE,
    default_max_tokens=32000,
    default_headers={
        "HTTP-Referer": "https://hermes-agent.nousresearch.com",
        "X-Title": "Hermes Agent",
        "User-Agent": f"HermesAgent/{_HERMES_VERSION}",
    },
    default_aux_model="kimi-k2-turbo-preview",
)

register_provider(kimi)
register_provider(kimi_cn)
register_provider(kimi_code)
