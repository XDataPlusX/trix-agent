"""Deterministic core of the support bot (spec 15) — behavior/invariant tests.

Mirrors ``AGENTS.md``'s testing rules: every test here asserts a relation
("every handler takes zero parameters", "'fixed' requires a successful
recheck") or exercises a real boundary through a mock of the actual I/O
call, never a snapshot of the current registry's size or contents. Nothing
reads ``hermes_cli/trix_support.py`` as text — every assertion runs the
real functions.

``tests/conftest.py``'s autouse ``_isolate_hermes_home`` fixture already
redirects ``HERMES_HOME`` to a fresh per-test tempdir, so the
``write_internal_report``/``record_feedback`` tests below never touch a
real ``~/.hermes``.
"""

from __future__ import annotations

import inspect
import json
import os
from unittest.mock import MagicMock, patch

import pytest

import hermes_cli.trix_support as ts
from hermes_constants import get_hermes_home


# ---------------------------------------------------------------------------
# The closed registry itself.
# ---------------------------------------------------------------------------


def test_every_action_has_a_nonempty_russian_label():
    for action in ts.SUPPORT_ACTIONS.values():
        assert action.label_ru.strip(), action.action_id
        # Cheap Cyrillic-presence check — this is a client/operator-facing
        # product where every label must actually be Russian, not English
        # left over from a copy-paste.
        assert any("а" <= ch <= "я" or ch == "ё" for ch in action.label_ru.lower()), action.action_id


def test_no_action_names_the_upstream_project_or_a_tool():
    banned = ("hermes", "nous")
    for action in ts.SUPPORT_ACTIONS.values():
        lowered = action.label_ru.lower()
        for word in banned:
            assert word not in lowered, (action.action_id, action.label_ru)


def test_every_registered_handler_takes_zero_parameters():
    """Security-critical invariant (spec's "жёсткое ограничение"): a support
    action can never accept a caller-supplied parameter — there is no slot
    for a path, a package name, or a command fragment to flow through.
    """
    handled = [a for a in ts.SUPPORT_ACTIONS.values() if a.handler is not None]
    assert handled, "expected at least one implemented action to check"
    for action in handled:
        params = inspect.signature(action.handler).parameters
        assert params == {}, (action.action_id, list(params))


def test_support_action_construction_rejects_a_parameterized_handler():
    """Direct proof the guard is load-bearing, not just an accident of the
    current registry: constructing a ``SupportAction`` around a handler
    that takes ANY parameter must fail immediately, the same way a
    mutation smuggling a "command with a parameter from the model's
    answer" into the registry (acceptance criterion 2) would.
    """

    def _handler_with_a_parameter(command: str) -> dict:  # pragma: no cover - never called
        return {"ok": True, "ran": command}

    with pytest.raises(TypeError):
        ts.SupportAction("evil", "тестовое действие с параметром", "fix", True, _handler_with_a_parameter)


def test_unimplemented_actions_carry_no_handler():
    unimplemented = [a for a in ts.SUPPORT_ACTIONS.values() if not a.implemented]
    assert unimplemented, "expected at least one unimplemented placeholder action"
    for action in unimplemented:
        assert action.handler is None, action.action_id
        assert action.kind == "fix", action.action_id  # every placeholder in this task is a fix


def test_check_order_is_exactly_the_implemented_check_actions():
    registered_checks = {
        action_id for action_id, action in ts.SUPPORT_ACTIONS.items() if action.kind == "check"
    }
    assert set(ts.CHECK_ORDER) == registered_checks
    assert len(ts.CHECK_ORDER) == len(set(ts.CHECK_ORDER))  # no duplicates


def test_action_dict_keys_match_their_own_action_id():
    for key, action in ts.SUPPORT_ACTIONS.items():
        assert key == action.action_id


# ---------------------------------------------------------------------------
# _classify_check_outcome — the three-way verdict, as a pure function.
# ---------------------------------------------------------------------------


def _result(ok: bool, action_id: str = "x") -> ts.ActionRunResult:
    return ts.ActionRunResult(
        action_id=action_id, ok=ok, error=None if ok else "failed",
        detail={"ok": ok}, started_at="t0", finished_at="t1", duration_s=0.01,
    )


def test_outcome_good_when_initial_check_already_ok():
    outcome = ts._classify_check_outcome(_result(True), None, None)
    assert outcome == "good"


def test_outcome_not_fixed_when_no_fix_was_attempted():
    outcome = ts._classify_check_outcome(_result(False), None, None)
    assert outcome == "not_fixed"


