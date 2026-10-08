"""Per-user default model (RAF-189) — store, resolution priority, UX.

Covers:
1. ``gateway.user_model_defaults`` — persistence, sanitization, user
   isolation, per-profile (hermes home) scoping, restart survival.
2. ``GatewayRunner._resolve_session_agent_runtime`` — priority
   session-override > user-default > config default, DM-only application.
3. ``/model <name> --default`` / ``--default off`` / bare ``/model``
   display through the real ``_handle_model_command``.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml as _yaml

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource, build_session_key

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dm_source(user_id: str = "203323840") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id=user_id,
        chat_id=user_id,
        user_name="tester",
        chat_type="dm",
    )


def _group_source(user_id: str = "203323840") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id=user_id,
        chat_id="-100123",
        user_name="tester",
        chat_type="group",
    )


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Point the hermes home (and thus the store) at a temp directory."""
    import hermes_constants
    import gateway.user_model_defaults as umd

    home = tmp_path / ".hermes"
    home.mkdir()
    token = hermes_constants.set_hermes_home_override(str(home))
    monkeypatch.setattr(
        umd, "_store_path", lambda: str(home / "state" / "user_model_defaults.json")
    )
    yield home
    hermes_constants.reset_hermes_home_override(token)


def _make_runner():
    """Minimal GatewayRunner with stubbed internals (bare object.__new__)."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="tok")}
    )
    runner._session_model_overrides = {}
    runner._pending_one_turn_model_restores = {}
    runner._pending_model_notes = {}
    runner._running_agents = {}
    runner._background_tasks = set()
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner.session_store = None
    runner._session_db = None
    return runner


# ---------------------------------------------------------------------------
# 1. Store
# ---------------------------------------------------------------------------


class TestUserModelDefaultsStore:
    def test_set_get_roundtrip_strips_secrets(self, fake_home):
        from gateway.user_model_defaults import (
            get_user_model_default,
            set_user_model_default,
        )

        assert set_user_model_default(
            "telegram:203323840",
            {
                "model": "gpt-5.6-sol",
                "provider": "openai",
                "base_url": "",
                "api_key": "SECRET",
                "api_mode": "responses",
            },
        )
        entry = get_user_model_default("telegram:203323840")
        assert entry == {"model": "gpt-5.6-sol", "provider": "openai"}

    def test_survives_restart(self, fake_home):
        # "Restart" = re-reading from disk with zero in-process caches.
        from gateway.user_model_defaults import (
            get_user_model_default,
            set_user_model_default,
        )

        set_user_model_default("telegram:1", {"model": "m1", "provider": "p1"})
        # A brand-new function call reads the same file a new process would.
        assert get_user_model_default("telegram:1")["model"] == "m1"
        raw = json.loads((fake_home / "state" / "user_model_defaults.json").read_text())
        assert raw["telegram:1"]["model"] == "m1"

    def test_users_are_isolated_and_clear_works(self, fake_home):
        from gateway.user_model_defaults import (
            clear_user_model_default,
            get_user_model_default,
            set_user_model_default,
        )

        set_user_model_default("telegram:1", {"model": "m1"})
        set_user_model_default("telegram:2", {"model": "m2"})
        assert get_user_model_default("telegram:1")["model"] == "m1"
        assert get_user_model_default("telegram:2")["model"] == "m2"
        clear_user_model_default("telegram:1")
        assert get_user_model_default("telegram:1") is None
        assert get_user_model_default("telegram:2")["model"] == "m2"

    def test_modelless_entry_is_rejected(self, fake_home):
        from gateway.user_model_defaults import (
            get_user_model_default,
            set_user_model_default,
        )

        set_user_model_default("telegram:1", {"provider": "openai"})
        assert get_user_model_default("telegram:1") is None

    def test_corrupt_file_degrades_to_none(self, fake_home):
        from gateway.user_model_defaults import get_user_model_default

        (fake_home / "state").mkdir(exist_ok=True)
        (fake_home / "state" / "user_model_defaults.json").write_text("{not json")
        assert get_user_model_default("telegram:1") is None

    def test_scoped_per_profile_home(self, tmp_path, monkeypatch):
        """Two hermes homes (multiplex profiles) never share defaults."""
        import hermes_constants
        import gateway.user_model_defaults as umd

        home_a = tmp_path / "profile_a"
        home_b = tmp_path / "profile_b"
        home_a.mkdir()
        home_b.mkdir()

        token = hermes_constants.set_hermes_home_override(str(home_a))
        try:
            monkeypatch.setattr(
                umd,
                "_store_path",
                lambda: str(home_a / "state" / "user_model_defaults.json"),
            )
            umd.set_user_model_default("telegram:1", {"model": "from-profile-a"})

            hermes_constants.set_hermes_home_override(str(home_b))
            monkeypatch.setattr(
                umd,
                "_store_path",
                lambda: str(home_b / "state" / "user_model_defaults.json"),
            )
            assert umd.get_user_model_default("telegram:1") is None
            umd.set_user_model_default("telegram:1", {"model": "from-profile-b"})
            assert umd.get_user_model_default("telegram:1")["model"] == "from-profile-b"
        finally:
            hermes_constants.reset_hermes_home_override(token)

        # Profile A's file still holds its own value.
        raw_a = json.loads((home_a / "state" / "user_model_defaults.json").read_text())
        assert raw_a["telegram:1"]["model"] == "from-profile-a"


class TestUserDefaultKey:
    def test_platform_user_id(self):
        from gateway.user_model_defaults import user_model_default_key

        assert user_model_default_key(_dm_source()) == "telegram:203323840"

    def test_alt_id_fallback_and_empty(self):
        from gateway.user_model_defaults import user_model_default_key

        src = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="",
            user_id_alt="42",
            chat_id="42",
            chat_type="dm",
        )
        assert user_model_default_key(src) == "telegram:42"
        empty = SessionSource(
            platform=Platform.TELEGRAM, user_id="", chat_id="1", chat_type="dm"
        )
        assert user_model_default_key(empty) == ""


# ---------------------------------------------------------------------------
# 2. Resolution priority (session > user > config), DM-only
# ---------------------------------------------------------------------------


class TestResolutionPriority:
    def _patch_resolution(self, monkeypatch, config_model="config-model"):
        import gateway.run as gateway_run

        monkeypatch.setattr(
            gateway_run, "_resolve_gateway_model", lambda uc: config_model
        )
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs",
            lambda: {"provider": "openrouter", "api_key": "cfg-key"},
        )
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs_for_provider",
            lambda provider: {
                "provider": provider,
                "api_key": f"key-{provider}",
                "base_url": "",
            },
        )

    def test_user_default_beats_config(self, fake_home, monkeypatch):
        from gateway.user_model_defaults import set_user_model_default

        set_user_model_default(
            "telegram:203323840", {"model": "user-model", "provider": "openai"}
        )
        self._patch_resolution(monkeypatch)
        runner = _make_runner()

        model, rt = runner._resolve_session_agent_runtime(source=_dm_source())
        assert model == "user-model"
        assert rt["provider"] == "openai"
        assert rt["api_key"] == "key-openai"

    def test_session_override_beats_user_default(self, fake_home, monkeypatch):
        from gateway.user_model_defaults import set_user_model_default

        set_user_model_default(
            "telegram:203323840", {"model": "user-model", "provider": "openai"}
        )
        self._patch_resolution(monkeypatch)
        runner = _make_runner()
        source = _dm_source()
        sk = build_session_key(source)
        runner._session_model_overrides[sk] = {
            "model": "session-model",
            "provider": "anthropic",
            "api_key": "sess-key",
            "base_url": "",
            "api_mode": "anthropic_messages",
        }

        model, rt = runner._resolve_session_agent_runtime(source=source)
        assert model == "session-model"
        assert rt["api_key"] == "sess-key"

    def test_config_used_when_no_default(self, fake_home, monkeypatch):
        self._patch_resolution(monkeypatch)
        runner = _make_runner()

        model, rt = runner._resolve_session_agent_runtime(source=_dm_source())
        assert model == "config-model"
        assert rt["api_key"] == "cfg-key"

    def test_group_source_never_applies_user_default(self, fake_home, monkeypatch):
        from gateway.user_model_defaults import set_user_model_default

        set_user_model_default(
            "telegram:203323840", {"model": "user-model", "provider": "openai"}
        )
        self._patch_resolution(monkeypatch)
        runner = _make_runner()

        model, _ = runner._resolve_session_agent_runtime(source=_group_source())
        assert model == "config-model"

    def test_new_session_after_user_default_uses_it(self, fake_home, monkeypatch):
        """The owner's exact scenario: set default, start a NEW topic/session."""
        from gateway.user_model_defaults import set_user_model_default

        self._patch_resolution(monkeypatch)
        runner = _make_runner()

        # New topic = new thread_id = a session key the runner has never
        # seen — exactly what a fresh Telegram DM topic produces.
        set_user_model_default(
            "telegram:203323840", {"model": "gpt-5.6-sol", "provider": "openai"}
        )
        new_topic = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="203323840",
            chat_id="203323840",
            thread_id="777",
            chat_type="dm",
        )
        model, _ = runner._resolve_session_agent_runtime(source=new_topic)
        assert model == "gpt-5.6-sol"


