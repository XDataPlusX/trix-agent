"""Registry-owned slash command execution (thin slice).

Shared, surface-independent executors for informational slash commands.
``CommandDef.execute`` (hermes_cli/commands.py) names a key in
:data:`EXECUTORS`; each surface (CLI REPL, gateway, TUI slash worker via the
CLI) resolves that key through :func:`run_execute` and applies only its own
decoration (Rich markup, emoji/markdown, ``_telegramize_command_mentions``)
to the canonical :class:`CommandReply`.

Invariant: an executor's output depends only on ``ctx.args`` / ``ctx.options``
— never on ``ctx.surface`` — so the core text is identical across surfaces
for a fixed context (enforced by tests/hermes_cli/test_commands_execute.py).

Import discipline: this module imports nothing heavy at module level and
``hermes_cli.commands`` does NOT import this module (the ``execute`` field is
a plain string), so the gateway can keep importing ``commands.py`` without
prompt_toolkit and without cycles.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CommandContext",
    "CommandReply",
    "EXECUTORS",
    "execute_command",
    "resolve_executor",
    "run_execute",
]


# ---------------------------------------------------------------------------
# Context / reply dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandContext:
    """Surface-provided inputs for a shared command executor."""

    surface: str = "cli"                # "cli" | "gateway" | "tui" — decoration only
    args: str = ""                      # raw argument string after the command word
    options: Mapping[str, Any] = field(default_factory=dict)  # surface params (page_size, ...)
    config_get: Callable[[str, Any], Any] | None = None       # optional config accessor


@dataclass(frozen=True)
class CommandReply:
    """Canonical result of a shared executor.

    ``text`` is the surface-independent core text.  ``data`` carries the
    structured values the executor derived so a surface may re-render them
    with its own decoration (Rich columns, markdown bullets) without
    duplicating the computation.  ``format`` is a rendering hint only.
    """

    text: str
    data: Mapping[str, Any] = field(default_factory=dict)
    format: str = "plain"               # "plain" | "markdown" (hint, not a contract)


# ---------------------------------------------------------------------------
# Executors — pure formatters, no agent/session mutation
# ---------------------------------------------------------------------------

def _exec_version(ctx: CommandContext) -> CommandReply:
    """Core /version text — product name + version, no git-maintenance tail.

    The CLI startup banner (``hermes_cli.banner.format_banner_version_label``)
    keeps the ``· upstream ... (+N carried commits)`` tail; this reply does
    not — see ``format_version_reply_text`` for why.
    """
    from hermes_cli.banner import format_version_reply_text

    return CommandReply(format_version_reply_text())


def _exec_egress(ctx: CommandContext) -> CommandReply:
    """Core /egress text — Docker egress proxy status."""
    from hermes_cli.proxy_cli import format_status_text

    return CommandReply(format_status_text())


def _exec_disk(ctx: CommandContext) -> CommandReply:
    """Место на диске машины клиента; аргумент ``clean`` убирает служебное.

    Работа идёт в процессе, который владеет ``HERMES_HOME`` (шлюз или
    CLI), а не в песочнице агента: в контейнер смонтированы только
    рабочая папка, домашний каталог контейнера и кэш на чтение, сокета
    Docker там нет — этих файлов агент не видит и удалить их не может.
    Побочно это и безопаснее: уборка не проходит через модель.

    Показ идёт через ``cached_report``: полный обход стоит секунды, а
    человек ждёт ответа в чате. ``clean`` кэш сбрасывает сам, поэтому
    следующий показ уже посчитан заново.

    Всё, кроме точного ``clean``, — просьба показать: опечатка в
    аргументе не должна ничего сносить. Но и промолчать о ней нельзя —
    отчёт впереди говорит «убирать было нечего», и клиент, набравший
    «/disk cleen», перестанет ждать уборки (``UNRECOGNIZED_ARGUMENT``).
    """
    from hermes_cli.trix_disk import (
        UNRECOGNIZED_ARGUMENT,
        cached_report,
        clean,
        disk_thresholds,
        format_clean_result,
        format_report,
    )
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    args = (ctx.args or "").strip()
    if args.lower() == "clean":
        return CommandReply(format_clean_result(clean(home)))
    # Те же пороги, что и у почасовой проверки (корень ``disk`` в
    # config.yaml): отчёт по запросу и предупреждение по расписанию обязаны
    # называть «мало места» в одной и той же точке, иначе клиент прочтёт их
    # подряд и решит, что бот противоречит сам себе.
    thresholds = disk_thresholds()
    report = format_report(
        cached_report(home),
        warn_percent=thresholds.warn_percent,
        min_cleanup_bytes=thresholds.min_cleanup_bytes,
    )
    if args:
        return CommandReply(f"{UNRECOGNIZED_ARGUMENT}\n\n{report}")
    return CommandReply(report)


def _exec_profile(ctx: CommandContext) -> CommandReply:
    """Core /profile data — active profile name + home directory.

    A multiplexed gateway may pre-resolve the per-source profile/home and pass
    them via ``options`` (``profile_name`` / ``home_display``); otherwise the
    process-level values are used (identical to the old CLI + non-multiplex
    gateway behavior).
    """
    profile_name = str(ctx.options.get("profile_name") or "").strip()
    home_display = str(ctx.options.get("home_display") or "").strip()

    if not profile_name:
        from hermes_cli.profiles import get_active_profile_name

        profile_name = get_active_profile_name()
    if not home_display:
        from hermes_constants import display_hermes_home

        home_display = display_hermes_home()

    from agent.i18n import t

    return CommandReply(
        t("trix.profile.profile_label", profile=profile_name)
        + "\n"
        + t("trix.profile.home_label", home=home_display),
        data={"profile": profile_name, "home": home_display},
    )


def _exec_bundles(ctx: CommandContext) -> CommandReply:
    """Core /bundles data — installed skill bundles listing."""
    from agent.i18n import t

    try:
        from agent.skill_bundles import _bundles_dir, list_bundles
    except Exception as exc:  # pragma: no cover - env-specific
        return CommandReply(
            t("trix.bundles.unavailable", error=str(exc)),
            data={"error": str(exc)},
        )

    bundles = list_bundles()
    bundles_dir = str(_bundles_dir())
    if not bundles:
        return CommandReply(
            t("trix.bundles.none", dir=bundles_dir),
            data={"bundles": [], "dir": bundles_dir},
        )

    lines = [t("trix.bundles.header", count=len(bundles))]
    for info in bundles:
        skill_count = len(info.get("skills", []))
        desc = info.get("description") or t("trix.bundles.default_desc", count=skill_count)
        lines.append(t("trix.bundles.entry", slug=info["slug"], desc=desc, count=skill_count))
        for s in info.get("skills", []):
            lines.append(f"    · {s}")
    lines.append(t("trix.bundles.invoke_hint"))
    return CommandReply(
        "\n".join(lines),
        data={"bundles": bundles, "dir": bundles_dir},
    )


def _menu_curation_enabled() -> bool:
    """Whether Trix's client-command-surface curation is on right now.

    Reads ``platforms.telegram.extra.command_menu.curated`` the same way
    ``hermes_cli.commands.telegram_menu_commands()`` does: raw config, with
    a defensive ``{}`` fallback so a missing/unreadable config.yaml doesn't
    raise (curation defaults to on in that case --
    ``hermes_cli.trix_menu.menu_curation_enabled``'s own default).
    """
    from hermes_cli.trix_menu import menu_curation_enabled

    try:
        from hermes_cli.config import read_raw_config
        raw_cfg = read_raw_config() or {}
    except Exception:
        raw_cfg = {}
    return menu_curation_enabled(raw_cfg)


def _exec_help(ctx: CommandContext) -> CommandReply:
    """Core gateway /help body (pre platform mention decoration).

    Curated to Trix's client command surface by default (Task 2,
    ``docs/product/plans/2026-09-01-client-command-surface.md`` --
    Ruling 2 of the design spec: "/help показывает ровно клиентский
    слой"). ``gateway_help_lines()`` itself stays uncurated -- this is the
    one caller that passes ``only=client_surface_commands()``.

    Skill commands are no longer appended when curated: the spec's Ruling 3
    says the skill catalog itself isn't curated, so /help cannot show a
    curated *sample* of it either, and the old "...and N more -- see
    /commands" pointer would dangle once Task 3 makes /commands stop
    listing skills at all under curation.

    ``platforms.telegram.extra.command_menu.curated: false`` restores the
    exact pre-Task-2 behavior (full registry + up to 10 skill commands +
    the /commands pointer) as our own debugging escape hatch on a client
    machine -- see the global constraints in the plan.
    """
    from agent.i18n import t
    from hermes_cli.commands import gateway_help_lines

    curated = _menu_curation_enabled()
    only = None
    if curated:
        from hermes_cli.trix_menu import client_surface_commands
        only = client_surface_commands()

    lines = [
        t("gateway.help.header"),
        *gateway_help_lines(only=only),
    ]
    if not curated:
        try:
            from agent.skill_commands import get_skill_commands
            skill_cmds = get_skill_commands()
            if skill_cmds:
                lines.append(t("gateway.help.skill_header", count=len(skill_cmds)))
                # Show first 10, then point to /commands for the rest
                sorted_cmds = sorted(skill_cmds)
                for cmd in sorted_cmds[:10]:
                    lines.append(f"`{cmd}` — {skill_cmds[cmd]['description']}")
                if len(sorted_cmds) > 10:
                    lines.append(t("gateway.help.more_use_commands", count=len(sorted_cmds) - 10))
        except Exception:
            pass
    return CommandReply("\n".join(lines), format="markdown")


def _exec_commands(ctx: CommandContext) -> CommandReply:
    """Core gateway /commands body — paginated command + skill listing.

    ``ctx.options["page_size"]`` is a surface parameter (Telegram uses 15,
    everything else 20) — for a fixed context the text is surface-invariant.

    Curated to Trix's client command surface by default (Task 3,
    ``docs/product/plans/2026-09-01-client-command-surface.md`` -- Ruling 3
    of the design spec: "/commands перестаёт быть второй дверью"). Skill
    commands are never appended while curated -- not truncated, not shown
    on a later page, not shown at all -- because the skill catalog itself
    isn't curated yet (spec Ruling 3 / Task 3 body): showing the client a
    random sample of whatever skills happen to be installed is exactly the
    leak this task closes (``/hermes-agent-skill-authoring``,
    ``/claude-code``, ``/apple-notes``, …).

    ``platforms.telegram.extra.command_menu.curated: false`` restores the
    exact pre-Task-3 behavior (every gateway command + every skill command,
    paginated) as our own debugging escape hatch on a client machine.
    """
    from agent.i18n import t
    from hermes_cli.commands import gateway_help_lines

    raw_args = (ctx.args or "").strip()
    if raw_args:
        try:
            requested_page = int(raw_args)
        except ValueError:
            return CommandReply(t("gateway.commands.usage"), format="markdown")
    else:
        requested_page = 1

    curated = _menu_curation_enabled()
    only = None
    if curated:
        from hermes_cli.trix_menu import client_surface_commands
        only = client_surface_commands()

    # Build combined entry list: built-in commands + (uncurated-only) skills
    entries = list(gateway_help_lines(only=only))
    if not curated:
        try:
            from agent.skill_commands import get_skill_commands
            skill_cmds = get_skill_commands()
            if skill_cmds:
                entries.append("")
                entries.append(t("gateway.commands.skill_header"))
                for cmd in sorted(skill_cmds):
                    desc = skill_cmds[cmd].get("description", "").strip() or t("gateway.commands.default_desc")
                    entries.append(f"`{cmd}` — {desc}")
        except Exception:
            pass

    if not entries:
        return CommandReply(t("gateway.commands.none"), format="markdown")

    try:
        page_size = int(ctx.options.get("page_size", 20))
    except (TypeError, ValueError):
        page_size = 20
    page_size = max(1, page_size)
    total_pages = max(1, (len(entries) + page_size - 1) // page_size)
    page = max(1, min(requested_page, total_pages))
    start = (page - 1) * page_size
    page_entries = entries[start:start + page_size]

    lines = [
        t("gateway.commands.header", total=len(entries), page=page, total_pages=total_pages),
        "",
        *page_entries,
    ]
    if total_pages > 1:
        nav_parts = []
        if page > 1:
            nav_parts.append(t("gateway.commands.nav_prev", page=page - 1))
        if page < total_pages:
            nav_parts.append(t("gateway.commands.nav_next", page=page + 1))
        lines.extend(["", " | ".join(nav_parts)])
    if page != requested_page:
        lines.append(t("gateway.commands.out_of_range", requested=requested_page, page=page))
    return CommandReply("\n".join(lines), format="markdown")


# ---------------------------------------------------------------------------
# Registry + resolution
# ---------------------------------------------------------------------------

EXECUTORS: dict[str, Callable[[CommandContext], CommandReply]] = {
    "version": _exec_version,
    "disk": _exec_disk,
    "egress": _exec_egress,
    "profile": _exec_profile,
    "bundles": _exec_bundles,
    "gateway_help": _exec_help,
    "gateway_commands": _exec_commands,
}


def resolve_executor(cmd_def: Any) -> Callable[[CommandContext], CommandReply] | None:
    """Return the shared executor for ``cmd_def`` (or None when not migrated)."""
    key = getattr(cmd_def, "execute", None)
    if not key:
        return None
    return EXECUTORS.get(key)


def run_execute(cmd_def: Any, ctx: CommandContext) -> CommandReply | None:
    """Run ``cmd_def``'s registry-owned executor, if any."""
    fn = resolve_executor(cmd_def)
    if fn is None:
        return None
    return fn(ctx)


def execute_command(name: str, ctx: CommandContext) -> CommandReply:
    """Run the shared executor for the command named ``name``.

    Raises ``LookupError`` when the command is unknown or not migrated —
    call sites use this only for commands they know carry ``execute``.
    """
    from hermes_cli.commands import resolve_command

    cmd_def = resolve_command(name)
    reply = run_execute(cmd_def, ctx) if cmd_def is not None else None
    if reply is None:
        raise LookupError(f"no registry-owned executor for /{name}")
    return reply
