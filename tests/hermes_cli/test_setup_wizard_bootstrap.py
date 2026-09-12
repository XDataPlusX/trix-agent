"""Tests for hermes_cli.setup_wizard.bootstrap (spec 8, §9.1/§9.5, §14.9).

``run_bootstrap`` no longer generates the login/password itself — it
receives both already-generated (cloud-init's job) and only hashes them
into the ``primary`` slot + generates the self-signed cert.
"""
import shutil
import subprocess

import pytest

needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl binary not available"
)


@needs_openssl
def test_cert_generated_with_ip_san(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.bootstrap import generate_self_signed_cert

    crt, key = generate_self_signed_cert("203.0.113.7")
    assert crt.is_file() and key.is_file()
    assert oct(key.stat().st_mode)[-3:] == "600"
    assert oct(crt.parent.stat().st_mode)[-3:] == "700"
    text = subprocess.run(
        ["openssl", "x509", "-in", str(crt), "-noout", "-text"],
        capture_output=True,
        text=True,
    ).stdout
    assert "203.0.113.7" in text


@needs_openssl
def test_bootstrap_writes_primary_slot_that_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.bootstrap import run_bootstrap
    from hermes_cli.setup_wizard.state import WizardState

    run_bootstrap("203.0.113.7", "trix-abc123", "SomeGeneratedPassword123")
    state = WizardState.load()
    assert state.verify("trix-abc123", "SomeGeneratedPassword123", ip="203.0.113.1") is True
    assert state.has_primary() is True
    assert state.primary_login() == "trix-abc123"


@needs_openssl
def test_bootstrap_rejects_wrong_password(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.bootstrap import run_bootstrap
    from hermes_cli.setup_wizard.state import WizardState

    run_bootstrap("203.0.113.7", "trix-abc123", "CorrectPassword1")
    assert WizardState.load().verify("trix-abc123", "WrongPassword", ip="203.0.113.1") is False


@needs_openssl
def test_bootstrap_cert_only_created_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard.bootstrap import cert_paths, run_bootstrap
    from hermes_cli.setup_wizard.state import WizardState

    run_bootstrap("203.0.113.7", "trix-abc123", "FirstPassword1")
    crt, _key = cert_paths()
    cert_bytes = crt.read_bytes()

    # Re-running bootstrap (e.g. re-run after a crash) must not regenerate
    # the cert, but must still overwrite the primary slot with the new
    # credentials the caller passed this time.
    run_bootstrap("203.0.113.7", "trix-def456", "SecondPassword2")

    assert crt.read_bytes() == cert_bytes
    state = WizardState.load()
    assert state.verify("trix-abc123", "FirstPassword1", ip="203.0.113.1") is False
    assert state.verify("trix-def456", "SecondPassword2", ip="203.0.113.1") is True


def test_bootstrap_never_writes_plaintext_password_to_state_file(tmp_path, monkeypatch):
    """§14.9: only a hash lands in state.json — the plaintext never does."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if shutil.which("openssl") is None:
        pytest.skip("no openssl")
    from hermes_cli.setup_wizard.bootstrap import run_bootstrap
    from hermes_cli.setup_wizard.state import state_path

    plaintext = "VeryDistinctivePlaintext123"
    run_bootstrap("203.0.113.7", "trix-abc123", plaintext)
    on_disk = state_path().read_text(encoding="utf-8")
    assert plaintext not in on_disk


def test_missing_openssl_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import bootstrap

    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError):
        bootstrap.generate_self_signed_cert("203.0.113.7")


@pytest.mark.parametrize("bad_ip", ["not-an-ip", "1.2.3.4/CN=evil", "1.2.3.4,DNS:evil"])
def test_invalid_ip_is_rejected_without_calling_openssl(tmp_path, monkeypatch, bad_ip):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import bootstrap

    called = False

    def _fake_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("openssl must not be invoked for an invalid ip")

    monkeypatch.setattr(bootstrap.subprocess, "run", _fake_run)
    with pytest.raises(RuntimeError, match="не является IP-адресом"):
        bootstrap.generate_self_signed_cert(bad_ip)
    assert called is False


def test_openssl_failure_surfaces_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.setup_wizard import bootstrap

    def _fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="bad ip")

    monkeypatch.setattr(bootstrap.subprocess, "run", _fake_run)
    with pytest.raises(RuntimeError, match="bad ip"):
        bootstrap.generate_self_signed_cert("203.0.113.7")
