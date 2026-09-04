"""Submit orchestration (spec §10): validate → apply → install → restart → prove.

Order is fixed by spec §10 and MUST NOT be reordered: any failing step
leaves the wizard open with a precise Russian message, and nothing past
that step runs. ``logged_in`` / ``app_env`` fixtures live in
``tests/hermes_cli/conftest.py``.

There is no self-extinguish step any more (spec 8, §5): a successful
submit stays open and reachable — see
``tests/hermes_cli/test_setup_wizard_app_auth.py`` and
``hermes_cli/setup_wizard/app.py``'s ``_run_submit`` docstring for why.
"""
from __future__ import annotations

import pytest

# As Task 7's FORM (tests/hermes_cli/test_setup_wizard_apply.py), extended
# with every other optional key apply_settings understands (spec §10.2 /
# task-7-brief) so this exercises the full submit body, not just the
# minimal one.
GOOD_FORM = {
    "telegram_token": "123:abc",
    "allowed_users": "111,222",
    "proxy": "",
    "provider": {
        "name": "openrouter",
        "env_var": "OPENROUTER_API_KEY",
        "api_key": "sk-or-test",
        "base_url": "",
        "model": "z-ai/glm-5.2",
    },
    "fallback": {
        "name": "zai",
        "env_var": "GLM_API_KEY",
        "api_key": "glm-test",
        "base_url": "",
        "model": "glm-5.2",
    },
    # "ddgs" — the real live catalog value for DuckDuckGo (see
    # tools_view.py's TITLES_RU / _catalog()); search_backend now goes
    # through the same closed-catalog validation search_env.key already
    # did, so a placeholder like the old "duckduckgo" would 422.
    "search_backend": "ddgs",
    "search_env": None,
    "browser_backend": "chromium",
    # Часовой пояс обязателен с спеки 11 — «хорошая форма» теперь включает
    # его так же, как токен бота.
    "timezone": "Europe/Moscow",
    "tts_voice": "ru-RU-SvetlanaNeural",
    # {} (not None): the finding-5/7 clear-signal contract reserves an
    # explicit `null` for "delete HASS_TOKEN/HASS_URL" — a form that isn't
    # touching Home Assistant at all must send the no-op default instead
    # (see app.py's `_SubmitBody` docstring / apply.py's own docstring).
    "hass": {},
}


def _ok_stack(monkeypatch, wapp):
    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"})
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    monkeypatch.setattr(wapp, "apply_settings", lambda f: {"ok": True, "written": [], "errors": []})
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"})
    # A successful apply ALWAYS reaches the install stage next (app.py's
    # _run_submit runs _pending_tool_installs/_run_tool_install_with_timeout
    # unconditionally between apply and restart — even a test that only
    # cares about a later stage, e.g. a restart/liveness failure, still
    # passes through it first). Without this, GOOD_FORM's own
    # search_backend "ddgs" — or whatever browser_backend/tool_provider a
    # caller sets afterward — gets checked against the REAL live catalog,
    # and a genuinely not-yet-installed row (Camofox on most dev machines;
    # even GOOD_FORM's own "ddgs" row is not-yet-installed on one without
    # the ``ddgs`` Python package) spawns a REAL installer (npm/pip)
    # against the checkout's own working tree. That is exactly how a
    # `pytest` run left package-lock.json dirty. A fake, no-op-selecting
    # catalog plus a fail-loud install stub matches every "Install stage"
    # test below; a caller that needs a SPECIFIC catalog/install behavior
    # overrides one or both again after calling this helper, same as those
    # tests already do — see _real_catalog_no_real_install() for callers
    # that need the real catalog back (a legal search/extract/tool_provider
    # value the fake catalog doesn't carry, or the "Finding 1/4" block
    # below, whose whole point IS the real catalog).
    monkeypatch.setattr(wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({}))
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: pytest.fail(f"unexpected real install stage reached: {key!r}")
    )


def _real_catalog_no_real_install(monkeypatch, wapp):
    """Undo `_ok_stack`'s fake, empty catalog for a caller that needs the
    REAL live catalog to recognize the specific backend/env value its own
    form carries (a legal search/extract/tool_provider value that only
    exists on the real catalog — `_ok_stack`'s minimal fake one 422s it
    instead — or, in the "Finding 1/4" block below, the invariant itself
    IS about the real catalog).

    `run_tool_install` gets a plain no-op stub rather than `_ok_stack`'s
    fail-loud one: GOOD_FORM's own `search_backend: "ddgs"` legitimately
    matches a real, not-yet-installed row on a dev machine without the
    ``ddgs`` Python package (unrelated to `_ok_stack`'s own doc comment's
    Camofox example, but the same shape) — a real pip install here is
    exactly the side effect this must not trigger, but that selection
    itself isn't a bug worth failing loud over."""
    from hermes_cli.setup_wizard.tools_view import wizard_tool_blocks as _real_wizard_tool_blocks

    monkeypatch.setattr(wapp, "wizard_tool_blocks", _real_wizard_tool_blocks)
    monkeypatch.setattr(wapp, "run_tool_install", lambda key: {"ok": True, "message": "test no-op"})


def _mock_support_pass(monkeypatch):
    """Stand in for a live ``trix_support.run_support_pass()`` (spec 15
    -- ``_run_submit`` now runs it once at the end of a successful
    "Готово", see ``_run_post_submit_support_pass`` in app.py).

    The ``wizard_app`` fixture (``tests/hermes_cli/conftest.py``) already
    defaults this for every test built through it/``app_env``/
    ``logged_in`` -- but a few return-mode tests below build their own
    ``TestClient(create_app())`` directly (they need specific
    config.yaml/.env content on disk before the app object even exists,
    which ``wizard_app`` doesn't parametrize), so they never go through
    that fixture and must mock it themselves. Left unmocked, the pass
    would make REAL network/subprocess calls (up to 15s per check, with a
    genuine ``hermes gateway restart`` as one check's own fix action) on
    every one of these tests -- slow, non-hermetic, and exactly what
    ``trix_support.py``'s own test suite (``test_trix_support.py``) mocks
    at its OWN, lower boundaries instead of stubbing ``run_support_pass``
    itself, which is why this helper is scoped to these specific tests
    rather than a suite-wide autouse fixture that would silently shadow
    that file's real subject under test."""
    from hermes_cli import trix_support as ts

    monkeypatch.setattr(
        ts,
        "run_support_pass",
        lambda: ts.SupportPassResult(
            run_id="test-return-mode-support-pass",
            started_at="t0",
            finished_at="t1",
            checks=(),
            ok=True,
        ),
    )


def test_validation_error_writes_nothing(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": False, "error": "Токен неверный"}
    )
    applied = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: applied.append(f))
    r = logged_in.post("/api/submit", json=GOOD_FORM)
    assert r.status_code == 422 and applied == []


def test_success_marks_completed(logged_in, monkeypatch):
    """Spec 8, §4.3/§5: a successful submit records ``completed`` (the
    "first-run form has been submitted once" hint for return-mode
    prefill) but does NOT close the wizard any more — it stays open and
    reachable at the same address/credentials."""
    from hermes_cli.setup_wizard import app as wapp
    from hermes_cli.setup_wizard.state import WizardState

    _ok_stack(monkeypatch, wapp)
    r = logged_in.post("/api/submit", json=GOOD_FORM)
    assert r.json()["ok"] is True
    assert WizardState.load().is_completed() is True
    assert WizardState.load().is_open() is True


def test_dead_bot_keeps_wizard_open(logged_in, monkeypatch):
    """Мутационно-защищённый инвариант §15.6: сохранение с неотвечающим
    ботом НЕ гасит мастер."""
    from hermes_cli.setup_wizard import app as wapp
    from hermes_cli.setup_wizard.state import WizardState

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp, "wait_bot_alive", lambda *a, **k: {"ok": False, "error": "бот не отвечает"}
    )
    r = logged_in.post("/api/submit", json=GOOD_FORM)
    assert r.json()["ok"] is False and r.json()["stage"] == "liveness"
    assert WizardState.load().is_open() is True


def test_submit_never_logs_secrets(logged_in, monkeypatch, caplog):
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    with caplog.at_level("DEBUG"):
        logged_in.post("/api/submit", json=GOOD_FORM)
    assert GOOD_FORM["telegram_token"] not in caplog.text
    assert GOOD_FORM["provider"]["api_key"] not in caplog.text
    assert GOOD_FORM["fallback"]["api_key"] not in caplog.text


