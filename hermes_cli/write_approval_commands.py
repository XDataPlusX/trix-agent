#!/usr/bin/env python3
"""Shared handlers for the /memory and /skills write-approval subcommands.

Both the interactive CLI (``cli.py``) and the gateway (``gateway/run.py``) call
into this module so the pending-review UX (list / approve / reject / diff /
mode) lives in one place. Each caller owns only its surface concerns:
formatting the returned text and, for the gateway, persisting config + evicting
the cached agent on a mode change.

Every public handler returns a plain text string suitable for both a terminal
and a chat message. Skill diffs are intentionally NOT inlined here — the
``diff`` handler returns the full diff for the CLI pager, but on a messaging
platform the gateway truncates it and points the user at the dashboard / file.

Text goes through :func:`agent.i18n.t` under the ``trix.cmd.write_approval.*``
namespace so it renders in the caller's active language instead of a hardcoded
English literal (this module previously bypassed ``t()`` entirely — see
``docs/product/plans/2026-09-01-client-command-surface.md`` Task 7 step 4).
"""

from __future__ import annotations

import json
from typing import List, Optional

from agent.i18n import t
from tools import write_approval as wa

# Localized noun for each subsystem, spliced into the generic templates below.
_LABEL_KEYS = {
    wa.MEMORY: "trix.cmd.write_approval.label_memory",
    wa.SKILLS: "trix.cmd.write_approval.label_skills",
}


def _label(subsystem: str) -> str:
    key = _LABEL_KEYS.get(subsystem, "trix.cmd.write_approval.label_memory")
    return t(key)


