"""Уборка трогает только служебное. Это инвариант, а не пожелание.

Цена ошибки здесь несимметрична: присланные клиентом документы обещано
хранить вечно, рабочая папка принадлежит ему, а ни консоли, ни бэкапов у
него нет. Поэтому проверки перед удалением намеренно избыточны, и каждая
проверена здесь ПООДИНОЧКЕ: остальные в тесте отключаются подменой, чтобы
выживание файла зависело ровно от одной из них.

Тесты проверяют отношения, а не сегодняшние числа: «освобождено ровно
столько, сколько исчезло с диска», «обещанное отчётом равно
освобождённому», «ни один защищённый путь не потерял ни байта».
"""
from pathlib import Path

import pytest

import hermes_cli.trix_disk as td
from hermes_cli.trix_disk import (
    DOCKER_PRUNE_ARGV,
    build_report,
    clean,
    docker_prune,
    format_clean_result,
    is_protected,
    _size,
)
from hermes_state import DEFAULT_DB_PATH

DB_NAME = DEFAULT_DB_PATH.name


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _tree_bytes(path: Path) -> int:
    """Независимый от модуля замер дерева — эталон для сверки."""
    if path.is_symlink() or not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(
        p.stat().st_size
        for p in path.rglob("*")
        if p.is_file() and not p.is_symlink()
    )


def _symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("файловая система не поддерживает симлинки")


def _drop_dir(path: Path) -> None:
    """Убрать настоящий каталог, чтобы поставить на его место ссылку."""
    for item in sorted(path.rglob("*"), reverse=True):
        item.unlink() if item.is_file() or item.is_symlink() else item.rmdir()
    path.rmdir()


@pytest.fixture(autouse=True)
def _no_sandbox_override(monkeypatch):
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / ".hermes"
    # Клиентское — не трогать ни при каких условиях.
    _write(h / "cache" / "documents" / "договор.pdf", 5000)
    _write(h / "sandboxes" / "docker" / "default" / "workspace" / "index.html", 2000)
    _write(h / "sandboxes" / "docker" / "default" / "home" / ".cache" / "pip.whl", 6000)
    _write(h / "images" / "фото.jpg", 1500)
    _write(h / "attachments" / "письмо.eml", 1200)
    _write(h / DB_NAME, 4000)
    _write(h / f"{DB_NAME}-wal", 1000)
    # Служебное — можно убирать.
    _write(h / "backups" / "pre-update.zip", 7000)
    _write(h / "logs" / "agent.log.1", 900)
    _write(h / "logs" / "старое" / "agent.log.2", 400)
    _write(h / "cache" / "images" / "shot.png", 3000)
    _write(h / "debug-reports" / "r1.json", 800)
    # Служебное, но НЕ из списка кандидатов: сам агент и его окружение.
    _write(h / "hermes-agent" / "blob.bin", 11000)
    return h


# ---------------------------------------------------------------------------
# Файлы клиента переживают уборку
# ---------------------------------------------------------------------------


class TestClientFilesSurvive:
    def test_documents_survive_cleanup(self, home):
        before = _tree_bytes(home / "cache" / "documents")
        clean(home)
        assert (home / "cache" / "documents" / "договор.pdf").exists()
        assert _tree_bytes(home / "cache" / "documents") == before

    def test_workspace_survives_cleanup(self, home):
        workspace = home / "sandboxes" / "docker" / "default" / "workspace"
        before = _tree_bytes(workspace)
        clean(home)
        assert (workspace / "index.html").exists()
        assert _tree_bytes(workspace) == before

    def test_the_whole_sandbox_tree_survives_cleanup(self, home):
        # Внутри песочницы живёт и /root контейнера: он служебный по
        # смыслу, но лежит в чужом каталоге и принадлежит root на хосте.
        before = _tree_bytes(home / "sandboxes")
        clean(home)
        assert _tree_bytes(home / "sandboxes") == before

    def test_sessions_database_survives_cleanup(self, home):
        clean(home)
        assert (home / DB_NAME).exists()
        assert (home / f"{DB_NAME}-wal").exists()

    def test_files_the_client_sent_through_other_surfaces_survive(self, home):
        clean(home)
        assert (home / "images" / "фото.jpg").exists()
        assert (home / "attachments" / "письмо.eml").exists()

    def test_a_link_into_client_data_is_unlinked_not_followed(self, home):
        """Ссылка на документы внутри служебного каталога.

        Убирается сама ссылка; идти по ней и чистить то, куда она ведёт,
        нельзя — там присланные клиентом файлы.
        """
        link = home / "logs" / "документы"
        _symlink(link, home / "cache" / "documents")
        before = _tree_bytes(home / "cache" / "documents")
        clean(home)
        assert not link.is_symlink()
        assert (home / "cache" / "documents" / "договор.pdf").exists()
        assert _tree_bytes(home / "cache" / "documents") == before

    def test_the_agent_itself_is_not_swept_away(self, home):
        # hermes-agent/ учтён в «служебном» отчёта, но кандидатом не является:
        # «служебное» и «удаляемое» — разные множества.
        before = _tree_bytes(home / "hermes-agent")
        clean(home)
        assert _tree_bytes(home / "hermes-agent") == before


