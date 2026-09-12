"""Замер места: что считается, что защищено и что попадает в план уборки.

Тесты проверяют отношения, а не сегодняшние числа: «строка про документы
несёт размер документов», «сумма разделов равна занятому», «ни один
предложенный к уборке путь не защищён». Снимков текста и чтения исходников
здесь нет.
"""
from pathlib import Path

import pytest

from hermes_constants import get_hermes_dir
from hermes_state import DEFAULT_DB_PATH
from hermes_cli.trix_disk import (
    PROTECTED_SUBPATHS,
    DiskReport,
    RemovableItem,
    build_report,
    format_report,
    is_protected,
    _size,
)

DB_NAME = DEFAULT_DB_PATH.name

GB = 1024 ** 3


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _tree_bytes(path: Path) -> int:
    """Независимый от модуля замер дерева — эталон для сверки."""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(
        p.stat().st_size
        for p in path.rglob("*")
        if p.is_file() and not p.is_symlink()
    )


class _Usage:
    def __init__(self, total: int, used: int, free: int) -> None:
        self.total, self.used, self.free = total, used, free


@pytest.fixture(autouse=True)
def _no_sandbox_override(monkeypatch):
    monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / ".hermes"
    _write(h / "cache" / "documents" / "договор.pdf", 5000)
    _write(h / "cache" / "images" / "shot.png", 3000)
    _write(h / "sandboxes" / "docker" / "default" / "workspace" / "site" / "index.html", 2000)
    # /root контейнера: кэши pip/npm агента, не файлы клиента.
    _write(h / "sandboxes" / "docker" / "default" / "home" / ".cache" / "pip" / "w.whl", 6000)
    _write(h / DB_NAME, 4000)
    _write(h / f"{DB_NAME}-wal", 1000)
    _write(h / "backups" / "pre-update-2026-01-01.zip", 7000)
    _write(h / "state-snapshots" / "s1" / "config.yaml", 100)
    _write(h / "logs" / "agent.log.1", 900)
    _write(h / "debug-reports" / "r1.json", 800)
    # Крупное, о чём модуль ничего не знает: на реальной машине это
    # hermes-agent/, node/, bin/ — вместе больше всех кэшей.
    _write(h / "hermes-agent" / "blob.bin", 11000)
    _write(h / "node" / "bin" / "node", 13000)
    return h


# ---------------------------------------------------------------------------
# Пункт 1: защита работает в обе стороны
# ---------------------------------------------------------------------------


class TestProtectionDirection:
    def test_file_inside_protected_subtree_is_protected(self, home):
        assert is_protected(home, home / "cache" / "documents" / "договор.pdf")

    def test_protected_path_itself_is_protected(self, home):
        assert is_protected(home, home / "cache" / "documents")

    def test_parent_of_protected_path_is_protected(self, home):
        # Опечатка ("cache", "cache/images") -> ("cache", "cache") отдаёт
        # удалятору весь cache вместе с документами клиента.
        assert is_protected(home, home / "cache")

    def test_home_itself_is_protected(self, home):
        assert is_protected(home, home)

    def test_empty_relative_path_is_protected(self, home):
        # home / "" == home: опечатка ("logs", "") предлагает к удалению
        # весь HERMES_HOME одним пунктом.
        assert is_protected(home, home / "")

    def test_path_outside_home_is_protected(self, home, tmp_path):
        # Аварийная ветка: не смогли отнести к дому — не трогаем.
        assert is_protected(home, tmp_path / "чужое")

    def test_removable_and_lookalike_paths_are_not_protected(self, home):
        assert not is_protected(home, home / "backups")
        assert not is_protected(home, home / "logs")
        assert not is_protected(home, home / "cache" / "images")
        assert not is_protected(home, home / "cache" / "documents-old")

    def test_protected_list_names_documents_and_sandboxes(self):
        assert PROTECTED_SUBPATHS
        assert any("documents" in p for p in PROTECTED_SUBPATHS)
        assert any("sandbox" in p for p in PROTECTED_SUBPATHS)


