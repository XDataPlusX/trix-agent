#!/usr/bin/env python3
"""Pick the commit range whose diff gates the CI lanes.

The ``detect-changes`` action classifies changed files into work lanes so the
orchestrator only spins up the sub-workflows a change can affect. That needs a
file list, the file list needs a commit range, and choosing the range is the
whole decision. This module is that decision, extracted so it can be tested
for real instead of asserted about as YAML text.

Contract — **gate only on a range we trust, otherwise fail open.**
Emitting no range makes the classifier mark every lane true, which costs
runner minutes but can never skip a lane a change could break. Emitting a
*wrong* range would skip lanes silently, so every case we are not sure about
resolves to "no range".

Who gets gated:

* ``pull_request`` — the event payload's pinned base/head. Unchanged; this is
  what the action always did.
* ``push`` to a branch named in ``gated_branches`` — the pushed range
  ``before...after``. This is the case that makes per-push CI affordable on
  a private repo: without it a docs-only push to a working branch spins up
  every lane, macOS included (billed at 10x).
* Everything else — no range.

**Gating is opt-in per branch, by name.** An earlier version of this module
exempted "the repository's default branch" instead, reasoning that a default
branch is where post-merge validation happens and must never be narrowed.
That reasoning does not survive contact with this repository: the fork's
default branch **is** ``xdata-agent``, the working branch, and there is no
``main`` on the remote at all. The exemption therefore swallowed the only
branch it was written to gate, and did so silently — fail-open is invisible,
it just bills.

The concept that was actually meant is "a branch whose runs are the last
gate before a release", which no API field reports. So it is stated
explicitly, once, next to ``push: branches:`` in ci.yml. A repository that
passes nothing gets the old behaviour: pushes are never gated.
"""

from __future__ import annotations

import os

# git's "this ref did not exist" sentinel, as it appears in a push payload's
# ``before`` when a branch is created (and in ``after`` when one is deleted).
_ZERO_SHA = "0" * 40


def _is_real_sha(value: str) -> bool:
    """True when ``value`` names a commit we can actually diff against."""
    return bool(value) and value != _ZERO_SHA


def parse_branch_list(raw: str) -> tuple[str, ...]:
    """Split a comma/whitespace-separated branch list into names."""
    return tuple(name for name in raw.replace(",", " ").split() if name)


def compare_range(
    event_name: str,
    *,
    ref_name: str = "",
    ref_type: str = "branch",
    gated_branches: "tuple[str, ...] | list[str]" = (),
    before: str = "",
    after: str = "",
    base: str = "",
    head: str = "",
) -> tuple[str | None, str | None]:
    """Return ``(base, head)`` to diff, or ``(None, None)`` to fail open.

    The two elements are always both set or both ``None`` — callers branch on
    one of them, so half a range would read as a usable range.
    """
    if event_name == "pull_request":
        if _is_real_sha(base) and _is_real_sha(head):
            return base, head
        return None, None

    if event_name == "push" and ref_type == "branch":
        # Not on the opt-in list (or no list at all) -> never gated.
        if ref_name not in tuple(gated_branches):
            return None, None
        if _is_real_sha(before) and _is_real_sha(after):
            return before, after
        return None, None

    return None, None


def main() -> int:
    base, head = compare_range(
        os.environ.get("EVENT_NAME", ""),
        ref_name=os.environ.get("REF_NAME", ""),
        ref_type=os.environ.get("REF_TYPE", "branch"),
        gated_branches=parse_branch_list(os.environ.get("GATED_PUSH_BRANCHES", "")),
        before=os.environ.get("BEFORE_SHA", ""),
        after=os.environ.get("AFTER_SHA", ""),
        base=os.environ.get("BASE_SHA", ""),
        head=os.environ.get("HEAD_SHA", ""),
    )
    # Shell-friendly: empty values mean "fail open", so the caller can test
    # with a plain [ -n "$COMPARE_BASE" ].
    print(f"compare_base={base or ''}")
    print(f"compare_head={head or ''}")
    if dest := os.environ.get("GITHUB_OUTPUT"):
        with open(dest, "a", encoding="utf-8") as fh:
            fh.write(f"compare_base={base or ''}\n")
            fh.write(f"compare_head={head or ''}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
