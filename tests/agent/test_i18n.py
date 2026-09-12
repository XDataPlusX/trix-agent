"""Tests for agent.i18n -- catalog parity, fallback, language resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent import i18n


LOCALES_DIR = Path(__file__).resolve().parents[2] / "locales"


def _load_raw(lang: str) -> dict:
    with (LOCALES_DIR / f"{lang}.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _flatten(d, prefix="") -> dict:
    flat = {}
    for k, v in (d or {}).items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            flat.update(_flatten(v, key))
        else:
            flat[key] = v
    return flat


# ---------------------------------------------------------------------------
# Catalog completeness -- this is the key invariant test.  If someone adds a
# new key to en.yaml they MUST add it to every other locale, else runtime
# falls back to English for those users and defeats the feature.
# ---------------------------------------------------------------------------



# Namespaces whose source of truth is NOT en.yaml, and which therefore do
# not participate in the strict two-way parity check:
#   commands.*  -- backed by COMMAND_REGISTRY; any locale may omit it and
#                  fall back to the registry's canonical English.
#   trix.*      -- introduced by this fork; required in en + ru, optional
#                  in the 15 locales this product does not serve.
# Everything upstream owns (gateway.*, approval.*) keeps the original
# strict contract, so merging upstream still catches a forgotten key.
_REGISTRY_BACKED_NAMESPACES = ("commands.",)
_FORK_OWNED_NAMESPACES = ("trix.",)


def _strict_parity_keys(flat: dict) -> set:
    exempt = _REGISTRY_BACKED_NAMESPACES + _FORK_OWNED_NAMESPACES
    return {k for k in flat if not k.startswith(exempt)}


@pytest.mark.parametrize("lang", [l for l in i18n.SUPPORTED_LANGUAGES if l != "en"])
def test_catalog_keys_match_english(lang: str):
    """Every non-English catalog must have the same upstream-owned key set
    as English, in both directions."""
    en_keys = _strict_parity_keys(_flatten(_load_raw("en")))
    lang_keys = _strict_parity_keys(_flatten(_load_raw(lang)))
    missing = en_keys - lang_keys
    extra = lang_keys - en_keys
    assert not missing, f"{lang}.yaml missing keys: {sorted(missing)}"
    assert not extra, f"{lang}.yaml has keys not in en.yaml: {sorted(extra)}"


def test_no_duplicate_keys_in_any_catalog():
    """PyYAML keeps only the LAST of duplicate keys, so a second ``trix:``
    block appended by a later task would erase an earlier task's
    translations with no error and no failing test -- the strings would
    quietly render English again.  Parse with a loader that refuses
    duplicates instead."""

    class _NoDuplicates(yaml.SafeLoader):
        pass

    def _construct(loader, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            assert key not in mapping, (
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            )
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    _NoDuplicates.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct
    )

    for lang in i18n.SUPPORTED_LANGUAGES:
        path = LOCALES_DIR / f"{lang}.yaml"
        yaml.load(path.read_text(encoding="utf-8"), _NoDuplicates)


def test_fork_owned_keys_exist_in_english_and_russian():
    """trix.* is exempt from the 17-language parity rule but NOT from
    parity between the two languages this product actually ships."""
    en = {k for k in _flatten(_load_raw("en")) if k.startswith("trix.")}
    ru = {k for k in _flatten(_load_raw("ru")) if k.startswith("trix.")}
    assert en - ru == set(), f"ru.yaml missing fork keys: {sorted(en - ru)}"
    assert ru - en == set(), f"en.yaml missing fork keys: {sorted(ru - en)}"


def test_exempt_namespace_values_carry_no_placeholders():
    """A key absent from en.yaml falls back to text this module never sees
    (a registry description). Interpolation could not work, so a
    placeholder there is always a bug."""
    import re
    placeholder_re = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
    en_flat = _flatten(_load_raw("en"))
    for lang in i18n.SUPPORTED_LANGUAGES:
        flat = _flatten(_load_raw(lang))
        for key, value in flat.items():
            if not key.startswith(_REGISTRY_BACKED_NAMESPACES):
                continue
            if key in en_flat:
                continue
            assert not placeholder_re.findall(value), f"{lang}.yaml {key}"


@pytest.mark.parametrize("lang", list(i18n.SUPPORTED_LANGUAGES))
def test_catalog_placeholders_match_english(lang: str):
    """Every translated value must use the same {placeholder} tokens as English.

    A mistranslated placeholder (e.g. ``{description}`` typoed as ``{descricao}``)
    would either raise KeyError at runtime or silently drop the interpolated
    value.  Pin parity at the test layer.
    """
    import re
    placeholder_re = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
    en_flat = _flatten(_load_raw("en"))
    lang_flat = _flatten(_load_raw(lang))
    for key, en_value in en_flat.items():
        if key.startswith(_REGISTRY_BACKED_NAMESPACES + _FORK_OWNED_NAMESPACES) and key not in lang_flat:
            # This locale legitimately doesn't carry the key at all (registry
            # fallback / fork namespace not shipped to this locale) -- that's
            # not a placeholder mismatch, it's the exemption working.
            continue
        en_placeholders = set(placeholder_re.findall(en_value))
        lang_value = lang_flat.get(key, "")
        lang_placeholders = set(placeholder_re.findall(lang_value))
        assert en_placeholders == lang_placeholders, (
            f"{lang}.yaml key={key!r}: placeholders {lang_placeholders} "
            f"don't match English {en_placeholders}"
        )


# ---------------------------------------------------------------------------
# Language resolution
# ---------------------------------------------------------------------------











def test_default_when_nothing_set(monkeypatch):
    """With no env var and no config override, falls back to English."""
    monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
    # Force config lookup to return None -- patch the cached reader.
    i18n.reset_language_cache()
    monkeypatch.setattr(i18n, "_config_language_cached", lambda: None)
    assert i18n.get_language() == "en"


# ---------------------------------------------------------------------------
# t() semantics
# ---------------------------------------------------------------------------







def test_t_returns_default_when_key_missing_everywhere(monkeypatch):
    """A caller that owns its own English source of truth passes it as
    `default` and must never see a raw key path leak to the user."""
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    assert i18n.t("commands.__nope__.description", default="Fallback text") == "Fallback text"


def test_t_still_returns_key_when_no_default_given(monkeypatch):
    """Existing behaviour is unchanged for every current call site."""
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    assert i18n.t("commands.__nope__.description") == "commands.__nope__.description"


def test_t_prefers_catalog_over_default(monkeypatch):
    """`default` is a fallback, not an override: supplying one must not
    perturb a key that already resolves in the active catalog. Pinned as an
    equality against the un-defaulted call rather than a specific string, so
    this doesn't freeze the Russian copy as a change-detector."""
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    key = "gateway.no_active_goal"
    assert i18n.t(key, default="IGNORED") == i18n.t(key)


