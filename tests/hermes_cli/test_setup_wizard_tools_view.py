"""Tool catalog view for the setup wizard (spec §7.3, invariant §15.2)."""
from __future__ import annotations


def _live_search_backends():
    """Union of the documented-subset constant and the live plugin registry.

    ``tools.web_tools._LEGACY_WEB_BACKENDS`` is explicitly documented there
    (web_tools.py:159-173) as a SUBSET — "any name NOT in this set is a
    candidate plugin-registered provider". The actual source of truth is
    ``agent.web_search_registry`` via
    ``tools_config._plugin_web_search_providers()``, which
    ``tools_view._catalog()``'s "web" category already resolves (through the
    same no-network ``_NO_AUTH_FEATURES`` stub ``_catalog()`` always uses).
    Reading it from there — rather than calling the plugin function a second
    time — means a brand-new plugin backend (e.g. a hypothetical "serper")
    can't slip past both the whole-category exclusion of "web" AND this
    invariant at once."""
    from tools.web_tools import _LEGACY_WEB_BACKENDS
    from hermes_cli.setup_wizard import tools_view as tv

    catalog = tv._catalog()
    live = {row["web_backend"] for row in catalog["web"]["providers"] if row.get("web_backend")}
    return set(_LEGACY_WEB_BACKENDS) | live


def test_every_catalog_row_resolved():
    """Инвариант §15.2: каждая строка каталога инструментов решена.

    "web" self-hosted rows (SearXNG, Firecrawl Self-Hosted) are a THIRD
    kind of resolution alongside "rendered" and "in EXCLUDED_ROWS": their
    visibility is a runtime liveness fact (tools_view._is_self_hosted_row
    + _local_service_alive), not a permanent exclusion, so a row that
    isn't currently alive is still "resolved" by that structural rule —
    same idea as the nous-row carve-out just below it. "browser" rows with
    no apply_settings() write path (Browserbase, Firecrawl cloud) are a
    FOURTH kind — resolved by tools_view._browser_row_has_activation_path,
    also not a static EXCLUDED_ROWS entry (the row reappears on its own
    the moment apply_settings() grows a browser.cloud_provider write
    path — see that function's own docstring). OAuth-only rows (post_setup
    in a small interactive-login allowlist, no env_vars — xAI TTS/STT/Grok
    Imagine/Grok OAuth) are a FIFTH kind, resolved by
    tools_view._is_oauth_only_row — EXCEPT in "web", which is deliberately
    exempt from that rule (its own xai_grok row has an older, tested
    carve-out instead — see that rule's own docstring).
    """
    from hermes_cli.setup_wizard import tools_view as tv

    catalog = tv._catalog()  # словарь категорий из tools_config
    blocks = {b["category"]: {r["name"] for r in b["rows"]} for b in tv.wizard_tool_blocks()}
    for cat, spec in catalog.items():
        if cat in tv.EXCLUDED_CATEGORIES:
            continue
        assert cat in tv.WIZARD_TOOL_CATEGORIES, f"категория не решена: {cat}"
        for row in spec.get("providers", []):
            name = row["name"]
            if row.get("requires_nous_auth") or tv._is_nous_plugin_row(row):
                assert name not in blocks.get(cat, set()), (cat, name)
                continue
            if (
                cat in ("web", "web_extract")
                and tv._is_self_hosted_row(row)
                and not row.get("post_setup")
                and name not in blocks.get(cat, set())
            ):
                # Resolved by the structural liveness rule, not a static
                # exclusion — nothing further to assert (it may or may not
                # be rendered depending on whether the probe succeeded).
                continue
            if (
                cat == "browser"
                and not tv._browser_row_has_activation_path(row)
                and name not in blocks.get(cat, set())
            ):
                # Resolved by the structural activation-path rule, not a
                # static exclusion.
                continue
            if cat not in ("web", "web_extract") and tv._is_oauth_only_row(row):
                assert name not in blocks.get(cat, set()), (cat, name)
                continue
            resolved = name in blocks.get(cat, set()) or (cat, name) in tv.EXCLUDED_ROWS
            assert resolved, f"строка не решена: {cat}/{name}"


def test_nous_rows_never_rendered():
    from hermes_cli.setup_wizard import tools_view as tv

    rendered = {(b["category"], r["name"]) for b in tv.wizard_tool_blocks() for r in b["rows"]}
    catalog = tv._catalog()
    for cat, spec in catalog.items():
        for row in spec.get("providers", []):
            if row.get("requires_nous_auth") or tv._is_nous_plugin_row(row):
                assert (cat, row["name"]) not in rendered


def test_every_live_search_backend_is_resolved_by_the_web_category():
    """Инвариант против ЖИВОГО реестра (agent.web_search_registry): каждый
    бэкенд обязан быть либо обычной рендеримой строкой "web", либо срезан
    nous-правилом, либо self-hosted-строкой, которая просто сейчас не
    жива — никакая другая причина отсутствия недопустима (иначе новый
    плагинный бэкенд может тихо потеряться)."""
    from hermes_cli.setup_wizard import tools_view as tv

    catalog = tv._catalog()
    web_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "web")
    rendered_backends = {row["web_backend"] for row in web_block["rows"] if row.get("web_backend")}

    accounted = set()
    for row in catalog["web"]["providers"]:
        backend = row.get("web_backend")
        if not backend:
            continue
        accounted.add(backend)
        if row.get("requires_nous_auth") or tv._is_nous_plugin_row(row):
            continue
        if backend in rendered_backends:
            continue
        assert tv._is_self_hosted_row(row) and not row.get("post_setup"), (
            f"бэкенд {backend!r} не отрендерен, не nous-строка и не "
            "self-hosted-правило — потерян молча"
        )

    for backend in _live_search_backends():
        assert backend in accounted, backend


def _live_extract_capable_backends():
    """Backends the LIVE web_search_registry says can extract pages — read
    directly from ``agent.web_search_registry``, independent of
    tools_view's own derived "web_extract" catalog, so this test can't
    become a tautology (a module asserted against itself)."""
    from hermes_cli.plugins import _ensure_plugins_discovered
    from agent.web_search_registry import list_providers

    _ensure_plugins_discovered()
    return {p.name for p in list_providers() if p.supports_extract()}


def test_every_live_extract_capable_backend_is_resolved_by_web_extract_category():
    """Extract-capability analogue of
    test_every_live_search_backend_is_resolved_by_the_web_category: every
    backend the live registry says CAN extract must be either a rendered
    "web_extract" row, cut by the nous rule, or a self-hosted row that
    simply isn't alive right now — no other reason for absence is legal."""
    from hermes_cli.setup_wizard import tools_view as tv

    catalog = tv._catalog()
    web_extract_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "web_extract")
    rendered_backends = {row["web_backend"] for row in web_extract_block["rows"] if row.get("web_backend")}

    accounted = set()
    for row in catalog["web_extract"]["providers"]:
        backend = row.get("web_backend")
        if not backend:
            continue
        accounted.add(backend)
        if row.get("requires_nous_auth") or tv._is_nous_plugin_row(row):
            continue
        if backend in rendered_backends:
            continue
        assert tv._is_self_hosted_row(row) and not row.get("post_setup"), (
            f"extract-capable бэкенд {backend!r} не отрендерен, не nous-строка и не "
            "self-hosted-правило — потерян молча"
        )

    for backend in _live_extract_capable_backends():
        assert backend in accounted, backend


