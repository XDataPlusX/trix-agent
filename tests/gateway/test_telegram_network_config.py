"""Telegram HTTP pool size + timeouts are configurable from config.yaml.

Why this exists
---------------
The five numbers that decide how patiently the bot waits on a flaky link
(connection pool size, pool/connect/read/write timeouts) plus the sixth that
covers uploads (media_write_timeout) used to be reachable only through
``HERMES_TELEGRAM_HTTP_*`` env vars. That put them out of reach of any user
without a shell, and it contradicted the project's own rule that ``.env``
holds secrets while behaviour lives in ``config.yaml``.

Contracts asserted here
-----------------------
1. A value written in ``config.yaml`` reaches the real ``HTTPXRequest`` the
   adapter builds — exercised through ``load_gateway_config()`` and
   ``connect()``, not through a stub on ``os.getenv``.
2. Config beats the environment. This is the direction the project already
   settled on for ``agent.gateway_timeout`` after a stale ``.env`` line
   silently shadowed the user's config (the 60-vs-500 max_turns incident).
   The env var still works when the config says nothing.
3. No config section = exactly today's numbers, for every setting in the
   table — asserted as a relation between the spec table and what the client
   receives, not as a copy of the numbers.
4. Garbage in a value is refused (and reported), not silently obeyed.
"""

import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)


_ensure_telegram_mock()

from plugins.platforms.telegram import adapter as tg_adapter  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402
from plugins.platforms.telegram.telegram_network import (  # noqa: E402
    _NETWORK_SPEC,
    NETWORK_CONFIG_KEYS,
    resolve_http_request_kwargs,
)

_ENV_NAMES = [spec[2] for spec in _NETWORK_SPEC if spec[2]]


@pytest.fixture(autouse=True)
def _no_stray_env(monkeypatch):
    """The suite must decide the environment, not the developer's shell."""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------
# The resolver itself
# --------------------------------------------------------------------------

def test_no_config_and_no_env_gives_the_documented_defaults():
    kwargs = resolve_http_request_kwargs({}, env={})
    # Relation, not a snapshot: every entry of the spec table must arrive at
    # its HTTPXRequest kwarg with its declared default.
    assert kwargs == {spec[1]: spec[3] for spec in _NETWORK_SPEC}
    # And the table must cover the whole client surface it claims to.
    assert set(kwargs) >= {
        "connection_pool_size", "pool_timeout", "connect_timeout",
        "read_timeout", "write_timeout", "media_write_timeout",
    }


def test_every_documented_key_reaches_the_client():
    """Each config key must actually move its own kwarg — no dead keys."""
    for config_key, request_kwarg, _env, default, cast in _NETWORK_SPEC:
        bumped = cast(default) + cast(7)
        kwargs = resolve_http_request_kwargs({"network": {config_key: bumped}}, env={})
        assert kwargs[request_kwarg] == bumped, config_key
        # Only that one moved.
        for other_key, other_kwarg, _e, other_default, _c in _NETWORK_SPEC:
            if other_kwarg != request_kwarg:
                assert kwargs[other_kwarg] == other_default, (config_key, other_key)


def test_config_beats_environment():
    env = {name: "1" for name in _ENV_NAMES}
    kwargs = resolve_http_request_kwargs(
        {"network": {"read_timeout": 45, "pool_size": 64}}, env=env
    )
    assert kwargs["read_timeout"] == 45.0
    assert kwargs["connection_pool_size"] == 64
    # Settings the config is silent about still honour the env override.
    assert kwargs["connect_timeout"] == 1.0


def test_environment_still_works_when_config_is_silent():
    kwargs = resolve_http_request_kwargs(
        {}, env={"HERMES_TELEGRAM_HTTP_READ_TIMEOUT": "33"}
    )
    assert kwargs["read_timeout"] == 33.0


def test_pool_size_stays_an_int_and_timeouts_stay_floats():
    kwargs = resolve_http_request_kwargs(
        {"network": {"pool_size": "256", "read_timeout": "12"}}, env={}
    )
    assert isinstance(kwargs["connection_pool_size"], int)
    assert kwargs["connection_pool_size"] == 256
    assert isinstance(kwargs["read_timeout"], float)
    assert kwargs["read_timeout"] == 12.0