# ---------------------------------------------------------------------------
# Пункты 1 и 7: метка, путь и размер связаны
# ---------------------------------------------------------------------------


class TestRemovablePlan:
    def test_every_offered_item_matches_its_own_directory(self, home):
        r = build_report(home)
        assert r.removable
        for item in r.removable:
            assert item.bytes == _tree_bytes(item.path), item
            assert item.bytes > 0
            assert item.path != home
            assert item.path.is_relative_to(home)

    def test_no_offered_path_is_protected(self, home):
        r = build_report(home)
        for item in r.removable:
            assert not is_protected(home, item.path), item

    def test_offered_paths_do_not_overlap_or_repeat(self, home):
        r = build_report(home)
        paths = [item.path for item in r.removable]
        assert len(paths) == len(set(paths))
        for a in paths:
            for b in paths:
                if a is b:
                    continue
                assert not a.is_relative_to(b), (a, b)

    def test_logs_and_backups_are_actually_offered(self, home):
        # Подмена пути у метки ("logs" -> debug-reports) оставляет реальные
        # логи нечищеными навсегда, а debug-reports считает дважды.
        offered = {item.path for item in build_report(home).removable}
        assert home / "logs" in offered
        assert home / "backups" in offered

    def test_client_data_is_never_offered(self, home):
        offered = {item.path for item in build_report(home).removable}
        assert home not in offered
        for path in (
            home / "cache" / "documents",
            home / "sandboxes",
            home / "sandboxes" / "docker" / "default" / "workspace",
            home / DB_NAME,
        ):
            assert path not in offered
            assert not any(p.is_relative_to(path) for p in offered), path

    def test_removable_bytes_equal_the_sum_of_offered_directories(self, home):
        r = build_report(home)
        assert r.removable_bytes == sum(_tree_bytes(i.path) for i in r.removable)


# ---------------------------------------------------------------------------
# Пункт 2: раскладка каталогов берётся у остального Hermes
# ---------------------------------------------------------------------------


class TestDirectoryLayout:
    def test_documents_are_read_where_the_rest_of_hermes_reads_them(self, home):
        r = build_report(home)
        canonical = get_hermes_dir("cache/documents", "document_cache", home=home)
        assert r.documents_bytes == _tree_bytes(canonical)
        assert r.documents_bytes > 0

    def test_legacy_layout_is_counted_and_protected(self, tmp_path):
        h = tmp_path / ".hermes"
        _write(h / "document_cache" / "акт.pdf", 4321)
        _write(h / "image_cache" / "shot.png", 2222)
        _write(h / "logs" / "agent.log", 500)
        r = build_report(h)

        assert r.documents_bytes == 4321
        assert is_protected(h, h / "document_cache")
        assert is_protected(h, h / "document_cache" / "акт.pdf")

        offered = {item.path for item in r.removable}
        assert h / "image_cache" in offered
        assert h / "document_cache" not in offered

    def test_new_layout_wins_when_legacy_is_empty(self, tmp_path):
        h = tmp_path / ".hermes"
        (h / "document_cache").mkdir(parents=True)
        _write(h / "cache" / "documents" / "договор.pdf", 777)
        assert build_report(h).documents_bytes == 777

    def test_workspace_follows_the_configured_sandbox_root(self, tmp_path, monkeypatch):
        h = tmp_path / ".hermes"
        boxes = h / "boxes"
        monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(boxes))
        _write(boxes / "docker" / "t1" / "workspace" / "site.html", 3333)
        _write(boxes / "docker" / "t1" / "home" / ".npm" / "cache.bin", 999)
        # Каталог по умолчанию существует, но песочница настроена не там.
        _write(h / "sandboxes" / "docker" / "t0" / "workspace" / "old.html", 4444)

        r = build_report(h)
        assert r.workspace_bytes == 3333
        assert is_protected(h, boxes)
        assert is_protected(h, boxes / "docker" / "t1" / "workspace")

    def test_external_sandbox_root_is_still_counted_as_agent_data(self, tmp_path, monkeypatch):
        h = tmp_path / ".hermes"
        boxes = tmp_path / "boxes"
        monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(boxes))
        _write(h / DB_NAME, 100)
        _write(boxes / "docker" / "t1" / "workspace" / "site.html", 3333)
        _write(boxes / "docker" / "t1" / "home" / "pip.bin", 999)

        r = build_report(h)
        assert r.workspace_bytes == 3333
        assert r.agent_bytes >= 3333 + 999 + 100
        assert is_protected(h, boxes / "docker" / "t1" / "workspace")

    def test_conversations_are_read_from_the_real_session_database(self, home):
        r = build_report(home)
        assert r.sessions_bytes == _tree_bytes(home / DB_NAME) + _tree_bytes(
            home / f"{DB_NAME}-wal"
        )
        assert r.sessions_bytes > 0


