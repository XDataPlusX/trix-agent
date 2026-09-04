"""Сообщения клиенту об отказе провайдера — по-русски и с действием."""
import agent.i18n as i18n_mod
import pytest

from agent.error_classifier import FailoverReason
from hermes_cli.trix_provider_errors import (
    client_error_message,
    client_fallback_attempt_message,
    client_fallback_empty_response_switch_message,
    client_fallback_message,
    client_fallback_switch_attempt_message,
)


@pytest.fixture(autouse=True)
def _russian_language(monkeypatch):
    # t() resolves language from env > config.yaml > "en". Tests run against
    # a temp HERMES_HOME with no config.yaml, so without this the catalog
    # would resolve to "en" and the Russian-text assertions below would fail
    # for the wrong reason (missing language, not a module bug).
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")


class TestClientErrorMessage:
    def test_billing_names_money_not_a_status_code(self):
        msg = client_error_message(FailoverReason.billing, 402)
        assert "средства" in msg or "баланс" in msg.lower()
        assert not msg.lstrip().startswith("402")
        # No status code at all, in either direction -- a customer staring
        # at an empty balance doesn't need a number, and status_code=None is
        # reachable here (the classifier can reach `billing` from text
        # patterns alone, agent/error_classifier.py ~1574-1608), so the old
        # "(код {status})" template rendered a dangling empty "(код )".
        assert "402" not in msg
        assert "(код" not in msg.lower()
        assert "(code" not in msg.lower()

    def test_billing_has_no_code_placeholder_even_with_no_status(self):
        # Same assertion with status_code=None -- this is the exact input
        # that used to produce the empty "(код )" parenthesis.
        msg = client_error_message(FailoverReason.billing, None)
        assert "(код" not in msg.lower()
        assert "(code" not in msg.lower()

    def test_auth_points_at_the_wizard_not_at_a_config_file(self):
        msg = client_error_message(FailoverReason.auth, 401, is_auth=True)
        assert "ключ" in msg.lower()
        assert "мастер" in msg.lower()

    def test_auth_reuses_the_gateway_key_and_ignores_the_unused_status_kwarg(self):
        # trix.errors.provider.auth_failed is the SAME key
        # gateway/run.py:_gateway_provider_error_reply reads -- it carries no
        # {status} placeholder. We still pass status=... into t(); this must
        # not raise and must not leak a literal "{status}" into the text.
        from gateway.run import _gateway_provider_error_reply

        via_status_kwarg = client_error_message(FailoverReason.auth, 401, is_auth=True)
        via_no_status = _gateway_provider_error_reply("401 invalid api key")
        assert via_status_kwarg == via_no_status
        assert "{status}" not in via_status_kwarg

    def test_unknown_reason_still_says_something_useful(self):
        msg = client_error_message(None, 500)
        assert msg.strip()
        assert "провайдер" in msg.lower()

    @pytest.mark.parametrize(
        "reason,status,expect_marker",
        [
            (FailoverReason.billing, None, "out of funds"),
            (FailoverReason.auth, 401, "setup wizard"),
            (FailoverReason.content_policy_blocked, 400, "safety filter"),
            (FailoverReason.ssl_cert_verification, None, "certificate"),
            (None, 503, "isn't responding"),
        ],
    )
    def test_default_kicks_in_when_the_catalog_is_totally_unreadable(
        self, monkeypatch, reason, status, expect_marker
    ):
        # agent.i18n._load_catalog swallows any read/parse error and CACHES
        # AN EMPTY CATALOG for that language (agent/i18n.py:159-176) -- this
        # isn't just "someone broke the YAML in the repo", it's reachable by
        # a locale file simply failing to read on a client machine. Without
        # an explicit default= on every t() call, the module would hand the
        # customer the bare key path (e.g. "trix.errors.provider.billing")
        # instead of English text.
        monkeypatch.setattr(i18n_mod, "_load_catalog", lambda lang: {})
        kwargs = {"is_auth": reason is FailoverReason.auth}
        msg = client_error_message(reason, status, **kwargs)
        assert "trix.errors.provider" not in msg, "leaked a catalog key path"
        assert expect_marker in msg.lower()

    @pytest.mark.parametrize(
        "reason,status",
        [
            (FailoverReason.billing, 402),
            (FailoverReason.auth, 401),
            (FailoverReason.auth_permanent, 403),
            (FailoverReason.content_policy_blocked, 400),
            (FailoverReason.ssl_cert_verification, None),
            (FailoverReason.overloaded, 503),
            (None, 500),
            (None, None),
        ],
    )
    def test_every_branch_returns_russian_text_without_latin_jargon(self, reason, status):
        msg = client_error_message(reason, status, is_auth=False)
        assert msg.strip(), "пустое сообщение — клиент увидит тишину"
        # Хотя бы одна кириллическая буква: ловит возврат ключа каталога
        # ("trix.errors.provider.billing") вместо текста.
        assert any("а" <= ch.lower() <= "я" for ch in msg)

    def test_status_code_is_available_but_not_the_headline(self):
        msg = client_error_message(None, 503)
        first_sentence = msg.split(".")[0]
        assert "503" not in first_sentence

    def test_status_present_vs_absent_render_genuinely_different_text(self):
        # Pins the branch itself, not just "code isn't in the headline":
        # a mutation that deletes the `if status:` branch (always falling
        # through to unavailable_no_code) renders near-identical prose for
        # both inputs -- the marker-word test above can't tell them apart
        # because they're intentionally the same sentence minus the code.
        # Presence of the actual status digits is the one thing that can.
        with_status = client_error_message(None, 503)
        without_status = client_error_message(None, None)
        assert with_status != without_status
        assert "503" in with_status
        assert "503" not in without_status


