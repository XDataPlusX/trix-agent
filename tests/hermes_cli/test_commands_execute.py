"""Invariant tests for registry-owned slash execution (CommandDef.execute).

Every ``CommandDef`` with ``execute`` set must:
  * name a key that exists in :data:`hermes_cli.slash_exec.EXECUTORS`, and
  * produce IDENTICAL core text across surfaces for a fixed context — the
    executor may only vary on ``args``/``options``, never on ``surface``.
"""

import pytest

from hermes_cli.commands import COMMAND_REGISTRY, resolve_command
from hermes_cli.slash_exec import (
    EXECUTORS,
    CommandContext,
    CommandReply,
    execute_command,
    resolve_executor,
    run_execute,
)

MIGRATED = [cmd for cmd in COMMAND_REGISTRY if cmd.execute]

SURFACES = ("cli", "gateway", "tui")


def test_some_commands_are_migrated():
    names = {cmd.name for cmd in MIGRATED}
    # The thin-slice set — extend as more commands migrate.
    assert {"version", "egress", "profile", "bundles", "help", "commands"} <= names




def test_unmigrated_commands_have_no_executor():
    for cmd in COMMAND_REGISTRY:
        if not cmd.execute:
            assert resolve_executor(cmd) is None
            assert run_execute(cmd, CommandContext()) is None


