"""Provider catalog view for the setup wizard (spec §7.2, invariant §15.1)."""
from __future__ import annotations


def _all_provider_names():
    import providers

    return {p.name for p in providers.list_providers()}


def test_every_provider_resolved():
    """Инвариант §15.1: каждый провайдер каталога либо рендерится, либо
    исключён с причиной. Новый upstream-провайдер роняет тест, пока его
    не разобрали. Имена НЕ фиксируются (не снимок)."""
    from hermes_cli.setup_wizard.providers_view import (
        EXCLUDED_PROVIDERS,
        wizard_providers,
    )

    rendered = {p["name"] for p in wizard_providers()}
    excluded = set(EXCLUDED_PROVIDERS)
    assert not (rendered & excluded)
    unresolved = _all_provider_names() - rendered - excluded
    assert not unresolved, f"нерешённые провайдеры: {sorted(unresolved)}"


def test_excluded_have_reasons_in_russian():
    from hermes_cli.setup_wizard.providers_view import EXCLUDED_PROVIDERS

    for name, reason in EXCLUDED_PROVIDERS.items():
        assert len(reason) > 15, name


def test_rendered_rows_carry_form_fields():
    from hermes_cli.setup_wizard.providers_view import wizard_providers

    for row in wizard_providers():
        assert row["kind"] in ("api_key", "device_code")
        if row["kind"] == "api_key":
            assert row["env_var"], row["name"]


def test_no_nous_in_rendered():
    """Продуктовое решение (спеки 1–2): машина клиента не предлагает Nous."""
    from hermes_cli.setup_wizard.providers_view import wizard_providers

    assert all(p["name"] != "nous" for p in wizard_providers())


def test_rendered_kind_matches_profile_auth_type():
    """Настоящий инвариант полноты: rendered kind обязан отражать
    фактический auth_type профиля, а не просто «имя не в EXCLUDED».
    Ловит случай, когда провайдер убрали из EXCLUDED_PROVIDERS, но его
    auth_type не api_key (например qwen-oauth, copilot — у обоих есть
    env_vars, поэтому test_rendered_rows_carry_form_fields их не ловит)."""
    import providers
    from hermes_cli.setup_wizard.providers_view import (
        DEVICE_CODE_PROVIDERS, wizard_providers)

    for row in wizard_providers():
        auth = providers.get_provider_profile(row["name"]).auth_type
        expected = "oauth_external" if row["name"] in DEVICE_CODE_PROVIDERS else "api_key"
        assert auth == expected, (row["name"], row["kind"], auth)


def test_excluded_providers_known_to_catalog():
    """Ловит переименование/удаление upstream-провайдера: запись в
    EXCLUDED_PROVIDERS не должна ссылаться на провайдер, которого больше
    нет в каталоге."""
    from hermes_cli.setup_wizard.providers_view import EXCLUDED_PROVIDERS

    assert set(EXCLUDED_PROVIDERS) <= _all_provider_names()


def test_device_code_providers_known_to_catalog():
    """То же для DEVICE_CODE_PROVIDERS."""
    from hermes_cli.setup_wizard.providers_view import DEVICE_CODE_PROVIDERS

    assert set(DEVICE_CODE_PROVIDERS) <= _all_provider_names()


def test_row_contract_keys_and_custom_last():
    """Контракт строки (задача 9b): ровно эти ключи, ни больше ни меньше;
    если есть строка custom — она должна идти последней."""
    from hermes_cli.setup_wizard.providers_view import wizard_providers

    expected_keys = {
        "name",
        "display_name",
        "description",
        "description_ru",
        "signup_url",
        "kind",
        "env_var",
        "base_url",
        "fallback_models",
    }
    rows = wizard_providers()
    for row in rows:
        assert set(row.keys()) == expected_keys, row["name"]

    names = [row["name"] for row in rows]
    if "custom" in names:
        assert names[-1] == "custom"


def test_display_name_overrides_reference_real_providers():
    """Ловит опечатку/переименование upstream-провайдера в
    ``DISPLAY_NAME_OVERRIDES``: каждый ключ обязан существовать в живом
    каталоге, иначе override молча не применяется."""
    from hermes_cli.setup_wizard.providers_view import DISPLAY_NAME_OVERRIDES

    assert set(DISPLAY_NAME_OVERRIDES) <= _all_provider_names()


