"""Tests for the Chromium preflight check (browser_* schema visibility).

`check_chromium_backend()` is a thin wrapper around
`tools.browser_tool.check_browser_requirements()` -- the exact `check_fn`
the tool registry uses to decide whether `browser_navigate` and friends are
advertised to the model at all. The wrapper is intentionally tested against
the real dependency function (monkeypatched at the call site, not the
Chromium-discovery internals it delegates to) so drift between "what the
preflight report says" and "what the check_fn actually returns" is
structurally impossible -- there IS no second implementation to drift.
"""

from __future__ import annotations

from hermes_cli.browser_preflight import check_chromium_backend


class TestCheckChromiumBackend:
    def test_reflects_true_from_the_real_check_fn(self, monkeypatch):
        import tools.browser_tool as bt

        monkeypatch.setattr(bt, "check_browser_requirements", lambda: True)

        result = check_chromium_backend()

        assert result.ok is True
        assert "готов" in result.message.lower()

    def test_reflects_false_from_the_real_check_fn(self, monkeypatch):
        import tools.browser_tool as bt

        monkeypatch.setattr(bt, "check_browser_requirements", lambda: False)

        result = check_chromium_backend()

        assert result.ok is False
        assert "chromium" in result.message.lower()
        assert "не попадают в схему" in result.message
        # Cyrillic content -- proves the message is actually Russian.
        assert any("а" <= ch <= "я" or ch == "ё" for ch in result.message.lower())


class TestNoChromiumAnywhereEndToEnd:
    """Drives the REAL check_browser_requirements() (no monkeypatching) in
    an isolated environment with no Chromium reachable anywhere, proving
    the silent-schema-disappearance defect this preflight exists to
    surface, and that this wrapper correctly reports it."""

    def test_missing_chromium_is_reported_not_ok(self, tmp_path, monkeypatch):
        import tools.browser_tool as bt

        # Isolate: no AGENT_BROWSER_EXECUTABLE_PATH, no system chrome/chromium
        # on PATH, no Playwright cache directory with a chromium-* build.
        monkeypatch.delenv("AGENT_BROWSER_EXECUTABLE_PATH", raising=False)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        # Force a fresh probe -- _chromium_installed() caches its result
        # per-process (see tools/browser_tool.py), which would otherwise
        # leak whatever an earlier test/import already decided.
        monkeypatch.setattr(bt, "_cached_chromium_installed", None)
        monkeypatch.setattr(bt, "_chromium_search_roots", lambda: [])

        result = check_chromium_backend()

        assert result.ok is False
