"""Client-facing replies for disabled commands (Task 5 of the client-command-
surface plan; spec ``docs/product/specs/2026-09-01-trix-agent-client-command-
surface-design.md``, Ruling 5 and Ruling 6).

``hermes_cli.trix_menu.DISABLED_COMMANDS`` records, per command, a ``reason``
(for a reviewer -- never shown to the client) and a ``replacement`` +
``hint`` pair that decide what the client sees. This module turns that pair
into the actual Russian sentence, via three ``t()`` templates
(``trix.cmd.disabled.replace`` / ``.words`` / ``.none`` in
``locales/ru.yaml`` and ``locales/en.yaml``) -- one per
:data:`hermes_cli.trix_menu.DisabledCommand.hint` value. The gateway
dispatcher (a separate agent's work, per the client-command-surface plan's
Task 4) is expected to call :func:`disabled_command_reply` before branching
to a command's handler and, on a non-``None`` result, send that text back
instead of running anything.

Ruling 5: a disabled command is a *known* command answering with an
explanation, not silence and not ``trix.cmd.unknown`` (the "never heard of
this command" reply) -- the client typed something that used to work, or
still shows up in a stale cached Telegram menu, and the reply must read as
"this isn't here" rather than as the bot being broken.

Ruling 6: the reviewer-facing ``reason`` and the client-facing reply are
different strings on purpose. ``reason`` explains a *decision* in developer
language (toolset names, subsystem internals); nothing in this module ever
surfaces it.
"""

from __future__ import annotations

from agent.i18n import t
from hermes_cli.trix_menu import disabled_entry


def disabled_command_reply(name: str) -> str | None:
    """Return the client-facing Russian reply for a disabled command, or
    ``None`` if *name* (a command name or any of its registered aliases)
    isn't disabled.

    Resolves through :func:`hermes_cli.trix_menu.disabled_entry`, which
    already resolves aliases via ``hermes_cli.commands.resolve_command()`` --
    so ``disabled_command_reply("ctx")`` and ``disabled_command_reply(
    "context")`` return the identical text.
    """
    entry = disabled_entry(name)
    if entry is None:
        return None
    if entry.hint == "replace":
        return t("trix.cmd.disabled.replace", replacement=f"/{entry.replacement}")
    if entry.hint == "words":
        return t("trix.cmd.disabled.words")
    # "none" -- and the safe fallback for any value outside
    # VALID_DISABLED_HINTS, which a test in test_trix_menu.py already keeps
    # every DISABLED_COMMANDS entry within.
    return t("trix.cmd.disabled.none")
