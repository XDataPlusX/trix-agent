"""Точечный досев тулсета доезжает до машины клиента.

Спека 20, Ruling 7. Сам по себе ``sync_toolset_list_entries`` ничего не
меняет на живой машине: у клиента нет ни терминала, ни повода звать
функцию руками. Доставку делают ровно два канала — ``hermes update`` и
``hermes doctor --fix``, и оба до этой правки звали только общий досев,
который список внутри существующего ключа не видит по построению.

Поэтому тесты здесь дергают ИМЕННО обёртки этих двух каналов, а не
функцию досева: проверяется проводка, а не механизм (механизм покрыт в
``test_trix_toolset_entry_sync.py``). Без такой проверки миграция была бы
мёртвым кодом, который зелёные тесты соседнего файла не отличают от
живого.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _client_config_without_mcp(path: Path) -> Path:
    """Форма машины, установленной ДО спеки 20: список тулсетов есть, mcp в нём нет."""
    path.write_text(
        "platform_toolsets:\n"
        "  # Набор инструментов, доступных агенту в Telegram.\n"
        "  telegram:\n"
        "    - terminal          # выполнение команд в песочнице\n"
        "    - file              # чтение, запись, правка, поиск по файлам\n"
        "    - secrets           # агент сам спросит недостающий ключ в чате\n"
        "\n"
        "_config_version: 35\n",
        encoding="utf-8",
    )
    return path


def _telegram_toolsets(path: Path) -> list:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {})[
        "platform_toolsets"
    ]["telegram"]


@pytest.fixture
def client_config(tmp_path):
    return _client_config_without_mcp(tmp_path / "config.yaml")


def test_doctor_fix_delivers_the_toolset(client_config, monkeypatch):
    """``hermes doctor --fix`` дописывает недостающий тулсет в живой конфиг."""
    from hermes_cli import doctor

    monkeypatch.setattr(doctor, "check_ok", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(doctor, "check_warn", lambda *a, **k: None, raising=False)

    doctor._sync_trix_config_sections(client_config, REPO_ROOT)

    assert "mcp" in _telegram_toolsets(client_config)


def test_doctor_fix_is_idempotent(client_config, monkeypatch):
    """Второй прогон того же канала список не удлиняет."""
    from hermes_cli import doctor

    monkeypatch.setattr(doctor, "check_ok", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(doctor, "check_warn", lambda *a, **k: None, raising=False)

    doctor._sync_trix_config_sections(client_config, REPO_ROOT)
    first = _telegram_toolsets(client_config)
    doctor._sync_trix_config_sections(client_config, REPO_ROOT)
    second = _telegram_toolsets(client_config)

    assert first == second


def test_update_delivers_the_toolset(client_config, monkeypatch):
    """``hermes update`` дописывает недостающий тулсет тем же проходом."""
    from hermes_cli import update_cmd
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "get_config_path", lambda: client_config)
    monkeypatch.setattr(
        config_mod, "get_project_root", lambda: REPO_ROOT, raising=False
    )

    update_cmd._sync_trix_config_sections(quiet=True)

    assert "mcp" in _telegram_toolsets(client_config)


def test_delivery_keeps_what_the_client_already_had(client_config, monkeypatch):
    """Доставка ничего не отнимает: прежние тулсеты остаются на месте."""
    from hermes_cli import doctor

    monkeypatch.setattr(doctor, "check_ok", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(doctor, "check_warn", lambda *a, **k: None, raising=False)

    before = set(_telegram_toolsets(client_config))
    doctor._sync_trix_config_sections(client_config, REPO_ROOT)
    after = set(_telegram_toolsets(client_config))

    assert before <= after
