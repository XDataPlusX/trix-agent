"""Tests for atomic_roundtrip_yaml_save() — comment-preserving full-state writes.

This helper backs tui_gateway/server.py:_save_cfg(), which used to call
yaml.safe_dump and silently clobber user-edited config files on every
TUI/gateway setting change (e.g. /personality, /reasoning, /details_mode).
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


class TestAtomicRoundtripYamlSave:
    @pytest.fixture
    def config_path(self, tmp_path):
        return tmp_path / "config.yaml"

    def test_creates_file_when_missing(self, config_path):
        from utils import atomic_roundtrip_yaml_save

        atomic_roundtrip_yaml_save(config_path, {"model": {"default": "test-model"}})

        assert config_path.exists()
        assert yaml.safe_load(config_path.read_text())["model"]["default"] == "test-model"

    def test_preserves_top_level_key_order(self, config_path):
        """Existing top-level keys keep their author-intended ordering."""
        config_path.write_text(
            "model:\n"
            "  default: claude-opus-4-7\n"
            "providers: {}\n"
            "agent:\n"
            "  max_turns: 90\n"
            "display:\n"
            "  skin: default\n",
            encoding="utf-8",
        )

        from utils import atomic_roundtrip_yaml_save

        # Pass keys in alphabetical order to make sure dict iteration order
        # in the caller doesn't accidentally rewrite the file alphabetically
        # (the old yaml.safe_dump bug).
        atomic_roundtrip_yaml_save(
            config_path,
            {
                "agent": {"max_turns": 100},
                "display": {"skin": "mono"},
                "model": {"default": "claude-opus-4-7"},
                "providers": {},
            },
        )

        text = config_path.read_text(encoding="utf-8")
        top_keys = [
            line.split(":", 1)[0]
            for line in text.splitlines()
            if line and not line.startswith(" ") and not line.startswith("#")
        ]
        # Comments are stripped from `top_keys` above, so the surviving
        # order should match the original file, NOT alphabetical.
        assert top_keys == ["model", "providers", "agent", "display"]

    def test_preserves_comments(self, config_path):
        config_path.write_text(
            "# header comment\n"
            "model:\n"
            "  # inline note\n"
            "  default: claude-opus-4-7\n"
            "display:\n"
            "  skin: default  # trailing note\n",
            encoding="utf-8",
        )

        from utils import atomic_roundtrip_yaml_save

        atomic_roundtrip_yaml_save(
            config_path,
            {
                "model": {"default": "claude-opus-4-7"},
                "display": {"skin": "mono"},
            },
        )

        text = config_path.read_text(encoding="utf-8")
        assert "# header comment" in text
        assert "# inline note" in text
        assert "# trailing note" in text
        assert yaml.safe_load(text)["display"]["skin"] == "mono"

    def test_preserves_readable_unicode(self, config_path):
        """Personalities with kaomoji/Chinese characters stay readable on disk
        instead of getting mangled to \\uXXXX escapes (the headline bug:
        kawaii/catgirl personality emoji turning into \\u30CE\\uFF65)."""
        config_path.write_text(
            "agent:\n"
            "  personalities:\n"
            "    catgirl: \"nya (=^･ω･^=) 你好\"\n"
            "display:\n"
            "  skin: default\n",
            encoding="utf-8",
        )

        from utils import atomic_roundtrip_yaml_save

        atomic_roundtrip_yaml_save(
            config_path,
            {
                "agent": {"personalities": {"catgirl": "nya (=^･ω･^=) 你好"}},
                "display": {"skin": "mono"},
            },
        )

        text = config_path.read_text(encoding="utf-8")
        assert "你好" in text
        assert "(=^･ω･^=)" in text
        assert "\\u4f60" not in text
        assert "\\u30CE" not in text

    def test_appends_new_keys(self, config_path):
        config_path.write_text(
            "model:\n"
            "  default: test-model\n",
            encoding="utf-8",
        )

        from utils import atomic_roundtrip_yaml_save

        atomic_roundtrip_yaml_save(
            config_path,
            {
                "model": {"default": "test-model"},
                "display": {"personality": "noir"},
                "agent": {"system_prompt": "you are noir"},
            },
        )

        result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert result["model"]["default"] == "test-model"
        assert result["display"]["personality"] == "noir"
        assert result["agent"]["system_prompt"] == "you are noir"

    def test_deletes_keys_missing_from_new_state(self, config_path):
        """Mirrors the cfg.pop()-then-_save_cfg(cfg) pattern in tui_gateway:
        e.g. /prompt clear removes custom_prompt entirely."""
        config_path.write_text(
            "model:\n"
            "  default: test-model\n"
            "custom_prompt: 'old prompt'\n",
            encoding="utf-8",
        )

        from utils import atomic_roundtrip_yaml_save

        atomic_roundtrip_yaml_save(
            config_path,
            {"model": {"default": "test-model"}},
        )

        result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert "custom_prompt" not in result
        assert result["model"]["default"] == "test-model"

    def test_overwrites_scalar_value(self, config_path):
        config_path.write_text(
            "display:\n"
            "  personality: noir\n",
            encoding="utf-8",
        )

        from utils import atomic_roundtrip_yaml_save

        atomic_roundtrip_yaml_save(
            config_path,
            {"display": {"personality": "kawaii"}},
        )

        result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert result["display"]["personality"] == "kawaii"

    def test_overwrites_list_wholesale(self, config_path):
        config_path.write_text(
            "toolsets:\n"
            "  - one\n"
            "  - two\n",
            encoding="utf-8",
        )

        from utils import atomic_roundtrip_yaml_save

        atomic_roundtrip_yaml_save(config_path, {"toolsets": ["three"]})

        result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert result["toolsets"] == ["three"]

    def test_recurses_into_nested_dicts(self, config_path):
        """Deep mutations target the matching subtree, not the whole parent.

        Without this, writing display.personality would drop sibling display.skin.
        """
        config_path.write_text(
            "display:\n"
            "  skin: default\n"
            "  personality: noir\n"
            "  compact: false\n",
            encoding="utf-8",
        )

        from utils import atomic_roundtrip_yaml_save

        atomic_roundtrip_yaml_save(
            config_path,
            {
                "display": {
                    "skin": "default",
                    "personality": "kawaii",
                    "compact": False,
                }
            },
        )

        result = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert result["display"]["skin"] == "default"
        assert result["display"]["personality"] == "kawaii"
        assert result["display"]["compact"] is False

    @staticmethod
    def _deny_config_reads(config_path):
        real_open = open

        def fake_open(file, mode="r", *args, **kwargs):
            if Path(file) == config_path and "r" in mode:
                raise PermissionError("denied")
            return real_open(file, mode, *args, **kwargs)

        return fake_open

    def test_refuses_to_overwrite_unreadable_existing_config(self, config_path):
        """Shares the fail-closed contract with hermes_cli.config.atomic_config_write:
        an existing-but-unreadable config.yaml (permission error, broken mount)
        must raise rather than being silently replaced with only new_state."""
        original = "model:\n  default: test-model\n"
        config_path.write_text(original, encoding="utf-8")

        from utils import atomic_roundtrip_yaml_save

        with patch("builtins.open", side_effect=self._deny_config_reads(config_path)):
            with pytest.raises(RuntimeError, match="Refusing to overwrite"):
                atomic_roundtrip_yaml_save(config_path, {"model": {"default": "replacement"}})

        assert config_path.read_text(encoding="utf-8") == original

    def test_restores_owner(self, config_path, monkeypatch):
        """Mirrors atomic_roundtrip_yaml_update_restores_owner — the write path
        must preserve the config file's original owner across the temp-file +
        atomic-replace swap, not just its mode."""
        if os.name != "posix":
            pytest.skip("POSIX-only")

        config_path.write_text("model:\n  default: test-model\n", encoding="utf-8")

        chown_calls: list[tuple[Path, int, int]] = []
        monkeypatch.setattr("utils._preserve_file_owner", lambda _path: (345, 678))
        monkeypatch.setattr(
            "utils.os.chown",
            lambda path, uid, gid: chown_calls.append((Path(path), uid, gid)),
        )

        from utils import atomic_roundtrip_yaml_save

        atomic_roundtrip_yaml_save(config_path, {"model": {"default": "updated-model"}})

        assert chown_calls == [(config_path, 345, 678)]

    def test_extra_content_is_idempotent_across_repeated_saves(self, config_path):
        """A trailing extra_content block must not duplicate itself.

        Regression: ruamel's round-trip loader treats a comment block
        appended after the document as part of the loaded structure, so it
        survives the *next* load and gets re-emitted by yaml_rt.dump() on
        its own. An unconditional re-append on every save therefore
        duplicated the block on every single subsequent save_config() call
        (observed: 3 saves -> the block emitted 3 times, file growing by a
        full copy each time).
        """
        from utils import atomic_roundtrip_yaml_save

        blocks = [
            "\n# ── Security ──────────\n# some comment\n# security:\n#   redact_secrets: true\n",
            "\n# ── Fallback Model ──────────\n# another comment\n",
        ]

        sizes = []
        for _ in range(3):
            atomic_roundtrip_yaml_save(
                config_path, {"model": {"provider": "openrouter"}}, extra_content=blocks
            )
            sizes.append(config_path.stat().st_size)

        assert sizes[0] == sizes[1] == sizes[2], sizes
        text = config_path.read_text(encoding="utf-8")
        assert text.count("── Security ──") == 1
        assert text.count("── Fallback Model ──") == 1

    def test_unchanged_list_keeps_its_comments(self, config_path):
        """A list value that ISN'T changing must not lose its comments.

        Regression: `_merge` used to reassign every non-dict value
        unconditionally, even when the new value was identical to what was
        already on disk — replacing the ruamel CommentedSeq wholesale (and
        with it, every inline per-item comment and any trailing comment
        after the last item) purely as a side effect of *some other* key
        changing in the same save, with the list itself untouched.
        """
        original = (
            "toolsets:\n"
            "  telegram:\n"
            "    - terminal          # run commands in the sandbox\n"
            "    - search            # web search only\n"
            "    # trailing note about the list as a whole\n"
            "model:\n"
            "  provider: old-provider\n"
        )
        config_path.write_text(original, encoding="utf-8")

        from utils import atomic_roundtrip_yaml_save

        # Same toolsets list, byte-for-byte — only `model.provider` changes.
        atomic_roundtrip_yaml_save(
            config_path,
            {
                "toolsets": {"telegram": ["terminal", "search"]},
                "model": {"provider": "new-provider"},
            },
        )

        text = config_path.read_text(encoding="utf-8")
        assert "# run commands in the sandbox" in text
        assert "# web search only" in text
        assert "# trailing note about the list as a whole" in text
        assert yaml.safe_load(text)["model"]["provider"] == "new-provider"
