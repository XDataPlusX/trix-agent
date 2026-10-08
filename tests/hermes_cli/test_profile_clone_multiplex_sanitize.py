"""RAF-191: profile clones must not inherit the multiplexer configuration.

Incident 2026-09-17 (prod): a clone of the default profile — the host's
multiplexer — inherited ``gateway.multiplex_profiles`` +
``gateway.profile_routes`` + ``gateway.multiplex_profile_allowlist``
byte-for-byte and became a "second multiplexer":

  * its gateway tried to serve the default profile with the MAIN bot
    token (409 conflict with the real multiplexer's poller);
  * in multiplex mode the platform allowlist gate reads the per-profile
    secret scope, which was empty in the clone → fail-closed lockout of
    the client.

Fix: ``create_profile`` strips the multiplexer keys from the clone's
config.yaml (both clone paths) unless ``keep_multiplex=True``.
"""

from pathlib import Path

import pytest
import yaml

from hermes_cli.profiles import create_profile


_NESTED_MULTIPLEX_CONFIG = """\
# multiplexer source config
gateway:
  multiplex_profiles: true
  multiplex_profile_allowlist:
    - system_admin
  profile_routes:
    - name: admin-topics
      platform: telegram
      profile: system_admin
      chat_id: "-100999"
model:
  provider: openai-codex
"""

_TOPLEVEL_MULTIPLEX_CONFIG = """\
# legacy spelling: multiplex keys at the top level
multiplex_profiles: true
profile_routes:
  - name: admin-topics
    platform: telegram
    profile: system_admin
    chat_id: "-100999"
model:
  provider: openai-codex
"""


@pytest.fixture()
def multiplex_default_home(tmp_path, monkeypatch):
    """Default profile (~/.hermes) configured as the host's multiplexer."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return default_home


def _load_clone_config(profile_dir: Path) -> dict:
    raw = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _assert_no_multiplex_keys(raw: dict) -> None:
    for key in ("multiplex_profiles", "profile_routes", "multiplex_profile_allowlist"):
        assert key not in raw, f"top-level {key} leaked into the clone"
        assert key not in (raw.get("gateway") or {}), (
            f"gateway.{key} leaked into the clone"
        )


class TestCloneSanitizesMultiplex:
    def test_clone_strips_nested_multiplex_keys(self, multiplex_default_home):
        multiplex_default_home.joinpath("config.yaml").write_text(
            _NESTED_MULTIPLEX_CONFIG, encoding="utf-8"
        )

        clone_dir = create_profile("operations", clone_config=True, no_alias=True)

        raw = _load_clone_config(clone_dir)
        _assert_no_multiplex_keys(raw)
        # The rest of the cloned config survives the rewrite.
        assert raw["model"]["provider"] == "openai-codex"

    def test_clone_strips_toplevel_multiplex_keys(self, multiplex_default_home):
        multiplex_default_home.joinpath("config.yaml").write_text(
            _TOPLEVEL_MULTIPLEX_CONFIG, encoding="utf-8"
        )

        clone_dir = create_profile("operations", clone_config=True, no_alias=True)

        _assert_no_multiplex_keys(_load_clone_config(clone_dir))

    def test_clone_all_strips_multiplex_keys(self, multiplex_default_home):
        multiplex_default_home.joinpath("config.yaml").write_text(
            _NESTED_MULTIPLEX_CONFIG, encoding="utf-8"
        )

        clone_dir = create_profile("operations", clone_all=True, no_alias=True)

        _assert_no_multiplex_keys(_load_clone_config(clone_dir))

    def test_keep_multiplex_preserves_keys(self, multiplex_default_home):
        multiplex_default_home.joinpath("config.yaml").write_text(
            _NESTED_MULTIPLEX_CONFIG, encoding="utf-8"
        )

        clone_dir = create_profile(
            "operations", clone_config=True, no_alias=True, keep_multiplex=True
        )

        gateway = _load_clone_config(clone_dir).get("gateway") or {}
        assert gateway.get("multiplex_profiles") is True
        assert gateway.get("multiplex_profile_allowlist") == ["system_admin"]
        assert gateway.get("profile_routes")

    def test_plain_clone_config_survives(self, tmp_path, monkeypatch):
        """A source without multiplex keys keeps its settings after cloning.

        (The clone config is normalized by the config-version migration
        pipeline, so the contract is semantic, not byte-identity: no
        multiplex keys appear and the plain settings survive.)
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(default_home))
        default_home.joinpath("config.yaml").write_text(
            "# plain profile\nmodel:\n  provider: x\n", encoding="utf-8"
        )

        clone_dir = create_profile("coder", clone_config=True, no_alias=True)

        raw = _load_clone_config(clone_dir)
        _assert_no_multiplex_keys(raw)
        assert raw["model"]["provider"] == "x"


class TestIncidentReproductionE2E:
    def test_clone_gateway_serves_only_itself(self, multiplex_default_home, monkeypatch):
        """E2E repro of the 2026-09-17 incident chain, at config level.

        Before the fix the clone kept multiplex on, so its gateway resolved
        the default profile into its served set and double-bound the main
        bot token (409). After the fix the clone's real gateway config loads
        with multiplex off and the real served-set chokepoint returns exactly
        one home — the clone itself.
        """
        multiplex_default_home.joinpath("config.yaml").write_text(
            _NESTED_MULTIPLEX_CONFIG, encoding="utf-8"
        )

        clone_dir = create_profile("operations", clone_config=True, no_alias=True)

        from gateway.config import load_gateway_config

        # What `hermes -p operations gateway` does: HERMES_HOME = the clone.
        monkeypatch.setenv("HERMES_HOME", str(clone_dir))
        monkeypatch.delenv("GATEWAY_MULTIPLEX_PROFILES", raising=False)

        cfg = load_gateway_config()
        assert cfg.multiplex_profiles is False
        assert cfg.profile_routes == []

        # The "which profiles does this gateway serve" chokepoint with the
        # clone's own resolved config: a single home — the clone itself.
        # (Pre-fix this resolved to default + every sibling profile, which
        # is what double-bound the main bot token.)
        from hermes_cli.profiles import profiles_to_serve

        homes = profiles_to_serve(
            multiplex=cfg.multiplex_profiles,
            profile_allowlist=cfg.multiplex_profile_allowlist,
        )
        assert [(name, Path(home)) for name, home in homes] == [
            ("operations", clone_dir)
        ]
