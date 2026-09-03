"""Tests for cmd_update.

Covers the update-channel branch resolution (pinned to RELEASE_BRANCH — see
hermes_cli.release_source), what happens when that branch is missing
locally/on origin, and the surrounding non-branch update behavior (npm
lockfile caching, Termux uv bootstrap, config-migration prompts, profile
skill sync).
"""

import hashlib
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from hermes_cli.main import cmd_update, PROJECT_ROOT
from hermes_cli.release_source import RELEASE_BRANCH, RELEASE_REPO_HTTPS


def _make_run_side_effect(branch="main", verify_ok=True, commit_count="0"):
    """Build a side_effect function for subprocess.run that simulates git commands.

    The origin response is pinned to our own release repo so callers pass
    the release-source guard's origin check (``_assert_release_source``) by
    default -- that guard is enforced unconditionally. ``branch`` still
    only feeds the "current branch" simulation; the guard itself no longer
    reads the branch at all (controller ruling, fix round 1: the branch
    refusal was dropped -- origin is the only property that carries the
    security guarantee, and the update flow's own checkout step already
    force-migrates any local branch onto RELEASE_BRANCH).
    """

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        # git remote get-url origin  (release-source guard's origin check)
        if "remote" in joined and "get-url" in joined and "origin" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{RELEASE_REPO_HTTPS}\n", stderr="")

        # git rev-parse --abbrev-ref HEAD  (get current branch)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{branch}\n", stderr="")

        # git rev-parse --verify origin/{branch}  (check remote branch exists)
        if "rev-parse" in joined and "--verify" in joined:
            rc = 0 if verify_ok else 128
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

        # git rev-list HEAD..origin/{branch} --count
        if "rev-list" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

        # Fallback: return a successful CompletedProcess with empty stdout
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return side_effect


@pytest.fixture
def mock_args():
    return SimpleNamespace()


# ---------------------------------------------------------------------------
# Managed-uv compatibility for tests that patch shutil.which
# ---------------------------------------------------------------------------
# The production code now uses ``ensure_uv()`` / ``update_managed_uv()``
# instead of ``shutil.which("uv")``.  Many tests in this file patch
# ``shutil.which`` to control whether uv is "available" — these autouse
# fixtures make the managed_uv functions delegate to the patched
# ``shutil.which`` so the existing test setup keeps working without
# per-test changes.
@pytest.fixture(autouse=True)
def _patch_managed_uv(request):
    """Make managed_uv helpers follow shutil.which mocking in tests."""
    import shutil

    # resolve_uv delegates to shutil.which("uv") so that test patches
    # on shutil.which flow through naturally.
    def _fake_resolve_uv():
        return shutil.which("uv")

    def _fake_ensure_uv(**_kwargs):
        return shutil.which("uv")

    def _fake_update_managed_uv(**_kwargs):
        return None  # never actually self-update in tests

    with patch("hermes_cli.managed_uv.resolve_uv", side_effect=_fake_resolve_uv), \
         patch("hermes_cli.managed_uv.ensure_uv", side_effect=_fake_ensure_uv), \
         patch("hermes_cli.managed_uv.update_managed_uv", side_effect=_fake_update_managed_uv):
        yield


