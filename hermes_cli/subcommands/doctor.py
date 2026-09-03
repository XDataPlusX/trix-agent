"""``hermes doctor`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_doctor_parser(subparsers, *, cmd_doctor: Callable) -> None:
    """Attach the ``doctor`` subcommand to ``subparsers``."""
    # =========================================================================
    # doctor command
    # =========================================================================
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check configuration and dependencies",
        description="Diagnose issues with Hermes Agent setup",
    )
    doctor_parser.add_argument(
        "--fix", action="store_true", help="Attempt to fix issues automatically"
    )
    doctor_parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Opt-in: run one bounded, read-only real-call health probe per "
            "configured tool backend (Firecrawl/FAL/browser/MCP/TTS/STT) "
            "after the static checks. Makes real network calls."
        ),
    )
    doctor_parser.add_argument(
        "--ack",
        metavar="ADVISORY_ID",
        default=None,
        help=(
            "Acknowledge a security advisory by ID and exit. After ack, the "
            "advisory will no longer trigger startup banners. Run `hermes "
            "doctor` first to see active advisories and their IDs."
        ),
    )
    doctor_parser.add_argument(
        "--exit-code",
        action="store_true",
        help=(
            "Exit with a nonzero status if unresolved issues remain after "
            "this run. Without this flag `hermes doctor` always exits 0, "
            "unchanged from prior behavior — pass it to use doctor as a "
            "gate in a script."
        ),
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print a machine-readable verdict (fixed_count, "
            "remaining_issues, verdict) to stdout instead of mixing it "
            "into the human report. The normal human-readable checks "
            "still run and print to stderr, so the terminal stays useful "
            "while stdout stays clean for a script to parse."
        ),
    )
    doctor_parser.set_defaults(func=cmd_doctor)
