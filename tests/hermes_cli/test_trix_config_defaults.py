"""Смена НАШЕГО умолчания доезжает до клиента, правка клиента — неприкосновенна.

Правило владельца 2026-09-10, дословно: «изменение клиента превыше всего,
но если он ничего не менял, то мы меняем за него».

Четыре исхода, которые он перечислил:

===========================  ===========================  ==============
клиент трогал ключ           наше умолчание               что делаем
===========================  ===========================  ==============
да, совпало с умолчанием     любое                        не трогаем
да, не совпало               любое                        не трогаем
нет                          не менялось                  нечего делать
нет                          изменилось                   **обновляем**
===========================  ===========================  ==============

**Почему нельзя обойтись сравнением с текущим умолчанием.** «Значение
клиента отличается от шаблона» означает ровно две несовместимые вещи:
клиент правил ключ ЛИБО мы поменяли умолчание, а клиент остался на старом.
Первое трогать нельзя, второе — обязаны. Различить их можно только помня,
что мы этой машине отгружали. Память живёт рядом с отметками досева, но в
своём файле: у досева формат «путь → когда дописан», и подмешивать туда
второй смысл значило бы сломать его собственную проверку.

**Первый прогон на уже работающей машине** ничего не меняет: отгруженного
значения мы не помним, а угадывать — значит рискнуть перетереть правку
клиента. Поэтому совпадающие с шаблоном ключи запоминаются как наши, а
разошедшиеся сразу объявляются клиентскими. Работать по-настоящему
механизм начинает со СЛЕДУЮЩЕЙ смены умолчания — то же свойство, что у
любой правки пути обновления (STATUS_release §6a).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "hermes-home"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _client(tmp_path: Path, body: str) -> Path:
    return _write(tmp_path / "config.yaml", body)


def _template(tmp_path: Path, body: str) -> Path:
    return _write(tmp_path / "trix-config.yaml", body)


def _baselines(home: Path) -> dict:
    from hermes_cli.trix_config_defaults import BASELINE_STATE_FILENAME

    p = home / BASELINE_STATE_FILENAME
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _value(path: Path, *keys):
    node = yaml.safe_load(path.read_text(encoding="utf-8"))
    for k in keys:
        node = node[k]
    return node


class TestFirstRunRecordsAndChangesNothing:
    def test_matching_value_is_remembered_as_ours(self, tmp_path, home):
        from hermes_cli.trix_config_defaults import apply_default_changes

        client = _client(tmp_path, "display:\n  show_reasoning: true\n")
        template = _template(tmp_path, "display:\n  show_reasoning: true\n")

        updated, skipped = apply_default_changes(client, template)

        assert updated == []
        assert _value(client, "display", "show_reasoning") is True
        assert _baselines(home)["display.show_reasoning"] == {"shipped": True}

    def test_diverged_value_is_declared_the_clients_immediately(self, tmp_path, home):
        """Кто его menял — мы или клиент — на первом прогоне неизвестно.
        Гадать нельзя: ошибка в одну сторону перетрёт правку клиента."""
        from hermes_cli.trix_config_defaults import apply_default_changes

        client = _client(tmp_path, "display:\n  show_reasoning: false\n")
        template = _template(tmp_path, "display:\n  show_reasoning: true\n")

        updated, _ = apply_default_changes(client, template)

        assert updated == []
        assert _value(client, "display", "show_reasoning") is False
        assert _baselines(home)["display.show_reasoning"] == {"owned_by_client": True}


class TestTheFourOutcomes:
    def _seed(self, home: Path, path: str, record: dict) -> None:
        from hermes_cli.trix_config_defaults import BASELINE_STATE_FILENAME

        (home / BASELINE_STATE_FILENAME).write_text(
            json.dumps({path: record}), encoding="utf-8"
        )

    def test_we_changed_the_default_and_client_never_touched_it(self, tmp_path, home):
        """Единственный исход, в котором мы что-то меняем."""
        from hermes_cli.trix_config_defaults import apply_default_changes

        self._seed(home, "display.show_reasoning", {"shipped": True})
        client = _client(tmp_path, "display:\n  show_reasoning: true\n")
        template = _template(tmp_path, "display:\n  show_reasoning: false\n")

        updated, _ = apply_default_changes(client, template)

        assert updated == ["display.show_reasoning"]
        assert _value(client, "display", "show_reasoning") is False
        assert _baselines(home)["display.show_reasoning"] == {"shipped": False}

    def test_client_edited_it_so_we_keep_out(self, tmp_path, home):
        from hermes_cli.trix_config_defaults import apply_default_changes

        self._seed(home, "display.show_reasoning", {"shipped": True})
        client = _client(tmp_path, "display:\n  show_reasoning: false\n")
        template = _template(tmp_path, "display:\n  show_reasoning: true\n")

        updated, _ = apply_default_changes(client, template)

        assert updated == []
        assert _value(client, "display", "show_reasoning") is False

    def test_client_edit_is_remembered_forever(self, tmp_path, home):
        """«Тронул — навсегда»: следующая смена умолчания его тоже не тронет."""
        from hermes_cli.trix_config_defaults import apply_default_changes

        self._seed(home, "agent.max_turns", {"shipped": 500})
        client = _client(tmp_path, "agent:\n  max_turns: 90\n")
        template = _template(tmp_path, "agent:\n  max_turns: 500\n")
        apply_default_changes(client, template)
        assert _baselines(home)["agent.max_turns"] == {"owned_by_client": True}

        # Мы снова двигаем умолчание — клиента это уже не касается.
        template = _template(tmp_path, "agent:\n  max_turns: 300\n")
        updated, _ = apply_default_changes(client, template)

        assert updated == []
        assert _value(client, "agent", "max_turns") == 90

    def test_nothing_changed_anywhere(self, tmp_path, home):
        from hermes_cli.trix_config_defaults import apply_default_changes

        self._seed(home, "display.show_reasoning", {"shipped": True})
        client = _client(tmp_path, "display:\n  show_reasoning: true\n")
        template = _template(tmp_path, "display:\n  show_reasoning: true\n")

        assert apply_default_changes(client, template) == ([], [])


class TestTheFileSurvivesIntact:
    def test_only_the_value_changes_comments_stay(self, tmp_path, home):
        """Русские комментарии — это документация, ради которой конфиг и
        существует. Правка значения не имеет права их тронуть."""
        from hermes_cli.trix_config_defaults import apply_default_changes

        body = (
            "# шапка файла\n"
            "display:\n"
            "  # Показывать ли клиенту размышления модели.\n"
            "  # Вторая строка объяснения.\n"
            "  show_reasoning: true  # хвостовой комментарий\n"
            "  streaming: false\n"
        )
        client = _client(tmp_path, body)
        template = _template(tmp_path, "display:\n  show_reasoning: false\n  streaming: false\n")
        (home / "trix_config_baselines.json").write_text(
            json.dumps({"display.show_reasoning": {"shipped": True}}), encoding="utf-8"
        )

        apply_default_changes(client, template)
        after = client.read_text(encoding="utf-8").splitlines()

        assert after[0] == "# шапка файла"
        assert after[2] == "  # Показывать ли клиенту размышления модели."
        assert after[3] == "  # Вторая строка объяснения."
        assert after[4] == "  show_reasoning: false  # хвостовой комментарий"
        assert after[5] == "  streaming: false"

    def test_same_key_name_under_another_parent_is_not_touched(self, tmp_path, home):
        """`enabled` встречается в конфиге десятки раз. Правка обязана идти
        по ПУТИ, а не по имени ключа."""
        from hermes_cli.trix_config_defaults import apply_default_changes

        client = _client(
            tmp_path, "memory:\n  enabled: true\ncron:\n  enabled: true\n"
        )
        template = _template(
            tmp_path, "memory:\n  enabled: false\ncron:\n  enabled: true\n"
        )
        (home / "trix_config_baselines.json").write_text(
            json.dumps({"memory.enabled": {"shipped": True}}), encoding="utf-8"
        )

        updated, _ = apply_default_changes(client, template)

        assert updated == ["memory.enabled"]
        assert _value(client, "memory", "enabled") is False
        assert _value(client, "cron", "enabled") is True


class TestWhatWeRefuseToTouch:
    def test_non_scalar_value_is_skipped_with_a_reason(self, tmp_path, home):
        """Список или словарь одной строкой не переписать, не рискуя файлом."""
        from hermes_cli.trix_config_defaults import apply_default_changes

        (home / "trix_config_baselines.json").write_text(
            json.dumps({"gateway.allow": {"shipped": ["a"]}}), encoding="utf-8"
        )
        client = _client(tmp_path, "gateway:\n  allow:\n    - a\n")
        template = _template(tmp_path, "gateway:\n  allow:\n    - a\n    - b\n")

        updated, skipped = apply_default_changes(client, template)

        assert updated == []
        assert [p for p, _ in skipped] == ["gateway.allow"]

    def test_readonly_client_file_is_left_alone(self, tmp_path, home):
        """Клиент защитил файл руками — уважаем, как это делает досев."""
        import os

        from hermes_cli.trix_config_defaults import apply_default_changes

        (home / "trix_config_baselines.json").write_text(
            json.dumps({"display.show_reasoning": {"shipped": True}}), encoding="utf-8"
        )
        client = _client(tmp_path, "display:\n  show_reasoning: true\n")
        template = _template(tmp_path, "display:\n  show_reasoning: false\n")
        os.chmod(client, 0o444)
        try:
            updated, _ = apply_default_changes(client, template)
        finally:
            os.chmod(client, 0o644)

        assert updated == []
        assert _value(client, "display", "show_reasoning") is True

    def test_broken_template_changes_nothing(self, tmp_path, home):
        from hermes_cli.trix_config_defaults import apply_default_changes

        client = _client(tmp_path, "display:\n  show_reasoning: true\n")
        template = _template(tmp_path, "display:\n  : : не yaml\n")

        assert apply_default_changes(client, template) == ([], [])
        assert _value(client, "display", "show_reasoning") is True

    def test_key_absent_from_the_client_is_left_to_the_seeder(self, tmp_path, home):
        """Дописывание отсутствующего — работа trix_config_sync, не наша."""
        from hermes_cli.trix_config_defaults import apply_default_changes

        client = _client(tmp_path, "display:\n  streaming: false\n")
        template = _template(tmp_path, "display:\n  streaming: false\n  show_reasoning: false\n")

        updated, _ = apply_default_changes(client, template)

        assert updated == []
        assert "display.show_reasoning" not in _baselines(home)


class TestResultIsVerifiedBeforeItIsWritten:
    def test_a_rewrite_that_would_break_the_file_is_discarded(
        self, tmp_path, home, monkeypatch
    ):
        """Итог перечитывается разбором YAML. Не разобралось или изменилось
        не то — файл остаётся прежним."""
        from hermes_cli import trix_config_defaults as mod

        (home / "trix_config_baselines.json").write_text(
            json.dumps({"display.show_reasoning": {"shipped": True}}), encoding="utf-8"
        )
        client = _client(tmp_path, "display:\n  show_reasoning: true\n")
        template = _template(tmp_path, "display:\n  show_reasoning: false\n")

        monkeypatch.setattr(
            mod, "_rewrite_scalar", lambda lines, idx, value: ["display:", "  : : сломано"]
        )

        updated, skipped = mod.apply_default_changes(client, template)

        assert updated == []
        assert _value(client, "display", "show_reasoning") is True
        assert skipped and skipped[0][0] == "display.show_reasoning"