# ---------------------------------------------------------------------------
# Пункт 3: категории сходятся с «занято»
# ---------------------------------------------------------------------------


class TestCategoriesAddUp:
    def test_sections_sum_to_used_space(self, home):
        r = build_report(home)
        assert (
            r.documents_bytes
            + r.workspace_bytes
            + r.sessions_bytes
            + r.service_bytes
            + r.other_bytes
            == r.used
        )

    def test_agent_data_covers_the_whole_home_tree(self, home):
        r = build_report(home)
        assert r.agent_bytes == _tree_bytes(home)

    def test_directories_the_module_never_heard_of_land_in_service(self, home):
        r = build_report(home)
        unknown = _tree_bytes(home / "hermes-agent") + _tree_bytes(home / "node")
        assert unknown > 0
        assert r.service_bytes >= unknown + r.removable_bytes

    def test_service_never_swallows_client_files(self, home):
        r = build_report(home)
        assert r.service_bytes == r.agent_bytes - r.client_bytes - r.sessions_bytes
        assert r.documents_bytes not in (0, r.service_bytes)

    def test_other_is_what_the_disk_holds_beyond_the_agent(self, home, monkeypatch):
        import hermes_cli.trix_disk as mod

        tree = _tree_bytes(home)
        monkeypatch.setattr(mod, "_disk_usage", lambda p: (p, _Usage(100 * GB, tree + 7 * GB, 93 * GB)))
        r = build_report(home)
        assert r.other_bytes == 7 * GB
        assert r.agent_bytes == tree

    def test_used_percent_is_measured_from_used_space(self, home, monkeypatch):
        import hermes_cli.trix_disk as mod

        monkeypatch.setattr(mod, "_disk_usage", lambda p: (p, _Usage(100 * GB, 96 * GB, 4 * GB)))
        r = build_report(home)
        assert r.used_percent == pytest.approx(96.0, abs=0.05)
        assert "96 %" in format_report(r)


# ---------------------------------------------------------------------------
# Пункт 4: sandboxes/docker/<id>/home — служебное, а не «ваши файлы»
# ---------------------------------------------------------------------------


class TestSandboxHomeIsNotClientData:
    def test_container_home_is_not_counted_as_client_files(self, home):
        r = build_report(home)
        container_home = home / "sandboxes" / "docker" / "default" / "home"
        assert _tree_bytes(container_home) > 0
        assert r.workspace_bytes == _tree_bytes(
            home / "sandboxes" / "docker" / "default" / "workspace"
        )
        assert r.client_bytes == r.documents_bytes + r.workspace_bytes
        assert r.client_bytes < r.client_bytes + _tree_bytes(container_home)

    def test_container_home_is_counted_as_service(self, home):
        r = build_report(home)
        container_home = home / "sandboxes" / "docker" / "default" / "home"
        assert r.service_bytes >= _tree_bytes(container_home)

    def test_container_home_is_still_not_offered_for_removal(self, home):
        offered = {item.path for item in build_report(home).removable}
        container_home = home / "sandboxes" / "docker" / "default" / "home"
        assert container_home not in offered
        assert not any(p.is_relative_to(container_home) for p in offered)


