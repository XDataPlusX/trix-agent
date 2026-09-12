"""Localizing an agent status line must not change whether the customer sees it.

The gateway decides which ``agent/`` status lines reach a messaging client by
matching their **English text** against
``gateway.run._TELEGRAM_NOISY_STATUS_RE``.  The string is written in one file
and matched in another, with nothing linking them -- so translating a
suppressed line silently *un*-suppresses it and the customer starts receiving
retry and compression chatter in chat.

Proven, not assumed::

    "⏳ Retrying in 5.0s (attempt 2/3)..."      -> suppressed
    "⏳ Повтор через 5.0 с (попытка 2/3)..."     -> DELIVERED

Hence the rule these tests enforce (spec Ruling 8): translate only what the
filter already lets through, and never let a translation start matching it.
The samples below are the live wordings from the emit sites in ``agent/``;
they are a contract between two files that never reference each other, not a
snapshot of copy.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from agent.conversation_compression import ROUTINE_COMPRESSION_STATUS_SAMPLES
from gateway.run import _TELEGRAM_NOISY_STATUS_RE

LOCALES_DIR = pathlib.Path(__file__).resolve().parents[2] / "locales"


# Status lines the gateway deliberately keeps out of the customer's chat.
# Translating any of these is the bug this module exists to catch.
# The compression half comes from the emit sites themselves
# (agent/conversation_compression.ROUTINE_COMPRESSION_STATUS_SAMPLES),
# not from copies typed in here. A hand-copied sample is a snapshot of what
# the string USED to be: reword the emit site and this file keeps testing the
# old text, green and meaningless. Two other test modules already read the
# same constant for the same reason.
#
# The retry/rate-limit half below has no such export yet — those lines are
# still inline f-strings in agent/conversation_loop.py — so they remain
# copies here, with the same weakness. The catalog guard at the bottom of
# this file is what actually closes that gap: a suppressed line can only be
# localized by adding it to the catalog, and that is now a failing test.
SUPPRESSED_SAMPLES = [
    *ROUTINE_COMPRESSION_STATUS_SAMPLES,
    "⏳ Retrying in 5.0s (attempt 2/3)...",
    "⚠️ Max retries (3) exhausted — trying fallback...",
    "⚠️ Session compressed 4 times — accuracy may degrade",
    "⏱️ Rate limited. Waiting 30s",
    "⚠ Skipping concurrent compression",
    "⚠ No auxiliary LLM provider configured",
]

# Status lines that are meant to reach the customer.  A translation that
# accidentally matched the filter would make these vanish instead.
DELIVERED_SAMPLES = [
    "⚠️ Rate limited — switching to fallback provider...",
    "⚠️ Billing or credits exhausted — switching to fallback provider...",
    "❌ Billing or credits exhausted — no credit remaining",
    "✓ Context compaction complete — continuing turn...",
    "⚠️ Provider unreachable — switching to fallback provider...",
    "⚠️ Empty/malformed response — switching to fallback...",
    # The ten agent/conversation_loop.py status/final-response lines this
    # doc's task localized under trix.agent.* (English defaults -- see
    # test_conversation_loop_l10n.py for the executed en/ru pairs at each
    # call site). None of these match _TELEGRAM_NOISY_STATUS_RE today; this
    # pins that so a future edit to the filter's alternation cannot start
    # swallowing them without a failing test here.
    "Nous Portal rate limit active — resets in 3h.",
    "No fallback provider available. Try again after the reset, or add a fallback provider in config.yaml.",
    "❌ Ollama runtime context is too small for Hermes tool use",
    "⚠️ Model declined to respond (safety refusal) — trying fallback...",
    "⚠️ The model declined to respond to this request (safety refusal).",
    "⚠️  Request payload too large (413) — compression attempt 1/3...",
    "↻ Empty response after tool calls — using earlier content as final answer",
    "⚠️ Model returned empty after tool calls — nudging to continue",
    "⚠️ Empty response from model — retrying (1/3) in 5s",
    "❌ Model returned no content after all retries and fallback attempts.",
    "❌ Model returned no content after all retries. No fallback providers configured.",
    # Their shipped Russian translations (locales/ru.yaml, trix.agent.*) --
    # the actual text a Telegram customer reads. Guards the same hazard on
    # the delivered side: these must never accidentally start reading like
    # suppressed retry/compression chatter.
    "Провайдер модели ограничил частоту запросов — сброс через 3h.",
    "Резервный провайдер не настроен. Попробуйте снова после сброса ограничения, либо попросите того, кто администрирует эту машину, настроить резервного провайдера.",
    "❌ Контекст, выделенный локальной модели, слишком мал для использования инструментов.",
    "⚠️ Модель отказалась отвечать (сработал фильтр безопасности) — пробую другого провайдера…",
    "⚠️ Модель отказалась отвечать на этот запрос (сработал фильтр безопасности).",
    "⚠️ Запрос слишком большой для провайдера — сжимаю переписку (попытка 1/3)…",
    "↻ После использования инструментов модель не добавила ничего нового — использую более ранний ответ.",
    "⚠️ Модель ничего не ответила после использования инструментов — подталкиваю её продолжить.",
    "⚠️ Модель ничего не ответила — повтор (1/3) через 5 с.",
    "❌ Модель не вернула ответ, несмотря на все повторы и попытки переключиться на резервного провайдера.",
    "❌ Модель не вернула ответ, несмотря на все повторы. Резервный провайдер не настроен.",
    # The two agent/manual_compression_feedback.py direct /compress replies.
    "⏳ Compression already in progress for this session (holder: worker-1). Please wait for it to finish.",
    "⏳ Compression skipped: could not acquire this session's compression lock. Another compression may still be running, or the lock check failed — try again shortly.",
    "⏳ Сжатие переписки для этого сеанса уже выполняется (исполнитель: worker-1). Подождите, пока оно завершится.",
    "⏳ Сжатие пропущено: не удалось получить блокировку для этого сеанса. Возможно, сжатие уже выполняется — попробуйте ещё раз чуть позже.",
]


@pytest.mark.parametrize("text", SUPPRESSED_SAMPLES)
def test_noisy_status_stays_suppressed(text: str):
    assert _TELEGRAM_NOISY_STATUS_RE.search(text), (
        "This status is meant to stay out of the customer's chat. If you just "
        "translated it, revert: the filter matches English only, so a "
        "translated line becomes DELIVERED. See the spec, Ruling 8."
    )


@pytest.mark.parametrize("text", DELIVERED_SAMPLES)
def test_customer_facing_status_is_not_suppressed(text: str):
    assert not _TELEGRAM_NOISY_STATUS_RE.search(text), (
        "This status is meant to reach the customer. A translation that "
        "accidentally matches the noisy filter would make it vanish."
    )


def _walk(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk(value, child)
    elif isinstance(node, str):
        yield prefix, node


# Fork-owned strings that legitimately read like suppressed status text but
# are delivered on a path the filter never touches.  Every entry carries its
# reason, and adding one is meant to be a reviewed decision rather than a
# reflex -- the whole value of the guard is that mis-filing stays loud.
_FILTER_COLLISION_EXEMPTIONS = {
    # Delivered by `await _adapter.send(...)` in gateway/run.py, in the same
    # elif-chain as trix.errors.compression_aborted.  The regex clause that
    # matches it exists for a DIFFERENT emitter --
    # agent/conversation_compression.py, which goes out through
    # status_callback and IS meant to be suppressed.  This gateway copy is
    # the deliberately-delivered duplicate of that notice.
    "trix.errors.compression_aux_fallback",
}


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_shipped_strings_do_not_collide_with_the_status_filter(lang: str):
    """No fork-owned string may accidentally read as suppressed status text.

    This deliberately polices the WHOLE ``trix.*`` namespace even though the
    filter only runs inside ``_prepare_gateway_status_message`` -- i.e. only
    on the ``status_callback`` path -- because the guard is a tripwire on the
    NAMING decision, not a model of the filter's reach.

    That distinction was learned the hard way.  An earlier version narrowed
    the scope to ``trix.status.*`` on the (correct) reasoning that direct
    replies never meet the filter.  The effect was backwards: it fired only
    for an author who had already filed the key correctly, and stayed silent
    for the one who had not -- who is precisely the person the warning is
    for.  It was also vacuous, because no ``trix.status.*`` key existed.

    A legitimate collision on a direct-reply path therefore costs one
    reviewed line in ``_FILTER_COLLISION_EXEMPTIONS``, not a dropped check.
    """
    raw = yaml.safe_load(
        (LOCALES_DIR / f"{lang}.yaml").read_text(encoding="utf-8")
    ) or {}
    offenders = [
        key
        for key, value in _walk(raw.get("trix") or {}, "trix")
        if key not in _FILTER_COLLISION_EXEMPTIONS
        and _TELEGRAM_NOISY_STATUS_RE.search(value)
    ]
    assert not offenders, (
        f"{lang}.yaml: these fork-owned strings match the gateway noise "
        f"filter. If the string is emitted through status_callback it would "
        f"be silently swallowed and the customer would never see it -- do not "
        f"translate it. If it is a direct reply that merely reads like noise, "
        f"add it to _FILTER_COLLISION_EXEMPTIONS with a reason. "
        f"Offenders: {offenders}"
    )


def test_the_exemption_list_has_no_stale_entries():
    """An exemption naming a key that no longer ships is a comment pretending
    to be a rule: it would keep silently excusing that key if someone later
    re-added it for a different reason."""
    shipped = set()
    for lang in ("en", "ru"):
        raw = yaml.safe_load(
            (LOCALES_DIR / f"{lang}.yaml").read_text(encoding="utf-8")
        ) or {}
        shipped.update(key for key, _ in _walk(raw.get("trix") or {}, "trix"))
    stale = _FILTER_COLLISION_EXEMPTIONS - shipped
    assert not stale, f"exemptions naming keys that no longer ship: {stale}"


def test_the_filter_is_english_only_and_that_is_the_hazard():
    """Pins the asymmetry that Ruling 8 exists to manage.

    Not a snapshot of copy: it asserts the *relationship* between the filter
    and a translated string, which is precisely what a future maintainer
    would otherwise have to rediscover by shipping the bug.
    """
    english = "⏳ Retrying in 5.0s (attempt 2/3)..."
    russian = "⏳ Повтор через 5.0 с (попытка 2/3)..."

    assert _TELEGRAM_NOISY_STATUS_RE.search(english), (
        "baseline broken: the English retry notice is no longer suppressed"
    )
    assert not _TELEGRAM_NOISY_STATUS_RE.search(russian), (
        "If this ever passes, the filter has become language-aware and "
        "Ruling 8 can be relaxed -- until then, do not translate a "
        "suppressed status line."
    )


# ---------------------------------------------------------------------------
# The guard the hand-copied lists above cannot be.
#
# Every list in this file is a copy of text that lives somewhere else, so each
# one goes stale the moment the original is reworded — and a stale copy still
# passes. This check has no copy in it: it asks the live catalog and the live
# regex whether they contradict each other, so it keeps working no matter how
# either side is edited.
#
# Both directions are real, and both have already happened here:
#
#   * localizing a SUPPRESSED line un-suppresses it. The filter matches
#     English; a Russian translation does not match, so chatter the gateway
#     deliberately hides starts arriving in the customer's chat. The only way
#     to localize a line is to put it in the catalog — so a catalog entry that
#     matches the filter IS that mistake, caught at the moment it is made.
#
#   * rewording a DELIVERED line can make it start matching. Found by this
#     very check on 2026-09-01: `trix.errors.compression_aux_fallback` was
#     reworded to "The auxiliary compression model failed", which matches the
#     filter's `auxiliary\s+.+\s+failed` clause. The English message went
#     silent while the Russian one kept arriving — the same event behaving
#     differently in two languages, and nothing else in the suite noticed.
# ---------------------------------------------------------------------------


def _flatten(d, prefix=""):
    flat = {}
    for k, v in (d or {}).items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            flat.update(_flatten(v, key))
        else:
            flat[key] = v
    return flat


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_no_localized_string_collides_with_the_noise_filter(lang):
    with (LOCALES_DIR / f"{lang}.yaml").open("r", encoding="utf-8") as fh:
        catalog = _flatten(yaml.safe_load(fh))

    collisions = []
    for key, value in catalog.items():
        if not key.startswith("trix.") or not isinstance(value, str):
            continue
        match = _TELEGRAM_NOISY_STATUS_RE.search(value)
        if match:
            collisions.append(f"{key}\n      matched {match.group(0)!r}\n      in {value!r}")

    assert not collisions, (
        f"{lang}.yaml entries the gateway's noise filter would swallow:\n    "
        + "\n    ".join(collisions)
        + "\n\n  Either this line is one the gateway deliberately hides — in which "
        "case it must not be localized at all, because a translation stops "
        "matching the filter and the customer starts receiving it — or the "
        "wording drifted into matching by accident and must be reworded."
    )
