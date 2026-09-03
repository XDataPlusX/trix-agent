"""Tests for hermes_cli.setup_wizard.cli — bootstrap/serve/open/status/close/
set-password/install-service subcommands and the systemd unit generator.

Spec 8 (``docs/product/specs/2026-08-25-trix-agent-wizard-permanent-access-
design.md``): the wizard stays up permanently behind a permanent login +
password (the ``primary`` slot). ``bootstrap`` no longer generates
credentials — it receives them from the caller. ``open_wizard()`` is split
into ``start_wizard_service()`` (no credential side effect) and
``issue_temporary_password()`` (emergency-only). ``close``/
``should_self_extinguish`` are keyed off ``disabled``, never ``completed``.
"""
from __future__ import annotations

import argparse
import shutil
import socket
from unittest.mock import MagicMock, patch

import pytest

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="no openssl")


def test_uvicorn_kwargs_carry_tls_and_quiet_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import bootstrap, cli

    crt, key = bootstrap.cert_paths()
    crt.parent.mkdir(parents=True)
    crt.write_text("x")
    key.write_text("y")
    kw = cli.build_uvicorn_kwargs(8443)
    assert kw["ssl_certfile"] == str(crt) and kw["ssl_keyfile"] == str(key)
    assert kw["access_log"] is False and kw["log_level"] == "warning"
    assert kw["port"] == 8443 and kw["host"] == "0.0.0.0"


def test_uvicorn_never_trusts_forwarded_headers(tmp_path, monkeypatch):
    """uvicorn must not rewrite client.host from X-Forwarded-For.

    Spec 8 §8.1 says the wizard reads the peer address and nothing else,
    and ``app._client_ip()`` honours that. uvicorn does not: it defaults to
    ``proxy_headers=True`` with ``forwarded_allow_ips="127.0.0.1"``, so its
    own middleware rewrites ``client.host`` from the header for any
    loopback connection — before our code ever runs. A local process (the
    agent's own terminal tool, say) could then walk past its own per-IP
    lockout and fill ``failures_by_ip`` with forged keys.

    This asserts the kwargs rather than the app's behaviour on purpose:
    TestClient drives the ASGI app directly, so uvicorn's middleware is not
    in the stack there and an app-level test cannot see this at all.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import bootstrap, cli

    crt, key = bootstrap.cert_paths()
    crt.parent.mkdir(parents=True)
    crt.write_text("x")
    key.write_text("y")
    assert cli.build_uvicorn_kwargs(8443).get("proxy_headers") is False


def test_serve_refuses_without_cert(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    with pytest.raises(SystemExit):
        cli.build_uvicorn_kwargs(8443)


def test_uvicorn_kwargs_prefer_configured_tls_over_self_signed(tmp_path, monkeypatch):
    """Docs-враньё 1 fix: setup_wizard.tls_cert/tls_key in config.yaml —
    documented in deployment-requirements.md's "TLS-компромисс" section —
    must actually override the self-signed cert once both are set and the
    files exist."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import bootstrap, cli

    # Self-signed cert also present — the configured pair must still win.
    crt, key = bootstrap.cert_paths()
    crt.parent.mkdir(parents=True)
    crt.write_text("self-signed-cert")
    key.write_text("self-signed-key")

    real_crt = tmp_path / "real.crt"
    real_key = tmp_path / "real.key"
    real_crt.write_text("real-cert")
    real_key.write_text("real-key")

    from hermes_cli.config import save_config

    save_config({"setup_wizard": {"tls_cert": str(real_crt), "tls_key": str(real_key)}})

    kw = cli.build_uvicorn_kwargs(8443)
    assert kw["ssl_certfile"] == str(real_crt)
    assert kw["ssl_keyfile"] == str(real_key)