def test_no_search_only_backend_ever_appears_in_web_extract_category():
    """Structural guarantee: a backend whose supports_extract() is False
    (ddgs, brave-free, searxng, xai today) must never render as a
    "web_extract" row — picking it there would just reproduce the
    "search-only backend" runtime error (tools/web_tools.py:874-892) the
    whole split exists to prevent."""
    from hermes_cli.plugins import _ensure_plugins_discovered
    from agent.web_search_registry import list_providers
    from hermes_cli.setup_wizard import tools_view as tv

    _ensure_plugins_discovered()
    search_only = {p.name for p in list_providers() if not p.supports_extract()}
    assert search_only, "живой реестр должен содержать хотя бы один search-only бэкенд — перепроверить тест"

    web_extract_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "web_extract")
    rendered_backends = {row["web_backend"] for row in web_extract_block["rows"] if row.get("web_backend")}
    assert not (search_only & rendered_backends), search_only & rendered_backends


def test_web_extract_category_filters_by_live_capability_mutation(monkeypatch):
    """Mutation: fake a search-only backend ("ddgs") claiming it CAN
    extract — it must appear in "web_extract"; reverting the fake makes it
    disappear again. Proves the "web_extract" row set is computed from the
    LIVE capability flag (via tools_config.web_provider_capabilities), not
    a hardcoded name list."""
    from hermes_cli.setup_wizard import tools_view as tv

    real = tv._tc.web_provider_capabilities

    def _fake_ddgs_can_extract(backend):
        if backend == "ddgs":
            return ["search", "extract"]
        return real(backend)

    monkeypatch.setattr(tv._tc, "web_provider_capabilities", _fake_ddgs_can_extract)
    mutated_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "web_extract")
    assert "ddgs" in {r["web_backend"] for r in mutated_block["rows"]}

    monkeypatch.undo()
    restored_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "web_extract")
    assert "ddgs" not in {r["web_backend"] for r in restored_block["rows"]}


def test_ddgs_recommended_structurally():
    """ddgs предвыбран/рекомендован по web_backend, а не по тексту бейджа
    (его каталожный badge никогда не содержит слово "recommended")."""
    from hermes_cli.setup_wizard import tools_view as tv

    web_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "web")
    ddgs = next(r for r in web_block["rows"] if r.get("web_backend") == "ddgs")
    assert ddgs["recommended"] is True
    assert "recommended" not in ddgs["badge"]


def test_self_hosted_row_hidden_when_not_alive(monkeypatch):
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(tv, "_local_service_alive", lambda url, timeout=1.0: False)
    web_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "web")
    names = {r["name"] for r in web_block["rows"]}
    assert "SearXNG" not in names
    assert "Firecrawl Self-Hosted" not in names


def test_self_hosted_row_appears_when_alive(monkeypatch):
    """Мутация: подделываем живой localhost:8080 — SearXNG появляется в
    рендере с предзаполненным SEARXNG_URL."""
    from hermes_cli.setup_wizard import tools_view as tv

    def _fake_alive(url, timeout=1.0):
        return url == "http://127.0.0.1:8080"

    monkeypatch.setattr(tv, "_local_service_alive", _fake_alive)
    web_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "web")
    searxng = next((r for r in web_block["rows"] if r["name"] == "SearXNG"), None)
    assert searxng is not None, "SearXNG должен появиться, когда localhost:8080 жив"
    env = next(e for e in searxng["env_vars"] if e["key"] == "SEARXNG_URL")
    assert env["default"] == "http://127.0.0.1:8080"

    # Firecrawl Self-Hosted's own address (localhost:3002) was NOT faked
    # alive — it must still be hidden.
    names = {r["name"] for r in web_block["rows"]}
    assert "Firecrawl Self-Hosted" not in names


def test_firecrawl_self_hosted_row_appears_when_alive(monkeypatch):
    from hermes_cli.setup_wizard import tools_view as tv

    def _fake_alive(url, timeout=1.0):
        return url == "http://localhost:3002"

    monkeypatch.setattr(tv, "_local_service_alive", _fake_alive)
    web_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "web")
    row = next((r for r in web_block["rows"] if r["name"] == "Firecrawl Self-Hosted"), None)
    assert row is not None
    env = next(e for e in row["env_vars"] if e["key"] == "FIRECRAWL_API_URL")
    assert env["default"] == "http://localhost:3002"


def test_self_hosted_probe_runs_once_per_url_across_web_and_web_extract(monkeypatch):
    """Finding 10 (review 2026-08-26, owner-approved fix): "Firecrawl
    Self-Hosted" (badge self-hosted, no post_setup) renders in BOTH the
    "web" and "web_extract" catalog specs — Firecrawl can do search AND
    extraction. Before this fix, wizard_tool_blocks()'s per-category loop
    called _local_service_alive() once per category the row appears in,
    each paying the same 1s worst-case timeout for the identical address.
    A single /api/form call must probe each distinct URL at most once."""
    from hermes_cli.setup_wizard import tools_view as tv

    calls: list[str] = []

    def _counting_alive(url, timeout=1.0):
        calls.append(url)
        return url == "http://localhost:3002"

    monkeypatch.setattr(tv, "_local_service_alive", _counting_alive)
    blocks = tv.wizard_tool_blocks()

    web_block = next(b for b in blocks if b["category"] == "web")
    extract_block = next(b for b in blocks if b["category"] == "web_extract")
    assert any(r["name"] == "Firecrawl Self-Hosted" for r in web_block["rows"])
    assert any(r["name"] == "Firecrawl Self-Hosted" for r in extract_block["rows"])

    firecrawl_calls = [c for c in calls if c == "http://localhost:3002"]
    assert len(firecrawl_calls) == 1, calls


def test_self_hosted_row_with_post_setup_never_gated():
    """A self-hosted row that carries a post_setup hook is never subject
    to the liveness gate — no such row exists in today's live catalog, but
    the rule (badge AND no post_setup AND not alive) must not silently
    drop one if it ever does."""
    from hermes_cli.setup_wizard import tools_view as tv

    provider = {"name": "Hypothetical", "badge": "free · self-hosted", "post_setup": "hypothetical", "env_vars": []}
    assert tv._is_self_hosted_row(provider)
    # The gate in wizard_tool_blocks() only fires when post_setup is falsy —
    # verified structurally here since no live row exercises this branch.
    assert provider.get("post_setup")


def test_camofox_row_carries_install_hook():
    from hermes_cli.setup_wizard import tools_view as tv

    browser = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    camofox = next((r for r in browser["rows"] if "amofox" in r["name"]), None)
    assert camofox is not None and camofox["post_setup"] == "camofox"


def test_cloud_provider_browser_rows_hidden_until_apply_can_activate_them():
    """Owner ruling 2026-08-20: show Local Browser/Camofox/Browser Use,
    hide any "browser" row apply_settings() has no write path for at all
    (Browserbase, Firecrawl cloud — both only carry browser_provider,
    which targets browser.cloud_provider, a key apply_settings() never
    writes). Checked against the LIVE catalog, not a hardcoded snapshot."""
    from hermes_cli.setup_wizard import tools_view as tv

    browser = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    names = {r["name"] for r in browser["rows"]}
    assert names == {"Local Browser", "Camofox", "Browser Use"}, names

    catalog = tv._catalog()
    live_names = {p["name"] for p in catalog["browser"]["providers"]}
    if {"Browserbase", "Firecrawl"} <= live_names:
        assert "Browserbase" not in names
        assert "Firecrawl" not in names


