"""The git mechanism behind the installers' origin-repoint fix, proven live.

Both ``install.sh`` (see ``tests/test_install_sh_origin_repoint.py``, which
exercises the real ``clone_repo()`` shell function) and ``install.ps1``'s
``Install-Repository`` fix the same bug the same way: a managed checkout
whose ``origin`` still points at the upstream repository must be repointed
to the release repository AND have its single-branch refspec widened, or
the checkout that follows the fetch cannot find the branch. This file
proves that mechanism directly against real git plumbing -- no shell, no
PowerShell, no reading either installer's source -- so it runs on any
machine with ``git`` (including this one, where PowerShell is unavailable).

The bug this guards, precisely (verified by hand against real git before
writing this file -- see the "Fix round 2" section of the Task 8 report for
the transcript): every clone either installer makes uses ``git clone
--depth 1 --branch <name>``, which configures git's ``remote.origin.fetch``
refspec to track ONLY the cloned branch
(``+refs/heads/main:refs/remotes/origin/main``). Repointing ``origin``'s URL
with ``git remote set-url`` does not touch that refspec. An explicit
``git fetch origin release`` still *exits 0* even then -- git honors an
explicit ref argument and updates ``FETCH_HEAD`` regardless of the stored
refspec -- but it does NOT create ``refs/remotes/origin/release``, because
the refspec has no mapping for that ref. The very next step both installers
run, a bare ``git checkout release`` (relying on git's DWIM remote-branch
tracking), then fails with "did not match any file(s) known to git": there
is no local branch and no ``origin/release`` to check out. ``git remote
set-branches origin <name>`` is the piece that widens the refspec so the
fetch actually creates the remote-tracking ref -- both installers call it,
unconditionally, immediately before the fetch that depends on it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _make_bare_repo(tmp_path: Path, name: str, *, branch: str, content: str) -> Path:
    """A bare repo with one commit on ``branch``, for use as a fake remote."""
    seed = tmp_path / f"{name}-seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    (seed / "f.txt").write_text(content, encoding="utf-8")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-qm", "seed")
    _git(seed, "branch", "-M", branch)

    bare = tmp_path / f"{name}.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "-q", "-u", "origin", branch)
    return bare


def _make_stale_clone(tmp_path: Path, upstream: Path) -> Path:
    """A ``--depth 1 --branch main`` clone -- exactly what either installer's
    fresh-clone path (and every install made before the release-repo switch)
    produces."""
    managed = tmp_path / "managed"
    r = _git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "main", str(upstream), str(managed))
    assert r.returncode == 0, r.stderr
    return managed


pytestmark = pytest.mark.live_system_guard_bypass


def test_url_repoint_alone_does_not_expose_the_release_branch(tmp_path: Path) -> None:
    """The bug, reproduced directly: `set-url` without `set-branches` still breaks.

    This is the exact failure a customer's stale Windows/POSIX install hits
    if only the URL half of the fix ships. The fetch itself deceptively
    *succeeds* (git honors an explicit ref argument regardless of the
    stored refspec and updates FETCH_HEAD) -- but it never creates
    `refs/remotes/origin/release`, because the clone's single-branch
    refspec (from `--branch main`) still has no mapping for "release". The
    checkout that both installers run right after the fetch is what
    actually fails.
    """
    upstream = _make_bare_repo(tmp_path, "upstream", branch="main", content="upstream content\n")
    release = _make_bare_repo(tmp_path, "release", branch="release", content="release content\n")
    managed = _make_stale_clone(tmp_path, upstream)

    _git(managed, "remote", "set-url", "origin", str(release))

    fetch_result = _git(managed, "fetch", "origin", "release", check=False)
    assert fetch_result.returncode == 0, (
        "sanity check on the deceptive half of the bug: an explicit ref "
        "fetch is expected to exit 0 even without set-branches"
    )
    assert not _git(managed, "rev-parse", "--verify", "refs/remotes/origin/release", check=False).stdout.strip(), (
        "the remote-tracking ref must NOT exist yet -- if it does, "
        "set-branches is no longer the mechanism protecting this bug, and "
        "this test (and the installers' fix) needs to be revisited"
    )

    checkout_result = _git(managed, "checkout", "release", check=False)
    assert checkout_result.returncode != 0, (
        "expected checkout to fail with only the URL repointed -- this is "
        "the actual customer-visible failure the set-branches fix closes"
    )


def test_widening_the_refspec_after_repointing_makes_the_release_branch_fetchable(
    tmp_path: Path,
) -> None:
    """The actual fix: `set-branches` after `set-url` is what makes it work."""
    upstream = _make_bare_repo(tmp_path, "upstream", branch="main", content="upstream content\n")
    release = _make_bare_repo(tmp_path, "release", branch="release", content="release content\n")
    managed = _make_stale_clone(tmp_path, upstream)

    _git(managed, "remote", "set-url", "origin", str(release))
    _git(managed, "remote", "set-branches", "origin", "release")

    result = _git(managed, "fetch", "origin", "release", check=False)
    assert result.returncode == 0, result.stderr
    assert _git(managed, "rev-parse", "--verify", "refs/remotes/origin/release", check=False).stdout.strip(), (
        "set-branches should make the fetch create the remote-tracking ref"
    )

    checkout_result = _git(managed, "checkout", "release", check=False)
    assert checkout_result.returncode == 0, checkout_result.stderr
    _git(managed, "reset", "--hard", "origin/release")
    assert (managed / "f.txt").read_text(encoding="utf-8") == "release content\n"


def test_origin_already_on_the_release_repo_needs_no_repoint(tmp_path: Path) -> None:
    """A correctly-configured post-fix install needs neither step.

    Cloned directly from the release repo with `--branch release` (what a
    fresh install now does), the refspec is already scoped to "release" --
    fetching it, and any later commit pushed to it, just works.
    """
    release = _make_bare_repo(tmp_path, "release", branch="release", content="v1\n")
    managed = tmp_path / "managed"
    r = _git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "release", str(release), str(managed))
    assert r.returncode == 0, r.stderr

    # Advance the remote so there is something real to fetch.
    seed2 = tmp_path / "release-seed"
    _git(seed2, "fetch", "-q", "origin")
    (seed2 / "f.txt").write_text("v2\n", encoding="utf-8")
    _git(seed2, "commit", "-qam", "v2")
    _git(seed2, "push", "-q", "origin", "release")

    result = _git(managed, "fetch", "origin", "release", check=False)
    assert result.returncode == 0, result.stderr
    _git(managed, "reset", "--hard", "origin/release")
    assert (managed / "f.txt").read_text(encoding="utf-8") == "v2\n"


# --- The identity check that gates repointing (shared design, not either
# installer's source): both install.sh's `case ... *NousResearch/hermes-agent*)`
# and install.ps1's `-like "*NousResearch/hermes-agent*"` repoint ONLY when
# origin matches this one known-bad identity -- never a customer's own fork
# or mirror. Reimplemented fresh here (not extracted from either script) to
# test the DESIGN INVARIANT, not either file's text.


def _is_known_upstream_origin(url: str) -> bool:
    return "nousresearch/hermes-agent" in url.lower()


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:NousResearch/hermes-agent.git",
        "https://github.com/NousResearch/hermes-agent.git",
        "https://github.com/nousresearch/hermes-agent",
        "git@github.com:NOUSRESEARCH/HERMES-AGENT.git",
    ],
)
def test_known_upstream_identity_is_recognized(url: str) -> None:
    assert _is_known_upstream_origin(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/xdataplusx/trix-agent.git",
        "git@github.com:xdataplusx/trix-agent.git",
        "https://git.example.com/customer/their-own-fork.git",
        "git@internal-mirror.example.com:ops/hermes-agent-mirror.git",
        "/srv/git/NousResearch-hermes-agent-backup.git",
    ],
)
def test_third_party_or_release_origin_is_not_matched(url: str) -> None:
    assert not _is_known_upstream_origin(url)


def test_third_party_origin_is_never_touched_by_the_repoint_flow(tmp_path: Path) -> None:
    """End-to-end: a customer's own mirror, once cloned, is never rewritten.

    Simulates what either installer's update path does when the identity
    check (above) says "leave this alone": nothing runs except the normal
    fetch/checkout a correctly-configured origin already supports.
    """
    customer_mirror = _make_bare_repo(
        tmp_path, "customer-mirror", branch="release", content="customer content\n"
    )
    managed = tmp_path / "managed"
    r = _git(
        tmp_path, "clone", "-q", "--depth", "1", "--branch", "release", str(customer_mirror), str(managed)
    )
    assert r.returncode == 0, r.stderr

    origin_before = _git(managed, "remote", "get-url", "origin").stdout.strip()
    assert not _is_known_upstream_origin(origin_before)

    # The identity check says "leave it" -- so neither installer would call
    # set-url or set-branches here. Confirm the origin a customer configured
    # survives a normal update untouched.
    result = _git(managed, "fetch", "origin", "release", check=False)
    assert result.returncode == 0, result.stderr
    origin_after = _git(managed, "remote", "get-url", "origin").stdout.strip()
    assert origin_after == origin_before == str(customer_mirror)
