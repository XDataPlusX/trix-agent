"""Tests for scripts/ci/pick_compare_range.py.

The classifier can only gate a lane when it knows which files changed. That
list comes from a commit range, and picking the range is the whole decision:
pick one and the run is gated, pick nothing and the classifier fails open and
every lane runs.

Contract, in one line: **gate only when the range is trustworthy, fail open
otherwise.** A wrong range is worse than no range — it would skip a lane that
a change could break.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "pick_compare_range.py"
_spec = importlib.util.spec_from_file_location("pick_compare_range", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load pick_compare_range.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
compare_range = _mod.compare_range
parse_branch_list = _mod.parse_branch_list
main = _mod.main

_ZERO = "0" * 40
_BEFORE = "a" * 40
_AFTER = "b" * 40
_BASE = "c" * 40
_HEAD = "d" * 40


class TestPullRequest:
    """PR events keep the behaviour they already had: gate on the PR diff."""

    def test_pull_request_uses_the_event_payload_shas(self):
        assert compare_range(
            "pull_request", base=_BASE, head=_HEAD
        ) == (_BASE, _HEAD)

    def test_pull_request_without_shas_fails_open(self):
        """A payload we cannot read is not a payload we may trust."""
        assert compare_range("pull_request", base="", head="") == (None, None)


_GATED = ("xdata-agent",)


class TestPushToWorkingBranch:
    """The new capability: a push to an opted-in branch is gated too."""

    def test_push_to_a_gated_branch_uses_the_pushed_range(self):
        assert compare_range(
            "push",
            ref_name="xdata-agent",
            gated_branches=_GATED,
            before=_BEFORE,
            after=_AFTER,
        ) == (_BEFORE, _AFTER)

    def test_the_working_branch_is_gated_even_when_it_IS_the_default_branch(self):
        """The regression that made the whole feature a no-op.

        An earlier version exempted "the default branch", meaning to protect
        post-merge validation. This fork's default branch IS its working
        branch -- there is no `main` on the remote -- so the exemption
        swallowed the only branch it was written for, silently: fail-open
        runs every lane, reports green, and only shows up on the bill.
        Gating is opt-in by name now; no default-branch concept is involved.
        """
        assert compare_range(
            "push",
            ref_name="xdata-agent",
            gated_branches=_GATED,
            before=_BEFORE,
            after=_AFTER,
        ) == (_BEFORE, _AFTER)

    def test_branch_creation_fails_open(self):
        """A brand-new branch has an all-zero ``before`` — no range exists."""
        assert compare_range(
            "push",
            ref_name="xdata-agent",
            gated_branches=_GATED,
            before=_ZERO,
            after=_AFTER,
        ) == (None, None)

    def test_missing_before_fails_open(self):
        assert compare_range(
            "push",
            ref_name="xdata-agent",
            gated_branches=_GATED,
            before="",
            after=_AFTER,
        ) == (None, None)

    def test_missing_after_fails_open(self):
        assert compare_range(
            "push",
            ref_name="xdata-agent",
            gated_branches=_GATED,
            before=_BEFORE,
            after="",
        ) == (None, None)


class TestBranchesNotOptedIn:
    """Anything not named stays on the old behaviour: run every lane."""

    def test_a_branch_not_on_the_list_fails_open(self):
        assert compare_range(
            "push",
            ref_name="release",
            gated_branches=_GATED,
            before=_BEFORE,
            after=_AFTER,
        ) == (None, None)

    def test_an_empty_list_gates_nothing(self):
        """Upstream passes nothing and keeps exactly the behaviour it had."""
        assert compare_range(
            "push",
            ref_name="main",
            gated_branches=(),
            before=_BEFORE,
            after=_AFTER,
        ) == (None, None)

    def test_matching_is_exact_not_a_prefix(self):
        assert compare_range(
            "push",
            ref_name="xdata-agent-experiment",
            gated_branches=_GATED,
            before=_BEFORE,
            after=_AFTER,
        ) == (None, None)


class TestBranchListParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", ()),
            ("   ", ()),
            ("xdata-agent", ("xdata-agent",)),
            ("xdata-agent,dev", ("xdata-agent", "dev")),
            ("xdata-agent, dev", ("xdata-agent", "dev")),
            ("xdata-agent  dev", ("xdata-agent", "dev")),
            ("\n xdata-agent \n dev \n", ("xdata-agent", "dev")),
        ],
    )
    def test_separators(self, raw, expected):
        assert parse_branch_list(raw) == expected


class TestTagsAndOtherRefs:
    def test_tag_push_fails_open(self):
        """A tag push carries no meaningful `before` — release runs everything."""
        assert compare_range(
            "push",
            ref_name="trix-v0.1.0",
            ref_type="tag",
            gated_branches=("trix-v0.1.0",),  # even if the name matched
            before=_BEFORE,
            after=_AFTER,
        ) == (None, None)


class TestEverythingElseFailsOpen:
    @pytest.mark.parametrize(
        "event",
        ["workflow_dispatch", "schedule", "release", "merge_group", "unknown_event"],
    )
    def test_non_push_non_pr_events_fail_open(self, event):
        assert compare_range(
            event,
            ref_name="xdata-agent",
            gated_branches=_GATED,
            before=_BEFORE,
            after=_AFTER,
            base=_BASE,
            head=_HEAD,
        ) == (None, None)


class TestInvariant:
    """The property that actually matters, stated once."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"event_name": "push", "ref_name": "main", "gated_branches": _GATED,
             "before": _BEFORE, "after": _AFTER},
            {"event_name": "push", "ref_name": "xdata-agent",
             "gated_branches": _GATED, "before": _ZERO, "after": _AFTER},
            {"event_name": "schedule"},
            {"event_name": "workflow_dispatch"},
        ],
    )
    def test_a_range_is_either_fully_present_or_fully_absent(self, kwargs):
        """Never emit half a range: the caller branches on `base` alone."""
        base, head = compare_range(**kwargs)
        assert (base is None) == (head is None)