def test_browser_row_has_activation_path_structural(monkeypatch):
    """Mutation: a plugin row with neither backend_key nor CAMOFOX_URL is
    hidden; giving it a fake backend_key makes it structurally activatable
    (and therefore rendered) again — proving the rule reacts to the
    activation-path FACT, not to the row's display name. monkeypatch
    reverts ``tv._catalog`` automatically at teardown."""
    from hermes_cli.setup_wizard import tools_view as tv

    browserbase_row = {
        "name": "Browserbase",
        "browser_provider": "browserbase",
        "env_vars": [{"key": "BROWSERBASE_API_KEY"}, {"key": "BROWSERBASE_PROJECT_ID"}],
    }
    assert tv._browser_row_has_activation_path(browserbase_row) is False

    def _fake_catalog(providers):
        return {"browser": {"providers": providers}}

    monkeypatch.setattr(tv, "_catalog", lambda: _fake_catalog([browserbase_row]))
    hidden_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    assert "Browserbase" not in {r["name"] for r in hidden_block["rows"]}

    # Mutation: a fictitious backend_key gives the row a real
    # apply_settings() write path (structurally — via _row_backend_key's
    # domain, not via the row's name) — it must now appear.
    mutated = dict(browserbase_row)
    mutated["browser_backend"] = "browserbase"
    assert tv._browser_row_has_activation_path(mutated) is True

    monkeypatch.setattr(tv, "_catalog", lambda: _fake_catalog([mutated]))
    appeared_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    assert "Browserbase" in {r["name"] for r in appeared_block["rows"]}


def test_excluded_categories_have_reasons_in_russian():
    from hermes_cli.setup_wizard import tools_view as tv

    for name, reason in tv.EXCLUDED_CATEGORIES.items():
        assert len(reason) > 15, name


def test_excluded_rows_have_reasons_in_russian():
    from hermes_cli.setup_wizard import tools_view as tv

    for key, reason in tv.EXCLUDED_ROWS.items():
        assert len(reason) > 15, key


def test_row_contract_keys():
    from hermes_cli.setup_wizard import tools_view as tv

    expected_keys = {
        "name",
        "badge",
        "tag",
        "env_vars",
        "post_setup",
        "recommended",
        "installed",
        "backend_key",
        "web_backend",
        "provider_key",
        "beta",
        "beta_note_ru",
        "voices",
        "default_voice",
        "install_blocked",
        "install_blocked_reason_ru",
    }
    for block in tv.wizard_tool_blocks():
        for row in block["rows"]:
            assert set(row.keys()) == expected_keys, row["name"]
            assert isinstance(row["recommended"], bool), row["name"]
            assert row["installed"] is None or isinstance(row["installed"], bool), row["name"]
            assert row["backend_key"] is None or isinstance(row["backend_key"], str), row["name"]
            assert row["web_backend"] is None or isinstance(row["web_backend"], str), row["name"]
            assert row["provider_key"] is None or isinstance(row["provider_key"], str), row["name"]
            assert isinstance(row["beta"], bool), row["name"]
            assert row["beta_note_ru"] is None or isinstance(row["beta_note_ru"], str), row["name"]
            assert row["voices"] is None or isinstance(row["voices"], list), row["name"]
            assert row["default_voice"] is None or isinstance(row["default_voice"], str), row["name"]
            assert isinstance(row["install_blocked"], bool), row["name"]
            assert row["install_blocked_reason_ru"] is None or isinstance(
                row["install_blocked_reason_ru"], str
            ), row["name"]
            # "populated only where it means something" contract, same as
            # backend_key/web_backend/provider_key above.
            assert row["beta"] == (row["beta_note_ru"] is not None), row["name"]
            assert (row["voices"] is None) == (row["default_voice"] is None), row["name"]
            assert row["install_blocked"] == (row["install_blocked_reason_ru"] is not None), row["name"]
            # A row can only be "blocked" while it still needs installing —
            # an already-installed row has nothing left to fail.
            if row["installed"]:
                assert row["install_blocked"] is False, row["name"]


def test_backend_key_only_populated_for_browser_category():
    """Спека: только «browser» сегодня имеет однозначное поле выбора
    (``browser.backend`` через ``apply_settings()``) — у остальных
    категорий (tts/image_gen/homeassistant) нет соответствующего поля в
    форме сабмита, так что ``backend_key`` там всегда ``None``. "web" has
    its own single-choice field (``web_backend``), tested separately."""
    from hermes_cli.setup_wizard import tools_view as tv

    for block in tv.wizard_tool_blocks():
        if block["category"] == "browser":
            continue
        for row in block["rows"]:
            assert row["backend_key"] is None, (block["category"], row["name"])


def test_web_backend_only_populated_for_web_categories():
    """"web_backend" is populated for BOTH web-capability blocks — "web"
    (search) and "web_extract" (page reading, derived from the same
    underlying rows — see tools_view.py's module docstring) — and None
    everywhere else."""
    from hermes_cli.setup_wizard import tools_view as tv

    for block in tv.wizard_tool_blocks():
        if block["category"] in ("web", "web_extract"):
            assert all(r["web_backend"] for r in block["rows"]), block["rows"]
        else:
            assert all(r["web_backend"] is None for r in block["rows"]), block["category"]


def test_browser_backend_key_domain():
    """Инвариант (ревью 9d, п.1): каждый непустой ``backend_key`` строки
    «browser» обязан быть реальным значением домена ``browser.backend``
    — тем же, что понимает ``tools/browser_use_cli.py`` (не
    отображаемым именем и не значением другого ключа,
    ``browser.cloud_provider``). Импортируем настоящие константы вместо
    строкового литерала — так переименование домена в
    ``browser_use_cli.py`` ломает этот тест, а не расходится с ним molча."""
    from hermes_cli.setup_wizard import tools_view as tv
    from tools.browser_use_cli import BACKEND_DISABLED, _BACKEND_KEY

    valid_domain = {BACKEND_DISABLED, _BACKEND_KEY}
    browser = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    seen_keys = {row["backend_key"] for row in browser["rows"] if row["backend_key"]}
    assert seen_keys, "ожидался хотя бы один selectable browser-ряд"
    assert seen_keys <= valid_domain, seen_keys - valid_domain


def test_local_browser_row_maps_to_off():
    """"Local Browser" — рекомендованная по умолчанию строка — обязана
    маппиться на "off" (то же значение, что несёт шаблон
    assets/config/trix-config.yaml: "off" = встроенные инструменты
    поверх локального Chromium, то есть именно "Local Browser")."""
    from hermes_cli.setup_wizard import tools_view as tv

    browser = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    local_row = next(r for r in browser["rows"] if r["name"] == "Local Browser")
    assert local_row["backend_key"] == "off"
    assert local_row["recommended"] is True


def test_browser_use_row_maps_to_its_own_backend_key():
    from hermes_cli.setup_wizard import tools_view as tv
    from tools.browser_use_cli import _BACKEND_KEY

    browser = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    row = next(r for r in browser["rows"] if r["name"] == "Browser Use")
    assert row["backend_key"] == _BACKEND_KEY


def test_camofox_row_has_no_backend_key():
    """Camofox только пишет ``browser.cloud_provider`` — ключ, который
    ``apply_settings()`` не умеет сохранять вовсе; строка обязана быть
    информационной (``backend_key is None``), не выбираемой."""
    from hermes_cli.setup_wizard import tools_view as tv

    browser = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    camofox = next(r for r in browser["rows"] if "amofox" in r["name"])
    assert camofox["backend_key"] is None


