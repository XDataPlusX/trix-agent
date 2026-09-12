"""Файл проектного контекста может называться именем продукта.

До этой правки агент читал только `.hermes.md`/`HERMES.md`. Клиенту
пришлось бы завести файл с именем чужого продукта, чтобы его контекст
вообще прочитали, — а скиллы, называющие этот файл, при переименовании
стали бы советовать имя, которого ядро не знает.

Апстримные имена продолжают работать: их пишут существующие проекты, и
молча перестать их читать значит потерять контекст без единого
сообщения.
"""

import pytest

from agent.prompt_builder import _find_hermes_md


class TestFilenameDiscovery:
    @pytest.mark.parametrize("name", [".trix.md", "TRIX.md", ".hermes.md", "HERMES.md"])
    def test_every_supported_name_is_found(self, tmp_path, name):
        (tmp_path / name).write_text("context")
        found = _find_hermes_md(tmp_path)
        assert found is not None and found.name == name

    def test_product_name_wins_when_both_are_present(self, tmp_path):
        """В каталоге с обоими файлами клиент вправе ожидать, что читают его."""
        (tmp_path / "TRIX.md").write_text("свой")
        (tmp_path / "HERMES.md").write_text("чужой")
        assert _find_hermes_md(tmp_path).read_text() == "свой"

    def test_dotted_form_wins_over_uppercase(self, tmp_path):
        """Порядок внутри пары сохраняет прежнее поведение апстрима."""
        (tmp_path / ".trix.md").write_text("точка")
        (tmp_path / "TRIX.md").write_text("капс")
        assert _find_hermes_md(tmp_path).read_text() == "точка"

    def test_nothing_found_returns_none(self, tmp_path):
        assert _find_hermes_md(tmp_path) is None

    def test_walks_up_inside_a_repository(self, tmp_path):
        """Вверх идём только внутри git-репозитория — это намеренно.

        Без корня репозитория поиск ограничен текущим каталогом, чтобы
        не подхватить чужой файл, оставленный в /tmp или /home.
        """
        (tmp_path / ".git").mkdir()
        (tmp_path / "TRIX.md").write_text("сверху")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        found = _find_hermes_md(nested)
        assert found is not None and found.read_text() == "сверху"

    def test_does_not_walk_up_outside_a_repository(self, tmp_path):
        (tmp_path / "TRIX.md").write_text("сверху")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert _find_hermes_md(nested) is None
