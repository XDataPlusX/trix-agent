"""Completes a pending ``hermes business setup`` DM-topic install (spec 21
§5/§6/§8, DM-path review).

**Why this can't happen at ``hermes business setup`` time.** The DM path
(the normal path — see ``hermes_cli/trix_business.py`` module docstring)
creates the administrator's "Администрирование" topic with Telegram's
``createForumTopic`` (Bot API 9.4, works for 1-on-1 chats since that
version), which requires a live, connected bot. The CLI setup script runs
over SSH with no bot connection at all, so it cannot know the resulting
``thread_id`` — it can only record that a topic still needs to be created
(``hermes_cli.trix_business._write_dm_topic_pending_marker``) and do
everything else offline (profile, tool restrictions, multiplexing config).
This module is where that pending work actually finishes, once a gateway
with a live Telegram adapter is running.

**Why here and not ``gateway/hooks.py``'s ``HookRegistry``.** That registry
exists for loosely-coupled, at-most-notify hooks: handlers receive only a
plain, JSON-safe ``context`` dict (platform name, ids, truncated text) —
deliberately NOT the running ``GatewayRunner`` or its live adapters, so a
user-authored hook script can never reach into gateway internals. Completing
a DM topic needs exactly those internals: the connected Telegram adapter
(to call ``createForumTopic``), ``self._session_db`` (to mark topic mode
enabled, mirroring what ``/topic`` does for an ordinary user), and
``self.config`` (to make the freshly-written route live in THIS process
without a second restart). ``gateway/builtin_hooks/`` is the extension
point named for exactly this shape of always-registered, in-process gateway
behavior — see the package docstring and CLAUDE.md's "Adding New Tools" /
project-structure notes. All the logic lives here; ``gateway/run.py`` gets
exactly one call (see ``run_pending_business_dm_topic_completions`` call
site in ``GatewayRunner.start()``), guarded so a failure here can never
block gateway startup.

**Where the pending marker lives, and why every profile is scanned.** The
marker is written under the ``system_admin`` profile's own
``HERMES_HOME`` (``<profile>/business_setup/pending_dm_topic.json`` —
:data:`hermes_cli.trix_business.DM_TOPIC_MARKER_RELATIVE`), not the
default profile's, because the pending work belongs to that admin profile.
But the gateway process serving Telegram (and therefore able to complete
it) normally runs as the DEFAULT profile. There is no cross-profile
"the gateway is running as X" signal to look up, so this scans every
profile directory for the marker — the same best-effort, per-profile
pattern ``hermes_cli.trix_business._apply_shared_skills_wiring`` already
uses for shared-skills wiring.

**One marker, several pending administrators (spec 21 §6 review, task B).**
Several administrators can share one ``system_admin`` profile — each gets
their OWN DM topic in their OWN Telegram chat, but there is only ever one
marker file per admin profile. Its shape is::

    {"admin_profile": "system_admin", "topic_name": "Администрирование",
     "pending": [{"chat_id": "111", "route_name": "system_admin-topic-111",
                  "dm_fallback_notified": false}, ...]}

Each entry in ``pending`` is completed INDEPENDENTLY: one administrator's
Telegram client not rendering DM topics (or any other failure) must not
block, delay, or undo another administrator's completion in the same
marker. ``dm_fallback_notified`` (the "already told this person to use the
group fallback" debounce) is tracked per entry, not once for the whole
marker — see :func:`_complete_one_admin` / :func:`_send_fallback_notice`.
The marker file itself is deleted only once every entry in ``pending`` has
completed; a partially-completed marker is rewritten with just the
still-pending entries.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from utils import atomic_write_text

logger = logging.getLogger(__name__)


def _iter_pending_markers() -> Iterator[Tuple[Any, Path, Dict[str, Any]]]:
    from hermes_cli.profiles import list_profiles
    from hermes_cli.trix_business import DM_TOPIC_MARKER_RELATIVE

    for info in list_profiles():
        marker_path = Path(info.path) / DM_TOPIC_MARKER_RELATIVE
        if not marker_path.is_file():
            continue
        try:
            raw = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning(
                "business setup: unreadable pending DM-topic marker at %s — leaving it, "
                "will retry next startup", marker_path,
            )
            continue
        if not isinstance(raw, dict):
            logger.warning(
                "business setup: pending DM-topic marker at %s is not a JSON object, ignoring",
                marker_path,
            )
            continue
        yield info, marker_path, raw


async def run_pending_business_dm_topic_completions(runner: Any) -> None:
    """Entry point called once from ``GatewayRunner.start()``.

    Best-effort per marker (mirrors ``_apply_shared_skills_wiring``): one
    broken/unreachable profile must never block another's completion, and
    must never block gateway startup itself — every exception is caught and
    logged, nothing propagates. Within a single marker, each pending
    administrator is ALSO completed independently (see :func:`_complete_marker`)
    — a marker-level exception here is a last-resort catch-all for something
    that escaped that inner isolation (e.g. a broken marker file read).
    """
    session_db = getattr(runner, "_session_db", None)
    for info, marker_path, data in list(_iter_pending_markers()):
        try:
            await _complete_marker(runner, session_db, marker_path, data)
        except Exception:
            logger.warning(
                "business setup: DM-topic completion failed for profile %r, marker kept "
                "for retry on next gateway startup", getattr(info, "name", "?"),
                exc_info=True,
            )


async def _complete_marker(
    runner: Any, session_db: Any, marker_path: Path, data: Dict[str, Any],
) -> None:
    """Complete every still-pending administrator in one marker file.

    One administrator's failure (adapter not connected, topics disabled,
    creation failed) must not block or undo another's completion in the
    same marker — each entry is processed independently and the marker is
    rewritten with only the entries that are STILL pending afterward
    (deleted outright once none are left).
    """
    from hermes_cli.trix_business import ADMIN_DM_TOPIC_NAME

    admin_profile = str(data.get("admin_profile") or "").strip()
    topic_name = str(data.get("topic_name") or "").strip() or ADMIN_DM_TOPIC_NAME
    pending = data.get("pending")
    if not admin_profile or not isinstance(pending, list) or not pending:
        logger.warning(
            "business setup: malformed or empty pending DM-topic marker at %s "
            "(admin_profile=%r), ignoring — remove it by hand if this profile was deleted",
            marker_path, admin_profile,
        )
        return

    still_pending: list = []
    for entry in pending:
        if not isinstance(entry, dict) or not str(entry.get("chat_id") or "").strip():
            continue  # malformed entry — nothing meaningful to retry, drop it
        chat_id = str(entry["chat_id"]).strip()
        route_name = (
            str(entry.get("route_name") or "").strip() or f"{admin_profile}-topic-{chat_id}"
        )
        try:
            kept = await _complete_one_admin(
                runner, session_db,
                admin_profile=admin_profile, topic_name=topic_name,
                chat_id=chat_id, route_name=route_name, entry=entry,
            )
        except Exception:
            logger.warning(
                "business setup: DM-topic completion failed for profile %r chat_id=%r, "
                "keeping pending for retry on next gateway startup",
                admin_profile, chat_id, exc_info=True,
            )
            kept = entry
        if kept is not None:
            still_pending.append(kept)

    if not still_pending:
        try:
            marker_path.unlink()
        except OSError:
            logger.warning(
                "business setup: every administrator complete for profile %r, but could "
                "not remove pending marker %s — remove it by hand", admin_profile, marker_path,
            )
        return

    _rewrite_marker(marker_path, admin_profile=admin_profile, topic_name=topic_name, pending=still_pending)


async def _complete_one_admin(
    runner: Any, session_db: Any, *, admin_profile: str, topic_name: str,
    chat_id: str, route_name: str, entry: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Try to complete ONE administrator's DM topic + route.

    Returns the entry to keep pending (possibly updated, e.g.
    ``dm_fallback_notified``), or ``None`` once this administrator is fully
    done (topic created, route written, ready message sent) — the caller
    drops a ``None`` result from the marker's ``pending`` list.
    """
    from gateway.config import Platform
    from gateway.session import SessionSource

    # No thread_id: this source addresses the chat's root (General), which is
    # exactly where an inbound getMe/capability probe and topic creation call
    # need to happen from. profile=None -> resolves through the DEFAULT
    # profile's own adapter (_authorization_adapter fallback), which is
    # always the one actually connected to Telegram for this chat_id.
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        user_id=chat_id,
        chat_type="dm",
    )

    adapter_for_source = getattr(runner, "_adapter_for_source", None)
    adapter = adapter_for_source(source) if callable(adapter_for_source) else None
    if adapter is None:
        logger.info(
            "business setup: Telegram adapter not connected yet — will retry pending DM "
            "topic for profile %r chat_id=%r on next gateway startup", admin_profile, chat_id,
        )
        return entry

    get_caps = getattr(runner, "_get_telegram_topic_capabilities", None)
    caps: Dict[str, Any] = {}
    if callable(get_caps):
        try:
            caps = await get_caps(source) or {}
        except Exception:
            logger.debug("business setup: topic capability probe failed", exc_info=True)
            caps = {}

    if caps.get("checked") and caps.get("has_topics_enabled") is False:
        # Спека 21 review, finding 4: раньше этот выбор (деградация в
        # обычное сообщение вместо темы) не логировался вовсе — на живой
        # машине маркер нёс `dm_fallback_notified: true`, а agent.log,
        # gateway.log и errors.log молчали, так что причину нельзя было
        # узнать без чтения исходников. INFO переживает конфигурацию по
        # умолчанию (agent.log пишет INFO+).
        logger.info(
            "business setup: falling back to group path for profile %r chat_id=%r — "
            "capability probe reports this Telegram chat does not have topics enabled "
            "(admin's client hasn't turned on threaded mode)", admin_profile, chat_id,
        )
        return await _send_fallback_notice(adapter, source, entry, admin_chat_id=chat_id)

    create_topic = getattr(adapter, "_create_dm_topic", None)
    thread_id: Optional[int] = None
    if callable(create_topic):
        try:
            thread_id = await create_topic(int(chat_id), topic_name)
        except Exception:
            logger.warning(
                "business setup: failed to create admin DM topic for profile %r chat_id=%r",
                admin_profile, chat_id, exc_info=True,
            )
    if not thread_id:
        # Covers both "topics disabled" (capability check above didn't catch
        # it — e.g. getMe failed) and any other creation failure. Same rule
        # either way: do not half-configure, leave the marker for retry, and
        # tell the human what to do instead.
        logger.info(
            "business setup: falling back to group path for profile %r chat_id=%r — "
            "createForumTopic did not return a thread_id (capability probe did not catch "
            "it beforehand — see the warning above if topic creation raised)",
            admin_profile, chat_id,
        )
        return await _send_fallback_notice(adapter, source, entry, admin_chat_id=chat_id)

    if session_db is not None:
        try:
            await session_db.enable_telegram_topic_mode(
                chat_id=chat_id,
                user_id=chat_id,
                has_topics_enabled=caps.get("has_topics_enabled"),
                allows_users_to_create_topics=caps.get("allows_users_to_create_topics"),
            )
        except Exception:
            logger.debug(
                "business setup: failed to persist Telegram topic-mode state (non-fatal, "
                "route/topic still gets wired)", exc_info=True,
            )

    try:
        route_report = _write_route(
            admin_profile=admin_profile, chat_id=chat_id, thread_id=str(thread_id),
            route_name=route_name,
        )
    except Exception:
        logger.error(
            "business setup: created DM topic %s for profile %r chat_id=%r but FAILED to "
            "write gateway.profile_routes — kept pending, will retry (topic won't be "
            "recreated: Telegram dedupes by name)", thread_id, admin_profile, chat_id,
            exc_info=True,
        )
        return entry

    if route_report.get("changed"):
        _apply_route_in_memory(
            runner, admin_profile=admin_profile, chat_id=chat_id,
            thread_id=str(thread_id), route_name=route_name,
        )

    try:
        await adapter.send(
            chat_id,
            _ready_message(admin_profile),
            metadata={"thread_id": str(thread_id)},
        )
    except Exception:
        logger.debug("business setup: failed to send admin topic ready message", exc_info=True)

    # Спека 21 review, finding 4: the adapter's own "Created DM topic ... ->
    # thread_id=..." line (plugins/platforms/telegram/adapter.py) doesn't
    # name WHICH administrator it belonged to — with several administrators
    # pending on the same profile, a reader couldn't tell them apart. This
    # line ties the two together and survives the default log config.
    logger.info(
        "business setup: completed DM topic for profile %r chat_id=%r -> thread_id=%s "
        "route_name=%r", admin_profile, chat_id, thread_id, route_name,
    )
    return None


