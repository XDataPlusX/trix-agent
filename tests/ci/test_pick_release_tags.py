"""Tests for scripts/sandbox/pick-release-tags.sh.

The script decides which released versions the install/update E2E updates
FROM. It is the one piece of the release check that has to work correctly on
a repository it has never seen before — the showcase repo, on its first
release, with exactly one tag in it.

These drive the real script against real throwaway git repositories. Nothing
here reads the script's text.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "sandbox" / "pick-release-tags.sh"
)


def _git(repo: Path, *args: str) -> None:
    # Inherit PATH rather than hardcoding one: the skip guard below decides
    # whether to run these at all by asking `shutil.which("git")`, and a
    # hardcoded list that disagrees with it turns "git lives somewhere else"
    # into a hard failure instead of the skip that was intended.
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        # Keep the sandbox from reading the developer's own git config.
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    })
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", ".")
    _git(r, "commit", "-q", "--allow-empty", "-m", "init")
    return r


def _pick(repo: Path, count: int = 5) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_SCRIPT), "--repo", str(repo), "--count", str(count)],
        capture_output=True,
        text=True,
    )


def _tags(repo: Path, count: int = 5) -> list[str]:
    r = _pick(repo, count)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="needs git and bash",
)


class TestNoReleasesYet:
    def test_a_repo_with_no_tags_fails_loudly(self, repo: Path):
        """Silence here would hand the matrix an empty list and pass vacuously."""
        r = _pick(repo)
        assert r.returncode != 0
        assert "no release tags found" in r.stderr
        assert r.stdout.strip() == ""

    def test_only_non_release_tags_is_the_same_as_none(self, repo: Path):
        _git(repo, "tag", "backup/2026-01-01")
        _git(repo, "tag", "some-one-off")
        assert _pick(repo).returncode != 0


class TestTrixScheme:
    def test_the_very_first_release_is_pickable(self, repo: Path):
        """The showcase's first `trix-v*` tag is present in its own tag push."""
        _git(repo, "tag", "trix-v0.1.0")
        assert _tags(repo) == ["trix-v0.1.0"]

    def test_fewer_tags_than_requested_emits_all_of_them(self, repo: Path):
        for t in ("trix-v0.1.0", "trix-v0.2.0"):
            _git(repo, "tag", t)
        assert _tags(repo, count=5) == ["trix-v0.1.0", "trix-v0.2.0"]

    def test_ordering_is_numeric_not_lexicographic(self, repo: Path):
        for t in ("trix-v0.1.0", "trix-v0.2.0", "trix-v0.10.0"):
            _git(repo, "tag", t)
        # Lexicographic order would put 0.10.0 before 0.2.0.
        assert _tags(repo) == ["trix-v0.1.0", "trix-v0.2.0", "trix-v0.10.0"]

    def test_endpoints_are_always_included(self, repo: Path):
        for t in ("trix-v0.1.0", "trix-v0.2.0", "trix-v0.3.0", "trix-v0.4.0"):
            _git(repo, "tag", t)
        picked = _tags(repo, count=2)
        assert picked[0] == "trix-v0.1.0"
        assert picked[-1] == "trix-v0.4.0"

    def test_non_release_tags_are_ignored(self, repo: Path):
        _git(repo, "tag", "trix-v0.1.0")
        _git(repo, "tag", "backup/x")
        _git(repo, "tag", "trix-vNOPE")
        assert _tags(repo) == ["trix-v0.1.0"]


class TestUpstreamSchemeStillWorks:
    def test_upstream_date_tags_are_recognised(self, repo: Path):
        for t in ("v2026.4.8", "v2026.4.13", "v2026.8.3"):
            _git(repo, "tag", t)
        assert _tags(repo) == ["v2026.4.8", "v2026.4.13", "v2026.8.3"]


class TestSchemesAreNeverMixed:
    def test_trix_tags_win_outright_when_both_exist(self, repo: Path):
        """Two unrelated version lines must not be interleaved by sort -V.

        A fork carries upstream's old `v2026.*` tags in its history. If those
        were mixed in, the "oldest" release the E2E tries to update from would
        be a different product entirely.
        """
        for t in ("v2026.4.8", "v2026.8.3"):
            _git(repo, "tag", t)
        _git(repo, "tag", "trix-v0.1.0")
        assert _tags(repo) == ["trix-v0.1.0"]


class TestCountContract:
    @pytest.mark.parametrize("count", [1, 2, 3, 5])
    def test_never_emits_more_than_requested_and_never_duplicates(
        self, repo: Path, count: int
    ):
        for i in range(8):
            _git(repo, "tag", f"trix-v0.{i}.0")
        picked = _tags(repo, count=count)
        assert len(picked) <= count
        assert len(set(picked)) == len(picked)

    def test_a_zero_count_is_rejected(self, repo: Path):
        _git(repo, "tag", "trix-v0.1.0")
        assert _pick(repo, count=0).returncode != 0


class TestBadRepoArgument:
    def test_a_path_that_is_not_a_repo_says_so(self, tmp_path: Path):
        """Not the same failure as "no tags", and must not be reported as it.

        The old message sent the reader off to check `fetch-depth`, which has
        nothing to do with a wrong `--repo` path.
        """
        r = subprocess.run(
            ["bash", str(_SCRIPT), "--repo", str(tmp_path / "nope"), "--count", "3"],
            capture_output=True, text=True,
        )
        assert r.returncode != 0
        assert "not a git repository" in r.stderr
        assert "no release tags" not in r.stderr