@pytest.mark.parametrize(
    "bad", ["", "abc", 0, -5, None, True, False, [], {}],
)
def test_unusable_value_falls_back_instead_of_being_obeyed(bad, caplog):
    with caplog.at_level("WARNING"):
        kwargs = resolve_http_request_kwargs({"network": {"read_timeout": bad}}, env={})
    assert kwargs["read_timeout"] == 20.0
    assert "read_timeout" in caplog.text


def test_unusable_config_value_falls_through_to_the_env_override():
    kwargs = resolve_http_request_kwargs(
        {"network": {"read_timeout": "soon"}},
        env={"HERMES_TELEGRAM_HTTP_READ_TIMEOUT": "31"},
    )
    assert kwargs["read_timeout"] == 31.0


def test_unusable_env_value_falls_back_to_the_default(caplog):
    with caplog.at_level("WARNING"):
        kwargs = resolve_http_request_kwargs(
            {}, env={"HERMES_TELEGRAM_HTTP_POOL_SIZE": "many"}
        )
    assert kwargs["connection_pool_size"] == 512
    assert "HERMES_TELEGRAM_HTTP_POOL_SIZE" in caplog.text


def test_typo_in_a_key_is_reported_not_swallowed(caplog):
    with caplog.at_level("WARNING"):
        kwargs = resolve_http_request_kwargs(
            {"network": {"read_timout": 45}}, env={}
        )
    assert kwargs["read_timeout"] == 20.0
    assert "read_timout" in caplog.text


def test_network_that_is_not_a_mapping_is_refused(caplog):
    with caplog.at_level("WARNING"):
        kwargs = resolve_http_request_kwargs({"network": "fast"}, env={})
    assert kwargs == {spec[1]: spec[3] for spec in _NETWORK_SPEC}
    assert "mapping" in caplog.text


def test_missing_extra_is_harmless():
    assert resolve_http_request_kwargs(None, env={}) == {
        spec[1]: spec[3] for spec in _NETWORK_SPEC
    }


def test_documented_key_set_matches_the_spec_table():
    assert NETWORK_CONFIG_KEYS == {spec[0] for spec in _NETWORK_SPEC}


# --------------------------------------------------------------------------
# config.yaml → loader → PlatformConfig.extra
# --------------------------------------------------------------------------

def _load_telegram_platform_config(hermes_home, yaml_text):
    (hermes_home / "config.yaml").write_text(yaml_text, encoding="utf-8")
    from gateway.config import Platform, load_gateway_config

    cfg = load_gateway_config()
    return cfg.platforms.get(Platform.TELEGRAM)


