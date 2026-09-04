"""E2E tests for the wizard's apply step (spec §10.2, §13).

Real imports, temp ``HERMES_HOME``, no mocks on the write path — per the
repo rule ("Never read source code in tests" / E2E validation for anything
touching config propagation). Each test exercises ``apply_settings``
against the actual writers (``save_env_value_secure``,
``save_provider_env_credential``, ``_update_config_for_provider``,
``save_config``) and reads the resulting files back off disk.
"""
import shutil
from pathlib import Path

import yaml

from hermes_constants import get_hermes_home

REPO_ROOT = Path(__file__).resolve().parents[2]
_TRIX_TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"

FORM = {
    "telegram_token": "123:abc",
    "allowed_users": "111,222",
    "proxy": "",
    "provider": {
        "name": "openrouter",
        "env_var": "OPENROUTER_API_KEY",
        "api_key": "sk-or-test",
        "base_url": "",
        "model": "z-ai/glm-5.2",
    },
}


def test_apply_writes_env_and_model(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    out = apply_settings(dict(FORM))
    assert out["ok"], out

    env_text = (get_hermes_home() / ".env").read_text()
    assert "OPENROUTER_API_KEY=" in env_text and "sk-or-test" in env_text
    assert "TELEGRAM_BOT_TOKEN=" in env_text

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert raw["model"]["provider"] == "openrouter"
    assert raw["model"]["default"] == "z-ai/glm-5.2"


def test_camofox_url_activates_the_real_runtime_switch(tmp_path, monkeypatch):
    """Round 2 fix: CAMOFOX_URL — not browser.cloud_provider — is what
    tools/browser_camofox.py::is_camofox_mode() actually reads
    (bool(get_secret("CAMOFOX_URL"))). Persisted through the same
    .env writer as HASS_URL — see apply_settings()'s own docstring."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["camofox_url"] = "http://localhost:9377"
    out = apply_settings(form)
    assert out["ok"], out
    assert "CAMOFOX_URL" in out["written"]

    env_text = (get_hermes_home() / ".env").read_text()
    assert "CAMOFOX_URL=" in env_text and "http://localhost:9377" in env_text


def test_camofox_url_empty_is_a_no_op(tmp_path, monkeypatch):
    """Optional-field no-op contract: an empty/missing camofox_url must
    never write CAMOFOX_URL at all — same as every other optional field."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    out = apply_settings(dict(FORM))
    assert out["ok"], out
    assert "CAMOFOX_URL" not in out["written"]
    env_path = get_hermes_home() / ".env"
    if env_path.exists():
        assert "CAMOFOX_URL=" not in env_path.read_text()


def test_gateway_loader_sees_the_same_values(tmp_path, monkeypatch):
    """Три загрузчика (AGENTS.md, Config loaders): проверяем путь шлюза —
    сырой YAML, без CLI-дефолтов."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    apply_settings(dict(FORM))
    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert raw["model"]["provider"] == "openrouter"


def test_firecrawl_swaps_toolset_idempotently(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import swap_search_to_web

    cfg = {"platform_toolsets": {"telegram": ["terminal", "search", "todo"]}}
    assert swap_search_to_web(cfg) is True
    assert cfg["platform_toolsets"]["telegram"].count("web") == 1
    assert "search" not in cfg["platform_toolsets"]["telegram"]
    assert swap_search_to_web(cfg) is False  # идемпотентно


# ---- extract_backend / extract_env: the "web" split's second capability
# (2026-08-26) — apply_settings()'s own docstring documents the contract.


def test_extract_backend_writes_web_extract_backend(tmp_path, monkeypatch):
    """`extract_backend` writes `web.extract_backend` independently of
    `search_backend` — the two capabilities can name DIFFERENT providers."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["search_backend"] = "ddgs"
    form["extract_backend"] = "tavily"
    form["extract_env"] = {"key": "TAVILY_API_KEY", "value": "tvly-test"}
    out = apply_settings(form)
    assert out["ok"], out
    assert "web.search_backend" in out["written"]
    assert "web.extract_backend" in out["written"]

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert raw["web"]["search_backend"] == "ddgs"
    assert raw["web"]["extract_backend"] == "tavily"

    env_text = (get_hermes_home() / ".env").read_text()
    assert "TAVILY_API_KEY=" in env_text and "tvly-test" in env_text


def test_extract_env_same_key_as_search_env_is_not_duplicated(tmp_path, monkeypatch):
    """Picking the SAME provider for both search and extract (so
    search_env.key == extract_env.key) must write the credential exactly
    ONCE — never a duplicate .env write or a duplicate `written` entry."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["search_backend"] = "firecrawl"
    form["search_env"] = {"key": "FIRECRAWL_API_KEY", "value": "fc-test"}
    form["extract_backend"] = "firecrawl"
    form["extract_env"] = {"key": "FIRECRAWL_API_KEY", "value": "fc-test"}
    out = apply_settings(form)
    assert out["ok"], out
    assert out["written"].count("FIRECRAWL_API_KEY") == 1

    env_text = (get_hermes_home() / ".env").read_text()
    assert env_text.count("FIRECRAWL_API_KEY=") == 1


def test_extract_env_different_key_from_search_env_writes_both(tmp_path, monkeypatch):
    """Different providers for search vs. extract each get their own
    credential written — the dedup only fires on a matching key."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["search_backend"] = "brave-free"
    form["search_env"] = {"key": "BRAVE_SEARCH_API_KEY", "value": "brave-test"}
    form["extract_backend"] = "exa"
    form["extract_env"] = {"key": "EXA_API_KEY", "value": "exa-test"}
    out = apply_settings(form)
    assert out["ok"], out
    assert "BRAVE_SEARCH_API_KEY" in out["written"]
    assert "EXA_API_KEY" in out["written"]

    env_text = (get_hermes_home() / ".env").read_text()
    assert "brave-test" in env_text and "exa-test" in env_text


def test_extract_backend_alone_swaps_toolset_even_without_search_backend_match(tmp_path, monkeypatch):
    """The web toolset swap (search -> web) is keyed on EXTRACT activation
    only — picking an extract-capable backend for `extract_backend` (with
    a real credential) upgrades the toolset regardless of what
    `search_backend` is set to (ddgs here — a search-only backend that
    could never trigger the OLD firecrawl-only swap)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  telegram: [terminal, search, todo]\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("", encoding="utf-8")

    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["search_backend"] = "ddgs"
    form["extract_backend"] = "parallel"
    form["extract_env"] = {"key": "PARALLEL_API_KEY", "value": "parallel-test"}
    out = apply_settings(form)
    assert out["ok"], out

    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert "web" in raw["platform_toolsets"]["telegram"]
    assert "search" not in raw["platform_toolsets"]["telegram"]


def test_search_backend_extract_capable_with_key_implicitly_activates_extract(tmp_path, monkeypatch):
    """Finding 3 (review 2026-08-26, owner-approved fix): the OLD
    firecrawl-only trigger this replaced upgraded the toolset whenever a
    client picked Firecrawl for search (the wizard's ONE combined block
    at the time). The 2026-08-26 search/extract split regressed that —
    picking Firecrawl for search alone, without opening the new separate
    "Чтение страниц" row, silently stopped granting extraction for a paid
    key the client entered in good faith. Restored, generalized: when
    extract is untouched (no earlier save, no explicit pick this round)
    and the chosen search backend is itself extract-capable with a usable
    credential, apply_settings() implicitly defaults extract to it — both
    the config value AND the toolset swap."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  telegram: [terminal, search, todo]\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("", encoding="utf-8")

    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["search_backend"] = "firecrawl"
    form["search_env"] = {"key": "FIRECRAWL_API_KEY", "value": "fc-test"}
    out = apply_settings(form)
    assert out["ok"], out
    assert not out["warnings"], out

    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert raw["web"]["extract_backend"] == "firecrawl"
    assert "web" in raw["platform_toolsets"]["telegram"]
    assert "search" not in raw["platform_toolsets"]["telegram"]


def test_search_backend_not_extract_capable_never_swaps_toolset(tmp_path, monkeypatch):
    """The implicit-default branch above (finding 3) only ever fires for a
    search backend that can actually extract — brave-free (search-only,
    not in _extract_capable_web_backends()) must never upgrade the
    toolset just because it has its own credential."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  telegram: [terminal, search, todo]\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("", encoding="utf-8")

    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["search_backend"] = "brave-free"
    form["search_env"] = {"key": "BRAVE_SEARCH_API_KEY", "value": "brave-test"}
    out = apply_settings(form)
    assert out["ok"], out

    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert raw["web"]["search_backend"] == "brave-free"
    assert "extract_backend" not in raw["web"]
    assert "search" in raw["platform_toolsets"]["telegram"]
    assert "web" not in raw["platform_toolsets"]["telegram"]


def test_extract_backend_no_credential_is_not_written_and_warns(tmp_path, monkeypatch):
    """Finding 2 (review 2026-08-26, owner-approved fix): a chosen
    extract_backend with no resolvable credential anywhere (not typed
    this round, not already in .env) must never be written — every
    backend the live "web_extract" catalog can offer (exa/firecrawl/
    parallel/tavily) needs a key, so an unbacked value here would make
    config.yaml/the summary screen claim a capability the agent doesn't
    actually have. `ddgs` (keyless — plugins/web/ddgs's own
    get_setup_schema() reports no env_vars, and it isn't a legal
    "web_extract" choice in the real catalog either — app.py validates
    that separately) stands in for "picked a backend, no credential
    resolves" here; the point under test is apply_settings()'s own
    credential gate, not catalog legality. Not written; surfaced instead
    as a non-fatal warning rather than failing the whole submission."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  telegram: [terminal, search, todo]\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("", encoding="utf-8")

    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["extract_backend"] = "ddgs"
    out = apply_settings(form)
    assert out["ok"], out
    assert out["warnings"], out

    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert "extract_backend" not in raw.get("web", {})
    assert "search" in raw["platform_toolsets"]["telegram"]
    assert "web" not in raw["platform_toolsets"]["telegram"]


def test_extract_backend_with_credential_but_not_extract_capable_never_swaps(tmp_path, monkeypatch):
    """apply_settings() stays a "dumb writer" at the WRITE site (catalog
    legality is app.py's job — see this module's own docstring), so a
    credentialed-but-not-extract-capable value still gets written through
    (matches the OLD pre-finding-2 "dumb writer" contract). The toolset
    swap, however, is NOT optional: it must never grant `web_extract` for
    a backend that can't actually extract, credential or not."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  telegram: [terminal, search, todo]\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("", encoding="utf-8")

    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["extract_backend"] = "brave-free"
    form["extract_env"] = {"key": "BRAVE_SEARCH_API_KEY", "value": "brave-test"}
    out = apply_settings(form)
    assert out["ok"], out

    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert raw["web"]["extract_backend"] == "brave-free"
    assert "search" in raw["platform_toolsets"]["telegram"]
    assert "web" not in raw["platform_toolsets"]["telegram"]


def test_extract_backend_already_configured_credential_still_swaps(tmp_path, monkeypatch):
    """Mutation of the OLD "written this round only" gap: a client who
    already saved TAVILY_API_KEY on an EARLIER visit, and this round only
    picks Tavily as extract_backend without retyping the key (no
    extract_env at all), must still get the toolset upgrade — the swap
    checks the FINAL on-disk .env state after step 1, not just "was
    something written this call"."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  telegram: [terminal, search, todo]\n",
        encoding="utf-8",
    )
    # Pre-existing credential from an earlier wizard visit.
    (home / ".env").write_text("TAVILY_API_KEY=tvly-already-saved\n", encoding="utf-8")

    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["extract_backend"] = "tavily"  # no extract_env this round
    out = apply_settings(form)
    assert out["ok"], out

    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert "web" in raw["platform_toolsets"]["telegram"]
    assert "search" not in raw["platform_toolsets"]["telegram"]


