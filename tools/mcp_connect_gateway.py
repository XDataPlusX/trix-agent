"""Gateway-side MCP-connect executor (spec 20 — client connects MCP over messaging).

Mirrors ``tools/secret_capture_gateway.py``'s shape (module-level state keyed
by ``session_key``, a per-session notify callback registered/unregistered by
``gateway/run.py`` around the same turn that runs ``agent.run_conversation``)
because it exists to close the exact same wall spec 19 closed: a Telegram-only
client's terminal/file tools run inside the Docker sandbox and cannot reach
the host's ``config.yaml``, so applying a new MCP server has to happen here,
on the gateway host process, driven by a tool call.

Connection steps (spec 20 §2, "Что у Hermes уже есть" — every piece below is
an EXISTING Hermes primitive, reused as-is):

1. Probe the server unauthenticated (``hermes_cli.mcp_config._probe_single_server``).
   Success means no token is needed — skip straight to saving.
2. An auth-shaped failure means a token is needed — detected via
   ``tools.mcp_tool._is_auth_error`` / an httpx 401-403 ``.response``, after
   unwrapping anyio's ``BaseExceptionGroup``, OR (see ``_probe_needs_auth``)
   a textual auth marker in the unwrapped message for SDK errors that carry
   no ``.response`` at all (``mcp.shared.exceptions.McpError``, common for
   self-hosted servers and n8n). If the caller passed ``secret_env_var``,
   request it from the chat via the
   EXISTING spec 19 primitive (``tools.secret_capture_gateway.request_secret``)
   — never a second capture mechanism. If no ``secret_env_var`` was given,
   fail with a reason the model can act on (call again with one set).
3. Re-probe with an ``Authorization: Bearer ${VAR}`` header referencing the
   just-captured variable, to confirm the token actually works and count the
   server's tools.
4. Validate the assembled entry (``hermes_cli.mcp_security.validate_mcp_server_entry``)
   and save it (``hermes_cli.mcp_config._save_mcp_server``, which validates a
   second time internally — belt and braces, matches the CLI/dashboard paths).

Why this does NOT call ``hermes_cli.mcp_config._save_bearer_auth_token``:
that function persists a *raw token value* the caller already has in hand
(the CLI wizard prompts for it directly, in-process). Here the token comes
through ``secret_capture_gateway.request_secret()``, whose entire point
(spec 19 Ruling — "the value stops there") is that the raw value NEVER comes
back to any caller, this module included. So the only thing this module can
do with the captured secret is reference it by the name it was asked for —
which is exactly what ``secret_env_var`` is: the model picks the variable
name (shown to the user in the capture prompt), and the assembled header
references that SAME name. ``_env_key_for_server``'s server-name-derived
convention is what the interactive CLI uses instead, precisely because it
never goes through the chat-capture path.

One more divergence from the spec-19 default: the call passes
``sandbox_passthrough=False``. Spec 19's ``_save_and_classify`` normally
also registers a captured non-provider key as Docker-sandbox env
passthrough, because the agent calls a captured TOOL key (e.g. Tavily) FROM
inside the container. An MCP server's bearer token is a different kind of
secret — only this module's own host-side probe/save path ever reads it, to
build the ``Authorization`` header; the sandboxed agent has no legitimate
use for it at all. Leaving passthrough on would let the model read the raw
token back with a plain ``terminal("echo $VAR")`` call, landing it in the
sandbox and the session transcript — exactly what spec 20 §5.1 promises
never happens.

Applying a saved server (Ruling 3 — restart, not hot-reload) is NOT driven
from here: this module only marks the session as pending a restart
(:func:`mark_pending_restart`). ``gateway/run.py`` consumes that flag at
*turn* boundary, after the model's own final response for this turn has been
delivered to the adapter (mirrors ``_post_turn_goal_continuation`` /
``_defer_goal_status_notice_after_delivery``'s post-delivery-callback seam) —
deliberately NOT the synchronous "send then restart in a thread" snippet
spec 19 uses at its message-interception seam (``gateway/run.py`` Ruling 7,
~15662-15683). That snippet runs OUTSIDE any model turn (it intercepts a
chat reply before the agent loop even starts), so restarting synchronously
right after sending its own confirmation cannot kill an in-flight turn.
``mcp_connect`` is different: it is one tool call INSIDE a live agent turn,
and the model still has to produce (and the gateway still has to deliver)
its own final text after the tool returns. Restarting synchronously here —
even after sending an emphatic "connected" notice — risks killing the
gateway process mid-turn, before the model's own reply is ever sent. Tying
the restart to post-delivery is the only shape that keeps "response before
restart" true for a tool-triggered application.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Textual fallback for _probe_needs_auth (see its docstring): a short,
# specific vocabulary that a plain "server unreachable" failure does not
# share, so it can't misfire into asking for a secret the server never
# actually wanted.
_AUTH_TEXT_MARKERS = ("unauthorized", "forbidden", "authentication", "401", "403")


# =========================================================================
# Module-level state
# =========================================================================

_lock = threading.RLock()
# session_key → notify callback (best-effort status ping, gateway → adapter
# bridge, mirrors secret_capture_gateway's _notify_cbs). Used ONLY to tell
# the chat a (possibly slow) probe/connect is in progress — never to send
# the final confirmation, and never paired with a restart (see module
# docstring for why that has to wait for post-delivery).
_notify_cbs: Dict[str, Callable[[Dict[str, Any]], None]] = {}
# session_key set → this session has a saved-but-not-yet-applied MCP server
# change, consumed (popped) exactly once by gateway/run.py after the current
# turn's response has been delivered.
_pending_restart: set = set()


def register_notify(session_key: str, cb: Callable[[Dict[str, Any]], None]) -> None:
    """Register the per-session status-notify callback."""
    if not session_key:
        return
    with _lock:
        _notify_cbs[session_key] = cb


def unregister_notify(session_key: str) -> None:
    """Drop the per-session status-notify callback."""
    with _lock:
        _notify_cbs.pop(session_key, None)


def get_notify(session_key: str) -> Optional[Callable[[Dict[str, Any]], None]]:
    with _lock:
        return _notify_cbs.get(session_key)


def _notify_status(session_key: str, name: str, url: str) -> None:
    """Best-effort "connecting..." ping. Never raises, never blocks on I/O
    itself (the registered callback is responsible for scheduling its own
    send); a missing/failing notify is not a connect failure.
    """
    cb = get_notify(session_key)
    if cb is None:
        return
    try:
        cb({"name": name, "url": url})
    except Exception:
        logger.debug("mcp_connect status notify failed for %s", name, exc_info=True)


def mark_pending_restart(session_key: str) -> None:
    if not session_key:
        return
    with _lock:
        _pending_restart.add(session_key)


def pop_pending_restart(session_key: str) -> bool:
    """Check-and-clear. True at most once per successful connect."""
    if not session_key:
        return False
    with _lock:
        if session_key in _pending_restart:
            _pending_restart.discard(session_key)
            return True
        return False


# =========================================================================
# Connect
# =========================================================================

def _failure(name: str, reason: str, message: str) -> Dict[str, Any]:
    return {
        "success": False,
        "name": name,
        "tool_count": 0,
        "needs_restart": False,
        "reason": reason,
        "message": message,
    }


def _probe_needs_auth(exc: BaseException) -> bool:
    """True when ``exc`` (from ``_probe_single_server``) looks like a 401/403.

    Three detection layers, checked in order:

    1. ``_is_auth_error`` — the MCP SDK's own OAuth exception types, plus
       ``httpx.HTTPStatusError`` gated to status 401 (``tools.mcp_tool``'s
       own contract).
    2. Any exception carrying ``.response.status_code`` in (401, 403) that
       ``_is_auth_error``'s narrower type-gate missed.
    3. A textual auth marker in the unwrapped exception's message. Layers 1
       and 2 both assume the failure surfaces as an ``httpx`` error with a
       real HTTP response — true for a plain 401/403 from an HTTP MCP
       endpoint, but the MCP SDK frequently reports an auth rejection as
       ``mcp.shared.exceptions.McpError`` instead: a JSON-RPC error with a
       ``.error.message`` and NO ``.response`` attribute at all (this is the
       common case for self-hosted servers and n8n). Without this layer,
       such a failure falls through as ``probe_failed`` and the model's
       documented recovery ("call again with secret_env_var") hits the same
       dead end forever, because ``_probe_needs_auth`` never asked for a
       secret in the first place.

       Matched against a short, specific vocabulary (unauthorized /
       forbidden / authentication / 401 / 403) — NOT "any probe failure" —
       so a server that is merely unreachable ("Connection refused", "Name
       or service not known", a bare timeout) is never misclassified as
       needing a secret; none of those messages share this vocabulary.
    """
    try:
        from tools.mcp_tool import _is_auth_error, _unwrap_exception_group
    except Exception:  # pragma: no cover - defensive
        return False
    try:
        root = _unwrap_exception_group(exc)
    except Exception:
        root = exc
    if _is_auth_error(root):
        return True
    status = getattr(getattr(root, "response", None), "status_code", None)
    if status in (401, 403):
        return True
    text = str(root).lower()
    return any(marker in text for marker in _AUTH_TEXT_MARKERS)


def _normalize_saved_bearer_token(env_var: str) -> None:
    """Срезать лишнее слово ``Bearer`` у токена, уже сохранённого в ``.env``.

    Заголовок собирается как ``Authorization: Bearer ${ПЕРЕМЕННАЯ}``. Клиент,
    скопировавший строку из документации сервиса целиком, присылает значение
    вместе со словом ``Bearer`` — сервер получает ``Bearer Bearer <токен>`` и
    отвечает отказом, а причина по сообщению не видна.

    У апстрима эта нормализация живёт в ``_save_bearer_auth_token``
    (``hermes_cli/mcp_config.py``, дефект #37792), но тот путь требует СЫРОЕ
    значение токена, а механизм спеки 19 сырое значение наружу принципиально
    не отдаёт. Поэтому правим уже сохранённое: значение читается и пишется
    хост-стороной, в контекст модели не попадает и в лог не уходит.

    Молча ничего не делает, если переменной нет или префикса в ней нет.
    """
    try:
        from hermes_cli.config import load_env, save_env_value
        from hermes_cli.mcp_config import _strip_bearer_prefix

        current = (load_env() or {}).get(env_var)
        if not current:
            return
        normalized = _strip_bearer_prefix(current)
        if normalized and normalized != current:
            save_env_value(env_var, normalized)
            logger.info(
                "mcp_connect: у значения %s срезан лишний префикс Bearer", env_var
            )
    except Exception as exc:  # noqa: BLE001 — нормализация не должна ронять подключение
        logger.debug("mcp_connect: не удалось нормализовать токен: %s", exc)


def connect_mcp_server(
    session_key: str,
    name: str,
    url: str,
    purpose: str,
    secret_env_var: Optional[str] = None,
) -> Dict[str, Any]:
    """Probe, optionally capture a token, validate, and save one MCP server.

    Returns a result dict — never the token value. ``needs_restart`` is True
    only on success (every saved server needs the gateway process to pick up
    a fresh MCP client set, Ruling 3); the caller marks the session pending
    and the actual restart is applied by ``gateway/run.py`` after this turn's
    response is delivered.
    """
    name = (name or "").strip()
    url = (url or "").strip()
    purpose = (purpose or "").strip()
    secret_env_var = (secret_env_var or "").strip()

    if not name or not _NAME_RE.match(name):
        return _failure(
            name,
            "invalid_name",
            "Имя сервера должно состоять из латинских букв, цифр, "
            "подчёркивания или дефиса.",
        )
    if not purpose:
        return _failure(name, "missing_purpose", "Не указано, зачем нужен сервер.")
    if secret_env_var and not _ENV_VAR_NAME_RE.match(secret_env_var):
        return _failure(
            name,
            "invalid_secret_env_var",
            f"'{secret_env_var}' — не имя переменной окружения.",
        )

    from hermes_cli.mcp_config import _probe_single_server, _save_mcp_server
    from hermes_cli.mcp_security import validate_mcp_server_entry
    from tools.mcp_tool import InvalidMcpUrlError, _validate_remote_mcp_url

    try:
        clean_url = _validate_remote_mcp_url(name, url)
    except InvalidMcpUrlError as exc:
        return _failure(name, "invalid_url", str(exc))

    _notify_status(session_key, name, clean_url)

    entry: Dict[str, Any] = {"url": clean_url}
    tool_count = 0

    try:
        found = _probe_single_server(name, dict(entry))
        tool_count = len(found)
    except Exception as exc:
        if not _probe_needs_auth(exc):
            return _failure(
                name,
                "probe_failed",
                f"Не удалось подключиться к «{name}»: {exc}",
            )
        if not secret_env_var:
            return _failure(
                name,
                "needs_secret_env_var",
                f"Серверу «{name}» нужен ключ доступа, но имя переменной для "
                "него не передано. Вызови mcp_connect ещё раз с secret_env_var.",
            )

        from tools.secret_capture_gateway import request_secret

        # sandbox_passthrough=False: unlike a spec-19 tool key, the MCP
        # server's token is never needed inside the Docker sandbox — only
        # the gateway's own host-side MCP client reads it, to build the
        # ``Authorization`` header below. Passing it through the default
        # spec-19 route would let the model read it back with a plain
        # `terminal("echo $VAR")` call and land it in the transcript.
        secret_result = request_secret(
            session_key, secret_env_var, purpose, sandbox_passthrough=False,
        )
        # Defense in depth: the primitive never puts the value here, but a
        # future caller mistake must not leak it past this module either.
        secret_result.pop("value", None)
        if not secret_result.get("success"):
            return _failure(
                name,
                secret_result.get("reason") or "secret_not_captured",
                secret_result.get("message")
                or "Не удалось получить ключ от пользователя.",
            )

        _normalize_saved_bearer_token(secret_env_var)
        entry["headers"] = {"Authorization": f"Bearer ${{{secret_env_var}}}"}
        try:
            found = _probe_single_server(name, dict(entry))
            tool_count = len(found)
        except Exception as exc2:
            return _failure(
                name,
                "probe_failed_after_auth",
                f"Ключ сохранён, но подключиться к «{name}» всё равно не "
                f"удалось: {exc2}",
            )

    issues = validate_mcp_server_entry(name, entry)
    if issues:
        return _failure(name, "validation_failed", "; ".join(issues))

    if not _save_mcp_server(name, entry):
        return _failure(
            name,
            "save_rejected",
            f"Запись для «{name}» не прошла проверку безопасности и не "
            "сохранена.",
        )

    mark_pending_restart(session_key)

    return {
        "success": True,
        "name": name,
        "tool_count": tool_count,
        "needs_restart": True,
        "reason": None,
        "message": f"Сервер «{name}» подключён, инструментов найдено: {tool_count}.",
    }