def test_t_default_is_formatted_like_a_catalog_value(monkeypatch):
    """A default carrying placeholders must interpolate the same way a
    catalog value does, or callers get raw braces on the fallback path."""
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    assert i18n.t("trix.__nope__", default="left {n} items", n=3) == "left 3 items"


def test_t_english_catalog_beats_default_for_the_middle_tier(monkeypatch):
    """Precedence is target catalog -> English catalog -> default -> key.
    Pin the middle tier explicitly: a key present in English but absent from
    the target language must resolve to the English value, never to a
    supplied `default`. This namespace is dormant today (`commands.*` is
    deliberately absent from en.yaml per spec 5.2, so target-miss +
    english-hit + default doesn't occur naturally yet) but becomes
    load-bearing the moment English command descriptions are added -- so it
    is pinned here via direct catalog injection rather than relying on
    locale file contents.
    """
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    key = "__test_only__.precedence_probe"
    try:
        i18n._catalog_cache["en"] = {key: "English catalog value"}
        i18n._catalog_cache["ru"] = {}
        assert i18n.t(key, default="IGNORED default") == "English catalog value"
    finally:
        i18n.reset_language_cache()


def test_t_honors_an_explicitly_empty_default(monkeypatch):
    """`default=""` is a plausible real value (e.g. a command with an empty
    description) and must be returned as-is, not treated as falsy and
    discarded in favor of the bare key path."""
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    assert i18n.t("commands.__nope__.description", default="") == ""


def test_t_positional_argument_order_is_key_lang_default(monkeypatch):
    """The plan pins `default` as the third positional parameter, after
    `lang`. Call positionally to catch an accidental reordering of the
    signature that every keyword-argument call site would silently survive."""
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    assert i18n.t("nope.nope", None, "D") == "D"


