"""Tests for tools/mcp_connect_gateway.py (spec 20 — client connects MCP over messaging).

Covers: a server reachable without a token saves a plain url entry; a server
that needs a token drives the EXISTING spec 19 secret-capture primitive and
persists only a ``${VAR}`` reference, never the value; a repeated connect
under the same name replaces the token; an entry that fails the security
check is never saved; and the pending-restart flag is consumed exactly once.

No source file is read as text anywhere in this module — every assertion
exercises real behavior (config.yaml contents after a real ``load_config()``,
outcome dicts, a real ``.env`` write) against a temporary ``HERMES_HOME``
supplied by the repo-wide ``_isolate_hermes_home`` autouse fixture.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest


def _auth_error(status: int = 401) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.invalid/mcp")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("auth required", request=request, response=response)


def _clear_mcp_connect_state():
    from tools import mcp_connect_gateway as mcg

    with mcg._lock:
        mcg._notify_cbs.clear()
        mcg._pending_restart.clear()


def _clear_secret_capture_state():
    from tools import secret_capture_gateway as scg

    with scg._lock:
        scg._entries.clear()
        scg._session_index.clear()
        scg._notify_cbs.clear()


@pytest.fixture(autouse=True)
def _clean_state():
    _clear_mcp_connect_state()
    _clear_secret_capture_state()
    yield
    _clear_mcp_connect_state()
    _clear_secret_capture_state()


class TestConnectWithoutAuth:
    def test_public_server_saves_a_plain_url_entry(self):
        from tools.mcp_connect_gateway import connect_mcp_server

        with patch(
            "hermes_cli.mcp_config._probe_single_server",
            return_value=[("do_thing", "does a thing"), ("do_other", "does another")],
        ) as probe_mock:
            result = connect_mcp_server(
                session_key="telegram:owner",
                name="publicmcp",
                url="https://mcp.example.invalid/mcp",
                purpose="Example public server",
            )

        assert result["success"] is True
        assert result["name"] == "publicmcp"
        assert result["tool_count"] == 2
        assert result["needs_restart"] is True
        probe_mock.assert_called_once()

        from hermes_cli.config import load_config

        cfg = load_config()
        saved = cfg["mcp_servers"]["publicmcp"]
        assert saved["url"] == "https://mcp.example.invalid/mcp"
        assert "headers" not in saved

    def test_success_marks_session_pending_restart_exactly_once(self):
        from tools.mcp_connect_gateway import connect_mcp_server, pop_pending_restart

        with patch("hermes_cli.mcp_config._probe_single_server", return_value=[]):
            connect_mcp_server(
                session_key="sess-restart",
                name="noauth",
                url="https://mcp.example.invalid/mcp",
                purpose="test",
            )

        assert pop_pending_restart("sess-restart") is True
        assert pop_pending_restart("sess-restart") is False

    def test_invalid_url_is_rejected_without_probing(self):
        from tools.mcp_connect_gateway import connect_mcp_server

        with patch("hermes_cli.mcp_config._probe_single_server") as probe_mock:
            result = connect_mcp_server(
                session_key="sess-bad-url",
                name="badurl",
                url="not-a-url",
                purpose="test",
            )

        assert result["success"] is False
        assert result["reason"] == "invalid_url"
        probe_mock.assert_not_called()


class TestConnectWithAuth:
    def _register_immediate_secret_reply(self, session_key: str, value: str):
        """Simulate the chat replying with the token the instant it's asked,
        exactly like tests/tools/test_secret_capture_gateway.py does.
        """
        from tools import secret_capture_gateway as scg

        def _notify(entry):
            with patch("hermes_cli.config.save_env_value_secure") as save_mock:
                scg.resolve_secret_reply(entry.session_key, value)
                save_mock.assert_called_once_with(entry.env_var, value)

        scg.register_notify(session_key, _notify)

    def test_auth_required_requests_secret_and_saves_a_reference_only(self):
        from tools.mcp_connect_gateway import connect_mcp_server

        session_key = "telegram:owner-2"
        token_value = "sk-super-secret-bitrix-token"
        self._register_immediate_secret_reply(session_key, token_value)

        probe_calls = []

        def _fake_probe(name, config, *args, **kwargs):
            probe_calls.append(dict(config))
            if "headers" not in config:
                raise _auth_error(401)
            return [("crm_lead_create", "create a lead")]

        with patch("hermes_cli.mcp_config._probe_single_server", side_effect=_fake_probe):
            result = connect_mcp_server(
                session_key=session_key,
                name="bitrix",
                url="https://bitrix.example.invalid/mcp",
                purpose="Bitrix24 CRM integration",
                secret_env_var="BITRIX_TOKEN",
            )

        assert result["success"] is True
        assert result["tool_count"] == 1
        assert result["needs_restart"] is True
        # The token value must never appear anywhere in the outcome.
        assert token_value not in str(result)

        # Probed twice: once unauthenticated (to discover auth is needed),
        # once with the captured-token header (to confirm + count tools).
        assert len(probe_calls) == 2
        assert "headers" not in probe_calls[0]
        assert probe_calls[1]["headers"]["Authorization"] == "Bearer ${BITRIX_TOKEN}"

        from hermes_cli.config import load_config

        cfg = load_config()
        saved = cfg["mcp_servers"]["bitrix"]
        assert saved["headers"]["Authorization"] == "Bearer ${BITRIX_TOKEN}"
        # The reference, never the value, lives in config.yaml.
        assert token_value not in str(saved)

    def test_auth_required_without_secret_env_var_fails_cleanly(self):
        """Task 1.2 order: probe first; only request a secret when
        ``secret_env_var`` was actually given. Otherwise fail with a reason
        the model can act on (call again with one set) instead of guessing
        a name or silently blocking forever.
        """
        from tools.mcp_connect_gateway import connect_mcp_server

        with patch("hermes_cli.mcp_config._probe_single_server", side_effect=_auth_error(401)):
            result = connect_mcp_server(
                session_key="sess-no-secret-name",
                name="needsauth",
                url="https://needsauth.example.invalid/mcp",
                purpose="test",
            )

        assert result["success"] is False
        assert result["reason"] == "needs_secret_env_var"

        from hermes_cli.config import load_config

        cfg = load_config()
        assert "needsauth" not in cfg.get("mcp_servers", {})

    def test_reconnect_same_name_replaces_the_token(self):
        """Ruling 2 — no "already have a key" check; a second call under the
        same name always asks again and the new value wins in .env.
        """
        from tools.mcp_connect_gateway import connect_mcp_server

        session_key = "sess-rotate"

        def _fake_probe(name, config, *args, **kwargs):
            if "headers" not in config:
                raise _auth_error(401)
            return [("tool_a", "desc")]

        # First connect with the old (soon to expire) token.
        self._register_immediate_secret_reply(session_key, "old-token-value")
        with patch("hermes_cli.mcp_config._probe_single_server", side_effect=_fake_probe):
            first = connect_mcp_server(
                session_key=session_key,
                name="rotating",
                url="https://rotating.example.invalid/mcp",
                purpose="test",
                secret_env_var="ROTATING_TOKEN",
            )
        assert first["success"] is True

        # Second connect — client sent a fresh token after the old one expired.
        from tools import secret_capture_gateway as scg

        save_calls = []

        def _notify_second(entry):
            with patch(
                "hermes_cli.config.save_env_value_secure",
                side_effect=lambda k, v: save_calls.append((k, v)),
            ):
                scg.resolve_secret_reply(entry.session_key, "new-token-value")

        scg.register_notify(session_key, _notify_second)
        with patch("hermes_cli.mcp_config._probe_single_server", side_effect=_fake_probe):
            second = connect_mcp_server(
                session_key=session_key,
                name="rotating",
                url="https://rotating.example.invalid/mcp",
                purpose="test",
                secret_env_var="ROTATING_TOKEN",
            )

        assert second["success"] is True
        assert save_calls == [("ROTATING_TOKEN", "new-token-value")]
        assert "old-token-value" not in str(second)
        assert "new-token-value" not in str(second)


class TestAuthDetectionWithoutHttpResponse:
    """Дефект 2 (ревью на Opus): MCP-SDK часто отдаёт отказ авторизации как
    ``McpError`` — JSON-RPC-ошибку без атрибута ``.response`` вообще. Старая
    ``_probe_needs_auth`` узнавала только ``_is_auth_error`` (httpx 401) и
    ``.response.status_code``, так что такой отказ падал в ``probe_failed``
    и совет модели «позови ещё раз с secret_env_var» упирался в ту же
    ветку. Основной сценарий для самописных серверов и n8n.
    """

    def _register_immediate_secret_reply(self, session_key: str, value: str):
        from tools import secret_capture_gateway as scg

        def _notify(entry):
            with patch("hermes_cli.config.save_env_value_secure"):
                scg.resolve_secret_reply(entry.session_key, value)

        scg.register_notify(session_key, _notify)

    def test_mcp_error_style_rejection_without_response_attribute_still_asks_for_a_secret(self):
        from tools.mcp_connect_gateway import connect_mcp_server

        class FakeMcpError(Exception):
            """Stand-in for mcp.shared.exceptions.McpError: a JSON-RPC error
            object with no ``.response`` attribute, exactly like the real
            SDK exception raises on an auth rejection."""

        session_key = "sess-mcperror"
        token_value = "n8n-secret-token"
        self._register_immediate_secret_reply(session_key, token_value)

        probe_calls = []

        def _fake_probe(name, config, *args, **kwargs):
            probe_calls.append(dict(config))
            if "headers" not in config:
                raise FakeMcpError("Unauthorized: invalid or missing token")
            return [("run_workflow", "runs a workflow")]

        with patch("hermes_cli.mcp_config._probe_single_server", side_effect=_fake_probe):
            result = connect_mcp_server(
                session_key=session_key,
                name="n8n",
                url="https://n8n.example.invalid/mcp",
                purpose="n8n automation",
                secret_env_var="N8N_TOKEN",
            )

        assert result["success"] is True
        assert result["tool_count"] == 1
        # Probed twice: the auth-shaped failure must have been recognized
        # so the second, authenticated probe actually happened.
        assert len(probe_calls) == 2
        assert token_value not in str(result)

    def test_forbidden_text_without_response_attribute_is_also_recognized(self):
        """A second wording ('Forbidden'), still no ``.response`` — proves
        the fix isn't a one-string special case."""
        from tools.mcp_connect_gateway import connect_mcp_server

        class FakeRpcError(Exception):
            pass

        session_key = "sess-forbidden"
        self._register_immediate_secret_reply(session_key, "self-hosted-token")

        def _fake_probe(name, config, *args, **kwargs):
            if "headers" not in config:
                raise FakeRpcError("Forbidden")
            return [("t", "d")]

        with patch("hermes_cli.mcp_config._probe_single_server", side_effect=_fake_probe):
            result = connect_mcp_server(
                session_key=session_key,
                name="selfhosted",
                url="https://selfhosted.example.invalid/mcp",
                purpose="internal tool",
                secret_env_var="SELFHOSTED_TOKEN",
            )

        assert result["success"] is True

    def test_unreachable_server_does_not_ask_for_a_secret(self):
        """The other half of the acceptance bar: a server that is simply
        down must keep returning a plain, actionable failure — NOT turn
        into a secret request. ConnectionRefusedError's message shares none
        of the auth vocabulary, so this must stay False both before and
        after the fix."""
        from tools.mcp_connect_gateway import connect_mcp_server

        with patch(
            "hermes_cli.mcp_config._probe_single_server",
            side_effect=ConnectionRefusedError("Connection refused"),
        ):
            result = connect_mcp_server(
                session_key="sess-down",
                name="downserver",
                url="https://down.example.invalid/mcp",
                purpose="test",
                secret_env_var="DOWN_TOKEN",
            )

        assert result["success"] is False
        assert result["reason"] == "probe_failed"

    def test_probe_needs_auth_directly_on_a_bare_mcp_style_error(self):
        """Unit-level pin on the classifier itself, independent of the
        full connect flow above."""
        from tools.mcp_connect_gateway import _probe_needs_auth

        class FakeMcpError(Exception):
            pass

        assert _probe_needs_auth(FakeMcpError("401 Unauthorized")) is True
        assert _probe_needs_auth(ConnectionRefusedError("Connection refused")) is False
        assert _probe_needs_auth(TimeoutError("timed out")) is False