def test_root_level_telegram_network_block_reaches_platform_extra(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plat = _load_telegram_platform_config(
        tmp_path,
        "telegram:\n"
        "  network:\n"
        "    pool_size: 128\n"
        "    read_timeout: 45\n",
    )
    assert plat is not None
    assert plat.extra.get("network") == {"pool_size": 128, "read_timeout": 45}
    kwargs = resolve_http_request_kwargs(plat.extra, env={})
    assert kwargs["connection_pool_size"] == 128
    assert kwargs["read_timeout"] == 45.0


def test_nested_platforms_telegram_extra_network_reaches_platform_extra(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plat = _load_telegram_platform_config(
        tmp_path,
        "platforms:\n"
        "  telegram:\n"
        "    extra:\n"
        "      network:\n"
        "        pool_timeout: 15\n",
    )
    assert plat is not None
    assert plat.extra.get("network") == {"pool_timeout": 15}
    assert resolve_http_request_kwargs(plat.extra, env={})["pool_timeout"] == 15.0


def test_no_telegram_section_leaves_todays_numbers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plat = _load_telegram_platform_config(tmp_path, "agent:\n  max_turns: 500\n")
    extra = plat.extra if plat is not None else {}
    assert "network" not in extra
    assert resolve_http_request_kwargs(extra, env={}) == {
        spec[1]: spec[3] for spec in _NETWORK_SPEC
    }


# --------------------------------------------------------------------------
# PlatformConfig.extra → the HTTPXRequest connect() actually builds
# --------------------------------------------------------------------------

class _StopConnect(Exception):
    """Sentinel raised to abort connect() once the requests are built."""


class _RecordingHTTPXRequest:
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        _RecordingHTTPXRequest.instances.append(self)


def _drive_connect(monkeypatch, extra):
    """Run the real connect() far enough to build the HTTPXRequests."""
    _RecordingHTTPXRequest.instances = []

    async def _no_fallback():
        return []

    monkeypatch.setattr(tg_adapter, "discover_fallback_ips", _no_fallback)
    monkeypatch.setattr(tg_adapter, "resolve_proxy_url", lambda *a, **k: None)
    monkeypatch.setattr(tg_adapter, "HTTPXRequest", _RecordingHTTPXRequest)

    adapter = TelegramAdapter(
        PlatformConfig.from_dict({"enabled": True, "token": "test-token", "extra": extra})
    )
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "_fallback_ips", lambda: [])

    chainable = MagicMock()
    for attr in ("token", "base_url", "base_file_url", "local_mode", "request", "get_updates_request"):
        getattr(chainable, attr).return_value = chainable
    chainable.build.side_effect = _StopConnect

    builder_root = MagicMock()
    builder_root.builder.return_value = chainable
    monkeypatch.setattr(tg_adapter, "Application", builder_root)

    try:
        asyncio.run(adapter.connect())
    except _StopConnect:
        pass
    except Exception:
        pass

    assert _RecordingHTTPXRequest.instances, "connect() built no HTTPXRequest"
    return list(_RecordingHTTPXRequest.instances)


def test_configured_numbers_reach_every_httpx_request(monkeypatch):
    instances = _drive_connect(
        monkeypatch,
        {"network": {
            "pool_size": 96,
            "pool_timeout": 3,
            "connect_timeout": 4,
            "read_timeout": 5,
            "write_timeout": 6,
            "media_write_timeout": 7,
        }},
    )
    # Both pools (general + get_updates) must be built from the same numbers.
    assert len(instances) >= 2
    for inst in instances:
        assert inst.kwargs["connection_pool_size"] == 96
        assert inst.kwargs["pool_timeout"] == 3.0
        assert inst.kwargs["connect_timeout"] == 4.0
        assert inst.kwargs["read_timeout"] == 5.0
        assert inst.kwargs["write_timeout"] == 6.0
        assert inst.kwargs["media_write_timeout"] == 7.0


def test_config_beats_env_on_the_real_connect_path(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.setenv(name, "2")
    instances = _drive_connect(monkeypatch, {"network": {"read_timeout": 42}})
    for inst in instances:
        assert inst.kwargs["read_timeout"] == 42.0
        # …and the env override still governs what the config left unsaid.
        assert inst.kwargs["connect_timeout"] == 2.0


def test_env_only_still_governs_on_the_real_connect_path(monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_HTTP_POOL_SIZE", "77")
    instances = _drive_connect(monkeypatch, {})
    for inst in instances:
        assert inst.kwargs["connection_pool_size"] == 77


def test_bare_config_builds_the_same_client_as_before(monkeypatch):
    instances = _drive_connect(monkeypatch, {})
    expected = {spec[1]: spec[3] for spec in _NETWORK_SPEC}
    for inst in instances:
        for kwarg, default in expected.items():
            assert inst.kwargs[kwarg] == default


def test_configured_pool_size_governs_the_keepalive_limits(monkeypatch):
    """The #31599 keepalive limits must follow the configured pool, not 512."""
    instances = _drive_connect(monkeypatch, {"network": {"pool_size": 40}})
    for inst in instances:
        limits = inst.kwargs.get("httpx_kwargs", {}).get("limits")
        assert limits is not None
        assert limits.max_connections == 40


# --------------------------------------------------------------------------
# The three places that state these numbers must agree
# --------------------------------------------------------------------------

def test_default_config_matches_the_adapter_defaults():
    """DEFAULT_CONFIG documents the same numbers the adapter falls back to.

    Two files state these defaults — hermes_cli/config_defaults.py (what the
    user is told) and telegram_network.py (what the client actually does).
    A drift between them is a lie in the documentation, so assert the
    relation rather than either set of literals.
    """
    from hermes_cli.config import DEFAULT_CONFIG

    documented = DEFAULT_CONFIG["telegram"]["network"]
    assert set(documented) == NETWORK_CONFIG_KEYS
    assert resolve_http_request_kwargs({"network": documented}, env={}) == {
        spec[1]: spec[3] for spec in _NETWORK_SPEC
    }
