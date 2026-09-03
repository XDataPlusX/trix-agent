"""Tests for hermes_cli.setup_wizard.gateway_ctl (spec §10.3-10.5, §18.1).

Fix round 1 (review): wait_bot_alive must prove OUR gateway process is
connected, not just that the token/Telegram side is reachable — see
gateway/status.py's read_runtime_status/runtime_status_is_stale/
runtime_status_pid_is_live and the platforms.telegram.state field written
by gateway/platforms/base.py. restart_gateway now also snapshots the
pre-restart PID so wait_bot_alive can distinguish a genuine restart from a
same-PID no-op (e.g. `systemctl start` hitting an already-active unit
after `restart` timed out/failed).

Fix round 2 (re-review): the round-1 pre_pid gate doesn't close the whole
gap — write_runtime_status's read-merge-write refreshes the top-level
pid/start_time/updated_at the instant the NEW process calls it with
gateway_state="starting" (gateway/run.py), well before the Telegram
adapter itself reaches platform_state="connecting"/"connected"/"fatal".
In that window platforms.telegram still holds the OLD process's record
while pid already looks fresh. restart_gateway now also snapshots
platforms.telegram.updated_at (pre_platform_stamp) and wait_bot_alive
trusts a connected/fatal verdict only once that per-platform stamp has
moved. Also: the loop checks the local status verdict before touching the
network, and stops re-calling check_telegram_token once the token has
been confirmed once (was re-hitting getMe on every poll indefinitely);
the timeout message carries the last token-check error when the token
itself never checked out; and platforms/platforms.telegram of the wrong
JSON shape degrade to "unconfirmed" instead of raising.
"""
import subprocess
from unittest.mock import patch, MagicMock


def test_restart_falls_back_to_start():
    from hermes_cli.setup_wizard import gateway_ctl as g
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        rc = 1 if "restart" in cmd else 0
        return MagicMock(returncode=rc, stdout="", stderr="")

    with patch.object(g.subprocess, "run", side_effect=fake_run):
        out = g.restart_gateway()
    assert out["ok"] is True
    assert any("restart" in c for c in calls) and any("start" in c for c in calls)


def test_restart_gateway_returns_pre_restart_pid_snapshot():
    from hermes_cli.setup_wizard import gateway_ctl as g

    def fake_run(cmd, **kw):
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(g.subprocess, "run", side_effect=fake_run), \
         patch.object(g, "read_runtime_status", return_value={"pid": 4242}):
        out = g.restart_gateway()
    assert out["ok"] is True
    assert out["pre_pid"] == 4242


def test_restart_gateway_pre_pid_none_when_no_status_record():
    from hermes_cli.setup_wizard import gateway_ctl as g

    def fake_run(cmd, **kw):
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch.object(g.subprocess, "run", side_effect=fake_run), \
         patch.object(g, "read_runtime_status", return_value=None):
        out = g.restart_gateway()
    assert out["pre_pid"] is None


def test_restart_start_fallback_exception_reports_failure():
    """restart returns nonzero, then the start attempt itself raises."""
    from hermes_cli.setup_wizard import gateway_ctl as g

    def fake_run(cmd, **kw):
        if "restart" in cmd:
            return MagicMock(returncode=1, stdout="", stderr="")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

    with patch.object(g.subprocess, "run", side_effect=fake_run), \
         patch.object(g, "read_runtime_status", return_value=None):
        out = g.restart_gateway()
    assert out["ok"] is False
    assert out["pre_pid"] is None


def test_wait_bot_alive_times_out():
    from hermes_cli.setup_wizard import gateway_ctl as g
    fake_now = [0.0]
    with patch.object(g, "check_telegram_token", return_value={"ok": False, "error": "нет"}), \
         patch.object(g.time, "monotonic", side_effect=lambda: fake_now.__setitem__(0, fake_now[0] + 30) or fake_now[0]), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive("t", None, timeout=90)
    assert out["ok"] is False


def test_wait_bot_alive_succeeds_only_when_token_and_adapter_both_connect():
    """Supersedes the old getMe-only success test (that was the bug: a
    valid token alone doesn't prove OUR gateway holds the connection).
    Polls across two runtime-status snapshots — connecting, then
    connected — while the token is already valid throughout, mirroring
    the real-world shape (token checks out immediately; the adapter
    catches up a beat later)."""
    from hermes_cli.setup_wizard import gateway_ctl as g
    recs = iter([
        {"pid": 111, "platforms": {"telegram": {"state": "connecting"}}},
        {"pid": 111, "platforms": {"telegram": {"state": "connected"}}},
    ])
    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}), \
         patch.object(g, "read_runtime_status", side_effect=lambda *a, **kw: next(recs)), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive("t", None, timeout=10, poll=0.01)
    assert out == {"ok": True, "username": "trixbot"}


