"""Обновление ходит только за релизной веткой нашего репозитория.

Флаг --branch давал клиенту штатный способ увести свою установку на
другую ветку. Резолвер теперь игнорирует любой вход.
"""

import argparse

from hermes_cli.release_source import RELEASE_BRANCH


def _args(**kw):
    return argparse.Namespace(**kw)


def test_resolver_returns_the_release_branch_when_nothing_is_asked():
    from hermes_cli.main import _resolve_update_branch

    assert _resolve_update_branch(_args()) == RELEASE_BRANCH


def test_resolver_ignores_a_branch_attribute_left_by_anything_else():
    """Даже если атрибут появится (плагин, старый вызов), канал не меняется."""
    from hermes_cli.main import _resolve_update_branch

    assert _resolve_update_branch(_args(branch="main")) == RELEASE_BRANCH
    assert _resolve_update_branch(_args(branch="какая-угодно")) == RELEASE_BRANCH


def test_update_parser_rejects_branch_flag():
    """CLI больше не принимает --branch: клиент получает явную ошибку."""
    import pytest

    from hermes_cli.subcommands.update import build_update_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_update_parser(subparsers, cmd_update=lambda args: None)

    with pytest.raises(SystemExit):
        parser.parse_args(["update", "--branch", "main"])


def test_update_parser_still_accepts_the_flags_we_kept():
    from hermes_cli.subcommands.update import build_update_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_update_parser(subparsers, cmd_update=lambda args: None)

    parsed = parser.parse_args(["update", "--check", "--yes"])
    assert parsed.check is True
    assert parsed.yes is True


def test_upstream_sync_machinery_is_gone():
    """Ни одной функции, способной привязать клиента к upstream.

    Не косметика: _sync_with_upstream_if_needed в gateway-режиме печатал
    клиенту в чат Telegram готовую инструкцию, как перейти на upstream —
    input() падает там с EOFError, ответ читается как "нет", и уходит
    ветка с текстом "Run 'git remote add upstream ...'".
    """
    import hermes_cli.update_cmd as uc

    for name in (
        "_is_fork",
        "_has_upstream_remote",
        "_add_upstream_remote",
        "_sync_with_upstream_if_needed",
        "_sync_fork_with_upstream",
        "_should_skip_upstream_prompt",
        "_mark_skip_upstream_prompt",
        "OFFICIAL_REPO_URL",
    ):
        assert not hasattr(uc, name), f"{name} всё ещё существует"


def test_main_does_not_re_export_removed_helpers():
    import hermes_cli.main as hm

    for name in ("_sync_with_upstream_if_needed", "_sync_fork_with_upstream"):
        assert not hasattr(hm, name), f"{name} реэкспортируется из main"


def test_update_refuses_a_foreign_origin(tmp_path, capsys):
    """Подменённый origin — отказ до любого обращения к сети."""
    import subprocess

    import pytest

    from hermes_cli.update_cmd import _assert_release_source

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "https://github.com/someone/else.git"],
        check=True,
    )

    with pytest.raises(SystemExit) as exc:
        _assert_release_source(["git"], tmp_path)
    assert exc.value.code == 1

    out = capsys.readouterr().out
    assert "XDataPlus" in out
    assert "nousresearch" not in out.lower()


def test_update_accepts_our_own_origin(tmp_path):
    """Наш origin проходит молча -- независимо от локальной ветки.

    Только remote несёт защитное свойство здесь: с проверенным origin
    обновление всегда тянет и мёржит origin/<RELEASE_BRANCH>, так что имя
    локальной ветки не может протащить чужой код. Отказ по ветке был
    убран (controller ruling, fix round 1) -- он не добавлял защиты, но
    ломал самовосстановление для чекаутов, сделанных до фиксации канала.
    """
    import subprocess

    from hermes_cli.release_source import RELEASE_REPO_HTTPS
    from hermes_cli.update_cmd import _assert_release_source

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", RELEASE_REPO_HTTPS],
        check=True,
    )

    _assert_release_source(["git"], tmp_path)  # не должно поднять SystemExit


def test_update_refuses_before_touching_the_network(tmp_path, monkeypatch):
    """The guard must exit before any subprocess call that could reach the
    network -- a client with a foreign origin gets the refusal without a
    fetch ever being attempted.

    ``_get_origin_url`` (the only call `_assert_release_source` makes before
    its origin check) uses `git remote get-url`, which is purely local --
    git never dials out for it. We prove "before any network access" by
    recording every subprocess invocation and asserting none of them is a
    network verb (fetch/pull/clone/push/ls-remote).
    """
    import subprocess as _subprocess

    import pytest

    from hermes_cli.update_cmd import _assert_release_source

    _subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "https://github.com/someone/else.git"],
        check=True,
    )

    recorded = []
    real_run = _subprocess.run

    def _tracking_run(cmd, *args, **kwargs):
        recorded.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(_subprocess, "run", _tracking_run)

    with pytest.raises(SystemExit):
        _assert_release_source(["git"], tmp_path)

    network_verbs = {"fetch", "pull", "clone", "push", "ls-remote"}
    for cmd in recorded:
        assert not (set(cmd) & network_verbs), (
            f"guard reached the network before refusing: {cmd}"
        )