# ---------------------------------------------------------------------------
# Служебное действительно убирается
# ---------------------------------------------------------------------------


class TestServiceFilesAreRemoved:
    def test_backups_and_logs_are_removed(self, home):
        result = clean(home)
        assert not (home / "backups" / "pre-update.zip").exists()
        assert not (home / "logs" / "agent.log.1").exists()
        assert not (home / "logs" / "старое").exists()
        assert result.freed_bytes >= 7000 + 900 + 400

    def test_every_offered_directory_is_emptied(self, home):
        offered = [item.path for item in build_report(home).removable]
        assert offered
        clean(home)
        for path in offered:
            assert _tree_bytes(path) == 0, path

    def test_the_directories_themselves_stay_so_writers_keep_working(self, home):
        # Шлюз держит logs/gateway.log открытым: снести каталог целиком —
        # уронить следующую ротацию на несуществующем пути.
        clean(home)
        assert (home / "logs").is_dir()
        assert (home / "backups").is_dir()

    def test_removed_labels_name_what_was_actually_removed(self, home):
        labels = {label for label, _ in td.removable_candidates(home)}
        result = clean(home)
        assert result.removed_labels
        assert set(result.removed_labels) <= labels
        assert len(result.removed_labels) == len(set(result.removed_labels))

    def test_clean_is_idempotent(self, home):
        clean(home)
        second = clean(home)
        assert second.freed_bytes == 0
        assert not second.errors
        assert not second.removed_labels

    def test_empty_candidates_are_not_announced_as_removed(self, tmp_path):
        h = tmp_path / ".hermes"
        (h / "logs").mkdir(parents=True)
        result = clean(h)
        assert result.freed_bytes == 0
        assert not result.removed_labels
        assert not result.errors


# ---------------------------------------------------------------------------
# Обещание и факт сходятся
# ---------------------------------------------------------------------------


class TestPromiseMatchesReality:
    def test_freed_equals_what_disappeared_from_the_disk(self, home):
        before = _tree_bytes(home)
        result = clean(home)
        after = _tree_bytes(home)
        assert result.freed_bytes == before - after

    def test_freed_equals_what_the_report_promised(self, home):
        promised = build_report(home).removable_bytes
        assert promised > 0
        assert clean(home).freed_bytes == promised

    def test_after_cleanup_the_report_promises_nothing_more(self, home):
        clean(home)
        assert build_report(home).removable_bytes == 0

    def test_a_partial_removal_is_counted_by_what_really_went(self, home, monkeypatch):
        """Отказ на одном файле не даёт зачесть весь каталог.

        Мутация «freed += обещанный размер каталога» пройдёт мимо всех
        тестов на успешную уборку и обманет клиента ровно там, где ему
        нужнее всего правда: на забитом диске.
        """
        real = td._remove_tree
        stuck = home / "backups" / "pre-update.zip"

        def flaky(path: Path) -> None:
            if path == stuck:
                raise OSError("занято")
            return real(path)

        promised = build_report(home).removable_bytes
        monkeypatch.setattr(td, "_remove_tree", flaky)
        before = _tree_bytes(home)
        result = clean(home)
        after = _tree_bytes(home)
        assert stuck.exists()
        assert result.freed_bytes == before - after
        assert result.freed_bytes < promised
        # Недоубранное осталось обещанием на следующий раз, а не пропало.
        assert result.freed_bytes + build_report(home).removable_bytes == promised