class TestCmdUpdateNpmLockfileCache:
    @staticmethod
    def _cache_file(hermes_root, project_root):
        cache_key = hashlib.sha256(str(project_root).encode()).hexdigest()[:12]
        return hermes_root / f".npm_lock_hash_{cache_key}"



    def test_record_npm_lockfile_hash(self, tmp_path, monkeypatch):
        from hermes_cli import main as hm

        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        (tmp_path / "package-lock.json").write_text('{"lockfileVersion": 3}')

        hm._record_npm_lockfile_hash(tmp_path)

        assert (
            self._cache_file(tmp_path, tmp_path).read_text()
            == hm._npm_manifests_digest()
        )

    def test_package_json_only_edit_defeats_skip(self, tmp_path, monkeypatch):
        """Reviewer scenario (#61580): dev edits package.json WITHOUT running
        npm — lockfile unchanged. `hermes update` must still install (the
        npm-install fallback is what syncs node_modules in that state)."""
        from hermes_cli import main as hm

        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        (tmp_path / "package-lock.json").write_text('{"lockfileVersion": 3}')
        (tmp_path / "package.json").write_text('{"dependencies": {}}')
        (tmp_path / "node_modules").mkdir()
        hm._record_npm_lockfile_hash(tmp_path)
        assert hm._npm_lockfile_changed(tmp_path) is False

        (tmp_path / "package.json").write_text(
            '{"dependencies": {"left-pad": "^1.0.0"}}'
        )
        assert hm._npm_lockfile_changed(tmp_path) is True







    def test_update_uses_one_shared_npm_cache_across_profiles(
        self, tmp_path, monkeypatch
    ):
        """The npm cache describes checkout-global node_modules, not a profile."""
        from hermes_cli import main as hm
        import hermes_constants

        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "package.json").write_text("{}")
        shared_root = tmp_path / ".hermes"
        named_profile = shared_root / "profiles" / "work"
        named_profile.mkdir(parents=True)

        monkeypatch.setattr(hm, "PROJECT_ROOT", checkout)
        monkeypatch.setattr(hermes_constants.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            hermes_constants, "find_node_executable", lambda _name: "/usr/bin/npm"
        )

        cache_roots = []
        with patch.object(
            hm,
            "_npm_lockfile_changed",
            side_effect=lambda root: cache_roots.append(root) or False,
        ):
            monkeypatch.setenv("HERMES_HOME", str(shared_root))
            hm._update_node_dependencies()

            monkeypatch.setenv("HERMES_HOME", str(named_profile))
            hm._update_node_dependencies()

        assert cache_roots == [shared_root, shared_root]


class TestCmdUpdateTermuxUvBootstrap:
    """Regression tests for Termux-specific uv bootstrap behavior."""

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_termux_uv_bootstrap_uses_binary_only_install(
        self, mock_run, _mock_which, monkeypatch
    ):
        from hermes_cli import main as hm

        mock_run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        monkeypatch.setattr(hm, "_is_termux_env", lambda env=None: True)

        uv_bin = hm._ensure_uv_for_termux(["/termux/python", "-m", "pip"])

        assert uv_bin is None
        assert mock_run.call_count == 1
        assert mock_run.call_args.args[0] == [
            "/termux/python",
            "-m",
            "pip",
            "install",
            "uv",
            "--only-binary",
            ":all:",
        ]
        assert mock_run.call_args.kwargs["cwd"] == PROJECT_ROOT
        assert mock_run.call_args.kwargs["check"] is False

    @patch("subprocess.run")
    def test_termux_reuses_existing_path_uv_without_pip(self, mock_run, monkeypatch):
        """A uv already on PATH (e.g. ``pkg install uv``) is reused before pip runs."""
        from hermes_cli import main as hm

        pkg_uv = "/data/data/com.termux/files/usr/bin/uv"
        monkeypatch.setattr(hm, "_is_termux_env", lambda env=None: True)
        # Production resolve_uv only checks $HERMES_HOME/bin/uv; model an empty
        # managed dir so the PATH probe is what surfaces the packaged uv.
        monkeypatch.setattr("hermes_cli.managed_uv.resolve_uv", lambda: None)
        monkeypatch.setattr("shutil.which", lambda name: pkg_uv if name == "uv" else None)

        uv_bin = hm._ensure_uv_for_termux(["/termux/python", "-m", "pip"])

        assert uv_bin == pkg_uv
        mock_run.assert_not_called()