def test_outcome_fixed_requires_a_genuinely_successful_recheck():
    """The load-bearing invariant from the brief: "вердикт «починили»
    невозможен без успешной перепроверки". A fix that CLAIMS success
    (``fix.ok=True``) must not be enough on its own — only the recheck's
    own ``ok`` decides.
    """
    initial = _result(False)
    fix_claims_success = _result(True)
    recheck_still_broken = _result(False)
    outcome = ts._classify_check_outcome(initial, fix_claims_success, recheck_still_broken)
    assert outcome == "not_fixed"


def test_outcome_fixed_when_recheck_actually_succeeds():
    initial = _result(False)
    fix = _result(True)
    recheck_now_ok = _result(True)
    outcome = ts._classify_check_outcome(initial, fix, recheck_now_ok)
    assert outcome == "fixed"


# ---------------------------------------------------------------------------
# build_client_report — client never sees internals.
# ---------------------------------------------------------------------------


def _pass_result(*, ok: bool, checks: tuple) -> ts.SupportPassResult:
    return ts.SupportPassResult(
        run_id="run-1", started_at="t0", finished_at="t1", checks=checks, ok=ok,
    )


def test_client_report_all_good():
    checks = (ts.CheckOutcome("telegram_token", _result(True), None, None, "good"),)
    result = _pass_result(ok=True, checks=checks)
    assert ts.build_client_report(result) == ts._MSG_CLIENT_ALL_GOOD


def test_client_report_mentions_a_fix_only_when_one_actually_happened():
    fixed_check = ts.CheckOutcome("doctor_no_fix", _result(False), _result(True), _result(True), "fixed")
    result = _pass_result(ok=True, checks=(fixed_check,))
    assert ts.build_client_report(result) == ts._MSG_CLIENT_FIXED


def test_client_report_not_fixed_names_the_single_escalation_contact():
    broken_check = ts.CheckOutcome("telegram_token", _result(False), None, None, "not_fixed")
    result = _pass_result(ok=False, checks=(broken_check,))
    report = ts.build_client_report(result)
    assert ts.SUPPORT_ESCALATION_CONTACT in report
    # No other escalation channel is ever named (spec owner ruling 5).
    for other_channel in ("@", "http://", "https://", "почт", "телефон"):
        if other_channel == "@":
            assert report.count("@") == 1
        else:
            assert other_channel not in report.lower()


def test_client_report_never_leaks_a_check_id_or_internal_error_text():
    """Acceptance criterion 3: log/internal text must not reach the client
    at the point the outgoing text is ASSEMBLED, not merely at the point
    logs are read. Plant a distinctive internal error string and assert
    it cannot appear in any of the three possible client messages.
    """
    secret_marker = "TRACEBACK_INTERNAL_ONLY_9f3a"
    initial = ts.ActionRunResult(
        action_id="telegram_token", ok=False, error=secret_marker,
        detail={"ok": False, "error": secret_marker, "raw": "Traceback (most recent call last): " + secret_marker},
        started_at="t0", finished_at="t1", duration_s=0.01,
    )
    broken_check = ts.CheckOutcome("telegram_token", initial, None, None, "not_fixed")
    result = _pass_result(ok=False, checks=(broken_check,))
    report = ts.build_client_report(result)
    assert secret_marker not in report
    assert "telegram_token" not in report
    assert "Traceback" not in report


# ---------------------------------------------------------------------------
# _execute — isolation and timeout handling.
# ---------------------------------------------------------------------------


def test_execute_isolates_a_raising_action_instead_of_propagating():
    def _boom() -> dict:
        raise RuntimeError("kaboom")

    result = ts._execute("boom_action", _boom, timeout=5.0)
    assert result.ok is False
    assert "kaboom" in (result.error or "")


def test_execute_treats_a_non_dict_return_as_failure_not_a_crash():
    def _wrong_shape() -> dict:
        return "not a dict"  # type: ignore[return-value]

    result = ts._execute("wrong_shape", _wrong_shape, timeout=5.0)
    assert result.ok is False


def test_execute_bounds_a_slow_action_by_its_timeout():
    import time

    def _slow() -> dict:
        time.sleep(1.0)
        return {"ok": True}

    started = time.monotonic()
    result = ts._execute("slow_action", _slow, timeout=0.05)
    elapsed = time.monotonic() - started
    assert result.ok is False
    assert elapsed < 0.9  # bounded well under the action's own sleep


# ---------------------------------------------------------------------------
# _check_gateway_state — the restored spec check 5, on its own.
# ---------------------------------------------------------------------------