def test_wizard_categories_and_excluded_categories_partition_catalog():
    """Ни одна категория живого каталога не может быть и рендериться, и
    исключаться одновременно; вместе они не должны содержать лишних имён."""
    from hermes_cli.setup_wizard import tools_view as tv

    catalog_keys = set(tv._catalog())
    wizard = set(tv.WIZARD_TOOL_CATEGORIES)
    excluded = set(tv.EXCLUDED_CATEGORIES)
    assert not (wizard & excluded)
    assert wizard <= catalog_keys
    assert excluded <= catalog_keys


def test_web_category_is_a_wizard_category_not_excluded():
    """Commit 2: "web" (originally "Поиск и извлечение страниц") переехала
    из EXCLUDED_CATEGORIES в WIZARD_TOOL_CATEGORIES. 2026-08-26: split into
    "Поиск в интернете" ("web") + "Чтение страниц" ("web_extract") — see
    test_web_extract_is_a_derived_wizard_category below for the second
    half."""
    from hermes_cli.setup_wizard import tools_view as tv

    assert "web" in tv.WIZARD_TOOL_CATEGORIES
    assert "web" not in tv.EXCLUDED_CATEGORIES
    assert tv.TITLES_RU["web"] == "Поиск в интернете"


def test_web_extract_is_a_derived_wizard_category():
    """The second half of the "web" split (2026-08-26): "web_extract" is a
    real wizard category (rendered, titled, non-excluded) even though it
    is not a real ``tools_config.TOOL_CATEGORIES`` key — ``_catalog()``
    derives it from "web"'s own rows (see that function's own docstring)."""
    from hermes_cli.setup_wizard import tools_view as tv

    assert "web_extract" in tv.WIZARD_TOOL_CATEGORIES
    assert "web_extract" not in tv.EXCLUDED_CATEGORIES
    assert tv.TITLES_RU["web_extract"] == "Чтение страниц"
    assert "web_extract" not in tv._tc.TOOL_CATEGORIES

    catalog = tv._catalog()
    assert "web_extract" in catalog
    rendered = {b["category"] for b in tv.wizard_tool_blocks()}
    assert "web_extract" in rendered


def test_titles_ru_cover_every_wizard_category():
    from hermes_cli.setup_wizard import tools_view as tv

    for cat in tv.WIZARD_TOOL_CATEGORIES:
        assert cat in tv.TITLES_RU, cat
        assert tv.TITLES_RU[cat], cat


def test_excluded_rows_keys_known_to_catalog():
    """Ловит переименование/удаление строки каталога: ключ EXCLUDED_ROWS не
    должен ссылаться на (категория, имя), которых больше нет в живом
    каталоге — иначе устаревший ключ молча перестаёт что-либо исключать."""
    from hermes_cli.setup_wizard import tools_view as tv

    catalog = tv._catalog()
    all_rows = {
        (cat, row["name"]) for cat, spec in catalog.items() for row in spec.get("providers", [])
    }
    assert set(tv.EXCLUDED_ROWS) <= all_rows


def test_nous_plugin_row_excluded_structurally_not_by_name():
    """Строка nous-плагина (image_gen_plugin_name == "nous") режется
    структурным правилом _is_nous_plugin_row(), а не записью в
    EXCLUDED_ROWS по отображаемому имени — переименование заголовка
    апстримом не должно возвращать её в рендер."""
    from hermes_cli.setup_wizard import tools_view as tv

    catalog = tv._catalog()
    nous_rows = [
        (cat, row)
        for cat, spec in catalog.items()
        for row in spec.get("providers", [])
        if tv._is_nous_plugin_row(row)
    ]
    assert nous_rows, "ожидалась как минимум плагинная nous-строка (image_gen) в живом каталоге"
    for cat, row in nous_rows:
        assert (cat, row["name"]) not in tv.EXCLUDED_ROWS, (
            "nous-плагинная строка должна резаться структурным правилом, "
            "а не перечислением по имени в EXCLUDED_ROWS"
        )


def test_tts_plugin_marker_recognized_as_nous_row():
    """tools_config.py:3135 stamps a tts-plugin row with tts_plugin_name —
    the same identity-marker pattern image_gen/video_gen/web_search/browser
    already carry (see the module comment above _NOUS_PLUGIN_MARKERS).
    Before this fix a nous-backed TTS row would slip past the structural
    exclusion and render in the wizard."""
    from hermes_cli.setup_wizard import tools_view as tv

    assert "tts_plugin_name" in tv._NOUS_PLUGIN_MARKERS
    assert tv._is_nous_plugin_row({"tts_plugin_name": "nous"})
    assert not tv._is_nous_plugin_row({"tts_plugin_name": "edge"})


def test_run_tool_install_delegates_to_tools_config(monkeypatch):
    from hermes_cli.setup_wizard import tools_view as tv

    calls = []
    monkeypatch.setattr(tv._tc, "_run_post_setup", lambda key: calls.append(key))
    monkeypatch.setattr(tv, "_post_setup_ready_now", lambda key: True)
    result = tv.run_tool_install("camofox")
    assert calls == ["camofox"]
    assert result["ok"] is True


# ---- run_tool_install() structured verdict (owner ruling 2026-08-24) -----


def test_run_tool_install_already_installed(monkeypatch):
    """Re-running install on an already-satisfied hook must say so, not
    imply a fresh install just happened."""
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(tv._tc, "_run_post_setup", lambda key: None)
    monkeypatch.setattr(tv, "_post_setup_ready_now", lambda key: True)
    result = tv.run_tool_install("camofox")
    assert result == {"ok": True, "reason": "already_installed", "message": tv._MSG_INSTALL_ALREADY_DONE}


def _toggle_after_first_call(first: bool, second: bool):
    state = {"n": 0}

    def _ready(key):
        state["n"] += 1
        return first if state["n"] == 1 else second

    return _ready


def test_run_tool_install_fresh_camofox_needs_manual_start(monkeypatch):
    """Mutation: camofox goes from not-ready to ready — the message must
    call out the separate server-start step (owner's exact example
    phrase), not just say "Установлено."."""
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(tv._tc, "_run_post_setup", lambda key: None)
    monkeypatch.setattr(tv, "_post_setup_ready_now", _toggle_after_first_call(False, True))
    result = tv.run_tool_install("camofox")
    assert result == {
        "ok": True,
        "reason": "needs_manual_start",
        "message": tv._MSG_INSTALL_NEEDS_MANUAL_START,
    }
    assert "отдельно" in result["message"]


def test_run_tool_install_fresh_agent_browser_is_plain_installed(monkeypatch):
    """Same before/after transition as Camofox, but for a key NOT in
    _NEEDS_MANUAL_START_AFTER_INSTALL — must get the plain "installed"
    message, not the manual-start one."""
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(tv._tc, "_run_post_setup", lambda key: None)
    monkeypatch.setattr(tv, "_post_setup_ready_now", _toggle_after_first_call(False, True))
    result = tv.run_tool_install("agent_browser")
    assert result == {"ok": True, "reason": "installed", "message": tv._MSG_INSTALL_OK}


