"""Gateway completion step for the `hermes business setup` DM path (spec 21
§5/§6/§8 review) — ``gateway/builtin_hooks/business_dm_topic.py``.

Real temp HERMES_HOME, real profile dirs, the real client config template
(``assets/config/trix-config.yaml``) — the pending marker comes from a real
(DM-path) ``hermes_cli.trix_business.run_setup()`` call, not a hand-rolled
fixture, so these tests exercise the actual handoff between the two
modules. The gateway side (adapter, session_db, config) is faked — this is
not a full GatewayRunner integration test, just the completion logic.
"""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway.builtin_hooks import business_dm_topic as bdt
from hermes_cli import trix_business as tb

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


@pytest.fixture()
def biz_env(tmp_path, monkeypatch):
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


def _dm_setup(default_home, tmp_path, **overrides):
    kwargs = dict(company_root=str(tmp_path / "srv" / "trix"), install_skills=False)
    kwargs.update(overrides)
    return tb.run_setup(**kwargs)


def _marker_path(default_home: Path) -> Path:
    return default_home / "profiles" / "system_admin" / "business_setup" / "pending_dm_topic.json"


class _FakeAdapter:
    def __init__(self, *, create_topic_result=42):
        self.create_topic_result = create_topic_result
        self.create_calls = []
        self.sent = []

    async def _create_dm_topic(self, chat_id, name):
        self.create_calls.append((chat_id, name))
        return self.create_topic_result

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata))
        return SimpleNamespace(message_id=1)


class _FakeSessionDB:
    def __init__(self):
        self.enabled_calls = []

    async def enable_telegram_topic_mode(self, **kwargs):
        self.enabled_calls.append(kwargs)


class _FakeRunner:
    def __init__(self, adapter, caps, config=None):
        self._adapter = adapter
        self._caps = caps
        self._session_db = _FakeSessionDB()
        self.config = config if config is not None else SimpleNamespace(profile_routes=[])

    def _adapter_for_source(self, source):
        return self._adapter

    async def _get_telegram_topic_capabilities(self, source):
        return self._caps


class TestSuccessfulCompletion:
    def test_creates_route_notifies_and_clears_marker(self, biz_env, tmp_path):
        result = _dm_setup(biz_env, tmp_path)
        marker_path = _marker_path(biz_env)
        assert marker_path.is_file()

        adapter = _FakeAdapter(create_topic_result=555)
        runner = _FakeRunner(
            adapter, {"checked": True, "has_topics_enabled": True, "allows_users_to_create_topics": True}
        )

        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner))

        assert not marker_path.exists()

        data = yaml.safe_load((biz_env / "config.yaml").read_text(encoding="utf-8"))
        routes = data["gateway"]["profile_routes"]
        assert len(routes) == 1
        assert routes[0] == {
            "name": result["route_name"],
            "platform": "telegram",
            "chat_id": result["chat_id"],
            "thread_id": "555",
            "profile": "system_admin",
        }

        # Takes effect in THIS running process immediately — no second
        # restart needed (GatewayConfig is parsed once at startup).
        assert len(runner.config.profile_routes) == 1
        assert runner.config.profile_routes[0].thread_id == "555"
        assert runner.config.profile_routes[0].profile == "system_admin"

        assert adapter.create_calls == [(int(result["chat_id"]), tb.ADMIN_DM_TOPIC_NAME)]
        assert len(adapter.sent) == 1
        sent_chat_id, _, metadata = adapter.sent[0]
        assert str(sent_chat_id) == result["chat_id"]
        assert metadata == {"thread_id": "555"}

        assert runner._session_db.enabled_calls == [
            {
                "chat_id": result["chat_id"],
                "user_id": result["chat_id"],
                "has_topics_enabled": True,
                "allows_users_to_create_topics": True,
            }
        ]

    def test_second_completion_pass_is_a_no_op(self, biz_env, tmp_path):
        """Once completed, nothing is left to scan — a second gateway
        startup finds no marker and does nothing."""
        _dm_setup(biz_env, tmp_path)
        adapter = _FakeAdapter(create_topic_result=1)
        runner = _FakeRunner(adapter, {"checked": True, "has_topics_enabled": True})
        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner))

        adapter2 = _FakeAdapter(create_topic_result=2)
        runner2 = _FakeRunner(adapter2, {"checked": True, "has_topics_enabled": True})
        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner2))

        assert adapter2.create_calls == []
        assert adapter2.sent == []