def _gateway_state_boundaries(monkeypatch, *, applicable: bool = True, active: bool = True):
    """Patch ``_check_gateway_state``'s real boundaries directly (its own
    origin modules — ``hermes_cli.gateway``/``hermes_cli.service_manager``
    — plus ``trix_support``'s own subprocess wrapper), never
    ``_check_gateway_state`` itself.
    """
    import subprocess as _subprocess

    monkeypatch.setattr("hermes_cli.gateway.is_linux", lambda: True)
    monkeypatch.setattr("hermes_cli.service_manager.detect_service_manager", lambda: "systemd")
    unit_path = MagicMock()
    unit_path.exists.return_value = applicable
    unit_path.stem = "hermes-gateway-test"
    monkeypatch.setattr("hermes_cli.gateway.get_systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(
        ts,
        "_run_gateway_systemctl_is_active",
        lambda unit_name: _subprocess.CompletedProcess(
            args=[], returncode=0, stdout=("active" if active else "inactive"), stderr="",
        ),
    )


def test_check_gateway_state_stays_silent_off_linux(monkeypatch):
    monkeypatch.setattr("hermes_cli.gateway.is_linux", lambda: False)
    result = ts._check_gateway_state()
    assert result == {"ok": True, "applicable": False, "reason": "not_linux"}


def test_check_gateway_state_stays_silent_under_s6(monkeypatch):
    monkeypatch.setattr("hermes_cli.gateway.is_linux", lambda: True)
    monkeypatch.setattr("hermes_cli.service_manager.detect_service_manager", lambda: "s6")
    result = ts._check_gateway_state()
    assert result == {"ok": True, "applicable": False, "reason": "s6_container"}


def test_check_gateway_state_stays_silent_when_unit_never_provisioned(monkeypatch):
    _gateway_state_boundaries(monkeypatch, applicable=False)
    result = ts._check_gateway_state()
    assert result["ok"] is True
    assert result["applicable"] is False
    assert result["reason"] == "unit_absent"


def test_check_gateway_state_ok_when_active(monkeypatch):
    _gateway_state_boundaries(monkeypatch, active=True)
    result = ts._check_gateway_state()
    assert result["ok"] is True
    assert result["applicable"] is True


def test_check_gateway_state_not_ok_when_inactive(monkeypatch):
    _gateway_state_boundaries(monkeypatch, active=False)
    result = ts._check_gateway_state()
    assert result["ok"] is False
    assert result["applicable"] is True


# ---------------------------------------------------------------------------
# run_support_pass — full orchestration, every real boundary mocked.
# ---------------------------------------------------------------------------