# ---------------------------------------------------------------------------
# Пункт 5: обещание уборки только там, где есть что убирать
# ---------------------------------------------------------------------------


def _report(**over) -> DiskReport:
    base = dict(
        total=100 * GB,
        used=95 * GB,
        free=5 * GB,
        used_percent=95.0,
        documents_bytes=3 * GB,
        workspace_bytes=7 * GB,
        sessions_bytes=2 * GB,
        service_bytes=4 * GB,
        other_bytes=79 * GB,
        removable=[],
    )
    base.update(over)
    return DiskReport(**base)


class TestCleanupPromise:
    def test_tiny_savings_are_not_offered_even_on_a_full_disk(self):
        r = _report(removable=[RemovableItem("журналы", Path("/x/logs"), 4096)])
        assert "/disk clean" not in format_report(r)

    def test_nothing_to_remove_is_not_offered_on_a_full_disk(self):
        assert "/disk clean" not in format_report(_report())

    def test_worthwhile_savings_are_offered_on_a_full_disk(self):
        r = _report(removable=[RemovableItem("медиакэш", Path("/x/cache/images"), 3 * GB)])
        text = format_report(r)
        assert "/disk clean" in text
        assert _size(3 * GB) in text

    def test_the_offer_shows_what_exactly_would_be_removed(self):
        r = _report(removable=[
            RemovableItem("медиакэш", Path("/x/cache/images"), 2 * GB),
            RemovableItem("медиакэш", Path("/x/cache/audio"), 1 * GB),
            RemovableItem("журналы", Path("/x/logs"), 4 * GB),
        ])
        text = format_report(r)
        assert _size(3 * GB) in _line_starting(text, "• медиакэш")
        assert _size(4 * GB) in _line_starting(text, "• журналы")
        assert _size(7 * GB) in _line_starting(text, "Служебное можно сократить")

    def test_savings_are_not_offered_while_the_disk_is_roomy(self):
        r = _report(
            used=10 * GB,
            free=90 * GB,
            used_percent=10.0,
            other_bytes=0,
            removable=[RemovableItem("медиакэш", Path("/x/cache/images"), 3 * GB)],
        )
        assert "/disk clean" not in format_report(r)


# ---------------------------------------------------------------------------
# Пункт 6: отчёт вместо падения
# ---------------------------------------------------------------------------