def test_wait_bot_alive_receives_both_restart_snapshots(logged_in, monkeypatch):
    """Критическая заметка ревью: только pre_pid молча ослабляет
    доказательство — оба снапшота из restart_gateway обязаны дойти до
    wait_bot_alive."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    monkeypatch.setattr(wapp, "apply_settings", lambda f: {"ok": True, "written": [], "errors": []})
    monkeypatch.setattr(
        wapp,
        "restart_gateway",
        lambda: {
            "ok": True,
            "message": "",
            "pre_pid": 4242,
            "pre_platform_stamp": "2026-08-18T00:00:00Z",
        },
    )
    calls = []

    def _fake_wait_bot_alive(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True, "username": "trixbot"}

    monkeypatch.setattr(wapp, "wait_bot_alive", _fake_wait_bot_alive)
    # Install stage guard (bug class fixed 2026-09-02) — see _ok_stack's
    # own comment for why a successful apply always reaches this stage.
    monkeypatch.setattr(wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({}))
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: pytest.fail(f"unexpected real install stage reached: {key!r}")
    )

    r = logged_in.post("/api/submit", json=GOOD_FORM)

    assert r.json()["ok"] is True
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["pre_pid"] == 4242
    assert kwargs["pre_platform_stamp"] == "2026-08-18T00:00:00Z"


def test_reset_tool_cache_called_after_successful_apply(logged_in, monkeypatch):
    """installed-вердикты зависят от env/config — a successful apply must
    invalidate the cached tool catalog (see app.py's _cached_tool_blocks).

    GOOD_FORM's `search_backend: "ddgs"` matches a REAL row of the live
    "web" catalog (unlike the old placeholder "duckduckgo") — mock
    `wizard_tool_blocks` with that row already `installed: True` (like
    every install-stage test below does) so the install stage has nothing
    to do here and the call count stays exactly the ONE this test is
    actually about.
    """
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp,
        "wizard_tool_blocks",
        lambda: _fake_tool_blocks(
            {"web": [_fake_row("DuckDuckGo (ddgs)", web_backend="ddgs", installed=True)]}
        ),
    )
    calls = []
    monkeypatch.setattr(wapp, "reset_tool_cache", lambda app: calls.append(app))

    r = logged_in.post("/api/submit", json=GOOD_FORM)

    assert r.json()["ok"] is True
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Install stage (owner ruling 2026-08-24, "Установка инструментов — кнопки
# нет" — docs/product/specs/2026-08-23-wizard-content-decisions.md): there
# is no standalone "Установить" button/route any more. /api/submit runs the
# install hook for whichever catalog rows THIS submission's own choices
# select, between apply and restart — see app.py's
# _pending_tool_installs/_run_tool_install_with_timeout. None of these
# tests let a real installer run — wapp.wizard_tool_blocks and
# wapp.run_tool_install are always mocked.
# ---------------------------------------------------------------------------


def _fake_tool_blocks(rows_by_category: dict) -> list[dict]:
    """Build a fake ``wizard_tool_blocks()`` catalog for a test's chosen
    categories, PLUS a default "web" block carrying just "ddgs" (matching
    GOOD_FORM's own ``search_backend: "ddgs"``) — search_backend is now
    validated against the live "web" catalog (same closed-catalog
    discipline ``search_env.key`` already had), so any test that swaps in
    a fake catalog without a "web" entry at all would otherwise 422 on
    GOOD_FORM's own default. A caller that wants to test "web"/
    "web_extract" specifically can still override it by passing its own
    "web"/"web_extract" key in ``rows_by_category``.
    """
    merged = {"web": [_fake_row("DuckDuckGo (ddgs)", web_backend="ddgs", recommended=True)]}
    merged.update(rows_by_category)
    return [
        {"category": category, "title_ru": category, "rows": rows}
        for category, rows in merged.items()
    ]


def _fake_row(name: str, **overrides) -> dict:
    row = {
        "name": name,
        "badge": "",
        "tag": "",
        "env_vars": [],
        "post_setup": None,
        "recommended": False,
        "installed": False,
        "backend_key": None,
        "web_backend": None,
        "provider_key": None,
    }
    row.update(overrides)
    return row


def test_install_stage_runs_between_apply_and_restart(logged_in, monkeypatch):
    """Order is fixed: validate -> apply -> install -> restart -> prove.
    A selected, not-yet-installed row's hook must run strictly after
    apply_settings and strictly before restart_gateway/wait_bot_alive."""
    from hermes_cli.setup_wizard import app as wapp

    order = []
    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"})
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    monkeypatch.setattr(
        wapp, "apply_settings", lambda f: order.append("apply") or {"ok": True, "written": [], "errors": []}
    )
    monkeypatch.setattr(
        wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({"browser": [_fake_row("Local Browser", post_setup="agent_browser", backend_key="off")]})
    )
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: order.append("install") or {"ok": True, "message": "Установлено."}
    )
    monkeypatch.setattr(wapp, "restart_gateway", lambda: order.append("restart") or {"ok": True, "message": ""})
    monkeypatch.setattr(
        wapp, "wait_bot_alive", lambda *a, **k: order.append("liveness") or {"ok": True, "username": "trixbot"}
    )

    form = dict(GOOD_FORM)
    form["browser_backend"] = "off"

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True, r.json()
    assert order == ["apply", "install", "restart", "liveness"]


def test_install_stage_runs_for_selected_uninstalled_row(logged_in, monkeypatch):
    """A row whose backend_key matches form.browser_backend AND isn't
    already installed gets its post_setup hook run, and the response
    carries an empty tool_install_failures on success."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp,
        "wizard_tool_blocks",
        lambda: _fake_tool_blocks({"browser": [_fake_row("Local Browser", post_setup="agent_browser", backend_key="off")]}),
    )
    calls = []
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: calls.append(key) or {"ok": True, "message": "Установлено."}
    )

    form = dict(GOOD_FORM)
    form["browser_backend"] = "off"

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True
    assert calls == ["agent_browser"]
    assert r.json()["tool_install_failures"] == []


def test_apply_warnings_surface_on_success(logged_in, monkeypatch):
    """Finding 2 (review 2026-08-26): apply_settings()'s non-fatal
    `warnings` (today, only the "extract backend picked with no usable
    key" notice — see apply.py's own docstring) must reach the success
    response verbatim, same "always present" contract as
    tool_install_failures — the client's success-screen renders these in
    #apply-warning-notice (page.py)."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp,
        "apply_settings",
        lambda f: {
            "ok": True,
            "written": [],
            "errors": [],
            "warnings": ["Чтение страниц: источник выбран, но подходящего ключа нет — настройка не сохранена."],
        },
    )

    r = logged_in.post("/api/submit", json=GOOD_FORM)

    assert r.json()["ok"] is True
    assert r.json()["warnings"] == [
        "Чтение страниц: источник выбран, но подходящего ключа нет — настройка не сохранена."
    ]


def test_apply_warnings_default_to_empty_list(logged_in, monkeypatch):
    """A caller (real apply_settings today) that returns no "warnings" key
    at all — same defensive default tool_install_failures's own docstring
    already relies on — must still surface an empty list, never crash on
    `.get`."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(wapp, "apply_settings", lambda f: {"ok": True, "written": [], "errors": []})

    r = logged_in.post("/api/submit", json=GOOD_FORM)

    assert r.json()["ok"] is True
    assert r.json()["warnings"] == []


def test_support_check_message_matches_build_client_report_on_success(logged_in, monkeypatch):
    """Spec 15's own machine-health pass now also runs once at the end of
    a successful "Готово" (app.py's ``_run_post_submit_support_pass``) --
    the SAME mechanism ``support_view.py``'s ``POST /api/support/run``
    already drives. The response carries exactly one of
    ``build_client_report()``'s three fixed sentences, always present,
    same "always present" contract as ``tool_install_failures``/
    ``warnings`` above."""
    from hermes_cli import trix_support as ts
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    result = ts.SupportPassResult(run_id="ok-run", started_at="t0", finished_at="t1", checks=(), ok=True)
    monkeypatch.setattr(ts, "run_support_pass", lambda: result)

    r = logged_in.post("/api/submit", json=GOOD_FORM)

    assert r.json()["ok"] is True
    assert r.json()["support_check_message"] == ts.build_client_report(result)


def test_bad_support_verdict_does_not_fail_the_submission(logged_in, monkeypatch):
    """Owner ruling: by the time this pass runs, settings are already
    saved, the gateway has already restarted, and the bot has already
    answered a live probe -- there is nothing left to roll back, so a bad
    verdict from the end-of-submit health pass is reported on the success
    screen, never turned into a failed submission. Same posture as a
    failed tool install or an apply warning (see the tests above)."""
    from hermes_cli import trix_support as ts
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    broken = ts.CheckOutcome(
        "telegram_token",
        ts.ActionRunResult(
            action_id="telegram_token",
            ok=False,
            error="боту не ответить",
            detail={},
            started_at="t0",
            finished_at="t1",
            duration_s=0.01,
        ),
        None,
        None,
        "not_fixed",
    )
    bad_result = ts.SupportPassResult(
        run_id="bad-run", started_at="t0", finished_at="t1", checks=(broken,), ok=False
    )
    monkeypatch.setattr(ts, "run_support_pass", lambda: bad_result)

    r = logged_in.post("/api/submit", json=GOOD_FORM)

    assert r.json()["ok"] is True
    assert r.json()["support_check_message"] == ts.build_client_report(bad_result)


def test_support_pass_crash_does_not_fail_the_submission(logged_in, monkeypatch):
    """A same-process exception inside ``run_support_pass()`` (a bug, an
    unexpected host condition) must degrade to "no extra message", never
    a 500 on an otherwise complete ``/api/submit`` -- the pass was never
    required for success in the first place (see
    ``_run_post_submit_support_pass``'s own docstring)."""
    from hermes_cli import trix_support as ts
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(ts, "run_support_pass", _boom)

    r = logged_in.post("/api/submit", json=GOOD_FORM)

    assert r.json()["ok"] is True
    assert r.json()["support_check_message"] is None


def test_already_installed_row_is_never_reinstalled(logged_in, monkeypatch):
    """Resubmitting an unchanged step 5 must not re-run an install hook
    for a row that's already installed — some take minutes."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp,
        "wizard_tool_blocks",
        lambda: _fake_tool_blocks(
            {"browser": [_fake_row("Local Browser", post_setup="agent_browser", backend_key="off", installed=True)]}
        ),
    )

    def _fail_if_called(key):
        pytest.fail("an already-installed row must never be reinstalled")

    monkeypatch.setattr(wapp, "run_tool_install", _fail_if_called)

    form = dict(GOOD_FORM)
    form["browser_backend"] = "off"

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True
    assert r.json()["tool_install_failures"] == []


def test_no_matching_row_skips_install_entirely(logged_in, monkeypatch):
    """GOOD_FORM's own browser_backend ("chromium") matches no catalog
    row's backend_key at all — the install stage must be a silent no-op,
    never an error."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp,
        "wizard_tool_blocks",
        lambda: _fake_tool_blocks({"browser": [_fake_row("Local Browser", post_setup="agent_browser", backend_key="off")]}),
    )

    def _fail_if_called(key):
        pytest.fail("no row should have matched this submission's own choices")

    monkeypatch.setattr(wapp, "run_tool_install", _fail_if_called)

    r = logged_in.post("/api/submit", json=dict(GOOD_FORM))

    assert r.json()["ok"] is True
    assert r.json()["tool_install_failures"] == []


