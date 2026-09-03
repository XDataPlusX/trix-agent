"""Task 9b: form data endpoints — catalogs, live models, spot checks.

``app_env`` / ``logged_in`` / ``logged_in_with_saved_env`` fixtures live in
``tests/hermes_cli/conftest.py`` (shared with 9a/9c — see the fixture
docstrings there).
"""
from __future__ import annotations


def test_form_carries_catalogs(logged_in):
    r = logged_in.get("/api/form")
    data = r.json()
    assert data["providers"] and data["tools"]
    names = {p["name"] for p in data["providers"]}
    assert "nous" not in names


def test_form_carries_provider_groups_with_openai_as_one_group(logged_in):
    """Owner requirement 1: the grouped view (page.py's actual picker
    source) folds openai-codex/openai-api under one OpenAI group — never
    two top-level provider rows for the same vendor."""
    r = logged_in.get("/api/form")
    groups = r.json()["provider_groups"]
    assert groups
    by_id = {g["group_id"]: g for g in groups}
    assert "openai" in by_id
    variant_names = {v["name"] for v in by_id["openai"]["variants"]}
    assert variant_names == {"openai-codex", "openai-api"}
    # Never a second top-level row carrying either OpenAI variant.
    assert sum(1 for g in groups if any(v["name"] in variant_names for v in g["variants"])) == 1


def test_form_current_provider_carries_device_login_ok_flag(logged_in):
    r = logged_in.get("/api/form")
    assert "device_login_ok" in r.json()["current"]["provider"]


def test_form_masks_secrets_in_return_mode(logged_in_with_saved_env):
    r = logged_in_with_saved_env.get("/api/form")
    cur = r.json()["current"]
    assert cur["telegram_token"]["is_set"] is True
    assert "123:abc" not in r.text  # полного значения нет в ответе


def test_form_current_carries_camofox_url_unmasked(tmp_path, monkeypatch):
    """Round 2 fix: camofox_url is a non-secret localhost URL (the real
    Camofox on/off switch apply.py writes CAMOFOX_URL for) — it must come
    back in `current` as a plain value, like hass.url, never masked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import save_env_value_secure

    save_env_value_secure("CAMOFOX_URL", "http://localhost:9377")

    from hermes_cli.setup_wizard.state import WizardState

    login, pw = "trix-testlogin01", "Tr1xTestPassw0rd0000000000000"
    WizardState.load().issue_primary(login, pw)
    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app

    client = TestClient(create_app(), base_url="https://testserver")
    client.auth = (login, pw)

    data = client.get("/api/form").json()
    assert data["current"]["camofox_url"] == "http://localhost:9377"


def test_form_current_camofox_url_empty_first_time(logged_in):
    r = logged_in.get("/api/form")
    assert r.json()["current"]["camofox_url"] == ""


def test_form_no_secrets_first_time(logged_in):
    """First-run mode: nothing saved yet, is_set is False."""
    r = logged_in.get("/api/form")
    cur = r.json()["current"]
    assert cur["telegram_token"] == {"is_set": False}
    assert cur["provider"]["api_key"] == {"is_set": False}


def test_form_never_echoes_any_saved_secret_fragment(tmp_path, monkeypatch):
    """Finding 6: `/api/form` used to send the first/last 4 characters of
    every saved secret via `_mask()`'s `masked` field — visible in
    DevTools/Network/HAR even though the client no longer reads it. This
    is the server-side half of the "saved secrets never echo" invariant
    (the client half is covered by
    test_setup_wizard_page.py::test_saved_secret_placeholder_never_echoes_server_masked_value).
    Saves one secret per masked field `_current_state()`/`_current_tool_env()`
    produce and asserts none of them — not even a >=4-char fragment — shows
    up anywhere in the raw `/api/form` response body, and that the `masked`
    key itself is gone from the JSON shape.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import save_env_value_secure

    # Random-looking fragments, deliberately NOT overlapping with any
    # catalog copy (display names, descriptions, env var names) the same
    # response also legitimately carries — a fragment like "Teleg" would
    # collide with the literal word "Telegram" elsewhere in the JSON and
    # produce a false positive that has nothing to do with secret leakage.
    secrets = {
        "TELEGRAM_BOT_TOKEN": "9f8k2m7q1z:x0w5v9u3t6r4",
        "HASS_TOKEN": "qz8n4x1v7m2k9p5w3j6r0y",
        "ELEVENLABS_API_KEY": "zk9x2q7m4v1n8p5w3j6y0r",
        "OPENROUTER_API_KEY": "wq7z2x9m4v1n8p5k3j6y0r",
    }
    for key, value in secrets.items():
        save_env_value_secure(key, value)

    from hermes_cli.config import save_config
    from hermes_cli.setup_wizard.state import WizardState

    save_config({"model": {"provider": "openrouter"}})
    login, pw = "trix-testlogin01", "Tr1xTestPassw0rd0000000000000"
    WizardState.load().issue_primary(login, pw)

    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app

    client = TestClient(create_app(), base_url="https://testserver")
    client.auth = (login, pw)

    r = client.get("/api/form")
    assert r.status_code == 200
    body_text = r.text

    for value in secrets.values():
        # Neither the whole value nor any >=4-char fragment of it (the old
        # head/tail-4 masked shape) may appear anywhere in the response.
        for start in range(0, len(value) - 3):
            fragment = value[start : start + 4]
            assert fragment not in body_text, (
                f"fragment {fragment!r} of a saved secret leaked into /api/form"
            )

    data = r.json()
    assert data["current"]["telegram_token"] == {"is_set": True}
    assert data["current"]["provider"]["api_key"] == {"is_set": True}
    assert data["current"]["hass"]["token"] == {"is_set": True}
    assert data["current"]["tool_env"]["ELEVENLABS_API_KEY"] == {"is_set": True}
    assert '"masked"' not in body_text


