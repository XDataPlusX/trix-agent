"""Плейсхолдер cwd понимается одинаково всеми, кто о нём спрашивает.

Три слова — ``.``, ``auto``, ``cwd`` — были объявлены четырьмя копиями, и
копии не совпадали: резолвер в ``hermes_constants`` сравнивал в нижнем
регистре, остальные три — как есть. ``terminal.cwd: Auto`` в config.yaml
давало два разных ответа на один вопрос: мост в ``cli.py`` считал это
названным оператором путём и уводил contained-профиль в каталог с именем
``Auto``, а резолвер — плейсхолдером и отдавал рабочую папку.

Правило теперь одно и живёт в одном месте: сравнение регистронезависимое,
пробелы по краям не считаются.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_constants
from hermes_constants import CWD_PLACEHOLDER_WORDS, is_cwd_placeholder

# Формы, которые оператор реально пишет в YAML.
PLACEHOLDER_FORMS = [".", "auto", "cwd", "Auto", "AUTO", "CWD", " auto ", "Cwd"]
REAL_PATHS = ["/srv/project", "~/work", "./relative", "autopilot", ""]


@pytest.mark.parametrize("value", PLACEHOLDER_FORMS)
def test_placeholder_forms_are_recognized(value: str) -> None:
    assert is_cwd_placeholder(value) is True


@pytest.mark.parametrize("value", REAL_PATHS)
def test_real_paths_are_not_placeholders(value: str) -> None:
    assert is_cwd_placeholder(value) is False


def test_every_copy_reads_the_same_set() -> None:
    """Четыре объявления — один источник."""
    from gateway.cwd_placeholder import CWD_PLACEHOLDERS

    assert CWD_PLACEHOLDERS is CWD_PLACEHOLDER_WORDS


@pytest.fixture
def contained_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "profile"
    root.mkdir()
    real_home = tmp_path / "oshome"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    hermes_constants.reset_isolation_warnings()
    return root


@pytest.mark.parametrize("value", PLACEHOLDER_FORMS)
def test_resolver_treats_every_form_as_the_default(
    contained_root: Path, monkeypatch, value: str
) -> None:
    monkeypatch.setenv("TERMINAL_CWD", value)

    assert hermes_constants.get_profile_workspace_dir() == contained_root / "workspace"


@pytest.mark.parametrize("value", PLACEHOLDER_FORMS)
def test_gateway_resolves_every_form_to_the_workspace(
    contained_root: Path, value: str
) -> None:
    from gateway.cwd_placeholder import (
        placeholder_home_fallback,
        resolve_placeholder_terminal_cwd,
    )

    resolved = resolve_placeholder_terminal_cwd(
        configured_cwd=value,
        terminal_backend="local",
        messaging_cwd=None,
        docker_mount_cwd_to_workspace=False,
        home_fallback=placeholder_home_fallback(),
    )

    assert resolved == str(contained_root / "workspace")


@pytest.mark.parametrize("value", PLACEHOLDER_FORMS)
def test_tui_gateway_does_not_call_a_placeholder_a_workspace(value: str) -> None:
    from tui_gateway.server import _configured_cwd_from_cfg

    assert _configured_cwd_from_cfg({"terminal": {"cwd": value}}) is None
