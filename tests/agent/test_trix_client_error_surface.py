"""Клиент получает русскую фразу, а не 'Non-retryable error (HTTP 402)'.

``agent.conversation_loop._emit_client_terminal_error`` is the single call
site that used to print the bare ``"❌ Non-retryable error (HTTP {code})"``
straight to a Telegram client. It now delegates the client-facing text to
``hermes_cli.trix_provider_errors.client_error_message`` (Task 1) and keeps
technical detail (provider/model/endpoint/raw body) in the log via
``agent._vprint`` -- never in the message the client sees.

These tests call the wrapper directly, not ``run_conversation`` -- a real
run would need a live API client. See task-2-report.md for the full
FailoverReason audit (which reasons can actually reach this call site).
"""
import pytest

import agent.conversation_loop as cl
from agent import i18n
from agent.error_classifier import FailoverReason


class _FakeAgent:
    def __init__(self):
        self.emitted = []
        self.vprinted = []
        self.buffered = []
        self.log_prefix = ""

    def _emit_status(self, message):
        self.emitted.append(message)

    def _vprint(self, *a, **kw):
        self.vprinted.append((a, kw))

    def _buffer_status(self, message):
        self.buffered.append(message)


@pytest.fixture(autouse=True)
def _russian_language(monkeypatch):
    # t() resolves language from env > config.yaml > "en". tests/agent/ has
    # no gateway-style autouse language pin, and the isolated test HERMES_HOME
    # carries no config.yaml, so without this the catalog resolves to "en"
    # and the Russian-text assertions below fail for the wrong reason.
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    # _catalog_cache is keyed by resolved language and lives for the whole
    # process, so an earlier test elsewhere in this file (or a future one
    # that flips HERMES_LANGUAGE locally, mirroring
    # tests/hermes_cli/test_trix_provider_errors.py) could leave a stale
    # entry behind. Reset on both sides of the test, not just teardown --
    # a test that runs before this fixture's first setup could still be
    # sharing the interpreter with a prior file-local mutation.
    i18n.reset_language_cache()
    yield
    i18n.reset_language_cache()


def test_terminal_failure_message_is_russian_and_actionable():
    agent = _FakeAgent()
    cl._emit_client_terminal_error(
        agent,
        reason=FailoverReason.billing,
        status_code=402,
        is_auth=False,
        summary="insufficient credits",
    )
    assert agent.emitted, "клиент не получил ничего"
    text = agent.emitted[0]
    assert "средства" in text or "баланс" in text.lower()
    assert "Non-retryable" not in text


def test_technical_summary_survives_but_not_as_the_headline():
    agent = _FakeAgent()
    cl._emit_client_terminal_error(
        agent,
        reason=None,
        status_code=503,
        is_auth=False,
        summary="upstream connect error",
    )
    text = agent.emitted[0]
    assert text.strip()
    assert not text.lstrip().startswith("503")


# Poison payload standing in for the two things ``summary`` can legitimately
# contain: a raw provider error body (Cloudflare/proxy HTML challenge pages,
# see run_agent.py's ``_summarize_api_error`` docstring and
# tests/run_agent/test_nonretryable_error_html_summary.py) and something
# that LOOKS like a live credential leaking through in a proxied error body.
# Neither must ever reach the client -- that is the entire reason
# ``_emit_client_terminal_error`` keeps ``summary`` on the ``_vprint`` side
# instead of splicing it into the ``_emit_status`` call.
_POISON_SUMMARY = (
    "<html><head><title>Attention Required! | Cloudflare</title></head>"
    "<body>Please complete the security check to access example.com "
    "Ray ID: 7f3a9c1b2e4d0000 sk-live-51H8xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    "</body></html>"
)


