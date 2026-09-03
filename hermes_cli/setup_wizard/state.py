"""Setup-wizard state: two credential slots, per-IP + global lockout.

Implements spec 8 (``docs/product/specs/2026-08-25-trix-agent-wizard-
permanent-access-design.md``), §4.2/§4.3/§8.1/§8.3. Two independent
credential slots live in ``state.json``:

- ``primary`` — permanent login + password hash, issued once by
  ``bootstrap`` from cloud-init-generated values (§9.1). Never expires,
  never overwritten by ``temporary``.
- ``temporary`` — emergency password issued by ``hermes setup-wizard
  open`` (§9.3). No login of its own (any login is accepted against it);
  lives only for its TTL and does not clear on first successful use —
  HTTP Basic auth (§8.3) resends credentials on every request, so a
  one-shot password would die on the second request of the same login.

``completed`` no longer gates access (§4.3) — it only records that the
wizard's first-run form has been submitted once, so the caller can decide
to render it in "return visit" mode. The actual open/closed gate is the
``disabled`` flag, flipped by ``hermes setup-wizard close``/
``install-service``.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import stat
import time
import uuid
from pathlib import Path

from hermes_constants import get_hermes_home, secure_parent_dir
from plugins.dashboard_auth.basic import hash_password as _hash_password
from plugins.dashboard_auth.basic import verify_password as _verify_password_hash
from utils import atomic_replace

# Per-IP lockout (§8.1): 5 free attempts, then exponential backoff capped
# at 15 minutes for that IP.
_MAX_FREE_ATTEMPTS = 5
_IP_LOCK_CAP_SECONDS = 15 * 60

# Global circuit breaker (§8.1): more than 50 failures from any IP within
# a trailing hour trips a flat (non-exponential) 60s slowdown for
# everyone. It is a brake, never a gate — it must not be able to close
# login entirely, which is why it is a small flat delay rather than an
# escalating lockout.
_GLOBAL_FAILURE_WINDOW_SECONDS = 60 * 60
_GLOBAL_FAILURE_THRESHOLD = 50
_GLOBAL_SLOWDOWN_SECONDS = 60
# Bounded history for the global failure timeline so a flood can't grow
# state.json without limit; we only ever need "how many in the last
# hour", so a cap comfortably above the trip threshold is enough headroom.
_MAX_GLOBAL_FAILURE_SAMPLES = 500

# failures_by_ip housekeeping (§8.1: "чистятся по TTL... ограничено
# сверху"). Entries older than this are dropped on the next mutation;
# if the map is still oversized after that, the oldest-by-last-seen
# entries are evicted until it fits. These two numbers aren't dictated
# by the spec — chosen generously above what 8443 realistically sees.
_IP_FAILURE_ENTRY_TTL_SECONDS = 24 * 60 * 60
_MAX_IP_FAILURE_ENTRIES = 2000


def state_path() -> Path:
    return get_hermes_home() / "setup-wizard" / "state.json"


def _read_state_dict() -> dict:
    """Read ``state.json`` from disk, tolerating a missing/corrupt file.

    Returns ``{}`` for: no file, unparsable JSON, or JSON whose top-level
    value isn't an object (e.g. a stray ``[]``/``null``/``"x"`` left by a
    partial write or manual edit). Also tolerates the pre-spec-8 flat
    format (``algo``/``salt``/``hash`` at the top level, no ``primary``/
    ``temporary`` slots) — spec §9.6 requires that old shape not crash the
    reader; it is simply read as "no primary, no temporary" since none of
    its keys collide with the new ones. Callers must never see an
    exception from this helper — a corrupt file must fail closed (treated
    as "no credentials issued"), not crash the caller.
    """
    p = state_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


class WizardState:
    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def load(cls) -> "WizardState":
        return cls(_read_state_dict())

    def _reload(self) -> None:
        """Refresh ``self._data`` from disk before mutating it.

        The wizard (web request handler) and the gateway are separate OS
        processes and can each hold a live ``WizardState`` instance backed
        by the same on-disk file. Without a read-modify-write here, a
        stale in-memory copy would write its own (outdated) view back over
        a mutation made by the other process in the meantime — e.g.
        resurrecting a failure count another process had already reset, or
        clobbering a ``temporary`` slot the other process just issued.
        Re-reading immediately before every mutation keeps cross-process
        state consistent, not just within a single instance.
        """
        self._data = _read_state_dict()

    @staticmethod
    def _num_from(d: dict, key: str, default: float) -> float:
        """Read a numeric field from an arbitrary dict, failing closed.

        Returns ``default`` when the field is missing or is not an
        ``int``/``float`` (``bool`` is excluded even though it subclasses
        ``int`` in Python — a stray JSON boolean is never a valid value
        for these fields). Callers must never see a ``TypeError``/
        ``ValueError`` from a hand-edited or corrupted ``state.json``
        propagate out of arithmetic on timestamps/counters.
        """
        val = d.get(key, default)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return default
        return val

    def _num(self, key: str, default: float = 0.0) -> float:
        return self._num_from(self._data, key, default)

    def _save(self) -> None:
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # secure_parent_dir refuses to chmod / or top-level dirs (#25821).
        secure_parent_dir(p)
        # Per-process + random temp suffix avoids collisions between
        # concurrent writers and stale leftovers from a crashed prior write.
        tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
        # Create with 0o600 atomically via os.open(O_EXCL) — closes the
        # TOCTOU window where write_text() + a post-write chmod would
        # briefly expose the password hash at the process umask.
        fd = os.open(
            str(tmp),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._data))
                fh.flush()
                os.fsync(fh.fileno())
            atomic_replace(tmp, p)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    # ---- credential issuance (§4.2, §9.1, §9.3) ----------------------------

    def issue_primary(self, login: str, password: str) -> None:
        """Write the permanent slot.

        ``password`` is a plaintext value the caller already has (cloud-init
        generates it — §9.1/§9.5; this method never generates one itself).
        It is hashed here, in the same ``scrypt$n$r$p$<salt_b64>$<dk_b64>``
        format the dashboard's ``basic`` auth plugin uses (§13.1), and the
        plaintext is never written to disk or returned. Does not touch
        ``temporary``.
        """
        self._reload()
        self._data["primary"] = {
            "login": login,
            "password_hash": _hash_password(password),
        }
        self._save()

    def issue_temporary(self, password: str, ttl_seconds: int) -> None:
        """Write the emergency slot (§4.2/§9.3) without touching ``primary``.

        Like ``issue_primary``, ``password`` is a plaintext value supplied
        by the caller (``hermes setup-wizard open`` generates it) and is
        hashed here, never stored in the clear.
        """
        self._reload()
        self._data["temporary"] = {
            "password_hash": _hash_password(password),
            "expires_at": time.time() + ttl_seconds,
        }
        self._save()

    # ---- verification (§4.3, §8.1, §8.3.2) ---------------------------------

    def _check_primary(self, login: str, password: str) -> bool:
        primary = self._data.get("primary")
        if not isinstance(primary, dict):
            return False
        stored_login = primary.get("login")
        stored_hash = primary.get("password_hash")
        if not isinstance(stored_login, str) or not isinstance(stored_hash, str):
            return False
        if login != stored_login:
            return False
        return _verify_password_hash(password, stored_hash)

    def _check_temporary(self, password: str, now: float) -> bool:
        temp = self._data.get("temporary")
        if not isinstance(temp, dict):
            return False
        stored_hash = temp.get("password_hash")
        if not isinstance(stored_hash, str):
            return False
        exp = temp.get("expires_at")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return False
        if now >= exp:
            return False
        return _verify_password_hash(password, stored_hash)

    @staticmethod
    def _normalize_ip(ip: str) -> str:
        """Bucket key for per-IP failure accounting.

        IPv4 addresses are used as-is. IPv6 addresses are aggregated to
        their /64 network (§8.1) — a single IPv6-enabled host typically
        controls its whole /64, so keying on the full address would let it
        dodge the 5-attempt budget by rotating addresses within its own
        prefix. Unparseable input falls back to the raw stripped string —
        that only affects which bucket a malformed value lands in, never
        whether a lockout is enforced.
        """
        if not isinstance(ip, str) or not ip.strip():
            return "unknown"
        raw = ip.strip()
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return raw
        if isinstance(addr, ipaddress.IPv6Address):
            network = ipaddress.ip_network(f"{addr}/64", strict=False)
            return f"{network.network_address}/64"
        return str(addr)

    def _ip_retry_after_seconds(self, ip_key: str, now: float) -> int:
        by_ip = self._data.get("failures_by_ip")
        if not isinstance(by_ip, dict):
            return 0
        entry = by_ip.get(ip_key)
        if not isinstance(entry, dict):
            return 0
        locked_until = self._num_from(entry, "locked_until", 0.0)
        return max(0, math.ceil(locked_until - now))

    def _global_retry_after_seconds(self, now: float) -> int:
        locked_until = self._num("global_locked_until", 0.0)
        remaining = max(0.0, locked_until - now)
        # Defense in depth: the global breaker must never exceed
        # _GLOBAL_SLOWDOWN_SECONDS (invariant §14.4) even if state.json was
        # hand-edited or corrupted to a larger value.
        return int(math.ceil(min(remaining, _GLOBAL_SLOWDOWN_SECONDS)))

    def retry_after_seconds(self, ip: str) -> int:
        """Seconds the caller must wait before the next attempt from `ip`.

        Combines the per-IP lockout (up to 15 minutes) and the global
        circuit breaker (capped at 60s) — whichever is currently longer.
        Reloads from disk first so a lockout recorded by another process
        is visible immediately.

        `ip` must be the address the connection actually came from (e.g.
        ``request.client.host``) — see ``verify()`` for why
        ``X-Forwarded-For``/``X-Real-IP`` must never be used here.
        """
        self._reload()
        now = time.time()
        ip_key = self._normalize_ip(ip)
        return max(
            self._ip_retry_after_seconds(ip_key, now),
            self._global_retry_after_seconds(now),
        )

    def _prune_ip_failures(self, by_ip: dict, now: float) -> dict:
        pruned: dict = {}
        for key, entry in by_ip.items():
            if not isinstance(entry, dict):
                continue
            last_seen = self._num_from(entry, "last_seen", 0.0)
            if now - last_seen > _IP_FAILURE_ENTRY_TTL_SECONDS:
                continue
            pruned[key] = entry
        if len(pruned) > _MAX_IP_FAILURE_ENTRIES:
            ordered = sorted(
                pruned.items(), key=lambda kv: self._num_from(kv[1], "last_seen", 0.0)
            )
            pruned = dict(ordered[-_MAX_IP_FAILURE_ENTRIES:])
        return pruned

    def _record_global_failure(self, now: float) -> None:
        """Update the trailing-hour failure timeline and, only on the
        transition across the threshold, arm the flat slowdown.

        Spec §8.1 requires the global breaker to be "a brake, never a
        gate" — it must never be able to close login entirely. The naive
        version of this (re-set ``global_locked_until = now + 60`` on
        every failure while the trailing window still holds more than
        ``_GLOBAL_FAILURE_THRESHOLD`` entries) does exactly what the spec
        forbids: an attacker who sustains roughly one failure a minute
        from rotating buckets keeps the window permanently over the
        threshold, so *every* failure re-arms the 60s slowdown and a
        clean IP with the correct password is refused for as long as the
        attack continues — there is never a gap where the breaker is not
        freshly re-armed.

        Fix: only arm when the window crosses the threshold on *this*
        call (``was_over_threshold`` was ``False``, the post-append count
        is over). When arming, also clear the timeline. That forces the
        next trip to accumulate a fresh threshold's worth of failures
        from scratch, so the flat 60s slowdown can recur under a sustained
        attack, but a clean IP is never blocked for longer than one
        60-second window at a time.
        """
        raw = self._data.get("global_failures")
        times = (
            [t for t in raw if isinstance(t, (int, float)) and not isinstance(t, bool)]
            if isinstance(raw, list)
            else []
        )
        times = [t for t in times if now - t < _GLOBAL_FAILURE_WINDOW_SECONDS]
        was_over_threshold = len(times) > _GLOBAL_FAILURE_THRESHOLD
        times.append(now)
        if len(times) > _MAX_GLOBAL_FAILURE_SAMPLES:
            times = times[-_MAX_GLOBAL_FAILURE_SAMPLES:]
        if len(times) > _GLOBAL_FAILURE_THRESHOLD and not was_over_threshold:
            self._data["global_locked_until"] = now + _GLOBAL_SLOWDOWN_SECONDS
            self._data["global_failures"] = []
        else:
            self._data["global_failures"] = times

    def _record_failure(self, ip_key: str, now: float) -> None:
        by_ip = self._data.get("failures_by_ip")
        by_ip = dict(by_ip) if isinstance(by_ip, dict) else {}
        entry = by_ip.get(ip_key)
        entry = dict(entry) if isinstance(entry, dict) else {}
        n = int(self._num_from(entry, "n", 0.0)) + 1
        locked_until = self._num_from(entry, "locked_until", 0.0)
        over = n - _MAX_FREE_ATTEMPTS
        if over >= 0:
            delay = min(60 * (2**over), _IP_LOCK_CAP_SECONDS)
            locked_until = now + delay
        by_ip[ip_key] = {"n": n, "locked_until": locked_until, "last_seen": now}
        self._data["failures_by_ip"] = self._prune_ip_failures(by_ip, now)
        self._record_global_failure(now)

    def _clear_ip_failures(self, ip_key: str) -> bool:
        """Drop `ip_key`'s failure-count entry after a successful login,
        but only report ``True`` (a real change) when there was a nonzero
        count to drop.

        Review finding 6 / spec §14.15 refinement (owner ruling
        2026-08-25): the point of "successful verification does not write
        state.json" was never "state must never change on success" — it
        was "a successful request must not cost a write", because HTTP
        Basic (§8.3) resends credentials on every request, including a
        page's status polling. An IP with a clean (zero or absent)
        counter costs nothing either way, so skipping the write there is
        free. An IP that just failed a few times and then authenticated
        correctly is different: leaving its stale count in place means a
        single unrelated typo hours or days later can trip a lockout with
        no real recent attack behind it (`n` never resets on its own —
        see `_record_failure`). The caller (`verify`) writes exactly once,
        right when a run of failures ends in a success, and never again
        afterward — the steady-state "already at zero" path stays free.
        """
        by_ip = self._data.get("failures_by_ip")
        if not isinstance(by_ip, dict):
            return False
        entry = by_ip.get(ip_key)
        if not isinstance(entry, dict) or not self._num_from(entry, "n", 0.0):
            return False
        by_ip = dict(by_ip)
        del by_ip[ip_key]
        self._data["failures_by_ip"] = by_ip
        return True

    def verify(self, login: str, password: str, ip: str) -> bool:
        """Check `login`/`password` against both slots (§4.2) and record
        the outcome for lockout accounting (§8.1).

        `ip` must be the address the TCP connection actually came from
        (e.g. ``request.client.host`` in the ASGI app) — resolving it is
        the caller's job. Never pass through ``X-Forwarded-For``/
        ``X-Real-IP`` here: the wizard has no reverse proxy in front of
        it, so those headers are attacker-controlled and would let a
        client both dodge its own lockout and pollute
        ``failures_by_ip`` with spoofed keys (§8.1).

        A match against `primary` requires both the login and the
        password to match. A match against `temporary` ignores `login`
        entirely (§4.2) and requires the slot to not be TTL-expired.

        On success this writes ``state.json`` **only when there is
        something to reset** — this IP's failure counter is nonzero (see
        ``_clear_ip_failures``). The common case (a clean IP, counter
        already at/near zero) still makes no write at all, which is what
        §8.3.2/§14.15 actually protects: callers that want to avoid
        paying the scrypt cost on every request (e.g. a page that polls
        status) must still keep their own short-TTL, in-memory cache keyed
        off the raw ``Authorization`` header; that cache does not belong
        in this class.
        """
        self._reload()
        if self.is_disabled():
            return False
        now = time.time()
        ip_key = self._normalize_ip(ip)
        if self._ip_retry_after_seconds(ip_key, now) > 0:
            return False
        if self._global_retry_after_seconds(now) > 0:
            return False
        if self._check_primary(login, password) or self._check_temporary(password, now):
            if self._clear_ip_failures(ip_key):
                self._save()
            return True
        self._record_failure(ip_key, now)
        self._save()
        return False

    # ---- gate flags (§4.3) --------------------------------------------------

    def is_disabled(self) -> bool:
        return bool(self._data.get("disabled", False))

    def set_disabled(self, value: bool) -> None:
        self._reload()
        self._data["disabled"] = bool(value)
        self._save()

    def is_open(self) -> bool:
        """Whether the wizard should accept requests at all.

        Unlike the pre-spec-8 behavior, this does NOT consider
        ``completed`` — a fully set-up wizard stays open so the client can
        come back to it (§4.3). The only thing that closes it is the
        ``disabled`` flag (``hermes setup-wizard close``).
        """
        return not self.is_disabled()

    def mark_completed(self) -> None:
        """Record that the first-run form has been submitted once.

        No longer a gate (see ``is_open()``) — only a hint for the caller
        to render the wizard in "return visit" mode (prefilled values,
        masked secrets) instead of the first-run flow.
        """
        self._reload()
        self._data["completed"] = True
        self._save()

    def is_completed(self) -> bool:
        return bool(self._data.get("completed", False))

    # ---- read-only accessors for callers outside this module -------------
    #
    # ``cli.py``'s ``_format_status()`` used to reach into ``self._data``
    # directly (a same-package read the module docstring called out as
    # deliberate). That broke silently when the flat single-slot shape
    # became two slots (§4.2) — the keys it read no longer existed at the
    # top level. These accessors give ``_format_status()`` (and any other
    # external caller) a stable read surface over the slot shape instead of
    # a second place that has to track ``state.json``'s layout by hand.

    def has_primary(self) -> bool:
        """Whether the permanent slot has ever been issued (§4.2)."""
        primary = self._data.get("primary")
        return (
            isinstance(primary, dict)
            and isinstance(primary.get("login"), str)
            and isinstance(primary.get("password_hash"), str)
        )

    def primary_login(self) -> str | None:
        """The permanent slot's login, or ``None`` if never issued."""
        primary = self._data.get("primary")
        if isinstance(primary, dict):
            login = primary.get("login")
            if isinstance(login, str):
                return login
        return None

    def temporary_remaining_seconds(self) -> int | None:
        """Seconds left on the ``temporary`` slot, or ``None`` if there is
        no temporary slot or it has already expired."""
        temp = self._data.get("temporary")
        if not isinstance(temp, dict):
            return None
        exp = self._num_from(temp, "expires_at", -1.0)
        remaining = exp - time.time()
        if remaining <= 0:
            return None
        return int(remaining)