def test_install_failure_does_not_fail_the_submission(logged_in, monkeypatch):
    """Owner ruling: a tool that fails to install must NOT fail the whole
    submission — settings are already saved, restart/liveness still run,
    and the failure is reported honestly in tool_install_failures instead
    of aborting."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp,
        "wizard_tool_blocks",
        lambda: _fake_tool_blocks({"browser": [_fake_row("Camofox", post_setup="camofox", backend_key="off")]}),
    )
    restart_calls = []
    monkeypatch.setattr(
        wapp, "restart_gateway", lambda: restart_calls.append(1) or {"ok": True, "message": ""}
    )
    monkeypatch.setattr(
        wapp,
        "run_tool_install",
        lambda key: {"ok": False, "message": "На этой машине не найден Node.js."},
    )

    form = dict(GOOD_FORM)
    form["browser_backend"] = "off"

    r = logged_in.post("/api/submit", json=form)
    body = r.json()

    assert body["ok"] is True, body
    assert restart_calls == [1]  # restart still ran despite the install failure
    assert body["tool_install_failures"] == [
        {"name": "Camofox", "message": "На этой машине не найден Node.js."}
    ]


def test_install_stage_generic_message_when_run_tool_install_returns_no_dict(logged_in, monkeypatch):
    """Defensive fallback: a non-dict/None return from run_tool_install
    (never supposed to happen per its own docstring, but nothing here
    trusts that blindly) still produces an honest, generic Russian
    message in tool_install_failures instead of crashing or silently
    dropping the row."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp,
        "wizard_tool_blocks",
        lambda: _fake_tool_blocks({"browser": [_fake_row("Local Browser", post_setup="agent_browser", backend_key="off")]}),
    )
    monkeypatch.setattr(wapp, "run_tool_install", lambda key: None)

    form = dict(GOOD_FORM)
    form["browser_backend"] = "off"

    r = logged_in.post("/api/submit", json=form)
    body = r.json()

    assert body["ok"] is True
    assert body["tool_install_failures"] == [
        {"name": "Local Browser", "message": wapp._MSG_TOOL_INSTALL_FAILED_GENERIC}
    ]


def test_install_stage_exception_is_caught_and_reported(logged_in, monkeypatch):
    """run_tool_install() raising outright (defensive — its own docstring
    says it shouldn't) must not 500 the whole submission."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp,
        "wizard_tool_blocks",
        lambda: _fake_tool_blocks({"browser": [_fake_row("Local Browser", post_setup="agent_browser", backend_key="off")]}),
    )

    def _boom(key):
        raise RuntimeError("install exploded")

    monkeypatch.setattr(wapp, "run_tool_install", _boom)

    form = dict(GOOD_FORM)
    form["browser_backend"] = "off"

    r = logged_in.post("/api/submit", json=form)
    body = r.json()

    assert body["ok"] is True
    assert body["tool_install_failures"] == [
        {"name": "Local Browser", "message": wapp._MSG_TOOL_INSTALL_FAILED_GENERIC}
    ]


def test_install_stage_timeout_reports_honest_message_without_hanging(logged_in, monkeypatch):
    """Mirrors the removed /api/install endpoint's own timeout protection
    — a hung installer must not hang the whole submission (restart/
    liveness/extinguish would otherwise never run). _INSTALL_TIMEOUT_SECONDS
    is monkeypatched down so this doesn't actually wait 600s."""
    import time

    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp,
        "wizard_tool_blocks",
        lambda: _fake_tool_blocks({"browser": [_fake_row("Camofox", post_setup="camofox", backend_key="off")]}),
    )
    monkeypatch.setattr(wapp, "_INSTALL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(wapp, "run_tool_install", lambda key: time.sleep(1))

    form = dict(GOOD_FORM)
    form["browser_backend"] = "off"

    r = logged_in.post("/api/submit", json=form)
    body = r.json()

    assert body["ok"] is True
    assert len(body["tool_install_failures"]) == 1
    assert "долго" in body["tool_install_failures"][0]["message"]


def test_tool_provider_selection_triggers_install_for_matching_row(logged_in, monkeypatch):
    """The generic tool_provider mechanism (tts/stt/image_gen/video_gen)
    selects a row for install the same way browser_backend does — matched
    on provider_key, not backend_key."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp,
        "wizard_tool_blocks",
        lambda: _fake_tool_blocks(
            {"tts": [_fake_row("KittenTTS", post_setup="kittentts", provider_key="kittentts")]}
        ),
    )
    calls = []
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: calls.append(key) or {"ok": True, "message": "Установлено."}
    )

    form = dict(GOOD_FORM)
    form["tool_provider"] = {"tts": "kittentts"}

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True
    assert calls == ["kittentts"]


def test_camofox_activation_wins_over_browser_backend_for_install(logged_in, monkeypatch):
    """A non-empty camofox_url this submission selects the Camofox row for
    install, NOT whatever row browser_backend ("off") would otherwise
    match — mirrors page.py's own pendingToolInstallNames() priority (see
    _selected_browser_row's own docstring)."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    local_row = _fake_row("Local Browser", post_setup="agent_browser", backend_key="off")
    camofox_row = _fake_row(
        "Camofox", post_setup="camofox", env_vars=[{"key": "CAMOFOX_URL", "prompt": "Camofox server URL"}]
    )
    monkeypatch.setattr(
        wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({"browser": [local_row, camofox_row]})
    )
    calls = []
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: calls.append(key) or {"ok": True, "message": "Установлено."}
    )

    form = dict(GOOD_FORM)
    form["browser_backend"] = "off"
    form["camofox_url"] = "http://localhost:9377"

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True
    assert calls == ["camofox"]


def test_reset_tool_cache_called_twice_when_install_stage_runs(logged_in, monkeypatch):
    """installed-verdicts flip after an install runs — the cache must be
    invalidated a SECOND time (once after apply, once after the install
    stage), on top of test_reset_tool_cache_called_after_successful_apply's
    single call for a submission with nothing to install."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp,
        "wizard_tool_blocks",
        lambda: _fake_tool_blocks({"browser": [_fake_row("Local Browser", post_setup="agent_browser", backend_key="off")]}),
    )
    monkeypatch.setattr(wapp, "run_tool_install", lambda key: {"ok": True, "message": "Установлено."})
    calls = []
    monkeypatch.setattr(wapp, "reset_tool_cache", lambda app: calls.append(app))

    form = dict(GOOD_FORM)
    form["browser_backend"] = "off"

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Regression, 2026-09-04 field report ("список «что доустановить» считается
# ПОСЛЕ того, как настройки уже записаны"): every test above fakes
# ``wizard_tool_blocks`` with an explicit ``installed`` flag, so none of them
# ever exercise the REAL interaction between ``apply_settings()`` (which
# writes config.yaml/.env for THIS submission) and
# ``tools_config.provider_readiness_status()`` (which _pending_tool_installs
# reads the "installed" verdict from). That interaction is exactly where the
# bug lived: a value ``apply_settings()`` just wrote — ``browser.backend`` /
# ``CAMOFOX_URL`` — made the very next readiness read think the row was
# already installed, before its post_setup hook ever ran once. Both tests
# below run the REAL ``apply_settings`` and the REAL ``wizard_tool_blocks``
# (only the login/liveness probes and the install hook itself are stubbed,
# so no subprocess actually runs) against an isolated HERMES_HOME
# (``logged_in``'s own ``tmp_path``).


def _real_apply_and_catalog_stack(monkeypatch, wapp):
    """Same login/restart/liveness stubs ``_ok_stack`` uses, but leaves
    ``apply_settings`` and ``wizard_tool_blocks`` real — see the module
    comment above for why that distinction is the whole point here."""
    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"})
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"})


def test_browser_use_first_pick_queues_the_cli_install(logged_in, monkeypatch):
    """Defect 1: ``_POST_SETUP_READY`` had no ``browser_use_cli`` entry, so
    the row fell into ``provider_readiness_status``'s ``is_active`` fallback
    — which reads ``config.browser.backend``, a value ``apply_settings()``
    had *just* written to "browser-use" for this very submission. A
    first-time pick therefore read as already-installed and
    ``uv tool install browser-use`` never ran (two-machine field report:
    empty ``tool_install_failures`` but no CLI on disk). Pin ``shutil.which``
    to "absent" so the assertion holds regardless of whether this test
    machine happens to have the CLI on PATH."""
    from hermes_cli.setup_wizard import app as wapp

    _real_apply_and_catalog_stack(monkeypatch, wapp)
    monkeypatch.setattr("shutil.which", lambda name: None)
    calls = []
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: calls.append(key) or {"ok": True, "message": "Установлено."}
    )

    form = dict(GOOD_FORM)
    form["browser_backend"] = "browser-use"

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True, r.json()
    assert "browser_use_cli" in calls


def test_camofox_first_pick_queues_the_npm_install(logged_in, monkeypatch):
    """Defect 2: Camofox's row carries BOTH ``env_vars`` (CAMOFOX_URL) and a
    ``post_setup`` install hook with a real predicate (``_camofox_installed``
    — checks node_modules). ``provider_readiness_status`` used to return
    "ready" the moment ``env_vars`` were satisfied, never reaching that
    predicate at all — so once ``apply_settings()`` wrote CAMOFOX_URL for
    THIS submission, the row read as ready before the npm package was ever
    installed. Pin ``_camofox_installed`` to False so the assertion holds
    regardless of this checkout's own node_modules state."""
    import hermes_cli.tools_config as tc
    from hermes_cli.setup_wizard import app as wapp

    _real_apply_and_catalog_stack(monkeypatch, wapp)
    monkeypatch.setattr(tc, "_camofox_installed", lambda: False)
    calls = []
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: calls.append(key) or {"ok": True, "message": "Установлено."}
    )

    form = dict(GOOD_FORM)
    form["camofox_url"] = "http://localhost:9377"

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True, r.json()
    assert "camofox" in calls