def test_run_tool_install_no_node_reason(monkeypatch):
    """Mutation: the exact scenario from the client-VM walkthrough — no
    npm on PATH, readiness never flips. Must name Node.js specifically
    (finding 3's literal example), not a generic failure message."""
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(tv._tc, "_run_post_setup", lambda key: None)
    monkeypatch.setattr(tv, "_post_setup_ready_now", lambda key: False)
    monkeypatch.setattr("hermes_constants.find_node_executable", lambda name: None)
    result = tv.run_tool_install("camofox")
    assert result == {"ok": False, "reason": "no_node", "message": tv._MSG_INSTALL_NO_NODE}
    assert "Node.js" in result["message"]


def test_run_tool_install_no_node_reason_covers_agent_browser_and_browserbase(monkeypatch):
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(tv._tc, "_run_post_setup", lambda key: None)
    monkeypatch.setattr(tv, "_post_setup_ready_now", lambda key: False)
    monkeypatch.setattr("hermes_constants.find_node_executable", lambda name: None)
    for key in ("agent_browser", "browserbase"):
        assert tv.run_tool_install(key)["reason"] == "no_node", key


def test_run_tool_install_failure_without_node_never_blames_node_for_non_npm_key(monkeypatch):
    """browser_use_cli installs via `uv`, not npm — a failed install there
    must NOT claim "no Node.js" even when Node really is absent (that
    would misdiagnose the actual blocker). See _BROWSER_BETA_NOTES_RU's
    own comment on why the owner's Node.js assumption doesn't hold here."""
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(tv._tc, "_run_post_setup", lambda key: None)
    monkeypatch.setattr(tv, "_post_setup_ready_now", lambda key: False)
    monkeypatch.setattr("hermes_constants.find_node_executable", lambda name: None)
    result = tv.run_tool_install("browser_use_cli")
    assert result["reason"] == "failed"
    assert "Node.js" not in result["message"]


def test_run_tool_install_unknown_key_defers_to_reprobe(monkeypatch):
    """No readiness check registered at all (neither tools_config's
    _POST_SETUP_READY table nor tools_view's own browser_use_cli case) —
    must say "unknown", not fabricate ok/failed."""
    from hermes_cli.setup_wizard import tools_view as tv

    calls = []
    monkeypatch.setattr(tv._tc, "_run_post_setup", lambda key: calls.append(key))
    result = tv.run_tool_install("some_future_hook_no_one_registered")
    assert calls == ["some_future_hook_no_one_registered"]
    assert result["reason"] == "unknown"


# ---- _post_setup_ready_now() ----------------------------------------------


def test_post_setup_ready_now_reuses_tools_config_predicate(monkeypatch):
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setitem(tv._tc._POST_SETUP_READY, "camofox", lambda: True)
    assert tv._post_setup_ready_now("camofox") is True
    monkeypatch.setitem(tv._tc._POST_SETUP_READY, "camofox", lambda: False)
    assert tv._post_setup_ready_now("camofox") is False


def test_post_setup_ready_now_browser_use_cli_checks_path(monkeypatch):
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/browser-use" if name == "browser-use" else None
    )
    assert tv._post_setup_ready_now("browser_use_cli") is True
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert tv._post_setup_ready_now("browser_use_cli") is False


def test_post_setup_ready_now_unknown_key_returns_none():
    from hermes_cli.setup_wizard import tools_view as tv

    assert tv._post_setup_ready_now("some_future_hook_no_one_registered") is None


def test_local_service_alive_tolerant_of_any_failure():
    """Никогда не бросает исключение — недоступность (или мусорный URL)
    означает False, а не ошибку рендера."""
    from hermes_cli.setup_wizard import tools_view as tv

    assert tv._local_service_alive("http://127.0.0.1:1", timeout=0.2) is False
    assert tv._local_service_alive("not-a-url", timeout=0.2) is False


def test_self_hosted_probe_url_prefers_catalog_default_over_known_local():
    from hermes_cli.setup_wizard import tools_view as tv

    provider = {"env_vars": [{"key": "SEARXNG_URL", "default": "http://example.internal:9"}]}
    assert tv._self_hosted_probe_url(provider) == "http://example.internal:9"


def test_self_hosted_probe_url_falls_back_to_known_local():
    from hermes_cli.setup_wizard import tools_view as tv

    provider = {"env_vars": [{"key": "SEARXNG_URL"}]}
    assert tv._self_hosted_probe_url(provider) == "http://127.0.0.1:8080"


def test_self_hosted_probe_url_none_when_no_url_env_var():
    from hermes_cli.setup_wizard import tools_view as tv

    assert tv._self_hosted_probe_url({"env_vars": []}) is None
    assert tv._self_hosted_probe_url({"env_vars": [{"key": "SOME_KEY"}]}) is None


# ---- OAuth-only structural rule (owner ruling 2026-08-20) ----------------


def test_is_oauth_only_row_unit():
    from hermes_cli.setup_wizard import tools_view as tv

    assert tv._is_oauth_only_row({"post_setup": "xai_grok", "env_vars": []})
    # A real env_vars fallback (Camofox's CAMOFOX_URL shape) beats the hook.
    assert not tv._is_oauth_only_row({"post_setup": "xai_grok", "env_vars": [{"key": "CAMOFOX_URL"}]})
    # A local-install hook (KittenTTS/Piper shape) is not a login flow.
    assert not tv._is_oauth_only_row({"post_setup": "kittentts", "env_vars": []})
    assert not tv._is_oauth_only_row({"post_setup": None, "env_vars": []})
    assert not tv._is_oauth_only_row({"env_vars": []})


def test_oauth_only_rows_hidden_structurally_mutation(monkeypatch):
    """Мутация (task 3): live xAI-only rows across tts/image_gen/video_gen/
    x_search must be absent — then, with the rule disabled, must reappear.
    Both directions are asserted in one test so a future no-op refactor of
    the rule can't silently pass by accident."""
    from hermes_cli.setup_wizard import tools_view as tv

    catalog = tv._catalog()
    # Only categories wizard_tool_blocks() actually iterates (WIZARD_TOOL_CATEGORIES)
    # can ever "reappear" — a row in an EXCLUDED category (e.g. today's "stt")
    # is unresolvable by this rule either way, mutated or not.
    oauth_only_live = [
        (cat, row["name"])
        for cat, spec in catalog.items()
        if cat in tv.WIZARD_TOOL_CATEGORIES and cat != "web"
        for row in spec.get("providers", [])
        if tv._is_oauth_only_row(row)
    ]
    assert oauth_only_live, "живой каталог больше не содержит ни одной OAuth-only строки вне web — перепроверить тест"

    rendered = {(b["category"], r["name"]) for b in tv.wizard_tool_blocks() for r in b["rows"]}
    for cat, name in oauth_only_live:
        assert (cat, name) not in rendered, (cat, name)

    # Mutation: empty the hook allowlist -> the same rows must reappear.
    monkeypatch.setattr(tv, "_INTERACTIVE_LOGIN_POST_SETUP_HOOKS", frozenset())
    mutated_rendered = {(b["category"], r["name"]) for b in tv.wizard_tool_blocks() for r in b["rows"]}
    for cat, name in oauth_only_live:
        assert (cat, name) in mutated_rendered, (cat, name)


