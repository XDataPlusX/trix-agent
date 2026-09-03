"""Live credential probes keyed by env var — leaf module.

Extracted from web_server.py so the setup wizard can validate keys
without importing the dashboard. web_server imports the table from here.
"""
from __future__ import annotations

import httpx

# (method is always GET) env var -> (url, auth) where auth is "bearer" or "query"
CREDENTIAL_PROBES: dict[str, tuple[str, str]] = {
    "OPENROUTER_API_KEY": ("https://openrouter.ai/api/v1/key", "bearer"),
    "OPENAI_API_KEY": ("https://api.openai.com/v1/models", "bearer"),
    "XAI_API_KEY": ("https://api.x.ai/v1/models", "bearer"),
    "GEMINI_API_KEY": ("https://generativelanguage.googleapis.com/v1beta/models", "query"),
}


def probe_provider_key(env_var: str, value: str, timeout: float = 10.0, proxy: str | None = None) -> dict:
    """Sync probe: {ok, reachable, message}. Unknown provider → ok (don't block).

    ``proxy`` is only passed to the client when non-empty (same convention
    as ``hermes_cli.setup_wizard.validate.check_telegram_token``) — the
    setup wizard's form proxy field, for RU-hosted deployments where the
    provider's own API is only reachable through it.
    """
    probe = CREDENTIAL_PROBES.get(env_var)
    if not probe:
        return {"ok": True, "reachable": False, "message": ""}
    url, auth = probe
    headers = {"Accept": "application/json"}
    params = {}
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {value}"
    else:
        params["key"] = value
    kwargs: dict = {"timeout": httpx.Timeout(timeout)}
    if proxy:
        kwargs["proxy"] = proxy
    try:
        with httpx.Client(**kwargs) as client:
            resp = client.get(url, headers=headers, params=params)
    except Exception:
        return {"ok": False, "reachable": False,
                "message": "Не удалось связаться с провайдером для проверки ключа."}
    if resp.status_code in (401, 403):
        return {"ok": False, "reachable": True,
                "message": "Провайдер отклонил этот ключ. Проверьте его и попробуйте ещё раз."}
    if resp.status_code == 429 or resp.is_success:
        return {"ok": True, "reachable": True, "message": ""}
    return {"ok": False, "reachable": True,
            "message": f"Провайдер ответил HTTP {resp.status_code} на этот ключ."}