def test_summary_never_leaks_into_the_client_facing_message():
    """Regression guard for a mutation that survived the round-1 review
    untouched: gluing the raw summary onto the client message
    (``agent._emit_status(client_error_message(...) + f"\\n\\n{summary}")``)
    passes both of the brief's original assertions (no literal
    "Non-retryable", doesn't start with the status code) while defeating
    the entire point of ``_summarize_api_error`` upstream and leaking
    Cloudflare markup / secret-shaped strings straight to the client.
    """
    agent = _FakeAgent()
    cl._emit_client_terminal_error(
        agent,
        reason=FailoverReason.billing,
        status_code=402,
        is_auth=False,
        summary=_POISON_SUMMARY,
    )
    assert agent.emitted, "клиент не получил ничего"
    client_text = agent.emitted[0]
    assert "cloudflare" not in client_text.lower()
    assert "<html" not in client_text.lower()
    assert "sk-live-" not in client_text
    assert "Ray ID" not in client_text

    # The technical detail must still reach the log -- this is not "delete
    # the summary", it's "never let the client see it".
    assert agent.vprinted, "техническая сводка не ушла в лог"
    logged_text = agent.vprinted[0][0][0]
    assert "Cloudflare" in logged_text
    assert "sk-live-" in logged_text


def test_vprint_receives_the_technical_summary_with_log_prefix():
    agent = _FakeAgent()
    agent.log_prefix = "[job] "
    cl._emit_client_terminal_error(
        agent,
        reason=FailoverReason.billing,
        status_code=402,
        is_auth=False,
        summary="insufficient credits",
    )
    assert agent.vprinted, "техническая сводка не ушла в лог"
    call_args, call_kwargs = agent.vprinted[0]
    assert "[job]" in call_args[0]
    assert "insufficient credits" in call_args[0]
    assert call_kwargs.get("force") is True


def test_empty_summary_does_not_produce_a_log_call():
    agent = _FakeAgent()
    cl._emit_client_terminal_error(
        agent,
        reason=FailoverReason.billing,
        status_code=402,
        is_auth=False,
        summary="",
    )
    assert agent.emitted
    assert not agent.vprinted


def test_a_broken_vprint_does_not_break_the_client_facing_message():
    # The docstring says technical logging is best-effort; a logging
    # failure must never take the client-facing status down with it.
    class _BrokenVprintAgent(_FakeAgent):
        def _vprint(self, *a, **kw):
            raise RuntimeError("logging is down")

    agent = _BrokenVprintAgent()
    cl._emit_client_terminal_error(
        agent,
        reason=FailoverReason.billing,
        status_code=402,
        is_auth=False,
        summary="insufficient credits",
    )
    assert agent.emitted


def test_is_auth_flag_routes_to_the_auth_message_independent_of_reason():
    # classified.is_auth covers both FailoverReason.auth and .auth_permanent
    # (agent/error_classifier.py) -- the wrapper is given the flag directly,
    # not asked to re-derive it from `reason`.
    agent = _FakeAgent()
    cl._emit_client_terminal_error(
        agent, reason=None, status_code=401, is_auth=True, summary="",
    )
    text = agent.emitted[0].lower()
    assert "ключ" in text and ("мастер" in text or "wizard" in text)


