"""Per-user default model store (RAF-189).

A Telegram/user-level ``/model <name> --default`` stores the user's
personal default model here. Distinct from:

* session overrides (``_session_model_overrides`` / session store) — those
  are conversation-scoped and cleared on ``/new``; a user default must
  survive new sessions/topics;
* ``config.yaml`` ``model.default`` — that is the *server* default owned by
  the administrator; a user's personal choice is gateway state, not server
  configuration.

Storage: a small JSON file under ``<hermes home>/state/``. Resolving the
path through ``get_hermes_home()`` means the active profile override is
honored — under ``gateway.multiplex_profiles`` every profile (own home
directory) gets an isolated defaults table, so the same platform user has
independent defaults per profile. The file survives gateway restarts and
is small/hand-editable; no state.db schema change is needed.

Only non-secret fields are stored (model/provider/base_url); credentials
are re-resolved at use time via the normal runtime provider resolution
(mirrors session-override rehydration, which also never persists keys).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Optional

# Serialize whole-file read-modify-write cycles within this process. The
# gateway is single-process; the atomic rename below additionally tolerates
# concurrent writers from other processes (last write wins, like the rich
# sent index).
_LOCK = threading.Lock()

# Value keys allowed in a persisted entry. Everything else (api_key et al)
# is stripped — creds live in the auth store and are re-resolved on read.
_PERSISTABLE_KEYS = ("model", "provider", "base_url")

_FILENAME = "user_model_defaults.json"


def sanitize_user_model_default(value: Any) -> Optional[Dict[str, str]]:
    """Reduce *value* to the persistable non-secret subset.

    Returns ``None`` when nothing usable remains (e.g. empty dict, wrong
    type, or a model-less entry) — callers treat that as "no default".
    """
    if not isinstance(value, dict):
        return None
    cleaned: Dict[str, str] = {}
    for key in _PERSISTABLE_KEYS:
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            cleaned[key] = raw.strip()
    if not cleaned.get("model"):
        return None
    return cleaned


def user_model_default_key(source) -> str:
    """Build the user-scoped key ``<platform>:<user_id>`` for a source.

    Falls back to ``user_id_alt`` when the primary id is empty (platforms
    that only populate the alt id). An empty id yields an empty key, which
    callers treat as "cannot key a default" (no storage, no lookup).
    """
    platform = getattr(getattr(source, "platform", None), "value", None) or str(
        getattr(source, "platform", "") or ""
    )
    user_id = str(getattr(source, "user_id", "") or "")
    if not user_id:
        user_id = str(getattr(source, "user_id_alt", "") or "")
    if not user_id:
        return ""
    return f"{platform}:{user_id}"


def _store_path() -> str:
    # Resolve via get_hermes_home() so the active profile override is
    # honored (same scoping rule as every other per-home state file).
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    return os.path.join(str(home), "state", _FILENAME)


def _load_all(path: str) -> Dict[str, Dict[str, str]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned: Dict[str, Dict[str, str]] = {}
    for key, value in data.items():
        entry = sanitize_user_model_default(value)
        if entry is not None:
            cleaned[str(key)] = entry
    return cleaned


def _save_all(path: str, data: Dict[str, Dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)  # atomic; tolerates concurrent writers racing


def get_user_model_default(user_key: str) -> Optional[Dict[str, str]]:
    """Return the persisted default for *user_key*, or ``None``."""
    if not user_key:
        return None
    try:
        entry = _load_all(_store_path()).get(user_key)
    except Exception:
        return None
    return dict(entry) if entry else None


def set_user_model_default(user_key: str, value: Dict[str, Any]) -> bool:
    """Persist (or clear, when *value* sanitizes to nothing) a default.

    Returns True when the store was written.
    """
    if not user_key:
        return False
    cleaned = sanitize_user_model_default(value)
    with _LOCK:
        try:
            path = _store_path()
            data = _load_all(path)
            if cleaned is None:
                data.pop(user_key, None)
            else:
                data[user_key] = cleaned
            _save_all(path, data)
            return True
        except Exception:
            return False


def clear_user_model_default(user_key: str) -> bool:
    """Remove the persisted default for *user_key*.

    Returns True when the store was written (even if the key was absent).
    """
    if not user_key:
        return False
    with _LOCK:
        try:
            path = _store_path()
            data = _load_all(path)
            data.pop(user_key, None)
            _save_all(path, data)
            return True
        except Exception:
            return False