class TestSurvivesBrokenTrees:
    @staticmethod
    def _symlink(link: Path, target: str) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("файловая система не поддерживает симлинки")

    def test_symlink_loop_is_protected_and_does_not_raise(self, home):
        self._symlink(home / "loopa", "loopb")
        self._symlink(home / "loopb", "loopa")
        assert is_protected(home, home / "loopa" / "x")

    def test_symlink_loop_does_not_break_the_report(self, home):
        self._symlink(home / "logs" / "loopa", "loopb")
        self._symlink(home / "logs" / "loopb", "loopa")
        r = build_report(home)
        assert r.total > 0
        assert format_report(r)

    def test_symlinks_inside_a_tree_are_not_followed(self, home, tmp_path):
        # Ни файл, ни каталог по ссылке не считается: иначе одни и те же
        # байты приезжают в отчёт дважды или приходят вообще извне.
        outside = tmp_path / "big.bin"
        _write(outside, 50_000)
        before = build_report(home)
        self._symlink(home / "logs" / "link.bin", str(outside))
        self._symlink(home / "logs" / "link-dir", str(home / "backups"))
        after = build_report(home)
        assert after.agent_bytes == before.agent_bytes
        assert after.removable_bytes == before.removable_bytes

    def test_a_symlinked_directory_is_not_planned_for_removal_twice(self, home):
        # checkpoints -> backups: одни и те же байты, обещание «освободить
        # вдвое больше, чем есть» клиент проверит и не поверит отчёту.
        self._symlink(home / "checkpoints", "backups")
        r = build_report(home)
        assert r.removable_bytes == _tree_bytes(home / "backups") + sum(
            _tree_bytes(i.path)
            for i in r.removable
            if i.path.resolve() != (home / "backups").resolve()
        )
        resolved = [i.path.resolve() for i in r.removable]
        assert len(resolved) == len(set(resolved))

    def test_a_named_directory_moved_to_another_volume_is_still_measured(self, home, tmp_path):
        # Клиент увёл документы симлинком на другой том — «0 МБ» было бы
        # ложью, а защита должна следовать за симлинком.
        elsewhere = tmp_path / "том" / "documents"
        _write(elsewhere / "договор.pdf", 12345)
        target = home / "cache" / "documents"
        for item in sorted(target.rglob("*"), reverse=True):
            item.unlink()
        target.rmdir()
        self._symlink(target, str(elsewhere))
        r = build_report(home)
        assert r.documents_bytes == 12345
        assert is_protected(home, target)

    def test_missing_home_without_a_parent_still_reports(self, tmp_path):
        r = build_report(tmp_path / "нет" / "такого" / "профиля")
        assert r.total > 0
        assert r.documents_bytes == 0
        assert r.removable == []
        assert "/disk clean" not in format_report(r)


# ---------------------------------------------------------------------------
# Пункт 7: подписи, числа и проценты в тексте отчёта
# ---------------------------------------------------------------------------


def _line_starting(text: str, prefix: str) -> str:
    """Строка отчёта, начинающаяся с подписи.

    Именно с начала: подпись «разговоры» встречается и в строке категории,
    и в предупреждении о заполненном диске, а проверять надо ту строку,
    которая несёт число.
    """
    matches = [line for line in text.splitlines() if line.startswith(prefix)]
    assert len(matches) == 1, (prefix, matches)
    return matches[0]


class TestReportText:
    def test_every_label_carries_its_own_number(self):
        text = format_report(_report())
        assert _size(3 * GB) in _line_starting(text, "• присланные документы")
        assert _size(7 * GB) in _line_starting(text, "• рабочая папка")
        assert _size(2 * GB) in _line_starting(text, "• разговоры")
        assert _size(4 * GB) in _line_starting(text, "• служебное")

    def test_headline_numbers_match_the_sections(self):
        r = _report()
        text = format_report(r)
        assert _size(r.agent_bytes) in _line_starting(text, "Данные агента")
        assert _size(r.other_bytes) in _line_starting(text, "Остальное")
        headline = _line_starting(text, "💾")
        assert _size(r.used) in headline
        assert _size(r.total) in headline
        assert _size(r.free) in headline

    def test_docker_is_named_inside_other_without_a_separate_number(self):
        """Отдельной строки про образы Docker в отчёте нет: их размер никем
        не измеряется. «Остальное» считается вычитанием, образы в него
        входят, и строка говорит об этом словами — обещать число, которого
        никто не считал, было бы хуже, чем не обещать ничего."""
        text = format_report(_report())
        other_line = _line_starting(text, "Остальное")
        assert "Docker" in other_line
        assert not any(line.startswith("• образы Docker") for line in text.splitlines())

    def test_report_speaks_about_documents_and_percentage(self, home):
        text = format_report(build_report(home))
        assert "документ" in text.lower()
        assert "%" in text


# ---------------------------------------------------------------------------
# Раунд 2, пункт 1: предупреждение о заполненном диске
# ---------------------------------------------------------------------------


