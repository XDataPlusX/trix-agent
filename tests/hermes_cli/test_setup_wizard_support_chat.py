"""The support bot's chat turn (``hermes_cli/setup_wizard/support_chat.py``).

No real network call ever happens here -- every test that exercises
``run_chat_turn`` supplies a fake ``get_client`` boundary (the same shape
``get_text_auxiliary_client`` returns: ``(client, model)``), never a real
provider. ``hermes_cli/trix_support.py``'s own checks/fixes are already
covered by ``tests/hermes_cli/test_trix_support.py``; this file only tests
what's new: the closed-registry dispatch, the per-message action cap, the
brand guard, and context assembly.
"""

from __future__ import annotations

import json

import pytest

import hermes_cli.trix_support as ts
from hermes_cli.setup_wizard import support_chat as sc
from hermes_cli.setup_wizard import support_skill
from hermes_constants import get_hermes_home


# ---------------------------------------------------------------------------
# Fakes for the one boundary this module is allowed to touch: an
# OpenAI-chat-completions-shaped client.
# ---------------------------------------------------------------------------


class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, action_id):
        self.id = call_id
        self.function = _FakeFunction("run_support_action", json.dumps({"action_id": action_id}))


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeChoice:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeResponse:
    def __init__(self, message: _FakeMessage):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    """Replays a queue of canned responses, one per call.

    If the queue runs out, the last response repeats -- keeps tests short
    when a loop's exact call count isn't the thing under test.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake completions queue exhausted")
        resp = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(resp, Exception):
            raise resp
        return _FakeResponse(resp)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class _FakeClient:
    def __init__(self, responses):
        self.completions = _FakeCompletions(responses)
        self.chat = _FakeChat(self.completions)


def _client_with(responses):
    fake = _FakeClient(responses)
    return lambda task: (fake, "fake-model"), fake


def _text_reply(text: str) -> _FakeMessage:
    return _FakeMessage(content=text)


def _tool_call_reply(action_id: str, call_id: str = "call-1") -> _FakeMessage:
    return _FakeMessage(tool_calls=[_FakeToolCall(call_id, action_id)])


def _multi_tool_call_reply(action_ids) -> _FakeMessage:
    """A single assistant message offering several tool calls at once --
    real chat-completions responses can do this in one round-trip, so the
    per-message cap must hold *within* one message's tool_calls list, not
    just across separate completion rounds."""
    return _FakeMessage(
        tool_calls=[_FakeToolCall(f"call-{i}", action_id) for i, action_id in enumerate(action_ids)]
    )


# ---------------------------------------------------------------------------
# The security boundary: only a literal registry key, with an implemented
# handler, ever runs. This is the mutation target for acceptance criterion
# 2 ("Ни одно действие бота не исполняет строку, которую он сам составил").
# ---------------------------------------------------------------------------


class TestActionDispatchIsClosed:
    def test_unregistered_action_id_is_not_executed(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("trix_support._execute must never be called for an unknown id")

        monkeypatch.setattr(ts, "_execute", _boom)

        outcome = sc.execute_support_action("rm -rf /; echo pwned")
        assert outcome == {"executed": False, "reason": "unknown_action_id"}

    def test_non_string_action_id_is_not_executed(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("trix_support._execute must never be called for a non-string id")

        monkeypatch.setattr(ts, "_execute", _boom)

        outcome = sc.execute_support_action({"action_id": "doctor_fix"})
        assert outcome == {"executed": False, "reason": "unknown_action_id"}

    def test_unimplemented_action_is_not_executed(self, monkeypatch):
        """Инвариант, а не снимок: КАЖДОЕ действие без обработчика получает
        отказ, каким бы ни был состав реестра.

        Раньше здесь стояло имя `ensure_tool` — и тест покраснел в тот день,
        когда это действие реализовали, хотя проверяемое поведение не
        менялось. Имя в реестре — данные, которые мы намеренно меняем;
        правило «нереализованное не исполняется» — контракт.
        """
        unimplemented = [
            action_id
            for action_id, action in ts.SUPPORT_ACTIONS.items()
            if action.handler is None
        ]
        if not unimplemented:
            pytest.skip("в реестре не осталось нереализованных действий")

        def _boom(*a, **k):
            raise AssertionError("trix_support._execute must never be called for an unimplemented action")

        monkeypatch.setattr(ts, "_execute", _boom)

        for action_id in unimplemented:
            outcome = sc.execute_support_action(action_id)
            assert outcome == {"executed": False, "reason": "not_implemented"}, action_id

    def test_registered_implemented_action_is_executed_through_the_bounded_wrapper(self, monkeypatch):
        calls = []

        def _fake_execute(action_id, handler, timeout):
            calls.append((action_id, timeout))
            return ts.ActionRunResult(
                action_id=action_id, ok=True, error=None, detail={"ok": True},
                started_at="t0", finished_at="t1", duration_s=0.01,
            )

        monkeypatch.setattr(ts, "_execute", _fake_execute)

        outcome = sc.execute_support_action("proxy_syntax")
        assert outcome == {"executed": True, "ok": True, "error": None}
        assert calls == [("proxy_syntax", ts.SUPPORT_ACTIONS["proxy_syntax"].timeout_s)]

    def test_resolve_action_returns_none_for_anything_off_the_closed_list(self):
        assert sc.resolve_action("telegram_token") is not None
        assert sc.resolve_action("telegram_token; rm -rf /") is None
        assert sc.resolve_action("") is None
        assert sc.resolve_action(None) is None


# ---------------------------------------------------------------------------
# Brand guard: block, never quietly rewrite past the point of confidence.
# ---------------------------------------------------------------------------


class TestBrandGuard:
    def test_clean_reply_passes_through_untouched(self):
        assert sc.apply_brand_guard("Всё в порядке, бот отвечает.") == "Всё в порядке, бот отвечает."

    def test_known_safe_replacement_is_applied_and_cleared(self):
        result = sc.apply_brand_guard("Проблема в Hermes Agent, но мы её решили.")
        assert "Hermes" not in result
        assert "Trix Agent" in result

    def test_still_forbidden_after_replacement_regenerates_once(self, monkeypatch):
        fake = _FakeClient([_text_reply("Второй ответ тоже без запрещённых слов.")])
        monkeypatch.setattr(sc, "_apply_known_safe_replacements", lambda text: text)  # force "still dirty"

        result = sc.apply_brand_guard(
            "Nous Research упоминается тут.",
            client=fake,
            model="fake-model",
            conversation=[{"role": "user", "content": "hi"}],
        )
        assert result == "Второй ответ тоже без запрещённых слов."

    def test_still_forbidden_after_regeneration_falls_back_to_canned_string(self, monkeypatch):
        fake = _FakeClient([_text_reply("Опять Nous Research, простите.")])
        monkeypatch.setattr(sc, "_apply_known_safe_replacements", lambda text: text)  # force "still dirty"

        result = sc.apply_brand_guard(
            "Nous Research тут.",
            client=fake,
            model="fake-model",
            conversation=[{"role": "user", "content": "hi"}],
        )
        assert result == sc._MSG_CHAT_FALLBACK
        assert "Nous" not in result
        assert ts.SUPPORT_ESCALATION_CONTACT in result

    def test_no_client_available_for_regeneration_falls_back_directly(self, monkeypatch):
        monkeypatch.setattr(sc, "_apply_known_safe_replacements", lambda text: text)
        result = sc.apply_brand_guard("Nous Research тут.")
        assert result == sc._MSG_CHAT_FALLBACK


# ---------------------------------------------------------------------------
# Per-message action cap -- "на одно сообщение клиента — ограниченное число
# действий" (module docstring explains why 3).
# ---------------------------------------------------------------------------


class TestActionsPerMessageCap:
    def test_model_offering_actions_forever_is_capped(self, monkeypatch):
        executed = []

        def _fake_execute(action_id, handler, timeout):
            executed.append(action_id)
            return ts.ActionRunResult(
                action_id=action_id, ok=True, error=None, detail={"ok": True},
                started_at="t0", finished_at="t1", duration_s=0.01,
            )

        monkeypatch.setattr(ts, "_execute", _fake_execute)

        # The model keeps trying to call the tool no matter how many rounds
        # pass -- every response in the queue offers another tool call.
        responses = [_tool_call_reply("proxy_syntax", call_id=f"call-{i}") for i in range(10)]
        # ... but once tools are withheld (cap reached) a real chat model
        # would stop emitting tool_calls -- simulate that faithfully so the
        # loop's own "no tools offered -> no tool_calls" contract is real,
        # not just assumed by the fake.
        get_client, fake = _client_with(responses)

        original_create = fake.completions.create

        def create(**kwargs):
            if "tools" not in kwargs:
                return _FakeResponse(_text_reply("Готово, вот итог."))
            return original_create(**kwargs)

        fake.completions.create = create

        result = sc.run_chat_turn("run-1", "почини всё", [], get_client=get_client)

        assert len(executed) == sc.MAX_ACTIONS_PER_MESSAGE
        assert result.reply == "Готово, вот итог."
        assert len(result.actions_executed) == sc.MAX_ACTIONS_PER_MESSAGE

    def test_a_single_response_offering_more_tool_calls_than_the_cap_is_still_bounded(self, monkeypatch):
        """A single model message can carry several tool_calls in one shot
        (real chat-completions responses do this) -- the cap must hold
        *within* that one message's list, not only across separate
        completion rounds."""
        executed = []

        def _fake_execute(action_id, handler, timeout):
            executed.append(action_id)
            return ts.ActionRunResult(
                action_id=action_id, ok=True, error=None, detail={"ok": True},
                started_at="t0", finished_at="t1", duration_s=0.01,
            )

        monkeypatch.setattr(ts, "_execute", _fake_execute)

        # One response, five tool_calls at once -- well over the cap.
        action_ids = ["proxy_syntax"] * 5
        get_client, fake = _client_with([_multi_tool_call_reply(action_ids)])
        original_create = fake.completions.create

        def create(**kwargs):
            if "tools" not in kwargs:
                return _FakeResponse(_text_reply("Готово, вот итог."))
            return original_create(**kwargs)

        fake.completions.create = create

        result = sc.run_chat_turn("run-multi", "почини всё", [], get_client=get_client)

        assert len(executed) == sc.MAX_ACTIONS_PER_MESSAGE
        assert len(result.actions_executed) == sc.MAX_ACTIONS_PER_MESSAGE

    def test_attempted_calls_count_toward_the_cap_even_when_refused(self, monkeypatch):
        """An unknown/unimplemented action id still consumes budget -- a
        confused or adversarial model can't get unlimited free refusals
        instead of the cap ever kicking in."""

        def _boom(*a, **k):
            raise AssertionError("no implemented action was ever selected in this test")

        monkeypatch.setattr(ts, "_execute", _boom)

        responses = [_tool_call_reply("not-a-real-action", call_id=f"call-{i}") for i in range(10)]
        get_client, fake = _client_with(responses)
        original_create = fake.completions.create

        def create(**kwargs):
            if "tools" not in kwargs:
                return _FakeResponse(_text_reply("Не получилось найти подходящее действие."))
            return original_create(**kwargs)

        fake.completions.create = create

        result = sc.run_chat_turn("run-2", "почини", [], get_client=get_client)
        assert result.actions_executed == ()
        assert result.reply == "Не получилось найти подходящее действие."


# ---------------------------------------------------------------------------
# Model unavailable -> honest refusal, never a crash.
# ---------------------------------------------------------------------------


class TestModelUnavailable:
    def test_get_client_returning_none_is_an_honest_refusal(self):
        result = sc.run_chat_turn("run-3", "привет", [], get_client=lambda task: (None, None))
        assert result.reply == sc._MSG_CHAT_UNAVAILABLE
        assert ts.SUPPORT_ESCALATION_CONTACT in result.reply
        assert result.actions_executed == ()

    def test_get_client_raising_is_an_honest_refusal(self):
        def _raise(task):
            raise RuntimeError("no credentials configured")

        result = sc.run_chat_turn("run-4", "привет", [], get_client=_raise)
        assert result.reply == sc._MSG_CHAT_UNAVAILABLE

    def test_completion_call_failing_mid_turn_is_an_honest_refusal(self):
        get_client, _fake = _client_with([RuntimeError("upstream 500")])
        result = sc.run_chat_turn("run-5", "привет", [], get_client=get_client)
        assert result.reply == sc._MSG_CHAT_UNAVAILABLE


# ---------------------------------------------------------------------------
# Plain conversation (no tool call) -- reply passed through the brand guard,
# history extended correctly.
# ---------------------------------------------------------------------------


class TestPlainReply:
    def test_clean_text_reply_is_returned_and_history_extended(self):
        get_client, _fake = _client_with([_text_reply("Опишите, пожалуйста, что не работает.")])
        result = sc.run_chat_turn("run-6", "бот не отвечает", [], get_client=get_client)

        assert result.reply == "Опишите, пожалуйста, что не работает."
        assert result.history == (
            {"role": "user", "content": "бот не отвечает"},
            {"role": "assistant", "content": "Опишите, пожалуйста, что не работает."},
        )
        assert result.actions_executed == ()

    def test_empty_model_reply_falls_back_to_a_fixed_russian_sentence(self):
        get_client, _fake = _client_with([_text_reply("   ")])
        result = sc.run_chat_turn("run-7", "?", [], get_client=get_client)
        assert result.reply == sc._MSG_CHAT_EMPTY_REPLY


# ---------------------------------------------------------------------------
# Context assembly: last run's report + allowed actions, never the main
# bot's own conversation with the client.
# ---------------------------------------------------------------------------


class TestContextAssembly:
    def test_format_allowed_actions_lists_every_registry_entry(self):
        text = sc.format_allowed_actions()
        for action_id, action in ts.SUPPORT_ACTIONS.items():
            assert action_id in text
            assert action.label_ru in text

    def test_missing_run_report_says_so_instead_of_erroring(self):
        assert "недоступен" in sc.format_last_run_report("no-such-run-id")
        assert "недоступен" in sc.format_last_run_report(None)

    def test_run_report_reflects_a_real_write_internal_report_call(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        broken = ts.CheckOutcome(
            "telegram_token",
            ts.ActionRunResult(
                action_id="telegram_token", ok=False, error="токен не настроен",
                detail={"ok": False, "raw": "SECRET_INTERNAL_DETAIL_marker"},
                started_at="t0", finished_at="t1", duration_s=0.01,
            ),
            None, None, "not_fixed",
        )
        pass_result = ts.SupportPassResult(
            run_id="run-8", started_at="t0", finished_at="t1", checks=(broken,), ok=False,
        )
        run_id = ts.write_internal_report(pass_result)

        report = sc.format_last_run_report(run_id)
        assert ts.SUPPORT_ACTIONS["telegram_token"].label_ru in report
        assert "токен не настроен" in report
        # The short human "error" string is fine to forward to the model;
        # the raw internal detail payload is not.
        assert "SECRET_INTERNAL_DETAIL_marker" not in report

    def test_system_prompt_never_includes_a_marker_planted_outside_the_support_log(self, tmp_path, monkeypatch):
        """The support chat's context comes from exactly one file
        (``support/runs.jsonl``) and the persona/skill constants -- nothing
        that reads the client's conversation with the main bot exists in
        this module. A marker planted in a plausible gateway-session
        location must never surface in the assembled system prompt."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        gateway_dir = tmp_path / "gateway"
        gateway_dir.mkdir(parents=True)
        (gateway_dir / "sessions.db").write_text("SECRET_MAIN_BOT_CONVERSATION_MARKER", encoding="utf-8")

        prompt = sc.build_system_prompt(None)
        assert "SECRET_MAIN_BOT_CONVERSATION_MARKER" not in prompt

    def test_system_prompt_does_not_describe_desktop_or_terminal_features(self):
        """Per the brief: the support persona is a separate, small skill,
        not a trimmed copy of the full trix-agent skill, which describes
        the desktop app / panel / plugins / terminal the client doesn't
        have."""
        prompt = sc.build_system_prompt(None).lower()
        for word in ("desktop", "терминал", "панел", "плагин", "delegate_task"):
            assert word not in prompt

    def test_system_prompt_includes_persona_and_skill_and_actions(self):
        """Раньше здесь стояло `DEFAULT_SOUL_MD in prompt` — то есть тест
        ТРЕБОВАЛ общую персону продукта, которая обещает код и творческую
        работу. Он закреплял дефект как требование: бот с пятнадцатью
        починками не умеет ничего из обещанного, и это работало против
        переадресации посторонних просьб.

        Теперь проверяется контракт, а не состав: в промпте есть навык
        поддержки и список действий. Чем именно подаётся персона, тест не
        фиксирует — это проверяется отдельно, по свойству.
        """
        from hermes_cli.setup_wizard.support_skill import SUPPORT_SKILL_MD

        prompt = sc.build_system_prompt(None)
        assert SUPPORT_SKILL_MD in prompt
        assert "telegram_token" in prompt  # from format_allowed_actions