def test_apply_failure_reports_stage_apply_and_skips_restart(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    monkeypatch.setattr(
        wapp,
        "apply_settings",
        lambda f: {"ok": False, "written": [], "errors": ["Не удалось сохранить X"]},
    )
    restart_calls = []
    monkeypatch.setattr(wapp, "restart_gateway", lambda: restart_calls.append(1))

    r = logged_in.post("/api/submit", json=GOOD_FORM)
    body = r.json()

    assert body["ok"] is False and body["stage"] == "apply"
    assert restart_calls == []


def test_restart_failure_reports_stage_restart_and_skips_liveness(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    monkeypatch.setattr(wapp, "apply_settings", lambda f: {"ok": True, "written": [], "errors": []})
    monkeypatch.setattr(
        wapp, "restart_gateway", lambda: {"ok": False, "message": "Не удалось перезапустить"}
    )
    alive_calls = []
    monkeypatch.setattr(wapp, "wait_bot_alive", lambda *a, **k: alive_calls.append(1))
    # Install stage guard (bug class fixed 2026-09-02): apply succeeds
    # here, so the install stage still runs BEFORE the restart failure
    # this test is actually about — see _ok_stack's own comment.
    monkeypatch.setattr(wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({}))
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: pytest.fail(f"unexpected real install stage reached: {key!r}")
    )

    r = logged_in.post("/api/submit", json=GOOD_FORM)
    body = r.json()

    assert body["ok"] is False and body["stage"] == "restart"
    assert alive_calls == []


def test_submit_requires_session(app_env):
    client, _ = app_env
    assert client.post("/api/submit", json=GOOD_FORM).status_code == 401


def test_validation_short_circuits_before_provider_key_probe(logged_in, monkeypatch):
    """A failing allowed_users check must stop the chain before the (live,
    network-touching) provider-key probe ever runs."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    calls = []
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: calls.append(a))
    form = dict(GOOD_FORM)
    form["allowed_users"] = "not-a-number"

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert "allowed_users" in r.json()["errors"]
    assert calls == []


def test_provider_key_error_surfaces_probe_message(logged_in, monkeypatch):
    """``probe_provider_key`` (credential_probes.py) puts its text in
    ``message``, not ``error`` — ``validate.check_provider_key`` merges
    that shape in unchanged (``{**probe_provider_key(...), "checked": True}``).
    Mocking the transport at ``probe_provider_key`` (not the whole
    ``check_provider_key``) proves ``_run_submit`` reads the field the
    real probe actually populates, rather than always falling back to the
    generic message."""
    from hermes_cli.setup_wizard import app as wapp
    from hermes_cli.setup_wizard import validate as wvalidate

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(
        wvalidate,
        "probe_provider_key",
        lambda env_var, value, timeout=10.0, proxy=None: {
            "ok": False,
            "reachable": True,
            "message": "Провайдер отклонил этот ключ. Проверьте его и попробуйте ещё раз.",
        },
    )

    r = logged_in.post("/api/submit", json=GOOD_FORM)

    assert r.status_code == 422
    assert (
        r.json()["errors"]["provider.api_key"]
        == "Провайдер отклонил этот ключ. Проверьте его и попробуйте ещё раз."
    )


def test_submit_forwards_the_form_proxy_to_the_provider_key_check(logged_in, monkeypatch):
    """Owner requirement: the same proxy field that gates Telegram
    reachability must also gate the live provider-key probe at submit
    time — a RU-hosted server can't reach OpenAI/OpenRouter/Anthropic
    directly without it."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"})
    calls = []
    monkeypatch.setattr(
        wapp, "check_provider_key", lambda *a: calls.append(a) or {"ok": True, "reachable": True}
    )
    monkeypatch.setattr(wapp, "apply_settings", lambda f: {"ok": True, "written": [], "errors": []})
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"})
    # Install stage guard (bug class fixed 2026-09-02) — see _ok_stack's
    # own comment for why a successful apply always reaches this stage.
    monkeypatch.setattr(wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({}))
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: pytest.fail(f"unexpected real install stage reached: {key!r}")
    )

    form = {**GOOD_FORM, "proxy": "socks5://u:p@h:1080"}
    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True
    # Both the primary provider key check AND the fallback's — each call
    # is (env_var, api_key, proxy).
    assert len(calls) == 2
    assert all(call[2] == "socks5://u:p@h:1080" for call in calls)


def test_return_mode_leaves_unchanged_secrets_untouched(tmp_path, monkeypatch):
    """Return-mode edit (spec §11): env already has a working token,
    allowed-users list, AND an already-active+keyed provider
    (model.provider in config.yaml + its credential in .env — the
    condition an empty provider.name is legal against, per the
    "provider requirement" gate); the form only changes ``tts_voice``.
    Secrets round-trip from ``/api/form`` as a mask, never in the clear,
    so an untouched field must be treated as "leave alone", not
    "missing" — and, critically, no live check_telegram_token/
    check_provider_key/check_allowed_users call should happen for a
    field the user didn't retype this round."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import load_config, save_config, save_env_value_secure
    from hermes_cli.setup_wizard.state import WizardState

    save_env_value_secure("TELEGRAM_BOT_TOKEN", "123:abc")
    save_env_value_secure("TELEGRAM_ALLOWED_USERS", "111,222")
    save_env_value_secure("OPENROUTER_API_KEY", "sk-existing")
    cfg = load_config()
    cfg["model"] = {"provider": "openrouter"}
    # Полностью настроенный возвратный клиент со спеки 11 имеет и
    # часовой пояс: пустое значение законно ровно потому, что ответ
    # уже сохранён.
    cfg["timezone"] = "Europe/Moscow"
    save_config(cfg)

    login, pw = "trix-testlogin01", "Tr1xTestPassw0rd0000000000000"
    WizardState.load().issue_primary(login, pw)
    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app

    client = TestClient(create_app(), base_url="https://testserver")
    client.auth = (login, pw)

    from hermes_cli.setup_wizard import app as wapp

    def _fail_if_called(*a, **k):
        pytest.fail("must not be called for an untouched, already-saved field")

    monkeypatch.setattr(wapp, "check_telegram_token", _fail_if_called)
    monkeypatch.setattr(wapp, "check_provider_key", _fail_if_called)
    monkeypatch.setattr(wapp, "check_allowed_users", _fail_if_called)
    monkeypatch.setattr(wapp, "apply_settings", lambda f: {"ok": True, "written": [], "errors": []})
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(
        wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"}
    )

    _mock_support_pass(monkeypatch)
    r = client.post("/api/submit", json={"tts_voice": "ru-RU-SvetlanaNeural"})

    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_first_time_empty_token_is_required(logged_in, monkeypatch):
    """First-run mode: nothing saved yet — an empty telegram_token must
    422 as "required", not silently pass through as a no-op (that
    contract only applies once something is actually saved)."""
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: calls.append(a))
    form = dict(GOOD_FORM)
    form["telegram_token"] = ""

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert "telegram_token" in r.json()["errors"]
    assert calls == []  # empty + nothing saved must never reach the live check


def test_double_submit_is_rejected_with_409(wizard_app, monkeypatch):
    """Two overlapping submits (double 'Готово' click, browser retry of a
    slow request) must not both drive a restart — a second concurrent
    submit could kill the gateway process the first request's liveness
    wait is still polling, turning a real success into a false 'бот не
    отвечает'. No threads here: the flag is set directly, then cleared."""
    from fastapi.testclient import TestClient

    app, (login, pw) = wizard_app
    client = TestClient(app, base_url="https://testserver")
    client.auth = (login, pw)

    app.state.submit_in_flight = True
    try:
        r = client.post("/api/submit", json=GOOD_FORM)
        assert r.status_code == 409
        assert "error" in r.json()
    finally:
        app.state.submit_in_flight = False

    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)

    r = client.post("/api/submit", json=GOOD_FORM)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_unknown_provider_name_rejected_before_any_check(logged_in, monkeypatch):
    """A provider name outside the wizard's own catalog — including a
    product-excluded one like 'nous' — must never reach apply_settings,
    which would hand it straight to _update_config_for_provider."""
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: calls.append(a))
    monkeypatch.setattr(wapp, "apply_settings", lambda f: calls.append(f))
    form = dict(GOOD_FORM)
    form["provider"] = {**GOOD_FORM["provider"], "name": "nous"}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["provider.name"]
    assert calls == []


def test_unknown_fallback_name_rejected_before_apply(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    applied = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: applied.append(f))
    form = dict(GOOD_FORM)
    form["fallback"] = {**GOOD_FORM["fallback"], "name": "nous"}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["fallback.name"]
    assert applied == []


def test_success_response_reports_key_checked_true_when_reachable(logged_in, monkeypatch):
    """§10.1's honest disclosure: key_checked reflects whether the
    provider key actually went through a REAL live probe THIS submission
    — check_provider_key's ``reachable`` flag, not its ``checked`` flag
    (see the next two tests for why ``checked`` alone would lie)."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp, "check_provider_key", lambda *a: {"ok": True, "checked": True, "reachable": True}
    )

    r = logged_in.post("/api/submit", json=GOOD_FORM)

    assert r.json()["ok"] is True
    assert r.json()["key_checked"] is True


def test_success_response_reports_key_checked_false_when_not_reachable(logged_in, monkeypatch):
    """``checked=True`` alone must NOT report key_checked=True — a probe
    that ran but never actually reached the provider (network failure,
    or the "checked" flag validate.check_provider_key sets for ANY
    non-empty env_var) is not verification."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp, "check_provider_key", lambda *a: {"ok": True, "checked": True, "reachable": False}
    )

    r = logged_in.post("/api/submit", json=GOOD_FORM)

    assert r.json()["ok"] is True
    assert r.json()["key_checked"] is False


