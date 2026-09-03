"""Task 9e (``docs/product/plans/2026-09-01-client-command-surface.md``):
``/blueprint``'s 16-entry catalog is the most client-facing thing in the
whole command surface (an owner decision: it stays available to the client),
but it renders entirely in English. This localizes ``_fmt_catalog()`` and the
one place a ``BlueprintSlot``'s ``label``/``help`` reach the client raw (the
direct-fill shortcut's "missing value" message) via
``trix.blueprint.<key>.*`` catalog keys, WITHOUT editing the English string
literals on ``cron/blueprint_catalog.py::CATALOG`` (an upstream file) --
``t(f"trix.blueprint.{bp.key}.title", default=bp.title)`` keeps the English
original as the fallback so an untranslated future (17th, 18th, ...)
blueprint still renders real English instead of a raw key path, and a merge
from upstream never conflicts with our translation keys.

Because the keys are built with an f-string (the catalog is a loop, not one
call site per blueprint), ``tests/agent/test_i18n.py``'s literal-``t("trix...")``
scanner cannot see them -- it only greps for quoted string literals. This
file is therefore this task's OWN completeness check: every ``CATALOG`` key
must carry a real (non-fallback) Russian translation. See
``docs/product/plans/2026-09-01-client-command-surface.md`` Task 9e.
"""

from __future__ import annotations

import pytest

from agent import i18n
from cron.blueprint_catalog import CATALOG
from hermes_cli.blueprint_cmd import handle_blueprint_command


@pytest.fixture
def russian(monkeypatch):
    """Force the Russian catalog for the duration of the test, exactly like
    ``tests/hermes_cli/test_trix_menu.py``'s ``HERMES_LANGUAGE`` pin sample:
    ``tests/conftest.py`` pins ``HERMES_LANGUAGE=en`` for the whole suite, so
    a test asserting "the client sees Russian" must set it itself and reset
    the i18n cache in ``try/finally`` -- otherwise the assertion either never
    runs against Russian at all, or leaks the override into later tests."""
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        yield
    finally:
        i18n.reset_language_cache()


# ---------------------------------------------------------------------------
# Completeness invariant -- the piece test_i18n.py's literal scanner can't
# see because the keys are f-string-built. "Every CATALOG entry has a real
# Russian title/description" is a relationship between two data sources
# (CATALOG and locales/ru.yaml), not a snapshot of either one's current
# contents -- upstream adding a 17th blueprint makes this loop longer, not
# red, and a missing translation for any one of the 16 makes it red.
# ---------------------------------------------------------------------------


class TestCatalogTranslationCompleteness:
    def test_every_catalog_entry_has_a_real_russian_title_and_description(self, russian):
        untranslated = []
        for bp in CATALOG:
            ru_title = i18n.t(f"trix.blueprint.{bp.key}.title", lang="ru")
            ru_desc = i18n.t(f"trix.blueprint.{bp.key}.description", lang="ru")
            # A missing key falls back through en.yaml to the key path itself
            # (agent/i18n.py's contract) since we deliberately did not pass a
            # `default=` here -- so an untranslated entry shows up either as
            # the raw English (en.yaml mirrors the Python literal) or as the
            # dotted key path, never as a real Russian sentence.
            if ru_title == bp.title or ru_title.startswith("trix.blueprint."):
                untranslated.append(f"{bp.key}.title")
            if ru_desc == bp.description or ru_desc.startswith("trix.blueprint."):
                untranslated.append(f"{bp.key}.description")
        assert not untranslated, f"missing Russian translation for: {untranslated}"

    def test_an_untranslated_future_blueprint_still_renders_real_english(self):
        # The mechanism behind "апстрим добавит семнадцатый" (plan Task 9e):
        # a key with no catalog entry at all -- as a brand-new upstream
        # blueprint would have on day one -- must degrade to the `default=`
        # value, not a raw key path, so the client never sees internal
        # plumbing.
        rendered = i18n.t(
            "trix.blueprint.__not_a_real_blueprint__.title",
            lang="ru",
            default="Some Future Blueprint",
        )
        assert rendered == "Some Future Blueprint"


# ---------------------------------------------------------------------------
# Rendered output -- what the client in Telegram actually reads.
# ---------------------------------------------------------------------------


_ENGLISH_TITLES = {bp.key: bp.title for bp in CATALOG}
_ENGLISH_DESCRIPTIONS = {bp.key: bp.description for bp in CATALOG}