class TestNoHalfConfiguredStateOnFailure:
    def test_topics_not_supported_leaves_marker_writes_no_route_sends_fallback(
        self, biz_env, tmp_path,
    ):
        _dm_setup(biz_env, tmp_path)
        marker_path = _marker_path(biz_env)

        adapter = _FakeAdapter()
        runner = _FakeRunner(adapter, {"checked": True, "has_topics_enabled": False})

        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner))

        # No half-configured state: marker survives, route absent.
        assert marker_path.is_file()
        data = yaml.safe_load((biz_env / "config.yaml").read_text(encoding="utf-8"))
        assert "profile_routes" not in data.get("gateway", {})
        assert adapter.create_calls == []  # never even attempted
        assert len(adapter.sent) == 1  # the fallback notice

        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert len(marker["pending"]) == 1
        assert marker["pending"][0]["dm_fallback_notified"] is True

    def test_fallback_notice_is_not_repeated_on_every_startup(self, biz_env, tmp_path):
        _dm_setup(biz_env, tmp_path)
        adapter = _FakeAdapter()
        runner = _FakeRunner(adapter, {"checked": True, "has_topics_enabled": False})

        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner))
        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner))

        assert len(adapter.sent) == 1

    def test_topic_creation_failure_leaves_marker_writes_no_route(self, biz_env, tmp_path):
        _dm_setup(biz_env, tmp_path)
        marker_path = _marker_path(biz_env)

        adapter = _FakeAdapter(create_topic_result=None)  # simulate Telegram API failure
        runner = _FakeRunner(adapter, {"checked": False})  # capability unknown -> attempt anyway

        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner))

        assert marker_path.is_file()
        assert adapter.create_calls  # attempted, since capability probe was inconclusive
        data = yaml.safe_load((biz_env / "config.yaml").read_text(encoding="utf-8"))
        assert "profile_routes" not in data.get("gateway", {})
        assert len(adapter.sent) == 1  # fallback notice

    def test_adapter_not_connected_yet_retries_next_startup(self, biz_env, tmp_path):
        _dm_setup(biz_env, tmp_path)
        marker_path = _marker_path(biz_env)
        before = marker_path.read_text(encoding="utf-8")

        class _NoAdapterRunner:
            _session_db = None
            config = SimpleNamespace(profile_routes=[])

            def _adapter_for_source(self, source):
                return None

        asyncio.run(bdt.run_pending_business_dm_topic_completions(_NoAdapterRunner()))

        assert marker_path.read_text(encoding="utf-8") == before

    def test_malformed_marker_does_not_crash_the_gateway(self, biz_env, tmp_path):
        _dm_setup(biz_env, tmp_path)
        marker_path = _marker_path(biz_env)
        marker_path.write_text(json.dumps({"admin_profile": "system_admin"}), encoding="utf-8")

        adapter = _FakeAdapter()
        runner = _FakeRunner(adapter, {"checked": True, "has_topics_enabled": True})

        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner))  # must not raise

        assert adapter.create_calls == []
        assert adapter.sent == []