class TestBranchesAreDistinctText:
    """The presence-of-cyrillic check above survives any branch getting
    wired to the wrong catalog key -- it only proves *a* Russian sentence
    came back, not the *right* one. These pin content per branch instead,
    so a key swap (e.g. content_policy_blocked silently rendering the
    unavailable_no_code text) fails loudly.
    """

    # One word that must appear in this branch's message and nowhere else
    # among the four -- picked to not be a substring of any other branch's
    # marker or boilerplate (e.g. "ключ" is out: both billing ("на ключе")
    # and auth_failed ("не принял ключ") contain it).
    _MARKERS = {
        "billing": "баланс",
        "policy": "фильтр",
        "tls": "сертификат",
        "unavailable": "не отвечает",
    }

    @staticmethod
    def _messages():
        return {
            "billing": client_error_message(FailoverReason.billing, None),
            "policy": client_error_message(FailoverReason.content_policy_blocked, 400),
            "tls": client_error_message(FailoverReason.ssl_cert_verification, None),
            "unavailable": client_error_message(None, 503),
        }

    def test_branches_are_pairwise_distinct(self):
        msgs = self._messages()
        values = list(msgs.values())
        assert len(set(values)) == len(values), (
            f"two different failure reasons rendered the same text: {msgs}"
        )

    def test_each_branch_carries_a_word_absent_from_the_others(self):
        msgs = self._messages()
        for name, marker in self._MARKERS.items():
            assert marker in msgs[name].lower(), (
                f"{name} lost its marker word {marker!r}: {msgs[name]!r}"
            )
            for other_name, other_msg in msgs.items():
                if other_name == name:
                    continue
                assert marker not in other_msg.lower(), (
                    f"marker {marker!r} for {name!r} leaked into "
                    f"{other_name!r}: {other_msg!r}"
                )