# ---------------------------------------------------------------------------
# Проверки перед удалением. Каждая — поодиночке.
# ---------------------------------------------------------------------------


def _poison(monkeypatch, label: str, path: Path) -> None:
    """Подсунуть уборке путь, который список кандидатов никогда не отдаст.

    Так проверяется именно защита ВНУТРИ clean: опечатка в таблице путей
    выглядит для уборки ровно так же.
    """
    monkeypatch.setattr(td, "removable_candidates", lambda home: [(label, path)])


def _blind(monkeypatch, *names: str) -> None:
    """Заставить перечисленные проверки врать. Остальные обязаны выстоять.

    Проверки намеренно перекрываются, и «убрал одну — тесты зелёные» тут
    ничего не доказывает: соседняя прикрыла. Поэтому каждая проверяется
    в одиночку — соседние ослепляются.
    """
    for name in names or ("is_protected",):
        monkeypatch.setattr(td, name, lambda *a, **kw: False)


class TestGuardsAreIndependent:
    def test_a_typo_in_the_candidate_list_does_not_cost_documents(self, home, monkeypatch):
        # Проверка A в одиночку: путь настоящий, внутри дома, не ссылка.
        documents = home / "cache" / "documents"
        _poison(monkeypatch, "документы", documents)
        before = _tree_bytes(documents)
        result = clean(home)
        assert _tree_bytes(documents) == before
        assert result.freed_bytes == 0

    def test_documents_survive_even_if_the_shared_check_lies(self, home, monkeypatch):
        # Проверка B в одиночку: is_protected отключён.
        documents = home / "cache" / "documents"
        _poison(monkeypatch, "документы", documents)
        _blind(monkeypatch)
        before = _tree_bytes(documents)
        result = clean(home)
        assert _tree_bytes(documents) == before
        assert result.freed_bytes == 0

    def test_documents_survive_even_if_the_second_check_lies(self, home, monkeypatch):
        # Проверка A в одиночку: собственная сверка с protected_paths()
        # отключена, спасает только общая защита модуля.
        documents = home / "cache" / "documents"
        _poison(monkeypatch, "документы", documents)
        _blind(monkeypatch, "_touches_protected")
        before = _tree_bytes(documents)
        result = clean(home)
        assert _tree_bytes(documents) == before
        assert result.freed_bytes == 0

    def test_a_candidate_that_contains_a_protected_path_is_refused(
        self, home, monkeypatch
    ):
        """Проверка B в направлении «кандидат НАД защищённым».

        Опечатка ("cache/images", "image_cache") -> ("cache", "cache")
        отдаёт уборке весь cache — документы лежат не рядом с ним, а
        внутри него. Проверка «кандидат внутри защищённого» этого не
        видит: родство здесь обратное, и испытывать его надо отдельно.
        """
        _poison(monkeypatch, "медиакэш", home / "cache")
        _blind(monkeypatch, "is_protected")
        documents = home / "cache" / "documents"
        before = _tree_bytes(documents)
        result = clean(home)
        assert (documents / "договор.pdf").exists()
        assert _tree_bytes(documents) == before
        assert result.freed_bytes == 0
        assert result.errors

    def test_a_path_led_into_client_data_by_its_parent_is_refused(
        self, home, monkeypatch
    ):
        """Проверка B по РАЗРЕШЁННОЙ форме пути.

        Сам кандидат ссылкой не является — в документы его заводит
        родитель (``home/ссылка -> home/cache``). Написанная форма ни с
        чем защищённым не роднится, и сверка только по ней пропускает
        путь прямо на документы клиента.
        """
        _symlink(home / "ссылка", home / "cache")
        _poison(monkeypatch, "документы", home / "ссылка" / "documents")
        _blind(monkeypatch, "is_protected")
        documents = home / "cache" / "documents"
        before = _tree_bytes(documents)
        result = clean(home)
        assert (documents / "договор.pdf").exists()
        assert _tree_bytes(documents) == before
        assert result.freed_bytes == 0
        assert result.errors

    def test_a_path_that_climbs_back_to_home_is_refused(self, home, monkeypatch):
        """Проверка C в направлении «путь вернулся в дом».

        ``home/logs/..`` написан внутри дома и разрешается в сам дом.
        Вложенность обеих форм здесь ничего не запрещает — запрещает
        только сверка с самим домом, и без неё уборка вычистила бы
        HERMES_HOME целиком.
        """
        _poison(monkeypatch, "журналы", home / "logs" / "..")
        _blind(monkeypatch, "is_protected", "_touches_protected")
        before = _tree_bytes(home)
        result = clean(home)
        assert (home / "cache" / "documents" / "договор.pdf").exists()
        assert _tree_bytes(home) == before
        assert result.freed_bytes == 0
        assert result.errors

    def test_the_workspace_survives_even_if_the_shared_check_lies(self, home, monkeypatch):
        workspace = home / "sandboxes" / "docker" / "default" / "workspace"
        _poison(monkeypatch, "рабочая папка", workspace)
        _blind(monkeypatch)
        before = _tree_bytes(workspace)
        clean(home)
        assert _tree_bytes(workspace) == before

    def test_a_symlink_out_of_home_is_never_followed(self, home, tmp_path, monkeypatch):
        # Проверка C в одиночку: путь не защищён списком, не ссылка на
        # защищённое — но ведёт за пределы каталога агента.
        outside = tmp_path / "чужое"
        _write(outside / "важное.txt", 1234)
        _drop_dir(home / "logs")
        _symlink(home / "logs", outside)

        _poison(monkeypatch, "журналы", home / "logs")
        _blind(monkeypatch, "is_protected", "_touches_protected")
        result = clean(home)
        assert (outside / "важное.txt").exists()
        assert _tree_bytes(outside) == 1234
        assert result.freed_bytes == 0
        assert result.errors

    def test_a_path_that_leaves_home_through_a_parent_link_is_refused(
        self, home, tmp_path, monkeypatch
    ):
        """Проверка C в одиночку.

        Сам путь ссылкой не является — наружу его уводит родитель. Ни
        проверка на ссылку, ни сверка со списком защищённого сюда не
        достают: остаётся только требование «строго внутри дома в обеих
        формах».
        """
        outside = tmp_path / "том"
        _write(outside / "logs" / "чужое.log", 2222)
        _symlink(home / "вынос", outside)
        candidate = home / "вынос" / "logs"

        _poison(monkeypatch, "журналы", candidate)
        _blind(monkeypatch, "is_protected", "_touches_protected")
        result = clean(home)
        assert (outside / "logs" / "чужое.log").exists()
        assert result.freed_bytes == 0
        assert result.errors

    def test_home_itself_is_never_emptied(self, home, monkeypatch):
        _poison(monkeypatch, "всё сразу", home)
        _blind(monkeypatch, "is_protected", "_touches_protected")
        before = _tree_bytes(home)
        result = clean(home)
        assert _tree_bytes(home) == before
        assert result.freed_bytes == 0

    def test_a_symlink_inside_home_is_removed_as_a_link_not_as_a_tree(
        self, home, monkeypatch
    ):
        # Проверка D в одиночку: ссылка ведёт ВНУТРЬ дома, на незащищённый
        # каталог, — проверки B и C её пропускают, а вычистить код агента
        # она бы дала.
        target = home / "hermes-agent"
        _drop_dir(home / "logs")
        _symlink(home / "logs", target)

        _poison(monkeypatch, "журналы", home / "logs")
        _blind(monkeypatch, "is_protected", "_touches_protected")
        before = _tree_bytes(target)
        result = clean(home)
        assert _tree_bytes(target) == before
        assert (target / "blob.bin").exists()
        assert result.freed_bytes == 0
        assert result.errors