class TestFallbackDecisionIsLogged:
    """Spec 21 review, finding 4: on the live machine, the marker showed
    ``dm_fallback_notified: true`` while agent.log/gateway.log/errors.log
    held not one line explaining why — the reason was unknowable without
    reading source. Every branch of ``_complete_one_admin`` (including the
    successful one, since the adapter's own "Created DM topic..." line
    doesn't say WHICH administrator it belonged to) must log the decision
    and its reason at a level that survives the default config (agent.log
    captures INFO+).
    """

    def test_topics_not_enabled_branch_logs_administrator_and_reason(
        self, biz_env, tmp_path, caplog,
    ):
        result = _dm_setup(biz_env, tmp_path)
        adapter = _FakeAdapter()
        runner = _FakeRunner(adapter, {"checked": True, "has_topics_enabled": False})

        with caplog.at_level(logging.INFO, logger="gateway.builtin_hooks.business_dm_topic"):
            asyncio.run(bdt.run_pending_business_dm_topic_completions(runner))

        assert any(
            record.levelno >= logging.INFO
            and "system_admin" in record.getMessage()
            and result["chat_id"] in record.getMessage()
            and "topics" in record.getMessage().lower()
            for record in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_topic_creation_failure_branch_logs_administrator_and_reason(
        self, biz_env, tmp_path, caplog,
    ):
        result = _dm_setup(biz_env, tmp_path)
        adapter = _FakeAdapter(create_topic_result=None)  # Bot API returned nothing
        runner = _FakeRunner(adapter, {"checked": False})

        with caplog.at_level(logging.INFO, logger="gateway.builtin_hooks.business_dm_topic"):
            asyncio.run(bdt.run_pending_business_dm_topic_completions(runner))

        assert any(
            record.levelno >= logging.INFO
            and "system_admin" in record.getMessage()
            and result["chat_id"] in record.getMessage()
            for record in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_successful_completion_logs_which_administrator_it_was_for(
        self, biz_env, tmp_path, caplog,
    ):
        """The adapter's own `Created DM topic ... -> thread_id=...` line
        (plugins/platforms/telegram/adapter.py) never names the
        administrator — with several administrators pending on the same
        profile, a reader of the log couldn't tell them apart."""
        result = _dm_setup(biz_env, tmp_path)
        adapter = _FakeAdapter(create_topic_result=555)
        runner = _FakeRunner(
            adapter, {"checked": True, "has_topics_enabled": True, "allows_users_to_create_topics": True}
        )

        with caplog.at_level(logging.INFO, logger="gateway.builtin_hooks.business_dm_topic"):
            asyncio.run(bdt.run_pending_business_dm_topic_completions(runner))

        assert any(
            record.levelno >= logging.INFO
            and "system_admin" in record.getMessage()
            and result["chat_id"] in record.getMessage()
            and "555" in record.getMessage()
            for record in caplog.records
        ), [r.getMessage() for r in caplog.records]


class _MultiChatAdapter:
    """Like _FakeAdapter, but topic-creation outcome and capability can vary
    per chat_id — needed to simulate one administrator succeeding while
    another fails, independently, in the same marker."""

    def __init__(self, *, create_topic_results=None):
        self.create_topic_results = create_topic_results or {}
        self.create_calls = []
        self.sent = []

    async def _create_dm_topic(self, chat_id, name):
        self.create_calls.append((chat_id, name))
        return self.create_topic_results.get(str(chat_id), 0)

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata))
        return SimpleNamespace(message_id=1)


class _MultiChatRunner:
    def __init__(self, adapter, caps_by_chat=None, default_caps=None, config=None):
        self._adapter = adapter
        self._caps_by_chat = caps_by_chat or {}
        self._default_caps = default_caps if default_caps is not None else {"checked": True, "has_topics_enabled": True}
        self._session_db = _FakeSessionDB()
        self.config = config if config is not None else SimpleNamespace(profile_routes=[])

    def _adapter_for_source(self, source):
        return self._adapter

    async def _get_telegram_topic_capabilities(self, source):
        return self._caps_by_chat.get(str(source.chat_id), self._default_caps)