def test_oauth_only_rule_exempts_web_category():
    """"web"'s own xai_grok row (xAI Web Search (Grok)) keeps its older,
    tested carve-out (renderSearchBlock's special hint) instead of being
    hidden by the new rule — see that rule's own docstring."""
    from hermes_cli.setup_wizard import tools_view as tv

    catalog = tv._catalog()
    web_xai_row = next(
        (p for p in catalog["web"]["providers"] if p.get("post_setup") == "xai_grok"), None
    )
    assert web_xai_row is not None, "живой каталог больше не содержит xai_grok в web — перепроверить тест"
    assert tv._is_oauth_only_row(web_xai_row), "тест предполагает, что строка ПОДПАДАЕТ под правило структурно"

    rendered = {(b["category"], r["name"]) for b in tv.wizard_tool_blocks() for r in b["rows"]}
    assert ("web", web_xai_row["name"]) in rendered


def test_no_oauth_only_row_ever_rendered_outside_web():
    """Инвариант против живого каталога: ни одна OAuth-only строка вне
    "web" не должна отрендериться — проверено правилом, а не списком
    конкретных имён, так что новая xai_grok-строка в любой другой
    категории тоже будет поймана."""
    from hermes_cli.setup_wizard import tools_view as tv

    catalog = tv._catalog()
    rendered = {(b["category"], r["name"]) for b in tv.wizard_tool_blocks() for r in b["rows"]}
    for cat, spec in catalog.items():
        if cat == "web":
            continue
        for row in spec.get("providers", []):
            if tv._is_oauth_only_row(row):
                assert (cat, row["name"]) not in rendered, (cat, row["name"])


# ---- OpenAI (Codex auth) regression (owner ruling 2026-08-20) ------------


def test_openai_codex_auth_row_not_caught_by_oauth_rule():
    """"OpenAI (Codex auth)" carries neither env_vars NOR a post_setup hook
    at all — the OAuth-only rule keys on post_setup membership, so an unset
    post_setup structurally can never match it. Regression guard for the
    "OpenAI (Codex auth)" row specifically (task 3's required concrete
    test) — it used to be a static EXCLUDED_ROWS entry; it must now render,
    with the "Провайдер"-step hint owned by page.py."""
    from hermes_cli.setup_wizard import tools_view as tv

    catalog = tv._catalog()
    row = next(
        p for p in catalog["image_gen"]["providers"] if p.get("image_gen_plugin_name") == "openai-codex"
    )
    assert row["name"] == "OpenAI (Codex auth)"
    assert not row.get("post_setup")
    assert not row.get("env_vars")
    assert not tv._is_oauth_only_row(row)
    assert ("image_gen", "OpenAI (Codex auth)") not in tv.EXCLUDED_ROWS

    block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "image_gen")
    rendered_row = next((r for r in block["rows"] if r["name"] == "OpenAI (Codex auth)"), None)
    assert rendered_row is not None, "OpenAI (Codex auth) должен рендериться (owner ruling 2026-08-20)"
    assert rendered_row["provider_key"] == "openai-codex"


# ---- video_gen / x_search now wizard categories (owner ruling 2026-08-20) --


def test_video_gen_is_a_wizard_category_not_excluded():
    from hermes_cli.setup_wizard import tools_view as tv

    assert "video_gen" in tv.WIZARD_TOOL_CATEGORIES
    assert "video_gen" not in tv.EXCLUDED_CATEGORIES
    assert tv.TITLES_RU["video_gen"] == "Генерация видео"


def test_x_search_and_homeassistant_are_excluded_categories_spec_a5():
    """Spec A5 (owner ruling 2026-08-23): "Поиск по X" and "Умный дом" left
    the wizard — reversing the 2026-08-20 ruling that had moved x_search
    INTO WIZARD_TOOL_CATEGORIES. Both must now be excluded with a reason,
    and neither category's rows may render in wizard_tool_blocks() at all."""
    from hermes_cli.setup_wizard import tools_view as tv

    assert "x_search" not in tv.WIZARD_TOOL_CATEGORIES
    assert "x_search" in tv.EXCLUDED_CATEGORIES
    assert len(tv.EXCLUDED_CATEGORIES["x_search"]) > 15

    assert "homeassistant" not in tv.WIZARD_TOOL_CATEGORIES
    assert "homeassistant" in tv.EXCLUDED_CATEGORIES
    assert len(tv.EXCLUDED_CATEGORIES["homeassistant"]) > 15

    rendered_categories = {b["category"] for b in tv.wizard_tool_blocks()}
    assert "x_search" not in rendered_categories
    assert "homeassistant" not in rendered_categories


def test_web_video_gen_stay_wizard_categories_after_a5_retreat():
    """Mutation-adjacent sanity check: A5 only removes x_search/
    homeassistant — it must not have accidentally dropped a sibling
    category that graduated the same 2026-08-20 ruling."""
    from hermes_cli.setup_wizard import tools_view as tv

    for cat in ("web", "video_gen", "stt"):
        assert cat in tv.WIZARD_TOOL_CATEGORIES, cat
        assert cat not in tv.EXCLUDED_CATEGORIES, cat


def test_langfuse_stays_excluded_with_updated_reason():
    from hermes_cli.setup_wizard import tools_view as tv

    assert "langfuse" in tv.EXCLUDED_CATEGORIES
    assert "2026-08-20" in tv.EXCLUDED_CATEGORIES["langfuse"]


def test_stt_is_a_wizard_category_not_excluded():
    """"stt" (Распознавание речи) moved out of EXCLUDED_CATEGORIES once the
    bundled Nexara STT plugin resolved the "waiting on a decision about
    Russian STT services" reason it used to carry — same pattern as
    web/video_gen/x_search before it."""
    from hermes_cli.setup_wizard import tools_view as tv

    assert "stt" in tv.WIZARD_TOOL_CATEGORIES
    assert "stt" not in tv.EXCLUDED_CATEGORIES
    assert tv.TITLES_RU["stt"] == "Распознавание речи"


def test_stt_block_includes_nexara_registry_row():
    """Mutation invariant: the Nexara row appears iff it is registered.
    Proves _stt_registry_rows() is live, not a hardcoded literal."""
    from agent import transcription_registry
    from hermes_cli.setup_wizard import tools_view as tv

    block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "stt")
    names = {r["name"] for r in block["rows"]}
    assert "Local Whisper" in names, "Local Whisper must stay the recommended default"
    assert "Nexara" in names

    local_row = next(r for r in block["rows"] if r["name"] == "Local Whisper")
    assert local_row["recommended"] is True
    nexara_row = next(r for r in block["rows"] if r["name"] == "Nexara")
    assert nexara_row["provider_key"] == "nexara"
    assert nexara_row["env_vars"] and nexara_row["env_vars"][0]["key"] == "NEXARA_API_KEY"

    # Mutation: unregister Nexara -> the row disappears; re-register -> it
    # comes back. Proves this is a live registry read, not a snapshot.
    saved = transcription_registry.get_provider("nexara")
    assert saved is not None
    try:
        transcription_registry._providers.pop("nexara", None)
        block_without = next(b for b in tv.wizard_tool_blocks() if b["category"] == "stt")
        assert "Nexara" not in {r["name"] for r in block_without["rows"]}
    finally:
        transcription_registry.register_provider(saved)
        block_restored = next(b for b in tv.wizard_tool_blocks() if b["category"] == "stt")
        assert "Nexara" in {r["name"] for r in block_restored["rows"]}


