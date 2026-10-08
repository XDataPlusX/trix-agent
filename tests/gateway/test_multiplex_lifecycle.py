"""Phase 4: lifecycle guard + per-profile observability."""
import logging

import pytest

from gateway.config import GatewayConfig


class TestServedProfilesStatus:
    def test_write_and_read_served_profiles(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import importlib
        import gateway.status as status
        importlib.reload(status)
        try:
            status.write_runtime_status(
                gateway_state="running", served_profiles=["default", "coder"]
            )
            rec = status.read_runtime_status()
            assert rec.get("served_profiles") == ["default", "coder"]
        finally:
            importlib.reload(status)


def test_cron_profile_homes_follow_allowlist(tmp_path, monkeypatch):
    """The helper wired into in-process cron returns only selected profiles."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    for name in ("worker", "guest"):
        (default_home / "profiles" / name).mkdir(parents=True)

    import gateway.run as gateway_run

    homes = gateway_run._multiplex_profile_homes(
        GatewayConfig(
            multiplex_profiles=True,
            multiplex_profile_allowlist=["worker"],
        )
    )

    assert [name for name, _home in homes] == ["default", "worker"]


class TestNamedProfileMultiplexerGuard:
    """_guard_named_profile_under_multiplexer is inert unless all conditions hold."""


    def test_force_bypasses(self, monkeypatch):
        from hermes_cli import gateway as gw
        # Even if it looks like a named profile, force returns immediately.
        monkeypatch.setattr(gw, "_profile_suffix", lambda: "coder")
        gw._guard_named_profile_under_multiplexer(force=True)

    def test_inert_when_no_default_gateway_running(self, monkeypatch, tmp_path):
        from hermes_cli import gateway as gw
        monkeypatch.setattr(gw, "_profile_suffix", lambda: "coder")
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: tmp_path
        )
        # No gateway.pid in tmp_path => no running default gateway => no raise.
        gw._guard_named_profile_under_multiplexer(force=False)

    def _fake_running_default_gateway(self, monkeypatch, tmp_path):
        """Make the guard believe a live default gateway exists at tmp_path."""
        from hermes_cli import gateway as gw
        import gateway.status as status

        monkeypatch.setattr(gw, "_profile_suffix", lambda: "coder")
        monkeypatch.setattr(
            "hermes_constants.get_default_hermes_root", lambda: tmp_path
        )
        (tmp_path / "gateway.pid").write_text("12345", encoding="utf-8")
        monkeypatch.setattr(status, "_read_pid_record", lambda p: {"pid": 12345})
        monkeypatch.setattr(status, "_pid_from_record", lambda rec: 12345)
        monkeypatch.setattr(status, "_pid_exists", lambda pid: True)

    def test_unset_allowlist_preserves_historical_guard(self, monkeypatch, tmp_path):
        self._fake_running_default_gateway(monkeypatch, tmp_path)
        (tmp_path / "config.yaml").write_text(
            "gateway:\n  multiplex_profiles: true\n",
            encoding="utf-8",
        )

        from hermes_cli import gateway as gw

        with pytest.raises(SystemExit, match="1"):
            gw._guard_named_profile_under_multiplexer(force=False)

    def test_served_profile_is_still_guarded(self, monkeypatch, tmp_path):
        self._fake_running_default_gateway(monkeypatch, tmp_path)
        (tmp_path / "config.yaml").write_text(
            "gateway:\n"
            "  multiplex_profiles: true\n"
            "  multiplex_profile_allowlist:\n"
            "    - Coder\n",
            encoding="utf-8",
        )

        from hermes_cli import gateway as gw

        with pytest.raises(SystemExit, match="1"):
            gw._guard_named_profile_under_multiplexer(force=False)

    @pytest.mark.parametrize(
        "allowlist_yaml",
        ["[]", "[worker]", "coder"],
    )
    def test_unserved_profile_may_run_standalone(
        self, monkeypatch, tmp_path, allowlist_yaml
    ):
        self._fake_running_default_gateway(monkeypatch, tmp_path)
        (tmp_path / "config.yaml").write_text(
            "gateway:\n"
            "  multiplex_profiles: true\n"
            f"  multiplex_profile_allowlist: {allowlist_yaml}\n",
            encoding="utf-8",
        )

        from hermes_cli import gateway as gw

        gw._guard_named_profile_under_multiplexer(force=False)


class TestNamedProfileMultiplexWarning:
    """RAF-191: a named profile starting with multiplex on gets a loud WARNING
    (clone-detect), not a hard error."""

    def _named_profile(self, monkeypatch, tmp_path):
        from hermes_cli import gateway as gw

        monkeypatch.setattr(gw, "_profile_suffix", lambda: "operations")
        profile_home = tmp_path / "profiles" / "operations"
        profile_home.mkdir(parents=True)
        monkeypatch.setattr(
            "hermes_constants.get_hermes_home", lambda: profile_home
        )
        monkeypatch.delenv("GATEWAY_MULTIPLEX_PROFILES", raising=False)
        return gw, profile_home

    def test_warns_for_named_profile_with_multiplex_on(
        self, monkeypatch, tmp_path, caplog
    ):
        gw, home = self._named_profile(monkeypatch, tmp_path)
        (home / "config.yaml").write_text(
            "gateway:\n  multiplex_profiles: true\n", encoding="utf-8"
        )

        with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway"):
            gw._warn_multiplex_on_named_profile()

        assert "clone of the default multiplexer" in caplog.text

    def test_silent_for_default_profile(self, monkeypatch, tmp_path, caplog):
        from hermes_cli import gateway as gw

        monkeypatch.setattr(gw, "_profile_suffix", lambda: "")
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)
        monkeypatch.delenv("GATEWAY_MULTIPLEX_PROFILES", raising=False)
        (home / "config.yaml").write_text(
            "gateway:\n  multiplex_profiles: true\n", encoding="utf-8"
        )

        with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway"):
            gw._warn_multiplex_on_named_profile()

        assert "multiplex" not in caplog.text

    def test_silent_when_multiplex_off(self, monkeypatch, tmp_path, caplog):
        gw, home = self._named_profile(monkeypatch, tmp_path)
        (home / "config.yaml").write_text(
            "gateway:\n  multiplex_profiles: false\n", encoding="utf-8"
        )

        with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway"):
            gw._warn_multiplex_on_named_profile()

        assert "multiplexer" not in caplog.text

    def test_env_override_on_warns_without_config(self, monkeypatch, tmp_path, caplog):
        gw, home = self._named_profile(monkeypatch, tmp_path)
        monkeypatch.setenv("GATEWAY_MULTIPLEX_PROFILES", "1")

        with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway"):
            gw._warn_multiplex_on_named_profile()

        assert "clone of the default multiplexer" in caplog.text

    def test_env_override_off_silences_config_true(
        self, monkeypatch, tmp_path, caplog
    ):
        gw, home = self._named_profile(monkeypatch, tmp_path)
        (home / "config.yaml").write_text(
            "gateway:\n  multiplex_profiles: true\n", encoding="utf-8"
        )
        monkeypatch.setenv("GATEWAY_MULTIPLEX_PROFILES", "0")

        with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway"):
            gw._warn_multiplex_on_named_profile()

        assert "multiplexer" not in caplog.text