def test_success_response_key_checked_false_for_provider_without_probe(logged_in, monkeypatch):
    """Real-shape regression: credential_probes.probe_provider_key's own
    "unknown provider" contract is {"ok": True, "reachable": False,
    "message": ""} — validate.check_provider_key merges that in UNCHANGED
    plus its own "checked": True (checked is True for any non-empty
    env_var, probe table entry or not — see credential_probes.py's
    CREDENTIAL_PROBES, which only lists 4 providers). Mocking at
    probe_provider_key (not check_provider_key) proves key_checked
    reads the field that's actually honest for an unprobed provider
    (e.g. Anthropic/DeepSeek/zai) rather than the always-True one."""
    from hermes_cli.setup_wizard import app as wapp
    from hermes_cli.setup_wizard import validate as wvalidate

    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(wapp, "check_provider_key", wvalidate.check_provider_key)
    monkeypatch.setattr(
        wvalidate,
        "probe_provider_key",
        lambda env_var, value, timeout=10.0, proxy=None: {"ok": True, "reachable": False, "message": ""},
    )

    r = logged_in.post("/api/submit", json=GOOD_FORM)

    assert r.json()["ok"] is True
    assert r.json()["key_checked"] is False


def test_first_time_empty_provider_is_required(logged_in, monkeypatch):
    """First-run mode: nothing configured yet (fresh HERMES_HOME via the
    ``logged_in`` fixture's tmp_path) — an empty provider block must 422
    as "select a provider", not silently skip the whole block and let the
    wizard extinguish itself with no model configured at all (the "D —
    breaks the acceptance criterion" gap this closes)."""
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: calls.append(a))
    monkeypatch.setattr(wapp, "apply_settings", lambda f: calls.append(f))
    form = dict(GOOD_FORM)
    form["provider"] = {}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["provider.name"]
    assert calls == []


def test_return_mode_empty_provider_passes_when_already_configured(tmp_path, monkeypatch):
    """Return-mode edit (spec §11): model.provider is already active in
    config.yaml AND its credential is already in .env — an empty
    provider block on a later visit is legal (leave the active provider
    alone), and must not be treated as "nothing selected"."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import load_config, save_config, save_env_value_secure
    from hermes_cli.setup_wizard.state import WizardState

    save_env_value_secure("TELEGRAM_BOT_TOKEN", "123:abc")
    save_env_value_secure("TELEGRAM_ALLOWED_USERS", "111,222")
    save_env_value_secure("OPENROUTER_API_KEY", "sk-existing")
    cfg = load_config()
    cfg["model"] = {"provider": "openrouter"}
    # Полностью настроенный возвратный клиент со спеки 11 имеет и
    # часовой пояс: пустое значение законно ровно потому, что ответ
    # уже сохранён.
    cfg["timezone"] = "Europe/Moscow"
    save_config(cfg)

    login, pw = "trix-testlogin01", "Tr1xTestPassw0rd0000000000000"
    WizardState.load().issue_primary(login, pw)
    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app

    client = TestClient(create_app(), base_url="https://testserver")
    client.auth = (login, pw)

    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"})
    monkeypatch.setattr(wapp, "apply_settings", lambda f: {"ok": True, "written": [], "errors": []})
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(
        wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"}
    )

    _mock_support_pass(monkeypatch)
    r = client.post("/api/submit", json={"tts_voice": "ru-RU-SvetlanaNeural"})

    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_fallback_api_key_without_name_rejected_before_apply(logged_in, monkeypatch):
    """The overwrite gap (review round 1, point A): a fallback dict with
    env_var+api_key but no name would previously skip validation entirely
    (the old gate keyed on fallback_name) while apply_settings still
    writes the credential (its own write condition keys on env_var+
    api_key, never name) — worst case, the SAME env_var as the primary
    provider, letting an unchecked key silently overwrite a checked one."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "reachable": True})
    applied = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: applied.append(f))
    form = dict(GOOD_FORM)
    form["fallback"] = {"env_var": "OPENROUTER_API_KEY", "api_key": "x"}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["fallback.name"]
    assert applied == []


def test_provider_env_var_mismatch_rejected_before_apply(logged_in, monkeypatch):
    """Point B from the second review pass: the client fully controls
    which env var a submitted key lands under unless the server checks
    it against the catalog — name "openrouter" paired with a foreign
    env_var (e.g. GLM_API_KEY, the fallback provider's own credential)
    must 422 rather than let apply_settings write the key under a
    mismatched name."""
    from hermes_cli.setup_wizard import app as wapp

    calls = []
    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: calls.append(a))
    monkeypatch.setattr(wapp, "apply_settings", lambda f: calls.append(f))
    form = dict(GOOD_FORM)
    form["provider"] = {**GOOD_FORM["provider"], "env_var": "GLM_API_KEY"}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["provider.env_var"]
    assert calls == []


def test_fallback_env_var_mismatch_rejected_before_apply(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "reachable": True})
    applied = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: applied.append(f))
    form = dict(GOOD_FORM)
    form["fallback"] = {**GOOD_FORM["fallback"], "env_var": "OPENROUTER_API_KEY"}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["fallback.env_var"]
    assert applied == []


def test_search_env_unknown_key_rejected_before_apply(logged_in, monkeypatch):
    """Commit 2: the generalized search_env mechanism validates `key`
    against the live "web" catalog's own env vars — an arbitrary env var
    name (e.g. a provider credential unrelated to search) must 422, never
    reach apply_settings."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"})
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    calls = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: calls.append(f))
    form = dict(GOOD_FORM)
    form["search_env"] = {"key": "ANTHROPIC_API_KEY", "value": "sk-whatever"}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["search_env.key"]
    assert calls == []


def test_search_env_legal_key_reaches_apply(logged_in, monkeypatch):
    """A key drawn from the live "web" catalog (BRAVE_SEARCH_API_KEY —
    always rendered, no liveness gate) passes validation and is handed to
    apply_settings verbatim."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    # "brave-free" only exists on the REAL "web" catalog — _ok_stack's fake
    # one (just GOOD_FORM's own "ddgs") would 422 this submission instead
    # of reaching apply. See _real_catalog_no_real_install's own docstring.
    _real_catalog_no_real_install(monkeypatch, wapp)
    captured = {}

    def _capture_apply(f):
        captured.update(f)
        return {"ok": True, "written": [], "errors": []}

    monkeypatch.setattr(wapp, "apply_settings", _capture_apply)
    form = dict(GOOD_FORM)
    form["search_backend"] = "brave-free"
    form["search_env"] = {"key": "BRAVE_SEARCH_API_KEY", "value": "brave-test-key"}

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True
    assert captured["search_env"] == {"key": "BRAVE_SEARCH_API_KEY", "value": "brave-test-key"}


# ---- search_backend / extract_backend / extract_env: the "web" split's
# closed-catalog validation (2026-08-26) -----------------------------------


def test_search_backend_unknown_value_rejected_before_apply(logged_in, monkeypatch):
    """`search_backend` (the row-select field itself, not its credential)
    must name a real "web" catalog `web_backend` value — an arbitrary
    string must 422, never reach apply_settings."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"})
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    calls = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: calls.append(f))
    form = dict(GOOD_FORM)
    form["search_backend"] = "not-a-real-backend"

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["search_backend"]
    assert calls == []


def test_extract_backend_unknown_value_rejected_before_apply(logged_in, monkeypatch):
    """`extract_backend` must name a real "web_extract" catalog
    `web_backend` value."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"})
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    calls = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: calls.append(f))
    form = dict(GOOD_FORM)
    form["extract_backend"] = "not-a-real-backend"

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["extract_backend"]
    assert calls == []


def test_extract_backend_search_only_value_rejected_before_apply(logged_in, monkeypatch):
    """A real "web" catalog backend that CANNOT extract (ddgs is
    search-only) must still 422 as `extract_backend` — "web_extract"'s
    catalog only ever contains extract-capable backends (see
    tools_view.py's module docstring), so ddgs is never a legal value
    there."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"})
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    calls = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: calls.append(f))
    form = dict(GOOD_FORM)
    form["extract_backend"] = "ddgs"

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["extract_backend"]
    assert calls == []


def test_extract_backend_legal_value_reaches_apply(logged_in, monkeypatch):
    """A real extract-capable backend (Firecrawl — always rendered, no
    liveness gate) passes validation and is handed to apply_settings
    verbatim, alongside its own extract_env credential."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    # "firecrawl" only exists on the REAL "web_extract" catalog — see
    # _real_catalog_no_real_install's own docstring.
    _real_catalog_no_real_install(monkeypatch, wapp)
    captured = {}

    def _capture_apply(f):
        captured.update(f)
        return {"ok": True, "written": [], "errors": []}

    monkeypatch.setattr(wapp, "apply_settings", _capture_apply)
    form = dict(GOOD_FORM)
    form["extract_backend"] = "firecrawl"
    form["extract_env"] = {"key": "FIRECRAWL_API_KEY", "value": "fc-test-key"}

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True
    assert captured["extract_backend"] == "firecrawl"
    assert captured["extract_env"] == {"key": "FIRECRAWL_API_KEY", "value": "fc-test-key"}


