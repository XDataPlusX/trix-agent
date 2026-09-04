"""Resolves which config.yaml/.env templates a fresh install should copy.

Trix ships a short, curated ``config.yaml`` template
(``assets/config/trix-config.yaml``, ~90 lines with Russian comments)
instead of upstream's ``cli-config.yaml.example`` (1700+ lines, upstream's
own kitchen-sink example), and a short, curated ``.env`` template
(``assets/config/trix.env.example``, ~45 lines / 3 required variables)
instead of upstream's ``.env.example`` (496 lines / 127 variables).
:func:`resolve_config_template` and :func:`resolve_env_template` are the
single, testable places that make these choices — ``scripts/install.sh``
(``copy_config_templates()``) calls them via a short Python subprocess
rather than duplicating the preference order as shell string logic.
"""

from pathlib import Path
from typing import Optional

_TRIX_TEMPLATE_RELATIVE = Path("assets") / "config" / "trix-config.yaml"
_UPSTREAM_TEMPLATE_RELATIVE = Path("cli-config.yaml.example")

_TRIX_ENV_TEMPLATE_RELATIVE = Path("assets") / "config" / "trix.env.example"
_UPSTREAM_ENV_TEMPLATE_RELATIVE = Path(".env.example")


def resolve_config_template(install_dir: Path) -> Optional[Path]:
    """Return the config.yaml template a fresh install should copy.

    Preference order:

    1. Trix's own curated template (``assets/config/trix-config.yaml``).
    2. Upstream's example (``cli-config.yaml.example``), if ours is absent.
    3. ``None`` if neither file exists under ``install_dir``.

    Args:
        install_dir: The Hermes/Trix install directory (the repo checkout
            root at install time — same directory ``INSTALL_DIR`` points at
            in ``scripts/install.sh``).

    Returns:
        The absolute path to the template to copy, or ``None``.
    """
    install_dir = Path(install_dir)

    trix_template = install_dir / _TRIX_TEMPLATE_RELATIVE
    if trix_template.is_file():
        return trix_template

    upstream_template = install_dir / _UPSTREAM_TEMPLATE_RELATIVE
    if upstream_template.is_file():
        return upstream_template

    return None


def resolve_trix_config_template_only(install_dir: Path) -> Optional[Path]:
    """Return Trix's own curated ``config.yaml`` template — NO upstream fallback.

    :func:`resolve_config_template` is the right choice for "copy into an
    empty place" (fresh install, a brand-new profile, ``doctor --fix``
    seeding a missing file): if our own curated template is absent,
    falling back to upstream's 1700-line kitchen-sink example is still
    strictly better than leaving the user with nothing.

    It is the WRONG choice for splicing new sections into an EXISTING,
    live client ``config.yaml``
    (:func:`hermes_cli.trix_config_sync.sync_missing_client_sections`):
    upstream's example carries English prose and sandbox-defeating keys
    (``docker_mount_cwd_to_workspace``, ``home_mode``, ...) that would get
    grafted straight into a client's file the moment our own curated
    template happens to be missing from the checkout. A confirmed repro
    showed 39 English blocks (115 -> 848 lines) landing in a client file
    this way. Splicing must use only the curated template, or nothing.

    Args:
        install_dir: The Hermes/Trix install directory (the repo checkout
            root — same directory :func:`resolve_config_template` takes).

    Returns:
        The absolute path to ``assets/config/trix-config.yaml``, or
        ``None`` if it isn't there.
    """
    install_dir = Path(install_dir)
    trix_template = install_dir / _TRIX_TEMPLATE_RELATIVE
    return trix_template if trix_template.is_file() else None


def resolve_env_template(install_dir: Path) -> Optional[Path]:
    """Return the .env template a fresh install should copy.

    Preference order:

    1. Trix's own curated template (``assets/config/trix.env.example``).
    2. Upstream's example (``.env.example``), if ours is absent.
    3. ``None`` if neither file exists under ``install_dir``.

    Args:
        install_dir: The Hermes/Trix install directory (the repo checkout
            root at install time — same directory ``INSTALL_DIR`` points at
            in ``scripts/install.sh``).

    Returns:
        The absolute path to the template to copy, or ``None``.
    """
    install_dir = Path(install_dir)

    trix_env_template = install_dir / _TRIX_ENV_TEMPLATE_RELATIVE
    if trix_env_template.is_file():
        return trix_env_template

    upstream_env_template = install_dir / _UPSTREAM_ENV_TEMPLATE_RELATIVE
    if upstream_env_template.is_file():
        return upstream_env_template

    return None
