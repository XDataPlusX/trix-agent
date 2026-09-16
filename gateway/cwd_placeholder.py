"""Resolve gateway ``terminal.cwd`` placeholder values to ``TERMINAL_CWD``.

When ``terminal.cwd`` is unset or a placeholder (``.``, ``auto``, ``cwd``),
the gateway must not blindly map host ``Path.home()`` into container backends.
Docker with workspace mounting still needs an explicit host path signal
(``MESSAGING_CWD`` or an absolute config path) for ``terminal_tool`` to map
``/host/project`` → ``/workspace``.
"""

from __future__ import annotations

# Одно объявление на весь движок живёт в ``hermes_constants``: этот модуль
# был «каноническим домом» списка только по названию, а копий было четыре и
# они расходились по регистру. Имя здесь оставлено — по нему импортируют
# ``gateway/run.py`` и тесты.
from hermes_constants import (  # noqa: E402
    CWD_PLACEHOLDER_WORDS as CWD_PLACEHOLDERS,
    is_cwd_placeholder,
)


def _truthy_env(value: str | None) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes"}


def placeholder_home_fallback() -> str:
    """Return the default cwd for a placeholder under the local backend.

    ``contained`` → ``<root>/workspace``, created on first use. That is the
    canonical workspace of the profile: a placeholder must not drop the agent
    into the operator's real home, which is the one directory profile data
    must never accumulate in.

    ``shared-host`` → ``Path.home()``, byte for byte what the gateway did
    before containment existed.
    """
    from pathlib import Path

    try:
        from hermes_constants import ensure_profile_workspace_dir, is_contained

        if is_contained():
            return str(ensure_profile_workspace_dir())
    except Exception:
        pass
    return str(Path.home())


def resolve_placeholder_terminal_cwd(
    *,
    configured_cwd: str,
    terminal_backend: str,
    messaging_cwd: str | None,
    docker_mount_cwd_to_workspace: bool,
    home_fallback: str,
) -> str | None:
    """Return the ``TERMINAL_CWD`` value to set, or ``None`` to leave it unset.

    Cases:
      - **local** + placeholder → ``MESSAGING_CWD`` or ``home_fallback``
      - **docker** + placeholder + mount on + host ``MESSAGING_CWD`` → host path
        (for ``terminal_tool`` ``/workspace`` mapping)
      - **docker** + placeholder + mount off → ``None`` (sandbox default)
      - other non-local backends + placeholder → ``None``
    """
    if configured_cwd and not is_cwd_placeholder(configured_cwd):
        return configured_cwd

    backend = (terminal_backend or "local").strip().lower()
    if backend == "local":
        messaging = (messaging_cwd or "").strip()
        return messaging or home_fallback

    if backend == "docker" and docker_mount_cwd_to_workspace:
        messaging = (messaging_cwd or "").strip()
        if messaging and not is_cwd_placeholder(messaging):
            return messaging

    return None