class TestFailoverReasonAudit:
    """Every ``FailoverReason`` that can actually reach this terminal call
    site (``is_client_error`` in agent/conversation_loop.py: not retryable,
    not should_compress, and not one of {rate_limit, overloaded,
    context_overflow, payload_too_large, long_context_tier,
    thinking_signature}) must not tell the client "wait and retry" when a
    retry provably reproduces the identical rejection.

    Confirmed reachable with retryable=False, per agent/error_classifier.py:
    billing, auth (handled via is_auth, tested above), content_policy_blocked,
    ssl_cert_verification, model_not_found, provider_policy_blocked,
    format_error, and (one circuit-breaker branch) timeout.

    Confirmed NOT reachable here (see task-2-report.md for the trace):
    - context_overflow: excluded twice over -- explicitly in
      is_client_error's exclusion set, AND caught earlier by its own
      ``is_context_length_error`` branch, which never falls through to
      this call site.
    - rate_limit, overloaded, payload_too_large, long_context_tier,
      thinking_signature: always retryable=True (or excluded by name).
    - upstream_rate_limit, server_error, unknown, image_too_large,
      invalid_encrypted_content, multimodal_tool_content_unsupported,
      oauth_long_context_beta_forbidden, llama_cpp_grammar_pattern: every
      classifier call site producing these sets retryable=True.
    """

    _RETRY_ADVICE_MARKERS = (
        "на его стороне", "попробуйте повторить", "on their end", "try again shortly",
    )

    @pytest.mark.parametrize(
        "reason",
        [
            FailoverReason.model_not_found,
            FailoverReason.provider_policy_blocked,
            FailoverReason.format_error,
        ],
    )
    def test_deterministic_terminal_reasons_do_not_promise_a_retry_will_help(self, reason):
        agent = _FakeAgent()
        cl._emit_client_terminal_error(
            agent, reason=reason, status_code=400, is_auth=False, summary="detail",
        )
        text = agent.emitted[0].lower()
        for marker in self._RETRY_ADVICE_MARKERS:
            assert marker.lower() not in text, (
                f"{reason} misleadingly promises a retry will help: {text!r}"
            )

    @pytest.mark.parametrize(
        "reason,expected_marker",
        [
            (FailoverReason.model_not_found, "модел"),
            (FailoverReason.provider_policy_blocked, "приватности"),
            (FailoverReason.format_error, "администр"),
        ],
    )
    def test_deterministic_terminal_reasons_have_dedicated_actionable_text(
        self, reason, expected_marker
    ):
        agent = _FakeAgent()
        cl._emit_client_terminal_error(
            agent, reason=reason, status_code=400, is_auth=False, summary="detail",
        )
        text = agent.emitted[0].lower()
        assert expected_marker in text

    def test_model_not_found_points_at_the_telegram_slash_command_not_a_shell_command(self):
        # Round-1 review: `hermes model` is a shell command a Telegram-only
        # client cannot run. /model is the same switch wired in as a normal
        # gateway slash command (hermes_cli/commands.py, no cli_only flag).
        agent = _FakeAgent()
        cl._emit_client_terminal_error(
            agent, reason=FailoverReason.model_not_found, status_code=404,
            is_auth=False, summary="",
        )
        text = agent.emitted[0]
        assert "/model" in text
        assert "hermes model" not in text.lower()
        assert "hermes" not in text.lower()

    def test_account_policy_blocked_drops_the_aggregator_jargon(self):
        # Round-1 review: "aggregator provider" / "провайдер-агрегатор" is
        # unexplained jargon to a non-technical client. Plain "provider"
        # says the same thing.
        agent = _FakeAgent()
        cl._emit_client_terminal_error(
            agent, reason=FailoverReason.provider_policy_blocked, status_code=451,
            is_auth=False, summary="",
        )
        text = agent.emitted[0].lower()
        assert "агрегатор" not in text
        assert "aggregator" not in text

    def test_format_error_does_not_suggest_destroying_the_conversation(self):
        # Round-1 review: FailoverReason.format_error is the classifier's
        # catch-all for any unrecognized 4xx (405/409/415/422/451, ...) and
        # any 400 matching no specific heuristic -- confidently diagnosing
        # "conversation is in an odd state" is usually wrong for that
        # population, and /reset is irreversible. Neither the diagnosis nor
        # the destructive suggestion belongs in this branch.
        agent = _FakeAgent()
        cl._emit_client_terminal_error(
            agent, reason=FailoverReason.format_error, status_code=422,
            is_auth=False, summary="",
        )
        text = agent.emitted[0].lower()
        assert "/reset" not in text
        assert "/compact" not in text
        assert "странном состоянии" not in text
        assert "odd state" not in text

    def test_the_three_new_branches_are_pairwise_distinct_from_each_other_and_from_generic(self):
        agent = _FakeAgent()
        texts = []
        for reason in (
            FailoverReason.model_not_found,
            FailoverReason.provider_policy_blocked,
            FailoverReason.format_error,
        ):
            cl._emit_client_terminal_error(
                agent, reason=reason, status_code=400, is_auth=False, summary="",
            )
            texts.append(agent.emitted[-1])
        cl._emit_client_terminal_error(
            agent, reason=None, status_code=400, is_auth=False, summary="",
        )
        texts.append(agent.emitted[-1])
        assert len(set(texts)) == len(texts), texts

    def test_content_policy_and_tls_keep_their_own_dedicated_text(self):
        # Pre-existing branches in client_error_message -- confirm the
        # rewrite (single call to _emit_client_terminal_error instead of
        # inline if/elif/else) preserved them.
        agent = _FakeAgent()
        cl._emit_client_terminal_error(
            agent, reason=FailoverReason.content_policy_blocked,
            status_code=400, is_auth=False, summary="",
        )
        policy_text = agent.emitted[-1].lower()
        assert "фильтр" in policy_text

        cl._emit_client_terminal_error(
            agent, reason=FailoverReason.ssl_cert_verification,
            status_code=None, is_auth=False, summary="",
        )
        tls_text = agent.emitted[-1].lower()
        assert "сертификат" in tls_text


