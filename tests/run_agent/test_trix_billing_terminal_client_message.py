"""Round 1 review gap-closer: drives ``run_conversation`` end-to-end with a
client whose ``chat.completions.create`` raises a real 402, and asserts the
Russian text actually reaches ``status_callback`` -- not a unit test of
``_emit_client_terminal_error`` in isolation (that's
``tests/agent/test_trix_client_error_surface.py``), but proof the real
call site in ``agent/conversation_loop.py`` still wires into it.

Harness lifted from the sibling regression tests in this directory that
already drive the exact same non-retryable-client-error abort path:
``test_31273_402_not_retried.py`` (402 must abort, no retry burn) and
``test_nonretryable_error_html_summary.py`` (the ``AIAgent(...)`` +
``agent.client = MagicMock(); ...create.side_effect = <exc>`` + real
``run_conversation(...)`` construction). Neither of those asserts what the
*client* actually sees -- this test closes exactly that gap, flagged in
code review after two mutations (gluing the raw provider body onto the
client message, and reverting the call site to the old English literal)
both survived the full ``tests/agent/`` + ``tests/gateway/`` suites
untouched.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent import i18n
from run_agent import AIAgent


def _make_402_billing_error() -> Exception:
    """A plain HTTP 402 with no transient usage-limit phrasing.

    ``agent.error_classifier._classify_402`` routes a 402 to
    ``FailoverReason.rate_limit`` only when BOTH a usage-limit pattern
    ("quota", "usage limit", ...) AND a transient signal ("try again",
    "retry", "wait", ...) are present in the body. This message has
    neither, so it lands on the confirmed-billing branch.
    """
    err = Exception("402 Payment Required: account balance is insufficient.")
    err.status_code = 402
    return err


def _make_billing_agent(statuses: list) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            api_mode="chat_completions",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            # No credential_pool, no fallback config -- both recovery paths
            # fall through immediately, matching #31273's real-world shape
            # (pay-per-token provider, no pool, no fallback).
            status_callback=lambda kind, message: statuses.append((kind, message)),
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def test_billing_abort_delivers_russian_text_through_status_callback(monkeypatch):
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        statuses: list = []
        agent = _make_billing_agent(statuses)
        agent.client.chat.completions.create.side_effect = _make_402_billing_error()

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("do the work")

        # Guard against a vacuous pass -- the mocked 402 must be the actual
        # failure that aborted the turn, matching the sibling tests' pattern.
        assert agent.client.chat.completions.create.called
        assert result.get("failed") is True

        lifecycle_texts = [msg for kind, msg in statuses if kind == "lifecycle"]
        assert lifecycle_texts, (
            "no status_callback('lifecycle', ...) call at all -- the client "
            "got total silence instead of an explanation"
        )
        joined = "\n".join(lifecycle_texts)
        assert "средства" in joined or "баланс" in joined.lower(), (
            f"expected a Russian billing explanation to reach status_callback, "
            f"got: {lifecycle_texts!r}"
        )
        assert "Non-retryable" not in joined
    finally:
        i18n.reset_language_cache()


def test_billing_abort_final_response_is_not_a_second_english_message(monkeypatch):
    """final_response (the second, durable reply -- see finalize_turn) used
    to stay the raw English ``"Billing or credits exhausted: ..."`` even
    when the status_callback message right before it was already Russian.
    It does not pass through the gateway's provider-error rewriter, so a
    Trix client got a correct Russian status immediately followed by an
    English second reply. Round-1 review fix: route it through the same
    module as the status.
    """
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        statuses: list = []
        agent = _make_billing_agent(statuses)
        agent.client.chat.completions.create.side_effect = _make_402_billing_error()

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("do the work")

        assert agent.client.chat.completions.create.called
        assert result.get("failed") is True
        final_response = result.get("final_response") or ""
        assert "Billing or credits exhausted" not in final_response
        assert "средства" in final_response or "баланс" in final_response.lower()
    finally:
        i18n.reset_language_cache()


def test_billing_abort_does_not_send_the_same_sentence_twice(monkeypatch):
    """Финальное ревью, §1.1: на 402 клиент читал ОДНО И ТО ЖЕ дважды.

    Статус (``status_callback``) и ``final_response`` звали одну и ту же
    ``client_error_message(billing)``; ко второй копии был приклеен
    английский ``_billing_or_entitlement_message`` с советом
    ``/model <model> --provider <provider>`` — синтаксис с флагами, который
    в Telegram не набирается.

    Модульная проверка различимости живёт в
    ``tests/agent/test_trix_client_error_surface.py``
    (``TestTwoConsecutiveBillingMessagesAreDifferent``). Здесь — то, чего
    она не видит: что РЕАЛЬНЫЙ сайт в ``agent/conversation_loop.py``
    по-прежнему берёт вторую фразу из отдельного места. Мутация, вернувшая
    сайту старую склейку, модульные тесты прошла бы молча.
    """
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        statuses: list = []
        agent = _make_billing_agent(statuses)
        agent.client.chat.completions.create.side_effect = _make_402_billing_error()

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("do the work")

        assert agent.client.chat.completions.create.called
        assert result.get("failed") is True

        lifecycle_texts = [msg for kind, msg in statuses if kind == "lifecycle"]
        assert lifecycle_texts, "клиент не получил первого сообщения вовсе"
        final_response = (result.get("final_response") or "").strip()
        assert final_response, "клиент не получил второго сообщения вовсе"

        for status_text in lifecycle_texts:
            assert status_text.strip() != final_response, (
                f"два одинаковых сообщения подряд: {final_response!r}"
            )
            assert status_text.strip() not in final_response, (
                "вторая строка целиком повторяет первую и дописывает к ней "
                f"хвост: {final_response!r}"
            )

        lowered = final_response.lower()
        for marker in ("--provider", "/model <", "add credits", "entitlement"):
            assert marker not in lowered, (
                f"{marker!r} — английский хвост или невыполнимый совет "
                f"в тексте клиенту: {final_response!r}"
            )
    finally:
        i18n.reset_language_cache()


def _make_content_policy_error() -> Exception:
    """Provider-agnostic safety-filter refusal, no HTTP status (#18028 shape).

    Matches ``_CONTENT_POLICY_BLOCKED_PATTERNS`` in
    ``agent/error_classifier.py`` via plain text, independent of provider --
    the OpenAI Codex SDK case from #18028 raises exactly like this, with no
    ``status_code`` attribute at all.
    """
    return Exception(
        "This content was flagged for possible cybersecurity risk. If this "
        "seems wrong, try rephrasing your request."
    )


def test_content_policy_blocked_final_response_does_not_name_the_upstream_product():
    """Round-1 review: the exception-path content-policy final_response used
    to read "(not a Hermes/gateway failure)" -- a competing upstream brand
    name leaking into a Trix client's chat, independent of locale (the
    string is hardcoded English, not routed through the catalog at all).
    """
    statuses: list = []
    agent = _make_billing_agent(statuses)
    agent.client.chat.completions.create.side_effect = _make_content_policy_error()

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do the work")

    assert agent.client.chat.completions.create.called
    assert result.get("failed") is True
    final_response = result.get("final_response") or ""
    assert final_response.strip(), "content-policy block produced no final_response at all"
    assert "hermes" not in final_response.lower()