# ---------------------------------------------------------------------------
# is_chat_available() -- the pre-verdict availability probe support_view.py
# uses to pick the "not everything got fixed" verdict's own wording (owner,
# 2026-09-03: "проверяй доступность до показа вердикта, не после"). Must
# share run_chat_turn's own success condition and must never build a real
# SDK client / make a network call to answer the question.
# ---------------------------------------------------------------------------


class TestIsChatAvailable:
    def test_true_when_a_client_and_model_resolve(self):
        get_client, _fake = _client_with([_text_reply("hi")])
        assert sc.is_chat_available(get_client) is True

    def test_false_when_the_resolver_returns_no_client(self):
        assert sc.is_chat_available(lambda task: (None, None)) is False

    def test_false_when_the_resolver_returns_a_client_but_no_model(self):
        assert sc.is_chat_available(lambda task: (_FakeClient([]), None)) is False

    def test_false_when_the_resolver_raises(self):
        def _boom(task):
            raise RuntimeError("provider misconfigured")

        assert sc.is_chat_available(_boom) is False

    def test_resolves_inside_probe_mode_not_a_real_client_build(self):
        """Availability checks must go through
        ``agent.auxiliary_client.aux_probe_mode()`` -- the same mode
        Hermes's own tool-gating ``check_fn``s use (``tools/vision_tools.py``)
        -- so that checking "is the chat reachable" before every verdict
        never pays for a real SDK client construction or, for the Nous
        Portal branch, a live "recommended model" network fetch. A fake
        resolver that asserts probe mode is active when it's called is a
        stand-in for that guarantee without touching real provider config.
        """
        from agent.auxiliary_client import _aux_probe_active

        seen = {}

        def _resolver(task):
            seen["probe_active"] = _aux_probe_active()
            return _FakeClient([]), "fake-model"

        assert sc.is_chat_available(_resolver) is True
        assert seen["probe_active"] is True

    def test_run_chat_turn_does_not_resolve_inside_probe_mode(self):
        """The opposite guarantee for the sibling call path: a resolution
        whose client is actually going to be used for a completion call
        must NOT run inside probe mode, or it would get back a
        non-functional stub (aux_probe_mode's own contract) instead of a
        client capable of making the real chat.completions.create() call.
        """
        from agent.auxiliary_client import _aux_probe_active

        base_get_client, _fake = _client_with([_text_reply("ok")])
        seen = {}

        def _resolver(task):
            seen["probe_active"] = _aux_probe_active()
            return base_get_client(task)

        sc.run_chat_turn("run-1", "привет", (), get_client=_resolver)
        assert seen["probe_active"] is False


