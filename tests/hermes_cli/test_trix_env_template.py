"""Tests for the Trix curated .env template and its resolver.

Trix ships a short, curated ~45-line .env template
(``assets/config/trix.env.example``) instead of upstream's 496-line,
127-variable ``.env.example``. ``resolve_env_template()`` is the single,
testable place that decides which file a fresh install copies — see
``hermes_cli/config_template.py``.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

from hermes_cli.config_defaults import OPTIONAL_ENV_VARS
from hermes_cli.config_template import resolve_env_template

REPO_ROOT = Path(__file__).resolve().parents[2]
TRIX_ENV_TEMPLATE_PATH = REPO_ROOT / "assets" / "config" / "trix.env.example"

# Exactly three variables the customer MUST fill in (per spec §3.5 /
# §6): the Telegram bot token, the allow-list of Telegram user ids, and
# ONE of two alternative provider-key lines (both ship empty; the
# customer fills whichever provider they picked).
REQUIRED_VAR_NAMES = frozenset({"TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"})
REQUIRED_PROVIDER_KEY_ALTERNATIVES = frozenset({"OPENROUTER_API_KEY", "GLM_API_KEY"})

# Names that are real, code-consumed env vars but are NOT part of
# hermes_cli.config_defaults.OPTIONAL_ENV_VARS (the setup-wizard/`hermes
# tools` prompt catalog) -- so the "every name is known to the product"
# check below needs an explicit allowance for them, same pattern as task
# 1's _VERSION_MARKER_KEYS.
#
# - TELEGRAM_HOME_CHANNEL: read by tools/environments/local.py,
#   hermes_cli/setup.py, hermes_cli/status.py, and the Telegram adapter's
#   cron-delivery target. Not in OPTIONAL_ENV_VARS because the setup
#   wizard never *prompts* for it directly -- it is normally captured via
#   the `/sethome` chat command instead (see hermes_cli/setup.py:1947).
# - NO_PROXY / no_proxy: standard proxy-bypass env vars consumed by
#   Python's own proxy resolution and by
#   tools/environments/docker.py / plugins/platforms/slack/adapter.py's
#   is_host_excluded_by_no_proxy(). Not a credential, so it was never a
#   candidate for the setup-wizard prompt catalog in the first place.
KNOWN_BUT_NOT_IN_OPTIONAL_ENV_VARS = frozenset(
    {"TELEGRAM_HOME_CHANNEL", "NO_PROXY", "no_proxy"}
)

# Suffixes that mark a name as a non-secret behavioral setting under the
# project rule "secrets only in .env" (see CLAUDE.md's "What we don't
# want" section) -- these belong in config.yaml, never in a .env template.
_NON_SECRET_SUFFIXES = ("_TIMEOUT", "_DEBUG", "_INTERVAL", "_LIMIT")

# Client input required, per spec §1's "half-ready loop" acceptance bar.
_MAX_REQUIRED_INPUT_VARS = 6


@pytest.fixture(scope="module")
def template_text() -> str:
    return TRIX_ENV_TEMPLATE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def active_values(template_text: str) -> dict:
    """Variables python-dotenv actually parses as active (uncommented)
    assignments -- exactly what a real ``load_dotenv()`` call would see."""
    return dotenv_values(stream=io.StringIO(template_text))


@pytest.fixture(scope="module")
def all_declared_names(template_text: str) -> set:
    """Every variable name mentioned in the template, whether active or
    commented out in the optional section.

    Uses python-dotenv's real parser (not a hand-rolled name regex) --
    a copy of the file with every ``# NAME=...`` line uncommented is fed
    through the same ``dotenv_values()`` the active-values fixture uses,
    so quoting/whitespace parsing stays identical to what the loader
    itself would do if the line were active.
    """
    uncommented = re.sub(
        r"^#\s*([A-Za-z_][A-Za-z0-9_]*=.*)$", r"\1", template_text, flags=re.MULTILINE
    )
    return set(dotenv_values(stream=io.StringIO(uncommented)).keys())


# ---------------------------------------------------------------------------
# resolve_env_template()
# ---------------------------------------------------------------------------


class TestResolveEnvTemplate:
    def test_prefers_trix_template_when_present(self, tmp_path):
        assets_dir = tmp_path / "assets" / "config"
        assets_dir.mkdir(parents=True)
        trix_file = assets_dir / "trix.env.example"
        trix_file.write_text("TELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
        upstream_file = tmp_path / ".env.example"
        upstream_file.write_text("SECRET=1\n", encoding="utf-8")

        result = resolve_env_template(tmp_path)

        assert result == trix_file

    def test_falls_back_to_upstream_example(self, tmp_path):
        upstream_file = tmp_path / ".env.example"
        upstream_file.write_text("SECRET=1\n", encoding="utf-8")

        result = resolve_env_template(tmp_path)

        assert result == upstream_file

    def test_returns_none_when_neither_exists(self, tmp_path):
        result = resolve_env_template(tmp_path)

        assert result is None


# ---------------------------------------------------------------------------
# assets/config/trix.env.example content
# ---------------------------------------------------------------------------


class TestTrixEnvTemplateContent:
    def test_template_file_exists(self):
        assert TRIX_ENV_TEMPLATE_PATH.is_file(), (
            f"expected curated template at {TRIX_ENV_TEMPLATE_PATH}"
        )

    def test_dotenv_values_gives_a_non_empty_dict(self, active_values):
        assert isinstance(active_values, dict)
        assert active_values, "dotenv_values() found no active assignments"

    def test_required_vars_present_and_empty(self, active_values):
        for name in REQUIRED_VAR_NAMES:
            assert name in active_values, f"required var {name} missing from template"
            assert not active_values[name], (
                f"required var {name} must ship EMPTY -- a non-empty value in "
                "the template would mean a leaked secret"
            )

    def test_at_least_one_provider_key_alternative_present_and_empty(
        self, active_values
    ):
        present = REQUIRED_PROVIDER_KEY_ALTERNATIVES & active_values.keys()
        assert present, (
            f"template must include at least one of "
            f"{sorted(REQUIRED_PROVIDER_KEY_ALTERNATIVES)}"
        )
        for name in present:
            assert not active_values[name], (
                f"provider key {name} must ship EMPTY -- a non-empty value "
                "would mean a leaked secret"
            )

    def test_every_declared_name_is_known_to_the_product(self, all_declared_names):
        unknown = [
            name
            for name in all_declared_names
            if name not in OPTIONAL_ENV_VARS
            and name not in REQUIRED_VAR_NAMES
            and name not in REQUIRED_PROVIDER_KEY_ALTERNATIVES
            and name not in KNOWN_BUT_NOT_IN_OPTIONAL_ENV_VARS
        ]
        assert not unknown, (
            f"unknown env var name(s) in trix.env.example: {unknown} -- a typo "
            "here means the customer fills in a variable nothing reads"
        )

    def test_no_non_secret_settings_in_template(self, all_declared_names):
        """Project rule: '.env' is for secrets only -- timeouts, debug
        flags, intervals, and limits belong in config.yaml."""
        offenders = [
            name
            for name in all_declared_names
            if name.upper().endswith(_NON_SECRET_SUFFIXES)
        ]
        assert not offenders, (
            f"non-secret setting(s) leaked into the .env template: {offenders} "
            "-- behavioral settings belong in config.yaml"
        )

    def test_client_input_count_within_half_ready_loop_budget(self, active_values):
        """Spec §1's acceptance bar for the 'half-ready loop': at most six
        uncommented, empty variables that require the customer to type
        something in before the agent can run."""
        needs_input = [name for name, value in active_values.items() if not value]
        assert len(needs_input) <= _MAX_REQUIRED_INPUT_VARS, (
            f"template asks the customer to fill in {len(needs_input)} "
            f"variable(s) ({sorted(needs_input)}), more than the budget of "
            f"{_MAX_REQUIRED_INPUT_VARS}"
        )


class TestTrixEnvTemplateProseFixes:
    """Regression coverage for wording issues a review round found: the
    template must not point the customer at a file it never installs
    (Important 3), must not sell a key that does nothing without a
    matching config.yaml edit (Important 4), must explain how to
    uncomment an optional line (Minor), and must not overclaim that a
    missing required var stops the agent from starting at all (Nit) --
    TELEGRAM_ALLOWED_USERS empty just denies everyone, and an empty
    provider key lets the process start and only fails on the first
    message.
    """

    def test_does_not_point_at_a_file_the_customer_does_not_have(self, template_text):
        """.env.example lives in the install directory
        ($HERMES_HOME/hermes-agent/.env.example), not next to the
        customer's ~/.hermes/.env -- the template must not send the
        customer looking for a file that was never copied there."""
        assert ".env.example" not in template_text

    def test_optional_section_explains_how_to_activate_a_line(self, template_text):
        optional_start = template_text.index("ПО ЖЕЛАНИЮ")
        first_var_start = template_text.index("# TELEGRAM_HOME_CHANNEL=")
        header = template_text[optional_start:first_var_start]
        assert "#" in header and (
            "уберите" in header.lower() or "удалите" in header.lower()
        ), "optional-section header must explain removing the leading '#' to activate a line"

    def test_firecrawl_comment_explains_the_config_yaml_toolset_swap(self, template_text):
        firecrawl_idx = template_text.index("FIRECRAWL_API_KEY=")
        # The explanatory comment block sits directly above the variable.
        preceding = template_text[max(0, firecrawl_idx - 500):firecrawl_idx]
        assert "config.yaml" in preceding, (
            "FIRECRAWL_API_KEY's comment must say the key alone does nothing -- "
            "web_extract also requires swapping 'search' for 'web' in "
            "platform_toolsets.telegram in config.yaml"
        )
        assert "web" in preceding and "search" in preceding

    def test_required_section_header_does_not_overclaim(self, template_text):
        assert "не запустится" not in template_text, (
            "an empty TELEGRAM_ALLOWED_USERS still lets the process start (it just "
            "denies everyone), and an empty provider key starts and fails on the "
            "first message -- neither prevents the agent from starting at all"
        )