def test_every_rendered_row_has_a_display_name():
    """Каждая строка мастера обязана иметь непустой display_name — сырой
    ``ProviderProfile`` с пустым ``display_name`` не должен доходить до
    владельца как голый id."""
    from hermes_cli.setup_wizard.providers_view import wizard_providers

    for row in wizard_providers():
        assert row["display_name"], row["name"]


def test_openai_codex_shows_recognizable_chatgpt_name():
    """Регресс диагностированного бага: владелец мастера не находил
    ChatGPT в селекте, потому что провайдер рендерился как сырой слаг
    ``openai-codex``."""
    from hermes_cli.setup_wizard.providers_view import wizard_providers

    rows = {row["name"]: row for row in wizard_providers()}
    assert rows["openai-codex"]["display_name"] == "ChatGPT (подписка OpenAI)"


def test_openai_api_provider_is_rendered():
    """Вторая часть диагноза: прямой OpenAI-по-ключу провайдер (``openai-api``,
    bundled plugin) обязан появиться в живом каталоге мастера — как
    api_key-строка с env_var."""
    from hermes_cli.setup_wizard.providers_view import wizard_providers

    rows = {row["name"]: row for row in wizard_providers()}
    assert "openai-api" in rows
    row = rows["openai-api"]
    assert row["kind"] == "api_key"
    assert row["env_var"] == "OPENAI_API_KEY"
    assert row["display_name"] and row["display_name"] != "openai-api"


def test_openai_api_shows_russian_display_name_via_override():
    """The profile's own display_name is English (``"OpenAI API"``) — it is
    read verbatim by non-localized surfaces (CLI picker, desktop Settings,
    see hermes_cli/provider_catalog.py). The wizard is Russian-only, so its
    Russian label must come from DISPLAY_NAME_OVERRIDES, not the profile."""
    from hermes_cli.setup_wizard.providers_view import wizard_providers

    rows = {row["name"]: row for row in wizard_providers()}
    assert rows["openai-api"]["display_name"] == "OpenAI (ChatGPT) — по API-ключу"


# ---------------------------------------------------------------------------
# wizard_provider_groups() — owner requirement 1: "Есть провайдер OpenAI —
# ОДИН. Внутри — выбор способа подключения." Grouping comes from
# hermes_cli.models.PROVIDER_GROUPS (upstream, not a hand-rolled list here).
# ---------------------------------------------------------------------------


def test_every_variant_belongs_to_exactly_one_group():
    """Completeness/uniqueness invariant: every wizard_providers() row shows
    up in exactly one group's variants — none dropped, none duplicated."""
    from hermes_cli.setup_wizard.providers_view import (
        wizard_provider_groups,
        wizard_providers,
    )

    flat_names = {row["name"] for row in wizard_providers()}
    seen: list[str] = []
    for group in wizard_provider_groups():
        for variant in group["variants"]:
            seen.append(variant["name"])
    assert sorted(seen) == sorted(set(seen)), "a variant appeared in more than one group"
    assert set(seen) == flat_names


def test_excluded_group_members_do_not_appear_as_variants():
    """copilot / copilot-acp are both in EXCLUDED_PROVIDERS — the whole
    ``copilot`` PROVIDER_GROUPS entry must vanish, not appear with an empty
    or partial variants list."""
    from hermes_cli.setup_wizard.providers_view import wizard_provider_groups

    groups = wizard_provider_groups()
    assert all(g["group_id"] != "copilot" for g in groups)
    all_variant_names = {v["name"] for g in groups for v in g["variants"]}
    assert "copilot" not in all_variant_names
    assert "copilot-acp" not in all_variant_names


def test_openai_is_a_single_group_with_two_variants():
    """The literal bug report: OpenAI must be ONE top-level entry with a
    device-code variant and an api-key variant underneath it — not two
    separate top-level provider rows."""
    from hermes_cli.setup_wizard.providers_view import wizard_provider_groups

    groups = {g["group_id"]: g for g in wizard_provider_groups()}
    assert "openai" in groups
    openai_group = groups["openai"]
    assert openai_group["display_name"] == "OpenAI (ChatGPT)"
    variant_names = {v["name"] for v in openai_group["variants"]}
    assert variant_names == {"openai-codex", "openai-api"}
    # Never two top-level rows for the same vendor.
    assert not any(
        gid != "openai" and any(v["name"] in ("openai-codex", "openai-api") for v in g["variants"])
        for gid, g in groups.items()
    )