def test_wait_bot_alive_token_ok_but_adapter_disconnected_times_out():
    from hermes_cli.setup_wizard import gateway_ctl as g
    rec = {"pid": 111, "platforms": {"telegram": {"state": "disconnected"}}}
    fake_now = [0.0]
    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "monotonic", side_effect=lambda: fake_now.__setitem__(0, fake_now[0] + 30) or fake_now[0]), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive("t", None, timeout=90)
    assert out["ok"] is False


def test_wait_bot_alive_fatal_state_returns_early_without_waiting_out_timeout():
    from hermes_cli.setup_wizard import gateway_ctl as g
    rec = {"pid": 111, "platforms": {"telegram": {"state": "fatal", "error_message": "invalid token"}}}
    with patch.object(g, "check_telegram_token", return_value={"ok": False, "error": "нет"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "sleep") as mock_sleep:
        out = g.wait_bot_alive("t", None, timeout=90)
    assert out["ok"] is False
    assert mock_sleep.call_count == 0
    assert g._MSG_FATAL_PREFIX in out["error"]


def test_wait_bot_alive_stale_status_is_not_success():
    from hermes_cli.setup_wizard import gateway_ctl as g
    rec = {"pid": 111, "platforms": {"telegram": {"state": "connected"}}}
    fake_now = [0.0]
    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=True), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "monotonic", side_effect=lambda: fake_now.__setitem__(0, fake_now[0] + 30) or fake_now[0]), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive("t", None, timeout=90)
    assert out["ok"] is False


def test_wait_bot_alive_dead_pid_is_not_success():
    from hermes_cli.setup_wizard import gateway_ctl as g
    rec = {"pid": 111, "platforms": {"telegram": {"state": "connected"}}}
    fake_now = [0.0]
    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=False), \
         patch.object(g.time, "monotonic", side_effect=lambda: fake_now.__setitem__(0, fake_now[0] + 30) or fake_now[0]), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive("t", None, timeout=90)
    assert out["ok"] is False


def test_wait_bot_alive_pre_pid_unchanged_keeps_waiting_until_timeout():
    """restart reported ok, but the runtime-status PID never turned over —
    the same-PID no-op restart_gateway's docstring warns about."""
    from hermes_cli.setup_wizard import gateway_ctl as g
    rec = {"pid": 555, "platforms": {"telegram": {"state": "connected"}}}
    fake_now = [0.0]
    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "monotonic", side_effect=lambda: fake_now.__setitem__(0, fake_now[0] + 30) or fake_now[0]), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive("t", None, timeout=90, pre_pid=555)
    assert out["ok"] is False


def test_wait_bot_alive_pre_pid_changed_succeeds():
    from hermes_cli.setup_wizard import gateway_ctl as g
    rec = {"pid": 999, "platforms": {"telegram": {"state": "connected"}}}
    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive("t", None, timeout=10, poll=0.01, pre_pid=555)
    assert out == {"ok": True, "username": "trixbot"}


def test_stop_and_disable_no_systemd_is_noop():
    from hermes_cli.setup_wizard import gateway_ctl as g

    def fake_run(cmd, **kw):
        raise FileNotFoundError("systemctl not found")

    with patch.object(g.subprocess, "run", side_effect=fake_run) as mock_run:
        g.stop_and_disable_wizard_service()  # must not raise
    assert mock_run.call_count == 2


def test_stop_and_disable_continues_to_disable_after_stop_failure():
    from hermes_cli.setup_wizard import gateway_ctl as g
    actions = []

    def fake_run(cmd, **kw):
        actions.append(cmd[2])
        if cmd[2] == "stop":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=15)
        return MagicMock(returncode=0)

    with patch.object(g.subprocess, "run", side_effect=fake_run):
        g.stop_and_disable_wizard_service()
    assert actions == ["stop", "disable"]


# --- Fix round 2: platforms.telegram surviving a process restart ---------


