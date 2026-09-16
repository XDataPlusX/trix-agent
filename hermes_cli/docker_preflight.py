"""Docker preflight check for the Trix Agent installer.

The curated config template (``assets/config/trix-config.yaml``) sets
``terminal.backend: docker`` — every agent command runs inside a Docker
sandbox. Docker itself is deliberately NOT installed by
``scripts/install.sh``: provisioning it is root's job at VM-creation time
(see the "Docker" section of ``docs/product/deployment-requirements.md``,
which documents this provisioning contract — the general "Root vs user —
зависимости без sudo.md" note does not mention Docker at all), and the
installer always runs as an unprivileged user without sudo.

Without a preflight check, a VM whose provisioning script never ran (or
ran incompletely) only discovers the gap the first time the client asks
the agent to run a command — and it is the client who finds out, not us.
See ``docs/product/specs/2026-08-17-trix-agent-standard-build-design.md``
§4.5 and §10.

:func:`check_docker_backend` is the single, testable place this decision
lives. ``scripts/install.sh`` calls it via a short Python subprocess, the
same pattern used by :mod:`hermes_cli.config_template` for template
resolution.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

# Short by design: this check runs at the tail of every install, and a
# wedged/overloaded Docker daemon must never be able to hang the
# installer. `docker info` against a healthy daemon returns in well under
# a second; 5s gives real machines slack without letting a dead daemon
# stall the operator for long.
_DEFAULT_TIMEOUT_SECONDS = 5.0

_PERMISSION_MARKERS = (
    "permission denied",
    "access is denied",
)


@dataclass(frozen=True)
class DockerPreflightResult:
    """Outcome of probing whether the Docker sandbox backend is usable."""

    binary_found: bool
    daemon_responds: bool
    usable_without_sudo: bool
    message: str

    @property
    def ok(self) -> bool:
        """True only when the sandbox is fully usable right now."""
        return self.binary_found and self.daemon_responds and self.usable_without_sudo

    def to_dict(self) -> dict:
        """Machine-readable form for programmatic callers.

        ``scripts/install.sh`` keeps parsing the two-line "OK"/"WARN" +
        message text this module has always printed via its short Python
        subprocess -- that contract is unchanged. This method is the
        *additional*, JSON-serializable path a future caller that isn't a
        human reading install output (the spec 12 support page) can use
        instead of re-deriving structure from that text. Shape is shared
        across :mod:`docker_preflight`, :mod:`browser_preflight`, and
        :mod:`search_preflight`: ``check``, ``ok``, ``message``, ``details``.
        """
        return {
            "check": "docker",
            "ok": self.ok,
            "message": self.message,
            "details": {
                "binary_found": self.binary_found,
                "daemon_responds": self.daemon_responds,
                "usable_without_sudo": self.usable_without_sudo,
            },
        }


def check_docker_backend(timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> DockerPreflightResult:
    """Probe whether ``docker`` is installed, reachable, and usable without sudo.

    Runs ``docker info`` as the current (unprivileged) user — never with
    sudo, matching the product's "agent needs tools, not root" posture.
    Distinguishes several outcomes, each with its own message:

    1. The ``docker`` binary is not on ``PATH`` at all.
    2. The binary exists but the daemon does not respond in time (hung or
       overloaded) — a ``subprocess.TimeoutExpired``.
    3. The binary exists but can't even be started (an ``OSError``, e.g.
       a broken/foreign-arch binary).
    4. The binary runs and exits non-zero without a permission marker in
       its output (daemon genuinely not running / other failure).
    5. The binary runs and exits non-zero WITH a permission marker in its
       output — the current user is denied access (not in the ``docker``
       group) — needs `usermod -aG docker` from an administrator.
    6. Everything works.

    Args:
        timeout: Hard wall-clock bound on the probe, in seconds. Keeps a
            wedged daemon from stalling the installer.

    Returns:
        A :class:`DockerPreflightResult` with three independent booleans
        plus a Russian, administrator-facing message.
    """
    docker_path = shutil.which("docker")
    if not docker_path:
        return DockerPreflightResult(
            binary_found=False,
            daemon_responds=False,
            usable_without_sudo=False,
            message=(
                "Docker не найден на этой машине. Агент настроен на выполнение "
                "команд в Docker-песочнице (terminal.backend: docker), но сам "
                "Docker не установлен. Это разовая задача администратора при "
                "подготовке VM — см. раздел «Docker» в "
                "docs/product/deployment-requirements.md: от root поставить "
                "docker (например, apt-get install -y docker.io), добавить "
                "пользователя агента в группу docker (usermod -aG docker "
                "<пользователь агента>) и включить автозапуск демона "
                "(systemctl enable --now docker). До этого агент продолжает "
                "отвечать на вопросы, но выполнять команды не сможет."
            ),
        )

    try:
        proc = subprocess.run(
            [docker_path, "info"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return DockerPreflightResult(
            binary_found=True,
            daemon_responds=False,
            usable_without_sudo=False,
            message=(
                f"Docker установлен, но демон не ответил за {timeout:g} с — "
                "похоже, он завис или перегружен. Администратору стоит "
                "проверить его состояние (systemctl status docker) и при "
                "необходимости перезапустить (systemctl restart docker) — "
                "см. раздел «Docker» в docs/product/deployment-requirements.md. "
                "До этого агент продолжает отвечать на вопросы, но "
                "выполнять команды не сможет."
            ),
        )
    except OSError:
        # Deliberately no raw exception text in the message: OSError here
        # (e.g. "Exec format error") surfaces the full local filesystem
        # path to the docker binary, which is noise, not actionable
        # guidance, for an administrator reading this over a Telegram/log
        # line. The detail is still available in the installer's own debug
        # output for anyone actually diagnosing it.
        return DockerPreflightResult(
            binary_found=True,
            daemon_responds=False,
            usable_without_sudo=False,
            message=(
                "Docker найден, но запустить его не удалось (docker info не "
                "смог стартовать). Администратору стоит проверить установку "
                "Docker на этой машине — см. раздел «Docker» в "
                "docs/product/deployment-requirements.md. До этого агент "
                "продолжает отвечать на вопросы, но выполнять команды не "
                "сможет."
            ),
        )

    if proc.returncode == 0:
        return DockerPreflightResult(
            binary_found=True,
            daemon_responds=True,
            usable_without_sudo=True,
            message="Docker найден, демон отвечает и доступен без sudo — песочница команд готова к работе.",
        )

    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
    if any(marker in combined for marker in _PERMISSION_MARKERS):
        return DockerPreflightResult(
            binary_found=True,
            daemon_responds=True,
            usable_without_sudo=False,
            message=(
                "Docker установлен и демон отвечает, но пользователю, от "
                "которого работает агент, не хватает прав обращаться к нему "
                "без sudo. Администратору стоит добавить этого пользователя "
                "в группу docker (usermod -aG docker <пользователь агента>) "
                "и перезапустить сам сервис агента (hermes gateway restart) "
                "— членство в группе применяется к новым процессам, уже "
                "запущенный процесс шлюза его не подхватит. См. раздел "
                "«Docker» в docs/product/deployment-requirements.md. До "
                "этого агент продолжает отвечать на вопросы, но выполнять "
                "команды не сможет."
            ),
        )

    return DockerPreflightResult(
        binary_found=True,
        daemon_responds=False,
        usable_without_sudo=False,
        message=(
            "Docker установлен, но демон не отвечает (docker info завершился "
            "с ошибкой). Администратору стоит проверить, запущен ли сервис "
            "(systemctl status docker), и включить автозапуск при "
            "необходимости (systemctl enable --now docker) — см. раздел "
            "«Docker» в docs/product/deployment-requirements.md. До этого "
            "агент продолжает отвечать на вопросы, но выполнять команды не "
            "сможет."
        ),
    )
