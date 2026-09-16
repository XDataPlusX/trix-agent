"""Support section (spec 15) — the HTTP layer wired in
``hermes_cli/setup_wizard/support_view.py`` + one registration call in
``hermes_cli/setup_wizard/app.py``. The deterministic core itself
(``hermes_cli/trix_support.py`` — checks, fixes, ``build_client_report``,
``record_feedback``) already has its own test suite,
``tests/hermes_cli/test_trix_support.py``; nothing here re-tests that
module's own logic. This file only covers what changed for this task:
auth/Origin/closed-wizard gate reuse on the two new routes, the
single-flight lock, the exact client-response shape (never anything
beyond ``message``/``run_id``), feedback wiring, and the page's own
Russian/no-Hermes/no-Nous/single-escalation-contact copy.

``wizard_app`` / ``app_env`` / ``logged_in`` fixtures live in
``tests/hermes_cli/conftest.py`` (shared with the rest of
``test_setup_wizard_app_*.py``).
"""

from __future__ import annotations

import json
import re

import pytest

import hermes_cli.trix_support as ts
from hermes_constants import get_hermes_home


@pytest.fixture(autouse=True)
def _russian_language(monkeypatch):
    """The provider_key-reason messages (added for the "причина, а не факт"
    follow-up) route through ``trix_provider_errors.client_error_message``,
    which resolves through ``agent.i18n.t()`` — env > config.yaml > "en".
    ``DEFAULT_CONFIG`` (``hermes_cli/config_defaults.py``) already pins
    ``display.language: "ru"`` for a real Trix install, but these tests run
    against an isolated temp ``HERMES_HOME`` with no config.yaml at all —
    same reasoning, and the same fixture, as
    ``tests/hermes_cli/test_trix_provider_errors.py``'s own
    ``_russian_language``.
    """
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")


def _result(ok: bool, action_id: str = "x", error: str | None = None) -> ts.ActionRunResult:
    return ts.ActionRunResult(
        action_id=action_id,
        ok=ok,
        error=error if error is not None else (None if ok else "failed"),
        detail={"ok": ok},
        started_at="t0",
        finished_at="t1",
        duration_s=0.01,
    )


def _pass_result(*, ok: bool, checks: tuple, run_id: str = "fixed-run-id") -> ts.SupportPassResult:
    return ts.SupportPassResult(run_id=run_id, started_at="t0", finished_at="t1", checks=checks, ok=ok)


def _has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


def _assert_debranded_and_russian(text: str, *, label: str) -> None:
    assert "Hermes" not in text, f"{label}: still mentions Hermes: {text!r}"
    assert "Nous" not in text, f"{label}: still mentions Nous: {text!r}"
    assert _has_cyrillic(text), f"{label}: no Cyrillic in the Russian render: {text!r}"


# ---------------------------------------------------------------------------
# Entry point is reachable immediately after auth, and gated the same way
# as everything else.
# ---------------------------------------------------------------------------


def test_unauthenticated_get_support_is_401_and_never_renders(app_env, monkeypatch):
    client, _ = app_env
    from hermes_cli.setup_wizard import support_view as sv

    calls = []
    monkeypatch.setattr(sv, "render_support_page", lambda: calls.append(1) or "SHOULD-NOT-RENDER")

    r = client.get("/support")
    assert r.status_code == 401
    assert calls == []
    assert "SHOULD-NOT-RENDER" not in r.text


def test_unauthenticated_post_support_run_is_401_and_never_runs(app_env, monkeypatch):
    client, _ = app_env
    calls = []
    monkeypatch.setattr(ts, "run_support_pass", lambda: calls.append(1) or _pass_result(ok=True, checks=()))

    r = client.post("/api/support/run")
    assert r.status_code == 401
    assert calls == []


def test_unauthenticated_post_support_feedback_is_401_and_never_records(app_env, monkeypatch):
    client, _ = app_env
    calls = []
    monkeypatch.setattr(ts, "record_feedback", lambda *a, **k: calls.append((a, k)))

    r = client.post("/api/support/feedback", json={"run_id": "x", "helped": True})
    assert r.status_code == 401
    assert calls == []


def test_authenticated_get_support_succeeds(logged_in):
    r = logged_in.get("/support")
    assert r.status_code == 200
    assert "Проверить и починить" in r.text


