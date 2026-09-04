"""``hermes setup-wizard`` CLI subcommands + the systemd unit that hosts the
setup wizard's own web server (spec §6, §11; Rulings 4, 9).

Implements spec 8 (``docs/product/specs/2026-08-25-trix-agent-wizard-
permanent-access-design.md``), §9.1-§9.3, §13: the wizard now stays up
permanently behind a permanent login+password (the ``primary`` slot),
issued once by cloud-init and never generated here except via the
emergency ``set-password`` path.

Subcommands:

- ``bootstrap --ip <ip> --login <login> --password-file <path>`` —
  first-run entry point (spec §9.1/§9.5): generates the self-signed TLS
  cert (:func:`bootstrap.run_bootstrap`) and writes the ``primary``
  credential slot from a login/password the *caller* already generated
  (cloud-init's synchronous stage — see the module's spec reference).
  Reads the password from ``--password-file`` (deleted immediately after
  reading) and never prints it, writes it to disk in the clear, or returns
  it — only the resulting URL goes to stdout. No ``--port`` override here
  — the printed URL must always match what ``serve``/the installed unit
  actually binds (``DEFAULT_PORT``; the unit's ``ExecStart`` never passes
  ``--port``), so offering one would let an operator print an address
  nothing listens on.
- ``serve [--port 8443]`` — runs the wizard's FastAPI app
  (:func:`app.create_app`) under a single-process ``uvicorn`` (no
  ``workers>1`` — the submit lock in ``app.py`` is process-local, see
  Ruling 9c). Refuses to start (with a Russian message) when no cert has
  been generated yet, and self-disables instead of binding at all when the
  wizard has been explicitly closed by a prior ``close`` (see
  :func:`should_self_extinguish`) — completing the first-run form no
  longer disables it (§4.3): the wizard stays open for return visits.
- ``open [--ttl-minutes 60]`` — emergency path (spec §9.3, not for the
  client): issue a fresh *temporary* password (the ``temporary`` slot,
  which never overwrites ``primary``) and (re)start the hosting service.
  Composes the public :func:`start_wizard_service` and
  :func:`issue_temporary_password` so other callers (the gateway's
  ``/setup`` handler) can use either half independently — ``/setup`` only
  needs the service-starting half, not a fresh credential (§6).
- ``status`` — human-readable (Russian) open/closed/completed state.
- ``close`` — disables the wizard (the ``disabled`` flag, §4.3) and stops
  the hosting service. Distinct from ``primary`` being issued/``completed``
  — a client who finished setup must NOT have the wizard close itself.
- ``set-password [--login <login>] [--print]`` — emergency credential
  rotation (spec §7): the client's only recourse if the provisioning email
  leaked. Not advertised to clients. Writes a fresh ``primary`` slot;
  prints the new login/password only when ``--print`` is given.
- ``install-service`` — writes the systemd user unit
  (``~/.config/systemd/user/trix-setup.service``,
  :func:`unit_file_text`) that runs ``serve`` as a long-lived service,
  clears ``disabled`` (spec §9.3: this is the documented way to reopen a
  panel a prior ``close`` shut), and enables **and starts** it (with a
  linger preflight — see :func:`_cmd_install_service`). Starting it here,
  not just enabling it, matters because cloud-init's provisioning email
  goes out before anyone has rebooted the VM (§9.1.3) — without an
  immediate start, the client's first connection is a refused one.

Why this module calls out to ``systemctl --user`` (rather than, say,
forking a daemon itself): the wizard's hosting process needs to survive
across the CLI invocation that started it (``open``/``bootstrap`` from a
cloud-init script, or Task 11's Telegram handler running inside the
gateway process) and be restartable by systemd on crash. This mirrors
``hermes_cli/gateway.py``'s own service-lifecycle pattern for the
messaging gateway — see the module docstring's "second instance" note in
the task report for exactly what is reused vs. deliberately duplicated.

Import discipline: this module (and everything it imports at *module*
scope — ``bootstrap``, ``state``) must stay cheap, because
``hermes_cli.main`` builds the whole argparse tree — including this
module's subparser — on every single ``hermes`` invocation, not just
``hermes setup-wizard ...`` ones. Anything that pulls in
``hermes_cli.gateway`` or ``gateway.status`` (heavier transitive imports)
is deliberately imported *inside* the function that needs it, not at the
top of this file — see ``unit_file_text()``, ``_cmd_close()``, and
``_cmd_install_service()``.
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import secrets
import socket
import string
import subprocess
import sys
from pathlib import Path

from hermes_cli.setup_wizard import bootstrap
from hermes_cli.setup_wizard.state import WizardState

DEFAULT_PORT = 8443

_MSG_NO_CERT = (
    "Сертификат мастера настройки не найден. Сначала выполните "
    "`hermes setup-wizard bootstrap --ip <IP>`."
)
_IP_PLACEHOLDER = "<IP-адрес-машины>"
_CERT_SUBJECT_CN_RE = re.compile(r"CN\s*=\s*([^,/\n]+)")


def _wizard_service_name() -> str:
    """The wizard's own systemd unit name (``"trix-setup.service"``).

    Imported lazily from :mod:`gateway_ctl` (which already owns this as
    ``_WIZARD_SERVICE_NAME`` — it needs the same name to stop+disable the
    unit in ``stop_and_disable_wizard_service()``) rather than duplicated
    as a second literal here that could drift out of sync. Kept as a
    function-local import (not a module-level one) so importing this
    module never pulls in ``gateway_ctl`` (and, transitively,
    ``gateway.status``) just to build the argparse tree — see the module
    docstring's "Import discipline" note.
    """
    from hermes_cli.setup_wizard.gateway_ctl import _WIZARD_SERVICE_NAME

    return _WIZARD_SERVICE_NAME


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _wizard_service_name()


def unit_file_text() -> str:
    """Render the systemd user-unit text for the wizard's hosting service.

    Modeled on ``hermes_cli/gateway.py``'s ``generate_systemd_unit`` (same
    ``[Unit]``/``[Service]``/``[Install]`` shape, same venv-aware
    interpreter via ``get_python_path()``), but deliberately not that same
    function: the gateway unit's system/user dual-scope handling, target-
    user remapping, and managed-Node PATH assembly all solve problems the
    wizard's always-current-user, no-PATH-dependent web server doesn't
    have. See the task report for the full "second instance" rationale.

    Ruling 9: the description must not name Hermes — the unit is a
    generic-sounding scaffolding service, not a product placement.
    """
    # Lazy import: only pays for the gateway module's import cost when a
    # unit is actually being generated (see module docstring).
    from hermes_cli.gateway import get_python_path

    python_path = get_python_path()
    return f"""[Unit]