class TestFullDiskAdvice:
    def test_full_disk_warns_even_when_cleanup_cannot_help(self):
        text = format_report(_report(removable=[]))
        assert "Места почти нет" in text
        assert "не смогу" in text  # чем это грозит
        assert "машину" in text  # что делать
        assert "/disk clean" not in text

    def test_full_disk_offers_cleanup_instead_of_outside_help_when_it_helps(self):
        text = format_report(
            _report(removable=[RemovableItem("медиакэш", Path("/x/cache/images"), 3 * GB)])
        )
        assert "Места почти нет" in text
        assert "/disk clean" in text
        assert "машину" not in text

    def test_roomy_disk_says_nothing_alarming(self):
        text = format_report(
            _report(
                used=10 * GB,
                free=90 * GB,
                used_percent=10.0,
                other_bytes=0,
                removable=[RemovableItem("медиакэш", Path("/x/cache/images"), 3 * GB)],
            )
        )
        assert "Места почти нет" not in text
        assert "/disk clean" not in text
        assert "машину" not in text

    def test_warning_reaches_a_report_built_from_a_real_tree(self, home, monkeypatch):
        import hermes_cli.trix_disk as mod

        monkeypatch.setattr(mod, "_disk_usage", lambda p: (p, _Usage(100 * GB, 96 * GB, 4 * GB)))
        text = format_report(build_report(home))
        assert "Места почти нет" in text
        assert "не смогу" in text


# ---------------------------------------------------------------------------
# Раунд 2, пункт 2: числа не зажимаются молча
# ---------------------------------------------------------------------------


class TestNumbersNeverLieSilently:
    def test_the_balance_closes_on_a_real_tree(self, home, monkeypatch):
        import hermes_cli.trix_disk as mod

        tree = _tree_bytes(home)
        monkeypatch.setattr(mod, "_disk_usage", lambda p: (p, _Usage(100 * GB, tree + 4096, 90 * GB)))
        r = build_report(home)
        assert r.agent_bytes == tree
        assert r.elsewhere_bytes == 0
        assert r.other_bytes == 4096
        assert r.local_agent_bytes + r.other_bytes == r.used

    def test_data_on_another_volume_is_named_and_not_subtracted(self, home, tmp_path, monkeypatch):
        import hermes_cli.trix_disk as mod

        boxes = tmp_path / "другой-том"
        _write(boxes / "docker" / "t1" / "workspace" / "site.html", 30_000)
        _write(boxes / "docker" / "t1" / "home" / "pip.bin", 9_000)
        monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(boxes))
        # Второй том в pytest не смонтируешь — подменяем определение устройства.
        real_device = mod._device_of
        monkeypatch.setattr(
            mod, "_device_of",
            lambda path, st: 4242 if str(boxes) in str(path) else real_device(path, st),
        )
        tree = _tree_bytes(home)
        monkeypatch.setattr(mod, "_disk_usage", lambda p: (p, _Usage(100 * GB, tree + 4096, 90 * GB)))

        r = build_report(home)
        assert r.workspace_bytes == 30_000
        assert r.elsewhere_bytes == 39_000
        assert r.local_agent_bytes == r.agent_bytes - 39_000
        assert r.other_bytes == r.used - r.local_agent_bytes
        assert "на другом диске" in format_report(r)

    def test_a_balance_that_does_not_close_is_said_out_loud(self, home, monkeypatch):
        import hermes_cli.trix_disk as mod

        monkeypatch.setattr(mod, "_disk_usage", lambda p: (p, _Usage(100 * GB, 1000, 100 * GB - 1000)))
        r = build_report(home)
        assert r.agent_bytes > r.used
        assert r.notes
        assert "разойтись" in format_report(r)


# ---------------------------------------------------------------------------
# Раунд 2, пункт 3: метка следует за каталогом в любой раскладке
# ---------------------------------------------------------------------------


