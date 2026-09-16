"""Fresh-install HTTPS clone must survive smart-HTTP-over-HTTP/2 breakage.

A live run of the installer on a clean client VM (2026-09-03, bb890c7cc5)
found ``git clone`` over HTTPS failing with ``could not read Username for
'https://github.com'`` on a PUBLIC, reachable repo. The real cause: the
datacenter network breaks git's smart-HTTP POST when it goes out over
HTTP/2 -- ``GET /info/refs`` succeeds, then ``POST /git-upload-pack`` comes
back ``HTTP/2 401``. ``github.com/git/git`` fails identically on the same
box, and plain ``curl`` to the same endpoint returns 200. Forcing
``http.version=HTTP/1.1`` clones fine.

The fix, entirely inside ``clone_repo()``'s fresh-install (SSH-then-HTTPS)
branch in scripts/install.sh:

  1. Both HTTPS clone attempts run with ``GIT_TERMINAL_PROMPT=0`` -- without
     it, any HTTPS clone failure makes git fall back to an interactive
     "Username for 'https://github.com'" prompt that hangs the installer
     forever on a tty, for a repo that needs no credentials at all.
  2. On a first-attempt HTTPS failure, exactly ONE retry runs with
     ``-c http.version=HTTP/1.1`` (a downgrade-on-failure retry, not an
     unconditional downgrade -- HTTP/2 is left alone wherever it already
     works).
  3. If the retry succeeds, ``http.version=HTTP/1.1`` is persisted into the
     freshly cloned tree's own git config, so a later ``hermes update`` on
     the same broken network doesn't hit the same wall again.
  4. If both HTTPS attempts fail, the partial checkout directory is removed
     and the function exits non-zero with an explicit message.

These tests exercise the real ``clone_repo()`` shell function (extracted
verbatim out of install.sh, same sed-range technique as
test_install_sh_origin_repoint.py and test_install_sh_bootstrap_marker.py)
against a fully stubbed ``git`` binary placed first on PATH -- no real
network or filesystem git operation ever runs. The stub is a small dispatch
script (not a source-text search) that:

  * logs every invocation (argv + the ``GIT_TERMINAL_PROMPT``/
    ``GIT_SSH_COMMAND`` env it saw) to a file the tests parse afterwards;
  * recognizes clone calls against the SSH vs. HTTPS remote by a marker
    string baked into each fake remote URL, and the retry specifically by
    the presence of a leading ``-c http.version=HTTP/1.1``;
  * on a failed clone, still creates a partial ``<dir>/.git/`` (mirroring
    how a real ``git clone`` can leave a half-made directory behind) so the
    "both attempts fail" test can prove the cleanup ``rm -rf`` actually
    ran, not merely that nothing was ever created;
  * on ``git -C <dir> config <key> <value>``, appends ``key=value`` to
    ``<dir>/.git/config.stub`` so the HTTP/1.1-persistence assertion reads
    real stub-recorded state, not a re-implementation's guess.

SSH is always made to fail in every test here (the installer already tries
SSH first and only reaches the HTTPS branch under test on SSH failure);
what's under test is exclusively the HTTPS-then-HTTP/1.1-retry behavior
bb890c7cc5 added.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

pytestmark = pytest.mark.live_system_guard_bypass

SSH_URL = "ssh-marker://unreachable/repo.git"
HTTPS_URL = "https-marker://example.invalid/repo.git"

# Dispatch stub: NOT a source-text search over install.sh, a real fake
# git binary that install.sh's real clone_repo() actually executes.
_GIT_STUB = r"""#!/usr/bin/env bash
# Fake git for scripts/install.sh clone_repo() fallback tests.
# Every invocation is appended to $GIT_STUB_LOG before being handled.
log="$GIT_STUB_LOG"
{
    printf 'ARGS:'
    for a in "$@"; do printf ' [%s]' "$a"; done
    printf '\n'
    printf '  GIT_TERMINAL_PROMPT=%s\n' "${GIT_TERMINAL_PROMPT-<unset>}"
    printf '  GIT_SSH_COMMAND=%s\n' "${GIT_SSH_COMMAND:+<set>}"
} >> "$log"

args=("$@")