def test_extract_backend_none_clears_config_and_swaps_toolset_back(tmp_path, monkeypatch):
    """Finding 1 (review 2026-08-26, owner-approved fix): explicit `null`
    for `extract_backend` — the same clear-signal contract as
    `camofox_url`/`hass` (this module's own docstring) — must actually
    remove `web.extract_backend` from config.yaml AND swap
    `platform_toolsets.telegram` from `web` back to `search`, mirroring
    `test_camofox_url_none_clears_the_secret`. A client who deliberately
    turns "Чтение страниц" off must lose the `web_extract` tool an
    earlier submission granted, not just the config value it resolved
    from."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "web:\n  extract_backend: tavily\nplatform_toolsets:\n  telegram: [terminal, web, todo]\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("TAVILY_API_KEY=tvly-already-saved\n", encoding="utf-8")

    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["extract_backend"] = None
    out = apply_settings(form)
    assert out["ok"], out
    assert "web.extract_backend" in out["removed"]

    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert "extract_backend" not in raw.get("web", {})
    assert "search" in raw["platform_toolsets"]["telegram"]
    assert "web" not in raw["platform_toolsets"]["telegram"]
    # The credential itself is left alone — same "never touches .env"
    # contract as tool_provider's clear signal (this module's own
    # docstring): the client can turn extraction back on later without
    # hunting the key down again.
    from hermes_cli.config import get_env_value

    assert get_env_value("TAVILY_API_KEY") == "tvly-already-saved"


def test_extract_backend_omitted_key_is_still_a_no_op(tmp_path, monkeypatch):
    """Same membership guard as `test_camofox_url_omitted_key_is_still_a_no_op`
    — a bare dict built directly for this function with no
    "extract_backend" key at all (every other test in this file, via
    `FORM`) must never be reinterpreted as a clear signal."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "web:\n  extract_backend: tavily\nplatform_toolsets:\n  telegram: [terminal, web, todo]\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("TAVILY_API_KEY=tvly-already-saved\n", encoding="utf-8")

    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    assert "extract_backend" not in form
    out = apply_settings(form)
    assert out["ok"], out
    assert "web.extract_backend" not in out.get("removed", [])

    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert raw["web"]["extract_backend"] == "tavily"
    assert "web" in raw["platform_toolsets"]["telegram"]