def test_extract_backend_null_reaches_apply_as_none_not_a_422(logged_in, monkeypatch):
    """Finding 1 (review 2026-08-26, owner-approved fix, reproduced against
    the LIVE app): before this fix, `extract_backend` was declared plain
    `str` in `_SubmitBody` — pydantic rejected an explicit JSON `null`
    outright (422 `{"detail": [{"loc": ["body", "extract_backend"], "type":
    "string_type"}]}`), a raw pydantic shape `errorsFromResponseBody()`
    doesn't map to any field or step, so the client saw an address-less
    "Неверное значение." with no way back to the right screen — even
    though `extractBackendChoiceValue()` on the client had always sent
    `null` as the deliberate "turn Чтение страниц off" signal (mirroring
    `imageGenProviderChoiceValue()`'s own contract). `extract_backend: str
    | None` makes the submit ACCEPT the null and hand it to
    apply_settings() verbatim, not stripped to "" and not 422ing."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    captured = {}

    def _capture_apply(f):
        captured.update(f)
        return {"ok": True, "written": [], "removed": ["web.extract_backend"], "errors": [], "warnings": []}

    monkeypatch.setattr(wapp, "apply_settings", _capture_apply)
    form = dict(GOOD_FORM)
    form["extract_backend"] = None

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert captured["extract_backend"] is None


def test_extract_env_unknown_key_rejected_before_apply(logged_in, monkeypatch):
    """Mirrors test_search_env_unknown_key_rejected_before_apply for
    extract_env — an arbitrary env var name must 422, never reach
    apply_settings."""
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"})
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    calls = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: calls.append(f))
    form = dict(GOOD_FORM)
    form["extract_env"] = {"key": "ANTHROPIC_API_KEY", "value": "sk-whatever"}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["extract_env.key"]
    assert calls == []


def test_search_only_backend_env_var_never_legal_as_extract_env():
    """Structural sanity check behind the extract_env legality rule: a
    search-only-only backend's own credential key (BRAVE_SEARCH_API_KEY)
    is a legal `search_env.key` but must NEVER be a legal
    `extract_env.key` — the two are validated against DIFFERENT catalogs
    (web vs web_extract, see tools_view.py's module docstring)."""
    from hermes_cli.setup_wizard.app import _legal_extract_env_vars, _legal_search_env_vars
    from hermes_cli.setup_wizard.tools_view import wizard_tool_blocks

    blocks = wizard_tool_blocks()
    # Brave Search (Free) always renders (no liveness gate) so this is a
    # stable, non-flaky member of the "web" catalog.
    assert "BRAVE_SEARCH_API_KEY" in _legal_search_env_vars(blocks)
    assert "BRAVE_SEARCH_API_KEY" not in _legal_extract_env_vars(blocks)


# ---- tool_env / tool_provider: generic provider-select validation -------
# (owner ruling 2026-08-20 — see tools_view.py's module docstring)


def test_tool_env_unknown_key_rejected_before_apply(logged_in, monkeypatch):
    """Mirrors test_search_env_unknown_key_rejected_before_apply for the
    generalized tool_env mechanism — an env var name that belongs to no
    row of any provider-select category (tts/image_gen/video_gen/
    x_search) must 422, never reach apply_settings."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    # The REAL catalog, deliberately: on _ok_stack's fake one there is no
    # `tts` category at all, so the 422 would be guaranteed by the absence
    # of the category rather than by the validator recognising that this key
    # belongs to no row -- which is the property the docstring claims.
    _real_catalog_no_real_install(monkeypatch, wapp)
    calls = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: calls.append(f))
    form = dict(GOOD_FORM)
    form["tool_env"] = [{"key": "ANTHROPIC_API_KEY", "value": "sk-whatever"}]

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["tool_env"]
    assert calls == []


def test_tool_env_legal_key_reaches_apply(logged_in, monkeypatch):
    """A key drawn from the live "tts" catalog (ELEVENLABS_API_KEY — a
    built-in hardcoded row, not plugin-dependent) passes validation and is
    handed to apply_settings verbatim."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    # ELEVENLABS_API_KEY only exists on the REAL "tts" catalog — see
    # _real_catalog_no_real_install's own docstring.
    _real_catalog_no_real_install(monkeypatch, wapp)
    captured = {}

    def _capture_apply(f):
        captured.update(f)
        return {"ok": True, "written": [], "errors": []}

    monkeypatch.setattr(wapp, "apply_settings", _capture_apply)
    form = dict(GOOD_FORM)
    form["tool_env"] = [{"key": "ELEVENLABS_API_KEY", "value": "el-test-key"}]

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True
    assert captured["tool_env"] == [{"key": "ELEVENLABS_API_KEY", "value": "el-test-key"}]


def test_tool_provider_unknown_value_rejected_before_apply(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    # The REAL catalog: with _ok_stack's fake one there is no `tts` category,
    # so the rejection would hold no matter what the validator did.
    _real_catalog_no_real_install(monkeypatch, wapp)
    calls = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: calls.append(f))
    form = dict(GOOD_FORM)
    form["tool_provider"] = {"tts": "not-a-real-tts-provider"}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["tool_provider"]
    assert calls == []


def test_tool_provider_legal_value_reaches_apply(logged_in, monkeypatch):
    """"elevenlabs" is a real provider_key on the live "tts" catalog's
    built-in ElevenLabs row (see tools_view.py) — passes validation and is
    handed to apply_settings verbatim."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    # "elevenlabs" only exists on the REAL "tts" catalog — see
    # _real_catalog_no_real_install's own docstring.
    _real_catalog_no_real_install(monkeypatch, wapp)
    captured = {}

    def _capture_apply(f):
        captured.update(f)
        return {"ok": True, "written": [], "errors": []}

    monkeypatch.setattr(wapp, "apply_settings", _capture_apply)
    form = dict(GOOD_FORM)
    form["tool_provider"] = {"tts": "elevenlabs"}

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True
    assert captured["tool_provider"] == {"tts": "elevenlabs"}


def test_tool_provider_nexara_stt_reaches_apply(logged_in, monkeypatch):
    """"nexara" is a real provider_key on the live "stt" catalog's
    registry-injected row (tools_view.py::_stt_registry_rows()) — proves
    the generic tool_provider validation path picks up plugin-injected
    rows, not just tools_config's static ones, and that "stt" itself
    validates now that it joined _TOOL_ENV_CATEGORIES."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    # "nexara"/NEXARA_API_KEY only exist on the REAL, registry-injected
    # "stt" catalog — see _real_catalog_no_real_install's own docstring.
    _real_catalog_no_real_install(monkeypatch, wapp)
    captured = {}

    def _capture_apply(f):
        captured.update(f)
        return {"ok": True, "written": [], "errors": []}

    monkeypatch.setattr(wapp, "apply_settings", _capture_apply)
    form = dict(GOOD_FORM)
    form["tool_provider"] = {"stt": "nexara"}
    form["tool_env"] = [{"key": "NEXARA_API_KEY", "value": "nx-test-key"}]

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True
    assert captured["tool_provider"] == {"stt": "nexara"}
    assert captured["tool_env"] == [{"key": "NEXARA_API_KEY", "value": "nx-test-key"}]


def test_tool_env_and_tool_provider_missing_are_pure_no_ops(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    r = logged_in.post("/api/submit", json=GOOD_FORM)
    assert r.json()["ok"] is True


def test_return_mode_empty_provider_passes_for_device_code_provider(tmp_path, monkeypatch):
    """Round-3 regression: device-code-auth catalog rows (openai-codex,
    minimax-oauth — plus custom) keep credentials in auth.json, not
    .env, so they carry no env_var in the catalog at all. The round-2
    "already configured" check (env_var + get_env_value) falsely 422'd
    a client who genuinely authenticated via device code, because it
    required a saved .env value that a device-code provider never has.
    An active catalog row with no env_var must be treated as already
    configured by itself."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import load_config, save_config, save_env_value_secure
    from hermes_cli.setup_wizard.state import WizardState

    save_env_value_secure("TELEGRAM_BOT_TOKEN", "123:abc")
    save_env_value_secure("TELEGRAM_ALLOWED_USERS", "111,222")
    cfg = load_config()
    cfg["model"] = {"provider": "openai-codex"}
    # Полностью настроенный возвратный клиент со спеки 11 имеет и
    # часовой пояс: пустое значение законно ровно потому, что ответ
    # уже сохранён.
    cfg["timezone"] = "Europe/Moscow"
    save_config(cfg)

    login, pw = "trix-testlogin01", "Tr1xTestPassw0rd0000000000000"
    WizardState.load().issue_primary(login, pw)
    from fastapi.testclient import TestClient

    from hermes_cli.setup_wizard.app import create_app

    client = TestClient(create_app(), base_url="https://testserver")
    client.auth = (login, pw)

    from hermes_cli.setup_wizard import app as wapp

    def _fail_if_called(*a, **k):
        pytest.fail("must not be called for an untouched, already-saved field")

    monkeypatch.setattr(wapp, "check_telegram_token", _fail_if_called)
    monkeypatch.setattr(wapp, "check_provider_key", _fail_if_called)
    monkeypatch.setattr(wapp, "check_allowed_users", _fail_if_called)
    monkeypatch.setattr(wapp, "apply_settings", lambda f: {"ok": True, "written": [], "errors": []})
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(
        wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"}
    )

    _mock_support_pass(monkeypatch)
    r = client.post("/api/submit", json={"tts_voice": "ru-RU-SvetlanaNeural"})

    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_provider_name_only_submission_not_rejected_when_key_already_saved(logged_in, monkeypatch):
    """Round-3 regression: the env_var mismatch gate (round 2) must not
    reject a legitimate name-only submission — env_var omitted entirely,
    not a mismatched one — as long as the provider's key is already on
    disk. Only an EXPLICIT, conflicting env_var should 422 (see
    test_provider_env_var_mismatch_rejected_before_apply, which stays
    green)."""
    from hermes_cli.config import save_env_value_secure
    from hermes_cli.setup_wizard import app as wapp

    save_env_value_secure("ANTHROPIC_API_KEY", "sk-ant-existing")

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    # Как у всех соседей в этом файле: живая проверка ключа подменяется.
    # Раньше её отсутствие здесь ничего не стоило — у GOOD_FORM["fallback"]
    # переменная GLM_API_KEY не имела курированной проверки, и probe
    # возвращал "ок, не проверяли", не касаясь сети. С выведенными
    # проверками (trix_derived_probes) адрес берётся из каталога, и
    # поддельный "glm-test" получает от провайдера честный 401 — то есть
    # тест про совсем другой запрет начал зависеть от наличия интернета.
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    monkeypatch.setattr(wapp, "apply_settings", lambda f: {"ok": True, "written": [], "errors": []})
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(
        wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"}
    )
    # Install stage guard (bug class fixed 2026-09-02) — see _ok_stack's
    # own comment for why a successful apply always reaches this stage.
    monkeypatch.setattr(wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({}))
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: pytest.fail(f"unexpected real install stage reached: {key!r}")
    )
    form = dict(GOOD_FORM)
    form["provider"] = {"name": "anthropic", "model": "x"}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_provider_name_only_with_api_key_normalizes_env_var_for_apply(logged_in, monkeypatch):
    """Round-3 regression: a name-only provider block that DOES carry a
    fresh api_key must still reach apply_settings with the catalog's
    env_var filled in — otherwise apply_settings' own write condition
    (env_var + api_key, both truthy) silently drops the credential
    because env_var arrived empty."""
    from hermes_cli.setup_wizard import app as wapp

    captured = []

    def _capture_apply(f):
        captured.append(f)
        return {"ok": True, "written": [], "errors": []}

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "reachable": True})
    monkeypatch.setattr(wapp, "apply_settings", _capture_apply)
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(
        wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"}
    )
    # Install stage guard (bug class fixed 2026-09-02) — see _ok_stack's
    # own comment for why a successful apply always reaches this stage.
    monkeypatch.setattr(wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({}))
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: pytest.fail(f"unexpected real install stage reached: {key!r}")
    )
    form = dict(GOOD_FORM)
    form["provider"] = {"name": "anthropic", "api_key": "sk-new", "model": "x"}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert len(captured) == 1
    assert captured[0]["provider"]["env_var"] == "ANTHROPIC_API_KEY"


def test_fallback_name_only_with_api_key_normalizes_env_var_for_apply(logged_in, monkeypatch):
    """Symmetric to the provider-side normalization test above — the
    fallback block gets the same catalog-sourced env_var fill-in before
    reaching apply_settings."""
    from hermes_cli.setup_wizard import app as wapp

    captured = []

    def _capture_apply(f):
        captured.append(f)
        return {"ok": True, "written": [], "errors": []}

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "reachable": True})
    monkeypatch.setattr(wapp, "apply_settings", _capture_apply)
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(
        wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"}
    )
    # Install stage guard (bug class fixed 2026-09-02) — see _ok_stack's
    # own comment for why a successful apply always reaches this stage.
    monkeypatch.setattr(wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({}))
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: pytest.fail(f"unexpected real install stage reached: {key!r}")
    )
    form = dict(GOOD_FORM)
    form["fallback"] = {"name": "zai", "api_key": "glm-new", "model": "glm-5.2"}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert len(captured) == 1
    assert captured[0]["fallback"]["env_var"] == "GLM_API_KEY"


def test_camofox_url_reaches_apply_settings(logged_in, monkeypatch):
    """Round 2 fix: camofox_url is a plain pass-through field on the
    submit body — no validation step touches it (spec ruling: it's a
    non-secret localhost URL, not a credential to live-check) — but it
    MUST still arrive at apply_settings() unchanged, since that's the
    only thing that actually writes CAMOFOX_URL (the real Camofox
    activation switch — see apply.py's own docstring)."""
    from hermes_cli.setup_wizard import app as wapp

    captured = []

    def _capture_apply(f):
        captured.append(f)
        return {"ok": True, "written": [], "errors": []}

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "reachable": True})
    monkeypatch.setattr(wapp, "apply_settings", _capture_apply)
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(
        wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"}
    )
    # Install stage guard (bug class fixed 2026-09-02): this test doesn't
    # mock wizard_tool_blocks/run_tool_install, so form.camofox_url
    # selects the REAL "Camofox" catalog row for the install stage that
    # runs right after apply (see _ok_stack's own comment) — Camofox is
    # genuinely not installed on most dev machines, which spawns a REAL
    # `npm install` against the checkout's own working tree and dirties
    # the tracked package-lock.json. camofox_url's own journey to
    # apply_settings (this test's actual point) has nothing to do with
    # the catalog, so a fake, no-op-selecting one is safe here.
    monkeypatch.setattr(wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({}))
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: pytest.fail(f"unexpected real install stage reached: {key!r}")
    )
    form = dict(GOOD_FORM)
    form["camofox_url"] = "http://localhost:9377"

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert len(captured) == 1
    assert captured[0]["camofox_url"] == "http://localhost:9377"