class TestPlanAndCleanupAgreeOnSymlinks:
    def test_a_directory_led_out_of_home_counts_as_protected(self, home, tmp_path):
        """Одной формы пути, ведущей наружу, достаточно.

        Каталог, уведённый симлинком на другой том, написан внутри дома, а
        лежит снаружи. Требовать, чтобы наружу вели ОБЕ формы, — значит
        выключить защиту ровно на той конфигурации, ради которой её писали:
        перенос каталога симлинком на забитом VPS делают первым делом.
        """
        outside = tmp_path / "том" / "logs"
        _write(outside / "agent.log.1", 4321)
        _drop_dir(home / "logs")
        _symlink(home / "logs", outside)
        assert is_protected(home, home / "logs")

    def test_a_directory_moved_out_of_home_is_not_promised_either(self, home, tmp_path):
        # Иначе отчёт обещает N, уборка отказывается его трогать, и клиент
        # получает обещаний больше, чем места.
        outside = tmp_path / "том" / "logs"
        _write(outside / "agent.log.1", 4321)
        _drop_dir(home / "logs")
        _symlink(home / "logs", outside)

        report = build_report(home)
        assert home / "logs" not in {item.path for item in report.removable}
        assert clean(home).freed_bytes == report.removable_bytes

    def test_a_symlinked_candidate_inside_home_is_not_promised_either(self, home):
        _drop_dir(home / "logs")
        _symlink(home / "logs", home / "hermes-agent")

        offered = {item.path for item in build_report(home).removable}
        assert home / "logs" not in offered
        before = _tree_bytes(home / "hermes-agent")
        clean(home)
        assert _tree_bytes(home / "hermes-agent") == before


