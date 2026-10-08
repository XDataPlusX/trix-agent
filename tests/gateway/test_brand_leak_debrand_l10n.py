"""Six client-reachable brand leaks found beyond the original de-brand spec
(Task 9d found 12 candidates beyond the spec's command-registry search; this
covers the five assigned plus one more found the same way while sweeping
for a sixth, each verified reachable):

1. ``agent.estop.paused_reply`` -- reached on every gateway turn while a
   global pause is engaged (``gateway/run.py:_handle_message``).
2. ``hermes_cli.active_sessions.active_session_limit_message`` -- reached
   when the gateway is at ``max_concurrent_sessions``.
3. ``hermes_cli.model_cost_guard.expensive_model_warning`` -- embedded in
   the ``/model`` expensive-model confirmation dialog.
4. ``hermes_cli.config.format_managed_message`` -- reached through
   ``/update`` on a managed install (``gateway/slash_commands.py``).
5. The Telegram handoff thread name built in
   ``gateway.run.GatewayRunner._process_handoff``.
6. The Discord auto-thread fallback title in
   ``gateway.run.GatewayRunner._sanitize_discord_thread_title`` -- used
   whenever a session has no title yet, found via a plain-text sweep for
   "Hermes"/"Nous" literals reachable by the client.

The original spec's grep only covered command *descriptions* in the
registry; these six leak through reply *bodies*, which is why they
survived that pass. Each assertion below is a behavior contract (render the
real code path, inspect the real string), not a source-text scan -- except
where noted, these exercise the function that produces the customer-visible
string, not a copy of it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent import i18n


def _has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


def _assert_debranded_and_russian(text: str, *, label: str) -> None:
    assert "Hermes" not in text, f"{label}: still mentions Hermes: {text!r}"
    assert "Nous" not in text, f"{label}: still mentions Nous: {text!r}"
    assert _has_cyrillic(text), f"{label}: no Cyrillic in the Russian render: {text!r}"


@pytest.fixture
def russian(monkeypatch):
    """Pin the resolved language to Russian for the duration of the test.

    ``tests/conftest.py`` pins ``HERMES_LANGUAGE=en`` for the whole suite
    (upstream tests assert English copy), so every test here that wants the
    customer-facing render has to opt back into Russian explicitly, per the
    pattern in ``tests/hermes_cli/test_trix_menu.py``.
    """
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        yield
    finally:
        i18n.reset_language_cache()


# ---------------------------------------------------------------------------
# 1. agent.estop.paused_reply
# ---------------------------------------------------------------------------

class TestEstopPausedReply:
    def test_with_reason_is_debranded_and_russian(self, russian, tmp_path, monkeypatch):
        from agent import estop

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        estop._reset_log_state_for_tests()
        estop.engage(reason="deploy window")
        try:
            notice = estop.paused_reply()
            assert notice is not None
            _assert_debranded_and_russian(notice, label="estop.paused_reply(reason)")
            # The client cannot run a CLI command -- the old copy told them to
            # run `hermes resume`.
            assert "hermes resume" not in notice.lower()
        finally:
            estop.disengage()

    def test_without_reason_is_debranded_and_russian(self, russian, tmp_path, monkeypatch):
        from agent import estop

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        estop._reset_log_state_for_tests()
        estop.engage()
        try:
            notice = estop.paused_reply()
            assert notice is not None
            _assert_debranded_and_russian(notice, label="estop.paused_reply()")
            assert "hermes resume" not in notice.lower()
        finally:
            estop.disengage()


# ---------------------------------------------------------------------------
# 2. hermes_cli.active_sessions.active_session_limit_message
# ---------------------------------------------------------------------------

class TestActiveSessionLimitMessage:
    def test_is_debranded_and_russian(self, russian):
        from hermes_cli.active_sessions import active_session_limit_message

        text = active_session_limit_message(3, 3, entries=[{"surface": "telegram"}])
        _assert_debranded_and_russian(text, label="active_session_limit_message")

    def test_without_holders_still_debranded(self, russian):
        from hermes_cli.active_sessions import active_session_limit_message

        text = active_session_limit_message(1, 1, entries=None)
        _assert_debranded_and_russian(text, label="active_session_limit_message (no holders)")


# ---------------------------------------------------------------------------
# 3. hermes_cli.model_cost_guard.expensive_model_warning
# ---------------------------------------------------------------------------

class TestExpensiveModelWarning:
    def test_above_threshold_line_is_debranded_and_russian(self, russian):
        from hermes_cli.model_cost_guard import expensive_model_warning
        from agent.models_dev import ModelInfo

        info = ModelInfo(
            id="pricey/model",
            name="Pricey Model",
            family="pricey",
            provider_id="pricey",
            cost_input=50.0,
            cost_output=200.0,
        )
        warning = expensive_model_warning("pricey/model", provider="pricey", model_info=info)
        assert warning is not None
        _assert_debranded_and_russian(warning.message, label="expensive_model_warning.message")


# ---------------------------------------------------------------------------
# 4. hermes_cli.config.format_managed_message
# ---------------------------------------------------------------------------

class TestFormatManagedMessage:
    def test_generic_managed_system_is_debranded_and_russian(self, russian):
        from hermes_cli.config import format_managed_message

        with patch("hermes_cli.config.get_managed_system", return_value="Homebrew"):
            text = format_managed_message("update Trix Agent")
        _assert_debranded_and_russian(text, label="format_managed_message (generic)")

    def test_nixos_managed_system_is_debranded_and_russian(self, russian):
        from hermes_cli.config import format_managed_message

        with patch("hermes_cli.config.get_managed_system", return_value="NixOS"):
            text = format_managed_message("update Trix Agent")
        _assert_debranded_and_russian(text, label="format_managed_message (NixOS)")
        # The client reading this via /update cannot edit a Nix module or run
        # a shell command -- the old copy told them to do both.
        assert "nixos-rebuild" not in text
        assert "configuration.nix" not in text


# ---------------------------------------------------------------------------
# 5. Telegram handoff thread name (gateway.run.GatewayRunner._process_handoff)
# ---------------------------------------------------------------------------

class _StopAfterThreadName(Exception):
    """Raised by the ``t()`` spy once it captures the thread name, so the
    test doesn't have to stand up the rest of the handoff pipeline (session
    store, synthetic message delivery, ...) to reach the one line under
    test."""


class TestHandoffThreadName:
    def test_thread_name_is_debranded_and_russian(self, russian, monkeypatch):
        from gateway.run import GatewayRunner

        real_t = i18n.t
        captured: dict[str, str] = {}

        def spy_t(key, *args, **kwargs):
            if key == "trix.handoff.thread_name":
                captured["value"] = real_t(key, *args, **kwargs)
                raise _StopAfterThreadName()
            return real_t(key, *args, **kwargs)

        monkeypatch.setattr("gateway.run.t", spy_t)

        runner = object.__new__(GatewayRunner)
        runner.config = MagicMock()
        runner.config.get_home_channel.return_value = MagicMock(chat_id="123", thread_id=None)
        runner.adapters = {}

        fake_transport = MagicMock(adapter=MagicMock())
        with patch("gateway.run.resolve_delivery_transport", return_value=fake_transport):
            with pytest.raises(_StopAfterThreadName):
                asyncio.run(
                    runner._process_handoff(
                        {"id": "abcdef1234", "handoff_platform": "telegram", "title": "My Session"}
                    )
                )

        thread_name = captured.get("value")
        assert thread_name, "the t() spy never observed trix.handoff.thread_name"
        assert "My Session" in thread_name
        _assert_debranded_and_russian(thread_name, label="handoff thread_name")


# ---------------------------------------------------------------------------
# 6. Discord auto-thread fallback title (found via the plain-text sweep)
# ---------------------------------------------------------------------------

class TestDiscordDefaultThreadTitle:
    def test_fallback_title_is_debranded_and_russian(self, russian):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        title = GatewayRunner._sanitize_discord_thread_title(runner, "   ")
        _assert_debranded_and_russian(title, label="discord default thread title")

    def test_non_empty_title_is_untouched(self, russian):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        title = GatewayRunner._sanitize_discord_thread_title(runner, "My Session")
        assert title == "My Session"


# ---------------------------------------------------------------------------
# 7. Tech-basis disclosure contract (owner decision 2026-09-17, RAF-182)
# ---------------------------------------------------------------------------
# «Трикс есть Трикс»: продукт — Trix Agent от XDataPlus, и клиентская речь
# НЕ раскрывает технологическую основу — ни «основан на open-source», ни
# «построен на Hermes», ни «от Nous Research», ни лицензии (MIT и любые
# другие). Честность сохраняется ровно до границы: короткий правдивый ответ
# «я Trix Agent от XDataPlus» без раскрытия внутренностей, без лжи про
# «нет инструкций».
#
# Контракт действует на КЛИЕНТ-ВИДИМЫЕ строки — собранные секции системного
# промпта, платформенные подсказки и текст скилла trix-agent, который модель
# читает целиком через skill_view, — а не на комментарии в коде. Настоящие
# внутренние идентификаторы (строчн. `hermes` CLI, $HERMES_HOME, ключ
# metadata.hermes, env-переменные) остаются в покое: их охраняет со своим
# закрытым списком исключений tests/test_no_hermes_reaches_the_client.py.

TECH_BASIS_LEAK_STRINGS = (
    "Hermes",
    "Nous",
    "open-source",
    "open source",
    "открытое ядро",
    "open core",
    "license: MIT",
    "лицензия MIT",
)


def _assert_no_tech_basis(text: str, *, label: str) -> None:
    for leak in TECH_BASIS_LEAK_STRINGS:
        pos = text.find(leak)
        assert pos == -1, (
            f"{label}: tech-basis leak {leak!r} at char {pos}: "
            f"{text[max(0, pos - 70): pos + 90]!r}"
        )


class TestNoTechBasisDisclosure:
    """Identity/help-секции промпта не называют технологическую основу."""

    def test_identity_help_and_confidentiality_sections(self):
        from agent.prompt_builder import (
            DEFAULT_AGENT_IDENTITY,
            TRIX_AGENT_HELP_GUIDANCE,
            TRIX_PROMPT_CONFIDENTIALITY_GUIDANCE,
        )

        _assert_no_tech_basis(DEFAULT_AGENT_IDENTITY, label="DEFAULT_AGENT_IDENTITY")
        _assert_no_tech_basis(TRIX_AGENT_HELP_GUIDANCE, label="TRIX_AGENT_HELP_GUIDANCE")
        _assert_no_tech_basis(
            TRIX_PROMPT_CONFIDENTIALITY_GUIDANCE,
            label="TRIX_PROMPT_CONFIDENTIALITY_GUIDANCE",
        )
        # Прямой вопрос «на чём ты построен?» должен получать короткий
        # честный ответ без раскрытия основ — проверяем, что позиция вообще
        # сформулирована (а не вычеркнута молча).
        assert "Trix Agent" in TRIX_AGENT_HELP_GUIDANCE
        assert "XDataPlus" in TRIX_AGENT_HELP_GUIDANCE

    def test_all_platform_hints(self):
        from agent.prompt_builder import PLATFORM_HINTS

        assert PLATFORM_HINTS, "платформенных подсказок нет — тест ничего не проверил"
        for platform_key, hint in PLATFORM_HINTS.items():
            _assert_no_tech_basis(hint, label=f"PLATFORM_HINTS[{platform_key!r}]")

    def test_conditional_guidance_blocks(self):
        from agent.prompt_builder import computer_use_guidance, hud_surface_note

        for platform_name in ("darwin", "win32", "linux"):
            _assert_no_tech_basis(
                computer_use_guidance(platform_name),
                label=f"computer_use_guidance({platform_name!r})",
            )
        _assert_no_tech_basis(
            hud_surface_note({"read_window_below", "computer_use", "browser_navigate"}),
            label="hud_surface_note",
        )

    def test_trix_agent_skill_content(self):
        """Скилл грузится в ответ skill_view целиком, с frontmatter —
        «license: MIT» в нём означало, что модель знает лицензию и может
        назвать её клиенту по прямому вопросу."""
        skill_md = (
            Path(__file__).resolve().parents[2]
            / "skills" / "autonomous-ai-agents" / "trix-agent" / "SKILL.md"
        )
        text = skill_md.read_text(encoding="utf-8")
        _assert_no_tech_basis(text, label="trix-agent SKILL.md")
        assert "MIT" not in text, (
            "trix-agent SKILL.md всё ещё называет лицензию MIT — "
            "прямое нарушение решения владельца 2026-09-17"
        )

    def test_assembled_telegram_prompt_has_no_tech_basis(self):
        """Собранный промпт реальной клиентской сессии — все три яруса.

        Ловит класс бага RAF-182 целиком: запрещённая фраза жила в константе
        промпта, и ни один тест её не видел, пока промпт не собрали."""
        import importlib.util
        from unittest.mock import patch

        from agent.system_prompt import build_system_prompt_parts

        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "_sysprompt_helper_raf182", root / "tests" / "agent" / "test_system_prompt.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)

        agent = helper._make_agent(
            platform="telegram",
            valid_tool_names=[
                "terminal", "secret_request", "memory", "session_search", "skill_manage",
            ],
        )
        with (
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=""),
        ):
            parts = build_system_prompt_parts(agent)

        assert parts.get("stable"), "промпт не собрался — тест ничего не проверил"
        for tier, text in parts.items():
            _assert_no_tech_basis(text, label=f"system prompt[{tier!r}]")