def test_closed_wizard_returns_410_for_support_page(tmp_path, monkeypatch):
    """The closed-wizard gate must cover the new page too — an owner who
    disabled the wizard entirely must not leave the support door open."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.state import WizardState

    st = WizardState.load()
    st.issue_primary("trix-closedtest", "somepassword0000000000000000")
    st.set_disabled(True)
    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app

    client = TestClient(create_app())
    r = client.get("/support", auth=("trix-closedtest", "somepassword0000000000000000"))
    assert r.status_code == 410


def test_closed_wizard_returns_410_for_support_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.state import WizardState

    st = WizardState.load()
    st.issue_primary("trix-closedtest", "somepassword0000000000000000")
    st.set_disabled(True)
    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app

    client = TestClient(create_app())
    r = client.post(
        "/api/support/run",
        auth=("trix-closedtest", "somepassword0000000000000000"),
        headers={"Origin": "https://evil.example.com"},
    )
    # 410, never 403 -- the closed gate must win over the Origin guard too,
    # the same ordering app.py's own auth tests already prove for /api/submit.
    assert r.status_code == 410


# ---------------------------------------------------------------------------
# Origin guard is REUSED, not reimplemented -- both mutating routes live
# under /api/, which _OriginGuardMiddleware already covers uniformly.
# ---------------------------------------------------------------------------


def test_origin_guard_blocks_foreign_origin_on_support_run(logged_in):
    r = logged_in.post("/api/support/run", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_origin_guard_blocks_foreign_origin_on_support_feedback(logged_in):
    r = logged_in.post(
        "/api/support/feedback",
        json={"run_id": "x", "helped": True},
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_origin_guard_rejects_null_origin_on_support_run(logged_in):
    r = logged_in.post("/api/support/run", headers={"Origin": "null"})
    assert r.status_code == 403


def test_origin_guard_allows_missing_origin_on_support_run(logged_in, monkeypatch):
    monkeypatch.setattr(ts, "run_support_pass", lambda: _pass_result(ok=True, checks=()))
    r = logged_in.post("/api/support/run")
    assert r.status_code != 403


# ---------------------------------------------------------------------------
# Single-flight lock -- same shape as /api/submit's submit_lock.
# ---------------------------------------------------------------------------


def test_double_run_is_rejected_with_409(wizard_app):
    from fastapi.testclient import TestClient

    app, (login, pw) = wizard_app
    client = TestClient(app, base_url="https://testserver")
    client.auth = (login, pw)

    app.state.support_in_flight = True
    try:
        r = client.post("/api/support/run")
        assert r.status_code == 409
        assert "error" in r.json()
    finally:
        app.state.support_in_flight = False


def test_lock_is_released_after_a_run_completes(logged_in, monkeypatch):
    monkeypatch.setattr(ts, "run_support_pass", lambda: _pass_result(ok=True, checks=()))
    r1 = logged_in.post("/api/support/run")
    assert r1.status_code == 200
    r2 = logged_in.post("/api/support/run")
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Client-facing report shape -- message + run_id + resolved + chat_available,
# never a check id, a stage, or any internal detail. Acceptance criterion 3:
# verified at the point the outgoing HTTP response is assembled, not at the
# log-reading function. resolved/chat_available are structural facts (the
# client needs them to decide whether to ask "всё наладилось?" or open the
# chat automatically), not internal detail -- they never carry a check id, a
# stage name, or a log line.
# ---------------------------------------------------------------------------


def test_support_run_success_response_has_exactly_the_four_client_safe_fields(logged_in, monkeypatch):
    monkeypatch.setattr(ts, "run_support_pass", lambda: _pass_result(ok=True, checks=()))
    r = logged_in.post("/api/support/run")
    assert r.status_code == 200
    assert set(r.json().keys()) == {"message", "run_id", "resolved", "chat_available"}


def test_support_run_response_never_leaks_check_id_or_internal_error(logged_in, monkeypatch):
    secret_marker = "TRACEBACK_INTERNAL_ONLY_support_view_9f3a"
    broken = ts.CheckOutcome(
        "gateway_state",
        _result(False, action_id="gateway_state", error=secret_marker),
        None,
        None,
        "not_fixed",
    )
    monkeypatch.setattr(ts, "run_support_pass", lambda: _pass_result(ok=False, checks=(broken,)))

    r = logged_in.post("/api/support/run")
    assert r.status_code == 200
    body_text = json.dumps(r.json(), ensure_ascii=False)
    assert secret_marker not in body_text
    assert "gateway_state" not in body_text
    assert "detail" not in r.json()
    assert "checks" not in r.json()
    assert "stage" not in r.json()


def test_support_run_writes_full_detail_to_the_internal_log_only(logged_in, monkeypatch):
    """The client response is trimmed (see the test above); the internal
    JSONL file under HERMES_HOME must still carry the full detail this
    module never surfaces to the client -- this is our only telemetry
    channel per the brief."""
    secret_marker = "INTERNAL_ONLY_detail_marker_77a1"
    broken = ts.CheckOutcome(
        "gateway_state",
        _result(False, action_id="gateway_state", error=secret_marker),
        None,
        None,
        "not_fixed",
    )
    monkeypatch.setattr(ts, "run_support_pass", lambda: _pass_result(ok=False, checks=(broken,)))

    r = logged_in.post("/api/support/run")
    assert r.status_code == 200
    assert secret_marker not in json.dumps(r.json(), ensure_ascii=False)

    log_path = get_hermes_home() / "support" / "runs.jsonl"
    content = log_path.read_text(encoding="utf-8")
    assert secret_marker in content
    assert "gateway_state" in content


def test_support_run_message_matches_build_client_report(logged_in, monkeypatch):
    result = _pass_result(ok=True, checks=(ts.CheckOutcome("x", _result(True), None, None, "good"),))
    monkeypatch.setattr(ts, "run_support_pass", lambda: result)
    r = logged_in.post("/api/support/run")
    assert r.json()["message"] == ts.build_client_report(result)
    assert r.json()["run_id"] == result.run_id


# ---------------------------------------------------------------------------
# Feedback -- the only telemetry channel the product has.
# ---------------------------------------------------------------------------


def test_feedback_helped_true_is_recorded_and_correlated_by_run_id(logged_in, monkeypatch):
    monkeypatch.setattr(ts, "run_support_pass", lambda: _pass_result(ok=True, checks=(), run_id="run-abc"))
    run_r = logged_in.post("/api/support/run")
    run_id = run_r.json()["run_id"]
    assert run_id == "run-abc"

    r = logged_in.post("/api/support/feedback", json={"run_id": run_id, "helped": True})
    assert r.status_code == 200

    log_path = get_hermes_home() / "support" / "runs.jsonl"
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    feedback_records = [rec for rec in records if rec["type"] == "feedback"]
    assert len(feedback_records) == 1
    assert feedback_records[0]["run_id"] == "run-abc"
    assert feedback_records[0]["helped"] is True


def test_feedback_helped_false_is_recorded(logged_in, monkeypatch):
    monkeypatch.setattr(ts, "run_support_pass", lambda: _pass_result(ok=True, checks=(), run_id="run-xyz"))
    logged_in.post("/api/support/run")

    r = logged_in.post("/api/support/feedback", json={"run_id": "run-xyz", "helped": False})
    assert r.status_code == 200

    log_path = get_hermes_home() / "support" / "runs.jsonl"
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    feedback_records = [rec for rec in records if rec["type"] == "feedback"]
    assert feedback_records[-1]["helped"] is False


def test_feedback_with_blank_run_id_is_rejected(logged_in):
    r = logged_in.post("/api/support/feedback", json={"run_id": "   ", "helped": True})
    assert r.status_code == 400


def test_feedback_missing_run_id_is_a_422_validation_error(logged_in):
    r = logged_in.post("/api/support/feedback", json={"helped": True})
    assert r.status_code == 422
    # The shared _validation_exception_handler (app.py) never echoes input.
    assert "helped" not in json.dumps(r.json())


# ---------------------------------------------------------------------------
# Page copy -- no Hermes/Nous, Russian, single escalation contact, no other
# escalation channel (owner ruling: no email/form/phone).
# ---------------------------------------------------------------------------


def test_support_page_is_debranded_and_russian(logged_in):
    r = logged_in.get("/support")
    _assert_debranded_and_russian(r.text, label="/support page")


def test_support_page_names_the_single_escalation_contact_only(logged_in):
    r = logged_in.get("/support")
    assert ts.SUPPORT_ESCALATION_CONTACT in r.text
    lowered = r.text.lower()
    for other_channel in ("почт", "телефон", "звон"):
        assert other_channel not in lowered


def test_support_page_shows_no_step_list(logged_in):
    """Owner ruling: no advanced settings, no choice of checks, no
    step-by-step account -- none of the internal check ids may appear
    anywhere in the page's own content.

    The page now renders through the wizard's shared shell/stylesheet
    (spec 15 restyle -- one visual system instead of a second, bespoke
    one). That shared stylesheet carries ordinary English prose in its
    own CSS comments (developer-facing only, never rendered, invisible
    outside "view source") -- one of them happens to contain the word
    "browser", which also happens to be a check id
    (``trix_support.SUPPORT_ACTIONS``). That is a coincidental substring
    collision in dead CSS commentary, not a leaked check id, so the
    ``<style>`` block is stripped before searching; the underlying
    property (no check id in the page's actual content) is unchanged.
    """
    r = logged_in.get("/support")
    content = re.sub(r"<style>.*?</style>", "", r.text, flags=re.DOTALL)
    for check_id in ts.SUPPORT_ACTIONS:
        assert check_id not in content


def test_support_run_response_messages_are_debranded_and_russian(logged_in, monkeypatch):
    """Exercise all three of build_client_report's fixed sentences through
    the actual HTTP response, not just the pure function."""
    good = _pass_result(ok=True, checks=())
    fixed = _pass_result(
        ok=True,
        checks=(ts.CheckOutcome("doctor_no_fix", _result(False), _result(True), _result(True), "fixed"),),
    )
    not_fixed = _pass_result(
        ok=False,
        checks=(ts.CheckOutcome("telegram_token", _result(False), None, None, "not_fixed"),),
    )
    for result in (good, fixed, not_fixed):
        monkeypatch.setattr(ts, "run_support_pass", lambda result=result: result)
        r = logged_in.post("/api/support/run")
        _assert_debranded_and_russian(r.json()["message"], label=f"support run message ({result.ok})")


class TestSupportIsReachableFromTheWizard:
    """Раздел поддержки бесполезен, если до него нельзя дойти — но и
    бесполезен ДО первой настройки, когда чинить ещё нечего.

    Разбор спеки 15 назвал вход отдельным, «доступным сразу после
    аутентификации», а не шагом формы — но первая реализация повесила на
    него только мелкую подчёркнутую строку в подвале рейла, которую
    владелец продукта не смог найти на скриншоте. Отдельно от вёрстки, у
    входа есть настоящее условие видимости: до первого успешного «Готово»
    (``WizardState.is_completed()``) на машине ещё нет ни сохранённого
    токена бота, ни ключа провайдера — прогон проверок из /support упёрся
    бы только в их отсутствие, так что предлагать «почини» тому, кто
    ещё не закончил настройку, — гарантированный тупик, а не помощь.

    Проверка держится за это наблюдаемое свойство продукта («со страницы
    мастера есть путь в поддержку — и только после того, как настройка
    пройдена хотя бы раз»), а не за вёрстку: где именно стоит ссылка, как
    она подписана и какого она цвета, тест не фиксирует.
    """

    def test_wizard_page_offers_no_way_into_support_before_the_first_completed_setup(self, logged_in):
        r = logged_in.get("/")
        assert r.status_code == 200
        assert '"/support"' not in r.text

    def test_wizard_page_offers_a_way_into_support_after_the_first_completed_setup(self, logged_in):
        from hermes_cli.setup_wizard.state import WizardState

        WizardState.load().mark_completed()

        r = logged_in.get("/")
        assert r.status_code == 200
        assert '"/support"' in r.text

    def test_support_page_itself_does_not_send_the_client_back_into_the_form(self):
        """Обратная сторона того же требования: страница починки не должна
        предлагать «Готово», которое заново применяет настройки и
        перезапускает шлюз."""
        from hermes_cli.setup_wizard import support_view

        page = support_view.render_support_page()
        assert "/api/submit" not in page

    def test_support_page_offers_a_way_back_into_the_wizard(self, logged_in):
        """Owner feedback, 2026-09-03: "со страницы поддержки нет выхода".

        Симметрично входу в поддержку со стороны мастера (проверки выше в
        этом классе): раз на /support есть путь из мастера, с /support
        должен быть путь обратно — и именно на /, а не на форму
        «Готово»/``/api/submit`` (та связь уже проверена тестом выше).
        Держится за наблюдаемое свойство (путь на / присутствует), а не за
        вёрстку.
        """
        r = logged_in.get("/support")
        assert r.status_code == 200
        assert 'href="/"' in r.text


# ---------------------------------------------------------------------------
# Verdict wording for "не всё исправлено" depends on whether the chat can
# open -- checked BEFORE the verdict, never after (owner, 2026-09-03:
# "проверяй доступность до показа вердикта, не после"; "порядок текста
# сдаётся до попытки"). The escalation contact must never appear in the
# verdict text when a chat is about to open right under it, and must always
# appear -- honestly, immediately -- when it can't.
# ---------------------------------------------------------------------------


def _not_fixed_result(run_id: str = "not-fixed-run") -> ts.SupportPassResult:
    broken = ts.CheckOutcome("telegram_token", _result(False), None, None, "not_fixed")
    return _pass_result(ok=False, checks=(broken,), run_id=run_id)


def test_not_fixed_verdict_never_names_the_address_when_chat_is_available(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import support_chat as sc

    monkeypatch.setattr(ts, "run_support_pass", lambda: _not_fixed_result())
    monkeypatch.setattr(sc, "is_chat_available", lambda *a, **k: True)

    r = logged_in.post("/api/support/run")
    assert r.status_code == 200
    body = r.json()
    assert ts.SUPPORT_ESCALATION_CONTACT not in body["message"]
    assert body["resolved"] is False
    assert body["chat_available"] is True


def test_not_fixed_verdict_names_the_address_when_chat_is_unavailable(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import support_chat as sc

    monkeypatch.setattr(ts, "run_support_pass", lambda: _not_fixed_result())
    monkeypatch.setattr(sc, "is_chat_available", lambda *a, **k: False)

    r = logged_in.post("/api/support/run")
    assert r.status_code == 200
    body = r.json()
    assert ts.SUPPORT_ESCALATION_CONTACT in body["message"]
    assert body["resolved"] is False
    assert body["chat_available"] is False


def test_resolved_verdict_is_unaffected_by_chat_availability(logged_in, monkeypatch):
    """The two "everything's fine" outcomes must not change wording or
    gain/lose the address depending on chat availability -- only the third
    outcome (not_fixed) is availability-dependent."""
    from hermes_cli.setup_wizard import support_chat as sc

    good = _pass_result(ok=True, checks=())
    for chat_available in (True, False):
        monkeypatch.setattr(ts, "run_support_pass", lambda: good)
        monkeypatch.setattr(sc, "is_chat_available", lambda *a, **k: chat_available)
        r = logged_in.post("/api/support/run")
        body = r.json()
        assert body["message"] == ts.build_client_report(good)
        assert body["resolved"] is True
        assert body["chat_available"] is chat_available


def test_chat_availability_is_resolved_before_the_verdict_message_is_built(logged_in, monkeypatch):
    """Not just "the field is correct" -- the resolution must happen before
    the message is picked, not be derived from it afterward. Proven by
    making is_chat_available itself raise if called AFTER run_support_pass
    already returned; a correct implementation calls it in between."""
    from hermes_cli.setup_wizard import support_chat as sc

    call_order = []

    def fake_pass():
        call_order.append("run_support_pass")
        return _not_fixed_result()

    def fake_available(*a, **k):
        call_order.append("is_chat_available")
        return True

    monkeypatch.setattr(ts, "run_support_pass", fake_pass)
    monkeypatch.setattr(sc, "is_chat_available", fake_available)

    r = logged_in.post("/api/support/run")
    assert r.status_code == 200
    assert call_order.index("is_chat_available") > call_order.index("run_support_pass")
    # ... and strictly before the response is returned -- there is no third
    # call site, so if it ran at all it ran before build.
    assert call_order == ["run_support_pass", "is_chat_available"]


# ---------------------------------------------------------------------------
# provider_key-derived availability + reason-specific messaging (spec
# follow-up, owner 2026-09-03: "выведи доступность чата из результата
# прогона"; "причина, а не факт"). When the pass's OWN provider_key check is
# on the result, it is the source of truth -- support_chat.is_chat_available()
# must not even be called.
# ---------------------------------------------------------------------------


def _provider_key_outcome(detail: dict, outcome: str = "not_fixed") -> ts.CheckOutcome:
    initial = ts.ActionRunResult(
        action_id="provider_key",
        ok=bool(detail.get("ok")),
        error=None if detail.get("ok") else (detail.get("message") or "failed"),
        detail=detail,
        started_at="t0",
        finished_at="t1",
        duration_s=0.01,
    )
    return ts.CheckOutcome("provider_key", initial, None, None, outcome)


def _chat_available(monkeypatch, available):
    """Задать ответ на вопрос «можем ли мы сейчас позвать модель».

    Раньше здесь был `_forbid_is_chat_available`, запрещавший этот вызов:
    доступность выводилась из исхода проверки ключа. Живой прогон на чистой
    клиентской VM (2026-09-04) показал, что это неверно. Проверка отвечает
    «поломан ли НАСТРОЕННЫЙ ключ» и по контракту даёт «в порядке», когда не
    настроено ничего — для диагностики отсутствие настройки не поломка. В
    результате вердикт обещал «давайте разберёмся вместе», клиент писал
    сообщение и получал «чат не открывается».

    Теперь это два разных вопроса: доступность спрашивается отдельно, а
    исход проверки только НАЗЫВАЕТ ПРИЧИНУ, когда ключ настроен и отвергнут.
    """
    from hermes_cli.setup_wizard import support_chat as sc

    monkeypatch.setattr(sc, "is_chat_available", lambda *a, **k: available)


def test_working_key_gives_chat_available_true(logged_in, monkeypatch):
    _chat_available(monkeypatch, True)
    good_key = _provider_key_outcome(
        {"ok": True, "reachable": True, "message": "", "reason": None, "checked": True}, "good"
    )
    result = _pass_result(ok=True, checks=(good_key,))
    monkeypatch.setattr(ts, "run_support_pass", lambda: result)

    r = logged_in.post("/api/support/run")
    assert r.status_code == 200
    assert r.json()["chat_available"] is True


def test_rejected_key_message_points_at_the_wizard_never_at_support(logged_in, monkeypatch):
    _chat_available(monkeypatch, False)
    bad_key = _provider_key_outcome(
        {"ok": False, "reachable": True,
         "message": "Провайдер отклонил этот ключ. Проверьте его и попробуйте ещё раз.",
         "reason": "auth", "checked": True}
    )
    result = _pass_result(ok=False, checks=(bad_key,))
    monkeypatch.setattr(ts, "run_support_pass", lambda: result)

    r = logged_in.post("/api/support/run")
    assert r.status_code == 200
    body = r.json()
    assert body["chat_available"] is False
    assert "мастер" in body["message"].lower()
    assert ts.SUPPORT_ESCALATION_CONTACT not in body["message"]


def test_billing_failure_message_points_at_top_up_never_at_support(logged_in, monkeypatch):
    _chat_available(monkeypatch, False)
    bad_key = _provider_key_outcome(
        {"ok": False, "reachable": True, "message": "На счету провайдера не осталось средств.",
         "reason": "billing", "checked": True}
    )
    result = _pass_result(ok=False, checks=(bad_key,))
    monkeypatch.setattr(ts, "run_support_pass", lambda: result)

    r = logged_in.post("/api/support/run")
    assert r.status_code == 200
    body = r.json()
    assert body["chat_available"] is False
    assert "средств" in body["message"]
    assert ts.SUPPORT_ESCALATION_CONTACT not in body["message"]


def test_unreachable_provider_message_names_support_not_the_wizard(logged_in, monkeypatch):
    """The one reason that IS about the escalation contact -- task
    instruction: "это не про ключ, и адрес поддержки тут уместен"."""
    _chat_available(monkeypatch, False)
    bad_key = _provider_key_outcome(
        {"ok": False, "reachable": False,
         "message": "Не удалось связаться с провайдером для проверки ключа.",
         "reason": "network", "checked": True}
    )
    result = _pass_result(ok=False, checks=(bad_key,))
    monkeypatch.setattr(ts, "run_support_pass", lambda: result)

    r = logged_in.post("/api/support/run")
    assert r.status_code == 200
    body = r.json()
    assert body["chat_available"] is False
    assert ts.SUPPORT_ESCALATION_CONTACT in body["message"]


def test_reason_specific_messages_are_debranded_and_russian(logged_in, monkeypatch):
    cases = (
        {"ok": False, "reachable": True, "message": "x", "reason": "auth", "checked": True},
        {"ok": False, "reachable": True, "message": "x", "reason": "billing", "checked": True},
        {"ok": False, "reachable": False, "message": "x", "reason": "network", "checked": True},
        {"ok": False, "reachable": True, "message": "x", "reason": "other", "checked": True, "status_code": 500},
    )
    for detail in cases:
        result = _pass_result(ok=False, checks=(_provider_key_outcome(detail),))
        monkeypatch.setattr(ts, "run_support_pass", lambda result=result: result)
        r = logged_in.post("/api/support/run")
        _assert_debranded_and_russian(r.json()["message"], label=f"provider_key reason={detail['reason']}")


def test_provider_key_absent_falls_back_to_is_chat_available(logged_in, monkeypatch):
    """Defense in depth: a pass with no provider_key check at all (should
    not happen in production -- CHECK_ORDER always includes it) still
    resolves availability, via the old fallback path."""
    from hermes_cli.setup_wizard import support_chat as sc

    monkeypatch.setattr(ts, "run_support_pass", lambda: _pass_result(ok=True, checks=()))
    monkeypatch.setattr(sc, "is_chat_available", lambda *a, **k: False)

    r = logged_in.post("/api/support/run")
    assert r.status_code == 200
    assert r.json()["chat_available"] is False


# ---------------------------------------------------------------------------
# Server-side "no chat without a working key" gate (owner ruling; spec
# follow-up: "пока ключ не работает, чат не открывается вовсе"). The
# client-side chatAvailable flag alone is not a security boundary -- a
# direct POST to /api/support/chat must be refused too, before any provider
# call is attempted.
# ---------------------------------------------------------------------------


def test_chat_is_refused_after_a_run_with_a_broken_key(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import support_chat as sc

    bad_key = _provider_key_outcome(
        {"ok": False, "reachable": True, "message": "x", "reason": "auth", "checked": True}
    )
    result = _pass_result(ok=False, checks=(bad_key,), run_id="broken-key-run")
    monkeypatch.setattr(ts, "run_support_pass", lambda: result)
    run_r = logged_in.post("/api/support/run")
    assert run_r.json()["chat_available"] is False

    calls = []
    monkeypatch.setattr(sc, "run_chat_turn", lambda *a, **k: calls.append(1))

    r = logged_in.post("/api/support/chat", json={"run_id": "broken-key-run", "message": "бот не отвечает"})
    assert r.status_code == 200
    assert calls == [], "run_chat_turn must not run -- no provider call once the key is known broken"
    assert ts.SUPPORT_ESCALATION_CONTACT in r.json()["reply"]


def test_chat_is_refused_for_a_run_id_that_never_ran(logged_in, monkeypatch):
    """A run_id this process never saw (client skipped /api/support/run, or
    the process restarted) is refused, not silently allowed through -- no
    evidence it's safe is the safe default."""
    from hermes_cli.setup_wizard import support_chat as sc

    calls = []
    monkeypatch.setattr(sc, "run_chat_turn", lambda *a, **k: calls.append(1))

    r = logged_in.post("/api/support/chat", json={"run_id": "never-ran", "message": "привет"})
    assert r.status_code == 200
    assert calls == []


def test_chat_is_allowed_after_a_run_with_a_working_key(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import support_chat as sc

    # Рабочий ключ сам по себе чат не открывает — открывает возможность
    # позвать модель. На машине, где ничего не настроено, проверка ключа
    # тоже говорит «в порядке» (её контракт: отсутствие настройки не
    # поломка), и именно на этом раньше строился ложный «чат доступен».
    _chat_available(monkeypatch, True)

    good_key = _provider_key_outcome(
        {"ok": True, "reachable": True, "message": "", "reason": None, "checked": True}, "good"
    )
    result = _pass_result(ok=True, checks=(good_key,), run_id="good-key-run")
    monkeypatch.setattr(ts, "run_support_pass", lambda: result)
    run_r = logged_in.post("/api/support/run")
    assert run_r.json()["chat_available"] is True

    monkeypatch.setattr(
        sc, "run_chat_turn",
        lambda *a, **k: sc.ChatTurnResult(reply="Ок, разбираемся.", history=(), actions_executed=()),
    )
    r = logged_in.post("/api/support/chat", json={"run_id": "good-key-run", "message": "привет"})
    assert r.status_code == 200
    assert r.json()["reply"] == "Ок, разбираемся."


def test_escalation_contact_is_wired_to_the_permanent_line_under_chat(logged_in):
    """Owner feedback: "адрес живёт постоянной строкой под чатом, а не в
    тексте вердикта". The contact must be present specifically in the
    STRING ASSIGNED to the chat's own permanent line (#chatEscalation) --
    not merely present somewhere else on the page (e.g. the "Нет"-after-
    good-verdict fallback also names the same contact; a page that dropped
    it from the permanent chat line but kept it elsewhere must still fail
    this test)."""
    r = logged_in.get("/support")
    assert r.status_code == 200

    match = re.search(r"chatEscalationEl\.textContent = (\"(?:[^\"\\\\]|\\\\.)*\");", r.text)
    assert match is not None, "no assignment to chatEscalationEl.textContent found on the page"
    assigned_text = json.loads(match.group(1))
    assert ts.SUPPORT_ESCALATION_CONTACT in assigned_text


# ---------------------------------------------------------------------------
# Chat route -- auth/Origin reuse (same as /api/support/run and /feedback),
# graceful degradation without a model, and a client-response shape that
# never leaks anything beyond the reply text.
# ---------------------------------------------------------------------------


def test_unauthenticated_post_support_chat_is_401_and_never_runs(app_env, monkeypatch):
    client, _ = app_env
    from hermes_cli.setup_wizard import support_chat as sc

    calls = []
    monkeypatch.setattr(sc, "run_chat_turn", lambda *a, **k: calls.append(1))

    r = client.post("/api/support/chat", json={"run_id": "x", "message": "привет"})
    assert r.status_code == 401
    assert calls == []


def test_closed_wizard_returns_410_for_support_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.state import WizardState

    st = WizardState.load()
    st.issue_primary("trix-closedtest", "somepassword0000000000000000")
    st.set_disabled(True)
    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app

    client = TestClient(create_app())
    r = client.post(
        "/api/support/chat",
        auth=("trix-closedtest", "somepassword0000000000000000"),
        json={"run_id": "x", "message": "привет"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 410


def test_origin_guard_blocks_foreign_origin_on_support_chat(logged_in):
    r = logged_in.post(
        "/api/support/chat",
        json={"run_id": "x", "message": "привет"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_support_chat_without_a_model_still_returns_200_with_an_honest_refusal(logged_in, monkeypatch):
    """Acceptance criterion 6: an unreachable provider key must not break
    the page -- the chat honestly explains it can't open and names the one
    allowed escalation contact, over a normal 200 response."""
    from hermes_cli.setup_wizard import support_chat as sc

    monkeypatch.setattr(sc, "get_text_auxiliary_client", lambda task: (None, None))

    r = logged_in.post("/api/support/chat", json={"run_id": "x", "message": "бот не отвечает"})
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"reply"}
    assert ts.SUPPORT_ESCALATION_CONTACT in body["reply"]


def test_support_chat_response_never_exceeds_the_reply_field(logged_in, monkeypatch, wizard_app):
    from hermes_cli.setup_wizard import support_chat as sc

    app, _ = wizard_app
    app.state.support_chat_allowed["x"] = True
    monkeypatch.setattr(
        sc, "run_chat_turn",
        lambda *a, **k: sc.ChatTurnResult(reply="Проверка завершена.", history=(), actions_executed=("proxy_syntax",)),
    )
    r = logged_in.post("/api/support/chat", json={"run_id": "x", "message": "привет"})
    assert r.status_code == 200
    assert set(r.json().keys()) == {"reply"}
    assert "proxy_syntax" not in json.dumps(r.json())


def test_support_chat_rejects_an_empty_message(logged_in):
    r = logged_in.post("/api/support/chat", json={"run_id": "x", "message": "   "})
    assert r.status_code == 400


def test_support_chat_missing_fields_is_a_422_validation_error(logged_in):
    r = logged_in.post("/api/support/chat", json={"message": "привет"})
    assert r.status_code == 422


def test_support_chat_history_persists_across_calls_for_the_same_run_id(logged_in, monkeypatch, wizard_app):
    from hermes_cli.setup_wizard import support_chat as sc

    app, _ = wizard_app
    app.state.support_chat_allowed["run-persist"] = True
    seen_histories = []

    def fake_run_chat_turn(run_id, message, history, **kwargs):
        seen_histories.append(list(history))
        new_history = list(history) + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": f"ответ {len(seen_histories)}"},
        ]
        return sc.ChatTurnResult(reply=f"ответ {len(seen_histories)}", history=tuple(new_history), actions_executed=())

    monkeypatch.setattr(sc, "run_chat_turn", fake_run_chat_turn)

    r1 = logged_in.post("/api/support/chat", json={"run_id": "run-persist", "message": "первое"})
    assert r1.json()["reply"] == "ответ 1"
    r2 = logged_in.post("/api/support/chat", json={"run_id": "run-persist", "message": "второе"})
    assert r2.json()["reply"] == "ответ 2"

    assert seen_histories[0] == []
    assert seen_histories[1] == [
        {"role": "user", "content": "первое"},
        {"role": "assistant", "content": "ответ 1"},
    ]


def test_unconfigured_machine_does_not_promise_a_chat(logged_in, monkeypatch):
    """Машина, на которой провайдер не настроен вовсе, не должна звать в чат.

    Найдено исполнением на чистой клиентской VM (2026-09-04). Проверка ключа
    вернула «good» — её контракт считает отсутствие настройки не поломкой, —
    и вердикт позвал клиента разбираться вместе. Клиент писал сообщение и
    получал «чат не открывается, напишите в поддержку»: тот самый тупик,
    который весь раздел и убирает, просто на шаг позже.

    Проверяется наблюдаемое: когда модель позвать нельзя, ответ НЕ обещает
    разговора и называет адрес поддержки.
    """
    _chat_available(monkeypatch, False)
    good_key = _provider_key_outcome(
        {"ok": True, "reachable": True, "message": "", "reason": None, "checked": True}, "good"
    )
    result = _pass_result(ok=False, checks=(good_key,))
    monkeypatch.setattr(ts, "run_support_pass", lambda: result)

    body = logged_in.post("/api/support/run").json()
    assert body["chat_available"] is False
    assert "разберёмся вместе" not in body["message"]
    assert "@Trix_Agent_Support_Bot" in body["message"]