# ---------------------------------------------------------------------------
# Отказ на одном каталоге не отменяет остальные и не молчит
# ---------------------------------------------------------------------------


class TestFailuresAreReportedNotSwallowed:
    def test_one_stuck_directory_does_not_abort_the_rest(self, home, monkeypatch):
        real = td._remove_tree

        def flaky(path: Path) -> None:
            if "backups" in path.parts:
                raise OSError("занято")
            return real(path)

        monkeypatch.setattr(td, "_remove_tree", flaky)
        result = clean(home)
        assert (home / "backups" / "pre-update.zip").exists()
        assert not (home / "logs" / "agent.log.1").exists()
        assert result.errors
        assert result.removed_labels

    def test_the_failing_directory_is_named_to_the_client(self, home, monkeypatch):
        """Клиент обязан узнать, ЧТО не убралось, — и по-русски.

        Прежде сюда попадал ``str(OSError)``: английский
        ``[Errno 13] Permission denied`` и абсолютный путь на хосте. Оба
        клиенту бесполезны, и это был единственный путь модуля, терявший
        голос ровно там, ради чего он написан.
        """
        stuck_label = dict(
            (path, label) for label, path in td.removable_candidates(home)
        )[home / "backups"]
        real = td._remove_tree

        def flaky(path: Path) -> None:
            if "backups" in path.parts:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)

        monkeypatch.setattr(td, "_remove_tree", flaky)
        result = clean(home)
        text = format_clean_result(result)
        assert stuck_label in " ".join(result.errors)
        assert stuck_label in text
        assert "не хватило прав" in text

    def test_an_unreadable_directory_is_reported(self, home, monkeypatch):
        import os as real_os

        real_scandir = real_os.scandir

        def refusing(path):
            if str(path).endswith("backups"):
                raise PermissionError("нет доступа")
            return real_scandir(path)

        monkeypatch.setattr(td.os, "scandir", refusing)
        result = clean(home)
        assert result.errors
        assert not (home / "logs" / "agent.log.1").exists()

    def test_a_failure_on_one_file_does_not_spare_its_neighbours(
        self, home, monkeypatch
    ):
        """Первый же отказ не должен обрывать обход каталога.

        Иначе один залипший файл сохраняет весь каталог, клиент видит
        «освобождено 0» и не понимает, почему место не появилось.
        """
        for i in range(4):
            _write(home / "logs" / f"agent.log.{i + 5}", 10)
        real = td._remove_tree
        state = {"failed": False}

        def fails_once(path: Path) -> None:
            if "logs" in path.parts and not state["failed"]:
                state["failed"] = True
                raise OSError("занято")
            return real(path)

        monkeypatch.setattr(td, "_remove_tree", fails_once)
        result = clean(home)
        assert state["failed"]
        # Уцелел ровно тот, что не поддался, — остальные убраны.
        assert len(list((home / "logs").iterdir())) == 1
        assert result.errors

    def test_several_failures_in_one_directory_are_summarised_not_repeated(
        self, home, monkeypatch
    ):
        for i in range(5):
            _write(home / "logs" / f"agent.log.{i + 5}", 10)
        real = td._remove_tree

        def flaky(path: Path) -> None:
            if "logs" in path.parts:
                raise OSError("занято")
            return real(path)

        monkeypatch.setattr(td, "_remove_tree", flaky)
        result = clean(home)
        # Клиент читает сообщение в мессенджере: один отказ на каталог,
        # а не по строке на каждый файл.
        assert len(result.errors) < 5


