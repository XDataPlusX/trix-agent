"""Rendered locale strings must present the product as Trix Agent.

`agent.i18n.t()` returns these strings verbatim to customers on Telegram
and other messaging platforms (/help, /status, /update, ...). A rebrand
that only touches the Python-level identity constants and misses this
catalog still shows "Hermes" on the product's main interface.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCALES_DIR = REPO_ROOT / "locales"
LOCALE_FILES = sorted(LOCALES_DIR.glob("*.yaml"))


def test_locales_directory_is_not_empty():
    assert LOCALE_FILES, "expected locale catalogs under locales/"


def test_no_locale_file_mentions_upstream_product_name():
    _UPSTREAM_TOKENS = ("Hermes", "Nous Research", "Nous Portal", "Nous")
    for path in LOCALE_FILES:
        content = path.read_text(encoding="utf-8")
        for token in _UPSTREAM_TOKENS:
            assert token not in content, f"{path.name} still mentions {token!r}"


def test_orphaned_gateway_debug_keys_are_gone():
    """gateway._handle_debug_command (gateway/slash_commands.py) stopped
    using the gateway.debug.* catalog keys when the no-upload rewrite
    landed -- it builds its reply inline now. A stale gateway.debug block
    left in the catalogs asserted an upload guarantee ("Share these links
    with the Hermes team") that no longer describes what the code does.
    """
    for path in LOCALE_FILES:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        gateway = data.get("gateway", {})
        assert "debug" not in gateway, f"{path.name} still has an orphaned gateway.debug block"


def test_help_and_status_headers_present_trix_agent():
    en = yaml.safe_load((LOCALES_DIR / "en.yaml").read_text(encoding="utf-8"))
    assert "Trix Agent" in en["gateway"]["help"]["header"]
    assert "Trix Agent" in en["gateway"]["status"]["header"]