def _write_route(*, admin_profile: str, chat_id: str, thread_id: str, route_name: str) -> dict:
    from hermes_cli.profiles import get_profile_dir
    from hermes_cli.trix_business import apply_business_dm_topic_route

    default_config_path = get_profile_dir("default") / "config.yaml"
    return apply_business_dm_topic_route(
        default_config_path,
        admin_profile=admin_profile,
        chat_id=chat_id,
        thread_id=thread_id,
        route_name=route_name,
    )


def _apply_route_in_memory(
    runner: Any, *, admin_profile: str, chat_id: str, thread_id: str, route_name: str,
) -> None:
    """Append the just-persisted route to ``runner.config.profile_routes`` so
    THIS already-running gateway process serves it immediately — without
    this, the freshly written config.yaml would only take effect after a
    SECOND restart, since ``GatewayConfig`` is parsed once at startup and
    ``_profile_name_for_source`` reads the in-memory list, never the disk."""
    config = getattr(runner, "config", None)
    if config is None:
        return
    from gateway.profile_routing import ProfileRoute

    routes = list(getattr(config, "profile_routes", None) or [])
    for r in routes:
        if r.platform == "telegram" and r.chat_id == chat_id and r.thread_id == thread_id:
            return  # already present (e.g. a concurrent completion)
    routes.append(ProfileRoute(
        name=route_name,
        platform="telegram",
        profile=admin_profile,
        chat_id=chat_id,
        thread_id=thread_id,
    ))
    config.profile_routes = routes


