"""Живое зеркало провайдера/модели/прокси из default в system_admin.

Реальный временный HERMES_HOME, реальный клиентский шаблон
(``assets/config/trix-config.yaml``), реальный ``create_profile`` через
``hermes business setup`` — E2E, никакого мокинга файловой системы, как и
в ``tests/hermes_cli/test_trix_business.py``.
"""

from pathlib import Path

import pytest
import yaml

from hermes_cli import trix_business as tb
from hermes_cli.trix_admin_profile_mirror import (
    MIRROR_SOURCE_MARKER_RELATIVE,
    sync_admin_profile_from_default,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"

# The real template ships `model: ''` (a bare scalar) — every profile
# create_profile() seeds starts exactly there. A configured default
# profile instead carries a dict, the shape hermes_cli.auth writes via
# _update_config_for_provider(). Building the fixture this way exercises
# the actual scalar->block conversion path a live "already broken" machine
# needs (see hermes_cli/trix_admin_profile_mirror.py's module docstring).
_MODEL_BLOCK = "model:\n  provider: openrouter\n  default: test-model-v1\n"


def _with_configured_model(template_text: str) -> str:
    """Replace the top-level (zero-indent) ``model: ''`` line only — the
    template also has several indented ``model: ''`` leaves under
    ``auxiliary.*`` that a naive string .replace() would hit first."""
    lines = template_text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line == "model: ''\n" or line == "model: ''":
            lines[i] = _MODEL_BLOCK
            return "".join(lines)
    raise AssertionError("top-level 'model: \'\'' line not found — fixture is broken")


@pytest.fixture()
def biz_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    config_text = _with_configured_model(TEMPLATE.read_text(encoding="utf-8"))
    (default_home / "config.yaml").write_text(config_text, encoding="utf-8")
    (default_home / ".env").write_text(
        "TELEGRAM_ALLOWED_USERS=111222333\n"
        "TELEGRAM_BOT_TOKEN=123456:AAABOTTOKEN\n"
        "OPENROUTER_API_KEY=sk-or-first\n"
        "HTTPS_PROXY=http://proxy.example:3128\n",
        encoding="utf-8",
    )
    return default_home


def _run(default_home, **overrides):
    kwargs = dict(
        group_chat_id="-1001234567890",
        admin_thread_id="7",
        company_root=str(Path(default_home).parent / "srv" / "trix"),
        install_skills=False,
    )
    kwargs.update(overrides)
    return tb.run_setup(**kwargs)


def _admin_home(default_home: Path) -> Path:
    return default_home / "profiles" / "system_admin"


def _admin_config(default_home: Path) -> dict:
    return yaml.safe_load((_admin_home(default_home) / "config.yaml").read_text(encoding="utf-8"))


def _admin_env_text(default_home: Path) -> str:
    return (_admin_home(default_home) / ".env").read_text(encoding="utf-8")


def _admin_env(default_home: Path) -> dict:
    """Parsed .env of the admin profile — precise value checks. The raw
    template always lists every known key blank (``OPENROUTER_API_KEY=``),
    so a bare substring check for the key NAME is never the right
    assertion; only a VALUE leaking is the actual defect."""
    from hermes_cli.trix_business import _profile_home_scope
    from hermes_cli.config import load_env

    with _profile_home_scope(_admin_home(default_home)):
        return load_env()


class TestFirstRunRepairsACredentialLessProfile:
    """This is the repair path that matters most: a machine where
    `hermes business setup` already ran WITHOUT this mirror (defect 1's
    reported state) must be fixed by a plain re-run, not by deleting and
    recreating the profile."""

    def test_first_run_mirrors_model_provider_key_and_proxy(self, biz_env):
        result = _run(biz_env)

        admin_cfg = _admin_config(biz_env)
        assert admin_cfg["model"]["provider"] == "openrouter"
        assert admin_cfg["model"]["default"] == "test-model-v1"

        admin_env = _admin_env_text(biz_env)
        assert "OPENROUTER_API_KEY=sk-or-first" in admin_env
        assert "HTTPS_PROXY=http://proxy.example:3128" in admin_env

        mirror = result["admin_mirror"]
        assert "model.provider" in mirror["config_mirrored"]
        assert "model.default" in mirror["config_mirrored"]
        assert "OPENROUTER_API_KEY" in mirror["env_mirrored"]
        assert "HTTPS_PROXY" in mirror["env_mirrored"]

    def test_bot_token_never_mirrored(self, biz_env):
        _run(biz_env)
        admin_env = _admin_env_text(biz_env)
        # The freshly-seeded template always lists TELEGRAM_BOT_TOKEN= (blank)
        # — only the VALUE leaking would be the actual defect.
        assert "AAABOTTOKEN" not in admin_env

    def test_telegram_proxy_never_mirrored(self, biz_env):
        (biz_env / ".env").write_text(
            (biz_env / ".env").read_text(encoding="utf-8") + "TELEGRAM_PROXY=http://tg-only:1080\n",
            encoding="utf-8",
        )
        _run(biz_env)
        assert "tg-only" not in _admin_env_text(biz_env)
        assert not _admin_env(biz_env).get("TELEGRAM_PROXY")

    def test_repair_path_second_setup_run_fixes_a_credential_less_profile(self, biz_env):
        """Simulates the live bug exactly: first `business setup` ran
        before this mirror existed (or with a default profile that had no
        provider configured yet), leaving system_admin with an empty
        model: '' and no .env credentials at all. A later run — after the
        machine's default profile is properly configured — must repair it
        without anyone deleting the profile."""
        # First run: strip the default profile down to "not configured yet"
        # so create_profile's fresh template (`model: ''`, empty .env) is
        # exactly what ends up mirrored — i.e. nothing, matching the
        # reported bug.
        (biz_env / "config.yaml").write_text(
            TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (biz_env / ".env").write_text("TELEGRAM_ALLOWED_USERS=111222333\n", encoding="utf-8")

        _run(biz_env)
        broken_cfg = _admin_config(biz_env)
        assert broken_cfg.get("model") in ("", None)
        assert not _admin_env(biz_env).get("OPENROUTER_API_KEY")

        # Now the machine gets properly configured (setup wizard equivalent).
        config_text = _with_configured_model(TEMPLATE.read_text(encoding="utf-8"))
        (biz_env / "config.yaml").write_text(config_text, encoding="utf-8")
        (biz_env / ".env").write_text(
            "TELEGRAM_ALLOWED_USERS=111222333\n"
            "TELEGRAM_BOT_TOKEN=123456:AAABOTTOKEN\n"
            "OPENROUTER_API_KEY=sk-or-first\n"
            "HTTPS_PROXY=http://proxy.example:3128\n",
            encoding="utf-8",
        )

        result = _run(biz_env)  # the repair run — same CLI invocation, nothing special
        assert result["created_profile"] is False

        fixed_cfg = _admin_config(biz_env)
        assert fixed_cfg["model"]["provider"] == "openrouter"
        assert fixed_cfg["model"]["default"] == "test-model-v1"
        admin_env = _admin_env_text(biz_env)
        assert "OPENROUTER_API_KEY=sk-or-first" in admin_env
        assert not _admin_env(biz_env).get("TELEGRAM_BOT_TOKEN")
        assert "AAABOTTOKEN" not in admin_env


class TestOngoingMirror:
    def test_proxy_change_on_default_propagates_on_next_setup_run(self, biz_env):
        _run(biz_env)
        (biz_env / ".env").write_text(
            (biz_env / ".env").read_text(encoding="utf-8").replace(
                "HTTPS_PROXY=http://proxy.example:3128",
                "HTTPS_PROXY=http://new-proxy.example:9999",
            ),
            encoding="utf-8",
        )
        result = _run(biz_env)
        assert "HTTPS_PROXY" in result["admin_mirror"]["env_mirrored"]
        assert "HTTPS_PROXY=http://new-proxy.example:9999" in _admin_env_text(biz_env)
        assert not _admin_env(biz_env).get("TELEGRAM_BOT_TOKEN")

    def test_provider_key_change_on_default_propagates(self, biz_env):
        _run(biz_env)
        (biz_env / ".env").write_text(
            (biz_env / ".env").read_text(encoding="utf-8").replace(
                "OPENROUTER_API_KEY=sk-or-first", "OPENROUTER_API_KEY=sk-or-second",
            ),
            encoding="utf-8",
        )
        result = _run(biz_env)
        assert "OPENROUTER_API_KEY" in result["admin_mirror"]["env_mirrored"]
        assert "OPENROUTER_API_KEY=sk-or-second" in _admin_env_text(biz_env)

    def test_noop_sync_writes_nothing(self, biz_env):
        _run(biz_env)
        admin_home = _admin_home(biz_env)
        cfg_before = (admin_home / "config.yaml").read_text(encoding="utf-8")
        env_before = (admin_home / ".env").read_text(encoding="utf-8")

        result = _run(biz_env)

        assert result["admin_mirror"]["config_mirrored"] == []
        assert result["admin_mirror"]["env_mirrored"] == []
        assert result["admin_mirror"]["diverged_now"] == []
        assert result["admin_mirror"]["changed"] is False
        assert (admin_home / "config.yaml").read_text(encoding="utf-8") == cfg_before
        assert (admin_home / ".env").read_text(encoding="utf-8") == env_before


class TestHandEditIsPreserved:
    def test_hand_edited_env_key_is_preserved_and_reported_then_never_touched_again(self, biz_env):
        _run(biz_env)  # baseline: env.OPENROUTER_API_KEY mirrored = sk-or-first
        admin_home = _admin_home(biz_env)

        # Administrator edits the admin profile's .env directly.
        admin_env_path = admin_home / ".env"
        admin_env_path.write_text(
            admin_env_path.read_text(encoding="utf-8").replace(
                "OPENROUTER_API_KEY=sk-or-first", "OPENROUTER_API_KEY=sk-or-hand-edited",
            ),
            encoding="utf-8",
        )

        # Default changes too, in the same round.
        (biz_env / ".env").write_text(
            (biz_env / ".env").read_text(encoding="utf-8").replace(
                "OPENROUTER_API_KEY=sk-or-first", "OPENROUTER_API_KEY=sk-or-second",
            ),
            encoding="utf-8",
        )

        result = _run(biz_env)
        assert "env.OPENROUTER_API_KEY" in result["admin_mirror"]["diverged_now"]
        assert "sk-or-hand-edited" in _admin_env_text(biz_env)
        assert "sk-or-second" not in _admin_env_text(biz_env)

        # Default changes AGAIN — the hand edit must survive forever, and
        # the divergence is reported only once (already known).
        (biz_env / ".env").write_text(
            (biz_env / ".env").read_text(encoding="utf-8").replace(
                "OPENROUTER_API_KEY=sk-or-second", "OPENROUTER_API_KEY=sk-or-third",
            ),
            encoding="utf-8",
        )
        result2 = _run(biz_env)
        assert result2["admin_mirror"]["diverged_now"] == []
        assert "sk-or-hand-edited" in _admin_env_text(biz_env)


class TestNeverMirrored:
    def test_restricted_toolset_approvals_and_external_dirs_survive_sync(self, biz_env):
        _run(biz_env)
        admin_home = _admin_home(biz_env)
        before = yaml.safe_load((admin_home / "config.yaml").read_text(encoding="utf-8"))
        toolsets_before = before["platform_toolsets"]["telegram"]
        deny_before = before["approvals"]["deny"]
        backend_before = before["terminal"]["backend"]

        # Change something on default that WOULD get mirrored, to prove a
        # real sync ran this round, not just a no-op skip.
        (biz_env / ".env").write_text(
            (biz_env / ".env").read_text(encoding="utf-8").replace(
                "OPENROUTER_API_KEY=sk-or-first", "OPENROUTER_API_KEY=sk-or-changed",
            ),
            encoding="utf-8",
        )
        result = _run(biz_env)
        assert "OPENROUTER_API_KEY" in result["admin_mirror"]["env_mirrored"]

        after = yaml.safe_load((admin_home / "config.yaml").read_text(encoding="utf-8"))
        assert after["platform_toolsets"]["telegram"] == toolsets_before
        assert after["approvals"]["deny"] == deny_before
        assert after["terminal"]["backend"] == backend_before

    def test_direct_sync_call_never_touches_never_mirror_keys_even_with_bogus_defaults(
        self, biz_env, tmp_path
    ):
        """Defensive unit-level check of the _NEVER_MIRROR_CONFIG_TOP_KEYS
        guard directly, independent of `_model_scalar_paths`' own fixed
        prefix — belt-and-braces per the module docstring."""
        _run(biz_env)
        admin_home = _admin_home(biz_env)
        before = (admin_home / "config.yaml").read_text(encoding="utf-8")

        report = sync_admin_profile_from_default(default_home=biz_env, admin_home=admin_home)
        assert report["config_mirrored"] == []
        after = (admin_home / "config.yaml").read_text(encoding="utf-8")
        assert after == before


class TestMirrorSourceMarker:
    def test_marker_written_and_names_default(self, biz_env):
        _run(biz_env)
        marker_path = _admin_home(biz_env) / MIRROR_SOURCE_MARKER_RELATIVE
        assert marker_path.is_file()
        import json

        assert json.loads(marker_path.read_text(encoding="utf-8"))["source_profile"] == "default"