class TestLabelsFollowDirectories:
    """Метка принадлежит каталогу, а не строке таблицы.

    Проверяется двумя отношениями, ни одно из которых не фиксирует
    сегодняшние слова: метка одного и того же каталога одинакова в новой
    и легаси-раскладке, и она не зависит от того, какие ЕЩЁ каталоги есть
    на диске.
    """

    @staticmethod
    def _labels(home: Path) -> dict[str, str]:
        return {item.path.name: item.label for item in build_report(home).removable}

    def test_a_directory_keeps_its_label_across_layouts(self, tmp_path):
        modern = tmp_path / "новый" / ".hermes"
        _write(modern / "cache" / "images" / "i.png", 300)
        _write(modern / "cache" / "web" / "page.html", 400)

        legacy = tmp_path / "легаси" / ".hermes"
        _write(legacy / "image_cache" / "i.png", 300)
        _write(legacy / "web_cache" / "page.html", 400)

        modern_labels, legacy_labels = self._labels(modern), self._labels(legacy)
        assert legacy_labels["image_cache"] == modern_labels["images"]
        assert legacy_labels["web_cache"] == modern_labels["web"]
        assert legacy_labels["image_cache"] != legacy_labels["web_cache"]

    def test_a_label_does_not_depend_on_which_other_directories_exist(self, tmp_path):
        alone_backups = tmp_path / "один" / ".hermes"
        _write(alone_backups / "backups" / "b.zip", 8000)
        alone_snapshots = tmp_path / "другой" / ".hermes"
        _write(alone_snapshots / "state-snapshots" / "s.yaml", 100)
        together = tmp_path / "оба" / ".hermes"
        _write(together / "backups" / "b.zip", 8000)
        _write(together / "state-snapshots" / "s.yaml", 100)

        both = self._labels(together)
        assert both["backups"] == self._labels(alone_backups)["backups"]
        assert both["state-snapshots"] == self._labels(alone_snapshots)["state-snapshots"]
        assert both["backups"] != both["state-snapshots"]

    def test_each_offered_item_is_labelled_by_the_directory_it_measures(self, tmp_path):
        legacy = tmp_path / ".hermes"
        _write(legacy / "backups" / "b.zip", 8000)
        _write(legacy / "state-snapshots" / "s.yaml", 100)
        report = build_report(legacy)
        for item in report.removable:
            assert item.bytes == _tree_bytes(item.path)
        by_label = {i.label: i.bytes for i in report.removable}
        assert len(by_label) == 2
        assert sorted(by_label.values()) == [100, 8000]


# ---------------------------------------------------------------------------
# Раунд 2, пункт 4: отсев дублей по родству, а не по равенству
# ---------------------------------------------------------------------------


class TestPlanNeverPromisesTwice:
    @staticmethod
    def _symlink(link: Path, target: str) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("файловая система не поддерживает симлинки")

    def test_a_symlink_into_another_candidate_is_not_promised_twice(self, home):
        _write(home / "backups" / "sub" / "big.bin", 8000)
        self._symlink(home / "checkpoints", str(home / "backups" / "sub"))
        r = build_report(home)

        resolved = [i.path.resolve() for i in r.removable]
        for first in resolved:
            for second in resolved:
                if first is second:
                    continue
                assert not first.is_relative_to(second), (first, second)
        assert r.removable_bytes == sum(_tree_bytes(p) for p in set(resolved))

    def test_dropping_a_duplicate_never_drops_the_real_directory(self, home):
        _write(home / "backups" / "sub" / "big.bin", 8000)
        self._symlink(home / "checkpoints", str(home / "backups" / "sub"))
        r = build_report(home)
        offered = {i.path for i in r.removable}
        assert home / "backups" in offered
        assert next(i.bytes for i in r.removable if i.path == home / "backups") == _tree_bytes(
            home / "backups"
        )


# ---------------------------------------------------------------------------
# Раунд 2, пункт 5: симлинки не выключают защиту и не обнуляют замер
# ---------------------------------------------------------------------------


