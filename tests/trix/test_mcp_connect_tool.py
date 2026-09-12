"""Tests for the ``mcp_connect`` tool (tools/mcp_connect_tool.py, spec 20).

Mirrors tests/tools/test_secret_request_tool.py's shape: parameter
validation, the no-session refusal (and what it tells the model to do
instead), and that a captured token never appears in the tool's JSON result
even when the underlying gateway executor runs for real.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from tools.mcp_connect_tool import mcp_connect_tool


class TestValidation:
    def test_missing_name_is_a_tool_error(self):
        result = json.loads(
            mcp_connect_tool(name="", url="https://x.invalid/mcp", purpose="test")
        )
        assert result.get("success") is False or "error" in result

    def test_invalid_name_is_a_tool_error(self):
        result = json.loads(
            mcp_connect_tool(name="not a name!", url="https://x.invalid/mcp", purpose="test")
        )
        assert result.get("success") is False or "error" in result

    def test_missing_url_is_a_tool_error(self):
        result = json.loads(
            mcp_connect_tool(name="ok", url="", purpose="test")
        )
        assert result.get("success") is False or "error" in result

    def test_missing_purpose_is_a_tool_error(self):
        result = json.loads(
            mcp_connect_tool(name="ok", url="https://x.invalid/mcp", purpose="")
        )
        assert result.get("success") is False or "error" in result

    def test_invalid_secret_env_var_is_a_tool_error(self):
        result = json.loads(
            mcp_connect_tool(
                name="ok", url="https://x.invalid/mcp", purpose="test",
                secret_env_var="not valid!",
            )
        )
        assert result.get("success") is False or "error" in result


class TestNoSession:
    def test_unavailable_outside_a_messaging_session(self):
        with patch("gateway.session_context.get_session_env", return_value=""):
            result = json.loads(
                mcp_connect_tool(name="ok", url="https://x.invalid/mcp", purpose="test")
            )
        assert result.get("success") is False or "error" in result

    def test_the_refusal_tells_the_model_what_to_do_instead(self):
        """Same contract as secret_request's refusal (tests/tools/test_secret_request_tool.py):
        stop, don't probe the filesystem, don't describe the sandbox, point
        at /setup — never a generic "not available here" a model reads as
        "try a different approach".
        """
        with patch("gateway.session_context.get_session_env", return_value=""):
            result = json.loads(
                mcp_connect_tool(name="ok", url="https://x.invalid/mcp", purpose="test")
            )
        text = json.dumps(result, ensure_ascii=False).lower()
        assert "stop" in text
        assert "/setup" in text
        assert "probe" in text
        assert "config.yaml" in text
        assert "describe your environment" in text


class TestEndToEndViaGatewayExecutor:
    def setup_method(self):
        from tools import mcp_connect_gateway as mcg
        from tools import secret_capture_gateway as scg

        with mcg._lock:
            mcg._notify_cbs.clear()
            mcg._pending_restart.clear()
        with scg._lock:
            scg._entries.clear()
            scg._session_index.clear()
            scg._notify_cbs.clear()

    def test_public_server_connects_and_reports_tool_count(self):
        with patch("gateway.session_context.get_session_env", return_value="telegram:owner"), \
             patch(
                 "hermes_cli.mcp_config._probe_single_server",
                 return_value=[("a", "d"), ("b", "d"), ("c", "d")],
             ):
            raw = mcp_connect_tool(
                name="threetool", url="https://threetool.invalid/mcp", purpose="test",
            )

        result = json.loads(raw)
        assert result["success"] is True
        assert result["name"] == "threetool"
        assert result["tool_count"] == 3
        assert result["needs_restart"] is True

    def test_token_never_reaches_the_tool_result(self):
        from tools import secret_capture_gateway as scg

        session_key = "telegram:owner-secret"
        token_value = "sk-do-not-leak-this-through-the-tool-result"

        def _notify(entry):
            with patch("hermes_cli.config.save_env_value_secure"):
                scg.resolve_secret_reply(entry.session_key, token_value)

        scg.register_notify(session_key, _notify)

        def _fake_probe(name, config, *args, **kwargs):
            if "headers" not in config:
                import httpx

                request = httpx.Request("GET", "https://needsauth.invalid/mcp")
                response = httpx.Response(401, request=request)
                raise httpx.HTTPStatusError("401", request=request, response=response)
            return [("secure_tool", "desc")]

        with patch("gateway.session_context.get_session_env", return_value=session_key), \
             patch("hermes_cli.mcp_config._probe_single_server", side_effect=_fake_probe):
            raw = mcp_connect_tool(
                name="secureserver",
                url="https://needsauth.invalid/mcp",
                purpose="test",
                secret_env_var="SECURE_SERVER_TOKEN",
            )

        assert token_value not in raw
        result = json.loads(raw)
        assert result["success"] is True
        assert "value" not in result
        assert token_value not in json.dumps(result, ensure_ascii=False)
