"""Tests for the gateway /setup command: make sure the setup wizard's
hosting service is up and hand the client its address.

``_handle_setup_wizard_command`` lives in ``gateway/slash_commands.py``
(``GatewaySlashCommandsMixin``, inherited by ``GatewayRunner`` — same home as
``_handle_restart_command``, the sibling handler this one is modeled on). It
does not touch ``self`` or ``event`` at all — it only calls
``hermes_cli.setup_wizard.cli.start_wizard_service()`` and formats the
reply — so, like
``tests/gateway/test_version_command.py::test_gateway_version_command_returns_release_line``,
it can be exercised as an unbound method with ``None`` for both.

Permanent-access design (§6, ``docs/product/specs/2026-08-25-trix-agent-
wizard-permanent-access-design.md``): `/setup` no longer mints or reveals a
password — the wizard's credentials are the ones the client already got by
email when the machine was created. The handler's only remaining job is
starting the hosting service back up (useful if it was turned off or
crashed) and telling the client where to find it. The reply is a plain
string, not an ``EphemeralReply`` — there is no secret in it anymore, and a
message that self-deletes would be actively harmful: a client coming back
to `/setup` a month later would find nothing.

``start_wizard_service()`` used to be reached via a temporary module-level
helper in ``gateway/slash_commands.py`` (``_start_wizard_service_sync``,
now removed) because ``hermes_cli/setup_wizard/cli.py`` hadn't split
``open_wizard()`` into its two halves yet. It has now (spec §13) — the
handler imports the public ``start_wizard_service()`` directly.
"""

import asyncio

from agent.i18n import t
from gateway.platforms.base import EphemeralReply
from hermes_cli.commands import COMMAND_REGISTRY, resolve_command


def test_setup_command_registered_gateway_only():
    cmd = next(c for c in COMMAND_REGISTRY if c.name == "setup")
    assert cmd.gateway_only is True
    assert cmd.cli_only is False
    resolved = resolve_command("setup")
    assert resolved is cmd


def test_setup_in_client_menu():
    from hermes_cli.trix_menu import CLIENT_MENU_COMMANDS

    assert "setup" in CLIENT_MENU_COMMANDS


def test_setup_in_client_menu_and_cap_matches(tmp_path, monkeypatch):
    """Инвариант спеки: кап меню равен числу отобранных команд.

    Второй экземпляр того же утверждения, что и
    tests/hermes_cli/test_trix_disk_command.py::
    test_menu_cap_equals_the_curated_command_count. С Task 1b
    ``max_commands`` в шаблоне не прописан вовсе -- кап выводится из
    ``len(CLIENT_MENU_COMMANDS)`` в
    ``hermes_cli.commands._telegram_command_menu_config``, поэтому здесь
    ставится реальный шаблон в отдельный ``HERMES_HOME`` и проверяется
    выведенное значение, а не буквальный ключ YAML (которого больше нет).
    """
    import pathlib

    from hermes_cli.commands import telegram_menu_max_commands
    from hermes_cli.trix_menu import CLIENT_MENU_COMMANDS

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    template_text = (repo_root / "assets" / "config" / "trix-config.yaml").read_text()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(template_text, encoding="utf-8")

    cap = telegram_menu_max_commands()
    assert cap == len(CLIENT_MENU_COMMANDS)


def test_setup_after_restart_in_menu_order():
    from hermes_cli.trix_menu import CLIENT_MENU_COMMANDS

    assert CLIENT_MENU_COMMANDS.index("setup") == CLIENT_MENU_COMMANDS.index("restart") + 1


def test_handler_replies_with_address_and_no_password(monkeypatch):
    """The success reply carries the wizard's address and is a plain,
    non-ephemeral string that never contains a password."""
    import hermes_cli.setup_wizard.cli as wizard_cli
    from gateway.run import GatewayRunner

    monkeypatch.setattr(
        wizard_cli, "start_wizard_service", lambda: ("https://203.0.113.7:8443", True)
    )

    result = asyncio.run(GatewayRunner._handle_setup_wizard_command(None, None))  # type: ignore[arg-type]

    assert "https://203.0.113.7:8443" in result
    assert not isinstance(result, EphemeralReply)
    assert isinstance(result, str)
    # The reply text is built from `url` alone -- the reply-formatting call
    # in the handler passes no `password` kwarg at all (would raise a
    # KeyError from `t()` if the catalog string still expected one), so
    # there is no secret value for any word "password" in the copy to be
    # standing in for. The real "no secret handling" invariant lives in
    # test_handler_does_not_touch_wizard_credentials below.
    assert t("trix.setup_wizard.port_unreachable") not in result


