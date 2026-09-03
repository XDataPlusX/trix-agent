"""Provider catalog view for the wizard form (spec §7.2).

Renders the wizard's "model provider" block generically from
``providers.list_providers()`` / ``ProviderProfile`` — never from a
hand-maintained list. A handful of providers cannot be expressed as a
web-form field (external OS-level credentials, a deliberate product
decision) and are excluded by name with a documented reason instead.

Invariant (spec §15.1, enforced by
``tests/hermes_cli/test_setup_wizard_providers_view.py::test_every_provider_resolved``):
every provider in the catalog must be either rendered by
``wizard_providers()`` or listed in ``EXCLUDED_PROVIDERS``. When a new
provider lands upstream with an ``auth_type`` that isn't a plain API key
and isn't one of the two known device-code flows, it must be added to
``EXCLUDED_PROVIDERS`` with a real reason — never silently rendered as a
generic api_key block, and never invented a reason for.
"""
from __future__ import annotations

import logging

import httpx

import providers
from hermes_cli.models import PROVIDER_GROUPS, provider_group_for_slug

logger = logging.getLogger(__name__)

# name -> причина исключения (спека §7.2, таблица «Семь отсутствуют»).
EXCLUDED_PROVIDERS: dict[str, str] = {
    "nous": "продуктовое решение: машина клиента не обращается к Nous (спека 2)",
    "qwen-oauth": "читает ~/.qwen/oauth_creds.json, который пишет отдельная CLI Qwen — веб-форме пути нет",
    "copilot": "аутентификация GitHub Copilot живёт во внешнем процессе на машине",
    "copilot-acp": "внешний процесс ACP, не поле формы",
    "bedrock": "креды AWS SDK (файлы/instance profile), не поле формы",
    "vertex": "креды GCP (service account), не поле формы",
    "custom": (
        "инлайновый endpoint-кредентиал мастеру записывать некуда (apply "
        "пишет ключи только в известные env-переменные) — кастомный "
        "эндпоинт настраивается по SSH через hermes model"
    ),
}

# Провайдеры с двухпутевым device-code входом (URL + код, машина опрашивает
# сама) — рендерятся отдельным под-блоком, а не generic api_key-полем.
DEVICE_CODE_PROVIDERS: frozenset[str] = frozenset({"openai-codex", "minimax-oauth"})

# Человекочитаемые имена для строк мастера, которые НЕ трогая
# upstream-профиль (``ProviderProfile.display_name`` пуст для этих
# провайдеров — см. plugins/model-providers/<name>/__init__.py). Это чисто
# отображение: подменяет только то, что видит владелец в живом мастере;
# ``name`` (id провайдера, используемый в config.yaml/CLI/резолвах) не
# меняется. Заполняется по мере обнаружения пустых display_name — тест
# ``test_display_name_overrides_reference_real_providers`` ловит опечатку
# или переименование ключа.
DISPLAY_NAME_OVERRIDES: dict[str, str] = {
    "openai-codex": "ChatGPT (подписка OpenAI)",
    "openai-api": "OpenAI (ChatGPT) — по API-ключу",
    "anthropic": "Anthropic (Claude)",
    "gemini": "Google Gemini",
    "xai": "xAI (Grok)",
    "minimax": "MiniMax",
    "minimax-cn": "MiniMax (Китай)",
    "kimi-coding": "Kimi (Moonshot)",
    "kimi-coding-cn": "Kimi (Китай)",
    "alibaba": "Alibaba Qwen",
    "ollama-cloud": "Ollama Cloud",
    "ai-gateway": "Vercel AI Gateway",
    "arcee": "Arcee AI",
    "kilocode": "Kilo Code",
    "opencode-go": "OpenCode Go",
    "opencode-zen": "OpenCode Zen",
    "stepfun": "StepFun",
    "xiaomi": "Xiaomi MiMo",
}