def test_tts_voice_explicit_default_name_reaches_apply_settings_unchanged(logged_in, monkeypatch):
    """Finding 2 (owner-approved fix, reversed from an earlier design):
    ``tts_voice`` no longer accepts a ``null`` clear signal at all — the
    client sends the literal default voice name (page.py's
    ``VOICE_DEFAULT_NAME``) for a deliberate return-to-default pick, and
    the server just passes it through untouched, same as any other plain
    string field. GOOD_FORM already carries this value; this pins that it
    survives to ``apply_settings()`` unmodified."""
    from hermes_cli.setup_wizard import app as wapp

    captured = []

    def _capture_apply(f):
        captured.append(f)
        return {"ok": True, "written": [], "errors": []}

    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "reachable": True})
    monkeypatch.setattr(wapp, "apply_settings", _capture_apply)
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(
        wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"}
    )
    # Install stage guard (bug class fixed 2026-09-02) — see _ok_stack's
    # own comment for why a successful apply always reaches this stage.
    monkeypatch.setattr(wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({}))
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: pytest.fail(f"unexpected real install stage reached: {key!r}")
    )
    form = dict(GOOD_FORM)
    form["tts_voice"] = "ru-RU-SvetlanaNeural"

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert len(captured) == 1
    assert captured[0]["tts_voice"] == "ru-RU-SvetlanaNeural"


def test_tts_voice_explicit_null_is_rejected_at_the_schema_level(logged_in):
    """Finding 2's other half: ``_SubmitBody.tts_voice`` reverted from
    ``str | None`` back to a plain ``str`` (owner-approved fix) — a client
    sending JSON ``null`` for this field is now a pydantic validation
    error, not a value that ever reaches ``apply_settings()``. Proves the
    clear-signal mechanism is retired at the API boundary, not just
    unused."""
    form = dict(GOOD_FORM)
    form["tts_voice"] = None

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Device-code login gate (owner requirement 2's other half): submitting a
# device-code provider (openai-codex / minimax-oauth) requires that the
# account is ALREADY logged in via the wizard's own /api/device/* flow —
# see device_login.device_login_is_valid()'s own docstring.
# ---------------------------------------------------------------------------


def test_device_code_provider_without_login_rejected_before_apply(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "device_login_is_valid", lambda name: False)
    calls = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: calls.append(f))
    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "b"})

    form = dict(GOOD_FORM)
    form["provider"] = {
        "name": "openai-codex",
        "env_var": None,
        "api_key": "",
        "base_url": "",
        "model": "",
    }

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["provider.name"] == (
        "Сначала выполните вход по аккаунту (кнопка в блоке провайдера)."
    )
    assert calls == []


def test_device_code_provider_with_valid_login_passes(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    monkeypatch.setattr(wapp, "device_login_is_valid", lambda name: True)
    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "apply_settings", lambda f: {"ok": True, "written": [], "errors": []})
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(
        wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"}
    )
    # Install stage guard (bug class fixed 2026-09-02) — see _ok_stack's
    # own comment for why a successful apply always reaches this stage.
    monkeypatch.setattr(wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({}))
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: pytest.fail(f"unexpected real install stage reached: {key!r}")
    )

    form = dict(GOOD_FORM)
    form["provider"] = {
        "name": "openai-codex",
        "env_var": None,
        "api_key": "",
        "base_url": "",
        "model": "gpt-5.3-codex",
    }
    form["fallback"] = None

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_device_code_check_never_runs_for_api_key_providers(logged_in, monkeypatch):
    """The gate must be scoped to DEVICE_CODE_PROVIDERS only — an ordinary
    api_key provider (openrouter, GOOD_FORM's default) must never even
    call device_login_is_valid, which would otherwise be a pointless
    (and, for some providers, network-touching) no-op check on every
    single submission."""
    from hermes_cli.setup_wizard import app as wapp

    def _fail_if_called(name):
        pytest.fail("device_login_is_valid must not run for an api_key provider")

    monkeypatch.setattr(wapp, "device_login_is_valid", _fail_if_called)
    monkeypatch.setattr(
        wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"}
    )
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True, "reachable": True})
    monkeypatch.setattr(wapp, "apply_settings", lambda f: {"ok": True, "written": [], "errors": []})
    monkeypatch.setattr(wapp, "restart_gateway", lambda: {"ok": True, "message": ""})
    monkeypatch.setattr(
        wapp, "wait_bot_alive", lambda *a, **k: {"ok": True, "username": "trixbot"}
    )
    # Install stage guard (bug class fixed 2026-09-02) — see _ok_stack's
    # own comment for why a successful apply always reaches this stage.
    monkeypatch.setattr(wapp, "wizard_tool_blocks", lambda: _fake_tool_blocks({}))
    monkeypatch.setattr(
        wapp, "run_tool_install", lambda key: pytest.fail(f"unexpected real install stage reached: {key!r}")
    )

    r = logged_in.post("/api/submit", json=dict(GOOD_FORM))

    assert r.status_code == 200
    assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# Finding 1/4 (owner-approved fix, reversed from an earlier design):
# `_run_submit` used to compute `form["tool_env_clear"]` — a supposedly
# shared-key-safe subset of a cleared category's own env vars to delete
# from `.env` — before handing the form to apply_settings(). The review
# reproduced a real credential-loss bug on a live catalog: the safety
# computation only ever knew about the wizard's own eight categories, not
# every OTHER Hermes subsystem that might depend on the same credential
# (`vision`'s toolset, `auxiliary` tasks, the credential pool, a provider
# the client used to run and might switch back to) — so turning off
# "Генерация изображений" on a deepseek install deleted OPENAI_API_KEY/
# OPENROUTER_API_KEY/KREA_API_KEY out from under them, with no way for the
# client to recover the value (the wizard never echoes a saved secret
# back). The mechanism is retired entirely: a category-clear now only ever
# removes config.yaml's `"<category>.provider"` and the category's
# toolset, never touches `.env`. These tests exercise the REAL live
# catalog (`wizard_tool_blocks()` is not mocked) and, for the invariant
# test, the REAL `apply_settings()` too — the point is to prove nothing
# gets deleted end to end, not just that `_run_submit` stopped computing a
# list.
# ---------------------------------------------------------------------------