@pytest.fixture
def _mocked_pass_boundaries(monkeypatch):
    """Mock every real I/O boundary ``run_support_pass`` crosses so the
    orchestration test below is hermetic (no network, no subprocess, no
    platform-dependent branching) and fast. Each patch target is the
    ORIGIN module of a genuine network/subprocess call — never our own
    orchestration logic — per the task's "подменяй границы" rule.

    ``gateway_state``'s own boundary defaults to a HEALTHY, applicable
    gateway (systemd, unit present, ``is-active`` → ``active``) — the
    realistic default for a provisioned client VM, and what makes the
    "healthy gateway is never touched" test below exercise the real
    condition instead of short-circuiting on "not applicable here".

    A token must actually be set for ``telegram_token``/``gateway_restart``
    to reach their mocked boundary at all — both wrappers short-circuit to
    an "not настроен" failure before calling anything when
    ``TELEGRAM_BOT_TOKEN`` is empty (the isolated test HERMES_HOME's
    ``.env`` starts empty), which would otherwise make the mocks below
    dead weight the test never actually exercises.
    """
    import subprocess

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    unit_path = MagicMock()
    unit_path.exists.return_value = True
    unit_path.stem = "hermes-gateway-test"
    with (
        patch(
            "hermes_cli.setup_wizard.validate.check_telegram_token",
            side_effect=RuntimeError("Telegram недоступен из теста"),
        ) as telegram_token,
        patch(
            "hermes_cli.setup_wizard.validate.check_reachability",
            return_value={"telegram": True, "via_proxy": {}, "direct": {}, "proxy_invalid": False},
        ) as reachability,
        patch(
            "hermes_cli.docker_preflight.check_docker_backend",
        ) as docker,
        patch(
            "hermes_cli.browser_preflight.check_chromium_backend",
        ) as browser,
        patch(
            "hermes_cli.search_preflight.check_ddgs_backend",
        ) as search,
        patch(
            "hermes_cli.trix_setup_service_check.check_trix_setup_service",
        ) as wizard_service,
        patch("hermes_cli.gateway.is_linux", return_value=True),
        patch("hermes_cli.service_manager.detect_service_manager", return_value="systemd"),
        patch("hermes_cli.gateway.get_systemd_unit_path", return_value=unit_path),
        patch.object(ts, "_run_gateway_systemctl_is_active") as gw_systemctl,
        patch.object(ts, "_run_doctor_subprocess") as doctor_subprocess,
        patch(
            "hermes_cli.setup_wizard.gateway_ctl.restart_gateway",
            return_value={"ok": True, "message": "Шлюз перезапущен", "pre_pid": 1, "pre_platform_stamp": "s0"},
        ) as gateway_restart,
        patch(
            "hermes_cli.setup_wizard.gateway_ctl.wait_bot_alive",
            return_value={"ok": True, "username": "trix_test_bot"},
        ) as wait_bot_alive,
    ):
        docker.return_value.to_dict.return_value = {"check": "docker", "ok": True, "message": "", "details": {}}
        browser.return_value.to_dict.return_value = {"check": "chromium", "ok": True, "message": "", "details": {}}
        search.return_value.to_dict.return_value = {"check": "ddgs", "ok": True, "message": "", "details": {}}

        def _wizard_service_ok(issues):
            return None  # stays silent — no issues appended

        wizard_service.side_effect = _wizard_service_ok

        gw_systemctl.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="active", stderr="",
        )

        # doctor_no_fix reports trouble the first time, and clean the second
        # (post-fix recheck) time — this is what lets the same fixture also
        # prove the fix→recheck chain actually re-invokes the real check.
        bad = subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout=json.dumps({"verdict": "needs_attention", "ok": False, "fixed_count": 0, "remaining_issues": ["x"]}),
            stderr="",
        )
        fixed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"verdict": "ok", "ok": True, "fixed_count": 1, "remaining_issues": []}),
            stderr="",
        )
        doctor_subprocess.side_effect = [bad, fixed, fixed]

        yield {
            "telegram_token": telegram_token,
            "reachability": reachability,
            "docker": docker,
            "browser": browser,
            "search": search,
            "wizard_service": wizard_service,
            "gw_systemctl": gw_systemctl,
            "doctor_subprocess": doctor_subprocess,
            "gateway_restart": gateway_restart,
            "wait_bot_alive": wait_bot_alive,
        }


def _make_doctor_ok_proc():
    import subprocess

    return subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"verdict": "ok", "ok": True, "fixed_count": 0, "remaining_issues": []}),
        stderr="",
    )


def test_run_support_pass_isolates_one_failing_check_and_still_completes(_mocked_pass_boundaries):
    result = ts.run_support_pass()

    by_id = {c.check_id: c for c in result.checks}

    # The check whose boundary raised is isolated to itself...
    assert by_id["telegram_token"].outcome == "not_fixed"
    assert by_id["telegram_token"].initial.ok is False

    # ...and does not stop the rest of the pass from running and succeeding.
    assert by_id["network_proxy"].outcome == "good"
    assert by_id["sandbox"].outcome == "good"
    assert by_id["browser"].outcome == "good"
    assert by_id["search"].outcome == "good"
    assert by_id["wizard_service"].outcome == "good"
    assert by_id["gateway_state"].outcome == "good"

    # doctor_no_fix failed, got fixed, and the RECHECK (a fresh subprocess
    # call, not just the fix's own claim) is what proves it.
    assert by_id["doctor_no_fix"].outcome == "fixed"
    assert by_id["doctor_no_fix"].fix is not None
    assert by_id["doctor_no_fix"].recheck is not None
    assert by_id["doctor_no_fix"].recheck.ok is True

    # gateway_state was already healthy — restart must never be reached.
    _mocked_pass_boundaries["gateway_restart"].assert_not_called()
    _mocked_pass_boundaries["wait_bot_alive"].assert_not_called()

    # Overall verdict reflects the one unresolved check.
    assert result.ok is False


def test_run_support_pass_calls_doctor_subprocess_exactly_thrice_on_a_fix_path(_mocked_pass_boundaries):
    ts.run_support_pass()
    # initial doctor_no_fix + doctor_fix + recheck doctor_no_fix.
    assert _mocked_pass_boundaries["doctor_subprocess"].call_count == 3


