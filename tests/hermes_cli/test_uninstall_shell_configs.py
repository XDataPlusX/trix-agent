"""Tests for ``remove_path_from_shell_configs`` — the uninstaller's shell-rc rewrite.

This rewrites files Hermes/Trix Agent does not own (``~/.bashrc``,
``~/.zshrc``, ...) and takes no backup of them, so the rewrite has to be both
(a) atomic -- a bare ``write_text()`` truncates the rc file before the new
content lands, and the caller wraps everything in
``except Exception: log_warn(...)``, so a partial write is downgraded to a
warning and the user's next login starts a bare shell -- and (b) precise: an
earlier version matched any line containing the substring 'hermes'
(case-insensitive) together with 'PATH=' or a '#'. That deleted a customer's
own ``export MY_HERMES_PATH=...``, ``export MYPATH=/opt/hermes-tools/bin``,
and (after the Trix rebrand made the product's own name collide with the
same heuristic) even a comment reading
``# Trix Agent notes: renew the license`` -- plus it collapsed every
blank-line run in the file down to one blank line, rewriting formatting that
was never the installer's to touch. The function now removes only the exact
comment+``export PATH=...`` pair this codebase itself writes, and nothing
else in the file is touched.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from hermes_cli import uninstall


# The exact two-line pairs scripts/install.sh, hermes_cli/update_cmd.py, and
# setup-hermes.sh write -- one per branding, for the common ~/.local/bin
# case. Keep these in sync with hermes_cli.uninstall._MANAGED_PATH_COMMENTS /
# _MANAGED_PATH_LINES.
OLD_BLOCK = (
    "# Hermes Agent — ensure ~/.local/bin is on PATH\n"
    'export PATH="$HOME/.local/bin:$PATH"\n'
)
NEW_BLOCK = (
    "# Trix Agent — ensure ~/.local/bin is on PATH\n"
    'export PATH="$HOME/.local/bin:$PATH"\n'
)
# The RHEL non-login-shell variant (a different comment text and a different
# PATH line -- /usr/local/bin, not ~/.local/bin).
RHEL_OLD_BLOCK = (
    "# Hermes Agent — ensure /usr/local/bin is on PATH (RHEL non-login shells)\n"
    'export PATH="/usr/local/bin:$PATH"\n'
)
RHEL_NEW_BLOCK = (
    "# Trix Agent — ensure /usr/local/bin is on PATH (RHEL non-login shells)\n"
    'export PATH="/usr/local/bin:$PATH"\n'
)

ZSHRC = (
    "export EDITOR=vim\n"
    "alias ll='ls -la'\n"
    "\n"
    + OLD_BLOCK
    + "\n"
    "source ~/.work-profile\n"
)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point both ``Path.home()`` and ``HERMES_HOME`` at a throwaway dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    return home


class TestHappyPath:
    def test_old_branded_rc_is_fully_cleaned(self, fake_home: Path):
        """An install from before the rebrand: nothing orphaned."""
        rc = fake_home / ".zshrc"
        rc.write_text(ZSHRC, encoding="utf-8")

        removed = uninstall.remove_path_from_shell_configs()

        assert removed == [rc]
        text = rc.read_text(encoding="utf-8")
        assert "Hermes Agent" not in text
        assert 'export PATH="$HOME/.local/bin:$PATH"' not in text
        # The user's own lines are untouched.
        assert "export EDITOR=vim" in text
        assert "alias ll='ls -la'" in text
        assert "source ~/.work-profile" in text

    def test_new_branded_rc_is_fully_cleaned(self, fake_home: Path):
        """An install from after the rebrand: same result, new spelling."""
        rc = fake_home / ".zshrc"
        rc.write_text(ZSHRC.replace(OLD_BLOCK, NEW_BLOCK), encoding="utf-8")

        removed = uninstall.remove_path_from_shell_configs()

        assert removed == [rc]
        text = rc.read_text(encoding="utf-8")
        assert "Trix Agent" not in text
        assert 'export PATH="$HOME/.local/bin:$PATH"' not in text
        assert "export EDITOR=vim" in text
        assert "source ~/.work-profile" in text

    def test_mixed_old_and_new_block_rc_is_fully_cleaned(self, fake_home: Path):
        """A customer who installed old, upgraded, then uninstalls.

        Both the pre-upgrade RHEL-variant block (still whatever an old
        install left behind) and the post-upgrade ~/.local/bin block must be
        recognized and removed -- a customer's rc can legitimately carry
        both if they were on the RHEL non-login-shell path once and the
        ~/.local/bin path on a later run.
        """
        rc = fake_home / ".zshrc"
        rc.write_text(
            "export EDITOR=vim\n"
            "\n"
            + RHEL_OLD_BLOCK
            + "\n"
            + NEW_BLOCK
            + "\n"
            "source ~/.work-profile\n",
            encoding="utf-8",
        )

        removed = uninstall.remove_path_from_shell_configs()

        assert removed == [rc]
        text = rc.read_text(encoding="utf-8")
        assert "Hermes Agent" not in text
        assert "Trix Agent" not in text
        assert 'export PATH="/usr/local/bin:$PATH"' not in text
        assert 'export PATH="$HOME/.local/bin:$PATH"' not in text
        assert "export EDITOR=vim" in text
        assert "source ~/.work-profile" in text

    def test_rhel_variant_is_recognized_in_both_brandings(self, fake_home: Path):
        rc = fake_home / ".bashrc"
        rc.write_text(RHEL_OLD_BLOCK, encoding="utf-8")

        removed = uninstall.remove_path_from_shell_configs()

        assert removed == [rc]
        assert rc.read_text(encoding="utf-8") == ""

        rc.write_text(RHEL_NEW_BLOCK, encoding="utf-8")
        removed = uninstall.remove_path_from_shell_configs()
        assert removed == [rc]
        assert rc.read_text(encoding="utf-8") == ""

    def test_untouched_rc_is_not_reported(self, fake_home: Path):
        rc = fake_home / ".zshrc"
        rc.write_text("export EDITOR=vim\n", encoding="utf-8")

        assert uninstall.remove_path_from_shell_configs() == []
        assert rc.read_text(encoding="utf-8") == "export EDITOR=vim\n"


class TestCustomerDataSurvives:
    """This is a paying customer's shell configuration, not a scratch file.

    Every one of these must come back byte-for-byte identical -- the old
    substring heuristic ('hermes' in line.lower() and 'PATH' in line, or a
    bare '#'-comment scan) deleted every single line in this fixture.
    """

    LOOKALIKE_RC = (
        "export MY_HERMES_PATH=/opt/tools\n"
        "export MYPATH=/opt/hermes-tools/bin\n"
        "# Trix Agent notes: renew the license\n"
        "export EDITOR=vim\n"
        "\n"
        "\n"
        "\n"
        "alias ll='ls -la'\n"
        "\n"
        "source ~/.work-profile\n"
    )

    def test_lookalike_lines_and_blank_runs_survive_with_no_managed_block(
        self, fake_home: Path
    ):
        rc = fake_home / ".zshrc"
        rc.write_text(self.LOOKALIKE_RC, encoding="utf-8")

        removed = uninstall.remove_path_from_shell_configs()

        assert removed == []
        assert rc.read_text(encoding="utf-8") == self.LOOKALIKE_RC

    def test_lookalike_lines_and_blank_runs_survive_alongside_a_managed_block(
        self, fake_home: Path
    ):
        """Same decoys, but this time there IS a real block to remove --
        the decoys must survive that removal too, not just its absence."""
        rc = fake_home / ".zshrc"
        content = self.LOOKALIKE_RC + "\n" + OLD_BLOCK
        rc.write_text(content, encoding="utf-8")

        removed = uninstall.remove_path_from_shell_configs()

        assert removed == [rc]
        text = rc.read_text(encoding="utf-8")
        assert text == self.LOOKALIKE_RC + "\n"
        # Spelled out explicitly, not just via the byte-for-byte diff above.
        assert "export MY_HERMES_PATH=/opt/tools" in text
        assert "export MYPATH=/opt/hermes-tools/bin" in text
        assert "# Trix Agent notes: renew the license" in text
        assert "\n\n\n" in text, "blank-line runs must not be collapsed"

    def test_pure_user_rc_with_no_managed_block_is_byte_for_byte_unchanged(
        self, fake_home: Path
    ):
        rc = fake_home / ".profile"
        content = (
            "export GOPATH=$HOME/go\n"
            "export PATH=$GOPATH/bin:$PATH\n"
            "\n\n\n"
            "# personal notes, not ours\n"
        )
        rc.write_text(content, encoding="utf-8")

        removed = uninstall.remove_path_from_shell_configs()

        assert removed == []
        assert rc.read_text(encoding="utf-8") == content


class TestCrashDurability:
    def test_shell_config_survives_an_interrupted_rewrite(self, fake_home: Path):
        """An interrupted rewrite must leave the rc file byte-identical.

        There is no backup of the user's shell rc anywhere in this code path,
        so a truncated write is unrecoverable.
        """
        rc = fake_home / ".zshrc"
        rc.write_text(ZSHRC, encoding="utf-8")
        original = rc.read_bytes()

        def boom(fd):
            raise OSError("simulated crash mid-write")

        # Scoped context so restoring os.fsync doesn't also undo the
        # Path.home()/HERMES_HOME patches the fake_home fixture installed.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "fsync", boom)
            removed = uninstall.remove_path_from_shell_configs()

        # The write failed, so the rc must not be reported as modified...
        assert removed == []
        # ...and it must still be exactly what the user had.
        assert rc.read_bytes() == original
        # The aborted write must not leave a temp file behind in $HOME.
        assert list(fake_home.glob("*.tmp")) == []

    def test_symlinked_shell_config_stays_a_symlink(self, fake_home: Path):
        """A dotfiles-repo ``~/.zshrc`` is a symlink; replacing it with a
        regular file silently detaches the user's dotfiles."""
        dotfiles = fake_home / "dotfiles"
        dotfiles.mkdir()
        real = dotfiles / "zshrc"
        real.write_text(ZSHRC, encoding="utf-8")
        rc = fake_home / ".zshrc"
        rc.symlink_to(real)

        removed = uninstall.remove_path_from_shell_configs()

        assert removed == [rc]
        assert rc.is_symlink(), "the symlink was replaced by a regular file"
        assert "Hermes Agent" not in real.read_text(encoding="utf-8")
        assert "export EDITOR=vim" in real.read_text(encoding="utf-8")

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_existing_file_mode_is_preserved(self, fake_home: Path):
        """Shell rc files are normally 0644; uninstalling must not change that."""
        rc = fake_home / ".zshrc"
        rc.write_text(ZSHRC, encoding="utf-8")
        os.chmod(rc, 0o644)

        uninstall.remove_path_from_shell_configs()

        mode = stat.S_IMODE(rc.stat().st_mode)
        assert mode == 0o644, f"mode changed to {oct(mode)}"
