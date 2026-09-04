"""A checkout still pointed at the wrong origin must self-heal, not crash raw.

A managed install that predates the release-repo switch (or one a customer
cloned by hand) has its git ``origin`` pointed somewhere other than the
release repository. Before this fix, the update path fetched
``origin "$BRANCH"`` unconditionally: against a repo with no branch of that
name this either aborted with a raw, unbranded git error (direct install,
``set -e`` active) or -- worse -- silently continued with whatever stale
remote-tracking refs happened to already be on disk, reporting "Repository
ready" without ever having fetched anything real (``--stage`` installs run
with ``set +e``, see ``run_stage_protocol`` in install.sh).

These exercise the real ``clone_repo()`` shell function in isolation
(extracted verbatim from install.sh, same technique as
test_install_sh_bootstrap_marker.py) against local bare repos standing in
for "the release repository" -- no network, no dependency on the real
xdataplusx/trix-agent repo existing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _make_bare_repo(
    tmp_path: Path, name: str, *, branch: str, content: str, bare_path: Path | None = None
) -> Path:
    """A bare repo with one commit on ``branch``, for use as a fake remote.

    ``bare_path`` overrides where the bare repo lives -- used to construct a
    local path that literally contains "NousResearch/hermes-agent" so it
    matches install.sh's repoint condition without touching a real host.
    """
    seed = tmp_path / f"{name}-seed"
    seed.mkdir()
    r = _git(seed, "init", "-q")
    assert r.returncode == 0, r.stderr
    (seed / "f.txt").write_text(content, encoding="utf-8")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-qm", "seed")
    _git(seed, "branch", "-M", branch)

    bare = bare_path if bare_path is not None else (tmp_path / f"{name}.git")
    bare.parent.mkdir(parents=True, exist_ok=True)
    r = _git(tmp_path, "init", "-q", "--bare", str(bare))
    assert r.returncode == 0, r.stderr
    _git(seed, "remote", "add", "origin", str(bare))
    r = _git(seed, "push", "-q", "-u", "origin", branch)
    assert r.returncode == 0, r.stderr
    return bare


def _make_fake_upstream_repo(tmp_path: Path) -> Path:
    """A bare repo whose local path literally contains "NousResearch/hermes-agent".

    install.sh's repoint condition matches on that substring, so this stands
    in for "a checkout cloned from the real upstream repository" without any
    network access or a real GitHub host.
    """
    return _make_bare_repo(
        tmp_path,
        "upstream",
        branch="main",
        content="upstream content\n",
        bare_path=tmp_path / "NousResearch" / "hermes-agent.git",
    )


def _run_clone_repo(
    tmp_path: Path,
    install_dir: Path,
    *,
    repo_url_ssh: str,
    repo_url_https: str,
    branch: str = "release",
) -> subprocess.CompletedProcess:
    """Source install.sh's real clone_repo() in isolation and invoke it.

    Extracts the function body verbatim (same approach as
    test_install_sh_bootstrap_marker.py) so this exercises the actual
    shipped logic, not a reimplementation of it, while overriding the
    hardcoded REPO_URL_SSH/REPO_URL_HTTPS so the test never touches a real
    GitHub host.
    """
    script = f"""