class TestCmdUpdateNonInteractiveMigrations:
    """cmd_update applies safe config migrations without prompting.

    Formerly ``TestCmdUpdateBranchFallback``: that class also carried a
    regression test for the upstream-fork-vs-``main`` check
    (``test_update_on_fork_checks_upstream_when_origin_up_to_date``). The
    channel is now pinned to the release branch (see
    ``hermes_cli.release_source.RELEASE_BRANCH``) and
    ``_resolve_update_branch`` never returns ``"main"`` — the ``branch ==
    "main"`` special-casing that test exercised can no longer be reached
    through ``cmd_update``, so the test was removed rather than kept red.
    """

    def test_update_non_interactive_runs_safe_config_migrations(self, mock_args, capsys):
        """Dashboard/web updates apply non-interactive migrations before restart."""
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input") as mock_input, patch(
            "hermes_cli.config.get_missing_env_vars", return_value=["MISSING_KEY"]
        ), patch(
            "hermes_cli.config.get_missing_config_fields",
            return_value=[{"key": "new.option", "default": True}],
        ), patch(
            "hermes_cli.update_cmd._reload_config_modules"
        ), patch(
            "hermes_cli.update_cmd._run_config_check_fresh", return_value=(1, 2)
        ), patch(
            "hermes_cli.update_cmd._run_migrate_config_fresh",
            return_value={"env_added": [], "config_added": ["new.option"]},
        ) as migrate_config, patch("hermes_cli.main.sys") as mock_sys:
            mock_sys.stdin.isatty.return_value = False
            mock_sys.stdout.isatty.return_value = False
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )

            cmd_update(mock_args)

            mock_input.assert_not_called()
            migrate_config.assert_called_once_with(interactive=False, quiet=False)
            captured = capsys.readouterr()
            assert "applying safe config migrations" in captured.out
            assert "API keys require manual entry" in captured.out


class TestCmdUpdateMigrationPrompt:
    """The config-migration prompt names what changed and skips the prompt
    entirely when only the config format version moved.

    Regression guard for the contentless-prompt report (ScottFive / Tt2021):
    previously the prompt printed only counts ("1 new config option") and
    asked "configure them now?" even for pure version bumps, where saying
    yes looked like a no-op.
    """

    def test_version_bump_only_applies_silently_without_prompt(
        self, mock_args, capsys
    ):
        """Only the version moved → apply non-interactively, never prompt."""
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input") as mock_input, patch(
            "hermes_cli.config.get_missing_env_vars", return_value=[]
        ), patch(
            "hermes_cli.config.get_missing_config_fields", return_value=[]
        ), patch(
            "hermes_cli.update_cmd._reload_config_modules"
        ), patch(
            "hermes_cli.update_cmd._run_config_check_fresh", return_value=(5, 24)
        ), patch(
            "hermes_cli.update_cmd._run_migrate_config_fresh",
            return_value={"env_added": [], "config_added": [], "warnings": []},
        ) as mock_migrate:
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )

            cmd_update(mock_args)

            mock_input.assert_not_called()
            mock_migrate.assert_called_once_with(interactive=False, quiet=True)
            out = capsys.readouterr().out
            assert "Updating config format (v5 → v24)" in out
            assert "no new settings to configure" in out
            # The misleading question must NOT appear for a pure version bump.
            assert "configure them now" not in out.lower()

    def test_new_options_are_listed_by_name_before_prompt(
        self, mock_args, capsys
    ):
        """New env/config keys are printed by name so the user can decide."""
        env_items = [
            {"name": "FOO_API_KEY", "description": "Foo service API key"},
        ]
        cfg_items = [
            {"key": "display.new_widget", "description": "New config option: display.new_widget"},
        ]
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input", return_value="n"), patch(
            "hermes_cli.config.get_missing_env_vars", return_value=env_items
        ), patch(
            "hermes_cli.config.get_missing_config_fields", return_value=cfg_items
        ), patch(
            "hermes_cli.update_cmd._reload_config_modules"
        ), patch(
            "hermes_cli.update_cmd._run_config_check_fresh", return_value=(1, 24)
        ), patch(
            "hermes_cli.update_cmd._run_migrate_config_fresh",
            return_value={"env_added": [], "config_added": [], "warnings": []},
        ), patch("hermes_cli.main.sys") as mock_sys:
            mock_sys.stdin.isatty.return_value = True
            mock_sys.stdout.isatty.return_value = True
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )

            cmd_update(mock_args)

            out = capsys.readouterr().out
            # Names, not just counts.
            assert "FOO_API_KEY" in out
            assert "Foo service API key" in out
            assert "display.new_widget" in out