def test_uvicorn_kwargs_configured_tls_missing_files_fails_loud(tmp_path, monkeypatch, capsys):
    """A configured tls_cert/tls_key pair whose files don't exist must
    exit loudly, not silently fall back to the self-signed cert — an
    explicit security-relevant config value is never quietly overridden."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import bootstrap, cli

    crt, key = bootstrap.cert_paths()
    crt.parent.mkdir(parents=True)
    crt.write_text("self-signed-cert")
    key.write_text("self-signed-key")

    from hermes_cli.config import save_config

    save_config(
        {
            "setup_wizard": {
                "tls_cert": str(tmp_path / "missing.crt"),
                "tls_key": str(tmp_path / "missing.key"),
            }
        }
    )

    with pytest.raises(SystemExit):
        cli.build_uvicorn_kwargs(8443)
    assert "config.yaml" in capsys.readouterr().err


def test_uvicorn_kwargs_no_configured_tls_falls_back_to_self_signed(tmp_path, monkeypatch):
    """No setup_wizard.tls_cert/tls_key at all — unchanged behavior, the
    self-signed cert wins with no warning."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import bootstrap, cli

    crt, key = bootstrap.cert_paths()
    crt.parent.mkdir(parents=True)
    crt.write_text("self-signed-cert")
    key.write_text("self-signed-key")

    kw = cli.build_uvicorn_kwargs(8443)
    assert kw["ssl_certfile"] == str(crt)
    assert kw["ssl_keyfile"] == str(key)


def test_unit_file_has_no_foreign_brand():
    from hermes_cli.setup_wizard.cli import unit_file_text

    text = unit_file_text()
    assert "trix-setup" not in text  # имя файла, не содержимого
    assert "setup-wizard serve" in text
    assert "hermes" not in text.split("ExecStart")[0].lower()  # описание юнита без чужого бренда


def test_unit_file_has_restart_on_failure():
    from hermes_cli.setup_wizard.cli import unit_file_text

    text = unit_file_text()
    assert "Restart=on-failure" in text


# ---------------------------------------------------------------------------
# bootstrap — receives login/password from a caller, never generates or
# prints them (§9.1/§9.5, §14.9)
# ---------------------------------------------------------------------------


def _write_password_file(tmp_path, password: str):
    pw_file = tmp_path / "primary.pw"
    pw_file.write_text(password, encoding="utf-8")
    return pw_file


def test_bootstrap_subcommand_prints_only_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if shutil.which("openssl") is None:
        pytest.skip("no openssl")
    from hermes_cli.setup_wizard.cli import cmd_setup_wizard
    from hermes_cli.setup_wizard.state import WizardState

    pw_file = _write_password_file(tmp_path, "SentinelPassword123")
    args = argparse.Namespace(
        wizard_command="bootstrap",
        ip="203.0.113.7",
        login="trix-abc123",
        password_file=str(pw_file),
    )
    cmd_setup_wizard(args)
    out = capsys.readouterr().out
    assert "https://203.0.113.7:8443" in out
    assert "SentinelPassword123" not in out
    assert WizardState.load().verify("trix-abc123", "SentinelPassword123", ip="203.0.113.1") is True