class TestClientErrorMessageAllBranchesPairwiseDistinct:
    """Full-set distinctness check across ALL nine ``client_error_message``
    output branches (billing, auth_failed, policy_rejected, tls,
    unavailable with/without a code, model_not_found,
    account_policy_blocked, malformed_request).

    ``TestBranchesAreDistinctText`` above only compares four of them
    (billing/policy/tls/unavailable); ``tests/agent/test_trix_client_error_surface.py``
    separately compares the three Task-2 branches against a generic
    fallback. Neither set includes ``auth_failed`` or
    ``unavailable_no_code`` in the SAME comparison, so a key swap onto
    either of those two branches is invisible to both existing checks.
    Confirmed by mutation: swapping ``unavailable_no_code``'s catalog key
    for ``tls``'s (same default kept) passed every test in this file and
    in test_trix_client_error_surface.py untouched -- this class exists to
    close exactly that hole, found during Task 3's review of the sibling
    hole in ``client_fallback_attempt_message``.
    """

    # "unavailable_no_code" has no entry here on purpose: its Russian text
    # is a strict prefix of "unavailable_with_code"'s (same sentence minus
    # " (код {status})"), so it cannot carry a word absent from that one
    # sibling by construction -- that specific pair is already pinned by
    # ``test_status_present_vs_absent_render_genuinely_different_text``.
    # The full pairwise-distinct check below still catches a swap of its
    # key onto any OTHER branch (which is the hole that was actually found).
    _MARKERS = {
        "billing": "баланс",
        "auth_failed": "мастер",
        "policy_rejected": "фильтр",
        "tls": "сертификат",
        "unavailable_with_code": "код",
        "model_not_found": "переименована",
        "account_policy_blocked": "приватности",
        "malformed_request": "администр",
    }

    @staticmethod
    def _messages():
        return {
            "billing": client_error_message(FailoverReason.billing, None),
            "auth_failed": client_error_message(FailoverReason.auth, 401, is_auth=True),
            "policy_rejected": client_error_message(FailoverReason.content_policy_blocked, 400),
            "tls": client_error_message(FailoverReason.ssl_cert_verification, None),
            "unavailable_with_code": client_error_message(None, 503),
            "unavailable_no_code": client_error_message(None, None),
            "model_not_found": client_error_message(FailoverReason.model_not_found, 404),
            "account_policy_blocked": client_error_message(FailoverReason.provider_policy_blocked, 451),
            "malformed_request": client_error_message(FailoverReason.format_error, 422),
        }

    def test_all_nine_branches_are_pairwise_distinct(self):
        msgs = self._messages()
        values = list(msgs.values())
        assert len(set(values)) == len(values), (
            f"two different branches rendered the same text: {msgs}"
        )

    def test_each_marked_branch_carries_a_word_absent_from_every_other_branch(self):
        msgs = self._messages()
        for name, marker in self._MARKERS.items():
            assert marker in msgs[name].lower(), (
                f"{name} lost its marker word {marker!r}: {msgs[name]!r}"
            )
            for other_name, other_msg in msgs.items():
                if other_name == name:
                    continue
                assert marker not in other_msg.lower(), (
                    f"marker {marker!r} for {name!r} leaked into "
                    f"{other_name!r}: {other_msg!r}"
                )