class TestRenderedCatalogIsRussian:
    def test_catalog_listing_uses_russian_title_and_description(self, russian):
        from hermes_cli.blueprint_cmd import _fmt_catalog

        text = _fmt_catalog()
        for bp in CATALOG:
            assert bp.key in text
            # The English original must NOT leak into the Russian rendering
            # -- that's the whole defect this task closes.
            assert _ENGLISH_TITLES[bp.key] not in text, (
                f"{bp.key}: English title leaked into the Russian catalog"
            )
            assert _ENGLISH_DESCRIPTIONS[bp.key] not in text, (
                f"{bp.key}: English description leaked into the Russian catalog"
            )
            ru_title = i18n.t(f"trix.blueprint.{bp.key}.title", lang="ru")
            assert ru_title in text

    def test_catalog_listing_header_and_tip_are_russian(self, russian):
        from hermes_cli.blueprint_cmd import _fmt_catalog

        text = _fmt_catalog()
        assert "Automation Blueprints" not in text
        assert "Tip:" not in text

    def test_english_default_catalog_is_the_original_upstream_wording(self):
        # HERMES_LANGUAGE=en is the suite-wide pin (tests/conftest.py) -- no
        # fixture needed. This is the other half of the "приём": the
        # upstream English literal must still render verbatim, since
        # cron/blueprint_catalog.py itself was never edited.
        from hermes_cli.blueprint_cmd import _fmt_catalog

        text = _fmt_catalog()
        assert "Automation Blueprints" in text
        assert "morning-brief" in text
        for bp in CATALOG:
            assert bp.title in text
            assert bp.description in text


# ---------------------------------------------------------------------------
# Slots: BlueprintSlot.label/.help reach the client raw in exactly one
# place -- the direct-fill shortcut's "missing value" message
# (`/blueprint <name> slot=val ...` with a value forced empty). Every real
# blueprint in the catalog has a default for every slot (verified below),
# so this path is a deliberately-forced edge case, not routine traffic --
# but it is the one place a slot's English label would otherwise leak
# straight to the client with zero agent involved to translate it.
# ---------------------------------------------------------------------------


class TestSlotLabelsReachTheClientTranslated:
    def test_no_catalog_slot_is_actually_optional_or_default_free(self):
        # Documents *why* this is a forced-edge-case test: fill_blueprint's
        # own defaulting means "missing required value" never fires from
        # routine `/blueprint <name> slot=val ...` traffic against the
        # current 16 blueprints -- every slot has a default and none are
        # optional. If a future blueprint changes that, this test is the
        # tripwire that says so.
        for bp in CATALOG:
            for s in bp.slots:
                assert not s.optional, f"{bp.key}.{s.name} is optional"
                assert s.default not in (None, ""), f"{bp.key}.{s.name} has no default"

    def test_forcing_an_empty_time_value_reports_translated_label_and_help(self, russian):
        res = handle_blueprint_command("morning-brief time=")
        assert res.agent_seed is None
        # "Во сколько?" -- trix.blueprint.morning-brief.slot.time.label
        assert "Во сколько?" in res.text
        # help text for the same slot
        assert "24-часовое" in res.text or "24-часов" in res.text
        assert "What time?" not in res.text
        assert "24h local time" not in res.text

    def test_forcing_an_empty_deliver_value_reports_translated_label(self, russian):
        res = handle_blueprint_command("price-watch deliver=")
        assert res.agent_seed is None
        assert "Куда доставить?" in res.text
        assert "Where to deliver?" not in res.text

    def test_missing_value_message_names_the_translated_title_and_replacement_command(self, russian):
        res = handle_blueprint_command("morning-brief time=")
        ru_title = i18n.t("trix.blueprint.morning-brief.title", lang="ru")
        assert ru_title in res.text
        assert "/blueprint morning-brief" in res.text

    def test_english_default_missing_value_message_is_still_the_original_shape(self):
        # HERMES_LANGUAGE=en (suite pin) -- the new pre-check must not change
        # English-language behavior for the (already-tested-elsewhere)
        # direct-fill shortcut.
        res = handle_blueprint_command("morning-brief time=")
        assert "What time?" in res.text
        assert "Can't set up 'Morning briefing'" in res.text
        assert "/blueprint morning-brief" in res.text