def test_t_missing_key_in_non_english_falls_back_to_english(tmp_path, monkeypatch):
    """If a key exists in English but not in the target locale, fall back."""
    # Stand up a fake incomplete locale under a temp locales dir.
    fake_locales = tmp_path / "locales"
    fake_locales.mkdir()
    (fake_locales / "en.yaml").write_text("foo: English Foo\n", encoding="utf-8")
    (fake_locales / "zh.yaml").write_text("# intentionally empty\n", encoding="utf-8")
    monkeypatch.setattr(i18n, "_locales_dir", lambda: fake_locales)
    i18n.reset_language_cache()
    try:
        assert i18n.t("foo", lang="zh") == "English Foo"
    finally:
        # Clear the cache on teardown so subsequent tests don't see the
        # fake "foo: English Foo" catalog instead of the real locales/*.yaml.
        i18n.reset_language_cache()




# ---------------------------------------------------------------------------
# _locales_dir resolution ladder -- regression for #23943 / #27632 / #35374.
# Sealed installs (Nix store venv, pip wheel) have no source tree next to
# agent/, so _locales_dir must resolve via env override or the data scheme.
# ---------------------------------------------------------------------------



def test_locales_dir_env_override_ignored_when_missing(tmp_path, monkeypatch):
    """A bogus HERMES_BUNDLED_LOCALES falls through to source/wheel resolution
    instead of returning a path that doesn't exist."""
    monkeypatch.setenv("HERMES_BUNDLED_LOCALES", str(tmp_path / "does-not-exist"))
    result = i18n._locales_dir()
    assert result != tmp_path / "does-not-exist"
    # In a source checkout this is the repo-root locales dir.
    assert result.name == "locales"


# ---------------------------------------------------------------------------
# display.language default -- Trix Agent ships to a Russian-speaking client,
# so the shipped default.yaml value must resolve to a real, complete catalog.
# ---------------------------------------------------------------------------


def test_default_display_language_is_supported_and_has_a_catalog():
    """Invariant, not a snapshot: whatever DEFAULT_CONFIG["display"]["language"]
    is, it must be a recognized language with a shipped catalog file. Guards
    against a future default change silently degrading to English (unknown
    values fall back to "en" instead of raising) -- this makes that failure
    loud instead of silent.
    """
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    default_lang = DEFAULT_CONFIG["display"]["language"]
    assert default_lang in i18n.SUPPORTED_LANGUAGES
    assert (LOCALES_DIR / f"{default_lang}.yaml").is_file(), (
        f"SUPPORTED_LANGUAGES lists {default_lang!r} but locales/{default_lang}.yaml is missing"
    )


def test_default_display_language_is_russian():
    """Trix Agent's customer is Russian-speaking; the product must not answer
    in English by default regardless of which language the user's first
    message happens to arrive in."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["display"]["language"] == "ru"


# ---------------------------------------------------------------------------
# commands.* coverage -- the Russian catalog must describe every command the
# gateway actually shows a Telegram customer.
# ---------------------------------------------------------------------------


def _gateway_visible_commands():
    from hermes_cli.commands import (
        COMMAND_REGISTRY, _is_gateway_available, _resolve_config_gates,
    )
    overrides = _resolve_config_gates()
    return [c for c in COMMAND_REGISTRY if _is_gateway_available(c, overrides)]


def test_every_gateway_command_has_a_russian_description():
    """Coverage contract between two data sources: the command registry and
    the Russian catalog. A new upstream gateway command turning this red is
    the intended signal that untranslated surface appeared."""
    ru = _flatten(_load_raw("ru"))
    missing = [
        c.name for c in _gateway_visible_commands()
        if f"commands.{c.name}.description" not in ru
    ]
    assert not missing, f"ru.yaml has no description for: {sorted(missing)}"


def test_no_orphan_command_keys_in_any_catalog():
    """Every commands.* key must name a real command. Catches typos and keys
    left behind when upstream renames or drops a command."""
    from hermes_cli.commands import COMMAND_REGISTRY
    known = {c.name for c in COMMAND_REGISTRY}
    for lang in i18n.SUPPORTED_LANGUAGES:
        flat = _flatten(_load_raw(lang))
        for key in flat:
            if not key.startswith("commands."):
                continue
            name = key.split(".")[1]
            assert name in known, f"{lang}.yaml: commands.{name}.* names no command"


def test_russian_command_descriptions_fit_telegram_limit():
    """Telegram rejects a BotCommand description over 256 characters; a
    rejected setMyCommands call leaves the client with a stale menu."""
    ru = _flatten(_load_raw("ru"))
    for key, value in ru.items():
        if key.startswith("commands.") and key.endswith(".description"):
            assert 1 <= len(value) <= 256, f"{key}: {len(value)} chars"


def test_russian_command_descriptions_are_single_line():
    """Newlines in a BotCommand description break the Telegram menu."""
    ru = _flatten(_load_raw("ru"))
    for key, value in ru.items():
        if key.startswith("commands.") and key.endswith(".description"):
            assert "\n" not in value, key


def test_hints_with_literal_subcommands_are_not_translated():
    """A hint listing literal subcommands (`[on|off|status]`) must stay
    English: translating it would teach the client to type a Russian word
    the parser does not accept."""
    ru = _flatten(_load_raw("ru"))
    for cmd in _gateway_visible_commands():
        if cmd.args_hint and "|" in cmd.args_hint:
            assert f"commands.{cmd.name}.args_hint" not in ru, cmd.name


def test_translated_hints_keep_their_bracket_shape():
    """`<x>` means required and `[x]` optional; the shape is the contract,
    the word inside it is the translatable part."""
    ru = _flatten(_load_raw("ru"))
    for cmd in _gateway_visible_commands():
        key = f"commands.{cmd.name}.args_hint"
        if key not in ru:
            continue
        translated = ru[key]
        assert translated.count("<") == cmd.args_hint.count("<"), cmd.name
        assert translated.count("[") == cmd.args_hint.count("["), cmd.name
        assert "|" not in translated, cmd.name


def test_stt_language_default_matches_the_ui_language():
    """Invariant between two defaults, not a snapshot of either: the
    product ships to a Russian-speaking customer, so the speech-to-text
    hint must not tell every provider the audio is English."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    ui_lang = DEFAULT_CONFIG["display"]["language"]
    stt_lang = DEFAULT_CONFIG["stt"]["language"]
    assert stt_lang == ui_lang, (
        f"display.language={ui_lang!r} but stt.language={stt_lang!r}: "
        "voice messages would be transcribed with the wrong language hint"
    )

