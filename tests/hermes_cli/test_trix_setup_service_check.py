"""Behavioral contract tests for ``hermes_cli.trix_setup_service_check``.

The setup wizard's own systemd unit (``trix-setup.service``) is the
client's only way back into their machine once it's provisioned — no
standing SSH, no console. These tests pin the contract, not the current
wording:

- the check must stay completely silent where it cannot apply at all
  (not Linux, an s6 container, no unit installed at all) — same posture
  as the sibling gateway-linger check it's modeled on;
- once the unit exists, "enabled and active" is the only silent-success
  state — anything else must surface exactly one issue;
- what lands in ``issues`` (a future client-facing verdict surface) must
  be usable by someone with no console: short, non-empty, and it must not
  tell them to run a command they cannot type.
"""

from __future__ import annotations

import pytest

from hermes_cli import trix_setup_service_check as tsc


def _patch_environment(monkeypatch, *, is_linux=True, service_manager="systemd"):
    monkeypatch.setattr("hermes_cli.gateway.is_linux", lambda: is_linux)
    monkeypatch.setattr("hermes_cli.service_manager.detect_service_manager", lambda: service_manager)


def _patch_unit_exists(monkeypatch, tmp_path, exists: bool):
    unit_path = tmp_path / "trix-setup.service"
    if exists:
        unit_path.write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(tsc, "_unit_path", lambda: unit_path)
    return unit_path


def _fail_if_called(*args, **kwargs):
    raise AssertionError("systemctl should not have been consulted here")


class TestSilentWhenNotApplicable:
    def test_silent_on_non_linux(self, monkeypatch, tmp_path):
        _patch_environment(monkeypatch, is_linux=False)
        _patch_unit_exists(monkeypatch, tmp_path, exists=True)
        monkeypatch.setattr(tsc, "_systemctl_query", _fail_if_called)

        issues = []
        tsc.check_trix_setup_service(issues)

        assert issues == []

    def test_silent_under_s6(self, monkeypatch, tmp_path):
        _patch_environment(monkeypatch, is_linux=True, service_manager="s6")
        _patch_unit_exists(monkeypatch, tmp_path, exists=True)
        monkeypatch.setattr(tsc, "_systemctl_query", _fail_if_called)

        issues = []
        tsc.check_trix_setup_service(issues)

        assert issues == []

    def test_silent_when_unit_not_installed(self, monkeypatch, tmp_path):
        _patch_environment(monkeypatch)
        _patch_unit_exists(monkeypatch, tmp_path, exists=False)
        monkeypatch.setattr(tsc, "_systemctl_query", _fail_if_called)

        issues = []
        tsc.check_trix_setup_service(issues)

        assert issues == []


class TestUnitPresentStates:
    def _run(self, monkeypatch, tmp_path, *, enabled: str | None, active: str | None):
        _patch_environment(monkeypatch)
        _patch_unit_exists(monkeypatch, tmp_path, exists=True)

        def fake_query(verb):
            if verb == "is-enabled":
                return enabled
            if verb == "is-active":
                return active
            raise AssertionError(f"unexpected systemctl verb: {verb}")

        monkeypatch.setattr(tsc, "_systemctl_query", fake_query)
        # doctor.py's printers are imported lazily inside the function under
        # test — patch the real attributes they resolve against at call time.
        for name in ("_section", "check_ok", "check_warn", "check_fail"):
            monkeypatch.setattr(f"hermes_cli.doctor.{name}", lambda *a, **k: None)

        issues = []
        tsc.check_trix_setup_service(issues)
        return issues

    def test_enabled_and_active_is_silent_success(self, monkeypatch, tmp_path):
        issues = self._run(monkeypatch, tmp_path, enabled="enabled", active="active")
        assert issues == []

    def test_not_enabled_produces_exactly_one_client_safe_issue(self, monkeypatch, tmp_path):
        issues = self._run(monkeypatch, tmp_path, enabled="disabled", active="inactive")

        assert len(issues) == 1
        message = issues[0]
        assert message.strip()
        assert "поддерж" in message.lower()  # points the client at support
        assert "systemctl" not in message  # not a command the client can run
        assert "trix-setup" not in message  # not internal jargon

    def test_enabled_but_inactive_produces_exactly_one_client_safe_issue(self, monkeypatch, tmp_path):
        issues = self._run(monkeypatch, tmp_path, enabled="enabled", active="inactive")

        assert len(issues) == 1
        message = issues[0]
        assert message.strip()
        assert "поддерж" in message.lower()
        assert "systemctl" not in message

    def test_not_enabled_and_enabled_but_inactive_are_distinguishable(self, monkeypatch, tmp_path):
        # Different underlying problems (permanent risk on reboot vs. a
        # currently-down-but-recoverable service) must not collapse into
        # one indistinguishable string — support needs to tell them apart
        # even though both are phrased for a client with no console.
        not_enabled_issues = self._run(monkeypatch, tmp_path, enabled="disabled", active="inactive")
        inactive_only_issues = self._run(monkeypatch, tmp_path, enabled="enabled", active="inactive")

        assert not_enabled_issues[0] != inactive_only_issues[0]

    def test_systemctl_unavailable_does_not_add_a_false_positive_issue(self, monkeypatch, tmp_path):
        issues = self._run(monkeypatch, tmp_path, enabled=None, active="active")
        assert issues == []

        issues = self._run(monkeypatch, tmp_path, enabled="enabled", active=None)
        assert issues == []


