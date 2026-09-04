"""Live credential probes keyed by env var — leaf module.

Extracted from web_server.py so the setup wizard can validate keys
without importing the dashboard. web_server imports the table from here.

**``reason``/``status_code`` (added for the support-page follow-up,
2026-09-03).** ``ok``/``reachable``/``message`` are the original, unchanged
contract every existing caller already reads. ``reason`` is a purely
additive, structural classification of the SAME HTTP response this
function already fetched — never a second request — so a caller that needs
to tell "provider rejected the key" apart from "the account is out of
funds" apart from "couldn't even reach the provider" doesn't have to parse
``message``'s Russian prose. Values: ``"network"`` (the request itself
never got a response), ``"auth"`` (401/403 — the key itself was rejected),
``"billing"`` (402 — precedented in this codebase by
``hermes_cli/doctor.py``'s own ``_probe_openrouter``, which already treats
402 on a similar OpenRouter endpoint as "out of credits"), ``"other"`` (any
other non-success, non-429 status), or ``None`` when ``ok`` is ``True``.
``status_code`` is the raw HTTP status when one was received, else
``None`` — present so a caller can still show/log the exact code for the
``"other"`` bucket without re-deriving it from ``message``.
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


def probe_provider_key(
    env_var: str,
    value: str,
    timeout: float = 10.0,
    proxy: str | None = None,
    base_url: str | None = None,
) -> dict:
    """Sync probe: {ok, reachable, message, reason, status_code}. Unknown
    provider → ok (don't block). See the module docstring for ``reason``.

    ``proxy`` is only passed to the client when non-empty (same convention
    as ``hermes_cli.setup_wizard.validate.check_telegram_token``) — the
    setup wizard's form proxy field, for RU-hosted deployments where the
    provider's own API is only reachable through it.
    """
    probe = CREDENTIAL_PROBES.get(env_var)
    derived = False
    if probe:
        url, auth = probe
    else:
        # Нет курированной записи — выводим адрес из каталога/введённого
        # клиентом base_url. См. trix_derived_probes: такая проверка
        # имеет право не пустить клиента только при явном отказе в ключе.
        from hermes_cli.trix_derived_probes import derived_probe_url

        url = derived_probe_url(env_var, base_url)
        if not url:
            return {"ok": True, "reachable": False, "message": "", "reason": None, "status_code": None}
        auth = "bearer"
        derived = True
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
        if derived:
            # Адрес мы угадывали — молчание провайдера ничего не говорит
            # о ключе. Читается как «проверить не удалось», не как отказ.
            return {"ok": True, "reachable": False, "message": "", "reason": None, "status_code": None}
        return {"ok": False, "reachable": False,
                "message": "Не удалось связаться с провайдером для проверки ключа.",
                "reason": "network", "status_code": None}
    if resp.status_code in (401, 403):
        return {"ok": False, "reachable": True,
                "message": "Провайдер отклонил этот ключ. Проверьте его и попробуйте ещё раз.",
                "reason": "auth", "status_code": resp.status_code}
    if resp.status_code == 402:
        return {"ok": False, "reachable": True,
                "message": "На счету провайдера не осталось средств.",
                "reason": "billing", "status_code": resp.status_code}
    if resp.status_code == 429 or resp.is_success:
        return {"ok": True, "reachable": True, "message": "", "reason": None, "status_code": resp.status_code}
    if derived:
        # Не 401/402/403 на угаданном адресе — скорее всего у провайдера
        # просто нет `/models`. Про ключ это не говорит ничего.
        return {"ok": True, "reachable": False, "message": "", "reason": None, "status_code": resp.status_code}
    return {"ok": False, "reachable": True,
            "message": f"Провайдер ответил HTTP {resp.status_code} на этот ключ.",
            "reason": "other", "status_code": resp.status_code}
