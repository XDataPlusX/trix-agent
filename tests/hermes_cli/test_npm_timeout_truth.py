"""Остановленный по потолку npm не имеет права обещать целость зависимостей.

Снято с клиентской машины 81.222.148.68 (2026-09-10). `hermes update`
запустил `npm install`, тот упёрся в 15-минутный потолок и был убит.
Клиент прочитал:

    npm не уложился в 15 мин и был остановлен. Обычно это залипшая
    загрузка одного пакета, а не поломка проекта: код уже обновлён,
    ЗАВИСИМОСТИ NODE ОСТАЛИСЬ ПРЕЖНИМИ. Повторите обновление позже.

Последнее — неправда. `npm install` работает по дереву на месте: убитый
на полпути, он оставляет `node_modules` неполным. На этой машине он унёс
из РАБОЧЕЙ установки пакет `agent-browser`, и агент лишился браузера.
Клиенту при этом сказали, что ничего не изменилось, — и искать было
некому.

Это худший вид отказа: тот, который не выглядит отказом. Ровно тот же
класс, что «Camofox installed» без Camofox и «✓ Update complete!» поверх
умершего распознавания речи.

Тесты держат две вещи: сообщение не обещает целости, и, когда пакеты
действительно пропали, оно называет их поимённо — чтобы следующий
читающий не гадал, а видел.
"""

from __future__ import annotations

import json
from pathlib import Path


def _project(tmp_path: Path, declared: dict, present: list[str]) -> Path:
    """Дерево установки: что объявлено в package.json и что реально лежит."""
    root = tmp_path / "install"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps({"name": "hermes-agent", "dependencies": declared}),
        encoding="utf-8",
    )
    for name in present:
        (root / "node_modules" / name).mkdir(parents=True)
    return root


class TestMissingDeclaredNodeDeps:
    def test_reports_package_npm_removed(self, tmp_path):
        """Ровно случай клиентской машины: объявлен, а на диске его нет."""
        from hermes_cli.main import _missing_declared_node_deps

        root = _project(
            tmp_path,
            {"agent-browser": "0.26.0", "@streamdown/math": "1.0.2"},
            present=["@streamdown/math"],
        )

        assert _missing_declared_node_deps(root) == ["agent-browser"]

    def test_intact_tree_reports_nothing(self, tmp_path):
        from hermes_cli.main import _missing_declared_node_deps

        root = _project(
            tmp_path,
            {"agent-browser": "0.26.0", "@streamdown/math": "1.0.2"},
            present=["agent-browser", "@streamdown/math"],
        )

        assert _missing_declared_node_deps(root) == []

    def test_scoped_package_path_is_understood(self, tmp_path):
        """`@scope/name` лежит двумя каталогами — плоская проверка соврала бы."""
        from hermes_cli.main import _missing_declared_node_deps

        root = _project(tmp_path, {"@askjo/camofox-browser": "1.5.2"}, present=[])

        assert _missing_declared_node_deps(root) == ["@askjo/camofox-browser"]

    def test_hoisted_workspace_dep_counts_as_present(self, tmp_path):
        """npm поднимает зависимости рабочих пространств в корневой
        node_modules. Плоская проверка объявила бы пропавшим всё, что
        объявлено в `ui-tui`/`web`, — то есть завалила бы ложной тревогой
        каждое обновление."""
        from hermes_cli.main import _missing_declared_node_deps

        root = _project(tmp_path, {"agent-browser": "0.26.0"}, present=["ink"])
        workspace = root / "ui-tui"
        workspace.mkdir()
        (workspace / "package.json").write_text(
            json.dumps({"name": "ui-tui", "dependencies": {"ink": "5.0.0"}}),
            encoding="utf-8",
        )

        assert _missing_declared_node_deps(workspace) == []

    def test_unreadable_project_does_not_raise(self, tmp_path):
        """Нет package.json — не повод уронить обновление на отчёте о нём."""
        from hermes_cli.main import _missing_declared_node_deps

        assert _missing_declared_node_deps(tmp_path / "нет-такого") == []


class TestNpmTimeoutMessage:
    def test_does_not_promise_dependencies_are_intact(self):
        """Убитый npm мог поменять дерево — обещать обратное нельзя."""
        from hermes_cli.main import _npm_timeout_message

        text = _npm_timeout_message(900, None)

        assert "остались прежними" not in text
        assert "остановлен" in text

    def test_names_the_packages_that_disappeared(self):
        """Пропажу надо назвать: иначе искать её пойдут вслепую."""
        from hermes_cli.main import _npm_timeout_message

        text = _npm_timeout_message(900, None, missing=["agent-browser"])

        assert "agent-browser" in text

    def test_keeps_the_captured_tail(self):
        """Хвост вывода npm не теряется — по нему видно, на чём встало."""
        from hermes_cli.main import _npm_timeout_message

        text = _npm_timeout_message(900, "npm WARN что-то\n", missing=[])

        assert "npm WARN что-то" in text
