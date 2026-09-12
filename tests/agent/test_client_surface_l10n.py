"""Client-layer commands must render Russian, never an English literal.

``docs/product/plans/2026-09-01-client-command-surface.md`` Task 7: `/status`,
`/resume`, `/memory`, `/model` and `/goal`/`/heartbeat` all had spots where the
client -- Telegram-only, Russian-only, no console, no config.yaml -- would see
raw English. Two distinct defect shapes are covered here:

1. The catalog entry itself was English (``locales/ru.yaml`` never
   translated) -- covered by rendering ``gateway.status.*`` /
   ``gateway.resume.*`` through :func:`agent.i18n.t`.
2. The catalog entry was fine but the code never called ``t()`` at all
   (``hermes_cli/write_approval_commands.py``), or called it for only ONE of
   several call sites that render the same screen (the /model picker's
   provider/model/group screens in ``plugins/platforms/telegram/adapter.py``,
   and ``GoalManager``/``HeartbeatManager.status_line()`` in
   ``hermes_cli/goals.py`` / ``hermes_cli/heartbeat.py``). A catalog-only
   sweep can't find this class of bug -- these tests call the actual
   producing function, not just the catalog key, matching the lesson that
   surfaced it on ``/goal`` and ``/heartbeat``.

Every test pins ``HERMES_LANGUAGE=ru`` itself and resets the i18n cache in a
``try/finally`` -- ``tests/conftest.py`` pins ``en`` for the whole suite, so
without this every assertion here is either always red or asserts nothing.
See ``tests/hermes_cli/test_trix_menu.py::TestDebugDescriptionMentionsLogs``
for the pattern this file follows.

Exceptions ``docs/product/specs/2026-09-01-...-design.md`` §7.7 carves out:
model/provider names (opaque identifiers, never translated) and the Matrix
strings in ``gateway.resume.matrix_*`` (dead code on this Telegram-only
product -- translating them buys nothing but merge conflicts upstream).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent import i18n
from hermes_cli.trix_menu import DISABLED_COMMANDS


@pytest.fixture
def ru():
    """Pin the active language to Russian for one test, then restore."""
    import os

    old = os.environ.get("HERMES_LANGUAGE")
    os.environ["HERMES_LANGUAGE"] = "ru"
    i18n.reset_language_cache()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("HERMES_LANGUAGE", None)
        else:
            os.environ["HERMES_LANGUAGE"] = old
        i18n.reset_language_cache()


# A handful of English function words that only show up if a template
# reverted to its English literal. Deliberately NOT a giant blocklist (that
# would just be a source-text regex in disguise) -- each word here is one
# that appeared verbatim in the pre-fix English string for the key that
# checks it, so a revert of THAT key is exactly what turns the assertion red.
_ENGLISH_MARKERS = (
    "Model", "Context", "Lifetime", "tokens billed",
    "Could not parse", "blocked:", "belongs to",
    "No pending", "Pending", "Approved", "Rejected", "Usage:", "Invalid value",
    "Configuration", "Current model", "Select a", "Provider family",
    "cancelled.", "more available",
    "No active goal", "turns", "subgoal", "gate", "parked", "active,", "paused,",
    "No heartbeat", "fired", "next in",
)


def _assert_no_english_markers(text: str, *, allow: tuple = ()) -> None:
    lowered = text
    offenders = [
        w for w in _ENGLISH_MARKERS
        if w not in allow and w in lowered
    ]
    assert not offenders, f"English literal leaked into client text: {offenders!r} in {text!r}"


def _assert_no_disabled_command_pointer(text: str) -> None:
    """No client-facing text may point the client at a disabled command."""
    for name in DISABLED_COMMANDS:
        needle = f"/{name}"
        assert needle not in text, (
            f"client text points at disabled command {needle!r}: {text!r}"
        )


# ---------------------------------------------------------------------------
# /status -- catalog values were English (Task 7 step 2/3)
# ---------------------------------------------------------------------------

class TestStatusCardIsRussian:
    def test_model_line(self, ru):
        text = i18n.t("gateway.status.model", model="gpt-4")
        assert "Модель" in text
        _assert_no_english_markers(text)

    def test_model_provider_line(self, ru):
        text = i18n.t("gateway.status.model_provider", model="gpt-4", provider="openai")
        assert "Модель" in text
        _assert_no_english_markers(text)

    def test_context_line(self, ru):
        text = i18n.t("gateway.status.context", used=10, total=100, pct=10)
        assert "Контекст" in text
        _assert_no_english_markers(text)

    def test_context_used_line(self, ru):
        text = i18n.t("gateway.status.context_used", used=10)
        assert "Контекст" in text
        assert "токенов" in text
        _assert_no_english_markers(text)

    def test_tokens_line_is_russian_and_has_no_context_pointer(self, ru):
        text = i18n.t("gateway.status.tokens", tokens=12345)
        assert "токенов" in text
        _assert_no_english_markers(text)
        # Step 3: /context is a disabled command -- the card must not point
        # the client at it (the pointer was removed, not just re-worded).
        _assert_no_disabled_command_pointer(text)
        assert "use `/context`" not in text

    def test_tokens_line_no_context_pointer_in_english_catalog_either(self):
        """Task 7 step 3 explicitly calls out checking the English catalog
        too -- it carried the same dead /context pointer."""
        text = i18n.t("gateway.status.tokens", tokens=12345, lang="en")
        assert "use `/context`" not in text
        assert "/context" not in text


# ---------------------------------------------------------------------------
# /resume -- parse_error and blocked_not_owner were English (Task 7 step 2)
# ---------------------------------------------------------------------------

class TestResumeIsRussian:
    def test_parse_error(self, ru):
        text = i18n.t("gateway.resume.parse_error", error="boom")
        assert "разобрать" in text
        _assert_no_english_markers(text)

    def test_blocked_not_owner(self, ru):
        text = i18n.t("gateway.resume.blocked_not_owner", name="Мой сеанс")
        assert "принадлежит" in text
        _assert_no_english_markers(text)


class TestResumeMatrixStringsDeliberatelyStayEnglish:
    """Matrix is dead code on this Telegram-only product (plan Task 7,
    global constraints) -- these four keys are the documented exemption,
    not an oversight the sweep above should catch."""

    _MATRIX_KEYS = (
        "gateway.resume.matrix_no_named_sessions",
        "gateway.resume.matrix_blocked_no_origin",
        "gateway.resume.matrix_blocked_other_room",
        "gateway.resume.matrix_cross_room_success",
    )

    def test_exactly_four_matrix_keys_are_exempted(self):
        assert len(self._MATRIX_KEYS) == 4

    def test_each_matrix_key_exists_in_the_catalog(self, ru):
        for key in self._MATRIX_KEYS:
            rendered = i18n.t(key, name="x", room="x", title="x", msg_part="")
            assert rendered != key, f"{key} missing from catalog"


# ---------------------------------------------------------------------------
# /memory -- hermes_cli/write_approval_commands.py called t() nowhere at all
# (Task 7 step 4). Rendering the CODE, not the catalog, is the point: a
# catalog-only sweep is exactly what missed this class of bug.
# ---------------------------------------------------------------------------

class TestMemoryCommandIsRussian:
    def test_bare_command_state_and_empty_pending(self, ru):
        from hermes_cli import write_approval_commands as wac
        from tools import write_approval as wa

        with patch.object(wa, "write_approval_enabled", return_value=False), \
             patch.object(wa, "list_pending", return_value=[]):
            text = wac.handle_pending_subcommand("memory", [])
        assert text is not None
        assert "выключено" in text
        assert "Нет ожидающих" in text
        _assert_no_english_markers(text)

    def test_pending_list_with_records(self, ru):
        from hermes_cli import write_approval_commands as wac
        from tools import write_approval as wa

        records = [{"id": "a1", "origin": "foreground", "summary": "тест"}]
        with patch.object(wa, "list_pending", return_value=records):
            text = wac.handle_pending_subcommand("memory", ["pending"])
        assert "Ожидающие" in text
        _assert_no_english_markers(text)

    def test_approve_with_no_target(self, ru):
        from hermes_cli import write_approval_commands as wac

        text = wac.handle_pending_subcommand("memory", ["approve"])
        assert "Формат" in text
        _assert_no_english_markers(text)

    def test_approval_invalid_value(self, ru):
        from hermes_cli import write_approval_commands as wac
        from tools import write_approval as wa

        with patch.object(wa, "write_approval_enabled", return_value=False):
            text = wac.handle_pending_subcommand("memory", ["approval", "xyz"])
        assert "Недопустимое значение" in text
        _assert_no_english_markers(text)

    def test_approval_toggled_on(self, ru):
        from hermes_cli import write_approval_commands as wac

        calls = []
        text = wac.handle_pending_subcommand(
            "memory", ["approval", "on"], set_mode_fn=calls.append,
        )
        assert calls == [True]
        assert "включено" in text
        _assert_no_english_markers(text)

    def test_not_found(self, ru):
        from hermes_cli import write_approval_commands as wac
        from tools import write_approval as wa

        with patch.object(wa, "list_pending", return_value=[{"id": "a1"}]), \
             patch.object(wa, "get_pending", return_value=None):
            text = wac.handle_pending_subcommand("memory", ["approve", "zzz"])
        assert "Нет ожидающей правки" in text
        _assert_no_english_markers(text)


# ---------------------------------------------------------------------------
# /model -- the first screen (and every screen the picker redraws to) was an
# f-string that never went through t() (Task 7 step 5). The plan cited one
# call site; the same literal was duplicated across six -- see report.
# ---------------------------------------------------------------------------

class TestModelPickerIsRussian:
    @staticmethod
    def _adapter():
        from plugins.platforms.telegram.adapter import TelegramAdapter

        return TelegramAdapter.__new__(TelegramAdapter)

    def test_provider_screen_first_view(self, ru):
        a = self._adapter()
        text = a._model_picker_provider_text("gpt-4", "OpenAI", "")
        assert "Настройка модели" in text
        assert "Текущая модель" in text
        assert "Провайдер" in text
        assert "Выберите провайдера" in text
        _assert_no_english_markers(text)

    def test_provider_screen_unknown_model(self, ru):
        a = self._adapter()
        text = a._model_picker_provider_text(None, "OpenAI", "")
        assert "неизвестна" in text
        assert "unknown" not in text

    def test_model_list_screen(self, ru):
        a = self._adapter()
        extra = a._model_picker_more_available(20, 8)
        text = a._model_picker_model_list_text("OpenAI", "", extra)
        assert "Выберите модель" in text
        assert "ещё 12 доступно" in text
        _assert_no_english_markers(text)

    def test_group_screen(self, ru):
        a = self._adapter()
        text = a._model_picker_group_text("OpenAI family")
        assert "Семейство провайдеров" in text
        assert "Выберите провайдера" in text
        _assert_no_english_markers(text)

    def test_more_available_is_empty_when_nothing_hidden(self, ru):
        a = self._adapter()
        assert a._model_picker_more_available(5, 5) == ""

    def test_cancelled_text(self, ru):
        text = i18n.t("trix.cmd.model.picker.cancelled")
        assert "отменён" in text
        _assert_no_english_markers(text)


# ---------------------------------------------------------------------------
# /goal and /heartbeat -- status_line() in the upstream module returned an
# English literal even though other branches of the SAME command already
# went through t() (Task 7 step 7 -- "translation written, call not wired").
# ---------------------------------------------------------------------------

class TestGoalStatusLineIsRussian:
    @staticmethod
    def _manager(state):
        from hermes_cli.goals import GoalManager

        gm = GoalManager.__new__(GoalManager)
        gm.session_id = "test"
        gm.default_max_turns = 20
        gm._state = state
        return gm

    def test_no_goal(self, ru):
        gm = self._manager(None)
        text = gm.status_line()
        assert "Цель не задана" in text
        _assert_no_english_markers(text)

    def test_active_goal_with_subgoal_and_gate(self, ru):
        from hermes_cli.goals import GoalState

        state = GoalState(
            goal="протестировать", status="active", max_turns=20, turns_used=3,
            subgoals=["доп. критерий"], gates=[{"command": "pytest"}],
        )
        gm = self._manager(state)
        text = gm.status_line()
        assert "ходов" in text
        assert "подцель" in text
        assert "проверка" in text
        _assert_no_english_markers(text)
        _assert_no_disabled_command_pointer(text)

    def test_paused_goal_with_reason(self, ru):
        from hermes_cli.goals import GoalState

        state = GoalState(goal="x", status="paused", paused_reason="ручная пауза")
        gm = self._manager(state)
        text = gm.status_line()
        assert "на паузе" in text
        assert "ручная пауза" in text
        _assert_no_english_markers(text)

    def test_done_goal(self, ru):
        from hermes_cli.goals import GoalState

        state = GoalState(goal="x", status="done")
        gm = self._manager(state)
        text = gm.status_line()
        assert "выполнена" in text
        _assert_no_english_markers(text)

    def test_parked_on_timer_with_default_reason(self, ru):
        """The 'parked {remaining}s' branch (timer barrier, no custom
        waiting_reason -- exercises the default-reason localization too)."""
        import time

        from hermes_cli.goals import GoalState

        state = GoalState(
            goal="x", status="active", waiting_until=time.time() + 30,
        )
        gm = self._manager(state)
        text = gm.status_line()
        assert "на паузе" in text
        assert "с" in text  # seconds suffix, e.g. "29с"
        _assert_no_english_markers(text)
        _assert_no_disabled_command_pointer(text)


class TestHeartbeatStatusLineIsRussian:
    @staticmethod
    def _manager(state):
        from hermes_cli.heartbeat import HeartbeatManager

        hm = HeartbeatManager.__new__(HeartbeatManager)
        hm.session_id = "test"
        hm._state = state
        return hm

    def test_no_heartbeat(self, ru):
        hm = self._manager(None)
        text = hm.status_line()
        assert "Пульс не задан" in text
        _assert_no_english_markers(text)

    def test_active_heartbeat_with_fire_count(self, ru):
        import time

        from hermes_cli.heartbeat import HeartbeatState

        state = HeartbeatState(
            prompt="проверь статус", interval_seconds=600, status="active",
            created_at=time.time(), fire_count=3,
        )
        hm = self._manager(state)
        text = hm.status_line()
        assert "сработал" in text
        assert "раза" in text
        _assert_no_english_markers(text)

    def test_paused_heartbeat_singular_fire_count(self, ru):
        import time

        from hermes_cli.heartbeat import HeartbeatState

        state = HeartbeatState(
            prompt="x", interval_seconds=60, status="paused",
            created_at=time.time(), fire_count=1,
        )
        hm = self._manager(state)
        text = hm.status_line()
        assert "на паузе" in text
        assert "сработал 1 раз" in text
        _assert_no_english_markers(text)


# ---------------------------------------------------------------------------
# /setup -- the placeholder in brackets (Task 7 step 6)
# ---------------------------------------------------------------------------

class TestSetupWizardHasNoUnfilledPlaceholder:
    def test_no_square_brackets(self, ru):
        text = i18n.t("trix.setup_wizard.reply", url="https://x")
        assert "[" not in text
        assert "]" not in text

    def test_no_xdataplus_mention(self, ru):
        text = i18n.t("trix.setup_wizard.reply", url="https://x")
        assert "XDataPlus" not in text

    def test_still_points_at_searching_for_trix(self, ru):
        text = i18n.t("trix.setup_wizard.reply", url="https://x")
        assert "Trix" in text


# ---------------------------------------------------------------------------
# /agents -- background-delegation progress rows were raw f-strings never
# wrapped in t() (found by the Task 7 step-8 sweep for the same "translation
# written, call not wired" class -- gateway/slash_commands.py, the /agents
# handler's background-delegation branch).
# ---------------------------------------------------------------------------

class TestAgentsDelegationRowsAreRussian:
    def test_no_progress(self, ru):
        text = i18n.t("trix.cmd.agents.no_progress", seconds="5")
        assert "прогресса" in text
        _assert_no_english_markers(text)

    def test_quiet(self, ru):
        text = i18n.t("trix.cmd.agents.quiet", seconds="12")
        assert "тихо" in text

    def test_child_label(self, ru):
        text = i18n.t("trix.cmd.agents.child_label", index=1)
        assert "потомок" in text

    def test_api_calls_line(self, ru):
        text = i18n.t("trix.cmd.agents.api_calls_line", calls=3, doing="между ходами")
        assert "вызовов API" in text

    def test_active_ago(self, ru):
        text = i18n.t("trix.cmd.agents.active_ago", seconds="7")
        assert "активен" in text
        assert "назад" in text


# ---------------------------------------------------------------------------
# /version -- the "(unreleased build)" fallback (no release tag reachable
# locally) was a bare literal outside t() in hermes_cli/banner.py (found by
# the same sweep).
# ---------------------------------------------------------------------------

class TestVersionUnreleasedBuildIsRussian:
    def test_reply_text_without_a_release_tag(self, ru):
        from unittest.mock import patch

        import hermes_cli.banner as banner

        with patch.object(banner, "get_latest_release_tag", return_value=None):
            text = banner.format_version_reply_text()
        assert "Trix Agent" in text
        assert "unreleased build" not in text
        assert "сборка" in text


# ---------------------------------------------------------------------------
# No client-facing text (across everything fixed above) points the client at
# a DISABLED command -- taken from trix_menu.DISABLED_COMMANDS, not
# hand-copied (plan global constraint + Task 7 step 8/3).
# ---------------------------------------------------------------------------

class TestNoPointersToDisabledCommands:
    def test_status_and_resume_and_setup(self, ru):
        texts = [
            i18n.t("gateway.status.tokens", tokens=1),
            i18n.t("gateway.resume.parse_error", error="x"),
            i18n.t("gateway.resume.blocked_not_owner", name="x"),
            i18n.t("trix.setup_wizard.reply", url="https://x"),
        ]
        for text in texts:
            _assert_no_disabled_command_pointer(text)

    def test_goal_and_heartbeat_status_lines(self, ru):
        from hermes_cli.goals import GoalManager, GoalState
        from hermes_cli.heartbeat import HeartbeatManager, HeartbeatState
        import time

        gm = GoalManager.__new__(GoalManager)
        gm.session_id = "t"
        gm.default_max_turns = 20
        gm._state = GoalState(goal="x", status="active")
        _assert_no_disabled_command_pointer(gm.status_line())

        hm = HeartbeatManager.__new__(HeartbeatManager)
        hm.session_id = "t"
        hm._state = HeartbeatState(
            prompt="x", interval_seconds=60, status="active", created_at=time.time(),
        )
        _assert_no_disabled_command_pointer(hm.status_line())


# ---------------------------------------------------------------------------
# /fast and /reasoning -- client-command-surface Task 9c, owner decision
# 2026-09-01: both stay in the client's command surface, but talk about
# price and quality, not provider terminology. "Priority Processing" is
# OpenAI's own feature name; "reasoning effort" mirrors the
# ``reasoning_effort`` API parameter almost verbatim -- neither means
# anything to a non-technical Telegram client. The replacement copy states
# the actual trade-off instead: faster/more-thorough costs more, slower/
# simpler costs less.
# ---------------------------------------------------------------------------

_PROVIDER_TERMS = ("Priority Processing", "reasoning effort", "OpenAI", "Anthropic")


def _assert_no_provider_terms(text: str) -> None:
    lowered = text.lower()
    offenders = [term for term in _PROVIDER_TERMS if term.lower() in lowered]
    assert not offenders, f"provider terminology leaked into client text: {offenders!r} in {text!r}"


class TestFastCommandSpeaksMoneyNotProviderTerms:
    def test_status_names_cost_and_speed_tradeoff(self, ru):
        text = i18n.t("gateway.fast.status", mode="обычный")
        _assert_no_provider_terms(text)
        assert "дороже" in text
        assert "быстрее" in text

    def test_not_supported_names_no_provider(self, ru):
        text = i18n.t("gateway.fast.not_supported")
        _assert_no_provider_terms(text)

    def test_saved_and_session_only_name_no_provider(self, ru):
        for key in ("gateway.fast.saved", "gateway.fast.session_only"):
            text = i18n.t(key, label=i18n.t("gateway.fast.label_fast"))
            _assert_no_provider_terms(text)

    def test_picker_title_and_choices_name_cost(self, ru):
        text = i18n.t("gateway.fast.picker_title", mode="обычный")
        _assert_no_provider_terms(text)
        choice_fast = i18n.t("gateway.fast.choice_fast")
        choice_normal = i18n.t("gateway.fast.choice_normal")
        _assert_no_provider_terms(choice_fast)
        _assert_no_provider_terms(choice_normal)
        assert "дороже" in choice_fast

    def test_english_catalog_also_avoids_provider_terms(self):
        for key, kwargs in [
            ("gateway.fast.status", {"mode": "standard"}),
            ("gateway.fast.not_supported", {}),
            ("gateway.fast.picker_title", {"mode": "standard"}),
            ("gateway.fast.choice_fast", {}),
            ("gateway.fast.choice_normal", {}),
        ]:
            text = i18n.t(key, lang="en", **kwargs)
            _assert_no_provider_terms(text)
        assert "costs more" in i18n.t("gateway.fast.status", lang="en", mode="standard")


class TestReasoningCommandSpeaksMoneyNotProviderTerms:
    def test_status_names_cost_and_accuracy_tradeoff(self, ru):
        text = i18n.t(
            "gateway.reasoning.status",
            level="medium", scope="глобальная конфигурация", display="выключено",
        )
        _assert_no_provider_terms(text)
        assert "дороже" in text
        assert "точн" in text  # "точнее"/"точность" -- stem match on purpose

    def test_set_global_and_set_session_name_no_provider(self, ru):
        for key in (
            "gateway.reasoning.set_global",
            "gateway.reasoning.set_global_save_failed",
            "gateway.reasoning.set_session",
        ):
            text = i18n.t(key, effort="high")
            _assert_no_provider_terms(text)
            assert "дороже" in text

    def test_picker_title_names_cost(self, ru):
        text = i18n.t(
            "gateway.reasoning.picker_title",
            level="medium", scope="глобальная конфигурация", display="выключено",
        )
        _assert_no_provider_terms(text)
        assert "дороже" in text

    def test_english_catalog_also_avoids_provider_terms(self):
        for key, kwargs in [
            ("gateway.reasoning.status", {"level": "medium", "scope": "global config", "display": "off"}),
            ("gateway.reasoning.set_global", {"effort": "high"}),
            ("gateway.reasoning.picker_title", {"level": "medium", "scope": "global config", "display": "off"}),
        ]:
            text = i18n.t(key, lang="en", **kwargs)
            _assert_no_provider_terms(text)
        assert "costs more" in i18n.t("gateway.reasoning.set_global", lang="en", effort="high")


class TestFastAndReasoningArgLiteralsStayEnglishAndTypeable:
    """Task 9c step 2's carve-out: /fast's ``normal|fast`` and /reasoning's
    effort levels are never translated -- ``command_args_hint()``
    (hermes_cli/commands.py) leaves any hint containing ``|`` in English
    because the parser only accepts the literal English word. So the
    Russian status/picker copy must show the client the exact command to
    type, not a translated word standing in for it."""

    def test_fast_usage_shows_the_literal_command(self, ru):
        text = i18n.t("gateway.fast.status", mode="обычный")
        assert "/fast <normal|fast|status>" in text

    def test_reasoning_usage_shows_the_literal_command(self, ru):
        text = i18n.t(
            "gateway.reasoning.status",
            level="medium", scope="глобальная конфигурация", display="выключено",
        )
        assert "none|minimal|low|medium|high|xhigh|max|ultra" in text