def test_bootstrap_never_prints_password(tmp_path, monkeypatch, capsys):
    """Sentinel approach: run_bootstrap is mocked out entirely (no openssl
    needed), so we can assert stdout never contains the plaintext password
    regardless of what a real hash looks like (§14.9)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    pw_file = _write_password_file(tmp_path, "SUPER-SECRET-SENTINEL")
    with patch.object(cli.bootstrap, "run_bootstrap") as run_bootstrap:
        args = argparse.Namespace(
            wizard_command="bootstrap",
            ip="203.0.113.7",
            login="trix-abc123",
            password_file=str(pw_file),
        )
        cli.cmd_setup_wizard(args)

    run_bootstrap.assert_called_once_with("203.0.113.7", "trix-abc123", "SUPER-SECRET-SENTINEL")
    out = capsys.readouterr().out
    assert "SUPER-SECRET-SENTINEL" not in out


def test_bootstrap_deletes_password_file_after_reading(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    pw_file = _write_password_file(tmp_path, "pw-to-delete")
    with patch.object(cli.bootstrap, "run_bootstrap"):
        args = argparse.Namespace(
            wizard_command="bootstrap",
            ip="203.0.113.7",
            login="trix-abc123",
            password_file=str(pw_file),
        )
        cli.cmd_setup_wizard(args)

    assert not pw_file.exists()


def test_bootstrap_keeps_password_file_when_run_bootstrap_fails(tmp_path, monkeypatch):
    """Review finding 4: the password file must not be deleted before
    ``run_bootstrap`` has actually succeeded — otherwise a cert-generation
    failure (e.g. an empty ``PUBLIC_IP``) destroys the only remaining copy
    of the password with no `primary` slot written to recover it from."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    pw_file = _write_password_file(tmp_path, "pw-should-survive")
    with patch.object(cli.bootstrap, "run_bootstrap", side_effect=RuntimeError("cert boom")):
        args = argparse.Namespace(
            wizard_command="bootstrap",
            ip="203.0.113.7",
            login="trix-abc123",
            password_file=str(pw_file),
        )
        with pytest.raises(RuntimeError):
            cli.cmd_setup_wizard(args)

    assert pw_file.exists()
    assert pw_file.read_text(encoding="utf-8") == "pw-should-survive"


def test_bootstrap_has_no_port_flag(tmp_path, monkeypatch, capsys):
    """bootstrap's printed URL must always match what `serve`/the installed
    unit actually binds (DEFAULT_PORT) — a --port override would let an
    operator print an address nothing listens on, so it must not exist."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    pw_file = _write_password_file(tmp_path, "pw")
    with patch.object(cli.bootstrap, "run_bootstrap"):
        # Namespace may still carry a stray `port` attribute (argparse
        # callers can pass whatever), but the handler must ignore it.
        args = argparse.Namespace(
            wizard_command="bootstrap",
            ip="203.0.113.7",
            login="trix-abc123",
            password_file=str(pw_file),
            port=9999,
        )
        cli.cmd_setup_wizard(args)

    out = capsys.readouterr().out
    assert "https://203.0.113.7:8443" in out
    assert "9999" not in out


# ---------------------------------------------------------------------------
# _ip_from_cert — authoritative IP from the TLS cert's Subject CN
# ---------------------------------------------------------------------------


def test_ip_from_cert_none_when_no_cert(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    assert cli._ip_from_cert() is None


def test_ip_from_cert_parses_subject_cn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import bootstrap, cli

    crt, key = bootstrap.cert_paths()
    crt.parent.mkdir(parents=True)
    crt.write_text("x")
    key.write_text("y")

    fake_result = MagicMock(returncode=0, stdout="subject=CN = 203.0.113.42\n")
    with patch.object(cli.subprocess, "run", return_value=fake_result):
        assert cli._ip_from_cert() == "203.0.113.42"


def test_ip_from_cert_rejects_non_ip_cn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import bootstrap, cli

    crt, key = bootstrap.cert_paths()
    crt.parent.mkdir(parents=True)
    crt.write_text("x")
    key.write_text("y")

    fake_result = MagicMock(returncode=0, stdout="subject=CN = not-an-ip\n")
    with patch.object(cli.subprocess, "run", return_value=fake_result):
        assert cli._ip_from_cert() is None


def test_ip_from_cert_none_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import bootstrap, cli

    crt, key = bootstrap.cert_paths()
    crt.parent.mkdir(parents=True)
    crt.write_text("x")
    key.write_text("y")

    fake_result = MagicMock(returncode=1, stdout="")
    with patch.object(cli.subprocess, "run", return_value=fake_result):
        assert cli._ip_from_cert() is None


@needs_openssl
def test_ip_from_cert_reads_real_generated_cert(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import bootstrap, cli

    bootstrap.generate_self_signed_cert("203.0.113.55")
    assert cli._ip_from_cert() == "203.0.113.55"


# ---------------------------------------------------------------------------
# _probe_port_open
# ---------------------------------------------------------------------------


def test_probe_port_open_true_when_listening(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert cli._probe_port_open(port, timeout=1.0) is True
    finally:
        srv.close()


def test_probe_port_open_false_when_nothing_listening(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # freed immediately — nothing listens here now
    assert cli._probe_port_open(port, timeout=1.0) is False


# ---------------------------------------------------------------------------
# start_wizard_service — no credential side effect (used by /setup, §6)
# ---------------------------------------------------------------------------


def test_start_wizard_service_returns_url_and_reachable_without_touching_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    def _boom_if_loaded(*_a, **_k):
        raise AssertionError("start_wizard_service must not touch WizardState")

    with patch.object(cli, "_start_service") as start_service, \
         patch.object(cli, "_ip_from_cert", return_value=None), \
         patch.object(cli, "_detect_ip", return_value="203.0.113.7"), \
         patch.object(cli, "_probe_port_open", return_value=True), \
         patch.object(cli.WizardState, "load", staticmethod(_boom_if_loaded)):
        url, reachable = cli.start_wizard_service()

    start_service.assert_called_once()
    assert url == "https://203.0.113.7:8443"
    assert reachable is True


def test_start_wizard_service_prefers_cert_cn_over_nic_ip(tmp_path, monkeypatch):
    """A NAT'd VM's _detect_ip() returns the private NIC address (e.g.
    10.x.x.x on AWS/GCP) — the cert's Subject CN (the public IP the
    operator chose at bootstrap time) is authoritative and must win."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    with patch.object(cli, "_start_service"), \
         patch.object(cli, "_ip_from_cert", return_value="198.51.100.9"), \
         patch.object(cli, "_detect_ip", return_value="10.0.0.5"), \
         patch.object(cli, "_probe_port_open", return_value=True):
        url, reachable = cli.start_wizard_service()

    assert url == "https://198.51.100.9:8443"
    assert reachable is True