class TestTheStdoutContractTheShellParses:
    """action.yml greps `compare_base=` out of stdout with sed.

    Renaming that prefix would silently disable every gate — fail-open, so
    green and invisible. This pins the wire format, not an implementation.
    """

    def _run(self, monkeypatch, tmp_path, **env):
        import io
        import contextlib

        for key in ("EVENT_NAME", "REF_NAME", "REF_TYPE", "GATED_PUSH_BRANCHES",
                    "BEFORE_SHA", "AFTER_SHA", "BASE_SHA", "HEAD_SHA",
                    "GITHUB_OUTPUT"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert main() == 0
        return buf.getvalue()

    def test_a_gated_push_prints_both_shas(self, monkeypatch, tmp_path):
        out = self._run(
            monkeypatch, tmp_path,
            EVENT_NAME="push", REF_NAME="xdata-agent", REF_TYPE="branch",
            GATED_PUSH_BRANCHES="xdata-agent",
            BEFORE_SHA=_BEFORE, AFTER_SHA=_AFTER,
        )
        assert f"compare_base={_BEFORE}" in out
        assert f"compare_head={_AFTER}" in out

    def test_fail_open_prints_empty_values_not_nothing(self, monkeypatch, tmp_path):
        """The shell tests `[ -n "$COMPARE_BASE" ]`, so the key must still
        appear with an empty value rather than vanish."""
        out = self._run(
            monkeypatch, tmp_path,
            EVENT_NAME="push", REF_NAME="release", REF_TYPE="branch",
            GATED_PUSH_BRANCHES="xdata-agent",
            BEFORE_SHA=_BEFORE, AFTER_SHA=_AFTER,
        )
        assert "compare_base=" in out
        assert "compare_head=" in out
        assert _BEFORE not in out

    def test_github_output_is_written_when_set(self, monkeypatch, tmp_path):
        dest = tmp_path / "out"
        self._run(
            monkeypatch, tmp_path,
            EVENT_NAME="push", REF_NAME="xdata-agent", REF_TYPE="branch",
            GATED_PUSH_BRANCHES="xdata-agent",
            BEFORE_SHA=_BEFORE, AFTER_SHA=_AFTER,
            GITHUB_OUTPUT=str(dest),
        )
        assert f"compare_base={_BEFORE}" in dest.read_text()


class TestForcePush:
    def test_a_force_pushed_range_is_still_gated(self):
        """`before` is a real SHA after a force-push, so the range is used.

        That is safe rather than lucky only because GitHub's compare is
        merge-base-based: a rewind yields an empty diff (which fails open at
        the classifier) and a rebase yields a superset. Pinned here so the
        next reader does not "fix" it into a fail-closed shape.
        """
        assert compare_range(
            "push",
            ref_name="xdata-agent",
            gated_branches=_GATED,
            before=_BEFORE,
            after=_AFTER,
        ) == (_BEFORE, _AFTER)