def test_minimax_group_has_three_variants_with_distinct_auth_labels():
    from hermes_cli.setup_wizard.providers_view import wizard_provider_groups

    groups = {g["group_id"]: g for g in wizard_provider_groups()}
    assert "minimax" in groups
    variants = {v["name"]: v for v in groups["minimax"]["variants"]}
    assert set(variants) == {"minimax", "minimax-oauth", "minimax-cn"}
    assert variants["minimax-oauth"]["auth_label"] == "Вход по аккаунту (код устройства)"
    assert variants["minimax-oauth"]["kind"] == "device_code"
    # The two api_key variants must not collapse to the identical label —
    # a client rendering a radio group needs to tell them apart.
    assert variants["minimax"]["auth_label"] != variants["minimax-cn"]["auth_label"]


def test_single_survivor_group_degrades_to_its_own_variant_display_name():
    """qwen's PROVIDER_GROUPS entry has 3 declared members but one
    (qwen-oauth) is excluded — 2 survive, so it stays a real (non-degraded)
    multi-variant group. This test instead fabricates the degrade case
    directly against the real implementation contract: a group_id whose
    survivors number exactly one must show that variant's OWN display_name,
    not the group's collapsed label — this is exercised for real whenever a
    future PROVIDER_GROUPS entry loses all-but-one member to
    EXCLUDED_PROVIDERS, so assert the general shape here rather than wait
    for that to happen upstream."""
    from hermes_cli.setup_wizard.providers_view import wizard_provider_groups

    for group in wizard_provider_groups():
        if len(group["variants"]) == 1:
            assert group["display_name"] == group["variants"][0]["display_name"]


def test_variant_row_carries_flat_fields_plus_auth_label():
    from hermes_cli.setup_wizard.providers_view import wizard_provider_groups

    expected_keys = {
        "name",
        "display_name",
        "description",
        "description_ru",
        "signup_url",
        "kind",
        "env_var",
        "base_url",
        "fallback_models",
        "auth_label",
    }
    for group in wizard_provider_groups():
        for variant in group["variants"]:
            assert set(variant.keys()) == expected_keys, variant["name"]
            assert variant["auth_label"], variant["name"]


def test_group_display_name_overrides_reference_real_groups():
    """Catches a typo'd/renamed key the same way
    test_display_name_overrides_reference_real_providers does for
    DISPLAY_NAME_OVERRIDES."""
    from hermes_cli.models import PROVIDER_GROUPS
    from hermes_cli.setup_wizard.providers_view import GROUP_DISPLAY_NAME_OVERRIDES

    assert set(GROUP_DISPLAY_NAME_OVERRIDES) <= set(PROVIDER_GROUPS)


def test_provider_groups_sorted_recommended_first_then_alphabetical():
    """Spec A3: order changed from a flat alphabetical sort to
    "recommended groups first, alphabetical within each partition" — every
    recommended group must precede every non-recommended one, and each
    partition must itself still be alphabetical by display_name."""
    from hermes_cli.setup_wizard.providers_view import wizard_provider_groups

    groups = wizard_provider_groups()
    recommended_flags = [g["recommended"] for g in groups]
    # Once a non-recommended group appears, every group after it must also
    # be non-recommended — i.e. recommended is a prefix, never scattered.
    assert recommended_flags == sorted(recommended_flags, key=lambda r: not r)

    rec_names = [g["display_name"].lower() for g in groups if g["recommended"]]
    rest_names = [g["display_name"].lower() for g in groups if not g["recommended"]]
    assert rec_names == sorted(rec_names)
    assert rest_names == sorted(rest_names)


def test_every_group_has_distinct_auth_labels():
    """Review finding: opencode-zen/opencode-go both fell through to the
    generic "По API-ключу" label and rendered as two indistinguishable
    radio options. This is the CLASS invariant, not a one-off regression
    test for that single group — any group with two variants sharing an
    auth_label is the same bug, wherever it shows up next (a new
    PROVIDER_GROUPS entry, a new api_key sibling added to an existing
    one, ...)."""
    from hermes_cli.setup_wizard.providers_view import wizard_provider_groups

    for group in wizard_provider_groups():
        labels = [v["auth_label"] for v in group["variants"]]
        assert len(set(labels)) == len(labels), (group["group_id"], labels)