Description=Trix Agent Setup Wizard
After=network-online.target

[Service]
Type=simple
ExecStart={python_path} -m hermes_cli.main setup-wizard serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


_MSG_CONFIGURED_TLS_MISSING = (
    "В config.yaml указаны setup_wizard.tls_cert/tls_key, но по этим путям "
    "нет файлов. Чтобы не подменять явную настройку самоподписанным "
    "сертификатом молча, запуск остановлен — проверьте пути или уберите "
    "эти ключи из config.yaml."
)


def _configured_tls_paths() -> tuple[Path, Path] | None:
    """A client-supplied ``setup_wizard.tls_cert``/``tls_key`` pair from
    ``config.yaml``, if both keys are set — ``None`` when neither/only one
    is configured (self-signed cert stays in charge, unchanged behavior).

    Deployment-requirements.md's "TLS-компромисс" section documents this
    escape hatch: once a client has their own domain + real certificate,
    pointing these two keys at it removes the browser warning without a
    code change. Lazy import of ``hermes_cli.config`` — see the module
    docstring's "Import discipline" note; ``load_config()`` is heavier than
    this file wants paid on every ``hermes`` invocation's argparse build.
    """
    from hermes_cli.config import load_config

    section = load_config().get("setup_wizard") or {}
    if not isinstance(section, dict):
        return None
    cert = section.get("tls_cert")
    key = section.get("tls_key")
    if not (cert and key):
        return None
    return Path(cert), Path(key)