async def _send_fallback_notice(
    adapter: Any, source: Any, entry: Dict[str, Any], *, admin_chat_id: str,
) -> Dict[str, Any]:
    """Ordinary (non-topic) DM message telling THIS administrator their
    Telegram client doesn't show DM topics, and what to run instead — spec
    21 §5 review, task C. Debounced PER ADMINISTRATOR via
    ``dm_fallback_notified`` on their own ``pending`` entry (spec 21 §6
    review, task B) — one administrator being re-notified on every gateway
    restart must not depend on, or affect, another administrator's entry in
    the same marker. Always returns the entry to keep pending (updated in
    place when the notice was actually sent) — the caller persists it back
    into the marker; no half-configured state either way, and a retry is
    free if the human later enables topics."""
    if entry.get("dm_fallback_notified"):
        return entry
    try:
        await adapter.send(source.chat_id, _fallback_message(admin_chat_id))
    except Exception:
        logger.debug("business setup: failed to send DM-topic fallback notice", exc_info=True)
        return entry
    updated = dict(entry)
    updated["dm_fallback_notified"] = True
    return updated


def _rewrite_marker(marker_path: Path, *, admin_profile: str, topic_name: str, pending: list) -> None:
    """Persist the marker with only the still-pending entries — a byte-level
    no-op if nothing actually changed (mirrors every other write in this
    feature, see ``hermes_cli.trix_business._apply_edits``)."""
    ordered = sorted(pending, key=lambda e: str(e.get("chat_id", "")))
    payload = {"admin_profile": admin_profile, "topic_name": topic_name, "pending": ordered}
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        current = marker_path.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current == content:
        return
    try:
        atomic_write_text(marker_path, content)
    except OSError:
        logger.warning(
            "business setup: failed to persist updated pending DM-topic marker at %s",
            marker_path,
        )