# ---------------------------------------------------------------------------
# Docker: уборка без полного варианта
# ---------------------------------------------------------------------------


class _FakeRun:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout, self.returncode, self.calls = stdout, returncode, []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return self

    @property
    def stderr(self) -> str:
        return ""


class TestDockerPruneIsNotTheFullVariant:
    def test_the_declared_command_never_asks_for_the_full_cleanup(self):
        """Полная очистка снесла бы образ песочницы (~1 ГБ).

        Следующая команда клиента ждала бы его повторной загрузки две
        минуты. Это решение продукта, а не предпочтение.
        """
        assert "prune" in DOCKER_PRUNE_ARGV
        for arg in DOCKER_PRUNE_ARGV:
            if not arg.startswith("-"):
                continue
            body = arg.lstrip("-").split("=")[0]
            assert body not in ("a", "all", "volumes"), arg
            if not arg.startswith("--"):
                # Связки вида -af полной очистке тоже открывают дверь.
                assert "a" not in body, arg

    def test_containers_are_not_pruned_either(self):
        # `docker system prune` сносит остановленные контейнеры: песочница
        # переживает остановку и хранит внутри установленные пакеты.
        assert "system" not in DOCKER_PRUNE_ARGV
        assert "container" not in DOCKER_PRUNE_ARGV

    def test_prune_runs_exactly_the_command_it_declares(self):
        run = _FakeRun("Total reclaimed space: 0B")
        docker_prune(run=run)
        assert run.calls == [list(DOCKER_PRUNE_ARGV)]

    def test_reclaimed_space_is_read_from_the_output(self):
        one_gb = docker_prune(run=_FakeRun("Total reclaimed space: 1GB"))
        two_gb = docker_prune(run=_FakeRun("Total reclaimed space: 2GB"))
        thousand_mb = docker_prune(run=_FakeRun("Total reclaimed space: 1000MB"))
        assert one_gb > 0
        assert two_gb == 2 * one_gb
        assert thousand_mb == one_gb
        assert docker_prune(run=_FakeRun("Total reclaimed space: 512B")) == 512

    def test_unreadable_output_is_zero_not_a_crash(self):
        assert docker_prune(run=_FakeRun("что-то пошло не так")) == 0

    def test_a_failed_prune_reports_nothing_reclaimed(self):
        assert docker_prune(run=_FakeRun("Total reclaimed space: 5GB", returncode=1)) == 0


class TestDockerInsideClean:
    def test_reclaimed_docker_space_is_added_to_the_freed_total(self, home):
        without = clean(home)
        # Второй прогон уже ничего не найдёт на диске — весь прирост
        # приходит от docker.
        with_docker = clean(home, docker_prune=lambda: 3000)
        assert without.freed_bytes > 0
        assert with_docker.freed_bytes == 3000
        assert any("ocker" in label for label in with_docker.removed_labels)

    def test_docker_is_not_touched_unless_the_caller_asks(self, home, monkeypatch):
        # Считаем вызовы, а не бросаем: clean ловит любое исключение из
        # уборки docker, и брошенное здесь потерялось бы в errors.
        calls = []

        def counted(*args, **kwargs):
            calls.append(args)
            return 0

        monkeypatch.setattr(td, "docker_prune", counted)
        assert clean(home).freed_bytes > 0
        assert calls == []

    def test_a_docker_failure_does_not_cancel_the_file_cleanup(self, home):
        def broken():
            raise RuntimeError("демон не отвечает")

        result = clean(home, docker_prune=broken)
        assert not (home / "backups" / "pre-update.zip").exists()
        assert result.freed_bytes > 0
        assert any("демон не отвечает" in e for e in result.errors)

    def test_docker_that_reclaimed_nothing_is_not_announced(self, home):
        result = clean(home, docker_prune=lambda: 0)
        assert not any("ocker" in label for label in result.removed_labels)