def test_opencode_variants_have_distinct_auth_labels():
    """The literal regression this invariant was written for."""
    from hermes_cli.setup_wizard.providers_view import wizard_provider_groups

    groups = {g["group_id"]: g for g in wizard_provider_groups()}
    assert "opencode" in groups
    labels = {v["name"]: v["auth_label"] for v in groups["opencode"]["variants"]}
    assert labels["opencode-zen"] != labels["opencode-go"]


# ---- fetch_live_models: proxy routes through our own httpx GET; no proxy
# keeps the original ProviderProfile.fetch_models (urllib) path untouched --


def _fake_profile(**overrides):
    from unittest.mock import MagicMock

    profile = MagicMock()
    profile.name = overrides.get("name", "openrouter")
    profile.base_url = overrides.get("base_url", "https://openrouter.ai/api/v1")
    profile.models_url = overrides.get("models_url", "")
    profile.default_headers = overrides.get("default_headers", {})
    return profile


def test_fetch_live_models_without_proxy_uses_the_original_fetch_models_path():
    """No proxy configured -> unchanged behavior: delegates to
    ProviderProfile.fetch_models (upstream, urllib-based) exactly as
    before this pass — the wizard's own httpx path must not be touched."""
    from unittest.mock import patch

    from hermes_cli.setup_wizard import providers_view as pv

    profile = _fake_profile()
    profile.fetch_models.return_value = ["m1", "m2"]

    with patch.object(pv.providers, "get_provider_profile", return_value=profile), patch.object(
        pv.httpx, "Client"
    ) as client_mk:
        out = pv.fetch_live_models("openrouter", "sk-live", "")

    assert out == ["m1", "m2"]
    profile.fetch_models.assert_called_once_with(api_key="sk-live", base_url=None)
    client_mk.assert_not_called()


def test_fetch_live_models_with_proxy_goes_through_our_own_httpx_get():
    """A form proxy must route the probe through our own httpx GET instead
    of the upstream, proxy-less ProviderProfile.fetch_models — and must
    never call that upstream method at all once a proxy is set."""
    from unittest.mock import MagicMock, patch

    from hermes_cli.setup_wizard import providers_view as pv

    profile = _fake_profile(base_url="https://openrouter.ai/api/v1")
    resp = MagicMock(is_success=True)
    resp.json.return_value = {"data": [{"id": "m1"}, {"id": "m2"}]}
    client = MagicMock()
    client.__enter__ = lambda s: client
    client.__exit__ = lambda s, *a: False
    client.get.return_value = resp

    with patch.object(pv.providers, "get_provider_profile", return_value=profile), patch.object(
        pv.httpx, "Client", return_value=client
    ) as client_mk:
        out = pv.fetch_live_models("openrouter", "sk-live", "", "socks5://u:p@h:1080")

    assert out == ["m1", "m2"]
    profile.fetch_models.assert_not_called()
    assert client_mk.call_args.kwargs.get("proxy") == "socks5://u:p@h:1080"
    called_url, called_headers = client.get.call_args[0][0], client.get.call_args[1]["headers"]
    assert called_url == "https://openrouter.ai/api/v1/models"
    assert called_headers["Authorization"] == "Bearer sk-live"


def test_fetch_live_models_with_proxy_prefers_models_url_override():
    """Mirrors ProviderProfile.fetch_models' own URL-resolution order
    (models_url wins over base_url + '/models' when set)."""
    from unittest.mock import MagicMock, patch

    from hermes_cli.setup_wizard import providers_view as pv

    profile = _fake_profile(
        base_url="https://openrouter.ai/api/v1", models_url="https://openrouter.ai/api/v1/models/public"
    )
    resp = MagicMock(is_success=True)
    resp.json.return_value = {"data": ["m1"]}
    client = MagicMock()
    client.__enter__ = lambda s: client
    client.__exit__ = lambda s, *a: False
    client.get.return_value = resp

    with patch.object(pv.providers, "get_provider_profile", return_value=profile), patch.object(
        pv.httpx, "Client", return_value=client
    ):
        out = pv.fetch_live_models("openrouter", "sk-live", "", "http://h:8080")

    assert out == ["m1"]
    called_url = client.get.call_args[0][0]
    assert called_url == "https://openrouter.ai/api/v1/models/public"