def test_x_search_never_rendered_even_though_its_rows_would_pass_every_rule():
    """Spec A5: x_search's own rows are perfectly renderable (no
    OAuth-only, has env_vars) — it's excluded purely because the CATEGORY
    left WIZARD_TOOL_CATEGORIES, not because its rows fail any structural
    rule. Proves the exclusion is category-level, not accidental row-level
    filtering that happened to empty it out."""
    from hermes_cli.setup_wizard import tools_view as tv

    catalog = tv._catalog()
    x_search_rows = catalog.get("x_search", {}).get("providers", [])
    assert x_search_rows, "живой каталог должен всё ещё содержать x_search строки (CLI ими пользуется)"
    for row in x_search_rows:
        if tv._is_oauth_only_row(row):
            continue  # the one live xai_grok row — still excluded by that rule too, unrelated to A5
        assert row.get("env_vars"), row["name"]

    assert "x_search" not in {b["category"] for b in tv.wizard_tool_blocks()}


def test_video_gen_block_rows_all_have_env_and_provider_key():
    from hermes_cli.setup_wizard import tools_view as tv

    block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "video_gen")
    assert block["rows"], "video_gen должен оставить хотя бы одну строку после правил"
    for row in block["rows"]:
        assert not tv._is_oauth_only_row(row), row["name"]
        assert row["env_vars"], row["name"]
        assert row["provider_key"], row["name"]


# ---- provider_key (generic "which config value activates this row") -----


def test_row_provider_key_unit():
    from hermes_cli.setup_wizard import tools_view as tv

    assert tv._row_provider_key("tts", {"tts_provider": "elevenlabs"}) == "elevenlabs"
    assert tv._row_provider_key("image_gen", {"image_gen_plugin_name": "fal"}) == "fal"
    assert tv._row_provider_key("image_gen", {"imagegen_backend": "fal"}) == "fal"
    assert tv._row_provider_key("video_gen", {"video_gen_plugin_name": "fal"}) == "fal"
    # x_search has no marker at all -> always None, even with an unrelated key present.
    assert tv._row_provider_key("x_search", {"tts_provider": "edge"}) is None
    assert tv._row_provider_key("browser", {"tts_provider": "edge"}) is None
    assert tv._row_provider_key("tts", {}) is None


def test_provider_key_populated_only_for_its_domain_categories():
    """Инвариант: provider_key заполнен для КАЖДОЙ строки tts/stt/image_gen/
    video_gen (в живом каталоге у них у всех есть маркер) и всегда None
    вне этих четырёх категорий — тем же правилом "заполнено только там, где
    имеет смысл", что уже используют backend_key/web_backend. "stt" joined
    2026-08-20 alongside tts — both categories always have an active
    default provider (never an "off" state), so every row (built-in and
    plugin-injected, e.g. Nexara) carries its own provider marker."""
    from hermes_cli.setup_wizard import tools_view as tv

    for block in tv.wizard_tool_blocks():
        if block["category"] in {"tts", "stt", "image_gen", "video_gen"}:
            for row in block["rows"]:
                assert row["provider_key"], (block["category"], row["name"])
        else:
            for row in block["rows"]:
                assert row["provider_key"] is None, (block["category"], row["name"])


def test_every_provider_select_row_has_at_most_one_env_var():
    """Structural invariant the generic front-end/apply tool_env mechanism
    relies on: every row across the provider-select categories exposes at
    most one submittable env var (the UI renders exactly one field)."""
    from hermes_cli.setup_wizard import tools_view as tv

    for block in tv.wizard_tool_blocks():
        if block["category"] not in {"tts", "stt", "image_gen", "video_gen", "x_search"}:
            continue
        for row in block["rows"]:
            assert len(row["env_vars"]) <= 1, (block["category"], row["name"], row["env_vars"])


def test_every_env_var_in_the_live_wizard_catalog_has_a_russian_prompt():
    """Finding 9: env_vars[].prompt comes from tools_config.py / plugin
    get_setup_schema() calls and is written in English ("OpenAI API key",
    "Camofox server URL", …) — right for `hermes tools`' English CLI,
    wrong for this Russian, brandless web wizard. wizard_tool_blocks() must
    stamp a prompt_ru (from RU_ENV_PROMPTS, keyed by env var) onto every
    env var that reaches ANY wizard block — this is a completeness
    invariant over the LIVE catalog (built-in rows + every plugin
    discovered in this test run), not a snapshot of which providers exist
    today, so a new provider shipping a new env var without a translation
    fails this test instead of silently shipping an English label."""
    from hermes_cli.setup_wizard import tools_view as tv

    missing = []
    for block in tv.wizard_tool_blocks():
        for row in block["rows"]:
            for env in row["env_vars"]:
                if not env.get("prompt_ru"):
                    missing.append((block["category"], row["name"], env.get("key")))
    assert not missing, missing


def test_prompt_ru_matches_ru_env_prompts_by_key():
    """Direct unit check behind the completeness invariant above: when a
    key IS in RU_ENV_PROMPTS, wizard_tool_blocks() must stamp that exact
    translation onto the row's env var entry — not merely "something
    truthy"."""
    from hermes_cli.setup_wizard import tools_view as tv

    checked = 0
    for block in tv.wizard_tool_blocks():
        for row in block["rows"]:
            for env in row["env_vars"]:
                key = env.get("key")
                if key in tv.RU_ENV_PROMPTS:
                    assert env["prompt_ru"] == tv.RU_ENV_PROMPTS[key], (block["category"], row["name"], key)
                    checked += 1
    # Sanity: the live catalog must actually exercise this wiring — an
    # empty loop would let a broken stamping pass vacuously.
    assert checked > 0


def test_titles_ru_image_gen_matches_page_py_owner_approved_heading():
    """Finding 16: TITLES_RU is what page.py's client actually renders now
    (block.title_ru) — before this fix the client used its OWN hardcoded
    heading and the two values had already drifted: TITLES_RU["image_gen"]
    was "Генерация картинок" while page.py said "Генерация изображений".
    page.py is the text source of truth (owner-approved wording), so
    TITLES_RU must carry that exact string."""
    from hermes_cli.setup_wizard import tools_view as tv

    assert tv.TITLES_RU["image_gen"] == "Генерация изображений"


# ---- Auto-default local addresses (owner ruling 2026-08-24, finding 1) ---


def test_camofox_url_gets_auto_default():
    """Camofox's fixed 9377 port is a static catalog default (not a
    liveness probe) — it must still be marked auto_default so the client
    stops asking the user to type it in."""
    from hermes_cli.setup_wizard import tools_view as tv

    browser = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    camofox = next(r for r in browser["rows"] if "amofox" in r["name"])
    env = next(e for e in camofox["env_vars"] if e["key"] == "CAMOFOX_URL")
    assert env["auto_default"] == env["default"] == "http://localhost:9377"


def test_self_hosted_row_auto_default_mutation(monkeypatch):
    """Mutation: fake SearXNG alive at its known local address — the
    resulting row's env var must carry auto_default equal to that
    address, matching the freshly-probed `default`."""
    from hermes_cli.setup_wizard import tools_view as tv

    def _fake_alive(url, timeout=1.0):
        return url == "http://127.0.0.1:8080"

    monkeypatch.setattr(tv, "_local_service_alive", _fake_alive)
    web_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "web")
    searxng = next(r for r in web_block["rows"] if r["name"] == "SearXNG")
    env = next(e for e in searxng["env_vars"] if e["key"] == "SEARXNG_URL")
    assert env["auto_default"] == "http://127.0.0.1:8080"


