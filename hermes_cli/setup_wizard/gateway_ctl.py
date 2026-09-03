"""Gateway lifecycle control for the setup wizard (spec §10.3-10.5, §18.1).

Two independent moves the last wizard step needs, plus a best-effort
teardown of the wizard's own scaffolding service once the real gateway is
confirmed alive:

- ``restart_gateway`` — apply the freshly-written ``.env``/``config.yaml``
  by asking the CLI to restart the gateway service the same way a human
  operator would (``hermes gateway restart``). Falls back to
  ``hermes gateway start`` on a nonzero exit code.

  What ``start`` actually covers: NOT "service not installed" (``start``
  also needs an installed unit — an uninstalled VM fails both ranks alike,
  and that gap is provisioning's job, documented by Task 10/14, not this
  module's). What it covers is the much more common case: the unit is
  installed but not currently *active* (``systemctl start`` on an inactive
  unit succeeds; ``restart`` on one — depending on platform/systemd
  version — can return nonzero instead of quietly starting it). Trying
  ``restart`` first still makes sense as the common-path move (the wizard
  usually reapplies settings to an already-running gateway), with
  ``start`` as the fallback for "installed but stopped".

  Returns two snapshots of ``gateway_state.json``, taken *before* either
  attempt runs — ``pre_pid`` (top-level PID) and ``pre_platform_stamp``
  (the telegram sub-record's own ``updated_at``). Callers pass both to
  :func:`wait_bot_alive`. Both exist because they guard against two
  different false positives (see ``_telegram_adapter_verdict``'s
  docstring for the race the second one closes):

  1. A restart that reports ``ok`` without the process actually turning
     over (``pre_pid`` unchanged).
  2. A restart whose PID *did* turn over, but whose freshly-started
     process hasn't rewritten ``platforms.telegram`` yet — the read-
     merge-write in ``write_runtime_status`` refreshes the top-level
     pid/start_time/updated_at the moment the new process calls it with
     ``gateway_state="starting"`` (``gateway/run.py``, well before the
     Telegram adapter itself reaches ``connecting``), while the
     ``platforms`` dict is left untouched from the OLD process until the
     new adapter writes its own state. A stale FATAL sub-record from the
     old process would otherwise look like a fresh terminal error; a
     stale CONNECTED one would look like proof the new process is
     already up (``pre_platform_stamp`` unchanged).

- ``wait_bot_alive`` — the liveness proof: poll Telegram's own ``getMe``
  (via :func:`hermes_cli.setup_wizard.validate.check_telegram_token`,
  Task 6) AND the gateway's own structured runtime status
  (``gateway/status.py`` — ``read_runtime_status``,
  ``runtime_status_is_stale``, ``runtime_status_pid_is_live``) until both
  agree the Telegram adapter is genuinely connected, one of them reports a
  terminal ``fatal`` error, or the timeout elapses. A ``getMe`` success
  alone proves the token is valid and Telegram is reachable, but NOT that
  *our* gateway process is the one holding the connection — a stale/dead
  gateway with a valid token would look identical from Telegram's side.
  The runtime-status check closes that gap without parsing any
  human-readable ``hermes gateway status`` text (still explicitly
  disallowed) — it reads the same structured ``gateway_state.json`` the
  dashboard/CLI status surfaces already read.

- ``stop_and_disable_wizard_service`` — best-effort teardown of the
  temporary ``trix-setup.service`` unit that hosts the wizard web server
  itself, once the wizard is done and the real gateway is confirmed up.
  No systemd (e.g. macOS, containers without a user session) means this
  is a silent no-op, not an error — the caller (Task 9c) still needs to
  set its own completion flag regardless of whether a systemd unit
  existed to tear down.

Why this module calls the CLI as a subprocess instead of importing
``hermes_cli.gateway`` functions directly: ``hermes gateway restart`` is
the exact command a human operator runs, and it already contains the
graceful-drain / SIGUSR1 / systemctl-fallback logic in
``hermes_cli/gateway.py`` (see ``_graceful_restart_via_sigusr1``). Calling
that command out-of-process, the same way a person would from a shell,
means the wizard never diverges from that logic or has to duplicate it —
whatever restart strategy the CLI decides on (in-process signal vs.
``systemctl restart`` vs. ``launchctl kickstart``) is exactly what the
wizard triggers too.
"""
from __future__ import annotations

import subprocess
import sys
import time

from gateway.status import (
    read_runtime_status,
    runtime_status_is_stale,
    runtime_status_pid_is_live,
)
from hermes_cli.setup_wizard.validate import check_telegram_token

_RESTART_TIMEOUT = 120.0
_SERVICE_STOP_TIMEOUT = 15.0
_WIZARD_SERVICE_NAME = "trix-setup.service"

_MSG_RESTARTED = "Шлюз перезапущен"
_MSG_STARTED = "Шлюз запущен"
_MSG_FAILED = "Не удалось перезапустить или запустить шлюз"
_MSG_FATAL_PREFIX = "Шлюз сообщил о неустранимой ошибке подключения к Telegram"
_MSG_NOT_CONFIRMED = (
    "Шлюз не подтвердил подключение к Telegram — проверьте логи и попробуйте перезапустить ещё раз"
)