set -e
INSTALL_DIR={install_dir!s}
REPO_URL_SSH={repo_url_ssh!r}
REPO_URL_HTTPS={repo_url_https!r}
BRANCH={branch!r}
INSTALL_COMMIT=""
FORCE_COMMIT=false
NON_INTERACTIVE=true
log_info() {{ echo "INFO: $*"; }}
log_warn() {{ echo "WARN: $*"; }}
log_success() {{ echo "OK: $*"; }}
log_error() {{ echo "ERROR: $*"; }}
discard_update_lockfile_churn() {{ :; }}
eval "$(sed -n '/^clone_repo()/,/^}}/p' {INSTALL_SH!s})"
clone_repo
"""
    return subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )


pytestmark = pytest.mark.live_system_guard_bypass


def test_wrong_origin_is_repointed_and_update_succeeds(tmp_path):
    """The common case: SSH URL is reachable, origin gets repointed to it."""
    release = _make_bare_repo(tmp_path, "release", branch="release", content="release content\n")
    wrong_origin = _make_fake_upstream_repo(tmp_path)

    managed = tmp_path / "hermes-agent"
    r = _git(tmp_path, "clone", "-q", "--branch", "main", str(wrong_origin), str(managed))
    assert r.returncode == 0, r.stderr

    result = _run_clone_repo(
        tmp_path, managed, repo_url_ssh=str(release), repo_url_https=str(release), branch="release"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "repointed to" in result.stdout
    assert _git(managed, "remote", "get-url", "origin").stdout.strip() == str(release)
    assert (managed / "f.txt").read_text(encoding="utf-8") == "release content\n"


def test_ssh_unreachable_falls_back_to_https_release_url(tmp_path):
    """SSH URL is bogus -- repoint must fall back to the HTTPS release URL."""
    release = _make_bare_repo(tmp_path, "release", branch="release", content="release content\n")
    wrong_origin = _make_fake_upstream_repo(tmp_path)
    bogus_ssh = tmp_path / "does-not-exist-ssh-target"

    managed = tmp_path / "hermes-agent"
    _git(tmp_path, "clone", "-q", "--branch", "main", str(wrong_origin), str(managed))

    result = _run_clone_repo(
        tmp_path, managed, repo_url_ssh=str(bogus_ssh), repo_url_https=str(release), branch="release"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SSH unreachable, using HTTPS instead" in result.stdout
    assert _git(managed, "remote", "get-url", "origin").stdout.strip() == str(release)
    assert (managed / "f.txt").read_text(encoding="utf-8") == "release content\n"


def test_unreachable_release_repo_fails_loud_with_a_clear_message(tmp_path):
    """Neither URL resolves: fail explicitly, not with a swallowed git error.

    This is the regression this test guards against directly: `--stage`
    installs run clone_repo() with errexit disabled, so an *unchecked*
    `git fetch` failure here would print git's raw stderr and then silently
    continue using whatever stale refs are already on disk -- never
    reporting failure at all. The fetch must be checked explicitly and the
    function must exit non-zero with its own message.
    """
    wrong_origin = _make_fake_upstream_repo(tmp_path)
    bogus = tmp_path / "does-not-exist-anywhere"

    managed = tmp_path / "hermes-agent"
    _git(tmp_path, "clone", "-q", "--branch", "main", str(wrong_origin), str(managed))

    result = _run_clone_repo(
        tmp_path, managed, repo_url_ssh=str(bogus), repo_url_https=str(bogus), branch="release"
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Could not fetch" in combined
    # The failure message must be install.sh's own text, not a bare
    # unexplained git traceback -- "fatal:" from git may still be present
    # (git's own stderr is not suppressed), but the script's explicit
    # ERROR: line must be there too so the customer gets a next step.
    assert "ERROR: Could not fetch" in combined


def test_origin_already_correct_is_left_alone(tmp_path):
    """No repointing noise when origin already matches the release repo."""
    release = _make_bare_repo(tmp_path, "release", branch="release", content="v1\n")

    managed = tmp_path / "hermes-agent"
    _git(tmp_path, "clone", "-q", "--branch", "release", str(release), str(managed))

    # Advance the remote so there is something real to fetch.
    seed2 = tmp_path / "release-seed"
    _git(seed2, "fetch", "-q", "origin")
    (seed2 / "f.txt").write_text("v2\n", encoding="utf-8")
    _git(seed2, "commit", "-qam", "v2")
    r = _git(seed2, "push", "-q", "origin", "release")
    assert r.returncode == 0, r.stderr

    result = _run_clone_repo(
        tmp_path, managed, repo_url_ssh=str(release), repo_url_https=str(release), branch="release"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "repointed to" not in result.stdout
    assert (managed / "f.txt").read_text(encoding="utf-8") == "v2\n"