def test_start_wizard_service_falls_back_to_placeholder_when_ip_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    with patch.object(cli, "_start_service"), \
         patch.object(cli, "_ip_from_cert", return_value=None), \
         patch.object(cli, "_detect_ip", return_value=None), \
         patch.object(cli, "_probe_port_open", return_value=True):
        url, reachable = cli.start_wizard_service()

    assert reachable is True
    assert "8443" in url
    assert "не удалось определить" in url.lower() or "IP-адрес-машины" in url


def test_start_wizard_service_brackets_ipv6(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    with patch.object(cli, "_start_service"), \
         patch.object(cli, "_ip_from_cert", return_value=None), \
         patch.object(cli, "_detect_ip", return_value="2001:db8::7"), \
         patch.object(cli, "_probe_port_open", return_value=True):
        url, reachable = cli.start_wizard_service()

    assert url == "https://[2001:db8::7]:8443"
    assert reachable is True


def test_start_wizard_service_reports_unreachable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    with patch.object(cli, "_start_service"), \
         patch.object(cli, "_ip_from_cert", return_value=None), \
         patch.object(cli, "_detect_ip", return_value="203.0.113.7"), \
         patch.object(cli, "_probe_port_open", return_value=False):
        url, reachable = cli.start_wizard_service()

    assert url == "https://203.0.113.7:8443"
    assert reachable is False


# ---------------------------------------------------------------------------
# issue_temporary_password / `open` — emergency path, never touches primary
# ---------------------------------------------------------------------------


def test_issue_temporary_password_stores_in_temporary_slot_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    WizardState.load().issue_primary("trix-abc123", "PrimaryPassword1")

    password = cli.issue_temporary_password(3600)

    assert len(password) == 30
    assert all(c.isalnum() and c.isascii() for c in password)

    state = WizardState.load()
    # Primary is untouched.
    assert state.verify("trix-abc123", "PrimaryPassword1", ip="203.0.113.1") is True
    # Temporary works too, with any login.
    assert state.verify("whatever", password, ip="203.0.113.1") is True


def test_cmd_open_starts_service_and_issues_temporary_password(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    with patch.object(cli, "start_wizard_service", return_value=("https://203.0.113.7:8443", True)):
        args = argparse.Namespace(wizard_command="open", ttl_minutes=60)
        cli.cmd_setup_wizard(args)

    out = capsys.readouterr().out
    assert "https://203.0.113.7:8443" in out
    # A 30-char alnum password line is present in the output.
    lines = [ln for ln in out.splitlines() if len(ln) == 30 and ln.isalnum()]
    assert len(lines) == 1
    password = lines[0]
    assert WizardState.load().verify("anything", password, ip="203.0.113.1") is True


def test_cmd_open_prints_warning_when_port_not_yet_listening(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    with patch.object(cli, "start_wizard_service", return_value=("https://203.0.113.7:8443", False)):
        args = argparse.Namespace(wizard_command="open", ttl_minutes=60)
        cli.cmd_setup_wizard(args)

    out = capsys.readouterr().out
    assert "предупреждение" in out.lower()
    assert "https://203.0.113.7:8443" in out


def test_start_service_uses_systemctl_when_available(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0)

    popen_calls = []

    def fake_popen(cmd, **kw):
        popen_calls.append(cmd)
        return MagicMock()

    with patch.object(cli.subprocess, "run", side_effect=fake_run), \
         patch.object(cli.subprocess, "Popen", side_effect=fake_popen):
        cli._start_service()

    assert any("systemctl" in c[0] for c in calls)
    assert any("start" in c for c in calls)
    assert popen_calls == []


def test_start_service_falls_back_to_detached_serve_when_systemctl_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    def fake_run(cmd, **kw):
        return MagicMock(returncode=1)

    popen_calls = []

    def fake_popen(cmd, **kw):
        popen_calls.append(cmd)
        return MagicMock()

    with patch.object(cli.subprocess, "run", side_effect=fake_run), \
         patch.object(cli.subprocess, "Popen", side_effect=fake_popen):
        cli._start_service()

    assert len(popen_calls) == 1
    assert "setup-wizard" in popen_calls[0] and "serve" in popen_calls[0]


def test_start_service_falls_back_when_systemctl_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    def fake_run(cmd, **kw):
        raise FileNotFoundError("systemctl not found")

    popen_calls = []

    def fake_popen(cmd, **kw):
        popen_calls.append(cmd)
        return MagicMock()

    with patch.object(cli.subprocess, "run", side_effect=fake_run), \
         patch.object(cli.subprocess, "Popen", side_effect=fake_popen):
        cli._start_service()

    assert len(popen_calls) == 1


# ---------------------------------------------------------------------------
# self-extinguish gate for `serve` — keyed off `disabled`, never `completed`
# (spec §4.3; this is the fix for the closed-loop-access bug)
# ---------------------------------------------------------------------------


def test_should_self_extinguish_true_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    state = WizardState.load()
    state.issue_primary("trix-abc123", "SomePassword1")
    state.set_disabled(True)

    assert cli.should_self_extinguish() is True


def test_should_self_extinguish_false_when_open_and_not_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    WizardState.load().issue_primary("trix-abc123", "SomePassword1")
    assert cli.should_self_extinguish() is False


def test_should_self_extinguish_false_when_never_issued(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli

    assert cli.should_self_extinguish() is False


def test_should_self_extinguish_false_after_completed_first_run(tmp_path, monkeypatch):
    """§4.3/§14.1: completing the first-run form must NOT close the wizard
    — only an explicit `close` (the `disabled` flag) does. This is the
    exact bug the permanent-access design fixes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    state = WizardState.load()
    state.issue_primary("trix-abc123", "SomePassword1")
    state.mark_completed()

    assert cli.should_self_extinguish() is False


def test_serve_self_extinguishes_without_starting_uvicorn_when_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    state = WizardState.load()
    state.issue_primary("trix-abc123", "SomePassword1")
    state.set_disabled(True)

    args = argparse.Namespace(wizard_command="serve", port=8443)
    with patch.object(cli, "_disable_service") as disable, \
         patch("uvicorn.run") as run:
        cli.cmd_setup_wizard(args)

    disable.assert_called_once()
    run.assert_not_called()


def test_serve_still_binds_after_completed_form_and_restart(tmp_path, monkeypatch):
    """§14.13 — the invariant that catches "panel switched itself off":
    after `mark_completed()` (a successful wizard submit) and a `serve`
    restart, the wizard must still open the port instead of self-disabling.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    state = WizardState.load()
    state.issue_primary("trix-abc123", "SomePassword1")
    state.mark_completed()

    # Simulate the unit restarting (a brand-new `serve` process/invocation):
    # should_self_extinguish() re-reads state fresh, same as a real restart.
    assert cli.should_self_extinguish() is False

    args = argparse.Namespace(wizard_command="serve", port=8443)
    with patch.object(cli, "build_uvicorn_kwargs", return_value={"host": "0.0.0.0"}), \
         patch("hermes_cli.setup_wizard.app.create_app", return_value="fake-app"), \
         patch("uvicorn.run") as run, \
         patch.object(cli, "_disable_service") as disable:
        cli.cmd_setup_wizard(args)

    run.assert_called_once()
    disable.assert_not_called()


# ---------------------------------------------------------------------------
# status / close / set-password
# ---------------------------------------------------------------------------


def test_status_reports_open_before_completion(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    WizardState.load().issue_primary("trix-abc123", "SomePassword1")
    args = argparse.Namespace(wizard_command="status")
    cli.cmd_setup_wizard(args)
    out = capsys.readouterr().out
    assert "открыт" in out.lower()


def test_status_reports_completed_and_still_open(tmp_path, monkeypatch, capsys):
    """§4.3: a completed wizard is reported as open, not closed."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    state = WizardState.load()
    state.issue_primary("trix-abc123", "SomePassword1")
    state.mark_completed()
    args = argparse.Namespace(wizard_command="status")
    cli.cmd_setup_wizard(args)
    out = capsys.readouterr().out
    assert "заверш" in out.lower()
    assert "открыт" in out.lower()
    assert "закрыт" not in out.lower()


def test_status_reports_closed_when_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    state = WizardState.load()
    state.issue_primary("trix-abc123", "SomePassword1")
    state.set_disabled(True)
    args = argparse.Namespace(wizard_command="status")
    cli.cmd_setup_wizard(args)
    out = capsys.readouterr().out
    assert "закрыт" in out.lower()


def test_status_does_not_claim_closed_when_port_is_actually_listening(
    tmp_path, monkeypatch, capsys
):
    """Review finding 7: `disabled` defaults to False and never getting a
    `primary` slot (e.g. a `bootstrap` that generated the cert but crashed
    before writing the slot) used to be reported as "закрыт", even though
    `serve` would actually bind and answer 401 to every request. An
    operator debugging that failure needs an honest status, not one that
    claims nothing is listening when something is."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    assert WizardState.load().has_primary() is False
    assert WizardState.load().is_disabled() is False

    with patch.object(cli, "_probe_port_open", return_value=True):
        args = argparse.Namespace(wizard_command="status")
        cli.cmd_setup_wizard(args)

    out = capsys.readouterr().out.lower()
    assert "закрыт" not in out
    assert "слуша" in out or "401" in out


def test_close_disables_and_stops_service_without_touching_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    WizardState.load().issue_primary("trix-abc123", "SomePassword1")
    args = argparse.Namespace(wizard_command="close")
    with patch("hermes_cli.setup_wizard.gateway_ctl.stop_and_disable_wizard_service") as stop:
        cli.cmd_setup_wizard(args)
    stop.assert_called_once()
    state = WizardState.load()
    assert state.is_disabled() is True
    assert state.is_open() is False
    # `close` must not fabricate a "completed" first run.
    assert state.is_completed() is False


def test_set_password_rotates_primary_and_prints_only_with_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    WizardState.load().issue_primary("trix-old000000", "OldPassword1")

    args = argparse.Namespace(wizard_command="set-password", login=None, print_credentials=False)
    cli.cmd_setup_wizard(args)
    out = capsys.readouterr().out

    state = WizardState.load()
    # The old primary credentials no longer verify.
    assert state.verify("trix-old000000", "OldPassword1", ip="203.0.113.1") is False
    assert state.has_primary() is True
    # Nothing that looks like the new plaintext password is in stdout
    # without --print.
    assert "Учётные данные обновлены" in out or "--print" in out


def test_set_password_prints_credentials_with_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    args = argparse.Namespace(
        wizard_command="set-password", login="trix-custom001", print_credentials=True
    )
    cli.cmd_setup_wizard(args)
    out = capsys.readouterr().out

    assert "trix-custom001" in out
    lines = [ln for ln in out.splitlines() if len(ln) == 30 and ln.isalnum()]
    assert len(lines) == 1
    password = lines[0]
    assert WizardState.load().verify("trix-custom001", password, ip="203.0.113.1") is True


def test_set_password_rejects_login_with_colon(tmp_path, monkeypatch, capsys):
    """Review finding 5: HTTP Basic (§8.3) encodes credentials as
    "login:password" and the client splits on the first ":" — a login
    containing one is unrepresentable and would silently lock the client
    out. `set-password` must reject it instead of writing it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    WizardState.load().issue_primary("trix-old000000", "OldPassword1")

    args = argparse.Namespace(
        wizard_command="set-password", login="trix:evil", print_credentials=True
    )
    with pytest.raises(SystemExit):
        cli.cmd_setup_wizard(args)

    out, err = capsys.readouterr()
    assert "trix:evil" not in out
    assert ":" in err  # explains the problem, doesn't just fail silently
    # The old primary slot must be untouched — a rejected `set-password`
    # must not have written anything.
    assert WizardState.load().verify("trix-old000000", "OldPassword1", ip="203.0.113.1") is True


def test_set_password_generates_login_when_not_given(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    args = argparse.Namespace(wizard_command="set-password", login=None, print_credentials=True)
    cli.cmd_setup_wizard(args)
    out = capsys.readouterr().out.splitlines()

    login = out[0]
    assert login.startswith("trix-")
    password = out[1]
    assert WizardState.load().verify(login, password, ip="203.0.113.1") is True


# ---------------------------------------------------------------------------
# _wizard_service_name — reused from gateway_ctl, not duplicated
# ---------------------------------------------------------------------------


def test_service_name_matches_gateway_ctl_constant(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import cli, gateway_ctl

    assert cli._wizard_service_name() == gateway_ctl._WIZARD_SERVICE_NAME


# ---------------------------------------------------------------------------
# install-service — writes unit, preflights user systemd + linger, enables
# ---------------------------------------------------------------------------


def test_install_service_writes_unit_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    from hermes_cli.setup_wizard import cli

    args = argparse.Namespace(wizard_command="install-service")
    with patch.object(cli.Path, "home", return_value=fake_home), \
         patch("hermes_cli.gateway._preflight_user_systemd") as preflight, \
         patch.object(cli.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
        cli.cmd_setup_wizard(args)

    unit_path = fake_home / ".config" / "systemd" / "user" / "trix-setup.service"
    assert unit_path.is_file()
    assert "setup-wizard serve" in unit_path.read_text()
    preflight.assert_called_once_with(auto_enable_linger=True)
    assert any("enable" in c.args[0] for c in run.call_args_list)


def test_install_service_starts_the_unit_not_just_enables_it(tmp_path, monkeypatch):
    """Review finding 1 (blocker): §9.1.3 requires cloud-init to "включить И
    запустить" trix-setup.service — enabling alone only survives a future
    reboot, it does not make the wizard reachable on a freshly provisioned
    VM. `install-service` must actually start the unit, not just enable it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    from hermes_cli.setup_wizard import cli

    args = argparse.Namespace(wizard_command="install-service")
    with patch.object(cli.Path, "home", return_value=fake_home), \
         patch("hermes_cli.gateway._preflight_user_systemd"), \
         patch.object(cli.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
        cli.cmd_setup_wizard(args)

    started = [
        c
        for c in run.call_args_list
        if "systemctl" in c.args[0] and "start" in c.args[0] and cli._wizard_service_name() in c.args[0]
    ]
    assert started, f"no systemctl ... start call among: {[c.args[0] for c in run.call_args_list]}"


def test_install_service_reopens_a_previously_closed_wizard(tmp_path, monkeypatch):
    """Review finding 2 (blocker): spec §9.3 says `close` "включается
    обратно `install-service`" — but nothing ever called `set_disabled(False)`
    anywhere, so the panel stayed closed forever even after re-running
    install-service."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    from hermes_cli.setup_wizard import cli
    from hermes_cli.setup_wizard.state import WizardState

    state = WizardState.load()
    state.issue_primary("trix-abc123", "SomePassword1")
    state.set_disabled(True)
    assert WizardState.load().is_disabled() is True

    args = argparse.Namespace(wizard_command="install-service")
    with patch.object(cli.Path, "home", return_value=fake_home), \
         patch("hermes_cli.gateway._preflight_user_systemd"), \
         patch.object(cli.subprocess, "run", return_value=MagicMock(returncode=0)):
        cli.cmd_setup_wizard(args)

    assert WizardState.load().is_disabled() is False


def test_install_service_handles_missing_systemd_gracefully(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    from hermes_cli.setup_wizard import cli

    args = argparse.Namespace(wizard_command="install-service")
    with patch.object(cli.Path, "home", return_value=fake_home), \
         patch("hermes_cli.gateway._preflight_user_systemd", side_effect=RuntimeError("no systemd")):
        cli.cmd_setup_wizard(args)  # must not raise / must not print a traceback

    unit_path = fake_home / ".config" / "systemd" / "user" / "trix-setup.service"
    assert unit_path.is_file()
    out = capsys.readouterr().out
    assert "systemd" in out.lower()


def test_install_service_reports_user_systemd_unavailable_in_russian(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    from hermes_cli.gateway import UserSystemdUnavailableError
    from hermes_cli.setup_wizard import cli

    args = argparse.Namespace(wizard_command="install-service")
    with patch.object(cli.Path, "home", return_value=fake_home), \
         patch(
             "hermes_cli.gateway._preflight_user_systemd",
             side_effect=UserSystemdUnavailableError("no D-Bus session"),
         ):
        cli.cmd_setup_wizard(args)

    unit_path = fake_home / ".config" / "systemd" / "user" / "trix-setup.service"
    assert unit_path.is_file()  # unit is still written even if enable can't happen
    out = capsys.readouterr().out
    assert "не настроен" in out.lower() or "недоступен" in out.lower()


# ---------------------------------------------------------------------------
# cmd_setup_wizard dispatch — unknown / missing subcommand
# ---------------------------------------------------------------------------


def test_unknown_subcommand_exits_and_lists_choices(capsys):
    from hermes_cli.setup_wizard import cli

    args = argparse.Namespace(wizard_command=None)
    with pytest.raises(SystemExit):
        cli.cmd_setup_wizard(args)
    out = capsys.readouterr().out
    for name in ("bootstrap", "serve", "open", "status", "close", "set-password", "install-service"):
        assert name in out
