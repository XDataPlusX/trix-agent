"""Tests for the ddgs search-backend preflight check.

`check_ddgs_backend()` is a thin wrapper around
`tools.web_tools._is_backend_available("ddgs")` -- the exact chokepoint
`_get_capability_backend()` uses to decide whether to honor
`web.search_backend: ddgs` or silently fall through to the `firecrawl`
default. The wrapper is intentionally tested against the real dependency
function (monkeypatched at the call site) so drift between "what the
preflight report says" and "what actually gates the runtime resolver" is
structurally impossible -- there IS no second implementation to drift.

See docs/product/specs/2026-08-17-trix-agent-standard-build-design.md §4.2,
§4.5, §10 and hermes_cli/search_preflight.py's module docstring for why
this check exists: unlike exa/firecrawl/parallel, the DDGS provider cannot
self-heal a missing package at runtime (plugins/web/ddgs/provider.py's
is_available() checks the very import a lazy install would need to
repair), so an install-time failure is only ever caught here.
"""

from __future__ import annotations

from hermes_cli.search_preflight import check_ddgs_backend


class TestCheckDdgsBackend:
    def test_reflects_true_from_the_real_chokepoint(self, monkeypatch):
        import tools.web_tools as wt

        monkeypatch.setattr(wt, "_is_backend_available", lambda backend: True)

        result = check_ddgs_backend()

        assert result.ok is True
        assert "готов" in result.message.lower()

    def test_reflects_false_from_the_real_chokepoint(self, monkeypatch):
        import tools.web_tools as wt

        monkeypatch.setattr(wt, "_is_backend_available", lambda backend: False)

        result = check_ddgs_backend()

        assert result.ok is False
        # Must tell the ADMIN the truth -- an actionable install
        # instruction -- not send them to log into Nous Portal the way a
        # live web_search call in the same state would route the client.
        assert "ddgs" in result.message.lower()
        assert "install" in result.message.lower() or "установите" in result.message.lower()
        assert "nous" not in result.message.lower()
        assert "войдите" not in result.message.lower()
        # Cyrillic content -- proves the message is actually Russian.
        assert any("а" <= ch <= "я" or ch == "ё" for ch in result.message.lower())

    def test_queries_the_ddgs_backend_specifically(self, monkeypatch):
        """A defensive guard against a copy-paste from check_chromium/
        check_docker that forgets to pass "ddgs" through -- would silently
        report on the wrong backend."""
        import tools.web_tools as wt

        captured = []

        def _spy(backend):
            captured.append(backend)
            return True

        monkeypatch.setattr(wt, "_is_backend_available", _spy)

        check_ddgs_backend()

        assert captured == ["ddgs"]


class TestDdgsPackageMissingEndToEnd:
    """Drives the REAL _is_backend_available("ddgs") (no monkeypatching)
    with the ddgs package genuinely not importable, proving the preflight
    reports the truthful gap it exists to surface -- not the Firecrawl
    upsell the client would get from a live web_search call in the same
    state (tools/web_tools.py:287-308)."""

    def test_missing_package_is_reported_not_ok(self, monkeypatch):
        import sys

        # ddgs is not a repo dependency (LAZY_DEPS-only), so in a hermetic
        # test env it is already absent -- but force it in case a sibling
        # test or environment happens to have it importable.
        monkeypatch.setitem(sys.modules, "ddgs", None)

        result = check_ddgs_backend()

        assert result.ok is False
        assert "не установлен" in result.message