def test_disabling_a_category_deletes_no_env_var_from_env(logged_in, monkeypatch):
    """Reproduces the review's exact finding on the real catalog: clearing
    "Генерация изображений" must never touch OPENAI_API_KEY/
    OPENROUTER_API_KEY/KREA_API_KEY — none of which belong to image_gen's
    OWN row (fal/deepinfra don't use them), but all of which used to be in
    the same category's shared-key blast radius under the old mechanism."""
    from hermes_cli.config import get_env_value, save_env_value_secure
    from hermes_cli.setup_wizard import app as wapp
    from hermes_cli.setup_wizard.apply import apply_settings as real_apply_settings

    _ok_stack(monkeypatch, wapp)
    # apply_settings runs for REAL here — every other test in this file
    # stubs it to capture the form; proving "nothing got deleted" requires
    # the actual .env writer, not a stand-in that never touches disk.
    monkeypatch.setattr(wapp, "apply_settings", real_apply_settings)
    # This section's whole point is exercising the REAL live catalog (see
    # the block comment above) — undo _ok_stack's fake one, without letting
    # a REAL installer run (see _real_catalog_no_real_install's own
    # docstring for why that's a no-op stub, not a fail-loud one, here).
    _real_catalog_no_real_install(monkeypatch, wapp)

    save_env_value_secure("OPENAI_API_KEY", "sk-openai-preexisting")
    save_env_value_secure("KREA_API_KEY", "krea-preexisting")
    save_env_value_secure("XAI_API_KEY", "xai-preexisting")

    form = dict(GOOD_FORM)
    form["tool_provider"] = {"image_gen": None}

    r = logged_in.post("/api/submit", json=form)

    assert r.json()["ok"] is True, r.json()
    assert get_env_value("OPENAI_API_KEY") == "sk-openai-preexisting"
    assert get_env_value("KREA_API_KEY") == "krea-preexisting"
    assert get_env_value("XAI_API_KEY") == "xai-preexisting"


def _capturing_ok_stack(monkeypatch, wapp):
    """`_ok_stack` + apply_settings that records the form it received.

    Also undoes `_ok_stack`'s fake catalog — this section's whole point
    (see the block comment above) is exercising the REAL live catalog —
    without letting a REAL installer run (see
    `_real_catalog_no_real_install`'s own docstring)."""
    _ok_stack(monkeypatch, wapp)
    _real_catalog_no_real_install(monkeypatch, wapp)
    captured = {}

    def _capture_apply(f):
        captured.update(f)
        return {"ok": True, "written": [], "errors": []}

    monkeypatch.setattr(wapp, "apply_settings", _capture_apply)
    return captured


# One POST per test: a successful submit marks the wizard completed, so a
# second POST through the same client would only ever see the 410 gate —
# never the code path these are asserting about.


def test_plain_submission_never_carries_tool_env_clear(logged_in, monkeypatch):
    """The retired `tool_env_clear` mechanism must not exist in
    `_run_submit` at all any more (finding 1/4, owner-approved fix)."""
    from hermes_cli.setup_wizard import app as wapp

    captured = _capturing_ok_stack(monkeypatch, wapp)
    r = logged_in.post("/api/submit", json=dict(GOOD_FORM))
    assert r.json()["ok"] is True, r.json()
    assert "tool_env_clear" not in captured


def test_category_clear_never_carries_tool_env_clear(logged_in, monkeypatch):
    """Even the submission shape that used to TRIGGER the env-key sweep
    (a category clear) must not resurrect it — asserted on the exact form
    apply_settings receives, so this fails loudly (not just "empty list")
    if the computation is ever reintroduced."""
    from hermes_cli.setup_wizard import app as wapp

    captured = _capturing_ok_stack(monkeypatch, wapp)
    form = dict(GOOD_FORM)
    form["tool_provider"] = {"x_search": None}
    r = logged_in.post("/api/submit", json=form)
    assert r.json()["ok"] is True, r.json()
    assert captured["tool_provider"] == {"x_search": None}
    assert "tool_env_clear" not in captured


# ---------------------------------------------------------------------------
# Finding 8 (owner-approved fix): `form.tool_provider`'s ``null`` path now
# validates ``cat_key`` against ``_TOOL_ENV_CATEGORIES`` the same way a
# non-empty value already did (indirectly, via an empty legal-values set —
# see `_run_submit`'s own comment). A session-authenticated client naming
# an arbitrary category with a ``null`` value used to sail straight
# through validation.
# ---------------------------------------------------------------------------


def test_tool_provider_unknown_category_null_rejected_before_apply(logged_in, monkeypatch):
    from hermes_cli.setup_wizard import app as wapp

    # The live telegram/key checks run BEFORE the tool_provider catalog
    # check in `_run_submit`'s fixed order — mock them green so the 422
    # this test asserts can only come from the category validation itself.
    monkeypatch.setattr(wapp, "check_telegram_token", lambda *a: {"ok": True, "username": "trixbot"})
    monkeypatch.setattr(wapp, "check_provider_key", lambda *a: {"ok": True})
    calls = []
    monkeypatch.setattr(wapp, "apply_settings", lambda f: calls.append(f))

    form = dict(GOOD_FORM)
    form["tool_provider"] = {"not_a_real_category": None}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert r.json()["errors"]["tool_provider"]
    assert calls == []


# ---------------------------------------------------------------------------
# Finding 13: a malformed proxy 422s on the `proxy` field, not on
# `telegram_token` (the old blanket "can't reach Telegram" misattribution).
# `check_proxy_syntax` itself is real here (not mocked) — the whole point is
# that it's cheap enough to run unconditionally before any live check.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_proxy", ["1.2.3.4:1080", "socks://user:pass@host:1080"])
def test_malformed_proxy_422s_on_the_proxy_field(logged_in, monkeypatch, bad_proxy):
    from hermes_cli.setup_wizard import app as wapp

    def _fail_if_called(*a, **k):
        pytest.fail("a live check must never run once proxy syntax already failed")

    monkeypatch.setattr(wapp, "check_telegram_token", _fail_if_called)
    monkeypatch.setattr(wapp, "check_provider_key", _fail_if_called)
    monkeypatch.setattr(wapp, "apply_settings", _fail_if_called)

    form = {**GOOD_FORM, "proxy": bad_proxy}
    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    errors = r.json()["errors"]
    assert "proxy" in errors
    assert "telegram_token" not in errors
    assert "provider.api_key" not in errors


def test_well_formed_proxy_still_reaches_the_live_checks(logged_in, monkeypatch):
    """The new syntax gate must not block a legitimately formatted proxy."""
    from hermes_cli.setup_wizard import app as wapp

    _ok_stack(monkeypatch, wapp)
    form = {**GOOD_FORM, "proxy": "socks5://u:p@h:1080"}

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 200
    assert r.json()["ok"] is True


# --- Часовой пояс (спека 11) -------------------------------------------


def test_first_time_missing_timezone_is_required(logged_in, monkeypatch):
    """Первая установка: пояса нет ни в форме, ни в конфиге.

    Пропуск обязан упереться в 422, а не тихо проехать: пустой ключ
    означает системное время машины хостера, и клиент об этом никогда не
    узнает — задачи просто начнут срабатывать не в своё время.
    """
    from hermes_cli.setup_wizard import app as wapp

    applied = []
    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(wapp, "apply_settings", lambda f: applied.append(f))
    form = dict(GOOD_FORM)
    form.pop("timezone", None)

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert "timezone" in r.json()["errors"]
    assert applied == []


def test_unknown_timezone_is_rejected_and_nothing_is_written(logged_in, monkeypatch):
    """Список уезжает браузеру, но доказательством не является."""
    from hermes_cli.setup_wizard import app as wapp

    applied = []
    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(wapp, "apply_settings", lambda f: applied.append(f))
    form = dict(GOOD_FORM)
    form["timezone"] = "Europe/Nowhere"

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 422
    assert "timezone" in r.json()["errors"]
    assert applied == []


def test_saved_timezone_makes_an_omitted_one_a_legal_no_op(tmp_path, monkeypatch, logged_in):
    """Возвратный клиент правит один прокси — переспрашивать пояс незачем.

    Тот же контракт, что у токена: пустое значение законно ровно тогда,
    когда ответ уже сохранён.
    """
    import yaml

    from hermes_constants import get_hermes_home
    from hermes_cli.setup_wizard import app as wapp

    get_hermes_home().mkdir(parents=True, exist_ok=True)
    (get_hermes_home() / "config.yaml").write_text(
        yaml.safe_dump({"timezone": "Asia/Omsk"}), encoding="utf-8"
    )
    _ok_stack(monkeypatch, wapp)
    form = dict(GOOD_FORM)
    form.pop("timezone", None)

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_submitted_timezone_reaches_the_writer(logged_in, monkeypatch):
    """Точка вызова, а не только проверка: значение обязано доехать до
    `apply_settings`. Возврат этой строки на «не передавать» оставил бы
    проверку выше зелёной и молча перестал бы сохранять пояс."""
    from hermes_cli.setup_wizard import app as wapp

    seen = {}
    _ok_stack(monkeypatch, wapp)
    monkeypatch.setattr(
        wapp,
        "apply_settings",
        lambda f: (seen.update(f), {"ok": True, "written": [], "errors": []})[1],
    )
    form = dict(GOOD_FORM)
    form["timezone"] = "Asia/Vladivostok"

    r = logged_in.post("/api/submit", json=form)

    assert r.status_code == 200, r.text
    assert seen["timezone"] == "Asia/Vladivostok"