def test_run_support_pass_is_all_good_when_every_boundary_is_healthy(_mocked_pass_boundaries):
    _mocked_pass_boundaries["telegram_token"].side_effect = None
    _mocked_pass_boundaries["telegram_token"].return_value = {"ok": True, "username": "trix_test_bot"}
    _mocked_pass_boundaries["doctor_subprocess"].side_effect = None
    _mocked_pass_boundaries["doctor_subprocess"].return_value = _make_doctor_ok_proc()

    result = ts.run_support_pass()
    assert result.ok is True
    assert all(c.outcome == "good" for c in result.checks)
    assert ts.build_client_report(result) == ts._MSG_CLIENT_ALL_GOOD


# ---------------------------------------------------------------------------
# gateway_restart as a CONSEQUENCE of gateway_state, never a ritual
# (coordinator correction 2026-09-03).
# ---------------------------------------------------------------------------


def _make_healthy_pass_boundaries(mocks):
    """Flip the shared fixture's telegram_token/doctor mocks from their
    default "one thing is broken" shape to fully healthy, so only
    ``gateway_state`` is left to vary between the two tests below.
    """
    mocks["telegram_token"].side_effect = None
    mocks["telegram_token"].return_value = {"ok": True, "username": "trix_test_bot"}
    mocks["doctor_subprocess"].side_effect = None
    mocks["doctor_subprocess"].return_value = _make_doctor_ok_proc()


def test_healthy_gateway_is_never_restarted(_mocked_pass_boundaries):
    """The coordinator's главный test: a gateway that is already active must
    never be touched — the earlier unconditional restart broke exactly
    this case (a client pressing the button out of curiosity).
    """
    _make_healthy_pass_boundaries(_mocked_pass_boundaries)
    # gw_systemctl already defaults to "active" (fixture docstring).

    result = ts.run_support_pass()

    by_id = {c.check_id: c for c in result.checks}
    assert by_id["gateway_state"].outcome == "good"
    assert by_id["gateway_state"].fix is None
    assert by_id["gateway_state"].recheck is None

    _mocked_pass_boundaries["gateway_restart"].assert_not_called()
    _mocked_pass_boundaries["wait_bot_alive"].assert_not_called()
    assert _mocked_pass_boundaries["gw_systemctl"].call_count == 1  # the one initial check only
    assert result.ok is True


def test_down_gateway_is_restarted_and_fixed_only_after_a_successful_recheck(_mocked_pass_boundaries):
    """The coordinator's second required direction: restart IS invoked when
    the gateway is down, and "почини­ли" requires the fresh recheck
    (a second, independent ``systemctl is-active``) to actually say
    ``active`` — not merely that ``restart_gateway``/``wait_bot_alive``
    reported success.
    """
    import subprocess

    _make_healthy_pass_boundaries(_mocked_pass_boundaries)
    inactive = subprocess.CompletedProcess(args=[], returncode=0, stdout="inactive", stderr="")
    active = subprocess.CompletedProcess(args=[], returncode=0, stdout="active", stderr="")
    _mocked_pass_boundaries["gw_systemctl"].side_effect = [inactive, active]

    result = ts.run_support_pass()

    by_id = {c.check_id: c for c in result.checks}
    gw = by_id["gateway_state"]
    assert gw.initial.ok is False
    assert gw.fix is not None
    assert gw.recheck is not None
    assert gw.recheck.ok is True
    assert gw.outcome == "fixed"

    _mocked_pass_boundaries["gateway_restart"].assert_called_once()
    _mocked_pass_boundaries["wait_bot_alive"].assert_called_once()
    assert result.ok is True


def test_restart_success_alone_does_not_prove_fixed_without_a_clean_recheck(_mocked_pass_boundaries):
    """Same "fixed requires a genuinely successful recheck" invariant as
    ``test_outcome_fixed_requires_a_genuinely_successful_recheck``, but
    exercised through the REAL ``run_support_pass`` orchestration and REAL
    boundary mocks rather than synthetic ``ActionRunResult``s: even though
    ``restart_gateway``/``wait_bot_alive`` both report success (the fix's
    own claim), a recheck that still observes ``inactive`` must yield
    "not_fixed", not "fixed".
    """
    import subprocess

    _make_healthy_pass_boundaries(_mocked_pass_boundaries)
    still_inactive = subprocess.CompletedProcess(args=[], returncode=0, stdout="inactive", stderr="")
    _mocked_pass_boundaries["gw_systemctl"].side_effect = [still_inactive, still_inactive]

    result = ts.run_support_pass()

    by_id = {c.check_id: c for c in result.checks}
    gw = by_id["gateway_state"]
    assert gw.fix is not None and gw.fix.ok is True  # the fix itself claims success
    assert gw.recheck is not None and gw.recheck.ok is False  # but the recheck disagrees
    assert gw.outcome == "not_fixed"
    assert result.ok is False


