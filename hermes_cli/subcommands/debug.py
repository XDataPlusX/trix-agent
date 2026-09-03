"""``hermes debug`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

import argparse
from typing import Callable


def build_debug_parser(subparsers, *, cmd_debug: Callable) -> None:
    """Attach the ``debug`` subcommand to ``subparsers``."""
    # =========================================================================
    # debug command
    # =========================================================================
    debug_parser = subparsers.add_parser(
        "debug",
        help="Debug tools — collect a local report for troubleshooting",
        description="Debug utilities for Trix Agent. 'hermes debug share' collects "
        "a redacted debug report (system info + recent logs) and prints it "
        "to your terminal. It is never uploaded anywhere.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    hermes debug share              Collect debug report and print it
    hermes debug share --lines 500  Include more log lines
    hermes debug share --no-redact  Disable local redaction of secrets
    hermes debug delete <url>       Delete a paste uploaded by an older build
""",
    )
    debug_sub = debug_parser.add_subparsers(dest="debug_command")
    share_parser = debug_sub.add_parser(
        "share",
        help="Collect a debug report and print it to your terminal (never uploaded)",
    )
    share_parser.add_argument(
        "--lines",
        type=int,
        default=200,
        help="Number of log lines to include per log file (default: 200)",
    )
    share_parser.add_argument(
        "--expire",
        type=int,
        default=7,
        help="Accepted for compatibility with older scripts; unused, since nothing is uploaded",
    )
    share_parser.add_argument(
        "--local",
        action="store_true",
        help="Accepted for compatibility with older scripts; this is the only behavior now",
    )
    share_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help=(
            "Accepted for compatibility with older scripts; has no effect. "
            "There is no confirmation prompt to skip since nothing is "
            "uploaded."
        ),
    )
    share_parser.add_argument(
        "--no-redact",
        action="store_true",
        help=(
            "Disable secret redaction in the printed report (default: "
            "redact). Logs are normally run through "
            "agent.redact.redact_sensitive_text with force=True before "
            "being printed, so credentials aren't shown even in local "
            "output."
        ),
    )
    share_parser.add_argument(
        "--nous",
        action="store_true",
        help=(
            "Accepted for compatibility with older scripts; performs no "
            "upload. The report is always collected and printed locally."
        ),
    )
    delete_parser = debug_sub.add_parser(
        "delete",
        help="Delete a paste uploaded by an older build of this tool",
    )
    delete_parser.add_argument(
        "urls",
        nargs="*",
        default=[],
        help="One or more paste URLs to delete (e.g. https://paste.rs/abc123)",
    )
    debug_parser.set_defaults(func=cmd_debug)