class TestWiredIntoRunDoctor:
    """Regression test targeting the WIRE between ``run_doctor()`` and this
    check, not the check's own logic (covered by every class above).

    Every test above calls ``check_trix_setup_service()`` directly and
    would stay green even if ``run_doctor()`` stopped calling it entirely
    -- e.g. the call site silently replaced with ``pass``. This test
    calls the real, unmodified ``run_doctor()`` (the same entrypoint
    ``cmd_doctor``/the CLI use) and proves the check actually fires from
    inside it, by substituting a spy for the leaf function and asserting
    it was invoked -- not by asserting anything about the check's own
    output, which is already covered elsewhere.
    """

    def test_run_doctor_actually_calls_check_trix_setup_service(self, monkeypatch):
        from types import SimpleNamespace

        from hermes_cli import doctor as doctor_mod

        calls = []
        monkeypatch.setattr(
            "hermes_cli.trix_setup_service_check.check_trix_setup_service",
            lambda issues: calls.append(issues),
        )

        doctor_mod.run_doctor(SimpleNamespace(fix=False, ack=None))

        assert len(calls) == 1


class TestSystemctlAnswerParsing:
    """``_systemctl_query`` must tell "не смогли спросить" from "выключено".

    The classes above stub this function out, so nothing exercised what it
    does with a real ``systemctl`` result. That is where the false alarm
    lived: without a systemd user session (``sudo -u user`` with no
    ``XDG_RUNTIME_DIR``, a provisioning step, a support script) systemctl
    exits non-zero, writes the bus error to stderr, and leaves stdout
    EMPTY. The empty string is not ``None``, so it sailed past the guard
    and was read as "not enabled" — the loudest verdict this module has,
    telling a client to contact support about a service that is enabled
    and running.
    """

    @staticmethod
    def _fake_run(monkeypatch, *, stdout: str, stderr: str = "", returncode: int = 0):
        import subprocess

        def fake(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args[0] if args else [], returncode=returncode,
                stdout=stdout, stderr=stderr,
            )

        monkeypatch.setattr(tsc.subprocess, "run", fake)

    def test_no_user_bus_reads_as_unknown_not_as_disabled(self, monkeypatch):
        self._fake_run(
            monkeypatch,
            stdout="",
            stderr=(
                "Failed to connect to bus: $DBUS_SESSION_BUS_ADDRESS and "
                "$XDG_RUNTIME_DIR not defined\n"
            ),
            returncode=1,
        )
        assert tsc._systemctl_query("is-enabled") is None

    @pytest.mark.parametrize(
        "answer,code",
        [("disabled", 1), ("inactive", 3), ("failed", 3), ("enabled", 0), ("active", 0)],
    )
    def test_a_real_answer_survives_even_with_a_non_zero_exit(self, monkeypatch, answer, code):
        # systemctl reports genuinely-negative states in stdout AND exits
        # non-zero. Those answers must reach the caller unchanged -- the
        # fix must not swallow a real "disabled" along with the bus error.
        self._fake_run(monkeypatch, stdout=answer + "\n", returncode=code)
        assert tsc._systemctl_query("is-enabled") == answer

    def test_a_disabled_unit_still_reaches_the_client_facing_issue(self, monkeypatch, tmp_path):
        _patch_environment(monkeypatch)
        _patch_unit_exists(monkeypatch, tmp_path, exists=True)
        self._fake_run(monkeypatch, stdout="disabled\n", returncode=1)
        issues: list = []
        tsc.check_trix_setup_service(issues)
        assert len(issues) == 1

    def test_a_missing_user_session_adds_no_issue_at_all(self, monkeypatch, tmp_path):
        _patch_environment(monkeypatch)
        _patch_unit_exists(monkeypatch, tmp_path, exists=True)
        self._fake_run(monkeypatch, stdout="", stderr="Failed to connect to bus", returncode=1)
        issues: list = []
        tsc.check_trix_setup_service(issues)
        assert issues == []
