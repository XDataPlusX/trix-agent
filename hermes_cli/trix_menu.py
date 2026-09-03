"""Defines Trix's client command surface and curates the Telegram `/` menu.

Every built-in gateway command belongs to exactly one of three layers (spec
``docs/product/specs/2026-09-01-trix-agent-client-command-surface-design.md``
§5, plan ``docs/product/plans/2026-09-01-client-command-surface.md`` Task 1):

- ``CLIENT_MENU_COMMANDS`` -- shown in the Telegram `/` menu, listed in
  ``/help``, dispatchable. The client's whole command surface.
- ``SERVICE_COMMANDS`` -- dispatchable, but never shown in the menu or in
  ``/help``. ``/start`` (the platform's own ping -- without it the bot could
  never message the client first) and ``/sethome`` (an escape hatch for a
  misconfigured home chat) live here.
- ``DISABLED_COMMANDS`` -- NOT dispatchable. Typing one of these is meant to
  produce a Russian explanation (naming a replacement command where one
  exists) instead of running the old handler or, worse, falling through to
  the model as a plain message. This is a genuine change from the module's
  earlier shape: "hidden from the menu" used to mean nothing more than
  "still runs when typed, still in /help" for every non-menu command (that
  was ``HIDDEN_REASONS``). It no longer does -- gating *execution*, not just
  *menu discovery*, is exactly what this layer exists to do.

This is a whitelist, not a blocklist: a command absent from all three dicts
is a bug in this module, not an implicit fourth state (Ruling 1 in the
spec) -- ``tests/hermes_cli/test_trix_menu.py`` enforces that every
gateway-known command resolves into exactly one layer.

``is_disabled_in_gateway()`` and ``disabled_entry()`` are this module's
contract for the dispatcher: callers are expected to consult them before
branching to a command's handler, resolving through
``hermes_cli.commands.resolve_command()`` first so an alias
(``/reset`` for ``/new``, ``/ctx`` for ``/context``) can't dodge a
disablement recorded under the canonical name. ``/help`` and ``/commands``
are expected to read ``client_surface_commands()`` the same way
``telegram_menu_commands()`` already reads ``CLIENT_MENU_COMMANDS`` via
``filter_client_menu()``. Wiring each of those call sites is incremental
work tracked by later tasks in the plan above -- this module is the single
source of truth they all converge on, not "the Telegram menu's private
filter" the way it was before this layer existed.

``curated`` defaults to ``True``. Setting
``platforms.telegram.extra.command_menu.curated: false`` in ``config.yaml``
turns curation off and is meant to restore the full built-in surface,
including execution of otherwise-disabled commands -- our own debugging
escape hatch on a client machine. ``menu_curation_enabled()`` only answers
"is curation on"; a caller that also wants "and therefore disabled commands
run too" combines that with ``is_disabled_in_gateway()`` itself, since this
module has no access to config from inside a pure name lookup.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from utils import is_truthy_value

# The 33 commands shown in Telegram's `/` menu, IN THE ORDER Telegram lists
# them.  This is a whitelist, not a filter expression -- do not alphabetize
# or otherwise reorder it; the order here is the menu order the client sees,
# and it is grouped by what the commands do (flow control, price/model
# management, automations, ...) rather than sorted alphabetically. Names are
# canonical CommandDef.name values (no leading slash, no aliases); none of
# them contain a hyphen so they match Telegram's sanitized names
# (``_sanitize_telegram_name``) unchanged.
CLIENT_MENU_COMMANDS: tuple[str, ...] = (
    "help",
    "new",
    "stop",
    "steer",
    "queue",
    "background",
    "status",
    "sessions",
    "resume",
    "title",
    "retry",
    "undo",
    "compress",
    "memory",
    "model",
    "fast",
    "reasoning",
    "usage",
    "disk",
    "voice",
    "agents",
    "approve",
    "deny",
    "goal",
    "subgoal",
    "heartbeat",
    "blueprint",
    "commands",
    "restart",
    "setup",
    "update",
    "version",
    "debug",
)

# Commands that execute but are deliberately absent from the menu and from
# /help. Distinct from CLIENT_MENU_COMMANDS (shown + runs) and from
# DISABLED_COMMANDS (hidden + does NOT run): these run, just quietly.
#
# Exactly two, both operational, not client-facing features:
# - "start": Telegram's own ping when the client taps "Start" -- there is
#   nothing to describe in a menu, and it must keep dispatching or the bot
#   can never message the client first (disk-space warnings, monthly
#   summaries all depend on this).
# - "sethome": an escape hatch for a misconfigured home chat. The wizard
#   sets the home chat automatically; this command exists only for the rare
#   case that needs correcting, and config.yaml isn't reachable by the
#   client to fix it another way.
SERVICE_COMMANDS: dict[str, str] = {
    "start": "служебный ответ на пинг платформы, описание клиенту ничего не говорит",
    "sethome": (
        "домашний чат задаётся мастером настройки автоматически и всегда "
        "личный — единственное, что даёт клиенту эта команда, это "
        "возможность увести уведомления туда, куда мы их сознательно не "
        "шлём (рабочая тема группы). Команда продолжает работать при "
        "наборе вручную: ручка на случай неверного адреса, а config.yaml "
        "клиенту недоступен"
    ),
}


@dataclass(frozen=True)
class DisabledCommand:
    """Record for one entry in :data:`DISABLED_COMMANDS`.

    ``reason`` is for us -- a reviewer reading this file six months from
    now needs to know why the product chose to disable the command, not
    what the command does (its docstring/description already says that).

    ``replacement`` and ``hint`` together decide what the client sees when
    they type the disabled command (Part C of the spec, wired in Task 5):

    - ``hint="replace"`` -- there IS a command in ``CLIENT_MENU_COMMANDS``
      that covers the same need. ``replacement`` names it, and the reply
      tells the client to use it instead (e.g. "/context" -> "/status").
    - ``hint="words"`` -- no command replaces this one, but the same result
      is reachable by asking the agent in plain language (e.g. "/learn" --
      the agent saves lessons on request without a dedicated command).
      ``replacement`` is ``None``: there's nothing to name.
    - ``hint="none"`` -- the capability itself isn't in this build (a
      broken subsystem, a disabled toolset, a deliberately withheld
      escape hatch). ``replacement`` is ``None``, and the reply does not
      promise the feature will show up later.

    Three templates, not two: the plan anticipated a binary "replacement
    command" / "no replacement" split, but seven commands (bundles, skills,
    init, diff, learn, personality, refine) fall into the third case --
    reachable by an ordinary request, not by any command at all -- which
    neither template expresses honestly.
    """

    reason: str
    replacement: str | None
    hint: str


# Valid values for DisabledCommand.hint (see the dataclass docstring above).
VALID_DISABLED_HINTS: frozenset[str] = frozenset({"replace", "words", "none"})

# The 29 commands that resolve in the registry, are gateway-known, and do
# NOT execute: typing one is meant to answer with a Russian explanation
# (Part C of the spec) instead of running the old handler or falling
# through to the model as an ordinary message. See DisabledCommand's
# docstring for what `reason` / `replacement` / `hint` each mean.
#
# `reason` explains the DECISION (why we chose to disable it), not what the
# command does -- restating the command's own description tells a reviewer
# nothing six months from now.
DISABLED_COMMANDS: dict[str, DisabledCommand] = {
    # --- сломаны или обещают то, чего нет ---------------------------------
    "curator": DisabledCommand(
        "обработчика в шлюзе нет вовсе — текст уходил модели как реплика; "
        "сама подсистема ухода за навыками работает и человеку не адресована",
        None, "none",
    ),
    "platform": DisabledCommand(
        "pause/resume не исполнялись никогда (аргумент читается из несуществующего "
        "поля); апстримная правка на одно слово превратит команду в способ запереть "
        "себя снаружи единственного канала",
        None, "none",
    ),
    "rollback": DisabledCommand(
        "контрольные точки не работают с песочницей: снимок не создаётся, список "
        "всегда пуст. Замены НЕТ и словами тоже нет — обещать откат нельзя ни в "
        "каком виде",
        None, "none",
    ),
    "verbose": DisabledCommand(
        "конфиг-гейт закрыт в поставке, а ответ отправляет править config.yaml — "
        "тупик, который клиенту не разрешить",
        None, "none",
    ),
    "codex-runtime": DisabledCommand(
        "рантайм чужого провайдера; ответ диктует установку npm-пакета, которую "
        "клиенту негде выполнить",
        None, "none",
    ),
    "moa": DisabledCommand(
        "пресет есть, но собран из провайдеров, ключей к которым у клиента нет: "
        "команда молча уходит в реальный вызов и падает, потратив ход",
        "model", "replace",
    ),
    "insights": DisabledCommand(
        "печатает клиенту «Hermes Insights» — бренд апстрима; сценарий закрыт /usage",
        "usage", "replace",
    ),
    "context": DisabledCommand(
        "весь блок ответа английский; сценарий полностью закрыт /status",
        "status", "replace",
    ),
    "suggestions": DisabledCommand(
        "недособранная функция: показывает черновики автоматизаций до того, как "
        "агент успел их предложить; готовые автоматизации живут в /blueprint",
        "blueprint", "replace",
    ),
    "kanban": DisabledCommand(
        "доска для нескольких агентов; тулсет выключен, справка английская. "
        "«Что выполняется сейчас» закрывает /agents, саму доску — нет",
        "agents", "replace",
    ),
    "bundles": DisabledCommand(
        "наборов навыков в поставке нет, а ответ диктует команду терминала",
        None, "words",
    ),
    "skills": DisabledCommand(
        "конфиг-гейт закрыт; навыками управляет сам агент по просьбе клиента",
        None, "words",
    ),

    # --- работают, но клиенту бессодержательны ----------------------------
    "egress": DisabledCommand(
        "прокси выключен дефолтом и в шаблоне не включается — отчёт всегда "
        "одинаков, диагностировать нечего",
        None, "none",
    ),
    "whoami": DisabledCommand(
        "пользователь один и он же владелец: ответ всегда «полный доступ». "
        "Замена слабая, но честнее пустоты",
        "help", "replace",
    ),
    "profile": DisabledCommand(
        "профиль один; путь к каталогу на сервере клиенту ни о чём не говорит",
        None, "none",
    ),
    "reload-skills": DisabledCommand(
        "перечитывает каталог, изменённый снаружи; агент свои навыки регистрирует "
        "сам, снаружи файлы меняем только мы",
        None, "none",
    ),
    "approvals": DisabledCommand(
        "охраняет ровно одно — удаление в рабочей папке, где нет отката. off "
        "снимает эту защиту, smart отдаёт решение другой модели; ни один вариант "
        "клиенту не помогает. Ответ на конкретный запрос остаётся за /approve и /deny",
        "approve", "replace",
    ),

    # --- снимают защиту или меняют систему молча --------------------------
    "yolo": DisabledCommand(
        "снимает подтверждение опасных команд одной командой, без второго вопроса; "
        "спека 9 оставила это подтверждение осознанно",
        None, "none",
    ),
    "footer": DisabledCommand(
        "без аргумента молча переключает настройку и пишет её в config.yaml — то "
        "есть меняет поведение продукта от попытки посмотреть, что стоит",
        "status", "replace",
    ),
    "init": DisabledCommand(
        "без подтверждения запускает проход агента по файловой системе и пишет "
        "AGENTS.md; промпт английский, ответ модели может съехать на английский",
        None, "words",
    ),

    # --- сценарий разработчика --------------------------------------------
    "branch": DisabledCommand(
        "ветвление сеанса — сценарий разработчика; плодит сеансы в базе без "
        "предупреждения. /new не замена (он обнуляет), но ближайшее, что есть",
        "new", "replace",
    ),
    "diff": DisabledCommand(
        "git-изменения; на каталоге клиента вернёт английское «Not a git repository»",
        None, "words",
    ),
    "learn": DisabledCommand(
        "команда — заготовленная фраза для агента, а не механизм: тот же результат "
        "даёт обычная просьба",
        None, "words",
    ),
    "personality": DisabledCommand(
        "готовые личности (пират, катгёрл) в деловом продукте",
        None, "words",
    ),
    "refine": DisabledCommand(
        "как и /learn — заготовленная фраза; агент сохраняет выводы и по обычной "
        "просьбе",
        None, "words",
    ),
    "topic": DisabledCommand(
        "темы Telegram требуют объяснить сначала топики платформы, потом наши "
        "сеансы поверх них; параллельные разговоры закрывают /sessions и /resume",
        "sessions", "replace",
    ),
    "reload-mcp": DisabledCommand(
        "MCP-серверов в поставке нет; ответ отправляет править config.yaml",
        None, "none",
    ),
    "pause": DisabledCommand(
        "останавливает ВСЮ работу шлюза, включая расписания, до /pause off — "
        "клиент забудет и решит, что бот сломался; его сценарий закрывает /stop",
        "stop", "replace",
    ),
    "topup": DisabledCommand(
        "баланс и оплата на стороне провайдера клиента, агент ими не управляет; "
        "расход показывает /usage",
        "usage", "replace",
    ),
}


def menu_curation_enabled(raw_config: Mapping[str, Any] | None) -> bool:
    """Return whether the Telegram `/` menu should be curated down to the
    client list.

    Reads ``platforms.telegram.extra.command_menu.curated`` from *raw_config*
    (the same raw, unmerged dict ``hermes_cli.config.read_raw_config()``
    returns).  Defaults to ``True`` when the key, any parent, or the whole
    config is absent -- curation is Trix's default posture, not an opt-in.
    """
    node: Any = raw_config if isinstance(raw_config, Mapping) else {}
    for key in ("platforms", "telegram", "extra", "command_menu"):
        if not isinstance(node, Mapping):
            return True
        node = node.get(key)
    if not isinstance(node, Mapping):
        return True
    return is_truthy_value(node.get("curated"), default=True)


def filter_client_menu(commands: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Filter and reorder *commands* down to :data:`CLIENT_MENU_COMMANDS`.

    *commands* is the ``(sanitized_name, description)`` pairs produced by
    :func:`hermes_cli.commands.telegram_bot_commands`.  The result contains
    exactly the entries whose name is in ``CLIENT_MENU_COMMANDS``, in
    ``CLIENT_MENU_COMMANDS`` order -- this function defines the menu order,
    superseding the priority-list reordering
    (``_prioritize_telegram_menu_commands``) that still governs the
    uncurated (full) menu.

    A name in ``CLIENT_MENU_COMMANDS`` that is missing from *commands* (e.g.
    a command definition was removed upstream) is silently skipped rather
    than invented -- ``tests/hermes_cli/test_trix_menu.py`` separately
    asserts every name resolves, so this is a defensive fallback, not the
    primary guarantee.
    """
    by_name = {name: (name, description) for name, description in commands}
    return [by_name[name] for name in CLIENT_MENU_COMMANDS if name in by_name]