if [ "${args[0]:-}" = "-C" ]; then
    dir="${args[1]}"
    sub="${args[2]:-}"
    case "$sub" in
        config)
            key="${args[3]}"
            val="${args[4]}"
            mkdir -p "$dir/.git"
            printf '%s=%s\n' "$key" "$val" >> "$dir/.git/config.stub"
            exit 0
            ;;
        rev-parse)
            [ -f "$dir/.git/HEAD_OK" ] && exit 0 || exit 1
            ;;
        *)
            # sparse-checkout, checkout, and anything else clone_repo()'s
            # fresh-install branch calls after a successful clone: no-op ok.
            exit 0
            ;;
    esac
fi

# Strip a leading "-c http.version=HTTP/1.1" pair and remember we saw it --
# this is exactly how the retry attempt is distinguished from the first.
has_http11=0
rest=()
i=0
while [ $i -lt ${#args[@]} ]; do
    if [ "${args[$i]}" = "-c" ] && [ "${args[$((i+1))]:-}" = "http.version=HTTP/1.1" ]; then
        has_http11=1
        i=$((i+2))
        continue
    fi
    rest+=("${args[$i]}")
    i=$((i+1))
done

if [ "${rest[0]:-}" = "clone" ]; then
    n=${#rest[@]}
    url="${rest[$((n-2))]}"
    dir="${rest[$((n-1))]}"
    case "$url" in
        *ssh-marker*)
            outcome="${STUB_SSH_CLONE:-fail}"
            ;;
        *https-marker*)
            if [ "$has_http11" = "1" ]; then
                outcome="${STUB_RETRY_CLONE:-fail}"
            else
                outcome="${STUB_FIRST_HTTPS_CLONE:-fail}"
            fi
            ;;
        *)
            outcome="fail"
            ;;
    esac
    if [ "$outcome" = "success" ]; then
        mkdir -p "$dir/.git"
        exit 0
    else
        # Mirror real git: a failed clone can still leave a partial
        # directory on disk. This is load-bearing for the
        # "both attempts fail -> directory cleaned up" test: without it,
        # that test couldn't distinguish "nothing was ever created" from
        # "something was created and then genuinely removed."
        mkdir -p "$dir/.git"
        : > "$dir/.git/PARTIAL_CLONE"
        exit 1
    fi
fi

exit 0
"""


def _install_git_stub(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "git"
    stub.write_text(_GIT_STUB, encoding="utf-8")
    stub.chmod(0o755)


def _run_clone_repo_fresh(
    tmp_path: Path,
    install_dir: Path,
    *,
    ssh_ok: bool = False,
    first_https_ok: bool,
    retry_ok: bool,
) -> tuple[subprocess.CompletedProcess, str]:
    """Run the real clone_repo() fresh-install branch against a stub git.

    Returns (completed_process, git_stub_log_text). install_dir must not
    exist yet -- that's what selects clone_repo()'s "else" (fresh clone)
    branch over its "existing install, update" branch.
    """
    import os

    bin_dir = tmp_path / "stubbin"
    _install_git_stub(bin_dir)
    log_path = tmp_path / "git-stub.log"
    log_path.write_text("", encoding="utf-8")

    script = f"""