def test_extract_backend_none_when_never_saved_reports_no_removal_or_swap(tmp_path, monkeypatch):
    """Finding 1's counterpart to `test_camofox_url_none_when_never_set_reports_no_removal`
    — clearing an extract backend that was never saved must not claim a
    removal that didn't happen, and must not touch a toolset list that
    never had "web" in it to begin with."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  telegram: [terminal, search, todo]\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("", encoding="utf-8")

    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["extract_backend"] = None
    out = apply_settings(form)
    assert out["ok"], out
    assert "web.extract_backend" not in out["removed"]

    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert "search" in raw["platform_toolsets"]["telegram"]
    assert "web" not in raw["platform_toolsets"]["telegram"]


def test_fallback_provider_lands_in_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["fallback"] = {
        "name": "zai",
        "env_var": "GLM_API_KEY",
        "api_key": "glm-test",
        "base_url": "",
        "model": "glm-5.2",
    }
    apply_settings(form)
    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    chain = raw.get("fallback_providers", [])
    assert {"provider": "zai", "model": "glm-5.2"}.items() <= chain[0].items()


def test_proxy_writes_https_proxy_and_merges_no_proxy(tmp_path, monkeypatch):
    """Variant A (owner-approved): a submitted proxy routes the whole
    runtime through HTTPS_PROXY, and NO_PROXY keeps the RU-reachable
    direct hosts present without dropping anything the user already had.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("NO_PROXY=localhost,my.internal.host\n", encoding="utf-8")

    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["proxy"] = "socks5://user:pass@host:1080"
    out = apply_settings(form)
    assert out["ok"], out
    assert "HTTPS_PROXY" in out["written"]
    assert "NO_PROXY" in out["written"]

    env_text = (home / ".env").read_text()
    assert "HTTPS_PROXY=" in env_text
    assert "socks5://user:pass@host:1080" in env_text

    from hermes_cli.config import load_env

    no_proxy = load_env().get("NO_PROXY", "")
    hosts = set(no_proxy.split(","))
    # User's own pre-existing host survives...
    assert "my.internal.host" in hosts
    # ...and every one of our direct hosts is now present alongside it.
    for host in ("api.z.ai", "api.deepseek.com", "generativelanguage.googleapis.com",
                 "api.search.brave.com", "api.parallel.ai", "api.firecrawl.dev",
                 "api.nexara.ru", "localhost", "127.0.0.1", "::1"):
        assert host in hosts, f"{host} missing from merged NO_PROXY"


def test_empty_proxy_is_a_no_op_for_https_proxy_and_no_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    out = apply_settings(dict(FORM))  # FORM's proxy is ""
    assert out["ok"], out
    assert "HTTPS_PROXY" not in out["written"]
    assert "NO_PROXY" not in out["written"]
    env_path = get_hermes_home() / ".env"
    if env_path.exists():
        text = env_path.read_text()
        assert "HTTPS_PROXY=" not in text
        assert "NO_PROXY=" not in text


def test_resolve_default_model_never_empty_for_known():
    from hermes_cli.setup_wizard.apply import resolve_default_model

    assert resolve_default_model("openrouter")
    assert resolve_default_model("zai")


def _leading_comment_line_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))