class TestExecutorLocalization:
    def test_profile_is_russian_under_ru(self, monkeypatch):
        from agent import i18n
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        try:
            text = execute_command("profile", CommandContext(surface="gateway")).text
            assert "Профиль:" in text
            assert "Profile:" not in text
        finally:
            i18n.reset_language_cache()

    def test_profile_is_english_under_en(self, monkeypatch):
        from agent import i18n
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        try:
            text = execute_command("profile", CommandContext(surface="gateway")).text
            assert text.startswith("Profile:")
        finally:
            i18n.reset_language_cache()

    def test_surface_still_does_not_change_the_text(self, monkeypatch):
        """The module's stated invariant: output depends on args/options,
        never on surface. Language must not smuggle a surface dependency in."""
        from agent import i18n
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        try:
            a = execute_command("profile", CommandContext(surface="cli")).text
            b = execute_command("profile", CommandContext(surface="gateway")).text
            assert a == b
        finally:
            i18n.reset_language_cache()

    @staticmethod
    def _stub_proxy_status(monkeypatch, *, pid=None):
        """Patch hermes_cli.proxy_cli's collaborators for a deterministic,
        minimal /egress reply — no real proxy install/config on the test box."""
        from hermes_cli import proxy_cli
        from agent.proxy_sources.iron_proxy import ProxyStatus

        monkeypatch.setattr(proxy_cli, "load_config", lambda: {})
        monkeypatch.setattr(proxy_cli.ip, "get_status", lambda: ProxyStatus(pid=pid))
        monkeypatch.setattr(proxy_cli.ip, "load_mappings", lambda: [])
        monkeypatch.setattr(proxy_cli.ip, "discover_uncovered_providers", lambda: [])

    def test_egress_is_russian_under_ru(self, monkeypatch):
        from agent import i18n
        self._stub_proxy_status(monkeypatch)
        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        try:
            text = execute_command("egress", CommandContext(surface="gateway")).text
            assert "Статус egress-прокси" in text
            assert "Egress proxy status" not in text
            assert "Enabled:" not in text
        finally:
            i18n.reset_language_cache()

    def test_egress_is_english_under_en(self, monkeypatch):
        from agent import i18n
        self._stub_proxy_status(monkeypatch)
        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        try:
            text = execute_command("egress", CommandContext(surface="gateway")).text
            assert text.startswith("Egress proxy status")
            assert "Статус" not in text
        finally:
            i18n.reset_language_cache()

    def test_version_reply_drops_git_maintenance_tail(self, monkeypatch):
        """format_banner_version_label()'s ``· upstream ... (+N carried
        commits)`` tail names OUR repo's tracked remote — maintenance
        jargon, not something a customer with no clone of this repo can
        act on. The /version reply must drop it; the CLI startup banner
        (format_banner_version_label itself) must keep it."""
        from hermes_cli import banner

        monkeypatch.setattr(
            banner, "get_latest_release_tag", lambda: ("trix-v1.2.3", "https://example.invalid/trix-v1.2.3")
        )
        monkeypatch.setattr(
            banner,
            "get_git_banner_state",
            lambda: {"upstream": "abc123de", "local": "def456ab", "ahead": 2},
        )

        reply_text = execute_command("version", CommandContext(surface="gateway")).text
        banner_text = banner.format_banner_version_label()

        assert reply_text == "Trix Agent v1.2.3"
        assert "upstream" not in reply_text
        assert "carried" not in reply_text
        assert "upstream" in banner_text
        assert "carried" in banner_text

    def test_bundles_default_desc_is_localized(self, monkeypatch):
        from agent import i18n

        monkeypatch.setattr(
            "agent.skill_bundles.list_bundles",
            lambda: [{"slug": "demo", "description": "", "skills": ["a", "b"]}],
        )
        monkeypatch.setattr("agent.skill_bundles._bundles_dir", lambda: "/tmp/bundles")

        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        try:
            text = execute_command("bundles", CommandContext(surface="gateway")).text
            assert "Load 2 skills" not in text
            assert "Загрузить навыков: 2" in text
        finally:
            i18n.reset_language_cache()

    def test_bundles_unavailable_when_subsystem_import_fails(self, monkeypatch):
        """The ``agent.skill_bundles`` import itself failing (missing
        dependency, broken install, ...) must produce the localized
        ``trix.bundles.unavailable`` reply, not an uncaught exception."""
        import sys
        import types

        from agent import i18n

        broken = types.ModuleType("agent.skill_bundles")
        # Deliberately no `_bundles_dir` / `list_bundles` attributes, so the
        # `from agent.skill_bundles import _bundles_dir, list_bundles` inside
        # _exec_bundles raises ImportError.
        monkeypatch.setitem(sys.modules, "agent.skill_bundles", broken)

        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        try:
            reply = execute_command("bundles", CommandContext(surface="gateway"))
            assert reply.text.startswith("Подсистема наборов навыков недоступна:")
            assert reply.data["error"]
        finally:
            i18n.reset_language_cache()

        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        try:
            reply = execute_command("bundles", CommandContext(surface="gateway"))
            assert reply.text.startswith("Bundles subsystem unavailable:")
        finally:
            i18n.reset_language_cache()

    def test_bundles_none_installed(self, monkeypatch):
        """No bundles on disk must produce the localized
        ``trix.bundles.none`` reply, with the real bundles dir spliced in."""
        from agent import i18n

        monkeypatch.setattr("agent.skill_bundles.list_bundles", lambda: [])
        monkeypatch.setattr("agent.skill_bundles._bundles_dir", lambda: "/tmp/bundles")

        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        try:
            reply = execute_command("bundles", CommandContext(surface="gateway"))
            assert "Наборы навыков не установлены." in reply.text
            assert "/tmp/bundles" in reply.text
            assert reply.data == {"bundles": [], "dir": "/tmp/bundles"}
        finally:
            i18n.reset_language_cache()

        monkeypatch.setenv("HERMES_LANGUAGE", "en")
        i18n.reset_language_cache()
        try:
            text = execute_command("bundles", CommandContext(surface="gateway")).text
            assert "No skill bundles installed." in text
        finally:
            i18n.reset_language_cache()

    def test_bundles_header_entry_and_invoke_hint_render(self, monkeypatch):
        """``header``, ``entry`` and ``invoke_hint`` are exercised as a side
        effect of the default_desc test above but nothing asserts their
        content — deleting any of the three would leave the suite green.
        This pins down their real rendered text from locales/ru.yaml."""
        from agent import i18n

        monkeypatch.setattr(
            "agent.skill_bundles.list_bundles",
            lambda: [{"slug": "demo", "description": "Custom desc", "skills": ["a", "b"]}],
        )
        monkeypatch.setattr("agent.skill_bundles._bundles_dir", lambda: "/tmp/bundles")

        monkeypatch.setenv("HERMES_LANGUAGE", "ru")
        i18n.reset_language_cache()
        try:
            text = execute_command("bundles", CommandContext(surface="gateway")).text
            assert "Наборы навыков (установлено: 1):" in text
            assert "/demo — Custom desc (навыков: 2)" in text
            assert "Вызовите /<slug>, чтобы загрузить все навыки набора." in text
        finally:
            i18n.reset_language_cache()