def test_fetch_live_models_with_proxy_returns_empty_on_network_failure():
    from unittest.mock import patch

    from hermes_cli.setup_wizard import providers_view as pv

    profile = _fake_profile()

    with patch.object(pv.providers, "get_provider_profile", return_value=profile), patch.object(
        pv.httpx, "Client", side_effect=OSError("unreachable")
    ):
        out = pv.fetch_live_models("openrouter", "sk-live", "", "http://h:8080")

    assert out == []


# ---------------------------------------------------------------------------
# A1 — Russian descriptions (never the English upstream original).
# ---------------------------------------------------------------------------


def test_every_wizard_provider_has_a_russian_description():
    """Completeness invariant against the LIVE catalog, not a snapshot: a
    new upstream provider that lands without a RU_PROVIDER_DESCRIPTIONS
    entry must fail this test instead of silently shipping an English (or
    blank-but-unnoticed) description to a Russian-only client."""
    from hermes_cli.setup_wizard.providers_view import wizard_providers

    missing = [row["name"] for row in wizard_providers() if not row["description_ru"]]
    assert not missing, missing


def test_description_ru_never_falls_back_to_the_english_original():
    """The defect this closes: row.description used to reach the client
    verbatim in English. Guard both ends — description_ru must differ from
    the raw upstream description whenever that description is non-empty
    (an empty upstream description trivially can't leak anything)."""
    from hermes_cli.setup_wizard.providers_view import wizard_providers

    for row in wizard_providers():
        if row["description"]:
            assert row["description_ru"] != row["description"], row["name"]


def test_missing_translation_degrades_to_empty_string_not_english(monkeypatch):
    """Mutation: a provider with NO entry in RU_PROVIDER_DESCRIPTIONS must
    render description_ru == "" — never fall through to the English
    ``description`` field (that fallback is exactly the bug this task
    closes)."""
    from hermes_cli.setup_wizard import providers_view as pv

    monkeypatch.setattr(pv, "RU_PROVIDER_DESCRIPTIONS", {})
    rows = pv.wizard_providers()
    assert rows, "live catalog must have at least one provider to exercise this"
    for row in rows:
        assert row["description_ru"] == ""


def test_recommended_five_use_the_verbatim_mockup_wording():
    """Spec A1/A3: the five owner-recommended groups' wording is taken
    VERBATIM from the approved mockup (screen 4) — not paraphrased."""
    from hermes_cli.setup_wizard.providers_view import wizard_provider_groups

    groups = {g["group_id"]: g for g in wizard_provider_groups()}
    expected = {
        "deepseek": "Дёшево и работает из России без прокси. Хороший выбор, если пробуете впервые.",
        "zai": "Тоже напрямую из России, сильнее в длинных задачах, дороже DeepSeek.",
        "google": "Работает напрямую, есть бесплатный уровень для нечастых задач.",
        "openai": "Лучшие модели. Можно войти подпиской ChatGPT вместо покупки ключа.",
        "openrouter": "Сотни моделей по одному ключу, включая Claude. Удобно, если хотите пробовать разные.",
    }
    for gid, text in expected.items():
        assert gid in groups, gid
        assert groups[gid]["description_ru"] == text, gid


def test_multi_variant_group_description_is_not_just_the_first_variants():
    """openai has two variants (openai-codex, openai-api) — its group-level
    description must come from GROUP_DESCRIPTION_OVERRIDES, not silently
    default to whichever variant happens to sort first."""
    from hermes_cli.setup_wizard.providers_view import GROUP_DESCRIPTION_OVERRIDES, wizard_provider_groups

    groups = {g["group_id"]: g for g in wizard_provider_groups()}
    assert "openai" in GROUP_DESCRIPTION_OVERRIDES
    assert groups["openai"]["description_ru"] == GROUP_DESCRIPTION_OVERRIDES["openai"]


def test_group_description_overrides_reference_real_groups():
    """Same typo/rename guard as GROUP_DISPLAY_NAME_OVERRIDES."""
    from hermes_cli.models import PROVIDER_GROUPS
    from hermes_cli.setup_wizard.providers_view import GROUP_DESCRIPTION_OVERRIDES

    assert set(GROUP_DESCRIPTION_OVERRIDES) <= set(PROVIDER_GROUPS)


# ---------------------------------------------------------------------------
# A2 — anthropic device-code variant must never render (forward-guard).
# ---------------------------------------------------------------------------