# ---------------------------------------------------------------------------
# write_internal_report / record_feedback — our own file under HERMES_HOME.
# ---------------------------------------------------------------------------


def test_write_internal_report_lands_under_isolated_hermes_home():
    checks = (ts.CheckOutcome("telegram_token", _result(True), None, None, "good"),)
    result = _pass_result(ok=True, checks=checks)

    run_id = ts.write_internal_report(result)
    assert run_id == result.run_id

    log_path = get_hermes_home() / "support" / "runs.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["type"] == "run"
    assert record["run_id"] == result.run_id
    assert record["ok"] is True
    assert record["checks"][0]["check_id"] == "telegram_token"


def test_record_feedback_appends_and_correlates_by_run_id():
    checks = (ts.CheckOutcome("telegram_token", _result(False), None, None, "not_fixed"),)
    result = _pass_result(ok=False, checks=checks)
    run_id = ts.write_internal_report(result)

    ts.record_feedback(run_id, helped=False, note="бот всё ещё не отвечает")

    log_path = get_hermes_home() / "support" / "runs.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    feedback = lines[1]
    assert feedback["type"] == "feedback"
    assert feedback["run_id"] == run_id
    assert feedback["helped"] is False
    assert feedback["note"] == "бот всё ещё не отвечает"


# ---------------------------------------------------------------------------
# The three newly-implemented fixes: ensure_tool, sandbox_image, disk_cleanup.
# config_edit stays deliberately unimplemented (owner decision 2026-09-03).
# ---------------------------------------------------------------------------


def test_config_edit_is_still_the_only_unimplemented_action():
    """Owner ruling 2026-09-03: config_edit will not be built. Everything
    else that was named alongside it in the brief must now be implemented
    — this pins that split so a future edit can't silently leave a second
    action unimplemented (or accidentally implement config_edit) without a
    test noticing.
    """
    unimplemented = {a.action_id for a in ts.SUPPORT_ACTIONS.values() if not a.implemented}
    assert unimplemented == {"config_edit"}
    assert ts.SUPPORT_ACTIONS["config_edit"].handler is None
    for action_id in ("ensure_tool", "sandbox_image", "disk_cleanup"):
        assert ts.SUPPORT_ACTIONS[action_id].implemented is True
        assert ts.SUPPORT_ACTIONS[action_id].handler is not None


# --- ensure_tool -----------------------------------------------------------


def test_ensure_tool_does_nothing_when_browser_already_ok(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.browser_preflight.check_chromium_backend",
        lambda: MagicMock(ok=True),
    )
    with patch.object(ts, "_run_ensure_tool_subprocess") as run_subprocess:
        result = ts._fix_ensure_tool()
    run_subprocess.assert_not_called()
    assert result["ok"] is True
    assert result["already"] is True


def test_ensure_tool_refuses_honestly_when_installer_is_missing(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.browser_preflight.check_chromium_backend",
        lambda: MagicMock(ok=False),
    )
    monkeypatch.setattr("hermes_cli.dep_ensure._find_install_script", lambda: (None, None))
    with patch.object(ts, "_run_ensure_tool_subprocess") as run_subprocess:
        result = ts._fix_ensure_tool()
    run_subprocess.assert_not_called()
    assert result["ok"] is False
    assert result["error"]


def test_ensure_tool_invokes_install_sh_ensure_node_browser(monkeypatch, tmp_path):
    import subprocess as _subprocess

    fake_script = tmp_path / "install.sh"
    fake_script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(
        "hermes_cli.dep_ensure._find_install_script", lambda: (fake_script, "bash")
    )

    calls = {"ok_calls": 0}

    def _fake_chromium_check():
        calls["ok_calls"] += 1
        # Not ready before the install, ready after — proves the handler
        # re-checks rather than trusting the subprocess's exit code alone.
        return MagicMock(ok=calls["ok_calls"] > 1)

    monkeypatch.setattr("hermes_cli.browser_preflight.check_chromium_backend", _fake_chromium_check)

    captured = {}

    def _fake_run(cmd, env, timeout):
        captured["cmd"] = cmd
        captured["env"] = env
        captured["timeout"] = timeout
        return _subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch.object(ts, "_run_ensure_tool_subprocess", side_effect=_fake_run):
        result = ts._fix_ensure_tool()

    assert result["ok"] is True
    assert result["already"] is False
    assert captured["cmd"] == ["bash", str(fake_script), "--ensure", "node,browser"]
    assert captured["env"]["IS_INTERACTIVE"] == "false"
    assert captured["timeout"] == ts._ENSURE_TOOL_TIMEOUT


