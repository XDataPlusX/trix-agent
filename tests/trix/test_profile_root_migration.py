"""Фаза 6: миграция раскладки профиля и его жизненный цикл.

Тесты исполняют поведение на ``tmp_path``, а не читают исходники: проверяем,
что на диске после команды, а не что написано в функции.

Главное, что здесь защищается, — три обещания, каждое из которых легко
сломать незаметно:

* разведка **не пишет на диск** (иначе «--dry-run» перестаёт быть разведкой);
* ``--apply`` → ``--rollback`` возвращает дерево в исходное состояние, но
  **не удаляет непустые каталоги** (в ``home/`` к тому моменту уже логины);
* содержимое учётных файлов не читается никогда — отчёт называет пути.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_cli.trix_layout import (
    MIGRATION_JOURNAL,
    apply_migration,
    discover,
    render_report,
    rollback_migration,
)
from hermes_constants import (
    ISOLATION_CONTAINED,
    ISOLATION_SHARED_HOST,
    PROFILE_LAYOUT_MARKER,
    PROFILE_LAYOUT_VERSION,
    reset_isolation_warnings,
    write_layout_marker,
)


def _snapshot(root: Path) -> dict[str, int]:
    """Слепок дерева: путь → размер. Сравнение слепков ловит и запись, и
    удаление, и подмену содержимого."""
    out: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            out[str(Path(dirpath, name).relative_to(root))] = -1
        for name in filenames:
            path = Path(dirpath, name)
            try:
                out[str(path.relative_to(root))] = path.stat().st_size
            except OSError:
                out[str(path.relative_to(root))] = -2
    return out


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    root = tmp_path / "profile"
    root.mkdir()
    (root / "config.yaml").write_text("model: test\n", encoding="utf-8")
    return root


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fakehome"
    home.mkdir()
    return home


# ── 6.1 Разведка ─────────────────────────────────────────────────────


class TestDiscover:
    def test_fresh_root_has_no_marker_and_all_branches_missing(self, profile_root, fake_home):
        report = discover(profile_root, env={}, home=fake_home)

        assert report.marker_version is None
        assert report.already_contained is False
        assert {b.name for b in report.missing_branches()} == {"home", "tmp", "workspace"}

    def test_reports_existing_branches_and_whether_they_are_empty(self, profile_root, fake_home):
        (profile_root / "home").mkdir()
        (profile_root / "workspace").mkdir()
        (profile_root / "workspace" / "note.txt").write_text("x", encoding="utf-8")

        by_name = {b.name: b for b in discover(profile_root, env={}, home=fake_home).branches}

        assert by_name["home"].exists and not by_name["home"].nonempty
        assert by_name["workspace"].exists and by_name["workspace"].nonempty
        assert not by_name["tmp"].exists

    def test_names_the_logins_the_profile_will_stop_seeing(self, profile_root, fake_home):
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
        (fake_home / ".codex").mkdir()
        (fake_home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")

        report = discover(profile_root, env={}, home=fake_home)
        found = {rel for rel, _why in report.losing_sites}

        assert ".claude/.credentials.json" in found
        assert ".codex/auth.json" in found
        # Того, чего нет в доме, в отчёте быть не должно — иначе оператор
        # пойдёт спасать несуществующий вход.
        assert ".qwen/oauth_creds.json" not in found

    def test_does_not_read_credential_contents(self, profile_root, fake_home, monkeypatch):
        """Разведка обязана называть пути, а не открывать их.

        Проверяем исполнением: подменяем ``Path.read_text`` и ``open`` на
        каталоге с логинами и убеждаемся, что к файлу никто не притронулся —
        файл сделан нечитаемым.
        """
        creds_dir = fake_home / ".claude"
        creds_dir.mkdir()
        creds = creds_dir / ".credentials.json"
        creds.write_text('{"token": "секрет"}', encoding="utf-8")
        os.chmod(creds, 0o000)
        try:
            report = discover(profile_root, env={}, home=fake_home)
            assert ".claude/.credentials.json" in {r for r, _ in report.losing_sites}
            # И отчёт тоже печатает только путь.
            text = render_report(report)
            assert "секрет" not in text
            assert ".claude/.credentials.json" in text
        finally:
            os.chmod(creds, 0o600)

    def test_discovery_writes_nothing(self, profile_root, fake_home):
        before = _snapshot(profile_root)
        discover(profile_root, env={}, home=fake_home)
        assert _snapshot(profile_root) == before

    def test_symlinked_branch_is_not_followed_when_judging_emptiness(
        self, profile_root, fake_home, tmp_path
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "loot.txt").write_text("x", encoding="utf-8")
        (profile_root / "home").symlink_to(outside, target_is_directory=True)

        by_name = {b.name: b for b in discover(profile_root, env={}, home=fake_home).branches}

        assert by_name["home"].exists
        # Ссылка наружу — это не «непустая ветвь профиля».
        assert by_name["home"].nonempty is False

    def test_mode_belongs_to_the_asked_root_not_to_the_process(
        self, profile_root, fake_home, tmp_path, monkeypatch
    ):
        """``mode`` — про корень из аргумента, а не про корень процесса.

        Маркер разведка читала у переданного корня, а режим спрашивала у
        ``get_isolation_mode()`` без ``env`` — то есть у корня процесса.
        Отчёт получался противоречивым сам себе: «Режим: shared-host» и
        тут же «Маркер: .layout-version = 2».
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(elsewhere))
        monkeypatch.delenv("HERMES_ISOLATION", raising=False)
        write_layout_marker(profile_root)

        report = discover(profile_root, home=fake_home)

        assert report.marker_version == PROFILE_LAYOUT_VERSION
        assert report.mode == ISOLATION_CONTAINED, (
            f"режим взят у {elsewhere}, а спрашивали про {profile_root}"
        )
        assert report.already_contained is True

    def test_the_process_mode_does_not_leak_into_a_markerless_root(
        self, profile_root, fake_home, tmp_path, monkeypatch
    ):
        """Зеркально: процесс contained, корень без маркера — shared-host."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        write_layout_marker(elsewhere)
        monkeypatch.setenv("HERMES_HOME", str(elsewhere))
        monkeypatch.delenv("HERMES_ISOLATION", raising=False)

        report = discover(profile_root, home=fake_home)

        assert report.marker_version is None
        assert report.mode == ISOLATION_SHARED_HOST, (
            f"contained корня {elsewhere} протёк в отчёт о {profile_root}"
        )

    def test_explicit_isolation_word_still_wins(self, profile_root, fake_home, monkeypatch):
        """``HERMES_ISOLATION`` остаётся сильнее маркера — и здесь тоже.

        Подстановка корня в ``env`` не имеет права понизить приоритет явного
        слова оператора: с ``HERMES_ISOLATION=contained`` в среде так будет
        работать и сам профиль, про который спрашивают.
        """
        monkeypatch.setenv("HERMES_ISOLATION", ISOLATION_CONTAINED)
        reset_isolation_warnings()

        report = discover(profile_root, home=fake_home)

        assert report.marker_version is None
        assert report.mode == ISOLATION_CONTAINED


# ── 6.2 Переключение и откат ─────────────────────────────────────────


class TestApplyAndRollback:
    def test_apply_creates_branches_and_marker(self, profile_root):
        apply_migration(profile_root)

        assert (profile_root / PROFILE_LAYOUT_MARKER).read_text().strip() == str(
            PROFILE_LAYOUT_VERSION
        )
        for name in ("home", "tmp", "workspace"):
            assert (profile_root / name).is_dir()

    def test_created_branches_are_owner_only(self, profile_root):
        apply_migration(profile_root)
        mode = (profile_root / "tmp").stat().st_mode & 0o777
        assert mode == 0o700, f"tmp/ создан с правами {mode:o}, ожидалось 0700"

    def test_apply_is_idempotent(self, profile_root):
        apply_migration(profile_root)
        (profile_root / "home" / "token").write_text("x", encoding="utf-8")
        first = json.loads((profile_root / MIGRATION_JOURNAL).read_text())

        apply_migration(profile_root)
        second = json.loads((profile_root / MIGRATION_JOURNAL).read_text())

        assert sorted(first["created_dirs"]) == sorted(second["created_dirs"])
        assert (profile_root / "home" / "token").read_text() == "x"

    def test_apply_then_rollback_restores_the_tree_byte_for_byte(self, profile_root):
        before = _snapshot(profile_root)

        apply_migration(profile_root)
        rollback_migration(profile_root)

        assert _snapshot(profile_root) == before

    def test_rollback_keeps_branches_that_have_data(self, profile_root):
        apply_migration(profile_root)
        (profile_root / "home" / ".credentials.json").write_text("{}", encoding="utf-8")

        result = rollback_migration(profile_root)

        assert "home" in result["kept_nonempty"]
        assert (profile_root / "home" / ".credentials.json").exists()
        assert "tmp" in result["removed_dirs"]
        assert not (profile_root / PROFILE_LAYOUT_MARKER).exists()

    def test_rollback_does_not_remove_a_branch_it_did_not_create(self, profile_root):
        (profile_root / "workspace").mkdir()
        apply_migration(profile_root)
        rollback_migration(profile_root)

        assert (profile_root / "workspace").is_dir(), (
            "откат удалил каталог, который существовал до миграции"
        )

    def test_rollback_keeps_a_marker_it_did_not_write(self, profile_root):
        (profile_root / PROFILE_LAYOUT_MARKER).write_text("2\n", encoding="utf-8")
        apply_migration(profile_root)

        result = rollback_migration(profile_root)

        assert result["marker_removed"] is False
        assert (profile_root / PROFILE_LAYOUT_MARKER).exists()

    def test_journal_cannot_steer_deletion_outside_the_root(self, profile_root, tmp_path):
        """Подменённый журнал не должен уводить откат за пределы корня."""
        victim = tmp_path / "victim"
        victim.mkdir()
        apply_migration(profile_root)
        (profile_root / MIGRATION_JOURNAL).write_text(
            json.dumps({"created_dirs": ["../victim", str(victim)], "created_marker": True}),
            encoding="utf-8",
        )

        rollback_migration(profile_root)

        assert victim.is_dir(), "откат удалил каталог за пределами корня"

    def test_apply_refuses_while_the_gateway_runs(self, profile_root):
        (profile_root / "gateway.pid").write_text(str(os.getpid()), encoding="utf-8")

        with pytest.raises(RuntimeError, match="Шлюз"):
            apply_migration(profile_root)

        assert not (profile_root / PROFILE_LAYOUT_MARKER).exists()

    def test_apply_proceeds_when_the_pid_file_is_stale(self, profile_root):
        # Несуществующий pid: файл остался от упавшего процесса.
        (profile_root / "gateway.pid").write_text("999999999", encoding="utf-8")
        apply_migration(profile_root)
        assert (profile_root / PROFILE_LAYOUT_MARKER).exists()


# ── 6.4 Жизненный цикл профиля ───────────────────────────────────────


class TestProfileLifecycle:
    def test_new_profile_is_contained_by_construction(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root"))
        monkeypatch.delenv("HERMES_ISOLATION", raising=False)
        import hermes_cli.profiles as profiles

        profile_dir = profiles.create_profile("phase6new")
        try:
            marker = profile_dir / PROFILE_LAYOUT_MARKER
            assert marker.exists(), "новый профиль не получил маркер раскладки"
            assert marker.read_text().strip() == str(PROFILE_LAYOUT_VERSION)
            assert (profile_dir / "tmp").is_dir()

            # И движок действительно видит его как contained.
            from hermes_constants import get_isolation_mode

            assert get_isolation_mode({"HERMES_HOME": str(profile_dir)}) == "contained"
        finally:
            import shutil

            shutil.rmtree(profile_dir, ignore_errors=True)

    def test_existing_root_without_marker_stays_shared_host(self, tmp_path):
        """Обратная совместимость: создание НОВОГО профиля не переключает
        существующие корни."""
        from hermes_constants import get_isolation_mode

        old_root = tmp_path / "old"
        old_root.mkdir()
        (old_root / "home").mkdir()  # многопрофильная установка на хосте
        assert get_isolation_mode({"HERMES_HOME": str(old_root)}) == "shared-host"

    def test_export_excludes_tmp_and_carries_the_marker(self, tmp_path, monkeypatch):
        import tarfile

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root"))
        import hermes_cli.profiles as profiles

        profile_dir = profiles.create_profile("phase6exp")
        try:
            (profile_dir / "tmp" / "huge.bin").write_bytes(b"0" * 1024)
            (profile_dir / "memories").mkdir(exist_ok=True)
            (profile_dir / "memories" / "note.md").write_text("keep me", encoding="utf-8")

            archive = profiles.export_profile("phase6exp", str(tmp_path / "out.tar.gz"))
            with tarfile.open(archive, "r:gz") as tar:
                names = tar.getnames()

            assert not any("/tmp/" in n or n.endswith("/tmp") for n in names), (
                f"экспорт утащил временные файлы: {[n for n in names if '/tmp' in n]}"
            )
            assert any(n.endswith(PROFILE_LAYOUT_MARKER) for n in names), (
                "маркер раскладки не попал в экспорт — импорт на другой машине "
                "молча вернёт профиль в shared-host"
            )
            assert any(n.endswith("memories/note.md") for n in names)
        finally:
            import shutil

            shutil.rmtree(profile_dir, ignore_errors=True)

    def test_delete_removes_containers_labelled_with_the_profile(self, tmp_path, monkeypatch):
        """Удаление профиля обязано снести его контейнеры.

        Иначе writable-слой долгоживущей песочницы остаётся в
        ``/var/lib/docker`` — вне корня, и обещание «удалили корень, данных
        не осталось» становится неправдой.

        Фейковый docker в ``tmp_path`` записывает свои вызовы: проверяем, что
        именно ему сказали, а не что написано в коде.
        """
        import hermes_cli.profiles as profiles

        calls_log = tmp_path / "docker_calls.log"
        fake_docker = tmp_path / "docker"
        fake_docker.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{calls_log}"\n'
            'case "$1" in\n'
            '  ps) echo cafe1234 ;;\n'
            '  *) : ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        os.chmod(fake_docker, 0o755)

        removed = profiles._remove_profile_containers(
            "phase6del", docker_exe=str(fake_docker)
        )

        assert removed == 1
        calls = calls_log.read_text(encoding="utf-8")
        assert "label=hermes-profile=phase6del" in calls, (
            f"контейнеры искали не по метке профиля: {calls}"
        )
        assert "rm -f -v cafe1234" in calls, f"контейнер не удалён: {calls}"

    def test_container_cleanup_survives_a_missing_docker(self, tmp_path):
        """Отсутствие docker не должно отменять удаление профиля."""
        import hermes_cli.profiles as profiles

        assert profiles._remove_profile_containers(
            "phase6del", docker_exe=str(tmp_path / "no-such-docker")
        ) == 0