def _ready_message(admin_profile: str) -> str:
    return (
        "Канал администрирования готов. Здесь отвечает профиль «" + admin_profile + "» — "
        "пишите сюда всё, что касается общей базы компании (папки, доступы отделов, "
        "навыки). Например, начните с «покажи, что видно в рабочем каталоге компании»."
    )


def _fallback_message(admin_chat_id: str) -> str:
    # Спека 21 review, finding 5: первым средством должно быть "включите
    # темы и перезапустите" — это чинится за десять секунд самим человеком,
    # а не отправка на деградированный групповой путь, который требует
    # заводить группу вручную. Раньше сообщение сразу вело на групповой
    # путь, минуя то, что реально помогло на живом стенде (владелец включил
    # темы у себя в клиенте, и следующий рестарт шлюза завёл тему сам).
    return (
        "Не получилось завести тему «Администрирование» в этом личном чате. Сначала "
        "попробуйте самое простое: откройте настройки этого чата с ботом в Telegram "
        "(значок ⚙️ или три точки в правом верхнем углу), включите там «Темы» "
        "(«Topics») и перезапустите шлюз (`hermes gateway restart`) — тема будет "
        "создана автоматически при следующем запуске, без повторного вызова установки.\n\n"
        "Если это не помогло (клиент всё равно не показывает темы в личных "
        "сообщениях) — заведите группу вручную: добавьте туда этого бота, включите в "
        "ней темы, создайте тему «Администрирование» и перезапустите установку с "
        "обоими id и вашим Telegram id (на случай, если администраторов несколько):\n"
        "hermes business setup --company-root <путь> --group-chat-id <id группы> "
        f"--admin-thread-id <id темы> --admin-telegram-id {admin_chat_id}"
    )