class TestCmdUpdateSyncsTrixConfigWithoutMigration:
    """Critical regression, spec9-task11 review round 1.

    Spec 9 deliberately did NOT bump ``_config_version`` when it added new
    trix-config.yaml sections (display, approvals, platform_hints,
    gateway, terminal.docker_extra_args, ...). That means an
    already-installed client's config.yaml — missing those sections —
    still reports ``check_config_version() == (current, current)``: no
    migration needed, the "Configuration is up to date" branch fires, and
    ``_run_migrate_config_fresh`` is never called. An earlier revision of
    this fix ran the section-sync from INSIDE
    ``_run_migrate_config_fresh``, which this test proves never executes
    for exactly this client. The sync must run unconditionally.
    """

    def test_up_to_date_config_still_gets_new_sections(self, mock_args, capsys):
        from hermes_cli.config import get_config_path
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        current_ver = DEFAULT_CONFIG["_config_version"]

        config_path = get_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "terminal:\n"
            "  backend: docker\n"
            "  cwd: /workspace\n"
            "\n"
            "web:\n"
            "  search_backend: ddgs\n"
            "\n"
            f"_config_version: {current_ver}\n",
            encoding="utf-8",
        )

        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input") as mock_input, patch(
            "hermes_cli.config.get_missing_env_vars", return_value=[]
        ), patch(
            "hermes_cli.config.get_missing_config_fields", return_value=[]
        ), patch(
            "hermes_cli.update_cmd._reload_config_modules"
        ), patch(
            "hermes_cli.update_cmd._run_config_check_fresh",
            return_value=(current_ver, current_ver),
        ), patch(
            "hermes_cli.update_cmd._run_migrate_config_fresh"
        ) as mock_migrate:
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )

            cmd_update(mock_args)

            mock_input.assert_not_called()
            mock_migrate.assert_not_called()  # exactly the branch this bug hid in

        out = capsys.readouterr().out
        assert "Configuration is up to date" in out
        assert "Дописаны новые настройки" in out

        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert "display" in data, (
            "check_config_version() reported no migration needed, but "
            "trix_config_sync must still splice in the client-template "
            "sections spec 9 added without a version bump — this is "
            "exactly what the review-round-1 Critical fix restores"
        )
        assert data["_config_version"] == current_ver, "sync must not touch the version"


class TestConfigVersionCheckUsesFreshModules:
    """Regression: config migration must use freshly-reloaded modules, not the
    sys.modules cache from before git pull.

    Before the fix, ``hermes update`` ran in the PRE-pull Python process.
    After ``git pull`` updated the source on disk, function-level imports
    returned the OLD cached ``hermes_cli.config`` module — so
    ``DEFAULT_CONFIG["_config_version"]`` was stale and
    ``check_config_version()`` reported ``(33, 33)`` "up to date" even though
    the freshly-pulled code had v34 with a migration to run. The personality
    reset migration (#81946) was silently skipped this way.
    """

    def test_run_config_check_fresh_reloads_modules(self):
        """_run_config_check_fresh must call _reload_config_modules which
        force-reloads the config modules from disk.

        Regression: config migration was silently skipped because
        sys.modules held the OLD hermes_cli.config with the OLD
        DEFAULT_CONFIG["_config_version"] after git pull.
        """
        from unittest.mock import patch

        import hermes_cli.update_cmd as update_cmd

        with patch.object(update_cmd, "_reload_config_modules") as mock_reload:
            update_cmd._run_config_check_fresh()

        mock_reload.assert_called_once()