class TestFallbackMessages:
    def test_switch_message_names_both_sides(self):
        msg = client_fallback_message("gpt-x", "openai", "deepseek-chat", "deepseek")
        assert "deepseek-chat" in msg
        assert "gpt-x" in msg

    def test_switch_message_puts_old_and_new_in_the_right_slots_not_just_present(self):
        """Round-3 review: a mutation swapping old_*/new_* inside
        ``client_fallback_message`` (the kwargs passed to ``t()``) passed
        every existing test untouched -- they all only check presence
        ("gpt-x" in msg), never which side of the sentence it lands on. A
        client reading "answering through the fallback: OLD-MODEL. The
        main one was NEW-MODEL" is told, in effect, that the model that
        just failed is the one they're now paying for.

        The RU/EN templates both put the {new_model} clause before "Основной
        был"/"The main one was" and the {old_model} clause after it -- so
        splitting the rendered text on that anchor phrase and checking which
        half each name lands in pins the ORDER, not just membership.
        """
        msg = client_fallback_message("old-model", "old-prov", "new-model", "new-prov")
        anchor = "Основной был" if "Основной был" in msg else "The main one was"
        assert anchor in msg, f"expected the fixed anchor phrase in: {msg!r}"
        before, _, after = msg.partition(anchor)
        assert "new-model" in before and "new-prov" in before, (
            f"new model/provider must appear BEFORE {anchor!r}: {msg!r}"
        )
        assert "old-model" in after and "old-prov" in after, (
            f"old model/provider must appear AFTER {anchor!r}: {msg!r}"
        )
        assert "old-model" not in before
        assert "new-model" not in after

    def test_attempt_message_differs_by_reason(self):
        billing = client_fallback_attempt_message(FailoverReason.billing)
        limit = client_fallback_attempt_message(FailoverReason.rate_limit)
        assert billing != limit
        assert all(m.strip() for m in (billing, limit))

    def test_upstream_name_is_used_when_given(self):
        msg = client_fallback_attempt_message(
            FailoverReason.upstream_rate_limit, upstream="deepseek"
        )
        assert "deepseek" in msg

    def test_unknown_upstream_does_not_splice_a_russian_word_into_english(self, monkeypatch):
        # Regression: the old code filled a missing upstream name with the
        # Russian word "провайдера", so under the English catalog this
        # rendered "The провайдера model rate-limited..." -- a dedicated key
        # for "upstream unknown" replaces that splice entirely.
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        msg = client_fallback_attempt_message(FailoverReason.upstream_rate_limit, upstream=None)
        assert msg.strip()
        assert not any("а" <= ch.lower() <= "я" for ch in msg), (
            f"a Russian word leaked into the English message: {msg!r}"
        )

    def test_unknown_upstream_still_says_something_useful_in_russian(self):
        msg = client_fallback_attempt_message(FailoverReason.upstream_rate_limit, upstream=None)
        assert msg.strip()
        assert "модель" in msg.lower()


