from unittest.mock import patch, MagicMock


def test_probes_table_shape():
    from hermes_cli.credential_probes import CREDENTIAL_PROBES
    assert CREDENTIAL_PROBES  # непустая
    for env_var, (url, auth) in CREDENTIAL_PROBES.items():
        assert env_var.isupper()
        assert url.startswith("https://")
        assert auth in ("bearer", "query")


def test_probe_rejects_bad_key():
    from hermes_cli import credential_probes as cp
    resp = MagicMock(status_code=401, is_success=False)
    client = MagicMock()
    client.__enter__ = lambda s: client
    client.__exit__ = lambda s, *a: False
    client.get.return_value = resp
    with patch.object(cp.httpx, "Client", return_value=client):
        out = cp.probe_provider_key("OPENROUTER_API_KEY", "bad")
    assert out["ok"] is False and out["reachable"] is True


def test_probe_unknown_provider_does_not_block():
    from hermes_cli.credential_probes import probe_provider_key
    out = probe_provider_key("NO_SUCH_KEY", "x")
    assert out["ok"] is True and out["reachable"] is False


def test_web_server_uses_shared_table():
    # поведенческая связка, не чтение исходника: дашборд и лист-модуль —
    # один объект
    from hermes_cli import credential_probes
    from hermes_cli import web_server
    assert web_server._CREDENTIAL_PROBES is credential_probes.CREDENTIAL_PROBES


def test_probe_uses_proxy_when_given():
    """RU-hosted deployments: OpenAI/OpenRouter/Anthropic's own API is
    often unreachable from the data center directly — the wizard's form
    proxy must reach the actual httpx.Client the live probe opens."""
    from hermes_cli import credential_probes as cp

    resp = MagicMock(status_code=200, is_success=True)
    client = MagicMock()
    client.__enter__ = lambda s: client
    client.__exit__ = lambda s, *a: False
    client.get.return_value = resp
    with patch.object(cp.httpx, "Client", return_value=client) as mk:
        out = cp.probe_provider_key("OPENAI_API_KEY", "sk-live", proxy="http://h:8080")
    assert out["ok"] is True
    assert mk.call_args.kwargs.get("proxy") == "http://h:8080"


def test_probe_omits_proxy_kwarg_when_not_given():
    """No proxy configured -> no `proxy` kwarg reaches httpx.Client at all
    (not an explicit `proxy=None`) — same convention
    ``setup_wizard.validate.check_telegram_token`` already established."""
    from hermes_cli import credential_probes as cp

    resp = MagicMock(status_code=200, is_success=True)
    client = MagicMock()
    client.__enter__ = lambda s: client
    client.__exit__ = lambda s, *a: False
    client.get.return_value = resp
    with patch.object(cp.httpx, "Client", return_value=client) as mk:
        cp.probe_provider_key("OPENAI_API_KEY", "sk-live")
    assert "proxy" not in mk.call_args.kwargs


# ---------------------------------------------------------------------------
# `reason`/`status_code` — structural classification of the SAME response
# already fetched (support-page follow-up, 2026-09-03). Never a second
# request: every test below still stubs exactly one httpx.Client.get call.
# ---------------------------------------------------------------------------


def _client_returning(status_code: int, is_success: bool) -> MagicMock:
    resp = MagicMock(status_code=status_code, is_success=is_success)
    client = MagicMock()
    client.__enter__ = lambda s: client
    client.__exit__ = lambda s, *a: False
    client.get.return_value = resp
    return client


def test_probe_auth_reason_on_401_and_403():
    from hermes_cli import credential_probes as cp

    for code in (401, 403):
        client = _client_returning(code, is_success=False)
        with patch.object(cp.httpx, "Client", return_value=client):
            out = cp.probe_provider_key("OPENROUTER_API_KEY", "bad")
        assert out["ok"] is False
        assert out["reason"] == "auth"
        assert out["status_code"] == code


def test_probe_billing_reason_on_402():
    """402 — precedented in this codebase by doctor.py's own OpenRouter
    probe treating the same status as "out of credits"."""
    from hermes_cli import credential_probes as cp

    client = _client_returning(402, is_success=False)
    with patch.object(cp.httpx, "Client", return_value=client):
        out = cp.probe_provider_key("OPENROUTER_API_KEY", "k")
    assert out["ok"] is False
    assert out["reason"] == "billing"
    assert out["status_code"] == 402


def test_probe_network_reason_when_the_request_itself_fails():
    from hermes_cli import credential_probes as cp

    with patch.object(cp.httpx, "Client", side_effect=RuntimeError("boom")):
        out = cp.probe_provider_key("OPENROUTER_API_KEY", "k")
    assert out["ok"] is False
    assert out["reachable"] is False
    assert out["reason"] == "network"
    assert out["status_code"] is None


def test_probe_other_reason_on_an_unclassified_status():
    from hermes_cli import credential_probes as cp

    client = _client_returning(500, is_success=False)
    with patch.object(cp.httpx, "Client", return_value=client):
        out = cp.probe_provider_key("OPENROUTER_API_KEY", "k")
    assert out["ok"] is False
    assert out["reason"] == "other"
    assert out["status_code"] == 500


def test_probe_ok_result_carries_no_reason():
    from hermes_cli import credential_probes as cp

    client = _client_returning(200, is_success=True)
    with patch.object(cp.httpx, "Client", return_value=client):
        out = cp.probe_provider_key("OPENROUTER_API_KEY", "k")
    assert out["ok"] is True
    assert out["reason"] is None