def test_ensure_tool_reports_honest_failure_when_recheck_still_fails(monkeypatch, tmp_path):
    import subprocess as _subprocess

    fake_script = tmp_path / "install.sh"
    fake_script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(
        "hermes_cli.dep_ensure._find_install_script", lambda: (fake_script, "bash")
    )
    monkeypatch.setattr(
        "hermes_cli.browser_preflight.check_chromium_backend", lambda: MagicMock(ok=False)
    )

    def _fake_run(cmd, env, timeout):
        # install.sh's own ensure_browser() always `return 0` even on a
        # failed Chromium fetch -- the handler must not trust this alone.
        return _subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="Chromium install failed."
        )

    with patch.object(ts, "_run_ensure_tool_subprocess", side_effect=_fake_run):
        result = ts._fix_ensure_tool()

    assert result["ok"] is False
    assert "Chromium install failed." in result["error"]


def test_ensure_tool_kills_a_genuinely_wedged_install(monkeypatch, tmp_path):
    import subprocess as _subprocess

    fake_script = tmp_path / "install.sh"
    fake_script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(
        "hermes_cli.dep_ensure._find_install_script", lambda: (fake_script, "bash")
    )
    monkeypatch.setattr(
        "hermes_cli.browser_preflight.check_chromium_backend", lambda: MagicMock(ok=False)
    )

    def _fake_run(cmd, env, timeout):
        raise _subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    with patch.object(ts, "_run_ensure_tool_subprocess", side_effect=_fake_run):
        result = ts._fix_ensure_tool()

    assert result["ok"] is False
    assert "900" in result["error"]


# --- sandbox_image -----------------------------------------------------------


def _patch_sandbox_image_name(monkeypatch, image: str = "nikolaik/python-nodejs:python3.11-nodejs20"):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"terminal": {"docker_image": image}},
    )


def test_sandbox_image_refuses_honestly_without_docker(monkeypatch):
    monkeypatch.setattr("tools.environments.docker.find_docker", lambda: None)
    result = ts._fix_sandbox_image()
    assert result["ok"] is False
    assert "docker" in result["error"].lower() or "Docker" in result["error"]


def test_sandbox_image_already_present_does_not_pull_again(monkeypatch):
    """Mutation-required invariant: 'образ уже есть — повторной загрузки
    не происходит'. docker pull must never be invoked when 'docker image
    inspect' already reports the image present.
    """
    _patch_sandbox_image_name(monkeypatch)
    monkeypatch.setattr("tools.environments.docker.find_docker", lambda: "/usr/bin/docker")

    import subprocess as _subprocess

    def _fake_run(cmd, timeout):
        assert cmd[1:3] == ["image", "inspect"], f"unexpected docker call: {cmd}"
        return _subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch.object(ts, "_run_sandbox_docker_subprocess", side_effect=_fake_run) as run_subprocess:
        with patch("tools.environments.docker.reap_orphan_containers") as reap:
            result = ts._fix_sandbox_image()

    assert result["ok"] is True
    assert result["already"] is True
    reap.assert_not_called()
    # Exactly one call -- the inspect -- and never a "pull".
    assert run_subprocess.call_count == 1
    for call in run_subprocess.call_args_list:
        assert "pull" not in call.args[0]


def test_sandbox_image_pulls_and_rechecks_when_missing(monkeypatch):
    _patch_sandbox_image_name(monkeypatch, image="custom/sandbox:v1")
    monkeypatch.setattr("tools.environments.docker.find_docker", lambda: "/usr/bin/docker")

    import subprocess as _subprocess

    calls = []

    def _fake_run(cmd, timeout):
        calls.append(cmd)
        if cmd[1:3] == ["image", "inspect"]:
            # Absent before the pull, present after -- an independent
            # recheck, not the pull's own exit code alone.
            present = any(c[1] == "pull" for c in calls[:-1])
            return _subprocess.CompletedProcess(
                args=cmd, returncode=0 if present else 1, stdout="", stderr="",
            )
        assert cmd == ["/usr/bin/docker", "pull", "custom/sandbox:v1"]
        return _subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch.object(ts, "_run_sandbox_docker_subprocess", side_effect=_fake_run):
        with patch("tools.environments.docker.reap_orphan_containers") as reap:
            result = ts._fix_sandbox_image()

    assert result["ok"] is True
    assert result["already"] is False
    assert result["image"] == "custom/sandbox:v1"
    reap.assert_called_once_with(max_age_seconds=0)


