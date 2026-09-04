"""Doctor check for the setup wizard's own persistence — the client's only
rescue door.

Lives here, not in `doctor.py`, for the same reason as `trix_status.py`:
our logic in our module, a one-line import + one-line call at the point
`doctor.py`'s `run_doctor()` already runs the sibling
`_check_gateway_service_linger()` check. `doctor.py` is ~3300 lines we
regularly pull down from upstream; a function of ours added directly
inside it is paid for in merge conflicts every time we do that.

**Why this exists.** `doctor.py` already checks whether the *gateway's*
systemd unit survives logout (`_check_gateway_service_linger`), but
nothing checked the setup wizard's own unit
(`trix-setup.service` — `hermes_cli/setup_wizard/gateway_ctl.py`'s
`_WIZARD_SERVICE_NAME`). That unit is not cosmetic: per
`docs/product/PROMPT-spec15-support-page.md`, it is the client's *only*
way back into their machine — XDataPlus does not hold standing SSH keys
or passwords onto a client's VM (`Вводные.md` §5), so if this service is
not enabled, a reboot severs access permanently, and support only learns
about it from a client who can no longer reach anything.

**Silence conditions mirror `_check_gateway_service_linger`.** Not Linux,
inside an s6-supervised container, or the unit file simply absent (a
developer laptop or any machine that was never provisioned as a Trix
client VM) all produce no output and no issue — those machines were
never going to have this unit, and `doctor` cannot tell "never
provisioned" apart from "provisioning silently failed" from the unit's
absence alone. Once the unit file exists, staying silent stops: enabled
AND active is the only quiet-success state.

**Client-safe vs. our-eyes-only text.** `check_warn`/`check_fail` (this
process's stdout) carry the systemd detail — `hermes doctor` is a tool
run by us or by scripts we control. What lands in `issues` is different:
per the PROMPT, that list is what a future support-page verdict may
surface to a client who has no console and cannot run `systemctl`
themselves, so it stays a short, jargon-free Russian sentence that points
at support rather than at a command they cannot type.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_UNIT_NAME = "trix-setup.service"
_SYSTEMCTL_TIMEOUT = 10.0

_MSG_NOT_ENABLED = (
    "Служба аварийного доступа (мастер настройки) выключена — после "
    "перезагрузки машины доступ к ней может быть потерян навсегда. "
    "Обратитесь в поддержку."
)
_MSG_NOT_ACTIVE = (
    "Служба аварийного доступа (мастер настройки) сейчас не запущена. "
    "Обратитесь в поддержку."
)


def _unit_path() -> Path:
    """Mirror `setup_wizard/cli.py`'s private `_unit_path()`.

    Deliberately not imported from there: it's a leading-underscore
    (private) helper in a module this task must not edit, and the path
    scheme it implements — a systemd --user unit under the OS account's
    own `~/.config/systemd/user/` — is a stable convention, not something
    likely to drift. `Path.home()`, not `get_hermes_home()`: this is the
    OS user's home directory that owns the systemd --user session, not a
    profile-scoped `HERMES_HOME` — the wizard's unit is one per machine
    account, unrelated to which Hermes profile is active.
    """
    return Path.home() / ".config" / "systemd" / "user" / _UNIT_NAME


def _systemctl_query(verb: str) -> str | None:
    """Run ``systemctl --user <verb> trix-setup.service``.

    Returns stripped stdout, or ``None`` when systemctl itself could not
    be asked (missing binary, no user session, timeout) — every failure
    mode here is swallowed the same way
    `gateway_ctl.stop_and_disable_wizard_service` swallows them, because
    "systemctl is unusable right now" is not itself proof the unit is
    misconfigured.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", verb, _UNIT_NAME],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SYSTEMCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    answer = result.stdout.strip()
    if not answer:
        # systemctl ran, but had nothing to say — it could not reach the
        # user bus. That is "не смогли спросить", not "выключено".
        #
        # Отличить одно от другого можно ровно по этому признаку: на
        # ВОПРОС systemctl всегда отвечает в stdout и словом, даже когда
        # ответ отрицательный ("disabled", "inactive", "failed") и код
        # возврата ненулевой. Пустой stdout остаётся только для отказа
        # самого systemctl, и он уходит в stderr — "Failed to connect to
        # bus: $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not
        # defined".
        #
        # Раньше пустая строка проскакивала мимо проверки `is None` и
        # читалась как "не enabled", то есть как САМЫЙ громкий вердикт
        # этого модуля: клиент получал «Служба аварийного доступа
        # выключена — ... Обратитесь в поддержку» на машине, где служба
        # enabled и active. Воспроизведено на trix-testing7.ru
        # 2026-09-04: любой вызов доктора из окружения без
        # XDG_RUNTIME_DIR (`sudo -u user` без сессии, скрипт поддержки,
        # шаг провижининга) печатал ложную тревогу; из-под настоящей
        # пользовательской сессии тот же доктор молчал.
        return None
    return answer


def check_trix_setup_service(issues: list) -> None:
    """Verify the setup wizard's own systemd unit exists, is enabled, and
    is active.

    Appends a short Russian, client-safe sentence to ``issues`` when the
    unit exists but isn't enabled+active; stays silent when the check
    does not apply here at all (see module docstring).
    """
    from hermes_cli.doctor import _section, check_fail, check_ok, check_warn
    from hermes_cli.gateway import is_linux
    from hermes_cli.service_manager import detect_service_manager

    if not is_linux():
        return

    # Inside a container under our s6 /init there is no systemd --user
    # session and no "reboot" concept for a host VM — same reasoning as
    # _check_gateway_service_linger's own s6 skip.
    if detect_service_manager() == "s6":
        return

    if not _unit_path().exists():
        return

    _section("Setup Wizard Service")

    enabled = _systemctl_query("is-enabled")
    active = _systemctl_query("is-active")

    if enabled is None or active is None:
        check_warn(
            "Could not verify trix-setup.service state",
            "(systemctl --user unavailable or timed out)",
        )
        return

    is_enabled = enabled == "enabled"
    is_active = active == "active"

    if is_enabled and is_active:
        check_ok(
            "trix-setup.service enabled and running",
            "(client's rescue access survives a reboot)",
        )
        return

    if not is_enabled:
        check_fail(
            "trix-setup.service is not enabled",
            f"(is-enabled: {enabled!r}, is-active: {active!r} — a reboot "
            "will permanently strand this client; "
            "`hermes setup-wizard install-service` re-enables it)",
        )
        issues.append(_MSG_NOT_ENABLED)
        return

    check_warn(
        "trix-setup.service is enabled but not currently active",
        f"(is-active: {active!r} — should come back on the next boot, "
        "but the client cannot reach it right now; "
        "`hermes setup-wizard install-service` starts it)",
    )
    issues.append(_MSG_NOT_ACTIVE)