# ---- tool_env / tool_provider read side (owner ruling 2026-08-20) -------


def test_form_current_tool_provider_fields_first_time(logged_in):
    """``tts.provider``/``stt.provider`` default to "edge"/"local" out of
    DEFAULT_CONFIG (the predvybor / recommended row for each category) —
    image_gen/video_gen have no such shipped default, so they read back
    empty until a client picks one."""
    r = logged_in.get("/api/form")
    cur = r.json()["current"]
    assert cur["tts_provider"] == "edge"
    assert cur["stt_provider"] == "local"
    assert cur["image_gen_provider"] == ""
    assert cur["video_gen_provider"] == ""


def test_form_current_tool_env_includes_nexara_key(tmp_path, monkeypatch):
    """NEXARA_API_KEY is a real "stt" catalog env var (Nexara's registry
    row, see tools_view.py's _stt_registry_rows()) — it must surface
    through current.tool_env exactly like every built-in provider-select
    category's env vars do."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import save_env_value_secure

    save_env_value_secure("NEXARA_API_KEY", "nx-real-secret")

    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app
    from hermes_cli.setup_wizard.state import WizardState

    login, pw = "trix-testlogin01", "Tr1xTestPassw0rd0000000000000"
    WizardState.load().issue_primary(login, pw)
    client = TestClient(create_app(), base_url="https://testserver")
    client.auth = (login, pw)

    r = client.get("/api/form")
    tool_env = r.json()["current"]["tool_env"]
    assert tool_env["NEXARA_API_KEY"]["is_set"] is True
    assert "nx-real-secret" not in r.text


def test_form_current_tool_env_masks_saved_keys(tmp_path, monkeypatch):
    """A saved ELEVENLABS_API_KEY (a real "tts" catalog env var) comes back
    masked under current.tool_env, keyed by env var name — same shape
    current.search_env already uses for "web"."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import save_env_value_secure

    save_env_value_secure("ELEVENLABS_API_KEY", "el-real-secret")

    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app
    from hermes_cli.setup_wizard.state import WizardState

    login, pw = "trix-testlogin01", "Tr1xTestPassw0rd0000000000000"
    WizardState.load().issue_primary(login, pw)
    client = TestClient(create_app(), base_url="https://testserver")
    client.auth = (login, pw)

    r = client.get("/api/form")
    tool_env = r.json()["current"]["tool_env"]
    assert tool_env["ELEVENLABS_API_KEY"]["is_set"] is True
    assert "el-real-secret" not in r.text


def test_form_carries_web_and_web_extract_as_separate_tool_blocks(logged_in):
    """The "web" split (2026-08-26): /api/form's `tools` array must carry
    TWO independent web-capability blocks — "web" (Поиск в интернете) and
    "web_extract" (Чтение страниц) — not one combined block."""
    r = logged_in.get("/api/form")
    categories = {b["category"] for b in r.json()["tools"]}
    assert "web" in categories
    assert "web_extract" in categories