# Human-readable overrides for a GROUP's own label (spec: owner requirement
# 1 — "Есть провайдер OpenAI — ОДИН"). Keyed by ``PROVIDER_GROUPS`` group_id,
# applied ON TOP OF that group's own ``(label, description, members)`` tuple
# — the group's collapsed row uses this text when present, the upstream
# label otherwise. Purely display; the group_id and member slugs are
# untouched. ``test_group_display_name_overrides_reference_real_groups``
# catches a typo'd/renamed key the same way
# ``test_display_name_overrides_reference_real_providers`` does above.
GROUP_DISPLAY_NAME_OVERRIDES: dict[str, str] = {
    "openai": "OpenAI (ChatGPT)",
    "kimi": "Kimi (Moonshot)",
}

# Russian descriptions (spec §7.2/A1): what the provider is, roughly what it
# costs, whether it needs a proxy from Russia, what it's good for — one
# sentence, keyed by provider ``name`` (variant slug, same key domain as
# ``DISPLAY_NAME_OVERRIDES``/``AUTH_LABEL_OVERRIDES`` above). This is the fix
# for the defect spec §7.2 documents: ``row.description`` used to reach the
# client verbatim from the upstream catalog in ENGLISH (e.g. Actual
# Computer's "hosted inference via api.actual.inc, or local offline
# inference via ACTUAL_BASE_URL") — the same class of bug already closed for
# tool field labels via ``tools_view.RU_ENV_PROMPTS``/``prompt_ru``, which
# never touched providers. Wording for the five owner-recommended groups
# (DeepSeek, GLM/Z.ai, Gemini, OpenAI, OpenRouter) is taken VERBATIM from the
# approved mockup (``docs/product/specs/assets/2026-08-23-wizard-approved-
# mockup.html``, screen 4) — do not paraphrase those five.
#
# Must cover every surviving ``wizard_providers()`` row (enforced by
# ``test_every_wizard_provider_has_a_russian_description`` against the LIVE
# catalog, not a hardcoded snapshot) — a provider with no entry here gets
# ``""`` from ``wizard_providers()`` (never the English original), so a gap
# degrades to "no description shown", not to a language leak.
RU_PROVIDER_DESCRIPTIONS: dict[str, str] = {
    "actual": (
        "Инференс через сервис Actual (api.actual.inc) либо локально на своём "
        "сервере. Нишевый вариант для тех, кто уже пользуется Actual; обычно "
        "нужен прокси."
    ),
    "alibaba": (
        "Модели Alibaba Qwen через DashScope по ключу API, оплата по факту "
        "использования. Китайский сервис, доступен из России напрямую."
    ),
    "alibaba-coding-plan": (
        "Тот же Alibaba Qwen (DashScope), но по подписке Coding Plan — "
        "фиксированная плата вместо оплаты за токены."
    ),
    "anthropic": (
        "Claude по собственному ключу API. Аккаунты Anthropic заблокированы "
        "для России, но ключ через прокси работает — нужен прокси."
    ),
    "arcee": (
        "Arcee AI — облачный инференс открытых и собственных моделей Arcee, "
        "оплата по факту использования. Зарубежный сервис, обычно нужен прокси."
    ),
    "azure-foundry": (
        "Microsoft Azure AI Foundry — модели через собственный "
        "OpenAI-совместимый эндпоинт Azure, адрес указывается вручную. "
        "Обычно нужен прокси или корпоративная подписка Azure."
    ),
    "deepinfra": (
        "DeepInfra — более 100 открытых моделей по одному ключу, оплата по "
        "факту использования. Зарубежный сервис, обычно нужен прокси."
    ),
    "deepseek": "Дёшево и работает из России без прокси. Хороший выбор, если пробуете впервые.",
    "fireworks": (
        "Fireworks AI — быстрый OpenAI-совместимый инференс открытых моделей, "
        "оплата по факту использования. Обычно нужен прокси."
    ),
    "gmi": (
        "GMI Cloud — прямой доступ к нескольким моделям по одному ключу, "
        "оплата по факту использования. Обычно нужен прокси."
    ),
    "gemini": "Работает напрямую, есть бесплатный уровень для нечастых задач.",
    "huggingface": (
        "HuggingFace Inference API — модели из каталога HuggingFace по одному "
        "ключу, оплата по факту использования. Обычно нужен прокси."
    ),
    "kilocode": (
        "Kilo Code — агрегатор моделей для кодовых задач по одному ключу. "
        "Обычно нужен прокси."
    ),
    "kimi-coding": (
        "Kimi (Moonshot) — китайская модель для длинного контекста и кода, "
        "глобальный тариф по ключу API."
    ),
    "kimi-coding-cn": (
        "Тот же Kimi (Moonshot), но китайский региональный эндпоинт — тарифы "
        "и доступность отличаются от глобального."
    ),
    "minimax": "MiniMax по глобальному ключу API, оплата по факту использования.",
    "minimax-oauth": "MiniMax — вход аккаунтом по коду устройства вместо покупки ключа API.",
    "minimax-cn": "MiniMax через китайский региональный эндпоинт вместо глобального.",
    "novita": (
        "NovitaAI — облачный инференс для разработчиков и агентов, оплата по "
        "факту использования. Обычно нужен прокси."
    ),
    "nvidia": (
        "NVIDIA NIM — ускоренный инференс на инфраструктуре NVIDIA, оплата по "
        "факту использования. Обычно нужен прокси."
    ),
    "ollama-cloud": (
        "Ollama Cloud — облачная версия Ollama для запуска открытых моделей "
        "без своего сервера. Обычно нужен прокси."
    ),
    "openai-api": (
        "По собственному ключу API, оплата по факту использования токенов. "
        "Нужен прокси."
    ),
    "openai-codex": (
        "Вход аккаунтом по подписке ChatGPT Plus/Pro (код устройства) вместо "
        "покупки ключа API. Нужен прокси."
    ),
    "opencode-go": "OpenCode Go — агрегатор моделей для кода по подписке с фиксированной платой.",
    "opencode-zen": (
        "OpenCode Zen — тот же агрегатор, но оплата по факту использования "
        "вместо подписки."
    ),
    "openrouter": "Сотни моделей по одному ключу, включая Claude. Удобно, если хотите пробовать разные.",
    "stepfun": "StepFun — китайские модели по ключу API, оплата по факту использования.",
    "upstage": (
        "Upstage Solar — корейский облачный инференс по ключу API. Обычно "
        "нужен прокси."
    ),
    "ai-gateway": (
        "Vercel AI Gateway — единая точка доступа к нескольким провайдерам "
        "моделей через один ключ Vercel. Обычно нужен прокси."
    ),
    "xai": "xAI (Grok) — модели Grok по ключу API, оплата по факту использования. Обычно нужен прокси.",
    "xiaomi": "Xiaomi MiMo — китайские модели Xiaomi по ключу API, оплата по факту использования.",
    "zai": "Тоже напрямую из России, сильнее в длинных задачах, дороже DeepSeek.",
}