# ---------------------------------------------------------------------------
# Текст для клиента
# ---------------------------------------------------------------------------


class TestClientMessage:
    def test_the_message_names_the_freed_space_and_what_went(self, home):
        result = clean(home)
        text = format_clean_result(result)
        assert _size(result.freed_bytes) in text
        for label in result.removed_labels:
            assert label in text

    def test_the_message_reassures_about_client_files(self, home):
        text = format_clean_result(clean(home))
        assert "документ" in text.lower()
        assert "рабоч" in text.lower()

    def test_nothing_to_clean_says_so_without_promising_numbers(self, tmp_path):
        h = tmp_path / ".hermes"
        h.mkdir(parents=True)
        text = format_clean_result(clean(h))
        assert "🧹" not in text
        assert text.strip()

    def test_the_message_does_not_start_with_protocol_noise(self, home):
        text = format_clean_result(clean(home))
        first = text.splitlines()[0]
        assert "/" not in first.replace("🧹", "")
        assert "clean" not in first.lower()


# ---------------------------------------------------------------------------
# Голос отказа: клиенту — словами, подробности — в журнал
# ---------------------------------------------------------------------------


class TestRefusalsSpeakToTheClientNotToTheHost:
    """Финальное ревью, §1.3: ``/disk clean`` при отказе печатал клиенту
    ``Не удалось убрать: журналы: [Errno 13] Permission denied:
    '/var/…/.hermes/logs/gateway.log'``.

    Английский код errno клиенту непонятен, абсолютный путь на хосте
    бесполезен (ни консоли, ни файлового менеджера у него нет) и вдобавок
    рассказывает про внутренности сервера. Мутация, заменившая ``str(exc)``
    на ``repr(exc)`` с путём, в финальном ревью выжила: текст отказа не был
    покрыт ничем.
    """

    @staticmethod
    def _fail_on_backups(monkeypatch, exc_factory):
        real = td._remove_tree

        def flaky(path: Path) -> None:
            if "backups" in path.parts:
                raise exc_factory(path)
            return real(path)

        monkeypatch.setattr(td, "_remove_tree", flaky)

    def test_no_errno_code_and_no_host_path_reach_the_client(self, home, monkeypatch):
        self._fail_on_backups(
            monkeypatch,
            lambda path: PermissionError(13, "Permission denied", str(path)),
        )
        text = format_clean_result(clean(home))
        assert "Errno" not in text
        assert "Permission denied" not in text
        assert str(home) not in text
        assert "backups" not in text.replace("бэкапы", "")

    def test_the_client_still_learns_why_it_failed(self, home, monkeypatch):
        self._fail_on_backups(
            monkeypatch,
            lambda path: PermissionError(13, "Permission denied", str(path)),
        )
        assert "не хватило прав" in format_clean_result(clean(home))

    def test_a_busy_file_reads_differently_from_a_permission_problem(
        self, home, monkeypatch
    ):
        """Причины обязаны различаться: «занят» лечится ожиданием, «прав
        не хватило» — только вмешательством владельца машины."""
        import errno as _errno

        self._fail_on_backups(
            monkeypatch, lambda path: PermissionError(13, "Permission denied", str(path))
        )
        denied = " ".join(clean(home).errors)

        self._fail_on_backups(
            monkeypatch, lambda path: OSError(_errno.EBUSY, "Device busy", str(path))
        )
        busy = " ".join(clean(home).errors)
        assert denied and busy and denied != busy

    def test_an_unknown_errno_still_says_something_russian(self, home, monkeypatch):
        self._fail_on_backups(monkeypatch, lambda path: OSError("что-то пошло не так"))
        errors = " ".join(clean(home).errors)
        assert errors
        assert any("а" <= ch.lower() <= "я" for ch in errors)

    def test_the_technical_detail_still_reaches_the_log(self, home, monkeypatch, caplog):
        """Подробности не выброшены — они переехали туда, где их читает
        тот, кто выдал машину."""
        import logging

        self._fail_on_backups(
            monkeypatch,
            lambda path: PermissionError(13, "Permission denied", str(path)),
        )
        with caplog.at_level(logging.WARNING, logger=td.__name__):
            clean(home)
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "backups" in logged
        assert "Permission denied" in logged

    def test_a_file_that_is_already_gone_is_not_called_a_failure(
        self, home, monkeypatch
    ):
        """``ENOENT`` — достигнутая цель, а не отказ. Именно так выглядит
        вторая одновременная уборка: она сносит уже снесённое."""
        import errno as _errno

        real = td._remove_tree

        def vanished(path: Path) -> None:
            if "backups" in path.parts:
                real(path)
                raise FileNotFoundError(
                    _errno.ENOENT, "No such file or directory", str(path)
                )
            return real(path)

        monkeypatch.setattr(td, "_remove_tree", vanished)
        result = clean(home)
        assert not result.errors, result.errors
        assert result.freed_bytes > 0

    def test_many_identical_failures_are_summarised_once(self, home, monkeypatch):
        for i in range(5):
            _write(home / "logs" / f"agent.log.{i + 5}", 10)
        real = td._remove_tree

        def flaky(path: Path) -> None:
            if "logs" in path.parts:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)

        monkeypatch.setattr(td, "_remove_tree", flaky)
        errors = clean(home).errors
        assert len(errors) == 1
        assert errors[0].count("не хватило прав") == 1