class TestSwitchAttemptMessageDistinctFromSuccessMessage:
    """Round-3 review, point 6 (the coordinator's own brief error): the
    original Task 3 brief numbered ``:2281`` (buffered, only reaches the
    client if THIS fallback attempt also fails) and ``:2291`` (one-shot,
    reaches the client only on eventual SUCCESS) as two different moments,
    then told the implementer to route both through the same function. On
    a failed turn the client used to read, back to back: "answering
    through the fallback: X" immediately followed by "the provider didn't
    respond after several attempts" -- two contradictory claims in a row.

    ``client_fallback_switch_attempt_message`` is the fix: a distinct key
    for the buffered/attempt line, worded as an attempt ("trying"), not a
    completed transition ("answering through"). ``client_fallback_message``
    keeps its wording for the one-shot success notice.
    """

    def test_attempt_and_success_messages_are_not_equal(self):
        attempt = client_fallback_switch_attempt_message("old-m", "old-p", "new-m", "new-p")
        success = client_fallback_message("old-m", "old-p", "new-m", "new-p")
        assert attempt != success

    def test_attempt_message_does_not_claim_the_transition_completed(self):
        # The success message's Russian verb for "already answering through"
        # must not appear in the attempt message -- that specific verb is
        # the exact false claim a client on a failed turn must not read.
        success = client_fallback_message("old-m", "old-p", "new-m", "new-p")
        attempt = client_fallback_switch_attempt_message("old-m", "old-p", "new-m", "new-p")
        assert "отвечаю через" in success
        assert "отвечаю через" not in attempt

    def test_attempt_message_still_names_both_models_in_the_right_slots(self):
        # Same order guarantee as client_fallback_message -- the attempt
        # line reuses the identical old/new template shape, just with
        # different wording around it.
        msg = client_fallback_switch_attempt_message("old-model", "old-prov", "new-model", "new-prov")
        anchor = "Основной был" if "Основной был" in msg else "The main one was"
        assert anchor in msg, f"expected the fixed anchor phrase in: {msg!r}"
        before, _, after = msg.partition(anchor)
        assert "new-model" in before and "new-prov" in before
        assert "old-model" in after and "old-prov" in after
        assert "old-model" not in before
        assert "new-model" not in after

    def test_attempt_message_does_not_diagnose_what_happened_to_the_primary(
        self, monkeypatch
    ):
        """Round-4 review: the attempt line used to open with "the main
        provider didn't respond" in both catalogs.

        That is a claim about the primary, and it is wrong for the two
        reasons that reach this line most often: on HTTP 402 the provider
        answered -- it refused on money -- and on 401 it answered refusing
        the key. It is also redundant. This line never appears alone: it is
        always preceded in the same buffer by
        ``client_fallback_attempt_message()``, which already named the real
        cause. A second, hardcoded diagnosis can only contradict that one,
        never add to it, so the switch line must report the transition and
        stop there.
        """
        for lang, forbidden in (("ru", "не отвеч"), ("en", "respond")):
            monkeypatch.setenv("HERMES_LANGUAGE", lang)
            i18n_mod.reset_language_cache()
            msg = client_fallback_switch_attempt_message("old-m", "old-p", "new-m", "new-p")
            assert forbidden not in msg.lower(), (
                f"the {lang} attempt line still diagnoses the primary: {msg!r}"
            )
            # It must still do its own job: name both sides of the switch.
            assert "new-m" in msg and "old-m" in msg

    def test_attempt_message_default_literal_also_stops_at_the_transition(
        self, monkeypatch
    ):
        # Same guarantee on the path where the catalog is unreadable and the
        # module's own default= literal is what the client reads.
        monkeypatch.setattr(i18n_mod, "_load_catalog", lambda lang: {})
        msg = client_fallback_switch_attempt_message("old-m", "old-p", "new-m", "new-p")
        assert "respond" not in msg.lower(), f"default literal diagnoses the primary: {msg!r}"
        assert "new-m" in msg and "old-m" in msg

    def test_attempt_message_is_russian_and_has_a_default(self, monkeypatch):
        msg = client_fallback_switch_attempt_message("m1", "p1", "m2", "p2")
        assert any("а" <= ch.lower() <= "я" for ch in msg)
        monkeypatch.setattr(i18n_mod, "_load_catalog", lambda lang: {})
        broken = client_fallback_switch_attempt_message("m1", "p1", "m2", "p2")
        assert "trix.errors.fallback" not in broken, f"leaked a catalog key path: {broken!r}"
        assert "m2" in broken and "p2" in broken