# Group-level Russian description override (spec A1), same "override on top
# of the per-variant default" shape as ``GROUP_DISPLAY_NAME_OVERRIDES`` right
# above — needed only for MULTI-variant groups where a single member's own
# ``description_ru`` wouldn't read as a coherent description of the group as
# a whole (a single-variant group's description is just that variant's own,
# no override needed — see ``wizard_provider_groups()``). "openai"'s text is
# VERBATIM from the approved mockup (screen 4's OpenAI card), matching
# ``RU_PROVIDER_DESCRIPTIONS``'s same rule for the five recommended groups.
GROUP_DESCRIPTION_OVERRIDES: dict[str, str] = {
    "kimi": (
        "Kimi (Moonshot) — китайская модель для длинного контекста и кода: "
        "глобальный тариф или региональный китайский эндпоинт, оплата по "
        "факту использования."
    ),
    "minimax": (
        "MiniMax — китайская модель по глобальному или китайскому ключу API, "
        "либо вход аккаунтом по коду устройства вместо покупки ключа."
    ),
    "openai": "Лучшие модели. Можно войти подпиской ChatGPT вместо покупки ключа.",
    "opencode": (
        "OpenCode — агрегатор моделей для кодовых задач: Zen — оплата по "
        "факту использования, Go — подписка с фиксированной платой."
    ),
    "qwen": (
        "Alibaba Qwen через DashScope — по ключу API (оплата по факту "
        "использования) или по подписке Coding Plan. Китайский сервис, "
        "доступен из России напрямую."
    ),
}