def test_apply_preserves_client_template_comments(tmp_path, monkeypatch):
    """Regression detector for the "two writers, one yaml.dump" defect class.

    ``assets/config/trix-config.yaml`` is Trix's curated client template —
    the file ``doctor --fix`` / ``scripts/install.sh`` seed a fresh VM's
    ``config.yaml`` from, carrying ~70 lines of Russian comments explaining
    *why* each non-default setting is set. Before the round-trip-YAML fix,
    ``save_config()`` and ``_update_config_for_provider()`` both wrote via
    plain ``yaml.dump`` (``atomic_yaml_write``), which serializes only data
    and drops every comment — so the very first ``apply_settings()`` call
    against a real client machine would silently wipe the template's
    documentation. This test starts from the real template (not a synthetic
    fixture) and asserts the comments are still there afterwards, alongside
    the actual settings the form changed.

    The toolset swap (search -> web) is now keyed on EXTRACT activation,
    not on `search_backend` (see apply_settings()'s own docstring) — the
    form below picks Firecrawl for BOTH search and extract, submitting its
    key once under `search_env` (extract_env intentionally names the SAME
    key to prove the write-dedup path — see
    test_extract_env_same_key_as_search_env_is_not_duplicated below —
    while still exercising the real swap trigger here).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)

    original_text = _TRIX_TEMPLATE.read_text(encoding="utf-8")
    shutil.copy(_TRIX_TEMPLATE, home / "config.yaml")
    (home / ".env").write_text("", encoding="utf-8")
    original_comment_lines = _leading_comment_line_count(original_text)

    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["search_backend"] = "firecrawl"
    form["search_env"] = {"key": "FIRECRAWL_API_KEY", "value": "fc-test"}
    form["extract_backend"] = "firecrawl"
    out = apply_settings(form)
    assert out["ok"], out

    new_text = (home / "config.yaml").read_text(encoding="utf-8")
    raw = yaml.safe_load(new_text)

    # The settings actually changed must have landed...
    telegram_toolsets = raw["platform_toolsets"]["telegram"]
    assert "web" in telegram_toolsets
    assert "search" not in telegram_toolsets
    assert raw["model"]["provider"] == "openrouter"
    assert raw["web"]["search_backend"] == "firecrawl"
    assert raw["web"]["extract_backend"] == "firecrawl"

    # ...and the template's documentation must have survived the write.
    assert "Чтобы вернуть что-то выключенное" in new_text
    assert "Ресурсы песочницы рассчитаны" in new_text
    assert "Поиск через DuckDuckGo" in new_text
    assert "Секреты (ключи, токены) сюда не пишутся" in new_text

    new_comment_lines = _leading_comment_line_count(new_text)
    assert new_comment_lines >= 0.8 * original_comment_lines, (
        f"only {new_comment_lines} of {original_comment_lines} original "
        f"comment lines survived apply_settings()"
    )


def test_written_is_truthful_on_save_config_failure(tmp_path, monkeypatch):
    """`written` must never list a step-3 field that didn't actually land.

    Forces ``save_config`` to blow up and asserts none of the step-3
    config.yaml edits (buffered in ``pending`` until the save succeeds) leak
    into ``written`` — only a truthful, already-on-disk write belongs there.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.setup_wizard.apply as apply_mod

    def _boom(cfg, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(apply_mod, "save_config", _boom)

    form = dict(FORM)
    form["search_backend"] = "duckduckgo"
    form["fallback"] = {
        "name": "zai",
        "env_var": "GLM_API_KEY",
        "api_key": "glm-test",
        "base_url": "",
        "model": "glm-5.2",
    }
    out = apply_mod.apply_settings(form)

    assert not out["ok"]
    assert out["errors"]
    assert "web.search_backend" not in out["written"]
    assert "fallback_providers" not in out["written"]
    assert "model.default" not in out["written"]
    # Step 1/2 (.env + model.provider) already landed on disk before
    # save_config() ever runs — those ARE truthfully written.
    assert "TELEGRAM_BOT_TOKEN" in out["written"]
    assert "model.provider" in out["written"]


def test_explicit_model_overrides_stale_nonslash_default(tmp_path, monkeypatch):
    """A form-specified model must win even over an existing plain default.

    ``_update_config_for_provider`` (step 2) has its own guard that skips
    overwriting an existing slash-free ``model.default`` — a legitimate
    "don't clobber a hand-picked model" behavior for its OTHER callers
    (``hermes auth``), but wrong here: the wizard form explicitly named a
    model, so that choice must always land regardless of what was on disk
    before.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: old-plain-model\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("", encoding="utf-8")

    from hermes_cli.setup_wizard.apply import apply_settings

    out = apply_settings(dict(FORM))  # FORM's provider.model = "z-ai/glm-5.2"
    assert out["ok"], out
    assert "model.default" in out["written"]

    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert raw["model"]["default"] == "z-ai/glm-5.2"


def test_fallback_providers_suppresses_commented_fallback_hint(tmp_path, monkeypatch):
    """A real ``fallback_providers`` chain must suppress the "how to set up
    a fallback" commented-out instructions ``save_config`` appends — that
    hint previously only checked the legacy singular ``fallback_model`` key,
    so a wizard-written chain still got the redundant hint glued on."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["fallback"] = {
        "name": "zai",
        "env_var": "GLM_API_KEY",
        "api_key": "glm-test",
        "base_url": "",
        "model": "glm-5.2",
    }
    apply_settings(form)
    text = (get_hermes_home() / "config.yaml").read_text()
    assert "── Fallback Model ──" not in text


def test_apply_preserves_untouched_toolset_list_comments(tmp_path, monkeypatch):
    """Regression detector for the ``_merge`` unconditional-reassign bug
    (round 2 fix): a list value ``apply_settings`` does NOT actually change
    (no ``firecrawl_key`` -> ``swap_search_to_web`` never runs) must keep
    every inline per-item comment and the trailing block comment on
    ``platform_toolsets.telegram``, even though ``model.*`` changes in the
    very same ``save_config()`` call. Before the fix, any non-dict value
    present in the saved dict was reassigned unconditionally — clobbering
    the list's ruamel CommentedSeq (and every comment attached to it) as a
    side effect of unrelated keys changing, even when the list itself was
    byte-for-byte identical to what was already on disk.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    shutil.copy(_TRIX_TEMPLATE, home / "config.yaml")
    (home / ".env").write_text("", encoding="utf-8")

    from hermes_cli.setup_wizard.apply import apply_settings

    out = apply_settings(dict(FORM))  # no firecrawl_key -> the list is untouched
    assert out["ok"], out

    text = (home / "config.yaml").read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    assert "search" in raw["platform_toolsets"]["telegram"]  # genuinely unchanged
    assert raw["model"]["provider"] == "openrouter"  # ...while this DID change

    assert "# выполнение команд в песочнице" in text
    assert "# веб-поиск (без web_extract — см. комментарий ниже)" in text
    assert '# Тулсет "web" (search + web_extract) заменён на "search": извлечение' in text


def test_model_default_not_force_written_when_provider_step_fails(tmp_path, monkeypatch):
    """Step 3's forced ``model.default`` write must not fire when step 2
    (``_update_config_for_provider``) failed — otherwise config.yaml could
    end up with the new provider's model name while ``model.provider``
    stays on the OLD provider, a mismatched pair.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  provider: old-provider\n  default: old-model\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("", encoding="utf-8")

    import hermes_cli.setup_wizard.apply as apply_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("provider switch failed")

    monkeypatch.setattr(apply_mod, "_update_config_for_provider", _boom)

    out = apply_mod.apply_settings(dict(FORM))  # FORM has provider.model = "z-ai/glm-5.2"

    assert not out["ok"]
    assert out["errors"]
    assert "model.default" not in out["written"]
    assert "model.provider" not in out["written"]

    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert raw["model"]["provider"] == "old-provider"
    assert raw["model"]["default"] == "old-model"