def _run_gateway_cli(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "hermes_cli.main", "gateway", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_RESTART_TIMEOUT,
        check=False,
    )


def _telegram_platform_record(rec: dict) -> dict | None:
    """Return ``rec["platforms"]["telegram"]`` if both levels are dicts.

    A hand-edited or partially-written ``gateway_state.json`` can have
    ``platforms`` (or ``platforms.telegram``) as any JSON type — a list, a
    string, ``null``. Every caller must degrade gracefully instead of
    raising ``AttributeError`` on ``.get()``.
    """
    platforms = rec.get("platforms")
    if not isinstance(platforms, dict):
        return None
    platform_rec = platforms.get("telegram")
    if not isinstance(platform_rec, dict):
        return None
    return platform_rec


def _snapshot_restart_state() -> tuple[int | None, str | None]:
    """Read ``gateway_state.json`` once and extract the pre-restart baseline.

    Returns ``(pre_pid, pre_platform_stamp)`` — the top-level PID and the
    telegram sub-record's own ``updated_at``, both ``None`` when no usable
    record exists yet (e.g. first-ever start). See the module docstring
    for why both are needed.
    """
    rec = read_runtime_status()
    if not isinstance(rec, dict):
        return None, None
    try:
        pid = int(rec["pid"])
    except (KeyError, TypeError, ValueError):
        pid = None
    platform_rec = _telegram_platform_record(rec)
    stamp = platform_rec.get("updated_at") if platform_rec else None
    return pid, stamp


def restart_gateway() -> dict:
    """Restart the gateway service, falling back to ``start`` if needed.

    Try ``restart`` first (the common case: the wizard is applying new
    settings to an already-running gateway), then fall back to ``start`` on
    a nonzero exit code or a subprocess failure/timeout. See the module
    docstring for what each rank actually covers.

    Returns ``{"ok": bool, "message": str, "pre_pid": int | None,
    "pre_platform_stamp": str | None}``. The two snapshots are taken
    *before* either attempt runs — pass both to :func:`wait_bot_alive` so
    it can tell a genuine restart apart from a same-process no-op (see the
    module docstring for the two distinct false-positive windows this
    closes).
    """
    pre_pid, pre_platform_stamp = _snapshot_restart_state()

    try:
        restart_result = _run_gateway_cli("restart")
    except (OSError, subprocess.SubprocessError):
        restart_result = None

    if restart_result is not None and restart_result.returncode == 0:
        return {
            "ok": True,
            "message": _MSG_RESTARTED,
            "pre_pid": pre_pid,
            "pre_platform_stamp": pre_platform_stamp,
        }

    try:
        start_result = _run_gateway_cli("start")
    except (OSError, subprocess.SubprocessError):
        return {
            "ok": False,
            "message": _MSG_FAILED,
            "pre_pid": pre_pid,
            "pre_platform_stamp": pre_platform_stamp,
        }

    if start_result.returncode == 0:
        return {
            "ok": True,
            "message": _MSG_STARTED,
            "pre_pid": pre_pid,
            "pre_platform_stamp": pre_platform_stamp,
        }

    return {
        "ok": False,
        "message": _MSG_FAILED,
        "pre_pid": pre_pid,
        "pre_platform_stamp": pre_platform_stamp,
    }


def _telegram_adapter_verdict(
    pre_pid: int | None,
    pre_platform_stamp: str | None,
) -> tuple[str, str | None]:
    """Classify the current runtime-status record for the telegram adapter.

    Returns ``(verdict, error_message)`` where ``verdict`` is one of:

    - ``"connected"`` — record is fresh, PID is alive, and
      ``platforms.telegram.state == "connected"``, AND (when the
      corresponding snapshot is given) both freshness checks pass:

      * ``pre_pid`` — the *live* PID must differ from it. A "connected"
        record still carrying the pre-restart PID means the old process
        never actually turned over (the ``start``-on-active-unit no-op
        case).
      * ``pre_platform_stamp`` — the telegram sub-record's own
        ``updated_at`` must differ from it. A "connected" record whose
        per-platform stamp hasn't moved is the OLD process's leftover
        state surviving into the new process's fresh top-level record
        (see the module docstring's read-merge-write race) — the PID
        alone can't catch this, because the top-level pid/updated_at
        refresh on process start happens well before the adapter
        rewrites its own ``platforms.telegram`` entry.

      Either snapshot defaults to ``None`` (not supplied) and is then
      not gated on — same behavior as before these parameters existed.

    - ``"fatal"`` — ``platforms.telegram.state == "fatal"``: a terminal,
      unrecoverable adapter error. Gated ONLY on ``pre_platform_stamp``
      (not ``pre_pid`` — the top-level PID is typically already fresh by
      the time this is read, per the race above, so it can't tell a
      leftover fatal record apart from a new one on its own). When
      ``pre_platform_stamp`` is unchanged, the record is treated as
      "unconfirmed" (keep polling) rather than a terminal failure — a
      stale fatal record must not cut the wait short.
    - ``"unconfirmed"`` — no record, a stale record, a dead/reused PID,
      any other adapter state (``connecting``, ``disconnected``,
      ``retrying``, ...), or a "connected"/"fatal" record that failed its
      freshness gate above. Caller should keep polling.
    """
    rec = read_runtime_status()
    if not isinstance(rec, dict):
        return "unconfirmed", None
    if runtime_status_is_stale(rec):
        return "unconfirmed", None
    if not runtime_status_pid_is_live(rec):
        return "unconfirmed", None

    platform_rec = _telegram_platform_record(rec)
    if platform_rec is None:
        return "unconfirmed", None

    state = platform_rec.get("state")
    stamp = platform_rec.get("updated_at")
    stamp_is_fresh = pre_platform_stamp is None or stamp != pre_platform_stamp

    if state == "fatal":
        if not stamp_is_fresh:
            return "unconfirmed", None
        return "fatal", platform_rec.get("error_message")

    if state == "connected":
        pid_is_fresh = pre_pid is None or rec.get("pid") != pre_pid
        if not (pid_is_fresh and stamp_is_fresh):
            return "unconfirmed", None
        return "connected", None

    return "unconfirmed", None