# ---------------------------------------------------------------------------
# 3. /model command UX (typed path, real handler)
# ---------------------------------------------------------------------------


class TestModelCommandUserDefault:
    @staticmethod
    def _runner_with_env(tmp_path, monkeypatch, fake_home):
        import gateway.run as gateway_run
        from gateway.run import GatewayRunner
        from hermes_cli.model_switch import ModelSwitchResult

        hermes_home = fake_home
        (hermes_home / "config.yaml").write_text(
            _yaml.safe_dump({
                "model": {"default": "old-model", "provider": "openrouter"}
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        monkeypatch.setattr(
            gateway_run,
            "_load_gateway_config",
            lambda: {"model": {"default": "old-model", "provider": "openrouter"}},
        )
        monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
        monkeypatch.setattr(
            "hermes_cli.model_switch.switch_model",
            lambda **kw: ModelSwitchResult(
                success=True,
                new_model="gpt-5.5",
                target_provider="openrouter",
                provider_changed=False,
                api_key="sk-test",
                base_url="https://openrouter.ai/api/v1",
                api_mode="chat_completions",
                provider_label="OpenRouter",
            ),
        )
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_home)
        monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: hermes_home)

        runner = object.__new__(GatewayRunner)
        runner.adapters = {}
        runner._voice_mode = {}
        runner._session_model_overrides = {}
        runner._pending_one_turn_model_restores = {}
        runner._running_agents = {}
        runner._background_tasks = set()
        runner._agent_cache = {}
        runner._agent_cache_lock = None
        runner._session_db = None
        _store = MagicMock()
        _store.set_model_override = AsyncMock()
        _store._store = None
        runner.session_store = None
        runner._async_session_store = _store
        return runner

    @staticmethod
    def _event(text, source=None):
        from gateway.platforms.base import MessageEvent, MessageType

        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source or _dm_source(),
        )

    @pytest.mark.asyncio
    async def test_set_default_persists_and_switches_session(
        self, tmp_path, monkeypatch, fake_home
    ):
        from gateway.user_model_defaults import get_user_model_default

        runner = self._runner_with_env(tmp_path, monkeypatch, fake_home)
        sk = build_session_key(_dm_source())

        result = await runner._handle_model_command(
            self._event("/model gpt-5.5 --default")
        )

        assert result is not None and "gpt-5.5" in result
        # Personal default persisted (non-secret fields only)...
        assert get_user_model_default("telegram:203323840") == {
            "model": "gpt-5.5",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
        }
        # ...AND the current session was switched to it.
        assert runner._session_model_overrides[sk]["model"] == "gpt-5.5"
        # config.yaml was NOT touched (--default never writes server config).
        cfg = _yaml.safe_load((fake_home / "config.yaml").read_text())
        assert cfg["model"]["default"] == "old-model"

    @pytest.mark.asyncio
    async def test_new_session_resolves_user_default(
        self, tmp_path, monkeypatch, fake_home
    ):
        import gateway.run as gateway_run

        runner = self._runner_with_env(tmp_path, monkeypatch, fake_home)
        await runner._handle_model_command(self._event("/model gpt-5.5 --default"))

        # Simulate the new topic/session: fresh runner state (conversation
        # scope cleared), same persisted store.
        monkeypatch.setattr(
            gateway_run, "_resolve_gateway_model", lambda uc: "old-model"
        )
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs",
            lambda: {"provider": "openrouter", "api_key": "cfg-key"},
        )
        monkeypatch.setattr(
            gateway_run,
            "_resolve_runtime_agent_kwargs_for_provider",
            lambda provider: {"provider": provider, "api_key": "k"},
        )
        fresh = _make_runner()
        new_topic = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="203323840",
            chat_id="203323840",
            thread_id="999",
            chat_type="dm",
        )
        model, _ = fresh._resolve_session_agent_runtime(source=new_topic)
        assert model == "gpt-5.5"

    @pytest.mark.asyncio
    async def test_default_off_clears(self, tmp_path, monkeypatch, fake_home):
        from gateway.user_model_defaults import get_user_model_default

        runner = self._runner_with_env(tmp_path, monkeypatch, fake_home)
        await runner._handle_model_command(self._event("/model gpt-5.5 --default"))
        assert get_user_model_default("telegram:203323840") is not None

        result = await runner._handle_model_command(self._event("/model --default off"))
        assert result is not None
        assert get_user_model_default("telegram:203323840") is None
        # /model --default off is metadata-only: the session override the
        # earlier --default command installed is left exactly as it was.
        sk = build_session_key(_dm_source())
        assert runner._session_model_overrides[sk]["model"] == "gpt-5.5"

    @pytest.mark.asyncio
    async def test_default_off_without_default_says_so(
        self, tmp_path, monkeypatch, fake_home
    ):
        runner = self._runner_with_env(tmp_path, monkeypatch, fake_home)

        result = await runner._handle_model_command(self._event("/model --default off"))
        assert result is not None
        assert "old-model" in result  # reports the server default

    @pytest.mark.asyncio
    async def test_bare_model_shows_three_levels(
        self, tmp_path, monkeypatch, fake_home
    ):
        from gateway.user_model_defaults import set_user_model_default

        runner = self._runner_with_env(tmp_path, monkeypatch, fake_home)
        set_user_model_default(
            "telegram:203323840", {"model": "gpt-5.5", "provider": "openrouter"}
        )

        result = await runner._handle_model_command(self._event("/model"))
        # Text-list fallback (no adapter): must surface all three levels.
        assert "old-model" in result  # session == server when no override
        assert "gpt-5.5" in result  # personal default
        assert "--default" in result  # usage hint for the new flag

    @pytest.mark.asyncio
    async def test_flag_conflicts_rejected(self, tmp_path, monkeypatch, fake_home):
        runner = self._runner_with_env(tmp_path, monkeypatch, fake_home)

        result = await runner._handle_model_command(
            self._event("/model gpt-5.5 --default --global")
        )
        assert result is not None and result.startswith("❌")
        result = await runner._handle_model_command(self._event("/model --default"))
        assert result is not None and result.startswith("❌")