# ---- tool_env / tool_provider: generic provider-select write path -------
# (owner ruling 2026-08-20 — see apply_settings()'s own docstring)


def test_tool_env_writes_each_pair_through_write_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["tool_env"] = [
        {"key": "ELEVENLABS_API_KEY", "value": "el-test-key"},
        {"key": "KREA_API_KEY", "value": "krea-test-key"},
    ]
    out = apply_settings(form)
    assert out["ok"], out
    assert "ELEVENLABS_API_KEY" in out["written"]
    assert "KREA_API_KEY" in out["written"]

    env_text = (get_hermes_home() / ".env").read_text()
    assert "ELEVENLABS_API_KEY=" in env_text and "el-test-key" in env_text
    assert "KREA_API_KEY=" in env_text and "krea-test-key" in env_text


def test_tool_env_ignores_malformed_and_empty_items(tmp_path, monkeypatch):
    """Return-mode no-op contract: an item missing a key/value, or not a
    dict at all, is silently skipped — never raised."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["tool_env"] = [
        {"key": "", "value": "no-key"},
        {"key": "SOME_KEY", "value": ""},
        {"value": "no-key-field"},
        "not-a-dict",
        {"key": "MISTRAL_API_KEY", "value": "mistral-test-key"},
    ]
    out = apply_settings(form)
    assert out["ok"], out
    assert out["written"].count("MISTRAL_API_KEY") == 1
    assert "SOME_KEY" not in out["written"]

    env_text = (get_hermes_home() / ".env").read_text()
    assert "MISTRAL_API_KEY=" in env_text
    assert "SOME_KEY=" not in env_text


def test_tool_env_missing_or_none_is_a_pure_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    out = apply_settings(dict(FORM))
    assert out["ok"], out
    assert not any(k.endswith("_API_KEY") and k != "OPENROUTER_API_KEY" for k in out["written"])


def test_tool_provider_writes_provider_field_per_category(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["tool_provider"] = {
        "tts": "elevenlabs", "stt": "nexara", "image_gen": "krea", "video_gen": "fal",
    }
    out = apply_settings(form)
    assert out["ok"], out
    for field in ("tts.provider", "stt.provider", "image_gen.provider", "video_gen.provider"):
        assert field in out["written"], out["written"]

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert raw["tts"]["provider"] == "elevenlabs"
    assert raw["stt"]["provider"] == "nexara"
    assert raw["image_gen"]["provider"] == "krea"
    assert raw["video_gen"]["provider"] == "fal"


def test_tool_provider_ignores_unknown_category_and_empty_value(tmp_path, monkeypatch):
    """"x_search" (no config field to disambiguate — see this module's
    _TOOL_PROVIDER_CONFIG_SECTIONS) and an empty value are both no-ops,
    never a KeyError or a stray config.yaml section."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["tool_provider"] = {"x_search": "xai", "tts": ""}
    out = apply_settings(form)
    assert out["ok"], out
    assert not any(w.startswith("x_search.") for w in out["written"])
    assert "tts.provider" not in out["written"]

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "x_search" not in raw
    assert "tts" not in raw or "provider" not in (raw.get("tts") or {})


def test_tool_provider_missing_is_a_pure_no_op(tmp_path, monkeypatch):
    """"model.provider" (step 2, always fires for the primary provider
    choice) is unrelated and must stay untouched by this check — only the
    tool_provider-specific fields (tts/image_gen/video_gen .provider) are
    asserted absent here."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    out = apply_settings(dict(FORM))
    assert out["ok"], out
    assert "model.provider" in out["written"]  # sanity: step 2 still ran
    for field in ("tts.provider", "image_gen.provider", "video_gen.provider"):
        assert field not in out["written"]


def test_tool_env_covers_fal_key(tmp_path, monkeypatch):
    """FAL.ai's catalog row exposes FAL_KEY through the generic tool_env
    mechanism — the only write path for it since the FAL-only `fal_key`
    form field was removed as a dead reader (Opus-review finding 15)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["tool_env"] = [{"key": "FAL_KEY", "value": "fal-via-tool-env"}]
    out = apply_settings(form)
    assert out["ok"], out
    assert "FAL_KEY" in out["written"]

    env_text = (get_hermes_home() / ".env").read_text()
    assert "FAL_KEY=" in env_text and "fal-via-tool-env" in env_text


# ---- Finding 3: category selection must actually grant the toolset -----
# (owner-approved fix) — Trix's platform_toolsets.telegram is an explicit
# list; tools_config._get_platform_tools() skips every auto-enable rule
# once an explicit list exists, so picking a provider for video_gen/
# x_search/Умный дом used to write config that gave the agent zero matching
# tools. These tests start from the real Trix template (same fixture the
# comment-preservation tests above use) so the "explicit list" precondition
# is the real one, not a synthetic stand-in.


def _seed_trix_config(home):
    home.mkdir(parents=True, exist_ok=True)
    shutil.copy(_TRIX_TEMPLATE, home / "config.yaml")
    (home / ".env").write_text("", encoding="utf-8")