def test_cmd_update_refuses_a_foreign_origin_at_the_call_site(monkeypatch, capsys):
    """Call-site coverage: the guard must actually be wired into
    ``cmd_update``, not just exist as a standalone function.

    Every other test in this module drives ``_assert_release_source``
    directly -- none of them would notice if the call site in
    ``_cmd_update_impl`` (``update_cmd.py``, right after ``git_cmd`` is
    built) were deleted or replaced with ``pass``. Go through the real
    ``cmd_update`` entry point instead, with ``subprocess.run`` mocked to
    report a foreign origin, and assert both that the process exits 1 and
    that no recorded git invocation is a ``fetch`` -- proving the refusal
    happens before the update flow ever reaches the network.
    """
    import subprocess
    from types import SimpleNamespace

    import pytest

    from hermes_cli.main import cmd_update

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        if "remote" in joined and "get-url" in joined and "origin" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/someone/else.git\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda *a, **kw: None)

    args = SimpleNamespace(check=False, yes=True)

    with pytest.raises(SystemExit) as exc:
        cmd_update(args)
    assert exc.value.code == 1

    assert not any("fetch" in cmd for cmd in recorded), recorded

    out = capsys.readouterr().out
    assert "XDataPlus" in out


def test_update_check_never_touches_an_upstream_remote(monkeypatch, tmp_path, capsys):
    """Regression guard for the old ``--check`` upstream-comparison block.

    ``_cmd_update_check`` used to probe for and fetch an ``upstream``
    remote when ``branch == "main"`` -- unreachable today because the
    channel resolver always passes ``RELEASE_BRANCH``, but the function's
    own default was still the string ``"main"``, so a direct call (or a
    future edit) could re-enable it. A client who migrated from an older
    install can still have a leftover ``upstream`` remote pointed at
    NousResearch in ``.git/config`` -- if that block ever fires again, the
    fetch succeeds and the product tells a paying customer they're behind
    upstream. Drive ``_cmd_update_check`` with subprocess mocked and an
    ``upstream`` remote present, and assert nothing recorded ever names it.
    """
    import subprocess

    import hermes_cli.update_cmd as uc

    monkeypatch.setattr(uc._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method", lambda root: "git"
    )

    (tmp_path / ".git").mkdir()

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        if "remote" in joined and "get-url" in joined and "upstream" in joined:
            # An old install with a leftover 'upstream' remote from before
            # the migration -- exactly the client this regression targets.
            return subprocess.CompletedProcess(cmd, 0, stdout="https://github.com/NousResearch/hermes-agent.git\n", stderr="")
        if "rev-parse" in joined and "--is-shallow-repository" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="false\n", stderr="")
        if "fetch" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "rev-parse" in joined and "--verify" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "rev-list" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Call with the function's own default -- exercising exactly the "no
    # branch passed" path a stray direct call or future edit would take.
    uc._cmd_update_check()

    for cmd in recorded:
        assert "upstream" not in cmd, f"–check touched an upstream remote: {cmd}"


def test_zip_fallback_downloads_from_our_archive_url(monkeypatch):
    """The Windows ZIP-fallback update path must fetch our release archive.

    Behavioral, not source-text: patches the actual download call
    (``urlretrieve``, imported locally inside ``_update_via_zip``) and
    asserts on the URL it was actually asked to fetch. A test that only
    read the function's source (as an earlier draft of this test did)
    would pass even if ``RELEASE_ARCHIVE_URL`` were built and then never
    threaded into the download call.
    """
    import argparse

    import pytest

    from hermes_cli.release_source import RELEASE_ARCHIVE_URL
    from hermes_cli.update_cmd import _update_via_zip

    requested = []

    def fake_urlretrieve(url, path):
        requested.append(url)
        # Network is disabled in tests; bail out immediately so the rest of
        # the extraction/copy machinery (which needs a real ZIP) never runs.
        # _update_via_zip catches every exception from this point on and
        # turns it into sys.exit(1), so the fetch is proven before we ever
        # need a real archive on disk.
        raise RuntimeError("network disabled in test")

    monkeypatch.setattr("urllib.request.urlretrieve", fake_urlretrieve)

    with pytest.raises(SystemExit):
        _update_via_zip(argparse.Namespace())

    assert requested == [RELEASE_ARCHIVE_URL]
    assert "nousresearch" not in requested[0].lower()