class TestBufferClientRetryingFallbackStatus:
    """``_buffer_client_retrying_fallback_status`` -- round-1 review fix.

    Before: only ``content_policy_blocked``/``ssl_cert_verification`` got
    dedicated (English) text on the pre-fallback buffered status; every
    other reason (billing, auth, model_not_found, ...) fell through to a
    bare ``"Non-retryable error (HTTP {code})"`` that reached Telegram
    verbatim if the subsequent fallback attempt also failed.
    """

    def test_uses_buffer_status_not_emit_status(self):
        agent = _FakeAgent()
        cl._buffer_client_retrying_fallback_status(
            agent, reason=FailoverReason.billing, status_code=402, is_auth=False,
        )
        assert agent.buffered, "ничего не забуферизовано"
        assert not agent.emitted, "статус должен ждать flush, а не уйти сразу"

    def test_billing_headline_is_russian_and_not_the_old_english_literal(self):
        agent = _FakeAgent()
        cl._buffer_client_retrying_fallback_status(
            agent, reason=FailoverReason.billing, status_code=402, is_auth=False,
        )
        text = agent.buffered[0]
        assert "средства" in text or "баланс" in text.lower()
        assert "Non-retryable" not in text

    def test_still_says_trying_a_fallback(self):
        agent = _FakeAgent()
        cl._buffer_client_retrying_fallback_status(
            agent, reason=FailoverReason.billing, status_code=402, is_auth=False,
        )
        text = agent.buffered[0].lower()
        assert "запасн" in text  # "запасного провайдера"

    def test_model_not_found_no_longer_falls_through_to_the_generic_http_code_line(self):
        # Regression target: before the fix, every reason except
        # content_policy_blocked/ssl_cert_verification rendered the bare
        # "Non-retryable error (HTTP {code})" here.
        agent = _FakeAgent()
        cl._buffer_client_retrying_fallback_status(
            agent, reason=FailoverReason.model_not_found, status_code=404, is_auth=False,
        )
        text = agent.buffered[0]
        assert "Non-retryable" not in text
        assert "модел" in text.lower()

    def test_content_policy_and_tls_keep_their_dedicated_headline_here_too(self):
        agent = _FakeAgent()
        cl._buffer_client_retrying_fallback_status(
            agent, reason=FailoverReason.content_policy_blocked, status_code=400, is_auth=False,
        )
        assert "фильтр" in agent.buffered[-1].lower()

        cl._buffer_client_retrying_fallback_status(
            agent, reason=FailoverReason.ssl_cert_verification, status_code=None, is_auth=False,
        )
        assert "сертификат" in agent.buffered[-1].lower()


