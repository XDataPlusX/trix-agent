"""E2E: secret-capture rotation must not leave a stale config.yaml mirror.

Spec 19, Ruling 4 — persistence routes through
``hermes_cli.config.save_env_value_secure`` ->
``credential_lifecycle.save_provider_env_credential`` specifically because a
hand/UI edit of ``.env`` alone reproduces bug #62269: a stale
``model.api_key`` mirror in ``config.yaml`` outranks ``.env`` at client
construction time, so the OLD key silently keeps winning after rotation.
This test drives the real on-disk stores (temp HERMES_HOME) through
``tools.secret_capture_gateway.resolve_secret_reply`` — the exact function
the gateway's runner-side intercept calls with the client's typed reply —
and asserts the mirror is updated, not just ``.env``.

All fake secrets are constructed at runtime so no key-shaped literal lands
in the repo.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

FAKE_OLD_KEY = "zk-" + "a" * 24
FAKE_NEW_KEY = "zk-" + "b" * 24


@pytest.fixture
def hermes_home(monkeypatch, tmp_path):
    home = tmp_path / "secret_rotation_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli.config import invalidate_env_cache

    invalidate_env_cache()
    return home


def _clear_capture_state():
    from tools import secret_capture_gateway as scg

    with scg._lock:
        scg._entries.clear()
        scg._session_index.clear()
        scg._notify_cbs.clear()


def test_rotation_replaces_env_and_scrubs_stale_config_mirror(hermes_home):
    from hermes_cli.config import get_config_path, invalidate_env_cache, load_env
    from tools import secret_capture_gateway as scg

    _clear_capture_state()

    # Seed .env with the OLD key and a config.yaml mirror of the SAME value
    # (the shape produced by the setup wizard's custom-endpoint flow).
    (hermes_home / ".env").write_text(f"ZAI_API_KEY={FAKE_OLD_KEY}\n", encoding="utf-8")
    (hermes_home / "config.yaml").write_text(
        f'model:\n  api_key: "{FAKE_OLD_KEY}"\n', encoding="utf-8"
    )
    invalidate_env_cache()

    scg.register("req-rot", "sess-rot", "ZAI_API_KEY", "model provider")
    with patch(
        "hermes_cli.credential_probes.probe_provider_key",
        return_value={"ok": True, "reachable": True, "reason": None},
    ):
        outcome = scg.resolve_secret_reply("sess-rot", FAKE_NEW_KEY)

    assert outcome["success"] is True
    assert outcome["provider"] is True
    # The outcome dict is what a tool/model could ever see — it must never
    # carry either key value.
    assert FAKE_NEW_KEY not in str(outcome)
    assert FAKE_OLD_KEY not in str(outcome)

    invalidate_env_cache()
    env_after = load_env()
    assert env_after["ZAI_API_KEY"] == FAKE_NEW_KEY

    config_text = get_config_path().read_text(encoding="utf-8")
    assert FAKE_OLD_KEY not in config_text, "stale mirror must be scrubbed on rotation"
    assert FAKE_NEW_KEY in config_text


def test_free_form_name_outside_the_wizard_catalog_is_saved(hermes_home):
    """(g) — a name like BITRIX_WEBHOOK, absent from every provider catalog,
    is still persisted through the exact same path."""
    from hermes_cli.config import invalidate_env_cache, load_env
    from tools import secret_capture_gateway as scg

    _clear_capture_state()
    scg.register("req-bx", "sess-bx", "BITRIX_WEBHOOK", "Bitrix24 integration")
    outcome = scg.resolve_secret_reply("sess-bx", "https://example.bitrix24.ru/rest/1/abc/")

    assert outcome["success"] is True
    assert outcome["provider"] is False

    invalidate_env_cache()
    assert load_env()["BITRIX_WEBHOOK"] == "https://example.bitrix24.ru/rest/1/abc/"