# Recommended groups + reason (spec A3), keyed by ``group_id`` (same domain
# as ``GROUP_DISPLAY_NAME_OVERRIDES``/``GROUP_DESCRIPTION_OVERRIDES`` above).
# The owner approved three grounds for recommending a provider: it works
# without a proxy, it has the best models, or it can be joined with an
# existing subscription instead of a bought key. Those grounds are not a
# 1:1 mapping onto tags — OpenAI satisfies two of them at once, and the
# subscription login is a property of a variant (Codex auth, MiniMax
# OAuth), not of a whole group, so no group is tagged with it. Each tag
# below states the single strongest reason to pick that group, and must
# never contradict the description printed under it.
# Order of THIS dict is irrelevant — ``wizard_provider_groups()``
# sorts recommended groups first, alphabetically within each partition; the
# client sees every group either way (owner ruling — no "hidden the rest"
# section), just recommended ones surfaced first.
RECOMMENDED_GROUPS: dict[str, str] = {
    "deepseek": "работает без прокси",
    "zai": "работает без прокси",
    "google": "работает без прокси",
    # OpenAI carries both owner-named reasons ("лучшие модели" and the
    # subscription login) — the stronger one is the label, the ChatGPT
    # login is already the second sentence of its own description.
    "openai": "лучшие модели",
    # NOT "вход по подписке": OpenRouter is an aggregator sold on breadth,
    # and its own description says so ("сотни моделей по одному ключу").
    # A tag contradicting the sentence right under it reads as noise.
    "openrouter": "много моделей одним ключом",
}

# Per-variant "способ подключения" label shown in the group's radio choice
# (spec: owner requirement 1's sub-picker). Keyed by provider ``name``
# (variant slug, not group_id) so it composes with DISPLAY_NAME_OVERRIDES
# without a second lookup table shape. A variant not listed here falls back
# to a generic label derived from its ``kind`` in ``_auth_label_for`` below
# — this dict only needs entries where the generic label would be
# ambiguous (multiple api_key variants in the same group).
AUTH_LABEL_OVERRIDES: dict[str, str] = {
    "openai-api": "По API-ключу",
    "openai-codex": "Вход по аккаунту (код устройства)",
    "minimax": "По API-ключу (глобальный)",
    "minimax-oauth": "Вход по аккаунту (код устройства)",
    "minimax-cn": "По API-ключу (Китай)",
    "kimi-coding": "По API-ключу (глобальный)",
    "kimi-coding-cn": "По API-ключу (Китай)",
    "alibaba": "По API-ключу (DashScope)",
    "alibaba-coding-plan": "По API-ключу (подписка Coding Plan)",
    # opencode-zen/opencode-go are BOTH api_key with no device_code sibling
    # to disambiguate against — without an explicit override here they'd
    # both fall through to the same generic "По API-ключу" label and render
    # as two indistinguishable radio options (review finding).
    "opencode-zen": "Zen — оплата по факту",
    "opencode-go": "Go — подписка",
}


