"""Раскладка профиля обязана отказывать, а не догадываться.

Два места, где изоляция молча отменялась:

* ветвь-симлинк засчитывалась как «ветвь существует». ``<root>/home ->
  /куда-нибудь/наружу`` — миграция довольна, отчёт пишет «есть, пустая», а
  логины подпроцессов пишутся за корень;
* маркер ``.layout-version`` писался одним ``write_text()``. Обрыв записи
  оставлял пустой файл, читатель возвращал ``None``, и профиль тихо
  переключался обратно в shared-host — то есть в общий дом ОС.

Обе ветки чинятся в одну сторону: **fail-closed**. Неизвестное состояние
корня — это отказ или contained, но никогда не «наверное, общий дом».
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import hermes_constants
from hermes_cli.trix_layout import (
    MIGRATION_JOURNAL,
    apply_migration,
    discover,
    render_report,
)
from hermes_constants import PROFILE_LAYOUT_MARKER, PROFILE_LAYOUT_VERSION

BRANCHES = ("home", "tmp", "workspace")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("model: test\n", encoding="utf-8")
    return profile


@pytest.fixture
def isolated_env(monkeypatch, root: Path, tmp_path: Path) -> Path:
    real_home = tmp_path / "realhome"
    real_home.mkdir()
    monkeypatch.delenv("HERMES_ISOLATION", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(real_home))
    hermes_constants.reset_isolation_warnings()
    return root


# ── Симлинк-ветвь ───────────────────────────────────────────────────────

@pytest.mark.parametrize("branch", BRANCHES)
def test_discover_marks_a_symlinked_branch(root: Path, tmp_path: Path, branch: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / branch).symlink_to(outside, target_is_directory=True)

    report = discover(root, home=tmp_path / "nohome")
    state = next(b for b in report.branches if b.name == branch)

    assert state.is_symlink is True
    assert report.symlinked_branches() == [state]


@pytest.mark.parametrize("branch", BRANCHES)
def test_report_names_the_symlink_instead_of_calling_it_empty(
    root: Path, tmp_path: Path, branch: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / branch).symlink_to(outside, target_is_directory=True)

    text = render_report(discover(root, home=tmp_path / "nohome"))

    assert "есть, пустая" not in text
    assert "симлинк" in text.lower()
    assert str(outside) in text


@pytest.mark.parametrize("branch", BRANCHES)
def test_apply_refuses_a_branch_that_points_outside(
    root: Path, tmp_path: Path, branch: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / branch).symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError) as excinfo:
        apply_migration(root)

    assert branch in str(excinfo.value)
    assert not (root / PROFILE_LAYOUT_MARKER).exists()
    assert not (root / MIGRATION_JOURNAL).exists()


def test_apply_refuses_a_symlink_that_points_inside_too(root: Path) -> None:
    """Ссылка внутрь корня — тоже отказ.

    «Внутрь» проверяется на момент миграции, а цель ссылки может переехать
    в любой момент после. Разбирать симлинки на классы — это заводить
    правило, которое нечем проверить; проще их не иметь.
    """
    (root / "real-tmp").mkdir()
    (root / "tmp").symlink_to(root / "real-tmp", target_is_directory=True)

    with pytest.raises(RuntimeError):
        apply_migration(root)


def test_apply_reports_every_symlinked_branch_at_once(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    for branch in BRANCHES:
        (root / branch).symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError) as excinfo:
        apply_migration(root)

    message = str(excinfo.value)
    for branch in BRANCHES:
        assert branch in message


def test_apply_still_works_on_an_ordinary_root(root: Path) -> None:
    journal = apply_migration(root)

    assert sorted(journal["created_dirs"]) == sorted(BRANCHES)
    assert (root / PROFILE_LAYOUT_MARKER).read_text(encoding="utf-8").strip() == str(
        PROFILE_LAYOUT_VERSION
    )


def test_apply_accepts_branches_that_already_exist_as_dirs(root: Path) -> None:
    for branch in BRANCHES:
        (root / branch).mkdir()

    journal = apply_migration(root)

    assert journal["created_dirs"] == []


# ── Маркер: атомарность и повреждение ───────────────────────────────────

def test_marker_write_leaves_no_debris(root: Path) -> None:
    apply_migration(root)

    stray = [p.name for p in root.iterdir() if p.name.startswith(PROFILE_LAYOUT_MARKER + ".")]
    assert stray == [], stray


def test_failed_marker_write_keeps_the_previous_marker(root: Path, monkeypatch) -> None:
    """Обрыв записи не имеет права оставить корень без валидного маркера."""
    marker = root / PROFILE_LAYOUT_MARKER
    marker.write_text(f"{PROFILE_LAYOUT_VERSION}\n", encoding="utf-8")
    for branch in BRANCHES:
        (root / branch).mkdir()

    real_replace = os.replace

    def _boom(src, dst, *args, **kwargs):
        if str(dst).endswith(PROFILE_LAYOUT_MARKER):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", _boom)

    with pytest.raises(OSError):
        apply_migration(root)

    assert marker.read_text(encoding="utf-8").strip() == str(PROFILE_LAYOUT_VERSION)
    stray = [p.name for p in root.iterdir() if p.name.startswith(PROFILE_LAYOUT_MARKER + ".")]
    assert stray == [], stray


@pytest.mark.parametrize("content", ["", "\n", "   ", "\x00\x00", "мусор"])
def test_broken_marker_does_not_fall_back_to_the_shared_home(
    isolated_env: Path, content: str, capsys
) -> None:
    """Повреждённый маркер — отказ в сторону contained, а не в сторону дома ОС.

    Оборванная запись раньше давала пустой файл → ``None`` → shared-host.
    Это fail-open ровно в ту сторону, которой тут быть не должно: профиль
    начинал писать логины в общий дом, никому об этом не сказав.
    """
    (isolated_env / PROFILE_LAYOUT_MARKER).write_text(content, encoding="utf-8")

    assert hermes_constants.is_contained() is True
    assert PROFILE_LAYOUT_MARKER in capsys.readouterr().err


def test_marker_of_a_newer_version_stays_contained(isolated_env: Path) -> None:
    """Маркер из будущего — тоже contained: новее ≠ «общий дом»."""
    (isolated_env / PROFILE_LAYOUT_MARKER).write_text(
        f"{PROFILE_LAYOUT_VERSION + 7}\n", encoding="utf-8"
    )

    assert hermes_constants.is_contained() is True


def test_marker_of_the_old_layout_is_shared_host(isolated_env: Path) -> None:
    """Версия 1 — это и есть старая раскладка, она объявлена явно."""
    (isolated_env / PROFILE_LAYOUT_MARKER).write_text("1\n", encoding="utf-8")

    assert hermes_constants.is_contained() is False


def test_no_marker_is_still_shared_host(isolated_env: Path) -> None:
    """Главный инвариант совместимости: нет файла — нет изменений."""
    assert hermes_constants.is_contained() is False


def test_unreadable_marker_does_not_fall_back_either(isolated_env: Path) -> None:
    """Маркер есть, но прочитать нельзя — всё равно не shared-host."""
    marker = isolated_env / PROFILE_LAYOUT_MARKER
    marker.mkdir()  # каталог вместо файла: read() упадёт с OSError

    assert hermes_constants.is_contained() is True