def test_form_current_search_and_extract_backend_readback(tmp_path, monkeypatch):
    """`current.search_backend`/`current.extract_backend` read back
    web.search_backend / web.extract_backend independently — the client
    can have configured different providers for each capability."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "web:\n  search_backend: ddgs\n  extract_backend: tavily\n", encoding="utf-8"
    )

    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app
    from hermes_cli.setup_wizard.state import WizardState

    login, pw = "trix-testlogin01", "Tr1xTestPassw0rd0000000000000"
    WizardState.load().issue_primary(login, pw)
    client = TestClient(create_app(), base_url="https://testserver")
    client.auth = (login, pw)

    data = client.get("/api/form").json()
    assert data["current"]["search_backend"] == "ddgs"
    assert data["current"]["extract_backend"] == "tavily"


def test_form_current_extract_env_masks_saved_key(tmp_path, monkeypatch):
    """A saved TAVILY_API_KEY, with web.extract_backend pointing at
    tavily, comes back masked under current.extract_env — same shape
    current.search_env already uses, resolved against the SEPARATE
    "web_extract" catalog block."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_constants import get_hermes_home
    from hermes_cli.config import save_env_value_secure

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("web:\n  extract_backend: tavily\n", encoding="utf-8")
    save_env_value_secure("TAVILY_API_KEY", "tvly-real-secret")

    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app
    from hermes_cli.setup_wizard.state import WizardState

    login, pw = "trix-testlogin01", "Tr1xTestPassw0rd0000000000000"
    WizardState.load().issue_primary(login, pw)
    client = TestClient(create_app(), base_url="https://testserver")
    client.auth = (login, pw)

    r = client.get("/api/form")
    extract_env = r.json()["current"]["extract_env"]
    assert extract_env["env_var"] == "TAVILY_API_KEY"
    assert extract_env["is_set"] is True
    assert "tvly-real-secret" not in r.text


def test_form_current_tool_provider_reads_config_yaml(tmp_path, monkeypatch):
    """A saved tts.provider in config.yaml comes back verbatim (plain
    value, not masked — it's a provider selector, not a credential)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("tts:\n  provider: elevenlabs\n", encoding="utf-8")

    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app
    from hermes_cli.setup_wizard.state import WizardState

    login, pw = "trix-testlogin01", "Tr1xTestPassw0rd0000000000000"
    WizardState.load().issue_primary(login, pw)
    client = TestClient(create_app(), base_url="https://testserver")
    client.auth = (login, pw)

    data = client.get("/api/form").json()
    assert data["current"]["tts_provider"] == "elevenlabs"


def test_models_endpoint_delegates(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "fetch_live_models", lambda *a: ["m1", "m2"])
    r = logged_in.post(
        "/api/models", json={"provider": "openrouter", "api_key": "k", "base_url": ""}
    )
    assert r.json()["models"] == ["m1", "m2"]


def test_check_telegram_delegates(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    r = logged_in.post("/api/check/telegram", json={"token": "t", "proxy": ""})
    assert r.json() == {"ok": True, "username": "trixbot"}


def test_check_telegram_malformed_proxy_never_touches_the_network(logged_in, monkeypatch):
    """Finding 13: the step-2 "Проверить" preview button (/api/check/telegram,
    never routes through _run_submit's own early gate) must ALSO reject a
    malformed proxy before it ever reaches httpx — check_telegram_token's
    own internal check_proxy_syntax call covers this route."""
    import hermes_cli.setup_wizard.validate as v

    def _fail_if_called(*a, **k):
        raise AssertionError("must not construct an httpx.Client for a malformed proxy")

    monkeypatch.setattr(v.httpx, "Client", _fail_if_called)

    r = logged_in.post("/api/check/telegram", json={"token": "123:abc", "proxy": "1.2.3.4:1080"})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body.get("proxy_invalid") is True
    assert not body.get("network")


def test_check_telegram_user_delegates(logged_in, monkeypatch):
    """Owner feedback п.4: /api/check/telegram_user must delegate straight
    to validate.check_telegram_user with the id and proxy from the
    request body — the "кто это" lookup for the "Ваш Telegram id" field."""
    from hermes_cli.setup_wizard import app as wapp

    captured = {}

    def _fake(token, user_id, proxy):
        captured["args"] = (token, user_id, proxy)
        return {"ok": True, "name": "Иван Петров", "username": "ivanpetrov"}

    monkeypatch.setattr(wapp, "check_telegram_user", _fake)
    r = logged_in.post(
        "/api/check/telegram_user",
        json={"token": "123:abc", "user_id": "555", "proxy": "socks5://h:1080"},
    )
    assert r.json() == {"ok": True, "name": "Иван Петров", "username": "ivanpetrov"}
    assert captured["args"] == ("123:abc", "555", "socks5://h:1080")


def test_check_telegram_user_negative_answer_is_never_upgraded_to_an_error(logged_in, monkeypatch):
    """The endpoint must pass a flat ``{"ok": False}`` straight through —
    never wrap it in an HTTP error status the client would have to treat
    as "something went wrong" instead of "can't confirm yet"."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_telegram_user", lambda *a: {"ok": False})
    r = logged_in.post(
        "/api/check/telegram_user", json={"token": "123:abc", "user_id": "555", "proxy": ""}
    )
    assert r.status_code == 200
    assert r.json() == {"ok": False}