def test_banner_update_probe_targets_our_release_branch(monkeypatch):
    """The update-check probe must never dial out to Nous Research.

    Behavioral: patches ``subprocess.run`` and inspects the argv the probe
    actually executes, rather than reading source -- this catches the case
    where the release-repo constant exists but was never threaded into the
    ``git ls-remote`` call, which a source-text scan cannot.
    """
    import subprocess

    import hermes_cli.banner as banner
    from hermes_cli.release_source import RELEASE_BRANCH, canonical_remote

    recorded = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(banner.subprocess, "run", fake_run)

    banner._check_via_rev("deadbeef")

    cmd = recorded["cmd"]
    assert cmd[:2] == ["git", "ls-remote"]
    url = cmd[2]
    assert canonical_remote(url) == canonical_remote(banner._RELEASE_REPO_URL)
    assert "nousresearch" not in url.lower()
    assert cmd[3] == f"refs/heads/{RELEASE_BRANCH}"


def test_local_git_update_check_targets_the_release_branch(monkeypatch):
    """``check_for_updates()`` on an HTTPS-remote checkout (the common
    install path -- ``install.sh`` clones HTTPS-first) must fetch and count
    commits behind our RELEASE branch, not a hardcoded ``main``.

    Our release repository has no ``main`` branch. Before this fix,
    ``_check_via_local_git`` fetched and counted against a hardcoded
    ``"main"`` literal: the fetch of a nonexistent branch quietly no-ops
    (return code discarded, no exception), the subsequent
    ``rev-parse origin/main`` / ``rev-list HEAD..origin/main`` finds no such
    ref, and the function falls through to ``None`` -- so the
    "N commits behind, run `hermes update`" banner never fires for any
    HTTPS-remote client. Behavioral: patches ``subprocess.run`` and asserts
    on the actual argv passed to ``fetch``/``rev-list``, not on source text.
    """
    import subprocess

    import hermes_cli.banner as banner
    from hermes_cli.release_source import RELEASE_BRANCH

    monkeypatch.delenv("HERMES_REVISION", raising=False)
    monkeypatch.setattr(
        "hermes_cli.config.detect_install_method", lambda root: "git"
    )

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        if "remote" in joined and "get-url" in joined and "origin" in joined:
            # HTTPS origin -- the common client install path, and the one
            # that takes the local-git-counting branch under test (the SSH
            # path is handled separately by ``_check_via_rev``).
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/xdataplusx/trix-agent.git\n", stderr=""
            )
        if "rev-parse" in joined and "--is-shallow-repository" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="false\n", stderr="")
        if "fetch" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if "rev-list" in joined and "--count" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="2\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    behind = banner.check_for_updates()

    assert behind == 2

    fetch_cmds = [c for c in recorded if "fetch" in c]
    assert fetch_cmds, f"check_for_updates never fetched: {recorded}"
    assert fetch_cmds[0] == ["git", "fetch", "origin", RELEASE_BRANCH, "--quiet"]
    assert "main" not in fetch_cmds[0]

    count_cmds = [c for c in recorded if "rev-list" in c]
    assert count_cmds, f"check_for_updates never counted commits behind: {recorded}"
    assert count_cmds[0][-1] == f"HEAD..origin/{RELEASE_BRANCH}"
    assert "main" not in count_cmds[0][-1]


def test_git_banner_state_targets_the_release_branch(monkeypatch):
    """``_compute_git_banner_state`` (rendered on EVERY banner, not only the
    cached update-check path) must resolve its ``upstream`` reference and
    "ahead" count against our RELEASE branch, not a hardcoded ``main``.

    Behavioral: patches ``subprocess.run`` and asserts on the actual argv of
    the ``rev-parse``/``rev-list`` calls it makes.
    """
    import subprocess

    import hermes_cli.banner as banner
    from hermes_cli.release_source import RELEASE_BRANCH

    repo_dir = banner._resolve_repo_dir()
    assert repo_dir is not None, "this checkout has no .git -- test needs one"

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        if "rev-parse" in joined and "--short=8" in joined:
            # Distinguish the "upstream" lookup from the "local" (HEAD) one
            # by which ref was asked for.
            if cmd[-1] == "HEAD":
                return subprocess.CompletedProcess(cmd, 0, stdout="deadbee0\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="cafebee0\n", stderr="")
        if "rev-list" in joined and "--count" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Call the uncached, direct implementation with an explicit repo_dir --
    # bypasses the process-global ``_git_banner_state_cache`` that
    # ``get_git_banner_state()`` uses when called with no argument, which
    # would otherwise leak a stale result between tests in this file.
    state = banner._compute_git_banner_state(repo_dir)

    assert state == {"upstream": "cafebee0", "local": "deadbee0", "ahead": 1}

    rev_parse_upstream_cmds = [
        c for c in recorded
        if "rev-parse" in c and "--short=8" in c and c[-1] != "HEAD"
    ]
    assert rev_parse_upstream_cmds, f"never resolved an upstream ref: {recorded}"
    assert rev_parse_upstream_cmds[0][-1] == f"origin/{RELEASE_BRANCH}"
    assert "main" not in rev_parse_upstream_cmds[0][-1]

    count_cmds = [c for c in recorded if "rev-list" in c and "--count" in c]
    assert count_cmds, f"never counted commits ahead: {recorded}"
    assert count_cmds[0][-1] == f"origin/{RELEASE_BRANCH}..HEAD"
    assert "main" not in count_cmds[0][-1]