class TestTwoConsecutiveBillingMessagesAreDifferent:
    """На 402 клиент читает подряд ДВА сообщения, и они обязаны говорить
    разное.

    Финальное ревью сняло исполнением такую пару: терминальный статус
    (``_emit_status``) и ``final_response`` звали одну и ту же
    ``client_error_message(billing)``, и клиент получал байт-в-байт
    одинаковый текст дважды подряд — второй раз с приклеенным английским
    ``_billing_or_entitlement_message`` и советом
    ``/model <model> --provider <provider>``: синтаксис с флагами, который
    в Telegram не набирается.

    До этой ветки те же две строки были РАЗНЫМИ («Non-retryable error
    (HTTP 402)» и «Billing or credits exhausted: …»). Мы их схлопнули,
    исправляя язык, — ровно та регрессия, которую задача 3 уже разводила
    обратно для ``fallback.switched`` / ``switched_after_empty``. Тесты
    ниже сделаны по образцу
    ``TestSwitchAttemptMessageDistinctFromSuccessMessage`` в
    ``tests/hermes_cli/test_trix_provider_errors.py``.

    Ни один из них не читает исходник: обе фразы снимаются вызовом.
    """

    # То, чего клиент из Telegram выполнить не может, и то, что выдаёт
    # непереведённый хвост.
    _FORBIDDEN = (
        "--provider",
        "/model <",
        "add credits",
        "you can switch providers",
        "update billing",
        "entitlement",
    )

    @staticmethod
    def _first_message():
        agent = _FakeAgent()
        cl._emit_client_terminal_error(
            agent,
            reason=FailoverReason.billing,
            status_code=402,
            is_auth=False,
            summary="Billing or credits exhausted: no funds",
        )
        return agent.emitted[0]

    @staticmethod
    def _second_message(provider="openrouter", base_url="https://openrouter.ai/api/v1"):
        return cl._client_billing_final_response(
            provider=provider, base_url=base_url, model="test/model",
        )

    def test_the_two_messages_are_not_equal(self):
        assert self._first_message() != self._second_message()

    def test_neither_message_contains_the_other(self):
        """Неравенства мало: приклеенный хвост делал вторую строку первой
        плюс английский абзац, и клиент всё равно читал одно и то же
        предложение дважды."""
        first, second = self._first_message(), self._second_message()
        assert first not in second, f"вторая строка целиком повторяет первую: {second!r}"
        assert second not in first

    def test_the_two_messages_do_not_share_a_sentence(self):
        def sentences(text):
            return {
                part.strip()
                for part in text.replace("\n", ". ").split(". ")
                if len(part.strip()) > 25
            }

        shared = sentences(self._first_message()) & sentences(self._second_message())
        assert not shared, f"одно и то же предложение в обоих сообщениях: {shared!r}"

    def test_the_second_message_carries_no_english_tail_and_no_infeasible_advice(self):
        lowered = self._second_message().lower()
        for marker in self._FORBIDDEN:
            assert marker not in lowered, f"{marker!r} в тексте клиенту: {lowered!r}"

    def test_both_messages_are_russian_when_russian_is_active(self):
        for text in (self._first_message(), self._second_message()):
            assert any("а" <= ch.lower() <= "я" for ch in text), text

    def test_the_second_message_says_what_to_do_next(self):
        # Своя работа, а не перефразировка первой: ход не выполнен,
        # разговор цел, что сделать после пополнения.
        lowered = self._second_message().lower()
        assert "повтор" in lowered, lowered

    def test_the_top_up_link_survives_as_the_one_useful_piece_of_the_old_tail(self):
        from agent.billing_links import build_billing_block

        expected = build_billing_block(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
        ).billing_url
        assert expected, "у openrouter обязан быть адрес пополнения"
        assert expected in self._second_message()

    def test_an_unknown_provider_does_not_invent_a_link(self):
        message = self._second_message(provider="", base_url="")
        assert "http" not in message.lower(), message

    def test_the_default_literals_are_distinct_too(self, monkeypatch):
        """Тот же инвариант на пути, где каталог не прочитался и клиент
        читает ``default=``-литералы модуля."""
        monkeypatch.setattr(i18n, "_load_catalog", lambda lang: {})
        first, second = self._first_message(), self._second_message()
        assert first != second
        assert first not in second
        lowered = second.lower()
        for marker in self._FORBIDDEN:
            assert marker not in lowered, f"{marker!r} в default-литерале: {lowered!r}"