class TestCmdUpdateProfileSkillSync:
    """cmd_update syncs bundled skills to all profiles, including the active one.

    Regression guard for #16176: previously the active profile was excluded
    from the seed_profile_skills loop, leaving it on stale skill content.
    """

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_active_profile_included_in_skill_sync(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        from pathlib import Path

        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="1"
        )

        default_p = SimpleNamespace(name="default", path=Path("/fake/.hermes"))
        active_p = SimpleNamespace(name="bit", path=Path("/fake/.hermes/profiles/bit"))
        other_p = SimpleNamespace(name="work", path=Path("/fake/.hermes/profiles/work"))
        all_profiles = [default_p, active_p, other_p]

        synced_paths = []

        def fake_seed(path, quiet=False):
            synced_paths.append(path)
            return {"copied": [], "updated": [], "user_modified": []}

        empty_sync = {"copied": [], "updated": [], "user_modified": [], "cleaned": []}

        with (
            patch("hermes_cli.profiles.list_profiles", return_value=all_profiles),
            patch("hermes_cli.profiles.seed_profile_skills", side_effect=fake_seed),
            patch("tools.skills_sync.sync_skills", return_value=empty_sync),
        ):
            cmd_update(mock_args)

        assert active_p.path in synced_paths, (
            f"Active profile 'bit' must be included in skill sync; got: {synced_paths}"
        )
        assert set(synced_paths) == {p.path for p in all_profiles}, (
            f"All profiles must be synced; got: {synced_paths}"
        )

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_single_profile_default_is_synced(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        from pathlib import Path

        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="1"
        )

        default_p = SimpleNamespace(name="default", path=Path("/fake/.hermes"))
        synced_paths = []

        def fake_seed(path, quiet=False):
            synced_paths.append(path)
            return {"copied": [], "updated": [], "user_modified": []}

        empty_sync = {"copied": [], "updated": [], "user_modified": [], "cleaned": []}

        with (
            patch("hermes_cli.profiles.list_profiles", return_value=[default_p]),
            patch("hermes_cli.profiles.seed_profile_skills", side_effect=fake_seed),
            patch("tools.skills_sync.sync_skills", return_value=empty_sync),
        ):
            cmd_update(mock_args)

        assert default_p.path in synced_paths


class TestResolvedBranchReachesGitUpdate:
    """The resolved update channel must actually reach the git plumbing.

    Restored from the (deleted) ``TestCmdUpdateBranchFlag``, which drove
    these through the removed ``--branch`` flag. ``_resolve_update_branch``
    no longer reads ``args`` at all, but its RELEASE_BRANCH return value
    must still flow all the way through to ``git rev-list`` and
    ``git merge --ff-only`` (hermes_cli/update_cmd.py) rather than silently
    regressing to origin/main — exactly the leak this task exists to
    prevent. A resolver-only unit test can't catch that regression; only an
    end-to-end run of ``cmd_update`` through mocked subprocess calls can.
    """

    def _branch_side_effect(self, current_branch, target_branch, *, checkout_fails=False, track_fails=False, commit_count="0"):
        """Mock side-effect that knows about checkout/track behavior.

        - ``current_branch``  what ``git rev-parse --abbrev-ref HEAD`` returns
        - ``target_branch``   the resolved release-channel branch; what we
                              expect the code to switch to
        - ``checkout_fails``  if True, ``git checkout <target>`` returns non-zero
                              (simulates branch absent locally; code should retry with -B)
        - ``track_fails``     if True, ``git checkout -B <target> origin/<target>`` ALSO fails
                              (simulates branch absent on origin too)
        - ``commit_count``    rev-list count returned (0 = up-to-date, >0 = behind)

        Origin always answers as our own release repo — these tests are
        about branch resolution, not the release-source origin guard.
        """

        def side_effect(cmd, **kwargs):
            joined = " ".join(str(c) for c in cmd)

            if "remote" in joined and "get-url" in joined and "origin" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{RELEASE_REPO_HTTPS}\n", stderr="")

            if "rev-parse" in joined and "--abbrev-ref" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{current_branch}\n", stderr="")

            if "checkout" in joined and "-B" in joined:
                rc = 128 if track_fails else 0
                err = f"fatal: '{target_branch}' did not match any file(s) known to git\n" if track_fails else ""
                return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=err)

            if "checkout" in joined and "-B" not in joined and "rev-parse" not in joined:
                rc = 128 if checkout_fails else 0
                err = f"error: pathspec '{target_branch}' did not match\n" if checkout_fails else ""
                return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=err)

            if "rev-list" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        return side_effect

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_resolved_branch_reaches_rev_list_and_merge(self, mock_run, _mock_which, capsys):
        """The resolver's RELEASE_BRANCH value makes rev-list and merge target it."""
        mock_run.side_effect = self._branch_side_effect(
            current_branch=RELEASE_BRANCH, target_branch=RELEASE_BRANCH, commit_count="3"
        )
        args = SimpleNamespace()

        cmd_update(args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]

        # rev-list must compare against origin/<RELEASE_BRANCH>, not origin/main
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert any(f"origin/{RELEASE_BRANCH}" in c for c in rev_list_cmds), rev_list_cmds
        assert not any("origin/main" in c for c in rev_list_cmds), rev_list_cmds

        # the ff-only merge must target origin/<RELEASE_BRANCH>
        merge_cmds = [c for c in commands if "merge --ff-only" in c]
        assert any(
            f"origin/{RELEASE_BRANCH}" in c and "origin/main" not in c for c in merge_cmds
        ), merge_cmds

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_missing_release_branch_everywhere_exits_cleanly(self, mock_run, _mock_which, capsys):
        """If the release branch doesn't exist locally OR on origin, exit non-zero with a clear error.

        Reachable for every client whose local checkout predates the channel
        pin and is still sitting on a stale branch (e.g. 'main') the first
        time it updates — the pin makes this path MORE reachable, not less.
        ``_assert_release_source`` only checks origin, not the local branch
        (controller ruling, fix round 1): with origin verified, this
        self-heal checkout step is what carries clients on any branch
        forward onto RELEASE_BRANCH, and this is its failure mode when
        RELEASE_BRANCH exists nowhere to land on.
        """
        mock_run.side_effect = self._branch_side_effect(
            current_branch="main",
            target_branch=RELEASE_BRANCH,
            checkout_fails=True,
            track_fails=True,
            commit_count="0",
        )
        args = SimpleNamespace()

        with pytest.raises(SystemExit) as exc_info:
            cmd_update(args)
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        assert "does not exist locally or on origin" in out
        assert RELEASE_BRANCH in out


