"""``hermes business setup`` — деплой бизнес-режима (спека 21 §5, §6, §8).

Реальный временный HERMES_HOME, реальные каталоги профилей, реальный
клиентский шаблон (``assets/config/trix-config.yaml``) как фикстура
конфига — E2E-путь, а не моки построчного редактора.
"""

import json
import os
from pathlib import Path

import pytest
import yaml

from hermes_cli import trix_business as tb
from hermes_cli.trix_business import BusinessSetupError

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


@pytest.fixture()
def biz_env(tmp_path, monkeypatch):
    """Уже установленная машина: дефолтный профиль с реальным шаблоном
    конфига и .env, в котором мастер настройки уже записал администратора.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    (default_home / "config.yaml").write_text(
        TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (default_home / ".env").write_text(
        "TELEGRAM_ALLOWED_USERS=111222333\n", encoding="utf-8"
    )
    return default_home


def _run(default_home, **overrides):
    # company_root defaults to a path UNDER THIS TEST'S OWN tmp_path, never
    # the real "/srv/trix" — run_setup() now does a real (idempotent)
    # filesystem write under company_root (ensure_skills_dir_marker, spec
    # 21 §9 review, blocker 1 follow-up), so a literal "/srv/trix" default
    # would have every caller of `_run()` touch the real host filesystem
    # outside the test sandbox (and typically fail there: "/srv" is
    # root-owned or doesn't exist on most dev/CI machines).
    kwargs = dict(
        group_chat_id="-1001234567890",
        admin_thread_id="7",
        company_root=str(Path(default_home).parent / "srv" / "trix"),
        install_skills=False,
    )
    kwargs.update(overrides)
    return tb.run_setup(**kwargs)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _comment_lines(text: str) -> set:
    return {line for line in text.splitlines() if line.strip().startswith("#")}


class TestHappyPathAndIdempotency:
    def test_first_run_creates_profile_and_wires_everything(self, biz_env):
        result = _run(biz_env)

        assert result["created_profile"] is True
        assert result["profile_name"] == "system_admin"
        assert result["admin_config"]["changed"] is True
        assert result["default_config"]["changed"] is True
        assert result["pin_changed"] is True

        admin_home = biz_env / "profiles" / "system_admin"
        assert (admin_home / "config.yaml").exists()

    def test_second_run_is_a_no_op(self, biz_env):
        _run(biz_env)
        result = _run(biz_env)

        assert result["created_profile"] is False
        assert result["admin_config"]["changed"] is False
        assert result["default_config"]["changed"] is False
        assert result["pin_changed"] is False
        assert result["skills_wiring"] == [] or all(
            not r["changed"] for r in result["skills_wiring"]
        )

    def test_second_run_leaves_config_bytes_identical(self, biz_env):
        _run(biz_env)
        default_before = (biz_env / "config.yaml").read_text(encoding="utf-8")
        admin_path = biz_env / "profiles" / "system_admin" / "config.yaml"
        admin_before = admin_path.read_text(encoding="utf-8")

        _run(biz_env)

        assert (biz_env / "config.yaml").read_text(encoding="utf-8") == default_before
        assert admin_path.read_text(encoding="utf-8") == admin_before


class TestRouteAndMultiplexInvariant:
    def test_route_names_system_admin_in_default_profile(self, biz_env):
        _run(biz_env)
        data = _load(biz_env / "config.yaml")
        routes = data["gateway"]["profile_routes"]
        assert len(routes) == 1
        route = routes[0]
        assert route["platform"] == "telegram"
        assert route["chat_id"] == "-1001234567890"
        assert route["thread_id"] == "7"
        assert route["profile"] == "system_admin"

    def test_multiplex_flag_and_allowlist_always_set_together(self, biz_env):
        """Невозможно получить allowlist без флага, или маршрут на профиль,
        которого нет в allowlist — оба пишутся в одном прогоне."""
        _run(biz_env)
        data = _load(biz_env / "config.yaml")
        gw = data["gateway"]

        assert gw["multiplex_profiles"] is True
        assert "system_admin" in gw["multiplex_profile_allowlist"]
        routed_profile = gw["profile_routes"][0]["profile"]
        assert routed_profile in gw["multiplex_profile_allowlist"]

    def test_custom_profile_name_is_also_allowlisted_and_routed(self, biz_env):
        result = _run(biz_env, profile_name="ops_desk")
        data = _load(biz_env / "config.yaml")
        gw = data["gateway"]

        assert result["profile_name"] == "ops_desk"
        assert "ops_desk" in gw["multiplex_profile_allowlist"]
        assert gw["profile_routes"][0]["profile"] == "ops_desk"

    def test_conflicting_route_to_a_different_profile_is_refused(self, biz_env):
        _run(biz_env)
        with pytest.raises(BusinessSetupError):
            _run(biz_env, profile_name="another_admin")

    def test_multiplex_profiles_explicitly_false_refuses_loudly(self, biz_env):
        """Item 11: an operator who explicitly set multiplex_profiles: false
        would previously get a 'successful' setup with a dead route (the
        route is only ever exercised through multiplexing). Refuse instead
        of silently completing."""
        text = (biz_env / "config.yaml").read_text(encoding="utf-8")
        text = text.replace("gateway:\n", "gateway:\n  multiplex_profiles: false\n", 1)
        assert "multiplex_profiles: false" in text, "fixture replace didn't take — test is broken"
        (biz_env / "config.yaml").write_text(text, encoding="utf-8")

        with pytest.raises(BusinessSetupError, match="multiplex_profiles"):
            _run(biz_env)

        # And nothing was written on disk — the refusal happens before the
        # profile is even created (item 11: fail before any half-deploy).
        from hermes_cli.profiles import profile_exists

        assert not profile_exists("system_admin")


class TestAdminIdentity:
    def test_admin_id_comes_from_default_profile_env_not_an_argument(self, biz_env):
        (biz_env / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=999888777,@somebody\n", encoding="utf-8"
        )
        result = _run(biz_env)

        assert result["admin_id"] == "999888777"
        env_text = (biz_env / ".env").read_text(encoding="utf-8")
        assert "TELEGRAM_GROUP_ALLOWED_USERS=999888777" in env_text

    def test_group_allowed_chats_is_never_written(self, biz_env):
        _run(biz_env)
        env_text = (biz_env / ".env").read_text(encoding="utf-8")
        assert "TELEGRAM_GROUP_ALLOWED_CHATS" not in env_text

    def test_admin_pin_is_written_before_the_topic_route(self, biz_env, monkeypatch):
        """Ordering fix (spec 21 §6 review): if writing the .env pin ever
        failed partway through setup, a route-first ordering would leave a
        LIVE admin topic with no per-user check — and Telegram checks
        chat-level access (open to the whole group by default) before
        user-level access, so anyone in the group would reach the admin
        topic during that window. The pin must land before the route.
        """
        calls: list = []

        import hermes_cli.config as cfg_mod

        real_save_env_value = cfg_mod.save_env_value

        def _spy_save_env_value(key, value, *a, **kw):
            if key == "TELEGRAM_GROUP_ALLOWED_USERS":
                calls.append("pin")
            return real_save_env_value(key, value, *a, **kw)

        monkeypatch.setattr(cfg_mod, "save_env_value", _spy_save_env_value)

        real_apply_edits = tb._apply_edits

        def _spy_apply_edits(config_path, build_edits):
            if Path(config_path).parent == biz_env:
                calls.append("default_route")
            return real_apply_edits(config_path, build_edits)

        monkeypatch.setattr(tb, "_apply_edits", _spy_apply_edits)

        _run(biz_env)

        assert calls.index("pin") < calls.index("default_route"), calls


class TestRefusals:
    def test_refuses_when_wizard_never_ran(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        default_home = tmp_path / ".hermes"
        default_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        (default_home / "config.yaml").write_text(
            TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        # Нет .env вовсе -> нет TELEGRAM_ALLOWED_USERS.
        with pytest.raises(BusinessSetupError):
            _run(default_home)

    def test_refuses_when_gateway_not_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        default_home = tmp_path / ".hermes"
        default_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        # Нет config.yaml дефолтного профиля вовсе.
        with pytest.raises(BusinessSetupError):
            _run(default_home)

    @pytest.mark.parametrize("bad_chat_id", ["not-a-number", "", "12.5"])
    def test_refuses_bad_chat_id(self, biz_env, bad_chat_id):
        with pytest.raises(BusinessSetupError):
            _run(biz_env, group_chat_id=bad_chat_id)

    @pytest.mark.parametrize("bad_thread_id", ["not-a-number", "-5", "0", ""])
    def test_refuses_bad_thread_id(self, biz_env, bad_thread_id):
        with pytest.raises(BusinessSetupError):
            _run(biz_env, admin_thread_id=bad_thread_id)

    def test_refuses_relative_company_root(self, biz_env):
        with pytest.raises(BusinessSetupError):
            _run(biz_env, company_root="srv/trix")

    def test_refuses_default_as_profile_name(self, biz_env):
        with pytest.raises(BusinessSetupError):
            _run(biz_env, profile_name="default")

    def test_refuses_profile_name_colliding_with_hermes_subcommand(self, biz_env):
        with pytest.raises(BusinessSetupError):
            _run(biz_env, profile_name="gateway")


class TestCompanyRootUnwritable:
    """Spec 21 review, findings 1+2 — observed on a real client machine:
    ``hermes business setup --company-root /srv/trix`` under a root-owned
    ``/srv`` printed ~20 lines of ``PermissionError`` traceback from
    ``ensure_skills_dir_marker`` → ``skills_dir.mkdir(parents=True)`` —
    AFTER the profile, its config edits, and both contour cron jobs had
    already been written to disk. Uses a genuinely unwritable directory
    (``chmod 0o555``), not a mocked ``mkdir``, so the assertion exercises
    the real permission-denied path end to end.
    """

    @pytest.mark.skipif(os.name == "nt", reason="chmod is a no-op on Windows")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions"
    )
    def test_unwritable_root_raises_business_setup_error_not_raw_oserror(self, biz_env, tmp_path):
        readonly_parent = tmp_path / "srv_readonly"
        readonly_parent.mkdir()
        os.chmod(readonly_parent, 0o555)
        try:
            with pytest.raises(BusinessSetupError) as excinfo:
                tb.run_setup(
                    company_root=str(readonly_parent / "trix"),
                    install_skills=False,
                )
            # The whole point: a PermissionError/OSError must never escape
            # run_setup as itself — it has to come back as ONE readable
            # BusinessSetupError (finding 1: no raw traceback to the operator).
            assert not isinstance(excinfo.value, OSError)
            assert "рабочий каталог компании" in str(excinfo.value)
        finally:
            os.chmod(readonly_parent, 0o755)

    @pytest.mark.skipif(os.name == "nt", reason="chmod is a no-op on Windows")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions"
    )
    def test_unwritable_root_leaves_no_mutation_behind(self, biz_env, tmp_path):
        """Finding 2: the failure above must happen in the same early phase
        as pure input validation, BEFORE the first mutation — not after the
        profile, cron jobs, and default config have already been written."""
        default_config_before = (biz_env / "config.yaml").read_text(encoding="utf-8")

        readonly_parent = tmp_path / "srv_readonly"
        readonly_parent.mkdir()
        os.chmod(readonly_parent, 0o555)
        try:
            with pytest.raises(BusinessSetupError):
                tb.run_setup(
                    company_root=str(readonly_parent / "trix"),
                    install_skills=False,
                )
        finally:
            os.chmod(readonly_parent, 0o755)

        from hermes_cli.profiles import profile_exists

        # No profile was created at all...
        assert not profile_exists("system_admin")
        assert not (biz_env / "profiles" / "system_admin").exists()
        # ...so there is nowhere a cron job could have been registered into.
        # ...and the default profile's config.yaml is untouched, byte for byte
        # (no multiplex_profiles/multiplex_profile_allowlist writes either).
        assert (biz_env / "config.yaml").read_text(encoding="utf-8") == default_config_before


class TestDmMarkerNotArmedOnFailure:
    """Spec 21 review, finding 3 — the worst of the five: a failed run on the
    live machine still left a pending-completion marker naming an
    administrator (in that incident, the CLIENT's Telegram id — the default
    "first id in the allow-list"). Had the gateway restarted, it would have
    created an administration DM topic for that id unattended — nobody had
    asked for it. The marker must not be written until the run that writes
    it has actually succeeded.
    """

    def test_late_failure_after_marker_would_be_queued_leaves_no_marker(
        self, biz_env, tmp_path, monkeypatch
    ):
        """Simulates a failure in the contour-executor wiring step, which
        runs AFTER the DM marker used to be written (spec 21 §8's
        `_write_dm_topic_pending_marker` used to happen mid-run, inside the
        administrator loop, well before `_wire_contour_executor`). Under the
        old ordering this left a live, armed marker on disk even though the
        run as a whole never completed — under the fix, the marker write is
        deferred until after every other mutation has succeeded, so a late
        failure here must leave no marker at all."""

        def _boom(*_args, **_kwargs):
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(tb, "_wire_contour_executor", _boom)

        with pytest.raises(BusinessSetupError):
            tb.run_setup(
                company_root=str(tmp_path / "srv" / "trix"),
                install_skills=False,
            )

        admin_home = biz_env / "profiles" / "system_admin"
        marker_path = admin_home / "business_setup" / "pending_dm_topic.json"
        assert not marker_path.exists()

    def test_successful_run_still_writes_the_marker(self, biz_env, tmp_path):
        """Sanity check for the test above: the deferred write still
        actually happens once the run succeeds — this isn't testing that the
        marker is now broken, only that it is no longer armed early."""
        result = tb.run_setup(
            company_root=str(tmp_path / "srv" / "trix"),
            install_skills=False,
        )
        assert result["dm_topic_pending"] is True

        admin_home = biz_env / "profiles" / "system_admin"
        marker_path = admin_home / "business_setup" / "pending_dm_topic.json"
        assert marker_path.is_file()


class TestSystemAdminConfigContents:
    def test_exact_toolset_and_deny_rules(self, biz_env):
        _run(biz_env)
        data = _load(biz_env / "profiles" / "system_admin" / "config.yaml")

        assert data["platform_toolsets"]["telegram"] == list(tb.SYSTEM_ADMIN_TOOLSETS)
        assert set(data["approvals"]["deny"]) == set(tb.SYSTEM_ADMIN_DENY_PATTERNS)
        assert data["terminal"]["backend"] == "docker"

    def test_no_disallowed_toolset_leaks_in(self, biz_env):
        _run(biz_env)
        data = _load(biz_env / "profiles" / "system_admin" / "config.yaml")
        toolsets = set(data["platform_toolsets"]["telegram"])
        disallowed = {"delegation", "cronjob", "memory", "session_search", "browser", "kanban", "mcp"}
        assert not (toolsets & disallowed)


class TestCommentsAndOnlyIntendedKeysChange:
    def test_every_template_comment_survives_in_both_configs(self, biz_env):
        before_default = (biz_env / "config.yaml").read_text(encoding="utf-8")
        before_comments = _comment_lines(before_default)

        _run(biz_env)

        after_default = (biz_env / "config.yaml").read_text(encoding="utf-8")
        after_admin = (biz_env / "profiles" / "system_admin" / "config.yaml").read_text(
            encoding="utf-8"
        )
        assert before_comments <= _comment_lines(after_default)
        assert before_comments <= _comment_lines(after_admin)

    def test_only_intended_paths_differ_in_default_config(self, biz_env):
        before = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        _run(biz_env)
        after = _load(biz_env / "config.yaml")

        intended_top_level = {"gateway", "skills"}
        for key in before:
            if key in intended_top_level:
                continue
            assert after.get(key) == before.get(key), f"unexpected change under {key!r}"

    def test_only_intended_paths_differ_in_admin_config(self, biz_env):
        before = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        _run(biz_env)
        after = _load(biz_env / "profiles" / "system_admin" / "config.yaml")

        intended_top_level = {"platform_toolsets", "approvals", "terminal", "skills"}
        for key in before:
            if key in intended_top_level:
                continue
            assert after.get(key) == before.get(key), f"unexpected change under {key!r}"

        # terminal.* — only .backend is allowed to move.
        before_terminal = dict(before.get("terminal") or {})
        after_terminal = dict(after.get("terminal") or {})
        before_terminal.pop("backend", None)
        after_terminal.pop("backend", None)
        assert before_terminal == after_terminal


class TestSharedSkillsWiring:
    def test_skills_external_dirs_points_at_company_root(self, biz_env, tmp_path):
        root = tmp_path / "srv" / "trix"
        _run(biz_env, company_root=str(root))
        for cfg in (
            biz_env / "config.yaml",
            biz_env / "profiles" / "system_admin" / "config.yaml",
        ):
            data = _load(cfg)
            assert f"{root}/skills" in data["skills"]["external_dirs"]

    def test_wiring_covers_every_existing_profile(self, biz_env, tmp_path):
        from hermes_cli.profiles import create_profile

        create_profile("sales")
        root = tmp_path / "srv" / "trix"
        _run(biz_env, company_root=str(root))

        sales_config = biz_env / "profiles" / "sales" / "config.yaml"
        data = _load(sales_config)
        assert f"{root}/skills" in data["skills"]["external_dirs"]

    def test_one_hand_edited_profile_does_not_abort_the_whole_setup(self, biz_env):
        """Item 11 (half-deploy fix): the shared-skills wiring loop runs
        LAST and best-effort, per profile. A profile whose config.yaml
        `_apply_edits` refuses to touch (here: not a YAML mapping at all)
        must not prevent the admin allowlist / launchers / cron jobs —
        the load-bearing steps — from being wired up, and must not raise
        out of run_setup at all."""
        from hermes_cli.profiles import create_profile

        create_profile("broken")
        broken_config = biz_env / "profiles" / "broken" / "config.yaml"
        broken_config.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

        result = _run(biz_env)  # must NOT raise

        broken_report = next(r for r in result["skills_wiring"] if r["profile"] == "broken")
        assert broken_report.get("error")
        assert broken_config.read_text(encoding="utf-8") == "- not\n- a\n- mapping\n"

        # The load-bearing contour wiring still happened.
        admin_home = biz_env / "profiles" / "system_admin"
        assert (admin_home / "trix_contour" / "admin_profiles.json").is_file()
        from cron.jobs import list_jobs, use_cron_store

        with use_cron_store(admin_home):
            jobs = list_jobs(include_disabled=True)
        assert len(jobs) == 2


class TestDmTopicPath:
    """DM-path (spec 21 §5/§8 review): no --group-chat-id/--admin-thread-id
    means the bot creates the "Администрирование" topic itself in the
    administrator's DM. thread_id isn't known at CLI time, so this only
    records a pending marker — the route is written later, by the gateway's
    completion step (see tests/gateway/test_business_dm_topic_completion.py).
    """

    def _dm_run(self, default_home, tmp_path, **overrides):
        kwargs = dict(
            company_root=str(tmp_path / "srv" / "trix"),
            install_skills=False,
        )
        kwargs.update(overrides)
        return tb.run_setup(**kwargs)

    def test_dm_path_succeeds_with_only_company_root(self, biz_env, tmp_path):
        result = self._dm_run(biz_env, tmp_path)

        assert result["is_group_path"] is False
        assert result["dm_topic_pending"] is True
        assert result["chat_id"] == "111222333"  # the admin's own Telegram id
        assert result["thread_id"] is None

    def test_dm_path_writes_no_group_key_at_all(self, biz_env, tmp_path):
        self._dm_run(biz_env, tmp_path)
        env_text = (biz_env / ".env").read_text(encoding="utf-8")
        assert "TELEGRAM_GROUP_ALLOWED_USERS" not in env_text
        assert "TELEGRAM_GROUP_ALLOWED_CHATS" not in env_text

    def test_dm_path_leaves_pending_completion_marker(self, biz_env, tmp_path):
        result = self._dm_run(biz_env, tmp_path)
        admin_home = biz_env / "profiles" / "system_admin"
        marker_path = admin_home / "business_setup" / "pending_dm_topic.json"

        assert marker_path.is_file()
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert marker["admin_profile"] == "system_admin"
        assert len(marker["pending"]) == 1
        assert marker["pending"][0]["chat_id"] == result["chat_id"]
        assert marker["pending"][0]["route_name"] == result["route_name"]
        assert marker["pending"][0]["dm_fallback_notified"] is False

    def test_dm_path_writes_no_route_yet(self, biz_env, tmp_path):
        self._dm_run(biz_env, tmp_path)
        data = _load(biz_env / "config.yaml")
        assert "profile_routes" not in data.get("gateway", {})

    def test_dm_path_still_sets_multiplex_flag_and_allowlist(self, biz_env, tmp_path):
        self._dm_run(biz_env, tmp_path)
        data = _load(biz_env / "config.yaml")
        gw = data["gateway"]
        assert gw["multiplex_profiles"] is True
        assert "system_admin" in gw["multiplex_profile_allowlist"]

    def test_dm_path_second_run_is_idempotent(self, biz_env, tmp_path):
        first = self._dm_run(biz_env, tmp_path)
        admin_home = biz_env / "profiles" / "system_admin"
        marker_path = admin_home / "business_setup" / "pending_dm_topic.json"
        before = marker_path.read_text(encoding="utf-8")

        second = self._dm_run(biz_env, tmp_path)

        assert marker_path.read_text(encoding="utf-8") == before
        assert second["created_profile"] is False
        assert second["dm_topic_pending"] is True
        assert second["default_config"]["changed"] is False

    def test_dm_path_marker_cleared_once_route_exists(self, biz_env, tmp_path):
        """Simulates the gateway's completion step having already run on an
        earlier startup: a route for this admin_profile/chat_id is already in
        default config.yaml. A re-run of `hermes business setup` must treat
        this as done and clear any leftover marker, not re-request it."""
        result = self._dm_run(biz_env, tmp_path)
        admin_home = biz_env / "profiles" / "system_admin"
        marker_path = admin_home / "business_setup" / "pending_dm_topic.json"
        assert marker_path.is_file()

        text = (biz_env / "config.yaml").read_text(encoding="utf-8")
        text = text.replace(
            "gateway:\n",
            "gateway:\n  profile_routes:\n"
            f"    - {{name: t, platform: telegram, chat_id: '{result['chat_id']}', "
            "thread_id: '999', profile: system_admin}\n",
            1,
        )
        (biz_env / "config.yaml").write_text(text, encoding="utf-8")

        second = self._dm_run(biz_env, tmp_path)

        assert second["dm_topic_pending"] is False
        assert not marker_path.exists()

    def test_only_one_of_the_two_group_args_is_refused(self, biz_env, tmp_path):
        with pytest.raises(BusinessSetupError):
            self._dm_run(biz_env, tmp_path, group_chat_id="-1001234567890")
        with pytest.raises(BusinessSetupError):
            self._dm_run(biz_env, tmp_path, admin_thread_id="7")

    def test_group_path_still_works_and_pins_per_user(self, biz_env, tmp_path):
        """The group path stays available for the fallback case (client
        Telegram doesn't render DM topics) — both ids still pin per-user and
        never write TELEGRAM_GROUP_ALLOWED_CHATS."""
        result = _run(biz_env)  # helper defaults to group_chat_id + admin_thread_id

        assert result["is_group_path"] is True
        assert result["dm_topic_pending"] is False
        env_text = (biz_env / ".env").read_text(encoding="utf-8")
        assert "TELEGRAM_GROUP_ALLOWED_USERS=111222333" in env_text
        assert "TELEGRAM_GROUP_ALLOWED_CHATS" not in env_text
        data = _load(biz_env / "config.yaml")
        assert data["gateway"]["profile_routes"][0]["thread_id"] == "7"


class TestExplicitAdminTelegramId:
    """``--admin-telegram-id`` (spec 21 §6 review, "who administers"): the
    first-allowed-id default is a bad guess for a machine where the person
    who received install credentials isn't the one who administers it. This
    covers explicit selection AND the multi-administrator "add, don't
    replace" contract (task B of the brief).
    """

    def _dm_run(self, default_home, tmp_path, **overrides):
        kwargs = dict(company_root=str(tmp_path / "srv" / "trix"), install_skills=False)
        kwargs.update(overrides)
        return tb.run_setup(**kwargs)

    def test_unknown_id_is_refused_naming_it_before_any_write(self, biz_env, tmp_path):
        from hermes_cli.profiles import profile_exists

        with pytest.raises(BusinessSetupError, match="999999999"):
            self._dm_run(biz_env, tmp_path, admin_telegram_id=["999999999"])

        assert not profile_exists("system_admin")

    def test_non_numeric_id_is_refused(self, biz_env, tmp_path):
        with pytest.raises(BusinessSetupError, match="@somebody"):
            self._dm_run(biz_env, tmp_path, admin_telegram_id=["@somebody"])

    def test_omitting_the_argument_still_picks_the_first_allowed_id(self, biz_env, tmp_path):
        """No behaviour change for the existing single-administrator case."""
        result = self._dm_run(biz_env, tmp_path, admin_telegram_id=None)
        assert result["admin_id"] == "111222333"
        assert result["administrators"] == [{
            "admin_id": "111222333",
            "chat_id": "111222333",
            "thread_id": None,
            "route_name": result["route_name"],
            "dm_topic_pending": True,
            "dm_topic_marker": result["dm_topic_marker"],
        }]

    def test_explicit_single_id_that_is_allowed_succeeds(self, biz_env, tmp_path):
        (biz_env / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=111222333,444555666\n", encoding="utf-8"
        )
        result = self._dm_run(biz_env, tmp_path, admin_telegram_id=["444555666"])
        assert result["admin_id"] == "444555666"
        assert result["chat_id"] == "444555666"

    def test_two_ids_in_one_invocation_queue_two_completions(self, biz_env, tmp_path):
        (biz_env / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=111222333,444555666\n", encoding="utf-8"
        )
        result = self._dm_run(
            biz_env, tmp_path, admin_telegram_id=["111222333", "444555666"],
        )

        assert result["is_group_path"] is False
        admins = result["administrators"]
        assert {a["admin_id"] for a in admins} == {"111222333", "444555666"}
        assert {a["chat_id"] for a in admins} == {"111222333", "444555666"}
        assert all(a["dm_topic_pending"] for a in admins)
        # Distinct route names — two administrators sharing one profile must
        # never collide on one route name.
        route_names = [a["route_name"] for a in admins]
        assert len(set(route_names)) == 2

        admin_home = biz_env / "profiles" / "system_admin"
        marker_path = admin_home / "business_setup" / "pending_dm_topic.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert marker["admin_profile"] == "system_admin"
        assert len(marker["pending"]) == 2
        assert {e["chat_id"] for e in marker["pending"]} == {"111222333", "444555666"}
        assert len({e["route_name"] for e in marker["pending"]}) == 2

        # A single profile/cron/allowlist deploy — not one per administrator.
        from cron.jobs import list_jobs, use_cron_store

        with use_cron_store(admin_home):
            jobs = list_jobs(include_disabled=True)
        assert len(jobs) == 2  # apply + push, not doubled

    def test_second_invocation_adds_administrator_without_touching_the_first(
        self, biz_env, tmp_path,
    ):
        (biz_env / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=111222333,444555666\n", encoding="utf-8"
        )
        first = self._dm_run(biz_env, tmp_path, admin_telegram_id=["111222333"])
        admin_home = biz_env / "profiles" / "system_admin"
        marker_path = admin_home / "business_setup" / "pending_dm_topic.json"
        allowlist_path = admin_home / "trix_contour" / "admin_profiles.json"
        allowlist_before = allowlist_path.read_text(encoding="utf-8")

        from cron.jobs import list_jobs, use_cron_store

        with use_cron_store(admin_home):
            jobs_before = list_jobs(include_disabled=True)

        second = self._dm_run(biz_env, tmp_path, admin_telegram_id=["444555666"])

        assert second["created_profile"] is False
        assert second["default_config"]["changed"] is False  # multiplexing already set up

        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert len(marker["pending"]) == 2
        chat_ids = {e["chat_id"] for e in marker["pending"]}
        assert chat_ids == {"111222333", "444555666"}
        first_entry = next(e for e in marker["pending"] if e["chat_id"] == "111222333")
        assert first_entry["route_name"] == first["route_name"]
        assert first_entry["dm_fallback_notified"] is False

        # Allowlist and cron jobs are untouched — still exactly one
        # administrator profile / apply+push pair, not duplicated.
        assert allowlist_path.read_text(encoding="utf-8") == allowlist_before
        with use_cron_store(admin_home):
            jobs_after = list_jobs(include_disabled=True)
        assert len(jobs_after) == len(jobs_before) == 2

    def test_group_path_pins_the_explicit_single_id(self, biz_env, tmp_path):
        (biz_env / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=111222333,444555666\n", encoding="utf-8"
        )
        result = tb.run_setup(
            group_chat_id="-1001234567890",
            admin_thread_id="7",
            company_root=str(tmp_path / "srv" / "trix"),
            install_skills=False,
            admin_telegram_id=["444555666"],
        )
        assert result["admin_id"] == "444555666"
        env_text = (biz_env / ".env").read_text(encoding="utf-8")
        assert "TELEGRAM_GROUP_ALLOWED_USERS=444555666" in env_text
        assert "TELEGRAM_GROUP_ALLOWED_CHATS" not in env_text

    def test_group_path_refuses_several_ids(self, biz_env, tmp_path):
        (biz_env / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=111222333,444555666\n", encoding="utf-8"
        )
        with pytest.raises(BusinessSetupError, match="--admin-telegram-id"):
            tb.run_setup(
                group_chat_id="-1001234567890",
                admin_thread_id="7",
                company_root=str(tmp_path / "srv" / "trix"),
                install_skills=False,
                admin_telegram_id=["111222333", "444555666"],
            )
        env_text = (biz_env / ".env").read_text(encoding="utf-8")
        assert "TELEGRAM_GROUP_ALLOWED_USERS" not in env_text

    def test_group_allowed_chats_is_still_never_written_with_explicit_id(self, biz_env, tmp_path):
        (biz_env / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=111222333,444555666\n", encoding="utf-8"
        )
        tb.run_setup(
            group_chat_id="-1001234567890",
            admin_thread_id="7",
            company_root=str(tmp_path / "srv" / "trix"),
            install_skills=False,
            admin_telegram_id=["444555666"],
        )
        env_text = (biz_env / ".env").read_text(encoding="utf-8")
        assert "TELEGRAM_GROUP_ALLOWED_CHATS" not in env_text


class TestTopicSlashCommandStaysDisabled:
    """The DM-topic mechanism this feature relies on (enable_telegram_topic_mode,
    the same call `/topic` makes) must NOT leak into the client-facing command
    surface — `/topic` stays operator/internal-only (see hermes_cli/trix_menu.py)."""

    def test_topic_is_in_disabled_commands(self):
        from hermes_cli.trix_menu import DISABLED_COMMANDS

        assert "topic" in DISABLED_COMMANDS


class TestContourWiring:
    """The gap this task closes (spec 21 §5/§8, CONTOUR_INTEGRATION_NOTE
    that used to live in trix_business.py): after setup, the system_admin
    profile must actually be wired to hermes_cli/trix_contour.py — the admin
    allowlist file exists where the executor reads it, and a real cron job
    applies pending requests. Every assertion here calls into trix_contour.py
    / cron.jobs.py for real rather than re-deriving their file formats.
    """

    def _admin_home(self, biz_env: Path) -> Path:
        return biz_env / "profiles" / "system_admin"

    def test_admin_allowlist_written_and_read_back_by_contour(self, biz_env, tmp_path):
        """Item 5 (spec 21 §6 review): `default` is the client's ordinary,
        outward-facing agent, NOT the contour administrator — the file must
        contain ONLY `system_admin`. Writing `default` here would hand that
        client-facing agent company:rw."""
        root = tmp_path / "srv" / "trix"
        _run(biz_env, company_root=str(root))
        admin_home = self._admin_home(biz_env)

        allowlist_path = admin_home / "trix_contour" / "admin_profiles.json"
        assert allowlist_path.is_file()
        on_disk = json.loads(allowlist_path.read_text(encoding="utf-8"))
        assert set(on_disk["admin_profiles"]) == {"system_admin"}
        assert "default" not in on_disk["admin_profiles"]

        # Call INTO trix_contour.load_admin_profiles() rather than trusting
        # our own JSON read above — it resolves the path itself via
        # get_hermes_home(), so this proves the file is where the real
        # executor looks, not just where we think it looks.
        from hermes_cli.trix_contour import load_admin_profiles
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(str(admin_home))
        try:
            admins = load_admin_profiles()
        finally:
            reset_hermes_home_override(token)
        assert admins == {"system_admin"}

    def test_admin_allowlist_strips_default_left_over_from_a_prior_buggy_run(self, biz_env, tmp_path):
        """Self-healing: if `default` is already sitting in the file (e.g.
        from before this fix), the next `hermes business setup` run must
        remove it rather than treat it as a deliberate customization."""
        root = tmp_path / "srv" / "trix"
        _run(biz_env, company_root=str(root))  # creates the profile for real
        admin_home = self._admin_home(biz_env)
        allowlist_path = admin_home / "trix_contour" / "admin_profiles.json"
        allowlist_path.write_text(
            json.dumps({"admin_profiles": ["default", "system_admin"]}), encoding="utf-8"
        )

        _run(biz_env, company_root=str(root))  # second, idempotent run

        on_disk = json.loads(allowlist_path.read_text(encoding="utf-8"))
        assert set(on_disk["admin_profiles"]) == {"system_admin"}

    def test_admin_allowlist_preserves_a_manually_added_other_profile(self, biz_env, tmp_path):
        """Union, not overwrite, for anything OTHER than `default`: a human
        who hand-added a second admin profile to the file must not have it
        silently removed on the next setup run."""
        root = tmp_path / "srv" / "trix"
        _run(biz_env, company_root=str(root))  # creates the profile for real
        admin_home = self._admin_home(biz_env)
        allowlist_path = admin_home / "trix_contour" / "admin_profiles.json"
        allowlist_path.write_text(
            json.dumps({"admin_profiles": ["some_other_admin"]}), encoding="utf-8"
        )

        _run(biz_env, company_root=str(root))  # second, idempotent run

        on_disk = json.loads(allowlist_path.read_text(encoding="utf-8"))
        assert set(on_disk["admin_profiles"]) == {"some_other_admin", "system_admin"}

    def test_cron_jobs_registered_no_agent_pointing_at_real_scripts(self, biz_env, tmp_path):
        root = tmp_path / "srv" / "trix"
        _run(biz_env, company_root=str(root))
        admin_home = self._admin_home(biz_env)

        from cron.jobs import list_jobs, use_cron_store

        with use_cron_store(admin_home):
            jobs = list_jobs(include_disabled=True)

        assert len(jobs) == 2
        names = {j["name"] for j in jobs}
        assert names == {tb.CONTOUR_APPLY_JOB_NAME, tb.CONTOUR_PUSH_JOB_NAME}
        for job in jobs:
            assert job["no_agent"] is True
            assert job["script"]
            # The script the job POINTS AT really exists inside the
            # system_admin profile's own scripts/ dir — the one place cron
            # is allowed to read a script from.
            assert (admin_home / "scripts" / job["script"]).is_file()

    def test_launcher_script_passes_crons_own_containment_check(self, biz_env, tmp_path):
        root = tmp_path / "srv" / "trix"
        _run(biz_env, company_root=str(root))
        admin_home = self._admin_home(biz_env)

        from cron.scheduler import _run_job_script
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(str(admin_home))
        try:
            ok, output = _run_job_script(tb.CONTOUR_APPLY_SCRIPT_NAME)
        finally:
            reset_hermes_home_override(token)

        # A path escaping HERMES_HOME/scripts/ fails with "Blocked: ..." and
        # never reaches Python — proves real containment validation ran and
        # passed, not just that the file happens to exist somewhere.
        assert "Blocked" not in output
        assert ok is True

    def test_rerun_is_idempotent_no_duplicate_jobs_or_allowlist_entries(self, biz_env, tmp_path):
        root = tmp_path / "srv" / "trix"
        _run(biz_env, company_root=str(root))
        _run(biz_env, company_root=str(root))
        admin_home = self._admin_home(biz_env)

        allowlist = json.loads(
            (admin_home / "trix_contour" / "admin_profiles.json").read_text(encoding="utf-8")
        )
        assert "default" not in allowlist["admin_profiles"]
        assert allowlist["admin_profiles"].count("system_admin") == 1

        from cron.jobs import list_jobs, use_cron_store

        with use_cron_store(admin_home):
            jobs = list_jobs(include_disabled=True)
        assert len(jobs) == 2

    def test_end_to_end_launcher_applies_a_real_request(self, biz_env, tmp_path):
        """Write a minimal valid request at the host-side workspace path (the
        same path the docker sandbox mounts as /workspace for this profile's
        top-level, non-delegated session — task_id 'default'), run the
        launcher's entry point directly, and confirm the request was really
        applied by run_executor — no mocking of the executor.
        """
        root = tmp_path / "srv" / "trix"
        _run(biz_env, company_root=str(root))
        admin_home = self._admin_home(biz_env)

        from cron.scheduler import _run_job_script
        from hermes_cli.trix_contour import CONTOUR_REQUEST_FILENAME
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from tools.environments.base import get_sandbox_dir

        token = set_hermes_home_override(str(admin_home))
        try:
            workspace = get_sandbox_dir() / "docker" / "default" / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            request_path = workspace / CONTOUR_REQUEST_FILENAME
            request_path.write_text(
                json.dumps({"when": "now", "ops": [{"op": "create_folder", "folder": "company"}]}),
                encoding="utf-8",
            )

            ok, output = _run_job_script(tb.CONTOUR_APPLY_SCRIPT_NAME)
        finally:
            reset_hermes_home_override(token)

        assert ok is True, output
        assert "applied" in output
        assert (root / "company").is_dir()
        assert not request_path.exists()  # consumed by run_executor
        result_file = request_path.with_name(request_path.name + ".result.txt")
        assert result_file.is_file()
