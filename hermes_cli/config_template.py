"""Resolves which config.yaml/.env templates a fresh install should copy.

Trix ships a short, curated ``config.yaml`` template
(``assets/config/trix-config.yaml``, ~90 lines with Russian comments)
instead of upstream's ``cli-config.yaml.example`` (1700+ lines, upstream's
own kitchen-sink example), and its own ``.env`` template
(``assets/config/trix.env.example``: a curated Russian head with the 3
variables the customer must fill in, then every other variable the agent
reads, commented out) instead of upstream's ``.env.example`` (496 lines /
125 variables, and neither a superset nor a subset of ours — see
``scripts/build_trix_env.py`` for why both are read).
:func:`resolve_config_template` and :func:`resolve_env_template` are the
single, testable places that make these choices — ``scripts/install.sh``
(``copy_config_templates()``) calls them via a short Python subprocess
rather than duplicating the preference order as shell string logic.
"""

import re
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


def resolve_trix_env_template_only(install_dir: Path) -> Optional[Path]:
    """Наш курированный шаблон ``.env`` — БЕЗ апстримного фолбэка.

    Тот же расклад, что у :func:`resolve_trix_config_template_only`.
    :func:`resolve_env_template` годится для «скопировать в пустое место»: если
    нашего шаблона в сборке нет, апстримный пример всё же лучше, чем ничего.

    Он НЕ годится для дописывания в живой клиентский ``.env``
    (:func:`hermes_cli.trix_env_sync.sync_missing_env_documentation`).
    Апстримный ``.env.example`` — это 496 строк английского текста, и в нём
    ОДИННАДЦАТЬ ЖИВЫХ строк с поведенческими настройками
    (``TERMINAL_TIMEOUT``, ``BROWSER_*``, ``*_DEBUG``). Дописать их в файл
    клиента значило бы включить настройки, которых он не просил, — и нарушить
    правило проекта «в .env только секреты». Дописывать можно только наш
    шаблон, или ничего.
    """
    install_dir = Path(install_dir)
    trix_template = install_dir / _TRIX_ENV_TEMPLATE_RELATIVE
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


# ``KEY=``/``export KEY=`` with nothing (or only whitespace) after the ``=``.
# Deliberately narrow: a value that merely LOOKS empty (``KEY=""``,
# ``KEY= # заметка``) is a written-down choice and is left exactly as-is.
_EMPTY_ASSIGNMENT_RE = re.compile(r"^(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*$")


def comment_out_empty_assignments(text: str) -> str:
    """Comment out bare ``KEY=`` lines while keeping every other byte.

    In the client ``.env`` template a bare ``TELEGRAM_ALLOWED_USERS=`` is a
    BLANK TO FILL IN. In a profile's ``.env`` under
    ``gateway.multiplex_profiles`` it is something else entirely: the profile
    secret scope (``agent.secret_scope.build_profile_secret_scope``) parses it
    into ``{"TELEGRAM_ALLOWED_USERS": ""}``, and from there on the profile is
    indistinguishable from one whose operator deliberately configured an empty
    allowlist — ``get_secret`` hands back ``""`` instead of ``None``, so a
    caller that tests ``is None`` to decide whether the profile configured the
    key at all gets the wrong answer.

    Those two meanings must not share a spelling. Commenting the line keeps
    the template's guidance verbatim in the seeded file (the comment block
    above each variable survives, and the client's own instructions already
    say «уберите "#" в её начале»), while leaving the key genuinely unset
    until someone sets it.

    Used when seeding a NEW PROFILE's ``.env`` (``hermes profile create``).
    The root install's ``.env`` keeps the template verbatim: that is the file
    ``hermes setup`` walks the customer through, and the three required
    variables are meant to be sitting there uncommented and waiting.
    """
    out = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and _EMPTY_ASSIGNMENT_RE.match(stripped):
            out.append("# " + line.lstrip())
        else:
            out.append(line)
    return "".join(out)
