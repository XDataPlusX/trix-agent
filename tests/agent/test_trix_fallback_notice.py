"""Переход на запасного провайдера объясняется клиенту по-русски (Task 3).

Task 2 перевело сообщения об окончательном отказе провайдера. Этот модуль
всплывал уже после Task 2 в другом виде: сам переход на запасного, а не
финальный отказ, оставался английским в семи местах —
``agent/chat_completion_helpers.py`` (2 места) и
``agent/conversation_loop.py`` (5 мест). Механизм подачи не менялся:

- ``_pending_fallback_notice`` — одноразовое уведомление, всплывающее ровно
  один раз через ``_emit_pending_fallback_notice`` (run_agent.py) при
  **успешном** восстановлении;
- ``_buffer_status`` / ``_flush_status_buffer`` — буфер попыток, который
  выливается клиенту, только если ход в итоге провалился.

Мы меняем только язык (и, там где старый код путал причину — авторизация,
контент-фильтр, пустой/повреждённый ответ — раньше молча проваливался в
дефолтную ветку "rate limited" — точность формулировки), не саму подачу.

Обязательный интеграционный тест для строки, видимой при успехе, живёт в
``tests/run_agent/test_trix_fallback_success_notice.py`` — модульного тут
недостаточно (см. шапку того файла).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent import i18n
from agent.error_classifier import FailoverReason
from hermes_cli.trix_provider_errors import (
    client_fallback_attempt_message,
    client_fallback_message,
)


def _has_cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" for ch in text)


@pytest.fixture(autouse=True)
def _russian_language(monkeypatch):
    # t() resolves language from env > config.yaml > "en". tests/agent/ runs
    # against an isolated HERMES_HOME with no config.yaml, so without this
    # the catalog resolves to "en" and the Russian-text assertions below
    # would fail for the wrong reason (missing language pin, not a bug).
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    yield
    i18n.reset_language_cache()


# ── Brief's Step 1: the two tests spelled out in task-3-brief.md ──────────


def test_switch_notice_names_the_new_model_in_russian():
    msg = client_fallback_message("m1", "p1", "m2", "p2")
    assert "m2" in msg and "p2" in msg
    assert "Switched" not in msg
    assert _has_cyrillic(msg)


def test_switch_notice_also_names_the_old_model():
    # A client seeing the conversation suddenly answer from a different
    # model deserves to know what it switched FROM, not just what it's
    # paying for now.
    msg = client_fallback_message("old-model", "old-prov", "new-model", "new-prov")
    assert "old-model" in msg
    assert "old-prov" in msg


def test_notice_is_emitted_once_on_recovery(monkeypatch):
    """Инвариант механизма, а не наш текст: уведомление всплывает один раз.

    Мы не меняем ``_emit_pending_fallback_notice`` в этой задаче — тест
    подтверждает, что перевод текста не задел эту логику.
    """
    import run_agent

    seen = []

    class Fake:
        _pending_fallback_notice = "x"

        def _emit_status(self, m):
            seen.append(m)

        _vprint = lambda self, *a, **k: None
        log_prefix = ""

    fake = Fake()
    run_agent.AIAgent._emit_pending_fallback_notice(fake)
    run_agent.AIAgent._emit_pending_fallback_notice(fake)
    assert seen == ["x"], "уведомление должно всплыть ровно один раз"


# ── The seven call sites, exercised through the real activation path ──────


class TestTryActivateFallbackCallSite:
    """Drives the ACTUAL ``try_activate_fallback`` in
    ``agent/chat_completion_helpers.py`` (not a copy of its logic) so a
    regression at that exact call site — reverting to the English literal,
    or gluing extra text onto the client-facing message — fails here, not
    just in the module-level tests above. Mirrors the harness in
    ``tests/run_agent/test_provider_fallback.py``.
    """

    def _make_agent(self, statuses):
        from run_agent import AIAgent

        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                provider="openrouter",
                model="primary/model",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                fallback_model=[{"provider": "openai", "model": "gpt-4o-fallback"}],
                status_callback=lambda kind, message: statuses.append((kind, message)),
            )
        agent.client = MagicMock()
        return agent

    def _fb_client(self):
        fb = MagicMock()
        fb.api_key = "fb-key"
        fb.base_url = "https://api.openai.com/v1"
        fb._custom_headers = None
        fb.default_headers = None
        return fb

    def test_buffered_and_pending_notice_are_both_russian_and_name_the_new_model(self):
        statuses: list = []
        agent = self._make_agent(statuses)
        buffered: list = []
        agent._buffer_status = lambda m: buffered.append(m)

        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(self._fb_client(), "gpt-4o-fallback"),
            ),
            patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ),
        ):
            assert agent._try_activate_fallback() is True

        assert buffered, "буферизованная строка о попытке перехода отсутствует"
        assert "gpt-4o-fallback" in buffered[0]
        assert "switching to fallback" not in buffered[0].lower()

        notice = getattr(agent, "_pending_fallback_notice", None)
        assert notice, "одноразовое уведомление об успехе не выставлено"
        assert "gpt-4o-fallback" in notice
        assert "primary/model" in notice
        assert "Switched" not in notice
        assert _has_cyrillic(notice)
        # Round-3 review fix (point 6): the buffered attempt line and the
        # one-shot success notice are DELIBERATELY different text now.
        # The buffered line only reaches the client when this fallback
        # attempt ALSO ends up failing the whole turn -- rendering it with
        # the success wording ("отвечаю через запасного") would have the
        # client read a completed-transition claim immediately followed by
        # a failure message, on the same failed turn. See
        # TestSwitchAttemptMessageDistinctFromSuccessMessage in
        # tests/hermes_cli/test_trix_provider_errors.py for the module-level
        # version of this guarantee.
        assert buffered[0] != notice
        assert "отвечаю через" in notice
        assert "отвечаю через" not in buffered[0]


# ── Attempt-message branches added in Task 3 ───────────────────────────────


class TestNewAttemptBranches:
    """conversation_loop.py's auth-failover (~4926), content-filter-stream
    (~3361), and empty/malformed-response eager-fallback (~2943, ~7280)
    branches now route through ``client_fallback_attempt_message`` with a
    reason. Before this task none of the three had a dedicated branch in
    that function, so they silently rendered the rate-limited default.
    """

    _RATE_LIMIT_MARKER = "ограничил частоту"

    @pytest.mark.parametrize("reason", [FailoverReason.auth, FailoverReason.auth_permanent])
    def test_auth_failover_branch_reason(self, reason):
        msg = client_fallback_attempt_message(reason)
        assert _has_cyrillic(msg)
        assert self._RATE_LIMIT_MARKER not in msg

    def test_content_filter_branch_reason(self):
        msg = client_fallback_attempt_message(FailoverReason.content_policy_blocked)
        assert _has_cyrillic(msg)
        assert self._RATE_LIMIT_MARKER not in msg

    def test_empty_malformed_response_branch_sentinel(self):
        # Literal string sentinel used at the two empty/malformed-response
        # call sites, which never go through the FailoverReason classifier.
        msg = client_fallback_attempt_message("invalid_response")
        assert _has_cyrillic(msg)
        assert self._RATE_LIMIT_MARKER not in msg


# ── No leftover jargon / infeasible advice across the whole family ────────


class TestFallbackFamilyAvoidsTelegramInfeasibleContent:
    """Ни одна фраза семейства ``trix.errors.fallback.*`` не должна
    называть hermes, шелл-команды или советы, которые в Telegram
    невыполнимы -- требование из поправки к объёму Task 3. Проверяет ВСЕ
    ключи семейства разом, не по одному."""

    _FORBIDDEN_MARKERS = (
        "hermes",
        "sudo ",
        "chmod",
        "systemctl",
        "apt-get",
        "apt install",
        "pip install",
        "npm install",
        "$ ",
        "cd ~",
    )

    @staticmethod
    def _all_fallback_texts():
        texts = [client_fallback_message("m1", "p1", "m2", "p2")]
        for reason in (
            FailoverReason.upstream_rate_limit,
            FailoverReason.billing,
            FailoverReason.timeout,
            FailoverReason.server_error,
            FailoverReason.overloaded,
            FailoverReason.auth,
            FailoverReason.auth_permanent,
            FailoverReason.content_policy_blocked,
            FailoverReason.rate_limit,
            "invalid_response",
        ):
            texts.append(client_fallback_attempt_message(reason))
        texts.append(
            client_fallback_attempt_message(
                FailoverReason.upstream_rate_limit, upstream="OpenRouter"
            )
        )
        return texts

    def test_no_forbidden_markers_anywhere_in_the_family(self):
        for text in self._all_fallback_texts():
            lowered = text.lower()
            for marker in self._FORBIDDEN_MARKERS:
                assert marker not in lowered, f"{marker!r} found in: {text!r}"

    def test_every_text_is_russian_when_russian_is_active(self):
        for text in self._all_fallback_texts():
            assert _has_cyrillic(text), f"expected Russian text, got: {text!r}"
