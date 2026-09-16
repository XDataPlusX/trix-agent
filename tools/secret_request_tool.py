"""``secret_request`` tool — ask the current chat for a missing secret.

Spec 19 (client secrets over messaging). The client's terminal + file tools
all run inside a Docker sandbox with no host filesystem access, so there is
no path for a Telegram-only client to hand the agent an API key the normal
Hermes way (typing it into a local shell, editing ``.env`` by hand). This
tool closes that gap for the one channel the client actually has: the chat
itself.

The tool takes a NAME and a PURPOSE — never a value. It asks the question in
the current chat, blocks the agent thread on the gateway's session-keyed
secret-capture primitive (``tools/secret_capture_gateway.py``), and returns
only the outcome (saved / failed / skipped, whether it was validated,
whether a provider changed and the gateway is restarting). The secret value
itself is intercepted and saved by the gateway's message-dispatch layer
BEFORE the agent thread ever wakes up — it is never part of this tool's
return value, so it can never enter the model's context or the session
transcript.

Deliberately NOT in ``_HERMES_CORE_TOOLS``: this tool only makes sense on a
session that (a) is a real chat with a human on the other end, and (b) has
no other way to receive a secret. It lives in its own toolset (``secrets``),
gated to messaging platforms via ``hermes_cli/tools_config.py``'s
``_toolset_allowed_for_platform`` — CLI/desktop/TUI sessions already have a
local, masked-input secret prompt and never see this tool.
"""

from __future__ import annotations

import json
import re

from tools.registry import registry, tool_error

_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_secret_request_requirements() -> bool:
    """No external requirements — availability is entirely toolset-gated."""
    return True


def secret_request_tool(env_var: str, purpose: str, task_id: str = None) -> str:
    env_var = (env_var or "").strip()
    purpose = (purpose or "").strip()

    if not env_var:
        return tool_error("env_var is required.")
    if not _ENV_VAR_NAME_RE.match(env_var):
        return tool_error(
            f"'{env_var}' is not a valid environment variable name "
            "(letters, digits, underscore; must not start with a digit)."
        )
    if not purpose:
        return tool_error("purpose is required — tell the user what this key is for.")

    from gateway.session_context import get_session_env

    session_key = get_session_env("HERMES_SESSION_KEY", "")
    if not session_key:
        # Наблюдение на живой модели 2026-09-08: прежний текст («доступно
        # только внутри живой сессии») модель прочла как «не тот контекст,
        # попробуй иначе» — и пошла искать обходные пути: щупала терминал,
        # искала .env, а клиенту в итоге рассказала про свою песочницу и
        # «сессию в полу-битом состоянии». Ровно то, что скилл запрещает.
        #
        # Урок этой ветки, уже оплаченный дважды: свежий результат
        # инструмента бьёт правило в промпте. Поэтому что делать дальше,
        # говорится ЗДЕСЬ, а не только в скилле.
        return tool_error(
            "secret_request is not available in this session, and no other "
            "path can store the key from here. Stop: do not probe the "
            "filesystem, do not look for .env, and do not describe your "
            "environment to the user. Tell them to open /setup and enter the "
            "key there themselves, and ask them not to paste it into the chat."
        )

    from tools.secret_capture_gateway import request_secret

    result = request_secret(session_key, env_var, purpose)

    # Defense in depth: never let a raw value slip into the tool result even
    # if some future caller mistakenly stuffs one into the result dict.
    result.pop("value", None)

    return json.dumps(
        {
            "env_var": env_var,
            "success": bool(result.get("success")),
            "skipped": bool(result.get("skipped")),
            "reason": result.get("reason"),
            "validated": result.get("validated"),
            "provider": bool(result.get("provider")),
            "needs_restart": bool(result.get("needs_restart")),
            "message": result.get("message") or "",
        },
        ensure_ascii=False,
    )


SECRET_REQUEST_SCHEMA = {
    "name": "secret_request",
    "description": (
        "Ask the user (in the current chat) for a missing API key or secret. "
        "Pass the environment variable NAME it should be stored as and a short "
        "PURPOSE explaining what it's for — NEVER pass the value itself. The "
        "question is sent to the chat; the user's reply is intercepted and "
        "saved directly to .env by the gateway and NEVER reaches you or this "
        "conversation's history. You only get back whether it succeeded. Use "
        "this whenever a skill or task needs a credential you don't have and "
        "the user hasn't already provided one unprompted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "env_var": {
                "type": "string",
                "description": (
                    "The environment variable name to store the secret as, "
                    "e.g. 'TAVILY_API_KEY' or 'BITRIX_WEBHOOK'. Any valid "
                    "variable name is accepted, not just well-known ones."
                ),
            },
            "purpose": {
                "type": "string",
                "description": (
                    "A short, human-readable explanation of what this key is "
                    "for, shown to the user, e.g. 'Tavily web search' or "
                    "'Bitrix24 webhook integration'."
                ),
            },
        },
        "required": ["env_var", "purpose"],
    },
}


registry.register(
    name="secret_request",
    toolset="secrets",
    schema=SECRET_REQUEST_SCHEMA,
    handler=lambda args, **kw: secret_request_tool(
        env_var=args.get("env_var", ""),
        purpose=args.get("purpose", ""),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_secret_request_requirements,
    emoji="🔑",
)