class TestSymlinkedLayouts:
    @staticmethod
    def _symlink(link: Path, target: str) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("файловая система не поддерживает симлинки")

    def _move_out(self, home: Path, relative: str, destination: Path) -> None:
        source = home / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        self._symlink(source, str(destination))

    def test_protection_holds_when_documents_are_moved_off_the_volume(self, home, tmp_path):
        self._move_out(home, "cache/documents", tmp_path / "том" / "documents")
        assert is_protected(home, home / "cache" / "documents")
        assert is_protected(home, home / "cache")
        offered = {i.path for i in build_report(home).removable}
        assert not any(p.is_relative_to(home / "cache") and p.name != "images" for p in offered)

    def test_documents_moved_off_the_volume_are_still_measured(self, home, tmp_path):
        self._move_out(home, "cache/documents", tmp_path / "том" / "documents")
        assert build_report(home).documents_bytes == 5000

    def test_sandbox_moved_to_another_disk_changes_no_number(self, home, tmp_path):
        # Перенос песочниц на отдельный диск симлинком — первое, что делают
        # на забитом VPS. Ни один размер от этого меняться не должен.
        before = build_report(home)
        self._move_out(home, "sandboxes/docker", tmp_path / "диск" / "docker")
        after = build_report(home)
        assert before.workspace_bytes == 2000
        assert after.workspace_bytes == before.workspace_bytes
        assert after.service_bytes == before.service_bytes
        assert after.agent_bytes == before.agent_bytes

    def test_a_candidate_pointing_into_protected_data_is_protected(self, home, tmp_path):
        # logs -> cache/documents: путь написан безобидно, ведёт в документы.
        (home / "logs" / "agent.log.1").unlink()
        (home / "logs").rmdir()
        self._symlink(home / "logs", str(home / "cache" / "documents"))
        assert is_protected(home, home / "logs")
        offered = {i.path for i in build_report(home).removable}
        assert home / "logs" not in offered


# ---------------------------------------------------------------------------
# Раунд 2, пункт 6: честное «не смог посчитать»
# ---------------------------------------------------------------------------


class TestHonestAboutFailures:
    def test_unreadable_directory_is_named_not_silently_zeroed(self, home):
        import os as _os

        if hasattr(_os, "geteuid") and _os.geteuid() == 0:
            pytest.skip("root читает что угодно, запрет не воспроизвести")
        locked = home / "logs"
        locked.chmod(0o000)
        try:
            r = build_report(home)
            text = format_report(r)
        finally:
            locked.chmod(0o755)
        assert r.unreadable > 0
        assert "прочитать не удалось" in text

    def test_sandbox_root_swallowing_the_home_is_explained(self, home, monkeypatch):
        monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(home.parent))
        r = build_report(home)
        assert r.removable == []
        assert "TERMINAL_SANDBOX_DIR" in format_report(r)

    def test_a_healthy_tree_gets_no_excuses(self, home):
        r = build_report(home)
        assert r.unreadable == 0
        assert r.notes == ()


# ---------------------------------------------------------------------------
# Раунд 2, пункт 7: формат под Telegram
# ---------------------------------------------------------------------------


class TestTelegramShape:
    def test_no_line_is_aligned_with_padding(self):
        text = format_report(
            _report(
                removable=[
                    RemovableItem("медиакэш", Path("/x/cache/images"), 3 * GB),
                    RemovableItem("журналы", Path("/x/logs"), 1 * GB),
                ],
            )
        )
        for line in text.splitlines():
            assert "  " not in line, line
            assert not line.startswith(" "), line

    def test_sizes_use_one_precision_rule(self):
        assert "." not in _size(12 * GB)  # десять и больше — целое
        assert "." in _size(6 * GB + GB // 4)  # меньше десяти — один знак
        for value in (0, 1024, 5 * GB, 12 * GB, 900 * 1024 ** 2):
            assert not _size(value).split()[0].endswith(".0"), value

    def test_service_line_explains_itself(self):
        line = _line_starting(format_report(_report()), "• служебное")
        assert "(" in line and ")" in line
        assert len(line.split("(")[1]) > 10