class TestSwitchFamilyIsThreeDistinctMessages:
    """Round-5 review: the client-visible switch vocabulary is three
    messages, and no two of them may render the same text.

    Two of them fire back to back on the empty-response failover path --
    ``try_activate_fallback()`` buffers the generic attempt line, then
    ``conversation_loop.py`` buffers this path's own confirmation. Nothing
    dedupes the buffer, so identical text means the client reads one sentence
    twice; it also means every call-site test can be satisfied by the wrong
    call site's output, which is how a deleted call site stayed green for
    three review rounds.

    Distinctness at THIS level is what makes the call-site tests honest, so
    it is asserted here once rather than re-derived in each of them.
    """

    def _renders(self):
        args = ("old-model", "old-prov", "new-model", "new-prov")
        return {
            "switched": client_fallback_message(*args),
            "switch_attempt": client_fallback_switch_attempt_message(*args),
            "switched_after_empty": client_fallback_empty_response_switch_message(*args),
        }

    def test_the_three_switch_messages_are_pairwise_distinct(self):
        msgs = self._renders()
        values = list(msgs.values())
        assert len(set(values)) == len(values), (
            f"two switch messages render the same text: {msgs}"
        )

    def test_they_stay_distinct_when_the_catalog_is_unreadable(self, monkeypatch):
        # The default= literals are a second, independent copy of the same
        # three sentences -- they must not collapse into each other either.
        monkeypatch.setattr(i18n_mod, "_load_catalog", lambda lang: {})
        msgs = self._renders()
        values = list(msgs.values())
        assert len(set(values)) == len(values), (
            f"two default= literals render the same text: {msgs}"
        )
        for name, msg in msgs.items():
            assert "trix.errors.fallback" not in msg, f"{name} leaked a key path: {msg!r}"

    def test_empty_response_switch_names_the_path_that_produced_it(self):
        msg = client_fallback_empty_response_switch_message(
            "old-model", "old-prov", "new-model", "new-prov"
        )
        assert any("а" <= ch.lower() <= "я" for ch in msg)
        # It is the one switch message tied to a single code path, so it can
        # say what happened without guessing -- the model really did come
        # back empty repeatedly. Contrast the generic attempt line, which is
        # shared by billing/auth/rate-limit and therefore must not diagnose.
        assert "пуст" in msg.lower(), f"the empty-response cause is missing: {msg!r}"

    def test_empty_response_switch_does_not_claim_the_turn_is_answered(self):
        # It is buffered, so it only ever reaches the client when the turn
        # failed anyway. Claiming an answer is being delivered would
        # contradict the failure message printed right after it (round-3).
        msg = client_fallback_empty_response_switch_message(
            "old-model", "old-prov", "new-model", "new-prov"
        )
        assert "отвечаю через" not in msg.lower()

    def test_empty_response_switch_puts_old_and_new_in_the_right_slots(self):
        msg = client_fallback_empty_response_switch_message(
            "old-model", "old-prov", "new-model", "new-prov"
        )
        anchor = "Прежняя" if "Прежняя" in msg else "The previous one"
        assert anchor in msg, f"expected the fixed anchor phrase in: {msg!r}"
        before, _, after = msg.partition(anchor)
        assert "new-model" in before and "new-prov" in before
        assert "old-model" in after and "old-prov" in after
        assert "old-model" not in before
        assert "new-model" not in after


class TestSuccessNoticeDoesNotDiagnoseThePrimary:
    """Round-5 review: ``client_fallback_message`` is the ONE line a client
    reads when the failover WORKS -- the buffered trace is dropped on
    recovery, so nothing else survives to explain the switch.

    It used to open with "the main provider didn't respond", which is false
    for the two reasons that reach it most often: on HTTP 402 the provider
    answered, refusing on money, and on 401 it answered refusing the key.
    Round 4 stripped that claim out of the buffered attempt line for exactly
    this reason; on the success path it is worse, because there is no
    preceding line naming the real cause to contradict it -- the wrong story
    is the only story. It must report the transition and who is answering
    now, and leave the cause to the messages that know it.
    """

    def test_success_notice_makes_no_claim_about_why_the_primary_failed(
        self, monkeypatch
    ):
        for lang, forbidden in (("ru", "не отвеч"), ("en", "respond")):
            monkeypatch.setenv("HERMES_LANGUAGE", lang)
            i18n_mod.reset_language_cache()
            msg = client_fallback_message("old-m", "old-p", "new-m", "new-p")
            assert forbidden not in msg.lower(), (
                f"the {lang} success notice still diagnoses the primary: {msg!r}"
            )
            # It must still do its own job: say the switch happened and name
            # both sides of it.
            assert "new-m" in msg and "old-m" in msg

    def test_default_literal_also_makes_no_such_claim(self, monkeypatch):
        monkeypatch.setattr(i18n_mod, "_load_catalog", lambda lang: {})
        msg = client_fallback_message("old-m", "old-p", "new-m", "new-p")
        assert "respond" not in msg.lower(), (
            f"the default literal still diagnoses the primary: {msg!r}"
        )
        assert "new-m" in msg and "old-m" in msg