set -e
INSTALL_DIR={shlex.quote(str(install_dir))}
REPO_URL_SSH={shlex.quote(SSH_URL)}
REPO_URL_HTTPS={shlex.quote(HTTPS_URL)}
BRANCH="release"
INSTALL_COMMIT=""
FORCE_COMMIT=false
log_info() {{ echo "INFO: $*"; }}
log_warn() {{ echo "WARN: $*"; }}
log_success() {{ echo "OK: $*"; }}
log_error() {{ echo "ERROR: $*"; }}
discard_update_lockfile_churn() {{ :; }}
eval "$(sed -n '/^clone_repo()/,/^}}/p' {shlex.quote(str(INSTALL_SH))})"
clone_repo
"""
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["GIT_STUB_LOG"] = str(log_path)
    env["STUB_SSH_CLONE"] = "success" if ssh_ok else "fail"
    env["STUB_FIRST_HTTPS_CLONE"] = "success" if first_https_ok else "fail"
    env["STUB_RETRY_CLONE"] = "success" if retry_ok else "fail"

    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, log_path.read_text(encoding="utf-8")


def _https_clone_attempts(log: str) -> list[str]:
    """Split the stub log into per-invocation blocks touching the HTTPS URL."""
    blocks = [b for b in log.split("ARGS:") if b.strip()]
    return [b for b in blocks if "https-marker" in b and "clone" in b]


def test_successful_first_https_clone_does_not_retry(tmp_path):
    """The common case: HTTPS clones on the first try, no HTTP/1.1 retry."""
    managed = tmp_path / "trix-agent"

    result, log = _run_clone_repo_fresh(
        tmp_path, managed, first_https_ok=True, retry_ok=True
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: Cloned via HTTPS" in result.stdout
    assert "(HTTP/1.1)" not in result.stdout
    assert "retrying over HTTP/1.1" not in result.stdout

    https_attempts = _https_clone_attempts(log)
    assert len(https_attempts) == 1, log
    assert "http.version=HTTP/1.1" not in https_attempts[0]

    # The retry's persistence step never ran -- no config.stub at all.
    assert not (managed / ".git" / "config.stub").exists()
    assert managed.is_dir()


def test_first_clone_fails_retry_over_http1_1_succeeds_and_persists(tmp_path):
    """First HTTPS attempt fails; the HTTP/1.1 retry must run, succeed, and
    stick http.version=HTTP/1.1 into the cloned tree's own git config so a
    later `hermes update` on the same network doesn't hit the same wall."""
    managed = tmp_path / "trix-agent"

    result, log = _run_clone_repo_fresh(
        tmp_path, managed, first_https_ok=False, retry_ok=True
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "HTTPS clone failed, retrying over HTTP/1.1..." in result.stdout
    assert "OK: Cloned via HTTPS (HTTP/1.1)" in result.stdout

    https_attempts = _https_clone_attempts(log)
    assert len(https_attempts) == 2, log
    assert "http.version=HTTP/1.1" not in https_attempts[0]  # first attempt: plain
    assert "http.version=HTTP/1.1" in https_attempts[1]  # retry: forced

    config_stub = managed / ".git" / "config.stub"
    assert config_stub.exists(), "http.version was never persisted into the clone"
    assert "http.version=HTTP/1.1" in config_stub.read_text(encoding="utf-8")
    assert managed.is_dir()


def test_both_https_attempts_fail_exits_nonzero_and_cleans_up(tmp_path):
    """Both HTTPS attempts fail: non-zero exit, explicit error, and the
    partial checkout directory must not be left behind."""
    managed = tmp_path / "trix-agent"

    result, log = _run_clone_repo_fresh(
        tmp_path, managed, first_https_ok=False, retry_ok=False
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "ERROR: Failed to clone repository" in combined

    https_attempts = _https_clone_attempts(log)
    assert len(https_attempts) == 2, log  # both attempts actually ran

    # The stub's failed clones leave a partial .git/ behind by design (see
    # module docstring) -- if the fix's cleanup rm -rf were missing, this
    # directory would still be here.
    assert not managed.exists(), (
        f"{managed} was not cleaned up after both HTTPS attempts failed"
    )


def test_git_terminal_prompt_disabled_on_both_https_attempts(tmp_path):
    """GIT_TERMINAL_PROMPT=0 must be set for BOTH the first HTTPS attempt
    and the HTTP/1.1 retry -- without it, any failure on a public repo
    falls back to an interactive username prompt that hangs the installer
    forever on a tty (this is the second half of the fix, independent of
    the retry-succeeds-or-fails outcome)."""
    managed = tmp_path / "trix-agent"

    result, log = _run_clone_repo_fresh(
        tmp_path, managed, first_https_ok=False, retry_ok=True
    )
    assert result.returncode == 0, result.stdout + result.stderr

    https_attempts = _https_clone_attempts(log)
    assert len(https_attempts) == 2, log
    for attempt in https_attempts:
        assert "GIT_TERMINAL_PROMPT=0" in attempt, (
            f"a HTTPS clone attempt ran without GIT_TERMINAL_PROMPT=0:\n{attempt}"
        )