def test_video_gen_selection_grants_the_toolset(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_trix_config(get_hermes_home())
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["tool_provider"] = {"video_gen": "fal"}
    out = apply_settings(form)
    assert out["ok"], out

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "video_gen" in raw["platform_toolsets"]["telegram"]
    assert raw["video_gen"]["provider"] == "fal"


def test_video_gen_selection_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_trix_config(get_hermes_home())
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["tool_provider"] = {"video_gen": "fal"}
    apply_settings(form)
    apply_settings(form)

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert raw["platform_toolsets"]["telegram"].count("video_gen") == 1


def test_image_gen_re_selection_after_clear_restores_the_toolset(tmp_path, monkeypatch):
    """Finding 3 (owner-approved fix): the old ADD branch only granted the
    toolset for "video_gen" — the one category NOT already present in
    Trix's baseline `platform_toolsets.telegram` (tts/image_gen ship in
    the template — see `assets/config/trix-config.yaml`). That asymmetry
    was invisible until a category got CLEARED first: image_gen starts
    present, a `tool_provider.image_gen: null` clear removes it (the
    REMOVE branch is generic), and a client who then picks a provider for
    image_gen again got the config field written but never the toolset
    back — "выбрал — не работает" in the exact opposite direction from the
    bug the REMOVE branch was built to fix."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_trix_config(get_hermes_home())
    from hermes_cli.setup_wizard.apply import apply_settings

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "image_gen" in raw["platform_toolsets"]["telegram"]  # Trix's own baseline

    clear_form = dict(FORM)
    clear_form["tool_provider"] = {"image_gen": None}
    out = apply_settings(clear_form)
    assert out["ok"], out
    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "image_gen" not in raw["platform_toolsets"]["telegram"]

    re_add_form = dict(FORM)
    re_add_form["tool_provider"] = {"image_gen": "fal"}
    out = apply_settings(re_add_form)
    assert out["ok"], out

    new_raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "image_gen" in new_raw["platform_toolsets"]["telegram"]
    assert new_raw["image_gen"]["provider"] == "fal"


def test_x_search_key_grants_the_toolset(tmp_path, monkeypatch):
    """x_search has no disambiguating config field (see apply.py's own
    docstring) — its ONLY form signal is a non-empty XAI_API_KEY submitted
    through the generic tool_env mechanism."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_trix_config(get_hermes_home())
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["tool_env"] = [{"key": "XAI_API_KEY", "value": "xai-test-key"}]
    out = apply_settings(form)
    assert out["ok"], out

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "x_search" in raw["platform_toolsets"]["telegram"]


def test_unrelated_tool_env_key_does_not_grant_x_search(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_trix_config(get_hermes_home())
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["tool_env"] = [{"key": "ELEVENLABS_API_KEY", "value": "el-test-key"}]
    out = apply_settings(form)
    assert out["ok"], out

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "x_search" not in raw.get("platform_toolsets", {}).get("telegram", [])


def test_homeassistant_credentials_grant_the_toolset(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_trix_config(get_hermes_home())
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["hass"] = {"url": "http://homeassistant.local:8123", "token": "hass-test-token"}
    out = apply_settings(form)
    assert out["ok"], out

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "homeassistant" in raw["platform_toolsets"]["telegram"]


def test_homeassistant_partial_credentials_do_not_grant_the_toolset(tmp_path, monkeypatch):
    """Only a token (no URL) or only a URL (no token) is not a complete
    activation — the wizard's own add-signal requires both, matching the
    task's explicit contract."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_trix_config(get_hermes_home())
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["hass"] = {"url": "", "token": "hass-test-token"}
    out = apply_settings(form)
    assert out["ok"], out

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "homeassistant" not in raw.get("platform_toolsets", {}).get("telegram", [])


def test_no_platform_toolsets_section_is_left_untouched(tmp_path, monkeypatch):
    """A non-Trix install (no `platform_toolsets` key at all) relies on
    tools_config's own auto-enable rules — apply_settings() must never
    invent the section."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["tool_provider"] = {"video_gen": "fal"}
    form["tool_env"] = [{"key": "XAI_API_KEY", "value": "xai-test-key"}]
    form["hass"] = {"url": "http://homeassistant.local:8123", "token": "hass-test-token"}
    out = apply_settings(form)
    assert out["ok"], out

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "platform_toolsets" not in raw


# ---- Findings 5/7: explicit `None`/`null` clear signal ------------------
# (owner-approved fix). Contract (see apply_settings()'s own docstring and
# app.py's `_SubmitBody`): missing key OR the field's own no-op default
# (""/`{}`) leaves the saved value alone (back-compat); an explicit
# `None` clears it. A bare dict built directly for `apply_settings()`
# (like `FORM` above) that simply omits the key entirely must ALSO stay a
# no-op — these tests cover both the omitted-key and explicit-null shapes.


def test_camofox_url_none_clears_the_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import save_env_value_secure
    from hermes_cli.setup_wizard.apply import apply_settings

    save_env_value_secure("CAMOFOX_URL", "http://localhost:9377")

    form = dict(FORM)
    form["camofox_url"] = None
    out = apply_settings(form)
    assert out["ok"], out
    assert "CAMOFOX_URL" in out["removed"]

    from hermes_cli.config import get_env_value

    assert not get_env_value("CAMOFOX_URL")


def test_camofox_url_omitted_key_is_still_a_no_op(tmp_path, monkeypatch):
    """A caller that never includes "camofox_url" in the dict at all (the
    shape every OTHER apply.py unit test in this file uses via `FORM`)
    must not be silently reinterpreted as a clear signal."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import save_env_value_secure
    from hermes_cli.setup_wizard.apply import apply_settings

    save_env_value_secure("CAMOFOX_URL", "http://localhost:9377")

    form = dict(FORM)
    assert "camofox_url" not in form
    out = apply_settings(form)
    assert out["ok"], out
    assert "CAMOFOX_URL" not in out.get("removed", [])

    from hermes_cli.config import get_env_value

    assert get_env_value("CAMOFOX_URL") == "http://localhost:9377"


def test_camofox_url_none_when_never_set_reports_no_removal(tmp_path, monkeypatch):
    """Finding 9 (owner-approved fix): `_delete_secret` used to append the
    key to `removed` unconditionally, regardless of what
    `remove_env_value()` actually reported — which returns `False`
    (without raising) both when the key was never set and when it lives
    in a managed scope the wizard can't touch. Clearing a Camofox address
    that was never saved must not claim a removal that didn't happen."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["camofox_url"] = None
    out = apply_settings(form)
    assert out["ok"], out
    assert "CAMOFOX_URL" not in out["removed"]


def test_hass_none_when_never_set_reports_no_removal(tmp_path, monkeypatch):
    """Finding 9's counterpart for the hass_clear branch, which calls the
    same `_delete_secret` helper twice — neither HASS_TOKEN nor HASS_URL
    was ever saved, so clearing "Умный дом" must report zero removals,
    not two phantom ones."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["hass"] = None
    out = apply_settings(form)
    assert out["ok"], out
    assert "HASS_TOKEN" not in out["removed"]
    assert "HASS_URL" not in out["removed"]


def test_hass_none_clears_both_secrets_and_the_toolset(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_trix_config(get_hermes_home())
    from hermes_cli.config import save_env_value_secure
    from hermes_cli.setup_wizard.apply import apply_settings

    save_env_value_secure("HASS_TOKEN", "hass-test-token")
    save_env_value_secure("HASS_URL", "http://homeassistant.local:8123")
    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    raw.setdefault("platform_toolsets", {})["telegram"].append("homeassistant")
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    form = dict(FORM)
    form["hass"] = None
    out = apply_settings(form)
    assert out["ok"], out
    assert "HASS_TOKEN" in out["removed"]
    assert "HASS_URL" in out["removed"]

    from hermes_cli.config import get_env_value

    assert not get_env_value("HASS_TOKEN")
    assert not get_env_value("HASS_URL")

    new_raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "homeassistant" not in new_raw["platform_toolsets"]["telegram"]


def test_hass_empty_dict_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import save_env_value_secure
    from hermes_cli.setup_wizard.apply import apply_settings

    save_env_value_secure("HASS_TOKEN", "hass-test-token")

    form = dict(FORM)
    form["hass"] = {}
    out = apply_settings(form)
    assert out["ok"], out
    assert "HASS_TOKEN" not in out.get("removed", [])

    from hermes_cli.config import get_env_value

    assert get_env_value("HASS_TOKEN") == "hass-test-token"


def test_hass_omitted_key_is_still_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import save_env_value_secure
    from hermes_cli.setup_wizard.apply import apply_settings

    save_env_value_secure("HASS_TOKEN", "hass-test-token")

    form = dict(FORM)
    assert "hass" not in form
    out = apply_settings(form)
    assert out["ok"], out
    assert "HASS_TOKEN" not in out.get("removed", [])

    from hermes_cli.config import get_env_value

    assert get_env_value("HASS_TOKEN") == "hass-test-token"


def test_tool_provider_null_clears_config_field_and_toolset(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_trix_config(get_hermes_home())
    from hermes_cli.setup_wizard.apply import apply_settings

    # First establish tts.provider + grant the "tts" toolset is already
    # baseline in Trix's template, so just seed the config field directly.
    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    raw.setdefault("tts", {})["provider"] = "elevenlabs"
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    form = dict(FORM)
    form["tool_provider"] = {"tts": None}
    out = apply_settings(form)
    assert out["ok"], out
    assert "tts.provider" in out["removed"]

    new_raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "provider" not in new_raw.get("tts", {})
    assert "tts" not in new_raw["platform_toolsets"]["telegram"]


def test_tool_provider_null_for_video_gen_clears_the_grant_added_earlier(tmp_path, monkeypatch):
    """Symmetric round-trip: grant video_gen (task 2's ADD path), then
    clear it in a second apply_settings() call — the toolset must come
    back out again, proving add/remove are true inverses."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_trix_config(get_hermes_home())
    from hermes_cli.setup_wizard.apply import apply_settings

    grant_form = dict(FORM)
    grant_form["tool_provider"] = {"video_gen": "fal"}
    apply_settings(grant_form)
    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "video_gen" in raw["platform_toolsets"]["telegram"]

    clear_form = dict(FORM)
    clear_form["tool_provider"] = {"video_gen": None}
    out = apply_settings(clear_form)
    assert out["ok"], out

    new_raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "video_gen" not in new_raw["platform_toolsets"]["telegram"]
    assert "provider" not in new_raw.get("video_gen", {})


def test_tool_provider_null_for_x_search_clears_only_the_toolset(tmp_path, monkeypatch):
    """x_search has no `.provider` config field (see
    `_TOOL_PROVIDER_CONFIG_SECTIONS`'s own docstring) — its null signal is
    purely a toolset-clear."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_trix_config(get_hermes_home())
    from hermes_cli.setup_wizard.apply import apply_settings

    grant_form = dict(FORM)
    grant_form["tool_env"] = [{"key": "XAI_API_KEY", "value": "xai-test-key"}]
    apply_settings(grant_form)
    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "x_search" in raw["platform_toolsets"]["telegram"]

    clear_form = dict(FORM)
    clear_form["tool_provider"] = {"x_search": None}
    out = apply_settings(clear_form)
    assert out["ok"], out
    assert "x_search" not in out["removed"]  # nothing config-side to remove

    new_raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "x_search" not in new_raw["platform_toolsets"]["telegram"]


def test_tool_provider_null_for_stt_clears_config_only_no_toolset(tmp_path, monkeypatch):
    """"stt" has no toolsets.py entry gating it at all (speech-to-text
    rides on config alone) — its null signal must clear `stt.provider`
    without touching `platform_toolsets` (there's nothing there to touch)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_trix_config(get_hermes_home())
    from hermes_cli.setup_wizard.apply import apply_settings

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    raw.setdefault("stt", {})["provider"] = "nexara"
    before_toolsets = list(raw["platform_toolsets"]["telegram"])
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    form = dict(FORM)
    form["tool_provider"] = {"stt": None}
    out = apply_settings(form)
    assert out["ok"], out
    assert "stt.provider" in out["removed"]

    new_raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert "provider" not in new_raw.get("stt", {})
    assert new_raw["platform_toolsets"]["telegram"] == before_toolsets


def test_tool_provider_missing_category_key_stays_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["tool_provider"] = {"tts": None, "image_gen": ""}
    out = apply_settings(form)
    assert out["ok"], out
    # "video_gen"/"stt" were never mentioned at all — untouched.
    assert not any(w.startswith("video_gen.") or w.startswith("stt.") for w in out["removed"])
    assert "image_gen.provider" not in out.get("removed", [])


def test_tool_env_clear_key_is_ignored_now_that_the_mechanism_is_retired(tmp_path, monkeypatch):
    """Finding 1/4 (owner-approved fix, reversed from an earlier design):
    apply_settings() no longer reads `form["tool_env_clear"]` at all — the
    retired mechanism used to trust it verbatim and delete every key it
    listed. A stray leftover key (an old client contract, or a
    reintroduced computation in app.py) must now be silently ignored
    rather than deleting anything real."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import get_env_value, save_env_value_secure
    from hermes_cli.setup_wizard.apply import apply_settings

    save_env_value_secure("XAI_API_KEY", "xai-test-key")
    save_env_value_secure("ELEVENLABS_API_KEY", "el-test-key")

    form = dict(FORM)
    form["tool_env_clear"] = ["XAI_API_KEY", "ELEVENLABS_API_KEY"]
    out = apply_settings(form)
    assert out["ok"], out
    assert out.get("removed", []) == []
    assert get_env_value("XAI_API_KEY") == "xai-test-key"
    assert get_env_value("ELEVENLABS_API_KEY") == "el-test-key"


# ---- tts_voice (Finding 2, owner-approved fix, reversed from an earlier
# design): picking the default "Голос Светлана" after a custom Edge voice
# name was saved now sends the literal default voice name explicitly, not
# a `None` clear signal — deleting `tts.edge.voice` used to hand the agent
# DEFAULT_CONFIG's English baseline voice instead of Светлана (Trix's own
# template ships the Russian voice explicitly, but load_config() falls
# back to upstream Hermes's default the instant the key is absent).


def test_tts_voice_explicit_default_name_overwrites_a_saved_custom_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    raw = {"tts": {"edge": {"voice": "ru-RU-DmitryNeural"}}}
    (home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    form = dict(FORM)
    form["tts_voice"] = "ru-RU-SvetlanaNeural"
    out = apply_settings(form)
    assert out["ok"], out
    assert "tts.edge.voice" in out["written"]
    assert "tts.edge.voice" not in out.get("removed", [])

    new_raw = yaml.safe_load((home / "config.yaml").read_text())
    assert new_raw["tts"]["edge"]["voice"] == "ru-RU-SvetlanaNeural"


def test_tts_voice_none_is_no_longer_a_clear_signal(tmp_path, monkeypatch):
    """A bare dict built directly for apply_settings() (the same shape
    every OTHER unit test in this file uses) carrying `tts_voice: None`
    must fall through to the plain "leave it alone" no-op — apply_settings()
    no longer special-cases `None` here at all (contrast camofox_url/hass,
    which still do)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    raw = {"tts": {"edge": {"voice": "ru-RU-DmitryNeural"}}}
    (home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    form = dict(FORM)
    form["tts_voice"] = None
    out = apply_settings(form)
    assert out["ok"], out
    assert "tts.edge.voice" not in out.get("removed", [])

    new_raw = yaml.safe_load((home / "config.yaml").read_text())
    assert new_raw["tts"]["edge"]["voice"] == "ru-RU-DmitryNeural"


def test_tts_voice_empty_string_is_a_no_op(tmp_path, monkeypatch):
    """Return-mode: the client left "Голос Светлана" selected without ever
    having saved a custom voice — "" (not None) — must leave a previously
    saved value alone, same no-op contract as every other optional field."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    raw = {"tts": {"edge": {"voice": "ru-RU-DmitryNeural"}}}
    (home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    form = dict(FORM)
    form["tts_voice"] = ""
    out = apply_settings(form)
    assert out["ok"], out
    assert "tts.edge.voice" not in out.get("removed", [])

    new_raw = yaml.safe_load((home / "config.yaml").read_text())
    assert new_raw["tts"]["edge"]["voice"] == "ru-RU-DmitryNeural"


def test_tts_voice_omitted_key_is_still_a_no_op(tmp_path, monkeypatch):
    """A caller that never includes "tts_voice" in the dict at all (the
    shape every OTHER apply.py unit test in this file uses via `FORM`)
    must not be silently reinterpreted as a clear signal."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    raw = {"tts": {"edge": {"voice": "ru-RU-DmitryNeural"}}}
    (home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    form = dict(FORM)
    assert "tts_voice" not in form
    out = apply_settings(form)
    assert out["ok"], out
    assert "tts.edge.voice" not in out.get("removed", [])

    new_raw = yaml.safe_load((home / "config.yaml").read_text())
    assert new_raw["tts"]["edge"]["voice"] == "ru-RU-DmitryNeural"


# --- Часовой пояс (спека 11) -------------------------------------------


def test_timezone_written_by_the_wizard_is_the_one_hermes_then_reads(tmp_path, monkeypatch):
    """Провод целиком, а не функция: мастер пишет — Hermes читает.

    Проверяется точка стыка, а не `apply_settings` отдельно и
    `hermes_time` отдельно. Возврат любой из сторон на прежнее поведение
    (мастер не пишет ключ / `hermes_time` его не видит) красит этот тест,
    и только он.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_time
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["timezone"] = "Europe/Moscow"
    out = apply_settings(form)
    assert out["ok"], out

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert raw["timezone"] == "Europe/Moscow"

    hermes_time.reset_cache()
    try:
        assert str(hermes_time.get_timezone()) == "Europe/Moscow"
        assert hermes_time.now().utcoffset().total_seconds() == 3 * 3600
    finally:
        hermes_time.reset_cache()


def test_timezone_survives_the_wizards_own_default_stripping(tmp_path, monkeypatch):
    """`save_config(strip_defaults=True)` вырезает значения, равные
    умолчанию. Умолчание `timezone` — пустая строка, наше значение ей не
    равно; тест держит это свойство на случай, если умолчание поменяют."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    form = dict(FORM)
    form["timezone"] = "Asia/Yekaterinburg"
    assert apply_settings(form)["ok"]

    text = (get_hermes_home() / "config.yaml").read_text()
    assert "Asia/Yekaterinburg" in text


def test_missing_timezone_leaves_an_already_saved_one_alone(tmp_path, monkeypatch):
    """Возвратный клиент, правящий только прокси, не должен терять пояс.

    Контракт `_SubmitBody`: пропущенное поле — «не трогай», а не «сотри».
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.apply import apply_settings

    first = dict(FORM)
    first["timezone"] = "Asia/Omsk"
    assert apply_settings(first)["ok"]

    second = dict(FORM)
    second.pop("timezone", None)
    assert apply_settings(second)["ok"]

    raw = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())
    assert raw["timezone"] == "Asia/Omsk"