class TestResolvedBranchReachesCheckPath:
    """``hermes update --check`` compares against the resolved release branch.

    Restored from the (deleted) ``TestCmdUpdateCheckBranchFlag``, which
    drove these through the removed ``--branch`` flag. Narrowed to
    RELEASE_BRANCH: the non-default-branch arm these exercise
    (update_cmd.py's ``_cmd_update_check``) is now the ONLY arm ``--check``
    can take, since the resolver never returns "main" anymore.
    """

    def _check_side_effect(
        self,
        target_branch: str,
        *,
        verify_ok: bool = True,
        commit_count: str = "0",
        upstream_fetch_ok: bool = True,
    ):
        """Mock side-effect for the _cmd_update_check git pipeline.

        - ``target_branch``      what we expect compare ref to point at
        - ``verify_ok``          if False, ``git rev-parse --verify --quiet
                                 origin/<branch>`` fails (branch missing
                                 on origin)
        - ``commit_count``       rev-list count (0 = up-to-date)
        - ``upstream_fetch_ok``  if False, ``git fetch upstream`` fails
                                 (forces fallback to origin on branch==main)
        """

        def side_effect(cmd, **kwargs):
            joined = " ".join(str(c) for c in cmd)

            if "fetch" in joined and "upstream" in joined:
                rc = 0 if upstream_fetch_ok else 128
                err = "" if upstream_fetch_ok else "fatal: 'upstream' does not appear to be a git repository\n"
                return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=err)

            if "fetch" in joined and "origin" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            if "rev-parse" in joined and "--verify" in joined:
                rc = 0 if verify_ok else 1
                return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

            if "rev-list" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        return side_effect

    @patch("hermes_cli.config.detect_install_method", return_value="git")
    @patch("subprocess.run")
    def test_check_compares_against_release_origin_branch(
        self, mock_run, _mock_method, capsys
    ):
        """--check compares against origin/<RELEASE_BRANCH>, never origin/main."""
        mock_run.side_effect = self._check_side_effect(
            target_branch=RELEASE_BRANCH, verify_ok=True, commit_count="2"
        )
        args = SimpleNamespace(check=True)

        cmd_update(args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        # Non-main branch skips upstream probe entirely.
        assert not any("fetch" in c and "upstream" in c for c in commands), commands
        # Verify and rev-list both target origin/<RELEASE_BRANCH>.
        verify_cmds = [c for c in commands if "rev-parse" in c and "--verify" in c]
        assert any(f"origin/{RELEASE_BRANCH}" in c for c in verify_cmds), verify_cmds
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert any(f"origin/{RELEASE_BRANCH}" in c for c in rev_list_cmds), rev_list_cmds
        assert not any("origin/main" in c for c in rev_list_cmds), rev_list_cmds

    @patch("hermes_cli.config.detect_install_method", return_value="git")
    @patch("subprocess.run")
    def test_check_missing_release_branch_on_origin_exits_cleanly(
        self, mock_run, _mock_method, capsys
    ):
        """If origin/<RELEASE_BRANCH> doesn't exist, surface a friendly error and exit 1.

        Pre-fix this case raised CalledProcessError from rev-list's check=True
        and dumped a Python traceback to stdout.
        """
        mock_run.side_effect = self._check_side_effect(
            target_branch=RELEASE_BRANCH, verify_ok=False
        )
        args = SimpleNamespace(check=True)

        with pytest.raises(SystemExit) as exc_info:
            cmd_update(args)
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        # No raw Python traceback.
        assert "Traceback" not in out
        assert "CalledProcessError" not in out
        # Friendly message naming the branch.
        assert RELEASE_BRANCH in out
        assert "not found" in out

        # rev-list must never have been called once verify failed.
        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        assert not any("rev-list" in c for c in commands), commands


def test_is_termux_env_true_for_termux_prefix():
    from hermes_cli import main as hm

    assert hm._is_termux_env({"PREFIX": "/data/data/com.termux/files/usr"}) is True


def test_load_installable_optional_extras_supports_termux_group(tmp_path, monkeypatch):
    from hermes_cli import main as hm

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "x"
version = "0.0.0"

[project.optional-dependencies]
all = ["x[mcp]"]
termux-all = ["x[termux]", "x[mcp]"]
mcp = ["mcp>=1"]
termux = ["rich>=14"]
""".strip()
    )
    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)

    assert hm._load_installable_optional_extras(group="all") == ["mcp"]
    assert hm._load_installable_optional_extras(group="termux-all") == ["termux", "mcp"]


class TestNodeRuntimeNpmResolution:
    """Regression tests for #30271 — WSL must not run Windows npm against the
    Linux checkout, and a failed Node refresh must not report success."""






    def test_node_failure_returns_failed_labels_and_warns(
        self, tmp_path, monkeypatch, capsys
    ):
        from hermes_cli import main as hm

        (tmp_path / "package.json").write_text("{}")
        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(hm, "_resolve_node_runtime_npm", lambda: "/usr/bin/npm")
        monkeypatch.setattr(
            hm,
            "_run_npm_install_deterministic",
            lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr=""),
        )

        failed = hm._update_node_dependencies()
        assert failed == ["repo root"]
        out = capsys.readouterr().out
        assert "mixed state" in out



    def test_wsl_update_skips_windows_npm_build_paths(self, mock_args, monkeypatch):
        """A Windows-only npm on WSL must not reach web or desktop builds."""
        from hermes_cli import main as hm
        import hermes_constants

        windows_npm = "/mnt/c/Program Files/nodejs/npm"
        monkeypatch.setattr(hm, "_is_windows", lambda: False)
        monkeypatch.setattr(hermes_constants, "is_wsl", lambda: True)
        monkeypatch.setattr(
            hermes_constants,
            "find_node_executable",
            lambda command: windows_npm if command == "npm" else None,
        )
        monkeypatch.setattr(
            hm.shutil,
            "which",
            lambda command, path=None: windows_npm if command == "npm" else "/usr/bin/uv",
        )
        monkeypatch.setenv("PATH", "/mnt/c/Program Files/nodejs")

        with patch("subprocess.run") as mock_run, \
             patch.object(hm, "_web_ui_build_needed", return_value=True), \
             patch.object(hm, "_desktop_packaged_executable", return_value=None), \
             patch.object(hm, "_desktop_dist_exists", return_value=True), \
             patch.object(hm, "_run_npm_install_deterministic") as mock_npm_install, \
             patch.object(hm, "_run_with_idle_timeout") as mock_idle_build, \
             patch.object(hm, "_run_logged_subprocess") as mock_desktop_build:
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )
            cmd_update(mock_args)

        mock_npm_install.assert_not_called()
        mock_idle_build.assert_not_called()
        mock_desktop_build.assert_not_called()
        assert all(
            not call.args or not call.args[0] or call.args[0][0] != windows_npm
            for call in mock_run.call_args_list
        )
