"""Tests for banner toolset name normalization and skin color usage."""

from unittest.mock import patch

from rich.console import Console

import hermes_cli.banner as banner
import model_tools
import tools.mcp_tool


def test_cprint_falls_back_to_plain_print_when_prompt_toolkit_has_no_console(capsys):
    with patch(
        "prompt_toolkit.print_formatted_text",
        side_effect=RuntimeError("no console screen buffer"),
    ):
        banner.cprint("fallback text")

    assert capsys.readouterr().out == "fallback text\n"








def test_build_welcome_banner_title_falls_back_when_no_tag():
    """Without a resolvable tag, the panel title renders as plain text (no hyperlink escape)."""
    import io
    from unittest.mock import patch as _patch
    import hermes_cli.banner as _banner
    import model_tools as _mt
    import tools.mcp_tool as _mcp

    _banner._latest_release_cache = None
    buf = io.StringIO()
    with (
        _patch.object(_mt, "check_tool_availability", return_value=(["web"], [])),
        _patch.object(_banner, "get_available_skills", return_value={}),
        _patch.object(_banner, "get_update_result", return_value=None),
        _patch.object(_mcp, "get_mcp_status", return_value=[]),
        _patch.object(_banner, "get_latest_release_tag", return_value=None),
    ):
        console = Console(file=buf, force_terminal=True, color_system="truecolor", width=160)
        _banner.build_welcome_banner(
            console=console, model="x", cwd="/tmp",
            session_id="abc123",
            tools=[{"function": {"name": "read_file"}}],
            get_toolset_for_tool=lambda n: "file",
        )

    raw = buf.getvalue()
    assert "Trix Agent" in raw, "Version label missing from title"
    assert "Hermes Agent" not in raw, "Must not claim to be the upstream product"
    assert "\x1b]8;" not in raw, "OSC-8 hyperlink should not be emitted without a tag"


def test_format_banner_version_label_uses_product_release_tag(monkeypatch):
    """When a trix-v tag is reachable, the label is built from it, not from
    the upstream Hermes VERSION/RELEASE_DATE constants."""
    monkeypatch.setattr(
        banner, "get_latest_release_tag", lambda: ("trix-v1.2.3", "https://example.invalid/trix-v1.2.3")
    )
    monkeypatch.setattr(banner, "get_git_banner_state", lambda: None)

    label = banner.format_banner_version_label()

    assert label == "Trix Agent v1.2.3"
    assert "Hermes Agent" not in label


def test_format_banner_version_label_falls_back_honestly_without_a_tag(monkeypatch):
    """No resolvable release tag must never fabricate a version or claim to
    be the upstream product."""
    monkeypatch.setattr(banner, "get_latest_release_tag", lambda: None)
    monkeypatch.setattr(banner, "get_git_banner_state", lambda: None)

    label = banner.format_banner_version_label()

    assert "Hermes Agent" not in label
    assert "Trix Agent" in label


def _init_git_repo(path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def test_get_latest_release_tag_ignores_upstream_style_tags(tmp_path):
    """A fork carries the upstream repo's ``v2026.8.3``-style tags in its
    shared history. Those must never be reported as this product's own
    release — only ``trix-v*`` tags count."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    subprocess.run(["git", "tag", "v2026.8.3"], cwd=repo, check=True)

    banner._latest_release_cache = None
    try:
        assert banner.get_latest_release_tag(repo_dir=repo) is None
    finally:
        banner._latest_release_cache = None


def test_get_latest_release_tag_returns_product_tag_when_present(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    subprocess.run(["git", "tag", "v2026.8.3"], cwd=repo, check=True)
    subprocess.run(["git", "tag", "trix-v1.2.3"], cwd=repo, check=True)

    banner._latest_release_cache = None
    try:
        result = banner.get_latest_release_tag(repo_dir=repo)
        assert result is not None
        tag, _url = result
        assert tag == "trix-v1.2.3"
    finally:
        banner._latest_release_cache = None




def test_build_welcome_banner_non_moa_unchanged(tmp_path, monkeypatch):
    """A normal provider still renders the bare model slug, no MoA prefix."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()

    with (
        patch.object(model_tools, "check_tool_availability", return_value=([], [])),
        patch.object(banner, "get_available_skills", return_value={}),
        patch.object(banner, "get_update_result", return_value=None),
        patch.object(tools.mcp_tool, "get_mcp_status", return_value=[]),
    ):
        console = Console(record=True, force_terminal=False, color_system=None, width=160)
        banner.build_welcome_banner(
            console=console,
            model="anthropic/claude-opus-4.8",
            cwd="/tmp/project",
            tools=[],
            enabled_toolsets=[],
            provider="openrouter",
        )

    out = console.export_text()
    assert "claude-opus-4.8" in out
    assert "MoA:" not in out