def test_check_key_delegates(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "checked": True})
    r = logged_in.post("/api/check/key", json={"env_var": "OPENROUTER_API_KEY", "value": "k"})
    assert r.json() == {"ok": True, "checked": True}


def test_check_proxy_delegates(logged_in, monkeypatch):
    """Spec A4: the "Прокси" step's auto-check-on-entry probe — a thin
    route over validate.check_reachability, no token involved. Also
    verifies the endpoint attaches the group-keyed ``providers`` view on
    top of whatever check_reachability returns."""
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(
        wapp,
        "check_reachability",
        lambda proxy: calls.append(proxy)
        or {
            "telegram": True,
            "via_proxy": {"openai-api": False, "anthropic": True, "openrouter": True},
            "direct": {"deepseek": True, "zai": True, "gemini": True},
        },
    )
    r = logged_in.post("/api/check/proxy", json={"proxy": "socks5://u:p@h:1080"})
    assert r.status_code == 200
    body = r.json()
    assert body["telegram"] is True
    assert body["via_proxy"] == {"openai-api": False, "anthropic": True, "openrouter": True}
    assert body["direct"] == {"deepseek": True, "zai": True, "gemini": True}
    # openai-api re-keys to its catalog group_id "openai"; the other five
    # slugs are ungrouped/single-member and already equal their group_id.
    assert body["providers"] == {
        "openai": False,
        "anthropic": True,
        "openrouter": True,
        "deepseek": True,
        "zai": True,
        "google": True,
    }
    assert calls == ["socks5://u:p@h:1080"]