def test_catalog_keys_are_all_strings():
    """YAML 1.1 resolves bare ``yes``/``no``/``on``/``off``/``true``/``false``
    to booleans, so a key spelled ``yes:`` silently becomes ``True`` and every
    ``t("....yes")`` lookup misses -- handing the customer a raw key path.
    ``/egress`` shipped exactly that: ``Включено: trix.egress.no``.

    Guards the cause rather than that one instance.
    """
    def walk(node, path=()):
        if isinstance(node, dict):
            for key, value in node.items():
                yield path + (key,)
                yield from walk(value, path + (key,))

    for lang in i18n.SUPPORTED_LANGUAGES:
        raw = _load_raw(lang) or {}
        for path in walk(raw):
            assert all(isinstance(seg, str) for seg in path), (
                f"{lang}.yaml: key {path!r} was coerced by YAML -- rename it "
                f"(bare yes/no/on/off/true/false parse as booleans)"
            )


def test_every_referenced_fork_key_exists_in_both_shipped_catalogs():
    """A ``t("trix....")`` call whose key is absent returns the key path, and
    the customer reads it in chat.  A contract between two sources -- the call
    sites and the catalogs -- not a snapshot of either.

    This reads source text, which is normally banned for behaviour tests, but
    the subject here *is* the code-to-data relationship and there is no runtime
    moment at which every ``t()`` call site executes.  Do not delete it as an
    antipattern.
    """
    import re

    root = LOCALES_DIR.parent
    pattern = re.compile(r"""t\(\s*["'](trix\.[a-z0-9_.]+)["']""")
    referenced: dict[str, str] = {}
    for directory in ("agent", "gateway", "hermes_cli", "tools"):
        for path in (root / directory).rglob("*.py"):
            for key in pattern.findall(path.read_text(encoding="utf-8")):
                referenced.setdefault(key, str(path.relative_to(root)))
    assert referenced, "found no trix.* references -- the scan is broken"

    def flat_keys(raw):
        out = set()
        def walk(node, prefix=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, f"{prefix}.{key}" if prefix else str(key))
            elif isinstance(node, str):
                out.add(prefix)
        walk(raw)
        return out

    for lang in ("en", "ru"):
        shipped = flat_keys(_load_raw(lang) or {})
        missing = {k: v for k, v in referenced.items() if k not in shipped}
        assert not missing, f"{lang}.yaml missing referenced keys: {missing}"