class TestFallbackAttemptNewBranches:
    """Task 3: three call sites (conversation_loop.py's auth-failover,
    content-filter-stream, and empty/malformed-response eager-fallback
    branches) used to fall through ``client_fallback_attempt_message``'s
    default case, which is worded for rate limiting specifically
    ("ограничил частоту запросов"). That is a wrong diagnosis for all
    three -- these tests pin each branch to its own text and confirm none
    of them silently degrade back to the rate-limit default.
    """

    _RATE_LIMIT_MARKER = "ограничил частоту"

    @pytest.mark.parametrize("reason", [FailoverReason.auth, FailoverReason.auth_permanent])
    def test_auth_reasons_get_dedicated_text_not_the_rate_limit_default(self, reason):
        msg = client_fallback_attempt_message(reason)
        assert msg.strip()
        assert any("а" <= ch.lower() <= "я" for ch in msg)
        assert self._RATE_LIMIT_MARKER not in msg

    def test_content_policy_blocked_gets_dedicated_text_not_the_rate_limit_default(self):
        msg = client_fallback_attempt_message(FailoverReason.content_policy_blocked)
        assert msg.strip()
        assert any("а" <= ch.lower() <= "я" for ch in msg)
        assert self._RATE_LIMIT_MARKER not in msg

    def test_invalid_response_sentinel_gets_dedicated_text_not_the_rate_limit_default(self):
        # "invalid_response" is a plain string, not a FailoverReason member
        # -- conversation_loop.py's empty/malformed-response eager-fallback
        # branches (there is no HTTP error to run through the classifier)
        # pass it as a literal sentinel.
        msg = client_fallback_attempt_message("invalid_response")
        assert msg.strip()
        assert any("а" <= ch.lower() <= "я" for ch in msg)
        assert self._RATE_LIMIT_MARKER not in msg

    def test_the_three_new_branches_and_the_default_are_pairwise_distinct(self):
        texts = [
            client_fallback_attempt_message(FailoverReason.auth),
            client_fallback_attempt_message(FailoverReason.content_policy_blocked),
            client_fallback_attempt_message("invalid_response"),
            client_fallback_attempt_message(FailoverReason.rate_limit),
        ]
        assert len(set(texts)) == len(texts), texts