def test_check_proxy_reports_proxy_invalid_end_to_end(logged_in, monkeypatch):
    """Finding 1 (review after the "Прокси"/"Telegram" step swap):
    check_reachability() computes proxy_invalid but the endpoint used to
    return the dict as-is WITHOUT it ever having been put there — the
    client had no way to tell a malformed proxy ("1.2.3.4:1080", no
    scheme) apart from "nothing answered" and blocked the client on step 2
    with the wrong advice ("нужен прокси") for a proxy they had already
    typed. This is the endpoint-plumbing half of the fix (validate.py's
    own test_check_reachability_reports_proxy_invalid_on_malformed_proxy
    covers the function itself) — same delegate-mocking pattern as
    test_check_proxy_delegates above, so no real network call happens."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(
        wapp,
        "check_reachability",
        lambda proxy: {
            "telegram": False,
            "via_proxy": {"openai-api": False, "anthropic": False, "openrouter": False},
            "direct": {"deepseek": True, "zai": True, "gemini": True},
            "proxy_invalid": True,
        },
    )
    r = logged_in.post("/api/check/proxy", json={"proxy": "1.2.3.4:1080"})
    assert r.status_code == 200
    body = r.json()
    assert body["proxy_invalid"] is True
    assert body["telegram"] is False


def test_check_proxy_forwards_empty_proxy_as_none(logged_in, monkeypatch):
    """Spec A4: an empty proxy is the legal "do I need a proxy at all"
    input — must reach check_reachability as ``None``, matching every
    other check-endpoint's own ``body.proxy or None`` convention (not the
    literal empty string, which check_reachability itself also treats the
    same way — this asserts the route's own plumbing, not the function's
    internal normalization)."""
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(
        wapp,
        "check_reachability",
        lambda proxy: calls.append(proxy)
        or {"telegram": True, "via_proxy": {}, "direct": {}},
    )
    r = logged_in.post("/api/check/proxy", json={"proxy": ""})
    assert r.status_code == 200
    assert calls == [None]


def test_reachability_providers_by_group_regroups_openai_and_gemini():
    """Unit check on the re-keying helper itself: openai-api -> openai,
    gemini -> google; every ungrouped slug (deepseek/zai/openrouter/
    anthropic) passes through unchanged."""
    from hermes_cli.setup_wizard import app as wapp

    out = wapp._reachability_providers_by_group(
        {
            "via_proxy": {"openai-api": True, "anthropic": False, "openrouter": True},
            "direct": {"deepseek": True, "zai": False, "gemini": True},
        }
    )
    assert out == {
        "openai": True,
        "anthropic": False,
        "openrouter": True,
        "deepseek": True,
        "zai": False,
        "google": True,
    }




def test_models_endpoint_forwards_the_form_proxy(logged_in, monkeypatch):
    """Owner requirement: RU-hosted deployments often can't reach
    OpenAI/OpenRouter/Anthropic's model catalog directly — the form's own
    proxy field must reach fetch_live_models."""
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(wapp, "fetch_live_models", lambda *a: calls.append(a) or ["m1"])
    r = logged_in.post(
        "/api/models",
        json={"provider": "openrouter", "api_key": "k", "base_url": "", "proxy": "socks5://u:p@h:1080"},
    )
    assert r.json()["models"] == ["m1"]
    assert calls == [("openrouter", "k", "", "socks5://u:p@h:1080")]


def test_models_endpoint_passes_none_for_empty_proxy(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(wapp, "fetch_live_models", lambda *a: calls.append(a) or [])
    logged_in.post("/api/models", json={"provider": "openrouter", "api_key": "k", "base_url": ""})
    assert calls == [("openrouter", "k", "", None)]


def test_check_key_forwards_the_form_proxy(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: calls.append(a) or {"ok": True, "checked": True})
    r = logged_in.post(
        "/api/check/key",
        json={"env_var": "OPENROUTER_API_KEY", "value": "k", "proxy": "http://h:8080"},
    )
    assert r.json() == {"ok": True, "checked": True}
    assert calls == [("OPENROUTER_API_KEY", "k", "http://h:8080")]




def test_models_endpoint_uses_provider_model_ids_for_device_code_provider(logged_in, monkeypatch):
    """Device-code providers authenticate through the Hermes auth store,
    not a client-supplied api_key — /api/models must route them to
    hermes_cli.models.provider_model_ids (the SAME source `hermes model`'s
    own picker uses for openai-codex), never fetch_live_models."""
    import hermes_cli.models as models_mod
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(models_mod, "provider_model_ids", lambda provider, **kw: calls.append(provider) or ["gpt-5.3-codex"])
    monkeypatch.setattr(wapp, "fetch_live_models", lambda *a: (_ for _ in ()).throw(AssertionError("must not be called")))

    r = logged_in.post(
        "/api/models", json={"provider": "openai-codex", "api_key": "", "base_url": ""}
    )

    assert r.status_code == 200
    assert r.json()["models"] == ["gpt-5.3-codex"]
    assert calls == ["openai-codex"]


def test_models_rejects_unknown_provider(logged_in, monkeypatch):
    """A provider outside the wizard's own catalog — including a
    product-excluded one like "nous" (spec §2's direct rule) — must never
    reach fetch_live_models."""
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(wapp, "fetch_live_models", lambda *a: calls.append(a))
    r = logged_in.post(
        "/api/models", json={"provider": "nous", "api_key": "k", "base_url": ""}
    )
    assert r.status_code == 400
    assert calls == []


def test_check_key_rejects_unknown_env_var(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: calls.append(a))
    r = logged_in.post(
        "/api/check/key", json={"env_var": "SOME_RANDOM_VAR", "value": "x"}
    )
    assert r.status_code == 400
    assert calls == []


def test_check_key_none_env_var_still_allowed(logged_in, monkeypatch):
    """``env_var: None`` is the legal "no live check for this field" case
    (see validate.check_provider_key) — it must not be caught by the
    allow-list gate."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "checked": False})
    r = logged_in.post("/api/check/key", json={"env_var": None, "value": ""})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "checked": False}


