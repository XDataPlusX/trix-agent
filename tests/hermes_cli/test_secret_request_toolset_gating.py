"""Spec 19, Ruling 3 — ``secret_request`` is gated to messaging sessions.

(h) The tool must be absent from CLI/desktop toolsets and present for a real
Telegram session, using the exact resolution chains those surfaces use:
``_get_platform_tools`` (gateway platforms) for CLI/Telegram, and the
``_HERMES_CORE_TOOLS`` / ``desktop_ui`` / ``project`` toolsets (what the
desktop/TUI GUI resolver folds together) for desktop.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hermes_cli.tools_config import _get_platform_tools
from toolsets import _HERMES_CORE_TOOLS, resolve_toolset

REPO_ROOT = Path(__file__).resolve().parents[2]
TRIX_TEMPLATE_PATH = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


def _load_trix_template() -> dict:
    with open(TRIX_TEMPLATE_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_secret_request_absent_from_hermes_core_tools():
    """Never in the shared bundle — CLI and every messaging platform's
    default composite are built from this list."""
    assert "secret_request" not in _HERMES_CORE_TOOLS


def test_secret_request_absent_from_desktop_surface_toolsets():
    """The GUI resolver folds desktop_ui/project onto the core bundle for a
    desktop-sourced session — neither carries this tool."""
    assert "secret_request" not in resolve_toolset("desktop_ui", include_registry=False)
    assert "secret_request" not in resolve_toolset("project", include_registry=False)


def test_secret_request_absent_for_bare_cli_platform():
    """A CLI session (default composite, no explicit config) never sees it."""
    enabled_toolsets = _get_platform_tools({}, "cli", include_default_mcp_servers=False)
    assert "secrets" not in enabled_toolsets
    tool_names = set()
    for ts in enabled_toolsets:
        tool_names.update(resolve_toolset(ts))
    assert "secret_request" not in tool_names


def test_secret_request_present_for_the_real_telegram_client_template():
    """The exact config shipped to Trix clients enables it for Telegram."""
    cfg = _load_trix_template()
    enabled_toolsets = _get_platform_tools(cfg, "telegram", include_default_mcp_servers=False)
    assert "secrets" in enabled_toolsets

    tool_names = set()
    for ts in enabled_toolsets:
        tool_names.update(resolve_toolset(ts))
    assert "secret_request" in tool_names


def test_secret_request_present_for_a_fresh_telegram_install_with_no_config():
    """Even without the Trix template (bare upstream default), a brand new
    Telegram platform composite includes it."""
    enabled_toolsets = _get_platform_tools({}, "telegram", include_default_mcp_servers=False)
    tool_names = set()
    for ts in enabled_toolsets:
        tool_names.update(resolve_toolset(ts))
    assert "secret_request" in tool_names


class TestSecretRequestIsNeverDeferred:
    """Найдено живым прогоном 2026-09-06, тестами не ловилось.

    Инструмент попадал в отбор тулсетов, но `get_tool_definitions`
    отдавал схему БЕЗ него: всё, что не в ядре, подлежит отложению за
    `tool_search`. Для реактивного инструмента это равно отсутствию —
    модель обязана знать о нём в ту секунду, когда клиент упомянул ключ,
    а не догадываться поискать.

    Второй эффект был ещё дороже: на машине клиента откладываемых
    инструментов не было ни одного, механизм не включался, и появление
    одного включило его — минус один инструмент, плюс три служебных.
    """

    def test_not_eligible_for_deferral(self):
        from tools.tool_search import is_deferrable_tool_name

        assert is_deferrable_tool_name("secret_request") is False

    def test_stays_in_the_schema_after_assembly(self):
        """Сквозная проверка: прогоняем сборку и смотрим, что уцелело."""
        from tools.tool_search import assemble_tool_defs, load_config

        defs = [
            {"type": "function", "function": {"name": "secret_request",
                                              "description": "x",
                                              "parameters": {}}},
            {"type": "function", "function": {"name": "terminal",
                                              "description": "y",
                                              "parameters": {}}},
        ]
        assembly = assemble_tool_defs(defs, context_length=8000, config=load_config())
        names = {(d.get("function") or {}).get("name") for d in assembly.tool_defs}
        assert "secret_request" in names

    def test_a_lone_non_core_tool_does_not_activate_tiering(self):
        """Размен «убрать один, добавить три» не должен происходить."""
        from tools.tool_search import assemble_tool_defs, load_config

        defs = [
            {"type": "function", "function": {"name": "secret_request",
                                              "description": "x",
                                              "parameters": {}}},
        ]
        assembly = assemble_tool_defs(defs, context_length=8000, config=load_config())
        assert assembly.deferred_count == 0
        names = {(d.get("function") or {}).get("name") for d in assembly.tool_defs}
        assert "tool_search" not in names