class TestMultipleAdministrators:
    """Spec 21 §6 review, task B: several administrators can share one
    ``system_admin`` profile, each getting their own DM topic. One marker
    carries all of them; each must be completed (or fail) independently.
    """

    def test_two_ids_in_one_invocation_write_two_distinct_routes(self, biz_env, tmp_path):
        (biz_env / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=111222333,444555666\n", encoding="utf-8"
        )
        result = _dm_setup(
            biz_env, tmp_path, admin_telegram_id=["111222333", "444555666"],
        )
        marker_path = _marker_path(biz_env)
        assert marker_path.is_file()

        adapter = _MultiChatAdapter(create_topic_results={"111222333": 10, "444555666": 20})
        runner = _MultiChatRunner(adapter)

        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner))

        assert not marker_path.exists()

        data = yaml.safe_load((biz_env / "config.yaml").read_text(encoding="utf-8"))
        routes = data["gateway"]["profile_routes"]
        assert len(routes) == 2
        by_chat = {r["chat_id"]: r for r in routes}
        assert set(by_chat) == {"111222333", "444555666"}
        assert by_chat["111222333"]["thread_id"] == "10"
        assert by_chat["444555666"]["thread_id"] == "20"
        assert by_chat["111222333"]["profile"] == "system_admin"
        assert by_chat["444555666"]["profile"] == "system_admin"
        assert by_chat["111222333"]["name"] != by_chat["444555666"]["name"]

        assert len(runner.config.profile_routes) == 2

        admin_ids = {a["admin_id"] for a in result["administrators"]}
        assert admin_ids == {"111222333", "444555666"}

    def test_second_setup_invocation_adds_administrator_gateway_completes_both(
        self, biz_env, tmp_path,
    ):
        (biz_env / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=111222333,444555666\n", encoding="utf-8"
        )
        first = _dm_setup(biz_env, tmp_path, admin_telegram_id=["111222333"])
        marker_path = _marker_path(biz_env)

        # Gateway completes the first administrator before the second one is
        # even added — this must not be disturbed by the later CLI call.
        adapter1 = _MultiChatAdapter(create_topic_results={"111222333": 10})
        runner1 = _MultiChatRunner(adapter1)
        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner1))
        assert not marker_path.exists()  # fully completed, nothing left pending

        second = _dm_setup(biz_env, tmp_path, admin_telegram_id=["444555666"])
        assert marker_path.is_file()
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert len(marker["pending"]) == 1
        assert marker["pending"][0]["chat_id"] == "444555666"

        data = yaml.safe_load((biz_env / "config.yaml").read_text(encoding="utf-8"))
        routes_before = data["gateway"]["profile_routes"]
        assert len(routes_before) == 1
        assert routes_before[0]["chat_id"] == "111222333"
        assert routes_before[0]["thread_id"] == "10"

        adapter2 = _MultiChatAdapter(create_topic_results={"444555666": 20})
        runner2 = _MultiChatRunner(adapter2)
        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner2))
        assert not marker_path.exists()

        data = yaml.safe_load((biz_env / "config.yaml").read_text(encoding="utf-8"))
        routes_after = data["gateway"]["profile_routes"]
        assert len(routes_after) == 2
        by_chat = {r["chat_id"]: r for r in routes_after}
        # First administrator's route is untouched by the second completion.
        assert by_chat["111222333"] == routes_before[0]
        assert by_chat["444555666"]["thread_id"] == "20"

    def test_one_administrators_failure_does_not_block_or_undo_the_others(
        self, biz_env, tmp_path,
    ):
        (biz_env / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=111222333,444555666\n", encoding="utf-8"
        )
        _dm_setup(biz_env, tmp_path, admin_telegram_id=["111222333", "444555666"])
        marker_path = _marker_path(biz_env)

        # 111222333's client doesn't render topics (creation fails); 444555666
        # succeeds normally, in the SAME completion pass.
        adapter = _MultiChatAdapter(create_topic_results={"111222333": None, "444555666": 99})
        runner = _MultiChatRunner(adapter)

        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner))

        # Marker survives (one admin still pending) but only for the failed one.
        assert marker_path.is_file()
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert len(marker["pending"]) == 1
        assert marker["pending"][0]["chat_id"] == "111222333"
        assert marker["pending"][0]["dm_fallback_notified"] is True

        # The other administrator's route IS live.
        data = yaml.safe_load((biz_env / "config.yaml").read_text(encoding="utf-8"))
        routes = data["gateway"]["profile_routes"]
        assert len(routes) == 1
        assert routes[0]["chat_id"] == "444555666"
        assert routes[0]["thread_id"] == "99"

        # Fallback notice went only to the failed administrator.
        fallback_recipients = {chat_id for chat_id, _, _ in adapter.sent if chat_id == "111222333"}
        assert fallback_recipients == {"111222333"}
        ready_recipients = {chat_id for chat_id, _, _ in adapter.sent if chat_id == "444555666"}
        assert ready_recipients == {"444555666"}

        # A second pass only retries the still-pending administrator.
        adapter2 = _MultiChatAdapter(create_topic_results={"111222333": 42})
        runner2 = _MultiChatRunner(adapter2, config=SimpleNamespace(profile_routes=list(runner.config.profile_routes)))
        asyncio.run(bdt.run_pending_business_dm_topic_completions(runner2))

        assert not marker_path.exists()
        data = yaml.safe_load((biz_env / "config.yaml").read_text(encoding="utf-8"))
        routes = data["gateway"]["profile_routes"]
        assert len(routes) == 2
        by_chat = {r["chat_id"]: r for r in routes}
        assert by_chat["111222333"]["thread_id"] == "42"
        assert by_chat["444555666"]["thread_id"] == "99"  # untouched by the retry