def test_anthropic_today_has_no_device_code_variant():
    """Precondition the plan documents: today's live catalog has nothing
    for the structural rule to exclude — anthropic's sole member is
    api_key. If this ever fails, the forward-guard test below has become
    a REAL regression guard, not a synthetic one — investigate rather than
    just deleting this test."""
    from hermes_cli.setup_wizard.providers_view import wizard_provider_groups

    groups = {g["group_id"]: g for g in wizard_provider_groups()}
    assert "anthropic" in groups
    kinds = {v["kind"] for v in groups["anthropic"]["variants"]}
    assert kinds == {"api_key"}


def test_anthropic_device_code_variant_structurally_excluded_mutation(monkeypatch):
    """Spec A2: mutate the catalog so ``anthropic`` LOOKS like a
    device-code provider (synthetic — no such variant exists upstream
    today) and assert it never reaches the wizard, while an unrelated
    device-code provider (openai-codex) is untouched."""
    from hermes_cli.setup_wizard import providers_view as pv

    monkeypatch.setattr(pv, "DEVICE_CODE_PROVIDERS", frozenset({"openai-codex", "minimax-oauth", "anthropic"}))

    rows = {row["name"]: row for row in pv.wizard_providers()}
    assert "anthropic" not in rows, "device_code anthropic variant must never render"
    assert "openai-codex" in rows, "the rule must not over-fire on other device_code providers"
    assert rows["openai-codex"]["kind"] == "device_code"

    groups = {g["group_id"]: g for g in pv.wizard_provider_groups()}
    assert "anthropic" not in groups, "the whole group vanishes when its only member is blocked"


def test_is_blocked_anthropic_device_code_unit():
    from hermes_cli.setup_wizard.providers_view import _is_blocked_anthropic_device_code

    assert _is_blocked_anthropic_device_code("anthropic", "device_code") is True
    assert _is_blocked_anthropic_device_code("anthropic", "api_key") is False
    # A device_code OTHER provider (real, ungrouped-from-anthropic's-
    # perspective) must not be caught — the rule is group-scoped, not a
    # blanket "no device_code anywhere" rule.
    assert _is_blocked_anthropic_device_code("openai-codex", "device_code") is False


# ---------------------------------------------------------------------------
# A3 — recommended groups + reason, recommended-first ordering.
# ---------------------------------------------------------------------------


def test_recommended_groups_reference_real_groups():
    """Same typo/rename guard as the display-name/description override
    dicts above — a RECOMMENDED_GROUPS key that isn't a real group_id in
    the live catalog would silently mark nothing as recommended."""
    from hermes_cli.setup_wizard.providers_view import RECOMMENDED_GROUPS, wizard_provider_groups

    live_group_ids = {g["group_id"] for g in wizard_provider_groups()}
    assert set(RECOMMENDED_GROUPS) <= live_group_ids


def test_exactly_the_five_owner_approved_groups_are_recommended():
    from hermes_cli.setup_wizard.providers_view import wizard_provider_groups

    groups = wizard_provider_groups()
    recommended_ids = {g["group_id"] for g in groups if g["recommended"]}
    assert recommended_ids == {"deepseek", "zai", "google", "openai", "openrouter"}
    for g in groups:
        if g["recommended"]:
            assert g["recommended_reason"], g["group_id"]
        else:
            assert g["recommended_reason"] == "", g["group_id"]


def test_client_sees_every_group_recommended_or_not():
    """Owner ruling: no "hidden until expanded" split server-side — the
    client receives every group, recommended just sorts first (see the
    ordering test above)."""
    from hermes_cli.setup_wizard.providers_view import wizard_provider_groups

    groups = wizard_provider_groups()
    assert any(g["recommended"] for g in groups)
    assert any(not g["recommended"] for g in groups)


def test_recommended_flag_mutation_via_monkeypatched_dict(monkeypatch):
    """Proves ``recommended``/``recommended_reason`` are LIVE reads of
    RECOMMENDED_GROUPS, not a value baked in some other way — emptying the
    dict clears every flag, and every group sorts as if none were
    recommended (falls back to a plain alphabetical order)."""
    from hermes_cli.setup_wizard import providers_view as pv

    monkeypatch.setattr(pv, "RECOMMENDED_GROUPS", {})
    groups = pv.wizard_provider_groups()
    assert all(g["recommended"] is False for g in groups)
    assert all(g["recommended_reason"] == "" for g in groups)
    names = [g["display_name"].lower() for g in groups]
    assert names == sorted(names)