def test_wait_bot_alive_ignores_stale_fatal_platform_record_surviving_restart():
    """Old process's FATAL record + already-fresh (new) top-level pid +
    unchanged per-platform stamp must NOT short-circuit into an early
    refusal — it must keep waiting for the new adapter's own write."""
    from hermes_cli.setup_wizard import gateway_ctl as g
    stale_stamp = "2026-08-18T00:00:00+00:00"
    rec = {
        "pid": 999,  # top-level pid already looks like the NEW process
        "platforms": {
            "telegram": {
                "state": "fatal",
                "error_message": "Unauthorized",
                "updated_at": stale_stamp,
            }
        },
    }
    fake_now = [0.0]
    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "monotonic", side_effect=lambda: fake_now.__setitem__(0, fake_now[0] + 30) or fake_now[0]), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive(
            "t", None, timeout=90, pre_pid=111, pre_platform_stamp=stale_stamp,
        )
    assert out["ok"] is False
    assert out["error"] == g._MSG_NOT_CONFIRMED  # NOT the fatal-specific message


def test_wait_bot_alive_ignores_stale_connected_platform_record_surviving_restart():
    """Old process's CONNECTED record + fresh pid + unchanged stamp must
    NOT count as proof the new process is up."""
    from hermes_cli.setup_wizard import gateway_ctl as g
    stale_stamp = "2026-08-18T00:00:00+00:00"
    rec = {
        "pid": 999,
        "platforms": {"telegram": {"state": "connected", "updated_at": stale_stamp}},
    }
    fake_now = [0.0]
    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "monotonic", side_effect=lambda: fake_now.__setitem__(0, fake_now[0] + 30) or fake_now[0]), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive(
            "t", None, timeout=90, pre_pid=111, pre_platform_stamp=stale_stamp,
        )
    assert out["ok"] is False


def test_wait_bot_alive_succeeds_once_platform_stamp_advances_past_restart():
    from hermes_cli.setup_wizard import gateway_ctl as g
    rec = {
        "pid": 999,
        "platforms": {
            "telegram": {"state": "connected", "updated_at": "2026-08-19T00:00:00+00:00"}
        },
    }
    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive(
            "t", None, timeout=10, poll=0.01,
            pre_pid=111, pre_platform_stamp="2026-08-18T00:00:00+00:00",
        )
    assert out == {"ok": True, "username": "trixbot"}


def test_wait_bot_alive_fatal_with_fresh_platform_stamp_returns_early():
    from hermes_cli.setup_wizard import gateway_ctl as g
    rec = {
        "pid": 999,
        "platforms": {
            "telegram": {
                "state": "fatal",
                "error_message": "Unauthorized",
                "updated_at": "2026-08-19T00:00:00+00:00",
            }
        },
    }
    with patch.object(g, "check_telegram_token", return_value={"ok": False, "error": "нет"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "sleep") as mock_sleep:
        out = g.wait_bot_alive(
            "t", None, timeout=90,
            pre_pid=111, pre_platform_stamp="2026-08-18T00:00:00+00:00",
        )
    assert out["ok"] is False
    assert mock_sleep.call_count == 0
    assert g._MSG_FATAL_PREFIX in out["error"]


def test_restart_gateway_returns_pre_platform_stamp_snapshot():
    from hermes_cli.setup_wizard import gateway_ctl as g

    def fake_run(cmd, **kw):
        return MagicMock(returncode=0, stdout="", stderr="")

    rec = {"pid": 1, "platforms": {"telegram": {"state": "connected", "updated_at": "2026-08-18T00:00:00+00:00"}}}
    with patch.object(g.subprocess, "run", side_effect=fake_run), \
         patch.object(g, "read_runtime_status", return_value=rec):
        out = g.restart_gateway()
    assert out["pre_pid"] == 1
    assert out["pre_platform_stamp"] == "2026-08-18T00:00:00+00:00"


def test_restart_gateway_pre_platform_stamp_none_when_platforms_malformed():
    from hermes_cli.setup_wizard import gateway_ctl as g

    def fake_run(cmd, **kw):
        return MagicMock(returncode=0)

    with patch.object(g.subprocess, "run", side_effect=fake_run), \
         patch.object(g, "read_runtime_status", return_value={"pid": 1, "platforms": "oops"}):
        out = g.restart_gateway()
    assert out["pre_pid"] == 1
    assert out["pre_platform_stamp"] is None


# --- Fix round 2: timeout message carries the last token error -----------


def test_wait_bot_alive_timeout_message_includes_last_token_error():
    from hermes_cli.setup_wizard import gateway_ctl as g
    fake_now = [0.0]
    with patch.object(
             g, "check_telegram_token",
             return_value={"ok": False, "error": "Токен неверный — проверьте у @BotFather"},
         ), \
         patch.object(g, "read_runtime_status", return_value=None), \
         patch.object(g.time, "monotonic", side_effect=lambda: fake_now.__setitem__(0, fake_now[0] + 30) or fake_now[0]), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive("t", None, timeout=90)
    assert out["ok"] is False
    assert "Токен неверный" in out["error"]


def test_wait_bot_alive_timeout_message_omits_token_error_when_token_was_fine():
    """Token confirmed fine; only the adapter never confirmed -> plain message."""
    from hermes_cli.setup_wizard import gateway_ctl as g
    rec = {"pid": 111, "platforms": {"telegram": {"state": "disconnected"}}}
    fake_now = [0.0]
    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "monotonic", side_effect=lambda: fake_now.__setitem__(0, fake_now[0] + 30) or fake_now[0]), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive("t", None, timeout=90)
    assert out == {"ok": False, "error": g._MSG_NOT_CONFIRMED}


