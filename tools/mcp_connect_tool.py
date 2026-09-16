"""``mcp_connect`` tool — connect a remote HTTP MCP server from the current chat.

Spec 20 (client connects MCP over messaging). The client's terminal + file
tools all run inside a Docker sandbox with no host filesystem access (spec 9),
so there is no path for a Telegram-only client to have the agent edit the
host's ``config.yaml`` the normal Hermes way. This tool closes that gap for
MCP server connections the exact same way ``secret_request`` (spec 19) closed
it for API keys: the tool takes structured parameters, delegates to a gateway
module that runs on the host, and returns only the outcome.

Deliberately NO ``command``/``args`` parameters — see spec 20 §3. An MCP
stdio server is a subprocess the gateway host spawns with the operator's own
user/filesystem and no sandbox of any kind (``tools/mcp_tool.py``'s stdio
transport); accepting an arbitrary command string from chat would hand a
remote/unauthenticated-by-default chat participant a way to run anything on
the host. Only http(s) MCP endpoints — reachable over the network the same
way ``web_search``/``browser_navigate`` already are — can be connected this
way. Pinned-catalog stdio servers (spec 20 Ruling 4) are a separate,
narrower follow-up, not this tool.

The secret, if the server needs one, is never a parameter here either: it is
requested from the chat via the EXISTING spec 19 primitive
(``tools.secret_capture_gateway.request_secret``, reached through
``tools/mcp_connect_gateway.py``) and never returns to this tool's result.

Deliberately NOT in ``_HERMES_CORE_TOOLS``: like ``secret_request``, this
tool only makes sense on a session that is a real chat with a human who
can both answer a token prompt and wait through a gateway restart. It lives
in its own toolset (``mcp``) so CLI/desktop/TUI sessions — which can already
edit ``config.yaml`` directly — never see it.
"""

from __future__ import annotations

import json
import logging
import re

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_mcp_connect_requirements() -> bool:
    """No external requirements — availability is entirely toolset-gated."""
    return True


def mcp_connect_tool(
    name: str,
    url: str,
    purpose: str,
    secret_env_var: str = "",
    task_id: str = None,
) -> str:
    name = (name or "").strip()
    url = (url or "").strip()
    purpose = (purpose or "").strip()
    secret_env_var = (secret_env_var or "").strip()

    if not name:
        return tool_error("name is required.")
    if not _NAME_RE.match(name):
        return tool_error(
            f"'{name}' is not a valid server name (letters, digits, "
            "underscore, hyphen; must start with a letter or digit)."
        )
    if not url:
        return tool_error("url is required.")
    if not purpose:
        return tool_error(
            "purpose is required — tell the user what this server is for."
        )
    if secret_env_var and not _ENV_VAR_NAME_RE.match(secret_env_var):
        return tool_error(
            f"'{secret_env_var}' is not a valid environment variable name "
            "(letters, digits, underscore; must not start with a digit)."
        )

    from gateway.session_context import get_session_env

    session_key = get_session_env("HERMES_SESSION_KEY", "")
    if not session_key:
        # Same lesson as secret_request (see tools/secret_request_tool.py):
        # a fresh tool result beats a rule in the prompt. Say what to do
        # next HERE, not just in a skill.
        return tool_error(
            "mcp_connect is not available in this session, and no other "
            "path can apply a server connection from here. Stop: do not "
            "probe the filesystem, do not look for config.yaml, and do not "
            "describe your environment to the user. Tell them to open "
            "/setup and add the server there themselves."
        )

    from tools.mcp_connect_gateway import connect_mcp_server

    try:
        result = connect_mcp_server(
            session_key=session_key,
            name=name,
            url=url,
            purpose=purpose,
            secret_env_var=secret_env_var,
        )
    except Exception as exc:  # noqa: BLE001
        # Ниже по стеку лежит запись конфига (``save_config`` умеет упасть на
        # нечитаемом файле) и сторонний клиент MCP. Сырое исключение ушло бы
        # модели мимо формата отказа, а вместе с ним — детали окружения и,
        # в худшем случае, подставленный заголовок с ключом. Сводим к тому же
        # формату, что и штатный отказ; подробность остаётся в логе.
        logger.exception("mcp_connect: непредвиденный отказ для %s", name)
        return json.dumps(
            {
                "name": name,
                "success": False,
                "tool_count": 0,
                "needs_restart": False,
                "reason": "unexpected_error",
                "message": (
                    f"Подключить «{name}» не удалось из-за внутренней ошибки. "
                    "Подробности в журнале."
                ),
            },
            ensure_ascii=False,
        )

    # Defense in depth: never let a raw token slip into the tool result even
    # if some future caller mistakenly stuffs one into the result dict.
    result.pop("value", None)

    return json.dumps(
        {
            "name": result.get("name") or name,
            "success": bool(result.get("success")),
            "tool_count": int(result.get("tool_count") or 0),
            "needs_restart": bool(result.get("needs_restart")),
            "reason": result.get("reason"),
            "message": result.get("message") or "",
        },
        ensure_ascii=False,
    )


MCP_CONNECT_SCHEMA = {
    "name": "mcp_connect",
    "description": (
        "Connect a remote HTTP MCP server the user described in chat (e.g. "
        "Bitrix24, n8n, Linear, a custom internal server). Only http(s) MCP "
        "endpoints are supported — there is no way to run an arbitrary local "
        "command from here. If the server needs a bearer token, pass "
        "secret_env_var with a descriptive environment variable name; the "
        "tool asks the user for the value in this chat and stores it "
        "securely — NEVER ask the user to paste a token into a message you "
        "compose yourself, and never pass a token value as a parameter. "
        "Applying a new connection needs a gateway restart, which happens "
        "automatically shortly after this turn's response is delivered — "
        "tell the user the server is connecting and there will be a short "
        "pause before its tools are usable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "A short identifier for the server, e.g. 'bitrix' or "
                    "'n8n'. Letters, digits, underscore, hyphen."
                ),
            },
            "url": {
                "type": "string",
                "description": "The server's HTTP(S) MCP endpoint URL.",
            },
            "purpose": {
                "type": "string",
                "description": (
                    "A short, human-readable explanation of what this "
                    "server is for, shown to the user if a token needs to "
                    "be requested, e.g. 'Bitrix24 CRM integration'."
                ),
            },
            "secret_env_var": {
                "type": "string",
                "description": (
                    "Optional. The environment variable name to store the "
                    "server's bearer token as, e.g. 'BITRIX_TOKEN'. Only "
                    "needed when the server requires authentication — pass "
                    "it up front if you already expect that, or call this "
                    "tool again with it set if the first attempt reports "
                    "the server needs one. NEVER pass the token value "
                    "itself here or anywhere else."
                ),
            },
        },
        "required": ["name", "url", "purpose"],
    },
}


registry.register(
    name="mcp_connect",
    toolset="mcp",
    schema=MCP_CONNECT_SCHEMA,
    handler=lambda args, **kw: mcp_connect_tool(
        name=args.get("name", ""),
        url=args.get("url", ""),
        purpose=args.get("purpose", ""),
        secret_env_var=args.get("secret_env_var", ""),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_mcp_connect_requirements,
    emoji="🔌",
)
