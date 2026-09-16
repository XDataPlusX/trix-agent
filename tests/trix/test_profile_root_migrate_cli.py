"""``hermes profile migrate-layout``: обещанное и сделанное — одно и то же.

Три расхождения между докстрингом, справкой и кодом:

* докстринг утверждал, что ``--apply`` требует пройденного ``--dry-run``.
  Не требовал никогда;
* ``--dry-run`` был объявлен в парсере и не читался НИГДЕ. Флаг, который
  ничего не делает, хуже отсутствующего: он обещает;
* отчёт «профиль перестанет видеть эти входы» на пути ``--apply``
  печатался ПОСЛЕ записи маркера. Предупреждение после события — это не
  предупреждение.

Чего здесь сознательно НЕТ: интерактивного подтверждения. Команда живёт в
рецептах и в чужой автоматизации; вопрос в терминал сломал бы их молча.
Явное намерение выражается флагом — этого достаточно.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli.trix_layout import MIGRATION_JOURNAL, cmd_migrate_layout
from hermes_constants import (
    ISOLATION_CONTAINED,
    ISOLATION_SHARED_HOST,
    PROFILE_LAYOUT_MARKER,
    write_layout_marker,
)


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("model: test\n", encoding="utf-8")
    real_home = tmp_path / "oshome"
    (real_home / ".claude").mkdir(parents=True)
    (real_home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.delenv("HERMES_ISOLATION", raising=False)
    return profile


def _args(**kwargs) -> Namespace:
    base = {"profile_name": None, "apply": False, "dry_run": False, "rollback": False}
    base.update(kwargs)
    return Namespace(**base)


def test_no_flags_changes_nothing(root: Path, capsys) -> None:
    assert cmd_migrate_layout(_args()) == 0

    assert not (root / PROFILE_LAYOUT_MARKER).exists()
    assert not (root / "home").exists()
    assert "разведка" in capsys.readouterr().out


def test_dry_run_is_the_explicit_form_of_the_default(root: Path, capsys) -> None:
    """Флаг обязан делать то, что обещает справка: только отчёт."""
    assert cmd_migrate_layout(_args(dry_run=True)) == 0

    out = capsys.readouterr().out
    assert not (root / PROFILE_LAYOUT_MARKER).exists()
    assert not (root / MIGRATION_JOURNAL).exists()
    assert "на диске ничего не изменено" in out


def test_dry_run_reports_what_the_profile_will_stop_seeing(root: Path, capsys) -> None:
    assert cmd_migrate_layout(_args(dry_run=True)) == 0

    out = capsys.readouterr().out
    assert "перестанет видеть" in out
    assert ".claude/.credentials.json" in out


def test_dry_run_with_apply_is_refused(root: Path, capsys) -> None:
    """Два взаимоисключающих намерения — отказ, а не молчаливый выбор одного."""
    assert cmd_migrate_layout(_args(dry_run=True, apply=True)) == 1

    assert not (root / PROFILE_LAYOUT_MARKER).exists()
    assert "--dry-run" in capsys.readouterr().out


def test_dry_run_with_rollback_is_refused(root: Path) -> None:
    assert cmd_migrate_layout(_args(dry_run=True, rollback=True)) == 1


def test_apply_reports_before_it_mutates(root: Path, monkeypatch, capsys) -> None:
    """Отчёт печатается ДО записи, а не после неё.

    Порядок здесь и есть контракт. Раньше «профиль перестанет видеть эти
    входы» приезжало после того, как маркер уже лежал на диске: в выводе
    строки стояли правильно, а на диске — нет. Предупреждение после
    события не предупреждение, поэтому проверяется порядок ДЕЙСТВИЙ, а не
    порядок строк.
    """
    import hermes_cli.trix_layout as layout

    order: list[str] = []
    real_report = layout.render_report
    real_apply = layout.apply_migration

    def _report(*args, **kwargs):
        order.append("report")
        return real_report(*args, **kwargs)

    def _apply(*args, **kwargs):
        order.append("apply")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(layout, "render_report", _report)
    monkeypatch.setattr(layout, "apply_migration", _apply)

    assert cmd_migrate_layout(_args(apply=True)) == 0

    assert "report" in order and "apply" in order, order
    assert order.index("report") < order.index("apply"), order
    out = capsys.readouterr().out
    assert "перестанет видеть" in out


def test_apply_does_not_write_when_preflight_refuses(root: Path, capsys) -> None:
    """Отказ preflight не имеет права оставить корень наполовину переключённым."""
    (root / "home").symlink_to(root.parent / "elsewhere", target_is_directory=True)

    assert cmd_migrate_layout(_args(apply=True)) == 1

    assert not (root / PROFILE_LAYOUT_MARKER).exists()
    assert not (root / MIGRATION_JOURNAL).exists()
    assert "симлинк" in capsys.readouterr().out.lower()


def test_apply_actually_applies(root: Path) -> None:
    assert cmd_migrate_layout(_args(apply=True)) == 0

    assert (root / PROFILE_LAYOUT_MARKER).exists()
    for branch in ("home", "tmp", "workspace"):
        assert (root / branch).is_dir()


def test_apply_stays_non_interactive(root: Path, monkeypatch) -> None:
    """Никаких вопросов в терминал: команда живёт в чужой автоматизации."""
    def _boom(*_args, **_kwargs):
        raise AssertionError("команда спросила подтверждение и сломала автоматизацию")

    monkeypatch.setattr("builtins.input", _boom)

    assert cmd_migrate_layout(_args(apply=True)) == 0


def test_rollback_returns_the_root(root: Path) -> None:
    assert cmd_migrate_layout(_args(apply=True)) == 0
    assert cmd_migrate_layout(_args(rollback=True)) == 0

    assert not (root / PROFILE_LAYOUT_MARKER).exists()


# ── Отчёт о ЧУЖОМ корне ──────────────────────────────────────────────
#
# `hermes profile migrate-layout <имя>` спрашивают ПРО ДРУГОЙ корень, а
# процесс при этом живёт в своём. Маркер отчёт читал у того корня, про
# который спросили, а строку «Режим сейчас» брал у корня процесса — и
# печатал отчёт, противоречащий сам себе:
#
#     Корень профиля: …/profiles/acme
#     Режим сейчас:   shared-host      <- режим процесса, не этого корня
#     Маркер:         .layout-version = 2
#
# Решения это не искажало (`apply`/`rollback` читают маркер того корня,
# который правят), но по этому отчёту оператор решает, переносить ли
# место хранения логинов. Противоречие в нём стоит дорого.


@pytest.fixture
def foreign_profile(root: Path) -> Path:
    """Второй корень рядом с корнем процесса: ``<hermes-root>/profiles/acme``.

    Фикстура ``root`` уже сделала текущий процесс хозяином СВОЕГО корня;
    этот — чужой, и команду спрашивают именно про него.
    """
    profile = root / "profiles" / "acme"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: test\n", encoding="utf-8")
    return profile


def test_report_about_another_root_names_that_roots_mode(
    root: Path, foreign_profile: Path, capsys
) -> None:
    """Процесс в shared-host, спрашиваем про contained-профиль."""
    write_layout_marker(foreign_profile)

    assert cmd_migrate_layout(_args(profile_name="acme")) == 0

    out = capsys.readouterr().out
    assert str(foreign_profile) in out
    assert f"Режим сейчас:   {ISOLATION_CONTAINED}" in out, (
        f"отчёт назвал режим корня процесса, а не корня {foreign_profile}:\n{out}"
    )


def test_report_about_another_root_is_not_contained_by_contagion(
    root: Path, foreign_profile: Path, capsys
) -> None:
    """Зеркально: процесс в contained, спрашиваем про корень без маркера."""
    write_layout_marker(root)

    assert cmd_migrate_layout(_args(profile_name="acme")) == 0

    out = capsys.readouterr().out
    assert str(foreign_profile) in out
    assert f"Режим сейчас:   {ISOLATION_SHARED_HOST}" in out, (
        f"режим корня процесса протёк в отчёт о {foreign_profile}:\n{out}"
    )
    assert "Маркер:         нет" in out


def test_apply_to_another_root_reports_its_new_mode(
    root: Path, foreign_profile: Path, capsys
) -> None:
    """После ``--apply`` отчёт о чужом корне тоже про него, а не про процесс.

    Второй вызов разведки (после записи маркера) обязан видеть корень,
    который только что переключили, — иначе итог печатается по режиму
    постороннего каталога.
    """
    assert cmd_migrate_layout(_args(profile_name="acme", apply=True)) == 0

    out = capsys.readouterr().out
    # Отчёт печатается ДО записи: там корень ещё shared-host.
    assert f"Режим сейчас:   {ISOLATION_SHARED_HOST}" in out
    assert (foreign_profile / PROFILE_LAYOUT_MARKER).exists()
    # А корень процесса остался нетронутым: правили не его.
    assert not (root / PROFILE_LAYOUT_MARKER).exists()