def test_check_key_accepts_tool_catalog_env_var(logged_in, monkeypatch):
    """FAL_KEY/FIRECRAWL_API_KEY aren't providers — they come from the
    "изменить" tool catalog's rows, and must still be legal."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "checked": True})
    for env_var in ("FAL_KEY", "FIRECRAWL_API_KEY"):
        r = logged_in.post("/api/check/key", json={"env_var": env_var, "value": "x"})
        assert r.status_code == 200, env_var


def test_check_key_rejects_hass_token_after_a5_retreat(logged_in, monkeypatch):
    """Spec A5: "homeassistant" left WIZARD_TOOL_CATEGORIES (owner ruling
    2026-08-23) — the wizard no longer renders that block at all, so
    HASS_TOKEN is no longer a legal ``/api/check/key`` target through this
    endpoint (the CLI's own hass write/clear path is untouched — see
    tools_view.py's EXCLUDED_CATEGORIES entry for HASS_TOKEN's reason).
    This reverses the old ``test_check_key_accepts_tool_catalog_env_var``
    expectation for HASS_TOKEN specifically."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "checked": True})
    r = logged_in.post("/api/check/key", json={"env_var": "HASS_TOKEN", "value": "x"})
    assert r.status_code == 400


def test_removed_install_route_returns_404(logged_in):
    """Owner ruling 2026-08-24 ("Установка инструментов — кнопки нет"):
    the standalone /api/install endpoint is gone, not just unlinked from
    the markup — a real 404, not a 401/403/400, proves the route itself
    was removed from the app rather than merely hidden behind a guard."""
    r = logged_in.post("/api/install", json={"post_setup_key": "camofox"})
    assert r.status_code == 404


def test_form_tools_cached_across_repeated_calls(logged_in, monkeypatch):
    """wizard_tool_blocks() spawns subprocesses — the wizard must cache the
    result across /api/form calls for the process's lifetime (see
    app.py's _cached_tool_blocks). Owner ruling 2026-08-24 ("Установка
    инструментов — кнопки нет") removed the standalone /api/install route
    this test used to also cover as a cache-refresh trigger — that
    refresh now happens inside /api/submit's own install stage instead
    (see test_setup_wizard_app_submit.py's
    test_reset_tool_cache_called_twice_when_install_stage_runs)."""
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    original = wapp.wizard_tool_blocks

    def _counting(*a, **k):
        calls.append(1)
        return original(*a, **k)

    monkeypatch.setattr(wapp, "wizard_tool_blocks", _counting)

    logged_in.get("/api/form")
    logged_in.get("/api/form")
    assert len(calls) == 1  # second call served from cache


def test_all_form_endpoints_require_session(app_env):
    client, _ = app_env
    assert client.get("/api/form").status_code == 401
    assert client.post(
        "/api/models", json={"provider": "openrouter", "api_key": "", "base_url": ""}
    ).status_code == 401
    assert client.post("/api/check/telegram", json={"token": "t", "proxy": ""}).status_code == 401
    assert client.post(
        "/api/check/key", json={"env_var": None, "value": ""}
    ).status_code == 401
    assert client.post("/api/check/proxy", json={"proxy": "socks5://u:p@h:1080"}).status_code == 401


def test_form_endpoints_never_log_secrets(logged_in_with_saved_env, monkeypatch, caplog):
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "fetch_live_models", lambda *a: ["m1"])
    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "checked": True})
    with caplog.at_level("DEBUG"):
        logged_in_with_saved_env.get("/api/form")
        logged_in_with_saved_env.post(
            "/api/models",
            json={"provider": "openrouter", "api_key": "SUPERSECRETKEY", "base_url": ""},
        )
        logged_in_with_saved_env.post(
            "/api/check/telegram", json={"token": "123:abcTOKEN", "proxy": ""}
        )
        logged_in_with_saved_env.post(
            "/api/check/key", json={"env_var": "OPENROUTER_API_KEY", "value": "ANOTHERSECRET"}
        )
    assert "SUPERSECRETKEY" not in caplog.text
    assert "123:abcTOKEN" not in caplog.text
    assert "ANOTHERSECRET" not in caplog.text
    assert "123:abc" not in caplog.text  # saved TELEGRAM_BOT_TOKEN from the fixture


# --- Часовой пояс (спека 11) -------------------------------------------