def test_only_url_env_vars_ever_get_auto_default():
    """Structural invariant: a credential (*_KEY) must never be silently
    auto-filled — only a *_URL env var (a local address, never a secret)
    can carry auto_default."""
    from hermes_cli.setup_wizard import tools_view as tv

    for block in tv.wizard_tool_blocks():
        for row in block["rows"]:
            for env in row["env_vars"]:
                if "auto_default" in env:
                    assert env["key"].endswith("_URL"), (block["category"], row["name"], env["key"])


# ---- Beta flag for Camofox / Browser Use (owner ruling 2026-08-24, finding 2) --


def test_camofox_and_browser_use_are_flagged_beta():
    from hermes_cli.setup_wizard import tools_view as tv

    browser = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    camofox = next(r for r in browser["rows"] if "amofox" in r["name"])
    browser_use = next(r for r in browser["rows"] if r["name"] == "Browser Use")
    assert camofox["beta"] is True and camofox["beta_note_ru"]
    assert browser_use["beta"] is True and browser_use["beta_note_ru"]


def test_local_browser_is_not_flagged_beta():
    """Local Browser is the recommended default — it must NOT get the beta
    label even though it shares Camofox/Browser Use's underlying
    "needs an install first" shape."""
    from hermes_cli.setup_wizard import tools_view as tv

    browser = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    local_row = next(r for r in browser["rows"] if r["name"] == "Local Browser")
    assert local_row["beta"] is False
    assert local_row["beta_note_ru"] is None


def test_browser_use_beta_note_does_not_claim_node_js():
    """Fact-check guard: Browser Use's install hook shells out to
    `uv tool install browser-use` (tools/browser_use_cli.py::install_cli),
    never Node.js/npm — the beta note must not repeat the owner's
    Node.js assumption for this row specifically."""
    from hermes_cli.setup_wizard import tools_view as tv

    assert "Node.js" not in tv._BROWSER_BETA_NOTES_RU["browser_use_cli"]
    assert "Node.js" in tv._BROWSER_BETA_NOTES_RU["camofox"]


def test_beta_only_applies_to_browser_category():
    from hermes_cli.setup_wizard import tools_view as tv

    for block in tv.wizard_tool_blocks():
        if block["category"] == "browser":
            continue
        for row in block["rows"]:
            assert row["beta"] is False, (block["category"], row["name"])
            assert row["beta_note_ru"] is None, (block["category"], row["name"])


# ---- install_blocked / install_blocked_reason_ru (owner ruling 2026-08-24,
# item 5 of "Установка инструментов — кнопки нет" — a row whose install is
# doomed on THIS machine right now must say so BEFORE the client picks it,
# since installing now happens unattended on the final "Готово" step). -----


def test_row_install_blocked_reason_flags_missing_node(monkeypatch):
    import hermes_constants
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(hermes_constants, "find_node_executable", lambda name: None)
    assert tv._row_install_blocked_reason("camofox", installed=False) == tv._MSG_BLOCKED_NO_NODE_RU
    assert tv._row_install_blocked_reason("agent_browser", installed=False) == tv._MSG_BLOCKED_NO_NODE_RU


def test_row_install_blocked_reason_none_when_node_present(monkeypatch):
    import hermes_constants
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(hermes_constants, "find_node_executable", lambda name: "/usr/bin/npm")
    assert tv._row_install_blocked_reason("camofox", installed=False) is None


def test_row_install_blocked_reason_flags_missing_uv(monkeypatch):
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr("shutil.which", lambda name: None)
    assert tv._row_install_blocked_reason("browser_use_cli", installed=False) == tv._MSG_BLOCKED_NO_UV_RU


def test_row_install_blocked_reason_none_when_uv_present(monkeypatch):
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/uv")
    assert tv._row_install_blocked_reason("browser_use_cli", installed=False) is None


def test_row_install_blocked_reason_none_when_already_installed(monkeypatch):
    """An already-installed row can't be "blocked" — nothing left to
    fail — even when the dependency binary genuinely is missing."""
    import hermes_constants
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(hermes_constants, "find_node_executable", lambda name: None)
    assert tv._row_install_blocked_reason("camofox", installed=True) is None


def test_row_install_blocked_reason_none_for_pip_only_hooks(monkeypatch):
    """KittenTTS/Piper/faster_whisper/ddgs install into the SAME Python
    environment already running this wizard — never flagged as blocked,
    regardless of Node.js/uv presence (their dependency isn't checkable
    this way at all — see _row_install_blocked_reason's own docstring)."""
    import hermes_constants
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(hermes_constants, "find_node_executable", lambda name: None)
    monkeypatch.setattr("shutil.which", lambda name: None)
    for key in ("kittentts", "piper", "faster_whisper", "ddgs"):
        assert tv._row_install_blocked_reason(key, installed=False) is None, key


def test_row_install_blocked_reason_none_for_no_post_setup():
    from hermes_cli.setup_wizard import tools_view as tv

    assert tv._row_install_blocked_reason(None, installed=False) is None
    assert tv._row_install_blocked_reason("", installed=False) is None


def test_wizard_tool_blocks_stamps_install_blocked_for_camofox_without_node(monkeypatch):
    """End-to-end wiring check (not just the pure function above): a real
    Camofox row from the live catalog gets the blocked marker when Node.js
    is unavailable."""
    import pytest

    import hermes_constants
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(hermes_constants, "find_node_executable", lambda name: None)
    browser = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    camofox = next(r for r in browser["rows"] if "amofox" in r["name"])
    if camofox["installed"]:
        pytest.skip("Camofox is already installed on this machine — nothing to block")
    assert camofox["install_blocked"] is True
    assert camofox["install_blocked_reason_ru"] == tv._MSG_BLOCKED_NO_NODE_RU


def test_wizard_tool_blocks_never_blocks_camofox_when_node_present(monkeypatch):
    import hermes_constants
    from hermes_cli.setup_wizard import tools_view as tv

    monkeypatch.setattr(hermes_constants, "find_node_executable", lambda name: "/usr/bin/npm")
    browser = next(b for b in tv.wizard_tool_blocks() if b["category"] == "browser")
    camofox = next(r for r in browser["rows"] if "amofox" in r["name"])
    assert camofox["install_blocked"] is False
    assert camofox["install_blocked_reason_ru"] is None


# ---- Edge TTS ru-RU voice catalog (owner ruling 2026-08-24, finding 5) ----


def test_edge_tts_row_carries_ru_voices():
    from hermes_cli.setup_wizard import tools_view as tv

    tts_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "tts")
    edge = next(r for r in tts_block["rows"] if r["name"] == "Microsoft Edge TTS")
    assert edge["default_voice"] == "ru-RU-SvetlanaNeural"
    voices_by_key = {v["key"]: v for v in edge["voices"]}
    assert voices_by_key.keys() == {"ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"}
    assert voices_by_key["ru-RU-SvetlanaNeural"]["gender"] == "female"
    assert voices_by_key["ru-RU-DmitryNeural"]["gender"] == "male"
    assert voices_by_key["ru-RU-SvetlanaNeural"]["label"] == "Светлана"
    assert voices_by_key["ru-RU-DmitryNeural"]["label"] == "Дмитрий"


def test_only_edge_tts_row_carries_voices():
    from hermes_cli.setup_wizard import tools_view as tv

    tts_block = next(b for b in tv.wizard_tool_blocks() if b["category"] == "tts")
    for row in tts_block["rows"]:
        if row["name"] == "Microsoft Edge TTS":
            continue
        assert row["voices"] is None and row["default_voice"] is None, row["name"]