def build_uvicorn_kwargs(port: int) -> dict:
    """Uvicorn config for ``serve`` — a pure function so tests can assert
    on it without ever binding a socket.

    Prefers an operator-configured ``setup_wizard.tls_cert``/``tls_key``
    pair (see :func:`_configured_tls_paths`) over the wizard's own
    self-signed cert when both are set. A configured pair whose files
    don't actually exist on disk exits loudly instead of silently falling
    back to the self-signed cert — an explicit config value must never be
    quietly overridden for something security-relevant like TLS material.
    With no configured pair at all, exits (Russian message on stderr) when
    no self-signed cert has been generated yet — ``serve`` must never fall
    back to a plaintext bind.
    """
    configured = _configured_tls_paths()
    if configured is not None:
        crt, key = configured
        if not (crt.is_file() and key.is_file()):
            print(_MSG_CONFIGURED_TLS_MISSING, file=sys.stderr)
            raise SystemExit(1)
    else:
        crt, key = bootstrap.cert_paths()
        if not (crt.is_file() and key.is_file()):
            print(_MSG_NO_CERT, file=sys.stderr)
            raise SystemExit(1)
    return {
        "host": "0.0.0.0",
        "port": port,
        "ssl_certfile": str(crt),
        "ssl_keyfile": str(key),
        "log_level": "warning",
        "access_log": False,
        # X-Forwarded-For НЕ доверяем (спека 8 §8.1). Наш _client_ip() и так
        # берёт только request.client.host, но uvicorn по умолчанию
        # proxy_headers=True с forwarded_allow_ips="127.0.0.1", и его
        # ProxyHeadersMiddleware подменяет client.host из заголовка ещё ДО
        # нашего кода — для любого соединения с локалхоста. То есть локальный
        # процесс (включая самого агента, у которого есть терминал) обходил бы
        # собственную блокировку и раздувал failures_by_ip поддельными
        # ключами. Прокси перед мастером нет и не предполагается.
        "proxy_headers": False,
    }


def should_self_extinguish() -> bool:
    """True when ``serve`` should refuse to bind and disable its own unit.

    Spec §4.3: ``completed`` stopped being a gate anywhere — a wizard whose
    first-run form has been submitted stays open for return visits (the
    entire point of permanent access, spec 8). The only thing that closes
    the wizard is the explicit ``disabled`` flag, flipped by ``hermes
    setup-wizard close``. Reads fresh state (not a cached instance) so a
    ``serve`` that starts right after a ``close`` (or after a crash left
    the unit enabled) still sees the up-to-date flag.
    """
    return WizardState.load().is_disabled()