# ---------------------------------------------------------------------------
# Placeholder parity across languages.
#
# The suite runs with HERMES_LANGUAGE=en pinned (tests/conftest.py), because
# this fork's DEFAULT is Russian and every upstream test that asserts English
# copy would otherwise break the moment a string moves into the catalog. That
# pin buys stability at a price: a Russian string whose {placeholder} set has
# drifted from its English twin is never executed by the ordinary suite, and
# the drift surfaces as a KeyError on a customer's machine, mid-conversation,
# in the one language the product actually ships.
#
# This is the invariant that pays that price back. It is deliberately a
# relationship between two catalogs, not a snapshot of either: adding,
# renaming or retranslating a string is free, and only a genuine mismatch --
# the thing that crashes -- fails.
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = __import__("re").compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)[^{}]*\}")


def _placeholders(text: str) -> set:
    return set(_PLACEHOLDER_RE.findall(text)) if isinstance(text, str) else set()


def test_locale_placeholders_agree_across_languages():
    """Every translated string must accept exactly the arguments its English
    twin accepts.

    A `{name}` present in one language and absent from the other is a live
    crash: ``t()`` formats with the caller's kwargs, so an extra placeholder
    raises ``KeyError`` for users of that language only. A missing one is
    quieter but still wrong -- the value the caller passed silently vanishes
    from the sentence (a path, a count, a model name).
    """
    english = _flatten(_load_raw("en"))
    mismatches = []
    for lang_file in sorted(LOCALES_DIR.glob("*.yaml")):
        lang = lang_file.stem
        if lang == "en":
            continue
        for key, value in _flatten(_load_raw(lang)).items():
            if key not in english:
                continue
            want = _placeholders(english[key])
            got = _placeholders(value)
            if want != got:
                mismatches.append(
                    f"{lang}.yaml :: {key}\n"
                    f"    en has {sorted(want) or '[]'}, {lang} has {sorted(got) or '[]'}\n"
                    f"    extra in {lang}: {sorted(got - want) or '[]'} "
                    f"(these raise KeyError at runtime)\n"
                    f"    missing from {lang}: {sorted(want - got) or '[]'} "
                    f"(the caller's value silently disappears)"
                )
    assert not mismatches, (
        "Translated strings disagree with English on their format arguments:\n\n"
        + "\n\n".join(mismatches)
    )


# ---------------------------------------------------------------------------
# Two catalog-wide invariants for the fork's own strings.
#
# Both exist because of what `t()` does on a bad key or a bad kwarg: it does
# NOT raise. `agent/i18n.py` returns the UNFORMATTED template on KeyError, so
# a typo is not a crash a test would catch — it is a live sentence in the
# customer's chat reading "Full report saved to {path}". And a string that
# was never actually translated is not a failure either; it just quietly
# ships English to a customer who was sold a Russian product.
#
# Neither is caught by executing call sites one at a time, because the miss
# is always in the key nobody wrote a test for. These check every key there
# is, which is exactly the shape that scales with the catalog instead of with
# the test suite.
# ---------------------------------------------------------------------------

_PROSE_RE = __import__("re").compile(r"[A-Za-z]{4,}")
_CYRILLIC_RE = __import__("re").compile(r"[А-Яа-яЁё]")
_BRACES_RE = __import__("re").compile(r"\{[^{}]*\}")
_PLACEHOLDER_SPEC_RE = __import__("re").compile(
    r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)((?:\.[a-zA-Z0-9_]+|\[[^\]]*\])*)(?::([^{}]*))?\}"
)


def _placeholders_with_spec(text: str):
    """``[(name, format_spec), ...]`` — the spec decides the dummy's type."""
    return [(m.group(1), m.group(3) or "") for m in _PLACEHOLDER_SPEC_RE.finditer(text)]