# ---------------------------------------------------------------------------
# Две одновременные уборки
# ---------------------------------------------------------------------------


class TestTwoSimultaneousCleansDoNotCountTheSameBytesTwice:
    """Финальное ревью, §2.1. Две уборки на общем барьере отчитывались
    каждая ПОЛНЫМ объёмом (7,6 МБ и 7,6 МБ при реальных 7,6 на диске), и
    обе выкладывали стену ``[Errno 2]`` с путями хоста — вторая натыкалась
    на файлы, снесённые первой, и называла чужой успех своей неудачей.

    После задачи 6 это стало достижимо руками: до `/disk` у клиента не было
    способа запустить уборку вообще, тем более две подряд.
    """

    @staticmethod
    def _run_two(home):
        import threading

        start = threading.Barrier(2)
        results = {}

        def worker(slot):
            start.wait(timeout=10)
            results[slot] = clean(home)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "уборка не завершилась"
        return results[0], results[1]

    def test_the_sum_reported_never_exceeds_what_left_the_disk(self, home):
        before = _tree_bytes(home)
        first, second = self._run_two(home)
        actually_freed = before - _tree_bytes(home)
        assert actually_freed > 0
        assert first.freed_bytes + second.freed_bytes == actually_freed

    def test_neither_cleanup_calls_the_others_success_a_failure(self, home):
        first, second = self._run_two(home)
        assert not first.errors, first.errors
        assert not second.errors, second.errors

    def test_the_second_one_honestly_reports_it_had_nothing_to_do(self, home):
        first, second = self._run_two(home)
        loser = second if first.freed_bytes else first
        assert loser.freed_bytes == 0
        assert not loser.removed_labels
        assert "Освобождено" not in format_clean_result(loser)

    def test_client_files_survive_two_simultaneous_cleanups(self, home):
        documents = home / "cache" / "documents"
        before = _tree_bytes(documents)
        self._run_two(home)
        assert _tree_bytes(documents) == before

    def test_cleanups_of_different_homes_do_not_block_each_other(self, tmp_path):
        """Замок — на дом, а не один на процесс: у профилей свои дома."""
        a, b = tmp_path / "a", tmp_path / "b"
        _write(a / "logs" / "x.log", 100)
        _write(b / "logs" / "x.log", 100)
        assert td._clean_lock(a) is not td._clean_lock(b)
        assert td._clean_lock(a) is td._clean_lock(a)