def _disable_service() -> None:
    """Best-effort ``systemctl --user disable`` of this service's own unit.

    Called only from inside ``serve`` right before it exits without ever
    binding (see :func:`should_self_extinguish`) — the process is about to
    exit 0 on its own, so nothing needs to ``stop`` it; ``disable`` alone
    prevents it from coming back at the next boot/session start. Systemd
    may not be present at all (macOS, minimal containers) — that is a
    normal outcome, not an error.
    """
    try:
        subprocess.run(
            ["systemctl", "--user", "disable", _wizard_service_name()],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _ip_from_cert() -> str | None:
    """Read the IP baked into ``tls.crt``'s Subject CN.

    ``bootstrap.generate_self_signed_cert`` sets the cert's ``-subj
    f"/CN={ip}"`` to whatever IP the operator gave ``bootstrap --ip``.
    That is the *authoritative* address — the one the cert itself claims
    to serve, and (per spec §6) the one an operator deliberately chose —
    unlike :func:`_detect_ip`'s routing guess, which on a NAT'd cloud VM
    (AWS/GCP) returns the private NIC address (``10.x.x.x``) rather than
    the public IP a client actually connects through.
    ``start_wizard_service``/``_cmd_open`` try this first and only fall back
    to ``_detect_ip`` when no cert exists yet or ``openssl`` can't be read.

    Returns ``None`` (never raises) on any failure: no cert file, no
    ``openssl`` binary, a non-zero exit, unparsable output, or a CN that
    isn't a valid IP address (defensive — the cert is always generated by
    this codebase, but a hand-edited/foreign cert must fail closed to the
    routing-guess fallback, not crash the callers above).
    """
    crt, _key = bootstrap.cert_paths()
    if not crt.is_file():
        return None
    try:
        result = subprocess.run(
            ["openssl", "x509", "-in", str(crt), "-noout", "-subject"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = _CERT_SUBJECT_CN_RE.search(result.stdout)
    if not match:
        return None
    candidate = match.group(1).strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def _detect_ip() -> str | None:
    """Best-effort local IP detection via a UDP "connect" to a public
    address (Google Public DNS, ``8.8.8.8``) — ``connect()`` on a UDP
    socket only asks the kernel to pick the outbound route/interface for
    that destination; no packet is ever actually sent, so this never
    touches the network or depends on ``8.8.8.8`` being reachable.

    This is a *routing guess*, not authoritative — on a NAT'd cloud VM it
    returns the private NIC address, not the public one a client connects
    through. :func:`start_wizard_service`/:func:`_cmd_open` only fall back
    to this when :func:`_ip_from_cert` (the address the operator actually
    chose at ``bootstrap`` time) is unavailable.

    Returns ``None`` (never raises) when there's no route at all (e.g. an
    offline dev box) — callers must fall back to an honest placeholder.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _format_url(ip: str | None, port: int) -> str:
    if ip is None:
        return f"https://{_IP_PLACEHOLDER}:{port} (не удалось определить IP автоматически)"
    try:
        is_v6 = ipaddress.ip_address(ip).version == 6
    except ValueError:
        is_v6 = ":" in ip
    host = f"[{ip}]" if is_v6 else ip
    return f"https://{host}:{port}"


def _probe_port_open(port: int, timeout: float = 2.0) -> bool:
    """Best-effort local liveness check: is anything answering on
    ``127.0.0.1:port`` yet?

    A single short attempt, not a retry/poll loop — its only job is to
    warn the operator up front (see :func:`start_wizard_service`) instead
    of them copying a URL and then hitting "connection refused", not to
    guarantee readiness. A ``False`` result does not mean the service
    will never come up — it may simply still be mid-boot.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def _start_service() -> None:
    """Start the wizard's hosting service: ``systemctl --user start`` first,
    falling back to a detached ``serve`` subprocess when systemd isn't
    available/working (no user session, unit not installed yet, etc.).
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "start", _wizard_service_name()],
            capture_output=True,
            timeout=15,
            check=False,
        )
        started = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        started = False

    if started:
        return

    subprocess.Popen(
        [sys.executable, "-m", "hermes_cli.main", "setup-wizard", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def start_wizard_service() -> tuple[str, bool]:
    """(Re)start the wizard's hosting service and report its address —
    never touches credentials.

    Public so the gateway's ``/setup`` handler (spec §6) can call this
    directly: `/setup` no longer mints or reveals a password, so it must
    not compose anything that mutates a credential slot as a side effect
    — only this half of what used to be a single ``open_wizard()``. See
    :func:`issue_temporary_password` for the other half, and
    :func:`_cmd_open` (§9.3) for the emergency path that uses both.

    Returns ``(url, reachable)``. ``url`` prefers the IP baked into the TLS
    cert's Subject CN (:func:`_ip_from_cert` — the address the operator
    chose at ``bootstrap`` time, authoritative even behind NAT), falling
    back to a routing guess (:func:`_detect_ip`) and finally an honest
    placeholder when neither is available.

    Before returning, does a short local TCP probe
    (:func:`_probe_port_open`) against the port the service should be
    listening on; ``reachable`` carries that probe's result so callers can
    surface an honest warning to their own client, not just a terminal.
    """
    _start_service()
    ip = _ip_from_cert() or _detect_ip()
    url = _format_url(ip, DEFAULT_PORT)
    reachable = _probe_port_open(DEFAULT_PORT)
    return url, reachable


_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def _generate_password(length: int) -> str:
    """`length`-char password drawn only from `A-Za-z0-9` (spec §4.1).

    Deliberately narrower than ``secrets.token_urlsafe`` (which includes
    ``-``/``_`` and can start with ``-``): the value is interpolated into
    the provisioning email's HTML template (§10) without escaping, where
    ``& < > " '`` would break it — and per §4.1 that constraint is a
    requirement, not a style choice.
    """
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def _generate_login() -> str:
    """``trix-`` + 12 random `A-Za-z0-9` chars (spec §4.1).

    Deliberately as unguessable as the password itself — not ``admin``,
    not a short recognizable suffix (owner's ruling 2026-08-25). The
    ``trix-`` prefix buys no secrecy (it's the same for every client); it
    only tells a client that stumbled on the string what it's for.
    """
    return "trix-" + "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(12))


def issue_temporary_password(ttl_seconds: int) -> str:
    """Generate and store a fresh emergency password (spec §4.2/§9.3).

    Writes only the ``temporary`` slot — ``primary`` (the client's
    permanent credentials from the provisioning email) is never touched,
    so an admin using this to help a client can't lock them out of their
    own login.
    """
    password = _generate_password(30)
    WizardState.load().issue_temporary(password, ttl_seconds)
    return password


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _read_password_file(path: str) -> str:
    """Read the plaintext password cloud-init's synchronous stage generated
    (spec §9.1/§9.5).

    Deliberately does NOT delete the file — deletion only happens once
    ``run_bootstrap`` has actually succeeded (see ``_cmd_bootstrap``).
    Deleting it here, before ``run_bootstrap`` runs, would mean a failure
    in cert generation (e.g. an empty ``PUBLIC_IP``) destroys the only
    remaining copy of the password: it isn't written to ``state.json``
    yet (that's the whole point of bootstrap), and cloud-init has no
    other channel back to it once the provisioning email has already gone
    out. Leaving the file in place lets the operator simply re-run
    ``bootstrap`` against the same file.
    """
    return Path(path).read_text(encoding="utf-8").strip()


def _delete_password_file(path: str) -> None:
    """Best-effort cleanup of the temp password file (§9.2's "временный
    файл ... удаление сразу после чтения") — called only after
    ``run_bootstrap`` has succeeded, see ``_cmd_bootstrap``.
    """
    try:
        Path(path).unlink()
    except OSError:
        pass


def _cmd_bootstrap(args: argparse.Namespace) -> None:
    password = _read_password_file(args.password_file)
    bootstrap.run_bootstrap(args.ip, args.login, password)
    # Only deleted once run_bootstrap has actually written the `primary`
    # slot — if it raised above, the file (and the only surviving copy of
    # the password) is left in place so a retry can use it (see
    # _read_password_file's docstring).
    _delete_password_file(args.password_file)
    url = _format_url(args.ip, DEFAULT_PORT)
    # Only the URL goes to stdout (spec §14.9) — the password arrived
    # already known to the caller (cloud-init generated it), is never
    # printed here, and is never written back to disk in the clear.
    print(url)


def _cmd_serve(args: argparse.Namespace) -> None:
    if should_self_extinguish():
        _disable_service()
        print("Мастер настройки закрыт — сервис не будет запущен.")
        return
    port = getattr(args, "port", DEFAULT_PORT) or DEFAULT_PORT
    kwargs = build_uvicorn_kwargs(port)
    import uvicorn

    from hermes_cli.setup_wizard.app import create_app

    uvicorn.run(create_app(), **kwargs)


def _cmd_open(args: argparse.Namespace) -> None:
    ttl_minutes = getattr(args, "ttl_minutes", 60) or 60
    url, reachable = start_wizard_service()
    password = issue_temporary_password(ttl_minutes * 60)
    if not reachable:
        print(
            "Предупреждение: порт мастера настройки пока не отвечает — "
            "возможно, сервис ещё запускается. Подождите немного и "
            "повторите подключение."
        )
    print(url)
    print(password)


_MSG_LOGIN_HAS_COLON = (
    "Логин не может содержать «:» — HTTP Basic передаёт учётные данные как "
    "«логин:пароль», и заголовок режется по первому «:». С таким логином "
    "клиент будет молча заперт снаружи. Уберите двоеточие и повторите."
)


def _cmd_set_password(args: argparse.Namespace) -> None:
    """Emergency credential rotation (spec §7) — not advertised to clients.

    Always mints a fresh 30-char password; a custom login can be supplied
    via ``--login``, otherwise one is generated the same way ``bootstrap``
    would have. The new values only reach stdout when ``--print`` is given
    — a plain confirmation is the default, so the plaintext doesn't land in
    terminal scrollback/session logs unless the caller explicitly asks for
    it.

    A ``--login`` containing ``:`` is rejected outright: HTTP Basic
    (§8.3) encodes credentials as ``login:password`` and the client
    splits on the first ``:``, so anything after it would be swallowed
    into the password half and the login could never be typed correctly
    again.
    """
    login = getattr(args, "login", None) or _generate_login()
    if ":" in login:
        print(_MSG_LOGIN_HAS_COLON, file=sys.stderr)
        raise SystemExit(2)
    password = _generate_password(30)
    WizardState.load().issue_primary(login, password)
    if getattr(args, "print_credentials", False):
        print(login)
        print(password)
    else:
        print("Учётные данные обновлены. Добавьте --print, чтобы увидеть пароль.")


def _format_status() -> str:
    state = WizardState.load()
    if state.is_disabled():
        return "Мастер настройки закрыт (отключён командой close)."
    if not state.has_primary():
        # `disabled` is False here, so `serve` (if running at all) will
        # bind and answer — just always with 401, since `verify()` can
        # never match without a `primary` slot. Saying "закрыт" in that
        # case is actively misleading to an operator debugging a partial
        # `bootstrap` failure (cert generated, slot never written): the
        # port is reachable and answering, just not with credentials that
        # work. Probe it so the message matches what a client would
        # actually see, instead of assuming "not issued" means "not
        # listening".
        if _probe_port_open(DEFAULT_PORT):
            return (
                "Порт мастера настройки слушает, но учётные данные ещё не "
                "выданы (bootstrap не завершён) — любой вход получит 401. "
                "Проверьте вывод `hermes setup-wizard bootstrap`."
            )
        return "Мастер настройки закрыт (учётные данные ещё не выданы)."
    if state.is_completed():
        line = "Мастер настройки открыт (первичная настройка завершена, доступен для повторного входа)."
    else:
        line = "Мастер настройки открыт (ждёт первичной настройки)."
    remaining = state.temporary_remaining_seconds()
    if remaining is not None:
        line += f" Также действует временный пароль, осталось: {remaining} с."
    return line


def _cmd_status(args: argparse.Namespace) -> None:  # noqa: ARG001
    print(_format_status())


def _cmd_close(args: argparse.Namespace) -> None:  # noqa: ARG001
    # Local import — see the module docstring's "Import discipline" note:
    # gateway_ctl pulls in gateway.status, which must not load just to
    # build the argparse tree for an unrelated `hermes` invocation.
    from hermes_cli.setup_wizard.gateway_ctl import stop_and_disable_wizard_service

    # `disabled` (spec §4.3), not `mark_completed()` — completing the
    # first-run form must NOT close the wizard (that was the whole bug
    # this spec fixes); only an explicit `close` does.
    WizardState.load().set_disabled(True)
    stop_and_disable_wizard_service()
    print("Мастер настройки закрыт.")


def _cmd_install_service(args: argparse.Namespace) -> None:  # noqa: ARG001
    # Spec §9.3: `install-service` is the documented un-close path ("[close]
    # включается обратно `install-service`") — clear `disabled` up front,
    # unconditionally, so re-running this command also reopens a panel a
    # prior `close` shut. This is a pure state.json write, independent of
    # whatever systemd does below.
    WizardState.load().set_disabled(False)

    unit_path = _unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit_file_text(), encoding="utf-8")
    print(f"Юнит записан: {unit_path}")

    # Local import — see the module docstring's "Import discipline" note.
    from hermes_cli.gateway import UserSystemdUnavailableError, _preflight_user_systemd

    try:
        # A fresh cloud-init/SSH session (the §11 path this unit is built
        # for) has no lingering user session yet — without enabling
        # linger, `systemctl --user enable` below either fails outright or
        # "succeeds" against a session that won't survive a reboot before
        # anyone has logged in interactively. §6.2 hands `enable` itself
        # to cloud-init, but linger was previously nobody's job — this is
        # that owner.
        _preflight_user_systemd(auto_enable_linger=True)
    except UserSystemdUnavailableError as exc:
        print(
            "Пользовательский systemd недоступен — автозапуск после "
            "перезагрузки не настроен. Подробности:"
        )
        print(str(exc))
        return
    except Exception:
        # Best-effort: no systemd at all (macOS dev box, minimal
        # container) is a normal outcome here, not an error worth a
        # traceback — the unit file is still written and usable manually.
        print(
            "systemd недоступен на этой машине — автозапуск после "
            "перезагрузки не настроен. Запустите сервис вручную: "
            "`hermes setup-wizard open`."
        )
        return

    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            timeout=15,
            check=False,
        )
        # Idempotent — safe to call on every install-service run, including
        # a repair of an already-enabled unit. An explicit `close`
        # (`should_self_extinguish`'s `_disable_service`) still disables
        # it, so enabling here only means "survive a reboot," not
        # "run forever regardless of `close`."
        subprocess.run(
            ["systemctl", "--user", "enable", _wizard_service_name()],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass

    # Spec §9.1.3/§9.5: "включить И запустить" — `enable` alone survives a
    # reboot but leaves nothing listening right now. Without this, a freshly
    # provisioned VM sends its client the address before anything is bound
    # to 8443, and the only way to start it — `hermes setup-wizard open` —
    # is reached through the same Telegram `/setup` path this spec exists
    # to route around. Reuses `_start_service()` (systemctl start, with a
    # detached-process fallback) instead of a second copy of that logic.
    _start_service()
    print("Сервис включён и запущен — переживёт перезагрузку до завершения мастера.")


_HANDLERS = {
    "bootstrap": _cmd_bootstrap,
    "serve": _cmd_serve,
    "open": _cmd_open,
    "status": _cmd_status,
    "close": _cmd_close,
    "set-password": _cmd_set_password,
    "install-service": _cmd_install_service,
}


def cmd_setup_wizard(args: argparse.Namespace) -> None:
    handler = _HANDLERS.get(args.wizard_command)
    if handler is None:
        print(
            "Укажите подкоманду: bootstrap, serve, open, status, close, "
            "set-password, install-service."
        )
        raise SystemExit(2)
    handler(args)