def client_surface_commands() -> list[str]:
    """Return the client command surface's names, in menu order.

    A thin, stable accessor over :data:`CLIENT_MENU_COMMANDS` for consumers
    outside this module (``/help``, ``/commands``) that want the same list
    ``telegram_menu_commands()`` uses without importing the constant
    directly.
    """
    return list(CLIENT_MENU_COMMANDS)


def is_disabled_in_gateway(name: str | None) -> bool:
    """Return whether *name* is disabled (see :data:`DISABLED_COMMANDS`).

    Accepts a canonical command name OR any of its registered aliases --
    resolved through ``hermes_cli.commands.resolve_command()`` first, so
    disabling ``context`` can't be dodged by typing its alias ``ctx``, and
    disabling ``pause`` can't be dodged by typing a quick-alias for it.
    Imported lazily to avoid a module-level import cycle with
    ``hermes_cli.commands`` (which already imports this module lazily, for
    the same reason, inside ``telegram_menu_commands()``).
    """
    if not name:
        return False
    from hermes_cli.commands import resolve_command

    cmd = resolve_command(name)
    if cmd is None:
        return False
    return cmd.name in DISABLED_COMMANDS


def disabled_entry(name: str | None) -> DisabledCommand | None:
    """Return the :class:`DisabledCommand` record for *name*, or ``None``.

    Same alias-resolving behavior as :func:`is_disabled_in_gateway` -- the
    two are meant to be used together (a truthy ``is_disabled_in_gateway``
    result followed by ``disabled_entry`` to build the client-facing reply).
    """
    if not name:
        return None
    from hermes_cli.commands import resolve_command

    cmd = resolve_command(name)
    if cmd is None:
        return None
    return DISABLED_COMMANDS.get(cmd.name)