# ---------------------------------------------------------------------------
# Telegram-redirect guard -- Layer 2 ("стена") of the "send unrelated agent
# requests to the main Telegram bot" feature. Layer 1 (the persona's
# asymmetric "when in doubt, don't redirect" rule) lives in
# support_skill.py's prose and isn't independently testable without a real
# model call; what's tested here is the structural backstop that holds
# regardless of what the model believed: a reply carrying the exact
# redirect marker is never delivered while the last run's own
# telegram_token check isn't green, and the guard never touches a reply
# that doesn't carry the marker in the first place.
# ---------------------------------------------------------------------------


def _write_run_with_telegram_outcome(outcome: str, run_id: str = "run-telegram") -> str:
    """A real ``trix_support.write_internal_report`` call (never a hand-built
    JSON blob) producing exactly one ``telegram_token`` check with the given
    outcome -- the same on-disk shape ``support_chat._load_run_record``
    reads in production."""
    ok = outcome == "good"
    check = ts.CheckOutcome(
        "telegram_token",
        ts.ActionRunResult(
            action_id="telegram_token", ok=ok, error=None if ok else "бот не отвечает",
            detail={"ok": ok}, started_at="t0", finished_at="t1", duration_s=0.01,
        ),
        None, None, outcome,
    )
    pass_result = ts.SupportPassResult(
        run_id=run_id, started_at="t0", finished_at="t1", checks=(check,), ok=ok,
    )
    return ts.write_internal_report(pass_result)