def test_form_carries_timezone_groups_with_russia_first(logged_in):
    """Форма рисуется из ответа, а не из зашитого в страницу списка."""
    data = logged_in.get("/api/form").json()
    groups = data["timezones"]
    assert groups
    assert groups[0]["title"] == "Россия"
    assert any(z["name"] == "Europe/Moscow" for z in groups[0]["zones"])


def test_form_offers_every_zone_the_runtime_knows(logged_in):
    """Владелец: нужны ВСЕ пояса, клиент может быть откуда угодно.

    Обе стороны сравнения берутся из разных мест — ответ эндпоинта против
    стандартной библиотеки, — поэтому это инвариант, а не снимок.
    """
    from zoneinfo import available_timezones

    data = logged_in.get("/api/form").json()
    offered = {z["name"] for g in data["timezones"] for z in g["zones"]}
    assert offered == available_timezones()


def test_form_current_timezone_is_empty_before_the_client_answers(logged_in):
    assert logged_in.get("/api/form").json()["current"]["timezone"] == ""


def test_form_current_timezone_reads_config_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import yaml

    from hermes_constants import get_hermes_home

    (get_hermes_home()).mkdir(parents=True, exist_ok=True)
    (get_hermes_home() / "config.yaml").write_text(
        yaml.safe_dump({"timezone": "Asia/Yekaterinburg"}), encoding="utf-8"
    )
    from hermes_cli.setup_wizard.app import _current_state

    assert _current_state({"timezone": "Asia/Yekaterinburg"}, [])["timezone"] == "Asia/Yekaterinburg"


def test_form_reports_no_scheduled_jobs_when_there_is_no_jobs_file(logged_in):
    """Нет файла — задач нет. Это знание, а не догадка: предупреждать не о чем."""
    assert logged_in.get("/api/form").json()["cron_jobs"] == 0


def test_form_counts_the_scheduled_jobs_that_exist(tmp_path, monkeypatch, logged_in):
    """Форма файла — та, которую пишет сам планировщик.

    `cron/jobs.py` сохраняет словарь `{"jobs": [...], "updated_at": ...}`.
    Фикстура из голого списка мерила бы форму, которой у клиента на диске
    не бывает, и слепо прошла бы мимо настоящей.
    """
    import json

    from hermes_constants import get_hermes_home

    cron_dir = get_hermes_home() / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "a"}, {"id": "b"}, {"id": "c"}], "updated_at": "x"}),
        encoding="utf-8",
    )
    assert logged_in.get("/api/form").json()["cron_jobs"] == 3


def test_form_counts_jobs_in_the_repaired_bare_list_shape_too(tmp_path, monkeypatch, logged_in):
    """Планировщик принимает голый список как форму авторемонта.

    Раз он на такой машине работает, мастер обязан видеть те же задачи, а
    не молчать о них.
    """
    import json

    from hermes_constants import get_hermes_home

    cron_dir = get_hermes_home() / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps([{"id": "a"}, {"id": "b"}]), encoding="utf-8"
    )
    assert logged_in.get("/api/form").json()["cron_jobs"] == 2


def test_a_windows_written_jobs_file_is_still_counted(tmp_path, monkeypatch, logged_in):
    """BOM в начале файла — не повод объявить задачи непроверяемыми.

    `cron/jobs.py` читает `utf-8-sig` и такой файл разбирает; мастер,
    читавший бы обычный utf-8, сказал бы «проверить не удалось» там, где
    планировщик прекрасно работает.
    """
    import json

    from hermes_constants import get_hermes_home

    cron_dir = get_hermes_home() / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(
        "\ufeff" + json.dumps({"jobs": [{"id": "a"}], "updated_at": "x"}),
        encoding="utf-8",
    )
    assert logged_in.get("/api/form").json()["cron_jobs"] == 1


def test_form_says_it_does_not_know_when_the_jobs_file_is_unreadable(tmp_path, monkeypatch, logged_in):
    """Третий исход существует отдельно от нуля.

    «Файла нет» и «файл есть, но разобрать его не вышло» — разные
    утверждения, и мастер не вправе выдавать второе за первое: иначе он
    промолчит о задачах, которые на самом деле есть.
    """
    from hermes_constants import get_hermes_home

    cron_dir = get_hermes_home() / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text("{не json", encoding="utf-8")
    assert logged_in.get("/api/form").json()["cron_jobs"] is None
