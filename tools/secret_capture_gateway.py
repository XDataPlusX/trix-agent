"""Gateway-side secret-capture primitive (spec 19 — client secrets over messaging).

Mirrors ``tools/clarify_gateway.py``'s shape (a blocking event-based queue,
keyed by ``session_key``) but with one load-bearing difference: **the raw
secret value never leaves this module**. ``clarify``'s answer is returned to
the model as tool output (see ``tools/clarify_tool.py``), which is exactly
why it cannot be reused as-is for a secret — the value would land in the
model's context and the session transcript forever.

Instead, the runner-side text intercept (``gateway/run.py::_handle_message``)
hands the raw reply text straight to :func:`resolve_secret_reply`, which
saves it to ``.env`` (via ``hermes_cli.config.save_env_value_secure``) and
immediately discards the local variable. Every other consumer — the
``secret_request`` tool waiting on the agent thread, the gateway dispatcher
wired into ``tools.skills_tool.set_secret_capture_callback`` — only ever sees
the *result* dict (``success``/``validated``/``provider``/``needs_restart``),
never the value.

Session-keyed by design (Ruling 1): ``set_secret_capture_callback()`` in
``tools/skills_tool.py`` is a MODULE-LEVEL GLOBAL, and the gateway serves many
concurrent sessions/platforms at once. Registering a capture handler from one
session must not open the gate for every other session, and the dispatcher
must be able to find out which chat to prompt. State here is keyed by
``session_key`` exactly like ``clarify_gateway``; the process-global callback
registered with ``skills_tool`` is a pure dispatcher that looks up the
CURRENT session (via ``gateway.session_context.get_session_env``) and
delegates to :func:`request_secret`.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# Module-level state
# =========================================================================

@dataclass
class _SecretCaptureEntry:
    """One pending secret-capture request inside a gateway session.

    ``result`` is set exactly once, by :func:`resolve_secret_reply` or a
    cancellation path — it NEVER carries the secret value itself, only the
    outcome (success/validated/provider/needs_restart/message).
    """
    request_id: str
    session_key: str
    env_var: str
    purpose: str
    event: threading.Event = field(default_factory=threading.Event)
    result: Optional[Dict[str, Any]] = None
    # Spec 19's Ruling: a captured TOOL key (not a model-provider key) is
    # by default also registered as sandbox env-passthrough, because the
    # agent calls that key FROM the container (see the comment above the
    # ``record_captured_passthrough`` call in ``_save_and_classify``).
    # ``mcp_connect`` (spec 20) captures a DIFFERENT kind of secret through
    # this exact same primitive: an MCP server's own bearer token. Nothing
    # inside the sandbox ever needs it — only the gateway's own host-side
    # MCP client reads it, to build the ``Authorization`` header when it
    # connects to the remote server. Passing it through anyway would hand
    # the raw token to any `terminal`/`execute_code` call the model makes
    # (e.g. `echo $BITRIX_TOKEN`), landing it in the sandbox and in the
    # session transcript — exactly what spec 19 §5.1 promises never
    # happens. Setting this False on registration keeps that promise for
    # MCP tokens while leaving spec 19's original tool-key behaviour
    # (default True) byte-for-byte unchanged.
    sandbox_passthrough: bool = True


_lock = threading.RLock()
# request_id → _SecretCaptureEntry
_entries: Dict[str, _SecretCaptureEntry] = {}
# session_key → list[request_id]  (FIFO; oldest-pending lookup + session cleanup)
_session_index: Dict[str, list] = {}
# session_key → notify callback (gateway → adapter bridge, mirrors
# clarify_gateway's _notify_cbs / tools.approval's _gateway_notify_cbs).
_notify_cbs: Dict[str, Callable[["_SecretCaptureEntry"], None]] = {}


# =========================================================================
# Public API — agent/tool-thread side
# =========================================================================

def register(
    request_id: str,
    session_key: str,
    env_var: str,
    purpose: str,
    sandbox_passthrough: bool = True,
) -> _SecretCaptureEntry:
    """Register a pending secret-capture request and return the entry.

    ``sandbox_passthrough=False`` marks this capture as a secret the sandbox
    must never see (see the field doc on :class:`_SecretCaptureEntry`) —
    used by ``mcp_connect`` (spec 20) for MCP server tokens. Every other
    caller keeps the spec 19 default of True.
    """
    entry = _SecretCaptureEntry(
        request_id=request_id,
        session_key=session_key,
        env_var=env_var,
        purpose=purpose,
        sandbox_passthrough=sandbox_passthrough,
    )
    with _lock:
        _entries[request_id] = entry
        _session_index.setdefault(session_key, []).append(request_id)
    return entry


def wait_for_response(request_id: str, timeout: float) -> Optional[Dict[str, Any]]:
    """Block until ``request_id`` resolves or the timeout fires.

    Polls in 1-second slices, same as ``clarify_gateway.wait_for_response``,
    so the agent's inactivity heartbeat keeps firing during a long wait.
    Returns the result dict (never the raw value) or ``None`` on timeout.
    """
    with _lock:
        entry = _entries.get(request_id)
    if entry is None:
        return None

    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:  # pragma: no cover - optional
        touch_activity_if_due = None

    unlimited = timeout is None or float(timeout) <= 0.0
    deadline = None if unlimited else time.monotonic() + float(timeout)
    activity_state = {"last_touch": time.monotonic(), "start": time.monotonic()}
    while True:
        if deadline is None:
            slice_s = 1.0
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            slice_s = min(1.0, remaining)
        if entry.event.wait(timeout=slice_s):
            break
        if touch_activity_if_due is not None:
            touch_activity_if_due(activity_state, "waiting for a secret from the user")

    with _lock:
        _entries.pop(request_id, None)
        ids = _session_index.get(entry.session_key)
        if ids and request_id in ids:
            ids.remove(request_id)
            if not ids:
                _session_index.pop(entry.session_key, None)

    return entry.result


def request_secret(
    session_key: str,
    env_var: str,
    purpose: str,
    timeout: Optional[float] = None,
    sandbox_passthrough: bool = True,
) -> Dict[str, Any]:
    """Register + notify + wait, in one call.

    Shared by the ``secret_request`` tool AND the gateway dispatcher wired
    into ``tools.skills_tool.set_secret_capture_callback`` (Ruling 1/2), so
    both entry points drive the exact same capture machinery. Returns a
    result dict that never carries the secret value; ``skipped=True`` means
    no capture ever happened (no notify registered for this session, or the
    notify callback itself failed) and the caller should treat this like a
    declined/unsupported request.

    ``sandbox_passthrough=False`` (used by ``mcp_connect``, spec 20) marks
    the captured value as one the Docker sandbox must never receive, even
    though it is a non-provider ("tool") key that would otherwise default to
    passthrough under spec 19. See :class:`_SecretCaptureEntry`.
    """
    notify = get_notify(session_key)
    if notify is None:
        # Раньше здесь был молчаливый выход, и наблюдение за живым агентом
        # показало его цену: получив "unsupported", модель бросает механизм
        # и уходит искать CLI по файловой системе — пять лишних вызовов и
        # совет клиенту выполнить команду в шелле, которого у него нет.
        #
        # Отказ обязан называть себя и в лог, и модели. В логе — ключ сессии
        # и то, что вообще зарегистрировано: расхождение ключей становится
        # самодиагностируемым с одной строки, а не остаётся невидимым.
        logger.warning(
            "Secret capture unavailable for %s: no notify registered for "
            "session %r (registered: %s)",
            env_var, session_key, sorted(_notify_cbs)[:8],
        )
        return {
            "success": False,
            "stored_as": env_var,
            "validated": False,
            "skipped": True,
            "reason": "no_capture_registered",
            "message": (
                "This chat cannot capture secrets right now. Tell the user "
                "plainly that you could not store the key and stop — do NOT "
                "search the filesystem for a CLI or another way to save it."
            ),
        }

    request_id = uuid.uuid4().hex[:10]
    entry = register(
        request_id, session_key, env_var, purpose,
        sandbox_passthrough=sandbox_passthrough,
    )
    try:
        notify(entry)
    except Exception:
        logger.warning("Secret-capture notify failed for %s", env_var, exc_info=True)
        with _lock:
            _entries.pop(request_id, None)
            ids = _session_index.get(session_key)
            if ids and request_id in ids:
                ids.remove(request_id)
                if not ids:
                    _session_index.pop(session_key, None)
        return {
            "success": False,
            "stored_as": env_var,
            "validated": False,
            "skipped": True,
            "reason": "notify_failed",
        }

    if timeout is None:
        timeout = get_secret_request_timeout()
    result = wait_for_response(request_id, timeout=float(timeout))
    if result is None:
        return {
            "success": False,
            "stored_as": env_var,
            "validated": False,
            "skipped": True,
            "reason": "timeout",
        }
    return result


# =========================================================================
# Public API — gateway / adapter side
# =========================================================================

def get_pending_for_session(session_key: str) -> Optional[_SecretCaptureEntry]:
    """Return the oldest pending secret-capture entry for a session, or None."""
    with _lock:
        ids = _session_index.get(session_key) or []
        for rid in ids:
            entry = _entries.get(rid)
            if entry is not None:
                return entry
        return None


def has_pending(session_key: str) -> bool:
    """True when this session has at least one pending secret-capture entry."""
    return get_pending_for_session(session_key) is not None


def resolve_secret_reply(session_key: str, raw_text: str) -> Optional[Dict[str, Any]]:
    """Consume the raw reply text for the oldest pending capture in a session.

    THE VALUE STOPS HERE. This is the one function in the whole feature that
    ever sees the secret in cleartext: it saves it (``save_env_value_secure``
    -> ``credential_lifecycle.save_provider_env_credential``, which also
    scrubs stale config.yaml mirrors on rotation — bug #62269), optionally
    runs a cheap live probe for a known provider credential, decides whether
    the change needs ``reload_env()`` (tool keys) or a full
    ``restart_gateway()`` (model-provider keys, Ruling 7), and returns a
    result dict — never the raw text — which becomes both the resolved
    entry's ``result`` (seen by anyone blocked in ``wait_for_response``) and
    the caller's own return value (used by the gateway to compose the
    client-facing confirmation).

    Returns ``None`` when no pending capture exists for this session (the
    caller should treat the message as ordinary text, not a secret reply).
    """
    entry = get_pending_for_session(session_key)
    if entry is None:
        return None

    value = (raw_text or "").strip()
    if not value:
        outcome = {
            "success": False,
            "stored_as": entry.env_var,
            "purpose": entry.purpose,
            "validated": False,
            "provider": False,
            "skipped": True,
            "reason": "empty",
            "needs_restart": False,
            "message": "",
        }
    else:
        outcome = _save_and_classify(
            entry.env_var, entry.purpose, value,
            sandbox_passthrough=entry.sandbox_passthrough,
        )
    value = None  # noqa: F841 — drop the only reference to the secret ASAP

    # Mirrors clarify_gateway.resolve_gateway_clarify: set the result and
    # fire the event, but leave removal from _entries/_session_index to
    # wait_for_response(). Popping here too would race a notify callback
    # that resolves synchronously (before request_secret ever calls
    # wait_for_response) — wait_for_response re-fetches the entry by id and
    # would find it already gone, misreporting a resolved capture as a
    # timeout.
    entry.result = outcome
    entry.event.set()
    return outcome


def _is_model_provider_env_var(env_var: str) -> bool:
    """True when ``env_var`` is a registered model-provider api key or base URL."""
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
    except Exception:
        return False
    for cfg in PROVIDER_REGISTRY.values():
        try:
            if env_var in (cfg.api_key_env_vars or ()) or env_var == cfg.base_url_env_var:
                return True
        except Exception:
            continue
    return False


def _save_and_classify(
    env_var: str,
    purpose: str,
    value: str,
    sandbox_passthrough: bool = True,
) -> Dict[str, Any]:
    """Save ``value`` for ``env_var`` and classify the result. Never returns it."""
    from hermes_cli.config import save_env_value_secure

    is_provider = _is_model_provider_env_var(env_var)

    validated: Optional[bool] = None
    reason: Optional[str] = None
    if is_provider:
        try:
            from hermes_cli.credential_probes import probe_provider_key

            probe = probe_provider_key(env_var, value)
            # probe_provider_key has no "a real check happened" flag of its
            # own (that's `checked=True`, a wrapper-only field added by
            # hermes_cli.setup_wizard.validate.check_provider_key, which we
            # don't call). ``reachable`` is the correct signal here: an
            # unknown provider or a derived-URL miss returns ok=True with
            # reachable=False ("don't block"), which must NOT be reported
            # to the client as "the provider confirmed this key" — that
            # would be a validation result nobody actually produced.
            if probe.get("reachable"):
                validated = bool(probe.get("ok"))
                reason = probe.get("reason")
        except Exception:
            logger.debug("Provider key probe failed for %s", env_var, exc_info=True)

    try:
        save_env_value_secure(env_var, value)
        success = True
    except Exception:
        logger.warning("Failed to save captured secret for %s", env_var, exc_info=True)
        success = False

    if success and not is_provider:
        try:
            from hermes_cli.config import reload_env

            reload_env()
        except Exception:
            logger.debug("reload_env() failed after saving %s", env_var, exc_info=True)

        # Значения в `.env` мало: агент зовёт такой ключ из `terminal`, то
        # есть ИЗ КОНТЕЙНЕРА, а туда переменные попадают только через набор
        # проброса. Без этой строки клиент отдавал ключ, агент видел внутри
        # пустоту, не мог проверить вебхук и спрашивал ключ второй раз —
        # снято с машины 2026-09-10.
        #
        # Только для ключей инструментов: ветка `not is_provider`. Ключ
        # провайдера едет через рестарт и в песочницу не попадает никогда
        # (спека 19, Ruling 7), а `record_captured_passthrough` откажет ему
        # и сам — вторым рубежом, по имени (GHSA-rhgp-j443-p4rf).
        #
        # `sandbox_passthrough=False` — второй, независимый отказ: ключ
        # MCP-сервера (спека 20, `mcp_connect`) агенту в контейнере не нужен
        # ни для чего, им пользуется только хостовый MCP-клиент шлюза.
        # Умолчание (True) не меняется — это осознанное решение спеки 19
        # для обычных ключей инструментов.
        if sandbox_passthrough:
            try:
                from tools.env_passthrough import record_captured_passthrough

                record_captured_passthrough(env_var)
            except Exception:
                logger.debug(
                    "не удалось открыть %s для песочницы", env_var, exc_info=True
                )

    return {
        "success": success,
        "stored_as": env_var,
        "purpose": purpose,
        "validated": validated,
        "reason": reason,
        "provider": is_provider,
        "skipped": False,
        "needs_restart": bool(success and is_provider),
        "message": "Secret stored securely. The value was never exposed to the model.",
    }


def clear_session(session_key: str) -> int:
    """Resolve and drop every pending secret capture for a session.

    Used at turn end (mirrors ``clarify_gateway.clear_session``) so a
    blocked agent thread doesn't hang past the end of its run.
    """
    with _lock:
        ids = list(_session_index.pop(session_key, []) or [])
        entries = [_entries.pop(rid, None) for rid in ids]
    cancelled = 0
    for entry in entries:
        if entry is None:
            continue
        entry.result = {
            "success": False,
            "stored_as": entry.env_var,
            "validated": False,
            "provider": False,
            "skipped": True,
            "reason": "cancelled",
            "needs_restart": False,
            "message": "",
        }
        entry.event.set()
        cancelled += 1
    return cancelled


def register_notify(session_key: str, cb: Callable[[_SecretCaptureEntry], None]) -> None:
    """Register the per-session notify callback (sends the question to the user)."""
    with _lock:
        _notify_cbs[session_key] = cb


def unregister_notify(session_key: str) -> None:
    """Drop the per-session notify callback and cancel any pending captures."""
    with _lock:
        _notify_cbs.pop(session_key, None)
    clear_session(session_key)


def get_notify(session_key: str) -> Optional[Callable[[_SecretCaptureEntry], None]]:
    with _lock:
        return _notify_cbs.get(session_key)


# =========================================================================
# Process-global dispatcher for tools.skills_tool.set_secret_capture_callback
# =========================================================================

def gateway_capture_dispatcher(
    env_var: str, prompt: str, metadata: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """The ONE process-global callback the gateway registers (Ruling 1).

    ``tools.skills_tool.set_secret_capture_callback`` is a module-level
    global — safe to point at this function once (it is idempotent and
    carries no per-turn state of its own) precisely BECAUSE all the actual
    per-turn state lives in this module's session-keyed tables. This
    function does nothing but look up the session the CURRENT call is
    running in (a real ContextVar, never a shared/racy env var) and hand
    off to :func:`request_secret` for that session alone — it can never
    resolve or notify a different session, even when two gateway sessions
    call into ``tools.skills_tool`` concurrently.
    """
    from gateway.session_context import get_session_env

    session_key = get_session_env("HERMES_SESSION_KEY", "")
    if not session_key:
        return {
            "success": False,
            "stored_as": env_var,
            "validated": False,
            "skipped": True,
            "reason": "no_session",
        }
    purpose = (prompt or "").strip() or f"Required for {env_var}"
    return request_secret(session_key, env_var, purpose)


# =========================================================================
# Config
# =========================================================================

def get_secret_request_timeout() -> int:
    """Reuse the clarify timeout — same "give the user time to reply" contract."""
    try:
        from tools.clarify_gateway import get_clarify_timeout

        return get_clarify_timeout()
    except Exception:
        return 3600