def _fork_owned_keys() -> dict:
    return {
        k: v
        for k, v in _flatten(_load_raw("en")).items()
        if k.startswith(_FORK_OWNED_NAMESPACES) and isinstance(v, str)
    }


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_every_fork_string_renders_with_no_leftover_placeholder(lang):
    """No `{placeholder}` may survive rendering, in either language.

    ``t()`` swallows a ``KeyError`` and hands back the raw template, so a
    placeholder the call site does not supply — or one misspelled on one side
    of the catalog — reaches the customer verbatim: "Полный отчёт сохранён на
    сервере: {path}". That is not a crash anyone will see in a log; it is a
    sentence someone reads and cannot act on.

    Rendering here uses each string's OWN placeholder names, so this asserts
    the weaker, always-true half: given correct kwargs, the template is
    well-formed. The other half — that call sites pass those kwargs — belongs
    to the executed l10n suites, and this test is what makes their gaps
    survivable rather than silent.
    """
    catalog = _flatten(_load_raw(lang))
    broken = []
    for key in _fork_owned_keys():
        value = catalog.get(key)
        if not isinstance(value, str):
            continue
        if not value.strip():
            # An intentionally empty entry (e.g. ``trix.approval.timeout_note``
            # is "" in English and carries a sentence only in Russian). Nothing
            # to render, and nothing to get wrong.
            continue
        # Dummy values typed by the format spec, not a bare "X": a string fed
        # to ``{seconds:.1f}`` raises inside ``str.format``, ``t()`` swallows
        # it and returns the raw template, and the test would then blame the
        # catalog for the test's own wrong argument type.
        kwargs = {}
        for name, spec in _placeholders_with_spec(value):
            kwargs[name] = 1.0 if ("f" in spec or "e" in spec) else (
                1 if ("d" in spec or "," in spec) else "X"
            )
        try:
            rendered = i18n.t(key, lang=lang, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the failure IS the finding
            broken.append(f"{key}: raised {type(exc).__name__}: {exc}")
            continue
        if "{" in rendered or "}" in rendered:
            broken.append(f"{key}: unreplaced placeholder in {rendered!r}")
        if not rendered.strip():
            broken.append(f"{key}: rendered empty from a non-empty template")
    assert not broken, (
        f"{lang}.yaml strings that do not render cleanly:\n  " + "\n  ".join(broken)
    )


def test_fork_prose_is_actually_translated_into_russian():
    """A Russian entry carrying no Russian is an untranslated string shipped.

    The product is sold to a Russian-speaking customer; a `trix.*` key that
    exists in ``ru.yaml`` but still holds the English sentence passes the
    parity check above (the key IS present) and passes the placeholder check
    (the braces DO match) while shipping English to the one audience that
    cannot read it.

    Placeholders are stripped before looking for prose, so format-only lines
    (``"  - {name}: {token} ({hosts})"``) are correctly exempt without an
    allowlist to maintain — there is nothing in them to translate.
    """
    russian = _flatten(_load_raw("ru"))
    untranslated = []
    for key, english in _fork_owned_keys().items():
        value = russian.get(key)
        if not isinstance(value, str):
            continue
        if not _PROSE_RE.search(_BRACES_RE.sub(" ", english)):
            continue  # nothing to translate: emoji, punctuation, placeholders
        if not _CYRILLIC_RE.search(value):
            untranslated.append(f"{key}: {value!r}")
    assert not untranslated, (
        "ru.yaml entries that still hold untranslated English prose:\n  "
        + "\n  ".join(untranslated)
    )


# ---------------------------------------------------------------------------
# Литеральный "\n" вместо перевода строки.
#
# Найдено клиентом в живом Телеграме 2026-09-04: заголовок /reasoning
# приходил одной строкой с видимыми "\n" внутри. Причина — двойное
# экранирование в YAML: "...\\n\\n..." в двойных кавычках даёт обратный
# слэш и букву n, а не перевод строки. Английский был цел, а все
# пятнадцать переводов сломаны одинаково — то есть ошибка внесена в
# момент перевода этих двух ключей, а не накопилась.
#
# Проверка — инвариант, а не снимок: она не перечисляет ключи и не знает
# про /reasoning. Она говорит "если в английском тут перевод строки, то и
# в переводе обязан быть перевод строки, а не его изображение".
# ---------------------------------------------------------------------------


def _flat_catalog(data, prefix=""):
    for key, value in (data or {}).items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _flat_catalog(value, path)
        elif isinstance(value, str):
            yield path, value


def test_no_translation_shows_a_literal_backslash_n():
    import pathlib

    import yaml

    locales = pathlib.Path(__file__).resolve().parents[2] / "locales"
    english = dict(_flat_catalog(yaml.safe_load((locales / "en.yaml").read_text(encoding="utf-8"))))

    offenders = []
    for path in sorted(locales.glob("*.yaml")):
        catalog = dict(_flat_catalog(yaml.safe_load(path.read_text(encoding="utf-8"))))
        for key, value in catalog.items():
            if "\\n" not in value:
                continue
            offenders.append(
                f"{path.name} :: {key} — литеральный '\\n' в тексте"
                + (" (в английском там настоящий перевод строки)"
                   if "\n" in english.get(key, "") else "")
            )

    assert not offenders, (
        "Клиент увидит '\\n' как два символа посреди фразы:\n  "
        + "\n  ".join(offenders)
    )