class TestAllFallbackAttemptBranchesPairwiseDistinct:
    """Full-set distinctness check across every
    ``client_fallback_attempt_message`` output branch: billing,
    unreachable (timeout/overloaded/server_error), upstream (named and
    unknown), auth, content-filter, invalid_response, and the rate-limit
    default.

    ``TestFallbackAttemptNewBranches.test_the_three_new_branches_and_the_default_are_pairwise_distinct``
    above only compares the three Task-3 branches plus the default -- it
    does NOT include billing/unreachable/upstream, so a swap between two
    of THOSE branches was invisible to it. Confirmed by mutation
    (coordinator's round): swapping the ``billing`` branch's key+default
    for ``unreachable``'s passed ``tests/hermes_cli/test_trix_provider_errors.py``,
    ``tests/agent/test_trix_fallback_notice.py``, and
    ``tests/run_agent/test_trix_fallback_attempt_buffer.py`` untouched (52
    passed, 0 failed) -- a client out of funds would have read "provider
    unreachable, try again shortly" instead of being told to top up. This
    class exists to close exactly that hole.
    """

    _MARKERS = {
        "billing": "средства",
        "unreachable": "недоступен",
        "upstream_unknown": "стороннего",
        "auth": "учётные данные",
        "content_filter": "фильтр",
        "invalid_response": "пустой",
        "rate_limit_default": "запросов",
        # "upstream_named" has no marker-word entry: its distinguishing
        # content is the caller-supplied {upstream} name itself, asserted
        # separately below rather than via a fixed Russian word.
    }

    @staticmethod
    def _messages():
        return {
            "billing": client_fallback_attempt_message(FailoverReason.billing),
            "unreachable": client_fallback_attempt_message(FailoverReason.timeout),
            "upstream_named": client_fallback_attempt_message(
                FailoverReason.upstream_rate_limit, upstream="DeepSeek"
            ),
            "upstream_unknown": client_fallback_attempt_message(FailoverReason.upstream_rate_limit),
            "auth": client_fallback_attempt_message(FailoverReason.auth),
            "content_filter": client_fallback_attempt_message(FailoverReason.content_policy_blocked),
            "invalid_response": client_fallback_attempt_message("invalid_response"),
            "rate_limit_default": client_fallback_attempt_message(FailoverReason.rate_limit),
        }

    def test_all_eight_branches_are_pairwise_distinct(self):
        msgs = self._messages()
        values = list(msgs.values())
        assert len(set(values)) == len(values), (
            f"two different branches rendered the same text: {msgs}"
        )

    def test_each_marked_branch_carries_a_word_absent_from_every_other_branch(self):
        msgs = self._messages()
        for name, marker in self._MARKERS.items():
            assert marker in msgs[name].lower(), (
                f"{name} lost its marker word {marker!r}: {msgs[name]!r}"
            )
            for other_name, other_msg in msgs.items():
                if other_name == name:
                    continue
                assert marker not in other_msg.lower(), (
                    f"marker {marker!r} for {name!r} leaked into "
                    f"{other_name!r}: {other_msg!r}"
                )

    def test_upstream_named_carries_the_given_name_and_differs_from_unknown(self):
        msgs = self._messages()
        assert "deepseek" in msgs["upstream_named"].lower()
        assert "deepseek" not in msgs["upstream_unknown"].lower()
        assert msgs["upstream_named"] != msgs["upstream_unknown"]


class TestFallbackMessagesSurviveABrokenCatalog:
    """Mirrors ``TestClientErrorMessage.test_default_kicks_in_when_the_catalog_is_totally_unreadable``
    for the fallback functions -- guards every ``default=`` on every ``t()``
    call in ``client_fallback_message``/``client_fallback_attempt_message``,
    not just the two the brief's own example touched. If any single call
    site loses its ``default=`` kwarg, the matching case below returns the
    bare dotted key path instead of English text once the catalog is empty.
    """

    def test_switched_falls_back_to_english_default_not_a_key_path(self, monkeypatch):
        monkeypatch.setattr(i18n_mod, "_load_catalog", lambda lang: {})
        msg = client_fallback_message("m1", "p1", "m2", "p2")
        assert "trix.errors.fallback" not in msg
        assert "m2" in msg and "p2" in msg

    @pytest.mark.parametrize(
        "reason,kwargs,expect_marker",
        [
            (FailoverReason.upstream_rate_limit, {"upstream": "deepseek"}, "deepseek"),
            (FailoverReason.upstream_rate_limit, {}, "upstream"),
            (FailoverReason.billing, {}, "fallback"),
            (FailoverReason.timeout, {}, "unreachable"),
            (FailoverReason.auth, {}, "authentication"),
            (FailoverReason.content_policy_blocked, {}, "safety filter"),
            ("invalid_response", {}, "empty"),
            (FailoverReason.rate_limit, {}, "rate-limited"),
        ],
    )
    def test_every_attempt_branch_falls_back_to_english_default_not_a_key_path(
        self, monkeypatch, reason, kwargs, expect_marker
    ):
        monkeypatch.setattr(i18n_mod, "_load_catalog", lambda lang: {})
        msg = client_fallback_attempt_message(reason, **kwargs)
        assert "trix.errors.fallback" not in msg, f"leaked a catalog key path: {msg!r}"
        assert expect_marker in msg.lower()