def _fatal_message(error_message: str | None) -> str:
    if error_message:
        try:
            from agent.redact import redact_sensitive_text

            safe = redact_sensitive_text(str(error_message), force=True)
        except Exception:
            safe = None
        if safe:
            return f"{_MSG_FATAL_PREFIX}: {safe}"
    return _MSG_FATAL_PREFIX


def wait_bot_alive(
    token: str,
    proxy: str | None,
    timeout: float = 90.0,
    poll: float = 3.0,
    pre_pid: int | None = None,
    pre_platform_stamp: str | None = None,
) -> dict:
    """Poll until both Telegram and the gateway's own status agree it's alive.

    Success requires: ``check_telegram_token`` has succeeded at least once
    (the token is valid, Telegram is reachable — checked once and cached,
    NOT re-checked every iteration once confirmed, to avoid hammering the
    Bot API for the rest of the wait) AND the gateway's own status shows
    the telegram adapter genuinely ``connected`` (see
    :func:`_telegram_adapter_verdict` for the freshness gating). A
    ``platforms.telegram.state == "fatal"`` record — once it passes its own
    freshness gate — ends the wait immediately as a terminal failure,
    without waiting out the rest of ``timeout``, and independent of
    whether the token has been confirmed yet. Any other combination keeps
    polling. Each iteration checks the local runtime-status verdict FIRST
    (a local file read) before touching the network with ``getMe``.

    ``pre_pid``/``pre_platform_stamp`` are optional and default to
    ``None`` (no freshness gating — same behavior as before these
    parameters existed); pass both from :func:`restart_gateway`'s return
    value to catch a restart that reported success but never actually
    replaced the process, or whose new process hasn't overwritten a
    leftover ``platforms.telegram`` record yet.

    Returns ``{"ok": True, "username": ...}`` on success or ``{"ok":
    False, "error": ...}`` on a fatal adapter error or timeout. The
    timeout message includes the last ``check_telegram_token`` error text
    when the token itself never checked out (nothing appended when the
    token was fine and only the adapter never confirmed).
    """
    deadline = time.monotonic() + timeout
    token_result: dict | None = None
    token_confirmed = False

    while True:
        verdict, error_message = _telegram_adapter_verdict(pre_pid, pre_platform_stamp)
        if verdict == "fatal":
            return {"ok": False, "error": _fatal_message(error_message)}

        if not token_confirmed:
            token_result = check_telegram_token(token, proxy)
            if token_result.get("ok"):
                token_confirmed = True

        if token_confirmed and verdict == "connected":
            return token_result

        if time.monotonic() >= deadline:
            break
        time.sleep(poll)

    if token_result is not None and not token_result.get("ok") and token_result.get("error"):
        return {"ok": False, "error": f"{_MSG_NOT_CONFIRMED} ({token_result['error']})"}
    return {"ok": False, "error": _MSG_NOT_CONFIRMED}


def stop_and_disable_wizard_service() -> None:
    """Best-effort ``systemctl --user stop/disable`` of the wizard's own service.

    Runs after the real gateway is confirmed alive, to shut down the
    temporary web server that hosted the setup wizard itself. Systemd may
    not be present at all (macOS dev boxes, minimal containers) — that's a
    normal outcome here, not an error. ``stop`` and ``disable`` are tried
    independently (each in its own ``try``): a failure on ``stop`` (e.g.
    the unit was already stopped, or timed out) must not skip ``disable``
    — otherwise the wizard service would still be enabled and could come
    back on the next boot. Every failure mode (missing ``systemctl``
    binary, no user session, unit not found, one action timing out) is
    swallowed independently per action. The caller (Task 9c) is
    responsible for marking the wizard's own completion state; this
    function only tears down the service process.
    """
    for action in ("stop", "disable"):
        try:
            subprocess.run(
                ["systemctl", "--user", action, _WIZARD_SERVICE_NAME],
                capture_output=True,
                timeout=_SERVICE_STOP_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