def _auth_label_for(row: dict) -> str:
    override = AUTH_LABEL_OVERRIDES.get(row["name"])
    if override:
        return override
    if row["kind"] == "device_code":
        return "Вход по аккаунту (код устройства)"
    return "По API-ключу"


def _is_blocked_anthropic_device_code(name: str, kind: str) -> bool:
    """Structural guard (spec A2): a subscription/account-login variant of
    the ``anthropic`` GROUP must never reach the wizard, even though the
    group otherwise allows plain API-key auth.

    Anthropic blocks Russian ACCOUNTS, not raw API traffic — an
    ``openai-codex``-shaped device-code login would be worthless for an RU
    client (the account itself can't log in) while the existing api_key
    variant works fine through a proxy (spec §content-decisions, "Claude —
    только по ключу"). No such variant exists in today's live catalog
    (``anthropic``'s sole member IS ``api_key``) — this is a forward-guard
    against a future upstream catalog update, not a name-based blocklist:
    the rule is "the provider's own GROUP resolves to anthropic AND its
    kind is device_code", not "the provider's name is literally
    'anthropic'" — so a hypothetical differently-named future Anthropic
    subscription variant explicitly grouped under "anthropic" in
    ``hermes_cli.models.PROVIDER_GROUPS`` would be caught the same way.
    Exercised in tests via a synthetic ``DEVICE_CODE_PROVIDERS`` entry
    (mutation), since the live catalog has nothing to exercise this branch
    with.
    """
    if kind != "device_code":
        return False
    return (provider_group_for_slug(name) or name) == "anthropic"


def wizard_providers() -> list[dict]:
    """Return one row per provider that the wizard form can render.

    Sorted by ``display_name`` (case-insensitive), with ``custom`` always
    last regardless of its display name.
    """
    rows: list[dict] = []
    for p in providers.list_providers():
        if p.name in EXCLUDED_PROVIDERS:
            continue
        kind = "device_code" if p.name in DEVICE_CODE_PROVIDERS else "api_key"
        if _is_blocked_anthropic_device_code(p.name, kind):
            continue
        rows.append(
            {
                "name": p.name,
                "display_name": DISPLAY_NAME_OVERRIDES.get(p.name) or p.display_name or p.name,
                "description": p.description or "",
                # Spec A1: русское описание вместо английского оригинала
                # апстрима — см. RU_PROVIDER_DESCRIPTIONS's own docstring
                # for the defect this closes. Missing translation -> "",
                # never the English ``description`` above.
                "description_ru": RU_PROVIDER_DESCRIPTIONS.get(p.name, ""),
                "signup_url": p.signup_url or "",
                "kind": kind,
                "env_var": (p.env_vars[0] if p.env_vars else None),
                "base_url": p.base_url or "",
                "fallback_models": list(p.fallback_models or ()),
            }
        )
    rows.sort(key=lambda r: (r["name"] == "custom", r["display_name"].lower()))
    return rows


