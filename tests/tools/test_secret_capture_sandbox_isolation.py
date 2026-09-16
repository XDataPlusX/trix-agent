"""(i) A model-provider key captured over messaging must never reach the
Docker sandbox.

Two angles, both load-bearing:

1. ``resolve_secret_reply`` classifies a provider key as needing a gateway
   RESTART (Ruling 7), not ``reload_env()`` — so unlike a tool key, it is
   never even exported into THIS process's ``os.environ`` until the new
   gateway process starts clean from a freshly-written ``.env``. There is no
   window where the current process (and anything it could pass through to
   a sandbox) holds the new value.
2. Every env var name spec 19's classifier calls a "model provider" key is a
   member of ``_HERMES_PROVIDER_ENV_BLOCKLIST`` — the SAME name-based
   blocklist ``tools/environments/{local,docker}.py`` already subtracts from
   the implicit env-passthrough set before anything is forwarded into a
   sandboxed command. If this ever drifted, a provider key saved through
   spec 19's path would ride along on the very passthrough mechanism the
   blocklist exists to close.
"""

from __future__ import annotations

import os
from unittest.mock import patch


def _clear_capture_state():
    from tools import secret_capture_gateway as scg

    with scg._lock:
        scg._entries.clear()
        scg._session_index.clear()
        scg._notify_cbs.clear()


class TestProviderClassificationMatchesSandboxBlocklist:
    def test_every_provider_api_key_env_var_is_sandbox_blocked(self):
        from hermes_cli.auth import PROVIDER_REGISTRY
        from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST
        from tools.secret_capture_gateway import _is_model_provider_env_var

        # CLAUDE_CODE_OAUTH_TOKEN is the one deliberate, documented exemption
        # (tools/environments/local.py:324, tests/tools/test_local_env_blocklist.py):
        # it is owned by the user's own Claude Code install and legitimately
        # needs to reach a Claude Code session running inside the sandbox —
        # an intentional design, not a gap this spec's classifier should
        # second-guess.
        exempt = {"CLAUDE_CODE_OAUTH_TOKEN"}

        checked = 0
        for cfg in PROVIDER_REGISTRY.values():
            for var in cfg.api_key_env_vars or ():
                if var in exempt:
                    continue
                checked += 1
                assert _is_model_provider_env_var(var), var
                assert var in _HERMES_PROVIDER_ENV_BLOCKLIST, (
                    f"{var} is classified as a model-provider credential by "
                    "spec 19 but is NOT in the sandbox's provider-env "
                    "blocklist — it would ride the implicit env-passthrough "
                    "straight into the Docker sandbox."
                )
        assert checked > 0  # sanity: the registry actually has api keys

    def test_a_tool_key_like_tavily_is_not_classified_as_provider(self):
        from tools.secret_capture_gateway import _is_model_provider_env_var

        assert _is_model_provider_env_var("TAVILY_API_KEY") is False
        assert _is_model_provider_env_var("BITRIX_WEBHOOK") is False


class TestProviderKeyNeverExportedBeforeRestart:
    def setup_method(self):
        _clear_capture_state()

    def test_provider_key_does_not_reach_process_env_until_restart(self, monkeypatch):
        from tools import secret_capture_gateway as scg

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        scg.register("req-sb", "sess-sb", "OPENAI_API_KEY", "model provider")

        with patch("hermes_cli.config.save_env_value_secure"), \
             patch(
                 "hermes_cli.credential_probes.probe_provider_key",
                 return_value={"ok": True, "reachable": True, "reason": None},
             ):
            outcome = scg.resolve_secret_reply("sess-sb", "sk-openai-should-not-leak")

        assert outcome["provider"] is True
        assert outcome["needs_restart"] is True
        # The whole point of routing provider keys through a restart instead
        # of reload_env(): this process's environment — and therefore
        # anything it could forward into a running sandbox this turn — is
        # untouched. Only the NEW gateway process, started clean from the
        # freshly-written .env, will ever see it.
        assert "OPENAI_API_KEY" not in os.environ

    def test_tool_key_does_reach_process_env_immediately(self, monkeypatch):
        """Contrast case: a non-provider key DOES reload in place — this is
        what makes "works immediately, no restart" true for tool keys."""
        from tools import secret_capture_gateway as scg

        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        scg.register("req-tk", "sess-tk", "TAVILY_API_KEY", "web search")

        with patch("hermes_cli.config.save_env_value_secure"), \
             patch("hermes_cli.config.load_env", return_value={"TAVILY_API_KEY": "tvly-abc"}):
            outcome = scg.resolve_secret_reply("sess-tk", "tvly-abc")

        assert outcome["provider"] is False
        assert outcome["needs_restart"] is False
        assert os.environ.get("TAVILY_API_KEY") == "tvly-abc"