def test_sandbox_image_reports_honest_failure_when_pull_leaves_it_missing(monkeypatch):
    _patch_sandbox_image_name(monkeypatch)
    monkeypatch.setattr("tools.environments.docker.find_docker", lambda: "/usr/bin/docker")

    import subprocess as _subprocess

    def _fake_run(cmd, timeout):
        if cmd[1:3] == ["image", "inspect"]:
            return _subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
        return _subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch.object(ts, "_run_sandbox_docker_subprocess", side_effect=_fake_run):
        with patch("tools.environments.docker.reap_orphan_containers") as reap:
            result = ts._fix_sandbox_image()

    assert result["ok"] is False
    reap.assert_not_called()


def test_sandbox_image_kills_a_genuinely_wedged_pull(monkeypatch):
    _patch_sandbox_image_name(monkeypatch)
    monkeypatch.setattr("tools.environments.docker.find_docker", lambda: "/usr/bin/docker")

    import subprocess as _subprocess

    def _fake_run(cmd, timeout):
        if cmd[1:3] == ["image", "inspect"]:
            return _subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
        raise _subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    with patch.object(ts, "_run_sandbox_docker_subprocess", side_effect=_fake_run):
        result = ts._fix_sandbox_image()

    assert result["ok"] is False
    assert "600" in result["error"]


# --- disk_cleanup ------------------------------------------------------------
#
# These exercise the REAL hermes_cli.trix_disk.clean() against the isolated
# per-test HERMES_HOME (tests/conftest.py's autouse fixture) -- no mocking
# of the protection logic itself, per the brief: this handler only calls
# trix_disk, it must never re-derive or weaken what trix_disk protects.


def _write(path, content: bytes = b"junk"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _no_docker_prune(*args, **kwargs):
    """Real boundary substitute for ``trix_disk.docker_prune`` -- a plain
    ``subprocess.run(["docker", ...])`` in the real function, which must
    never actually run in a test (no real docker mutation, no dependence
    on whether docker happens to be installed on the test host)."""
    return 0


def test_disk_cleanup_removes_ordinary_service_files(monkeypatch):
    home = get_hermes_home()
    _write(home / "logs" / "old.log", b"x" * 4096)

    monkeypatch.setattr("hermes_cli.trix_disk.docker_prune", _no_docker_prune)
    result = ts._fix_disk_cleanup()

    assert result["ok"] is True
    assert result["freed_bytes"] > 0
    assert not (home / "logs" / "old.log").exists()


def test_disk_cleanup_reports_idempotent_success_when_nothing_to_clean(monkeypatch):
    monkeypatch.setattr("hermes_cli.trix_disk.docker_prune", _no_docker_prune)
    result = ts._fix_disk_cleanup()
    assert result["ok"] is True
    assert result["freed_bytes"] == 0
    assert result["removed_labels"] == []
    assert result["errors"] == []


def test_disk_cleanup_never_touches_client_files_or_the_workspace(monkeypatch):
    """Mutation-required invariant: присланные клиентом файлы и рабочая
    папка не удаляются никогда. Real trix_disk.clean() protection logic,
    exercised end to end -- byte-for-byte comparison, not just
    existence, so a mutation that truncates/rewrites instead of deleting
    would also be caught.
    """
    import hashlib

    monkeypatch.setattr("hermes_cli.trix_disk.docker_prune", _no_docker_prune)
    home = get_hermes_home()

    client_file = home / "cache" / "documents" / "invoice_from_client.pdf"
    client_bytes = os.urandom(4096)
    _write(client_file, client_bytes)

    workspace_file = home / "sandboxes" / "docker" / "task-1" / "workspace" / "notes.py"
    workspace_bytes = os.urandom(2048)
    _write(workspace_file, workspace_bytes)

    # Ordinary removable service junk alongside them, so the pass has
    # something real to clean and isn't a no-op.
    _write(home / "logs" / "old.log", b"y" * (200 * 1024))

    before_client_hash = hashlib.sha256(client_bytes).hexdigest()
    before_workspace_hash = hashlib.sha256(workspace_bytes).hexdigest()

    result = ts._fix_disk_cleanup()

    assert client_file.exists()
    assert workspace_file.exists()
    assert client_file.read_bytes() == client_bytes
    assert workspace_file.read_bytes() == workspace_bytes
    assert hashlib.sha256(client_file.read_bytes()).hexdigest() == before_client_hash
    assert hashlib.sha256(workspace_file.read_bytes()).hexdigest() == before_workspace_hash

    # The service junk alongside them WAS removed -- proves the pass ran
    # for real rather than silently refusing everything.
    assert not (home / "logs" / "old.log").exists()
    assert result["freed_bytes"] > 0