def wizard_provider_groups() -> list[dict]:
    """Fold ``wizard_providers()`` rows into one row per upstream provider
    group (spec: owner requirement 1 — "Есть провайдер OpenAI — ОДИН").

    Uses ``hermes_cli.models.PROVIDER_GROUPS`` — the same grouping the CLI
    ``hermes model`` picker and the Telegram ``/model`` keyboard already
    use — as the single source of truth for which provider slugs are one
    vendor's several auth methods. This is a NEW function, not a rewrite of
    ``wizard_providers()``: ``app.py``'s submission validation
    (``_legal_provider_names``, ``_expected_provider_env_var``) and every
    existing test key off the flat per-variant shape, so the flat function
    stays exactly as it is and this one is an additional view over the same
    data. The wire payload the client submits is unchanged either way —
    ``provider.name`` is always a VARIANT slug (``openai-api`` /
    ``openai-codex``), never a ``group_id``.

    Each returned row::

        {"group_id": <gid or ungrouped slug>,
         "display_name": <group label, GROUP_DISPLAY_NAME_OVERRIDES-aware>,
         "description_ru": <group description, GROUP_DESCRIPTION_OVERRIDES-aware>,
         "recommended": <bool>, "recommended_reason": <str, "" when not recommended>,
         "variants": [<wizard_providers() row + "auth_label">, ...]}

    Rules (mirrors ``hermes_cli.models.group_providers()``'s own contract,
    restricted to what THIS catalog actually renders):
      * A group's ``variants`` list only contains members that survived
        ``wizard_providers()`` (i.e. are not in ``EXCLUDED_PROVIDERS``) —
        member order follows ``PROVIDER_GROUPS``' own declared order,
        restricted to the survivors.
      * A group reduced to zero surviving members is dropped entirely
        (e.g. ``copilot``: both ``copilot`` and ``copilot-acp`` are
        excluded — see ``EXCLUDED_PROVIDERS``).
      * A group reduced to exactly one surviving member still renders as
        a one-variant row (no pointless single-option radio the caller
        would have to special-case) — the row's ``display_name`` AND
        ``description_ru`` are that variant's own, not a group-level
        override, matching the "ungrouped" case below (single-variant
        groups have no need for ``GROUP_DESCRIPTION_OVERRIDES`` — see that
        dict's own docstring).
      * A provider not in any ``PROVIDER_GROUPS`` entry renders as its own
        one-variant group, ``group_id`` = its provider ``name``.
      * Spec A3: ``recommended``/``recommended_reason`` come from
        ``RECOMMENDED_GROUPS`` by ``group_id`` — every group not in that
        dict is ``recommended: False``, ``recommended_reason: ""``. The
        client still receives EVERY group either way (owner ruling: no
        "recommended vs. the rest" section split) — only the ORDER changes
        (see the final sort below).
      * Rows are sorted with recommended groups first, alphabetically by
        ``display_name`` (case-insensitive) within each partition — same
        ordering rule as ``wizard_providers()`` otherwise used, minus the
        ``custom`` special case (``custom`` is excluded from the catalog
        entirely).
    """
    rows = wizard_providers()
    rows_by_name = {row["name"]: row for row in rows}
    groups: list[dict] = []
    emitted_group_ids: set[str] = set()
    handled_names: set[str] = set()

    for row in rows:
        name = row["name"]
        if name in handled_names:
            continue
        gid = provider_group_for_slug(name)
        if not gid:
            handled_names.add(name)
            groups.append(
                {
                    "group_id": name,
                    "display_name": row["display_name"],
                    "description_ru": row["description_ru"],
                    "recommended": name in RECOMMENDED_GROUPS,
                    "recommended_reason": RECOMMENDED_GROUPS.get(name, ""),
                    "variants": [{**row, "auth_label": _auth_label_for(row)}],
                }
            )
            continue
        if gid in emitted_group_ids:
            continue
        emitted_group_ids.add(gid)
        label, _desc, members = PROVIDER_GROUPS[gid]
        variants = []
        for member in members:
            member_row = rows_by_name.get(member)
            if member_row is None:
                continue  # excluded, or not present in this catalog build
            handled_names.add(member)
            variants.append({**member_row, "auth_label": _auth_label_for(member_row)})
        if not variants:
            continue
        if len(variants) == 1:
            groups.append(
                {
                    "group_id": gid,
                    "display_name": variants[0]["display_name"],
                    "description_ru": variants[0]["description_ru"],
                    "recommended": gid in RECOMMENDED_GROUPS,
                    "recommended_reason": RECOMMENDED_GROUPS.get(gid, ""),
                    "variants": variants,
                }
            )
        else:
            groups.append(
                {
                    "group_id": gid,
                    "display_name": GROUP_DISPLAY_NAME_OVERRIDES.get(gid) or label,
                    "description_ru": GROUP_DESCRIPTION_OVERRIDES.get(gid) or variants[0]["description_ru"],
                    "recommended": gid in RECOMMENDED_GROUPS,
                    "recommended_reason": RECOMMENDED_GROUPS.get(gid, ""),
                    "variants": variants,
                }
            )

    # Spec A3: recommended groups first, alphabetical within each partition.
    # A plain `bool` sorts False < True, so `not recommended` puts every
    # recommended group ahead of every non-recommended one; the secondary
    # key keeps the same alphabetical order the wizard has always used
    # otherwise. The client still receives every group either way (owner
    # ruling — no hidden "rest" section, just a reordered list).
    groups.sort(key=lambda g: (not g["recommended"], g["display_name"].lower()))
    return groups


