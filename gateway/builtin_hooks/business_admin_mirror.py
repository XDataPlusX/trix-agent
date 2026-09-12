"""Runs the admin-profile provider/model/proxy mirror on every gateway
startup (owner review on spec 21, "ongoing mirror" requirement).

**Why this exists.** ``hermes business setup`` used to clone-at-creation
only: a one-time copy of the default profile's provider/model/proxy into
the freshly created ``system_admin`` profile. When the client later
changes any of that on ``default`` (normally through the setup wizard),
the admin profile silently drifted — it kept working against a dead key or
a retired proxy, and the failure surfaced days later as "the admin bot
stopped thinking" with no obvious cause at the time of failure. See
``hermes_cli/trix_admin_profile_mirror.py``'s module docstring for the
actual sync logic and its "shipped vs owned_by_client" rule — the same
shape as ``hermes_cli/trix_config_defaults.py``, adapted from
template-vs-client to default-profile-vs-admin-profile.

**Why here and not ``gateway/hooks.py``'s ``HookRegistry``.** Same
reasoning as the sibling ``business_dm_topic.py``'s own docstring: this is
best-effort, in-process, pure file I/O with no dependency on a live
adapter at all, so nothing about it *requires* this location — but it is
co-located with the OTHER spec-21 business-setup startup task
(``business_dm_topic.py``) precisely so a reader sees both "run once at
gateway start, scan every profile for a marker" behaviors next to each
other rather than scattered.

**Why every profile is scanned, not just a hardcoded ``system_admin``.**
``--profile-name`` lets an operator rename the admin profile at deploy
time, and the process serving this hook (normally the ``default``
profile's gateway) has no built-in registry of "which OTHER profile on
this machine is a business-setup admin mirror target". Instead, this scans
every profile directory for the marker file
``hermes_cli.trix_admin_profile_mirror.MIRROR_SOURCE_MARKER_RELATIVE``
that ``hermes business setup`` writes (and re-confirms on every re-run,
including the repair run on an already-broken machine) into the admin
profile — the same best-effort, per-profile scanning pattern
``business_dm_topic.py`` already uses for pending DM topics.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

logger = logging.getLogger(__name__)


def _iter_mirror_targets() -> Iterator[Tuple[Any, Path, Dict[str, Any]]]:
    from hermes_cli.profiles import list_profiles
    from hermes_cli.trix_admin_profile_mirror import MIRROR_SOURCE_MARKER_RELATIVE

    for info in list_profiles():
        marker_path = Path(info.path) / MIRROR_SOURCE_MARKER_RELATIVE
        if not marker_path.is_file():
            continue
        try:
            raw = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning(
                "business admin mirror: unreadable marker at %s — skipping", marker_path,
            )
            continue
        if not isinstance(raw, dict):
            continue
        yield info, marker_path, raw


async def run_pending_business_admin_mirror(runner: Any) -> None:
    """Entry point called once from ``GatewayRunner.start()``.

    Mirrors provider/model/proxy settings from each admin mirror target's
    source profile (normally ``default``) into it. Pure file I/O — the
    ``runner`` argument only mirrors the call convention shared with
    ``run_pending_business_dm_topic_completions`` for readability at the
    call site; nothing here reads from it. Best-effort per profile: one
    broken/unreachable profile must never block another's sync, and must
    never block gateway startup itself.
    """
    from hermes_cli.profiles import get_profile_dir, profile_exists
    from hermes_cli.trix_admin_profile_mirror import (
        format_sync_report,
        sync_admin_profile_from_default,
    )

    for info, _marker_path, data in list(_iter_mirror_targets()):
        source_profile = str(data.get("source_profile") or "default").strip() or "default"
        admin_name = getattr(info, "name", None)
        try:
            if not profile_exists(source_profile):
                continue
            source_home = get_profile_dir(source_profile)
            admin_home = Path(info.path)
            report = sync_admin_profile_from_default(
                default_home=source_home, admin_home=admin_home,
            )
            if report.get("changed") or report.get("diverged_now"):
                logger.info(
                    "business admin mirror: profile %r <- %r: %s",
                    admin_name, source_profile, format_sync_report(report),
                )
        except Exception:
            logger.warning(
                "business admin mirror: sync failed for profile %r, will retry next gateway startup",
                admin_name, exc_info=True,
            )