_REDIRECT_REPLY = f"{support_skill.TELEGRAM_REDIRECT_MARKER} — напишите ему, пожалуйста."


class TestTelegramRedirectGuardUnit:
    """Direct, function-level coverage of the guard itself -- separate from
    the ``run_chat_turn`` integration tests below."""

    def test_reply_without_the_marker_passes_through_untouched(self):
        reply = "У вас, похоже, сломан прокси, я это чиню."
        assert sc.apply_telegram_redirect_guard(reply, run_id=None) == reply

    def test_marker_reply_blocked_when_no_run_id_at_all(self):
        result = sc.apply_telegram_redirect_guard(_REDIRECT_REPLY, run_id=None)
        assert result == sc._MSG_TELEGRAM_REDIRECT_BLOCKED
        assert support_skill.TELEGRAM_REDIRECT_MARKER not in result

    def test_marker_reply_blocked_when_run_id_has_no_record(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        result = sc.apply_telegram_redirect_guard(_REDIRECT_REPLY, run_id="no-such-run")
        assert result == sc._MSG_TELEGRAM_REDIRECT_BLOCKED

    def test_marker_reply_blocked_when_telegram_check_failed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        run_id = _write_run_with_telegram_outcome("not_fixed")
        result = sc.apply_telegram_redirect_guard(_REDIRECT_REPLY, run_id=run_id)
        assert result == sc._MSG_TELEGRAM_REDIRECT_BLOCKED
        assert support_skill.TELEGRAM_REDIRECT_MARKER not in result
        assert ts.SUPPORT_ESCALATION_CONTACT in result

    def test_marker_reply_passes_through_when_telegram_check_is_good(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        run_id = _write_run_with_telegram_outcome("good")
        assert sc.apply_telegram_redirect_guard(_REDIRECT_REPLY, run_id=run_id) == _REDIRECT_REPLY


class TestTelegramRedirectGuardIntegration:
    """Through the real ``run_chat_turn`` loop -- a fake model stands in for
    the persona's own decision (redirect vs. handle it here), and the guard
    is exercised exactly the way ``support_view.py`` triggers it in
    production: as post-processing on whatever the model actually said."""

    def test_broken_telegram_the_redirect_address_never_reaches_the_client(self, tmp_path, monkeypatch):
        """Required behavior 1: even though the model tried to redirect,
        the client never sees an address to write to -- because the last
        run proved that bot is unreachable."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        run_id = _write_run_with_telegram_outcome("not_fixed", run_id="run-broken")
        get_client, _fake = _client_with([_text_reply(_REDIRECT_REPLY)])

        result = sc.run_chat_turn(run_id, "напомни мне завтра позвонить в банк", [], get_client=get_client)

        assert support_skill.TELEGRAM_REDIRECT_MARKER not in result.reply
        assert result.reply == sc._MSG_TELEGRAM_REDIRECT_BLOCKED
        assert ts.SUPPORT_ESCALATION_CONTACT in result.reply

    def test_working_telegram_an_unrelated_request_is_redirected(self, tmp_path, monkeypatch):
        """Required behavior 2: with a working Telegram bot, a request for
        something only the main agent does (reminders) is allowed through
        to the client as a redirect."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        run_id = _write_run_with_telegram_outcome("good", run_id="run-working")
        get_client, _fake = _client_with([_text_reply(_REDIRECT_REPLY)])

        result = sc.run_chat_turn(run_id, "напомни мне завтра позвонить в банк", [], get_client=get_client)

        assert result.reply == _REDIRECT_REPLY
        assert support_skill.TELEGRAM_REDIRECT_MARKER in result.reply

    def test_malfunction_like_request_is_not_redirected_even_with_working_telegram(self, tmp_path, monkeypatch):
        """Required behavior 3: a report that sounds like a real breakage
        stays this bot's own job -- when the model (correctly, per the
        persona's asymmetric bias) answers without the redirect marker, the
        guard has nothing to do and the reply reaches the client
        unmodified, marker or Telegram-address free."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        run_id = _write_run_with_telegram_outcome("good", run_id="run-malfunction")
        own_reply = "Проверяю токен бота — подождите немного."
        get_client, _fake = _client_with([_text_reply(own_reply)])

        result = sc.run_chat_turn(run_id, "бот в Телеграме не отвечает вообще", [], get_client=get_client)

        assert result.reply == own_reply
        assert support_skill.TELEGRAM_REDIRECT_MARKER not in result.reply

    def test_malfunction_like_request_not_redirected_even_when_telegram_is_broken(self, tmp_path, monkeypatch):
        """Same as above, but for the broken-Telegram case too: nothing
        about a broken check forces the fallback sentence onto a reply that
        never tried to redirect in the first place."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        run_id = _write_run_with_telegram_outcome("not_fixed", run_id="run-malfunction-broken")
        own_reply = "Похоже, сломан токен бота, сейчас проверю ещё раз."
        get_client, _fake = _client_with([_text_reply(own_reply)])

        result = sc.run_chat_turn(run_id, "бот в Телеграме не отвечает вообще", [], get_client=get_client)

        assert result.reply == own_reply


class TestPromptDoesNotPromiseWhatTheBotLacks:
    """Промпт не должен обещать возможностей, которых у бота нет.

    Первая версия подставляла перед навыком общую персону продукта
    (``DEFAULT_SOUL_MD``) — а та говорит про «wide range of tasks
    including writing and editing code, creative work». Для бота с
    пятнадцатью починками из закрытого списка это ложь про самого себя, и
    она работает ПРОТИВ переадресации посторонних просьб: одна часть
    промпта разрешает то, что другая запрещает.

    Проверка держится за свойство («в промпте нет обещаний, которые бот не
    может выполнить»), а не за то, какие именно константы в него
    складываются.
    """

    def test_prompt_makes_no_promise_the_closed_action_list_cannot_keep(self):
        prompt = sc.build_system_prompt(None)
        for promise in ("creative work", "writing and editing code", "wide range of tasks"):
            assert promise not in prompt, promise

    def test_prompt_still_states_who_the_bot_is(self):
        """Обратная сторона: убрав общую персону, нельзя остаться вообще без
        персоны — иначе бот не знает, кто он и с кем говорит."""
        assert "бот технической поддержки" in sc.build_system_prompt(None)