class TestValidationFailureBlocksSave:
    def test_an_entry_the_security_check_rejects_is_never_saved(self):
        from tools.mcp_connect_gateway import connect_mcp_server

        with patch("hermes_cli.mcp_config._probe_single_server", return_value=[("t", "d")]), \
             patch(
                 "hermes_cli.mcp_security.validate_mcp_server_entry",
                 return_value=["synthetic security rejection for this test"],
             ):
            result = connect_mcp_server(
                session_key="sess-invalid-entry",
                name="rejected",
                url="https://rejected.example.invalid/mcp",
                purpose="test",
            )

        assert result["success"] is False
        assert result["reason"] == "validation_failed"
        assert result["needs_restart"] is False

        from hermes_cli.config import load_config

        cfg = load_config()
        assert "rejected" not in cfg.get("mcp_servers", {})


class TestNameAndPurposeValidation:
    def test_blank_name_is_rejected(self):
        from tools.mcp_connect_gateway import connect_mcp_server

        result = connect_mcp_server(
            session_key="sess-x", name="  ", url="https://x.invalid/mcp", purpose="test",
        )
        assert result["success"] is False
        assert result["reason"] == "invalid_name"

    def test_missing_purpose_is_rejected(self):
        from tools.mcp_connect_gateway import connect_mcp_server

        result = connect_mcp_server(
            session_key="sess-x", name="ok", url="https://x.invalid/mcp", purpose="",
        )
        assert result["success"] is False
        assert result["reason"] == "missing_purpose"