def _fmt_state(subsystem: str) -> str:
    on = wa.write_approval_enabled(subsystem)
    state = t("trix.cmd.write_approval.state_on") if on else t("trix.cmd.write_approval.state_off")
    return t("trix.cmd.write_approval.state", label=_label(subsystem), state=state)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_pending_list(subsystem: str) -> str:
    records = wa.list_pending(subsystem)
    if not records:
        return t("trix.cmd.write_approval.no_pending", label=_label(subsystem))
    lines = [t("trix.cmd.write_approval.pending_header", label=_label(subsystem), count=len(records))]
    for r in records:
        origin = r.get("origin", "foreground")
        tag = t("trix.cmd.write_approval.auto_tag") if origin == "background_review" else ""
        lines.append(f"  {r['id']}{tag}  {r.get('summary', '')}")
    lines.append("")
    lines.append(t("trix.cmd.write_approval.apply_reject_hint", subsystem=subsystem))
    if subsystem == wa.SKILLS:
        lines.append(t("trix.cmd.write_approval.diff_hint"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcommand dispatch
# ---------------------------------------------------------------------------

def handle_pending_subcommand(
    subsystem: str,
    args: List[str],
    *,
    memory_store=None,
    set_mode_fn=None,
) -> Optional[str]:
    """Dispatch a /memory or /skills subcommand.

    Args:
        subsystem: ``memory`` or ``skills``.
        args: tokens after the slash command (e.g. ``["approve", "a1b2"]``).
        memory_store: live MemoryStore for applying approved memory writes
            (CLI passes ``self.agent._memory_store``; gateway applies against a
            freshly loaded store).
        set_mode_fn: optional callable ``(enabled: bool) -> None`` that
            persists the new write_approval boolean to config (gateway provides
            this; CLI uses its own ``save_config_value`` and passes a closure).

    Returns a text string to show the user. Returns None when the args are not
    a write-approval subcommand (caller falls through to its other handling,
    e.g. /skills search).
    """
    if not args:
        # Bare /memory or /skills with no sub → show pending + gate state.
        return f"{_fmt_state(subsystem)}\n\n" + _fmt_pending_list(subsystem)

    sub = args[0].lower()
    rest = args[1:]

    if sub == "pending":
        return _fmt_pending_list(subsystem)

    if sub in {"approve", "apply"}:
        return _approve(subsystem, rest, memory_store)

    if sub in {"reject", "deny", "drop"}:
        return _reject(subsystem, rest)

    if sub == "diff" and subsystem == wa.SKILLS:
        return _diff(rest)

    if sub in {"approval", "mode"}:  # 'mode' kept as a back-compat alias
        return _set_approval(subsystem, rest, set_mode_fn)

    return None  # not ours — caller handles


def _resolve_one(subsystem: str, rest: List[str]):
    if not rest:
        return None, t("trix.cmd.write_approval.usage_approve_reject", subsystem=subsystem)
    return rest[0], None


def _approve(subsystem: str, rest: List[str], memory_store) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or t("trix.cmd.write_approval.usage_approve", subsystem=subsystem)

    records = wa.list_pending(subsystem)
    if not records:
        return t("trix.cmd.write_approval.no_pending", label=_label(subsystem))

    if target.lower() == "all":
        targets = list(records)
    else:
        rec = wa.get_pending(subsystem, target)
        if not rec:
            return t("trix.cmd.write_approval.not_found", label=_label(subsystem), id=target)
        targets = [rec]

    applied, failed = 0, []
    for rec in targets:
        ok, msg = _apply_one(subsystem, rec, memory_store)
        if ok:
            wa.discard_pending(subsystem, rec["id"])
            applied += 1
        else:
            failed.append(f"{rec['id']}: {msg}")

    out = [t("trix.cmd.write_approval.approved", count=applied, label=_label(subsystem))]
    if failed:
        out.append(t("trix.cmd.write_approval.failed_header"))
        out.extend(f"  {f}" for f in failed)
    return "\n".join(out)


def _apply_one(subsystem: str, rec, memory_store):
    payload = rec.get("payload", {})
    try:
        if subsystem == wa.MEMORY:
            if memory_store is None:
                return False, "memory store unavailable"
            from tools.memory_tool import apply_memory_pending
            result = apply_memory_pending(payload, memory_store)
            return bool(result.get("success")), result.get("error", "")
        else:
            from tools.skill_manager_tool import apply_skill_pending
            result = json.loads(apply_skill_pending(payload))
            return bool(result.get("success")), result.get("error", "")
    except Exception as e:
        return False, str(e)


def _reject(subsystem: str, rest: List[str]) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or t("trix.cmd.write_approval.usage_reject", subsystem=subsystem)
    if target.lower() == "all":
        n = 0
        for rec in wa.list_pending(subsystem):
            if wa.discard_pending(subsystem, rec["id"]):
                n += 1
        return t("trix.cmd.write_approval.rejected", count=n, label=_label(subsystem))
    if wa.discard_pending(subsystem, target):
        return t("trix.cmd.write_approval.rejected_one", label=_label(subsystem), id=target)
    return t("trix.cmd.write_approval.not_found", label=_label(subsystem), id=target)


def _diff(rest: List[str]) -> str:
    if not rest:
        return t("trix.cmd.write_approval.usage_diff")
    rec = wa.get_pending(wa.SKILLS, rest[0])
    if not rec:
        return t("trix.cmd.write_approval.not_found", label=_label(wa.SKILLS), id=rest[0])
    diff = wa.skill_pending_diff(rec)
    header = t("trix.cmd.write_approval.diff_header", id=rec["id"], summary=rec.get("summary", ""))
    return header + "\n" + diff


def _set_approval(subsystem: str, rest: List[str], set_mode_fn) -> str:
    """Turn the approval gate on/off for a subsystem.

    ``set_mode_fn`` (when provided) persists the new boolean to config.
    """
    if not rest:
        return (f"{_fmt_state(subsystem)}\n"
                + t("trix.cmd.write_approval.set_hint", subsystem=subsystem))
    arg = rest[0].strip().lower()
    truthy = {"on", "true", "yes", "1", "enable", "enabled"}
    falsey = {"off", "false", "no", "0", "disable", "disabled"}
    if arg in truthy:
        enabled = True
    elif arg in falsey:
        enabled = False
    else:
        return t("trix.cmd.write_approval.invalid_value", value=arg)
    if set_mode_fn is None:
        # CLI-only fallback (the gateway always supplies set_mode_fn) — the
        # client never reaches this branch, so pointing at a terminal command
        # here is fine.
        val = "true" if enabled else "false"
        return t("trix.cmd.write_approval.cli_only_hint", subsystem=subsystem, value=val)
    try:
        set_mode_fn(enabled)
    except Exception as e:
        return t("trix.cmd.write_approval.set_failed", label=_label(subsystem), error=e)
    state = t("trix.cmd.write_approval.state_on") if enabled else t("trix.cmd.write_approval.state_off")
    return t("trix.cmd.write_approval.set_ok", label=_label(subsystem), state=state)