def test_handler_does_not_touch_wizard_credentials(monkeypatch):
    """The handler must not mint or rotate any password — a status-check
    command like `/setup` must be a no-op on credential state. Only the
    service-starting seam (``start_wizard_service``) may run; neither
    ``issue_temporary_password`` nor ``WizardState`` may be touched."""
    import hermes_cli.setup_wizard.cli as wizard_cli
    from gateway.run import GatewayRunner

    def _boom_if_called(*_a, **_k):
        raise AssertionError("/setup must not issue any credential anymore")

    monkeypatch.setattr(wizard_cli, "issue_temporary_password", _boom_if_called)
    monkeypatch.setattr(wizard_cli.WizardState, "load", staticmethod(_boom_if_called))
    monkeypatch.setattr(
        wizard_cli, "start_wizard_service", lambda: ("https://203.0.113.7:8443", True)
    )

    result = asyncio.run(GatewayRunner._handle_setup_wizard_command(None, None))  # type: ignore[arg-type]

    assert "https://203.0.113.7:8443" in result


def test_handler_warns_when_port_unreachable(monkeypatch):
    """``start_wizard_service()``'s ``reachable`` flag is a best-effort
    local TCP probe; over the gateway the client is reading a chat reply,
    not a terminal, so the same honest disclosure must ride along in the
    reply text.

    This test file's environment is pinned to English by the gateway
    conftest's ``_pin_english_ui_language`` autouse fixture (Trix's
    default ``display.language`` is ``"ru"`` in production, but these
    unit tests assert against ``agent.i18n.t()``'s English catalog) —
    so it renders the ``trix.setup_wizard.port_unreachable`` key through
    ``t()`` itself rather than hardcoding either language's literal text.
    """
    import hermes_cli.setup_wizard.cli as wizard_cli
    from gateway.run import GatewayRunner

    monkeypatch.setattr(
        wizard_cli, "start_wizard_service", lambda: ("https://203.0.113.7:8443", False)
    )

    result = asyncio.run(GatewayRunner._handle_setup_wizard_command(None, None))  # type: ignore[arg-type]

    assert isinstance(result, str) and not isinstance(result, EphemeralReply)
    assert t("trix.setup_wizard.port_unreachable") in result


def test_handler_reports_failure_without_raising(monkeypatch):
    """A failure inside ``start_wizard_service()`` (e.g. systemd
    unavailable) must not propagate as an unhandled exception -- it should
    degrade to a plain error reply, same shape as the success reply.

    client-command-surface Task 8: the raw exception text (Python/English
    internals -- unit names, subprocess/systemctl blurbs) must NOT reach
    the chat. The client sees a short, localized, actionable reply; the
    untruncated exception still lands in the gateway log via
    ``logger.warning()`` (support's view), which is what
    ``test_handler_logs_the_full_exception_text`` below pins."""
    import hermes_cli.setup_wizard.cli as wizard_cli
    from gateway.run import GatewayRunner

    def _boom():
        raise RuntimeError("systemctl not found")

    monkeypatch.setattr(wizard_cli, "start_wizard_service", _boom)

    result = asyncio.run(GatewayRunner._handle_setup_wizard_command(None, None))  # type: ignore[arg-type]

    assert isinstance(result, str) and result.strip()
    assert result == t("trix.setup_wizard.failed")
    assert "systemctl not found" not in result


def test_handler_never_leaks_any_part_of_a_long_error_into_the_reply(monkeypatch, caplog):
    """A verbose subprocess/systemctl exception must not dump any of its
    text (possibly a multi-hundred-char traceback-ish blurb) into the
    client-facing reply -- not even a truncated prefix. The client only
    gets the generic localized reply; the untruncated text still reaches
    the gateway log via logger.warning()."""
    import logging

    import hermes_cli.setup_wizard.cli as wizard_cli
    from gateway.run import GatewayRunner

    long_message = "systemctl failed: " + ("x" * 500)

    def _boom():
        raise RuntimeError(long_message)

    monkeypatch.setattr(wizard_cli, "start_wizard_service", _boom)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        result = asyncio.run(GatewayRunner._handle_setup_wizard_command(None, None))  # type: ignore[arg-type]

    assert result == t("trix.setup_wizard.failed")
    assert result.startswith("⚠️")
    assert "systemctl failed:" not in result
    assert "x" * 20 not in result
    # The full exception text is not lost -- it went to the log instead.
    assert any(long_message in record.getMessage() for record in caplog.records)


def test_handler_uses_public_start_wizard_service_not_a_private_shim():
    """The old private ``_start_wizard_service_sync`` shim in
    ``gateway/slash_commands.py`` is gone (spec §13) — the handler now
    imports and calls ``hermes_cli.setup_wizard.cli.start_wizard_service``
    directly."""
    import gateway.slash_commands as slash_commands
    import hermes_cli.setup_wizard.cli as wizard_cli

    assert not hasattr(slash_commands, "_start_wizard_service_sync")
    assert callable(wizard_cli.start_wizard_service)
