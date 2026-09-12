import re
from decimal import Decimal

import pytest

from agent.models_dev import ModelInfo
from agent.usage_pricing import PricingEntry
from hermes_cli.model_cost_guard import expensive_model_warning


def test_no_warning_when_known_prices_are_at_threshold():
    info = ModelInfo(
        id="edge/model",
        name="edge/model",
        family="",
        provider_id="test",
        cost_input=20.0,
        cost_output=100.0,
    )

    assert expensive_model_warning("edge/model", provider="test", model_info=info) is None






def test_openai_gpt55_pro_warns_for_nous_portal_pricing(monkeypatch):
    monkeypatch.setattr("agent.models_dev.get_model_info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "agent.usage_pricing.fetch_endpoint_model_metadata",
        lambda base_url, api_key="": {
            "openai/gpt-5.5-pro": {
                "pricing": {
                    "prompt": "0.000025",
                    "completion": "0.000125",
                }
            }
        },
    )

    warning = expensive_model_warning("openai/gpt-5.5-pro", provider="nous")

    assert warning is not None
    assert warning.input_cost_per_million == Decimal("25.000000")
    assert warning.output_cost_per_million == Decimal("125.000000")
    assert "did you mean to select openai/gpt-5.5?" in warning.message


# ---------------------------------------------------------------------------
# Task 5: the warning *body* (banner, cost lines, threshold sentence,
# pricing-source line, gpt-5.5 nudge, confirm hint) goes through ``t()`` with
# an English ``default=`` (Ruling R4) -- ``tests/conftest.py`` pins
# ``HERMES_LANGUAGE=en`` for the suite, so each test below sets the language
# itself and resets the process-wide cache in ``try/finally`` (pattern from
# ``tests/hermes_cli/test_trix_menu.py::TestDebugDescriptionMentionsLogs``).
# ---------------------------------------------------------------------------


def _expensive_pro_info() -> ModelInfo:
    return ModelInfo(
        id="openai/gpt-5.5-pro",
        name="openai/gpt-5.5-pro",
        family="",
        provider_id="test",
        cost_input=25.0,
        cost_output=125.0,
    )


def _with_language(monkeypatch, lang: str):
    from agent import i18n

    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    i18n.reset_language_cache()
    return i18n


def test_ru_warning_body_has_no_stray_english_words(monkeypatch):
    """R7 invariant: with the language switched to ``ru``, nothing in the
    body reads as an English word except the data Task 5 explicitly
    exempts -- the model id, the pricing-source name, and money amounts.
    Not a snapshot of the ru wording, which is free to evolve.
    """
    i18n = _with_language(monkeypatch, "ru")
    try:
        warning = expensive_model_warning(
            "openai/gpt-5.5-pro", provider="test", model_info=_expensive_pro_info()
        )
    finally:
        i18n.reset_language_cache()

    assert warning is not None
    message = warning.message

    # Strip the data Task 5 exempts before judging the rest of the body:
    # money amounts ("$25.00/M"), the pricing-source name, and any
    # slash-separated model identifier (covers both the model actually
    # picked, "openai/gpt-5.5-pro", and the different model named by the
    # gpt-5.5 nudge, "openai/gpt-5.5").
    scrubbed = re.sub(r"\$\d+(?:\.\d+)?/M", "", message)
    scrubbed = scrubbed.replace(warning.source, "")
    scrubbed = re.sub(r"[\w][\w.\-]*/[\w][\w.\-]*", "", scrubbed)

    words = re.findall(r"[A-Za-z]+", scrubbed)
    assert not words, f"ru warning body still has English words {words} in: {message!r}"


def test_en_warning_body_matches_todays_english_text(monkeypatch):
    """With ``HERMES_LANGUAGE=en`` the body is exactly today's English --
    the ``default=`` fallback (Ruling R4) must keep this path unchanged.
    """
    i18n = _with_language(monkeypatch, "en")
    try:
        warning = expensive_model_warning(
            "openai/gpt-5.5-pro", provider="test", model_info=_expensive_pro_info()
        )
    finally:
        i18n.reset_language_cache()

    assert warning is not None
    message = warning.message
    assert message.startswith("!!! EXPENSIVE MODEL WARNING !!!")
    assert "Input tokens: $25.00/M" in message
    assert "Output tokens: $125.00/M" in message
    assert (
        "Threshold: more than $20/M input tokens or more than "
        "$100/M output tokens." in message
    )
    assert "Pricing source: models.dev." in message
    assert "did you mean to select openai/gpt-5.5?" in message
    assert "Confirm only if you intend to use this model." in message


def test_ru_unknown_pricing_side_uses_translated_word(monkeypatch):
    """``_format_money(None)``'s ``"unknown"`` is the word a client sees
    when one side's price is unknown -- it must be translated too, not
    left as the sole English word in an otherwise-Russian body.
    """
    monkeypatch.setattr("agent.models_dev.get_model_info", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "agent.usage_pricing.get_pricing_entry",
        lambda *_a, **_kw: PricingEntry(
            input_cost_per_million=None,
            output_cost_per_million=Decimal("125"),
            source="test",
        ),
    )

    i18n = _with_language(monkeypatch, "ru")
    try:
        warning = expensive_model_warning("some/model", provider="test")
    finally:
        i18n.reset_language_cache()

    assert warning is not None
    assert "unknown" not in warning.message.lower()