def _parse_model_ids(resp: httpx.Response) -> list[str]:
    """Extract model ids from an OpenAI-compatible ``/v1/models`` response.

    Duplicated (not imported) from ``hermes_cli.web_server._parse_model_ids``
    — this module never imports the dashboard (see the module docstring's
    own framing: the wizard is a standalone surface). Kept in sync by hand;
    ~15 lines, tolerant of the same two common shapes:
    ``{"data": [{"id": ...}]}`` (OpenAI / vLLM / llama.cpp) and a bare
    ``{"data": ["id", ...]}``. Returns ``[]`` on any parse/HTTP error.
    """
    try:
        if not resp.is_success:
            return []
        payload = resp.json()
    except Exception:
        return []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for item in data:
        if isinstance(item, dict):
            mid = str(item.get("id") or "").strip()
        else:
            mid = str(item or "").strip()
        if mid:
            ids.append(mid)
    return ids


def _fetch_live_models_via_proxy(profile, api_key: str, base_url: str | None, proxy: str) -> list[str]:
    """Own httpx GET against the provider's models endpoint, routed through
    ``proxy`` — ``ProviderProfile.fetch_models`` (``providers/base.py``) is
    ``urllib``-based and has no proxy support at all, and it is upstream
    code this wizard must not modify. Mirrors that function's own URL
    resolution (``models_url`` override, else ``base_url`` + ``/models``)
    and auth (Bearer + ``default_headers``) exactly — see its docstring —
    so the two code paths return the same shape for the same provider.
    """
    effective_base = base_url or profile.base_url
    url = (profile.models_url or "").strip()
    if not url:
        if not effective_base:
            return []
        url = effective_base.rstrip("/") + "/models"

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(profile.default_headers or {})

    try:
        with httpx.Client(timeout=httpx.Timeout(8.0), proxy=proxy) as client:
            resp = client.get(url, headers=headers)
        return _parse_model_ids(resp)
    except Exception:
        logger.debug("fetch_live_models(%s) via proxy failed", profile.name, exc_info=True)
        return []


def fetch_live_models(name: str, api_key: str, base_url: str | None, proxy: str | None = None) -> list[str]:
    """Probe the provider's live model list; ``[]`` on any failure.

    Wraps ``ProviderProfile.fetch_models`` (``providers/base.py:197``) so a
    network hiccup or an invalid key degrades to the caller falling back to
    ``fallback_models`` instead of raising into the wizard's request handler.

    ``proxy``, when non-empty, routes the probe through it instead — the
    upstream ``fetch_models`` has no proxy support (it is ``urllib``-based,
    and upstream code this wizard must not modify), which matters for a
    RU-hosted server where OpenAI/OpenRouter/Anthropic's own catalog
    endpoints aren't reachable directly. When ``proxy`` is empty the
    behavior is exactly the original, unmodified path.
    """
    try:
        profile = providers.get_provider_profile(name)
        if proxy:
            return _fetch_live_models_via_proxy(profile, api_key, base_url, proxy)
        models = profile.fetch_models(api_key=api_key or None, base_url=base_url or None)
        return list(models or [])
    except Exception:
        logger.debug("fetch_live_models(%s) failed", name, exc_info=True)
        return []