# --- Fix round 2: malformed gateway_state.json degrades, doesn't raise ---


def test_wait_bot_alive_handles_non_dict_platforms_gracefully():
    from hermes_cli.setup_wizard import gateway_ctl as g
    rec = {"pid": 1, "platforms": "not-a-dict"}
    fake_now = [0.0]
    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "monotonic", side_effect=lambda: fake_now.__setitem__(0, fake_now[0] + 30) or fake_now[0]), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive("t", None, timeout=90)  # must not raise
    assert out["ok"] is False


def test_wait_bot_alive_handles_non_dict_platform_entry_gracefully():
    from hermes_cli.setup_wizard import gateway_ctl as g
    rec = {"pid": 1, "platforms": {"telegram": "not-a-dict"}}
    fake_now = [0.0]
    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}), \
         patch.object(g, "read_runtime_status", return_value=rec), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "monotonic", side_effect=lambda: fake_now.__setitem__(0, fake_now[0] + 30) or fake_now[0]), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive("t", None, timeout=90)  # must not raise
    assert out["ok"] is False


# --- Fix round 2: no redundant getMe polling once confirmed ---------------


def test_wait_bot_alive_does_not_recheck_token_once_confirmed():
    from hermes_cli.setup_wizard import gateway_ctl as g
    call_count = {"n": 0}

    def fake_check(*a, **kw):
        call_count["n"] += 1
        return {"ok": True, "username": "trixbot"}

    recs = iter([
        {"pid": 1, "platforms": {"telegram": {"state": "connecting"}}},
        {"pid": 1, "platforms": {"telegram": {"state": "connecting"}}},
        {"pid": 1, "platforms": {"telegram": {"state": "connected"}}},
    ])
    with patch.object(g, "check_telegram_token", side_effect=fake_check), \
         patch.object(g, "read_runtime_status", side_effect=lambda *a, **kw: next(recs)), \
         patch.object(g, "runtime_status_is_stale", return_value=False), \
         patch.object(g, "runtime_status_pid_is_live", return_value=True), \
         patch.object(g.time, "sleep"):
        out = g.wait_bot_alive("t", None, timeout=10, poll=0.01)
    assert out == {"ok": True, "username": "trixbot"}
    assert call_count["n"] == 1


# --- Fix round 2 (rule): one E2E test over the real status chain ----------


def test_wait_bot_alive_e2e_real_status_chain_confirms_connection():
    """Writes an actual gateway_state.json via the REAL write_runtime_status()
    and lets the REAL read_runtime_status/runtime_status_is_stale/
    runtime_status_pid_is_live chain evaluate it end to end — only
    check_telegram_token is mocked. Exercises this module's real resolution
    chain against real imports and a real (isolated) HERMES_HOME, not a
    mock stand-in for gateway.status."""
    from hermes_cli.setup_wizard import gateway_ctl as g
    from gateway.status import write_runtime_status

    write_runtime_status(
        gateway_state="running",
        platform="telegram",
        platform_state="connected",
        error_code=None,
        error_message=None,
    )

    with patch.object(g, "check_telegram_token", return_value={"ok": True, "username": "trixbot"}):
        out = g.wait_bot_alive("t", None, timeout=10, poll=0.01)
    assert out == {"ok": True, "username": "trixbot"}
