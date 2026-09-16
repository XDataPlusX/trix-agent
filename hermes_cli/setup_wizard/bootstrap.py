"""Self-signed TLS cert + primary-credential write for the setup wizard.

Spec 8 (``docs/product/specs/2026-08-25-trix-agent-wizard-permanent-access-
design.md``), §9.1/§9.5: this module no longer generates the login/password
— it receives both from cloud-init (already exported to VMmanager for the
provisioning email) and only hashes them into the ``primary`` slot.
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
from pathlib import Path

from hermes_cli.setup_wizard.state import WizardState
from hermes_constants import get_hermes_home, secure_parent_dir


def cert_paths() -> tuple[Path, Path]:
    d = get_hermes_home() / "setup-wizard"
    return d / "tls.crt", d / "tls.key"


def generate_self_signed_cert(ip: str) -> tuple[Path, Path]:
    """Generate a self-signed cert with `ip` as the SAN.

    `ip` must be a bare IPv4 or IPv6 address (no URL brackets around IPv6,
    no port, no extra `-subj`/`-addext` content) — it is validated with
    `ipaddress.ip_address()` before being interpolated into the openssl
    command line.
    """
    if shutil.which("openssl") is None:
        raise RuntimeError(
            "openssl не найден — мастер настройки не может создать сертификат. "
            "Установите пакет openssl и повторите."
        )
    try:
        ipaddress.ip_address(ip)
    except ValueError as e:
        raise RuntimeError(
            f"{ip!r} не является IP-адресом — укажите IP машины."
        ) from e
    crt, key = cert_paths()
    crt.parent.mkdir(parents=True, exist_ok=True)
    secure_parent_dir(crt)
    try:
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
                "-days", "3650", "-nodes",
                "-keyout", str(key), "-out", str(crt),
                "-subj", f"/CN={ip}",
                "-addext", f"subjectAltName=IP:{ip}",
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            "openssl не смог создать сертификат: "
            f"{e.stderr.decode(errors='replace') if isinstance(e.stderr, bytes) else e.stderr}"
        ) from e
    os.chmod(key, 0o600)
    return crt, key


def run_bootstrap(ip: str, login: str, password: str) -> None:
    """Generate the self-signed TLS cert (if not already present) and write
    the ``primary`` credential slot from values the caller already generated.

    Spec §9.1/§9.5 ("Кто генерирует и когда"): the provisioning recipe
    (cloud-init), not this module, generates the login and password — the
    synchronous part of the recipe needs them immediately to hand off to
    VMmanager over ``vm_export_variable`` (§9.4), and by the time this
    background-stage call runs, that handoff has already happened. This
    function therefore never generates a credential itself; it only hashes
    what it's given (via ``WizardState.issue_primary`` — never written to
    disk in the clear, never printed, never returned) and creates the cert.
    """
    crt, key = cert_paths()
    if not (crt.is_file() and key.is_file()):
        generate_self_signed_cert(ip)
    WizardState.load().issue_primary(login, password)
