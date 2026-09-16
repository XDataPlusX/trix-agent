"""Machine-readable verdict for `hermes doctor`.

Lives here, not in `doctor.py`/`main.py`, for the same reason as
`trix_status.py`: our logic stays in its own module and the upstream file
gets a one-line import at the call site. `doctor.py` is ~3300 lines we
regularly pull down from upstream; every function of ours added directly
inside it is paid for in merge conflicts every time.

**The problem this closes.** `hermes doctor` always exits 0 — the only
`sys.exit()` calls inside `run_doctor()` are on an invalid `--ack` id, not
on the diagnostic outcome itself. That makes it useless as a gate: neither
the install script nor the setup-wizard support page can tell "fixed" from
"still broken" (`docs/product/PROMPT-spec15-support-page.md`, blocking
dependency #2). `run_doctor()` already computes everything needed to
answer that — the local `issues`, `manual_issues` and `fixed_count` — it
just never returned it.

**Design.** `run_doctor()` now returns a `DoctorRunResult` built from
those three already-computed values (see the one-line change at the end
of `run_doctor()` in `doctor.py` — no check's logic or printed string was
touched). This module turns that result into a verdict two ways:

- `run_doctor_with_verdict()` — the orchestrator `hermes_cli.main.cmd_doctor`
  calls. Without `--json`/`--exit-code` it is a byte-for-byte passthrough
  to the old behavior (calls `run_doctor(args)`, returns `None`, process
  exits 0 exactly as before). This preserves every existing caller,
  including `hermes_cli/console_engine.py`'s `_doctor()`, which invokes
  `run_doctor()` directly (not through `cmd_doctor`) and inspects the
  return value itself (`_capture_output` raises `SystemExit` only when the
  callable returns a *truthy int* — a `DoctorRunResult` instance is never
  an `int`, so that branch can never fire; `_doctor()` is unaffected by
  this change even though it never gains the new flags).
- `verdict_json()` / `doctor_exit_code()` — the two facts a consumer
  (install script, setup-wizard support page) actually needs: did
  anything need attention, and what's left.

**Why `--json` redirects doctor's own printing to stderr rather than
rewriting it.** `run_doctor()` reports through ~100 direct `print()` call
sites scattered across every individual check, not through a buffer or a
return value — rewiring each one to route through structured data instead
of printing is a large, invasive change this task does not ask for and
that would fight the "don't touch a single printed string" constraint.
Redirecting stdout to stderr for the duration of the run — the same
`contextlib.redirect_stdout` trick `console_engine.py`'s `_capture_output`
already uses elsewhere in this codebase — gets a clean separation for
free: stdout carries *only* the JSON verdict (safe for
`result=$(hermes doctor --json)`), while the human-readable diagnostic
text is still visible on a terminal (a shell's `$(...)` capture does not
swallow stderr) so `hermes doctor --json` stays useful when a person runs
it directly, not only when a script parses it.
"""

from __future__ import annotations

import contextlib
import json
import sys
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class DoctorRunResult:
    """What one `run_doctor()` pass found.

    Built by `doctor.py` from its own `issues`/`manual_issues`/
    `fixed_count` locals (see the `return` at the end of `run_doctor()`);
    consumed here. `issues` are checks `run_doctor()` flagged as auto-
    fixable; `manual_issues` need a human. Both are already
    human-readable strings — `run_doctor()` built them for its own
    printed report, and that same text is what a verdict consumer sees.

    ``advisories`` is a third bucket, and the reason it exists is a defect
    a client hit on a live machine 2026-09-04. It carries findings that are
    **true, but not this machine's to answer** — today, `npm audit`
    advisories in build-time tooling. They come from packages we ship, so
    they are byte-identical on every client VM; `doctor --fix` does not run
    `npm audit fix` at all, and for the workspace-scoped ones this file
    itself refuses to even print a manual command (npm's arborist crashes
    on that tree). Nothing on the client's machine can clear them, ever.

    While they sat in ``issues``, they made ``ok`` False on every healthy
    machine — and ``ok`` is what the support pass turns into the ONE
    sentence a client reads. Every client finished a perfectly successful
    setup and was told «часть неполадок исправить самостоятельно не
    удалось… напишите в поддержку», over three lines about esbuild.

    So the rule this bucket encodes: **``issues``/``manual_issues`` are for
    things this machine can act on; ``advisories`` are for things we can.**
    They are still printed in full by ``run_doctor()`` and still carried in
    ``verdict_json()`` — nothing is hidden from us. Our own supply-chain
    posture is covered where it belongs: `hermes security` (OSV), the
    dependency-pinning policy in CLAUDE.md, and CI.
    """

    issues: list = field(default_factory=list)
    manual_issues: list = field(default_factory=list)
    fixed_count: int = 0
    advisories: list = field(default_factory=list)

    @property
    def remaining_issues(self) -> list:
        """Everything still outstanding after this run, auto-fixable or not.

        Deliberately excludes ``advisories`` — see the field's own note in
        ``run_doctor()`` and the docstring below.
        """
        return [*self.issues, *self.manual_issues]

    @property
    def ok(self) -> bool:
        return not self.remaining_issues


def verdict_json(result: DoctorRunResult) -> str:
    """Serialize a `DoctorRunResult` to the machine-readable verdict.

    A consumer (install script, setup-wizard support page) needs exactly
    three facts: how much got fixed automatically, what is still
    outstanding, and the bottom line — so that's all this carries.
    """
    payload = {
        "verdict": "ok" if result.ok else "needs_attention",
        "ok": result.ok,
        "fixed_count": result.fixed_count,
        "remaining_issues": result.remaining_issues,
        # Reported, never counted — see DoctorRunResult.advisories.
        "advisories": result.advisories,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def doctor_exit_code(result: DoctorRunResult) -> int:
    """0 when nothing remains unresolved after this run, 1 otherwise."""
    return 0 if result.ok else 1


def run_doctor_with_verdict(run_doctor_fn: Callable[[object], object], args: object) -> int | None:
    """Adapt `run_doctor_fn`'s structural result into the process contract.

    Returns `None` when the caller (`hermes_cli.main`'s `rc = args.func(args)`
    dispatch) should leave the exit code untouched (i.e. 0), or an `int`
    exit code when `--exit-code` was passed. See the module docstring for
    why `--json` alone does not force a nonzero exit and for the
    stdout/stderr split rationale.

    `getattr(..., False)` (not `args.json`/`args.exit_code`) because
    `run_doctor()` is also invoked with a bare `SimpleNamespace(fix=False,
    ack=None)` that predates these flags (`console_engine.py`) — that call
    site does not go through this function at all, but this function is
    written to tolerate the same kind of minimal namespace if ever reused
    that way.
    """
    json_flag = bool(getattr(args, "json", False))
    exit_flag = bool(getattr(args, "exit_code", False))

    if not json_flag and not exit_flag:
        # Exactly the old cmd_doctor() body: call it, ignore the return,
        # exit 0. No behavior change of any kind without the new flags.
        run_doctor_fn(args)
        return None

    if json_flag:
        with contextlib.redirect_stdout(sys.stderr):
            result = run_doctor_fn(args)
    else:
        result = run_doctor_fn(args)

    # `--ack <id>` short-circuits run_doctor() before it builds a result
    # (it sys.exit()s on failure, plain-returns None on success) — nothing
    # to report either way, so leave the exit code alone.
    if result is None:
        return None

    if json_flag:
        print(verdict_json(result))

    if exit_flag:
        return doctor_exit_code(result)
    return None
