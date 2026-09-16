"""Точечный досев тулсета в уже существующий platform_toolsets.<платформа>.

Отдельный от общего досева случай (спека 20, Ruling 7): у клиента ключ
``platform_toolsets.telegram`` существует с самой первой поставки (спека
9), а значит для sync_missing_client_sections список внутри него — "уже на
месте" (сравнение идёт по наличию КЛЮЧА, не по содержимому списка). Новый
тулсет (``mcp``) в этот список общий механизм никогда не допишет — нужна
sync_toolset_list_entries.

Требования те же, что и у общего досева: дописываем только отсутствующее,
не трогая ни одной существующей строки, вместе с комментарием, и не
воскрешая то, что клиент удалил сам.
"""

import difflib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


def _old_lines_are_untouched(before: str, after: str) -> bool:
    """True, если новый файл отличается от старого ТОЛЬКО добавлениями."""
    diff = difflib.ndiff(before.splitlines(), after.splitlines())
    return all(not line.startswith(("-", "?")) for line in diff)


def _skipped_reasons(skipped) -> set:
    return {reason for _, reason in skipped}


@pytest.fixture
def client_config(tmp_path):
    """Клиентский config.yaml с уже доставленным (спека 9/19) списком
    тулсетов Telegram, но БЕЗ mcp — ровно форма машины, установленной до
    спеки 20."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "platform_toolsets:\n"
        "  # Набор инструментов, доступных агенту в Telegram.\n"
        "  # Чтобы вернуть что-то выключенное — допишите строку и перезапустите агента.\n"
        "  telegram:\n"
        "    - terminal          # выполнение команд в песочнице\n"
        "    - file              # чтение, запись, правка, поиск по файлам\n"
        "    - secrets           # агент сам спросит недостающий ключ в чате\n"
        "    # Тулсет \"web\" заменён на \"search\": извлечение страниц требует\n"
        "    # платного ключа.\n"
        "\n"
        "platforms:\n"
        "  telegram:\n"
        "    require_mention: false\n"
        "\n"
        "_config_version: 35\n",
        encoding="utf-8",
    )
    return path


def test_appends_missing_toolset_to_existing_list(client_config):
    from hermes_cli.trix_config_sync import sync_toolset_list_entries

    added, skipped = sync_toolset_list_entries(client_config)

    assert added == ["platform_toolsets.telegram[mcp]"]
    assert skipped == []

    data = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    toolsets = data["platform_toolsets"]["telegram"]
    assert toolsets == ["terminal", "file", "secrets", "mcp"], toolsets
    # Остальная форма файла — не только этот список — не пострадала.
    assert data["platforms"]["telegram"]["require_mention"] is False


def test_second_run_changes_nothing(client_config):
    from hermes_cli.trix_config_sync import sync_toolset_list_entries

    sync_toolset_list_entries(client_config)
    after_first = client_config.read_text(encoding="utf-8")

    added2, skipped2 = sync_toolset_list_entries(client_config)

    assert added2 == []
    assert skipped2 == []
    assert client_config.read_text(encoding="utf-8") == after_first


def test_already_present_toolset_is_left_alone(client_config):
    """Клиент, который сам вписал "mcp" (или свежая установка из шаблона),
    не получает второй копии — идемпотентность и без sidecar-состояния."""
    from hermes_cli.trix_config_sync import sync_toolset_list_entries

    text = client_config.read_text(encoding="utf-8").replace(
        "    - secrets           # агент сам спросит недостающий ключ в чате\n",
        "    - secrets           # агент сам спросит недостающий ключ в чате\n"
        "    - mcp\n",
    )
    client_config.write_text(text, encoding="utf-8")
    before = client_config.read_text(encoding="utf-8")

    added, skipped = sync_toolset_list_entries(client_config)

    assert added == []
    assert skipped == []
    assert client_config.read_text(encoding="utf-8") == before


def test_comments_travel_and_no_existing_line_changes(client_config):
    from hermes_cli.trix_config_sync import sync_toolset_list_entries

    before = client_config.read_text(encoding="utf-8")
    sync_toolset_list_entries(client_config)
    after = client_config.read_text(encoding="utf-8")

    assert _old_lines_are_untouched(before, after), (
        "врезка изменила или удалила существующую строку клиентского файла"
    )
    # Комментарий про исключённый "web" тулсет — единственная документация
    # этого решения для клиента — обязан пережить врезку.
    assert 'Тулсет "web" заменён на "search"' in after
    # Новая строка тоже документирована, а не голое имя без объяснения.
    assert "mcp" in after and "# агент подключает MCP-серверы" in after


def test_config_without_platform_toolsets_section_does_not_crash(tmp_path):
    from hermes_cli.trix_config_sync import sync_toolset_list_entries

    path = tmp_path / "config.yaml"
    path.write_text("_config_version: 35\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    added, skipped = sync_toolset_list_entries(path)

    assert added == []
    assert skipped == []
    assert path.read_text(encoding="utf-8") == before


def test_config_with_platform_toolsets_but_no_telegram_key_does_not_crash(tmp_path):
    from hermes_cli.trix_config_sync import sync_toolset_list_entries

    path = tmp_path / "config.yaml"
    path.write_text(
        "platform_toolsets:\n"
        "  discord:\n"
        "    - terminal\n"
        "\n"
        "_config_version: 35\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")

    added, skipped = sync_toolset_list_entries(path)

    assert added == []
    assert skipped == []
    assert path.read_text(encoding="utf-8") == before


def test_telegram_toolset_list_replaced_by_non_list_is_skipped_gracefully(tmp_path):
    """Клиент выключил все тулсеты Telegram целиком (``telegram:`` пуст /
    не список) — вставлять элементы некуда, форме клиента противоречить
    нельзя, файл остаётся как есть."""
    from hermes_cli.trix_config_sync import sync_toolset_list_entries

    path = tmp_path / "config.yaml"
    path.write_text(
        "platform_toolsets:\n"
        "  telegram:\n"
        "\n"
        "_config_version: 35\n",
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")

    added, skipped = sync_toolset_list_entries(path)

    assert added == []
    assert path.read_text(encoding="utf-8") == before


def test_deliberately_removed_toolset_is_not_resurrected(client_config):
    """Клиент, стерший ранее дописанный "mcp" вручную, не должен видеть,
    как следующий прогон возвращает его обратно — тот же приём, что
    защищает от воскрешения в sync_missing_client_sections."""
    from hermes_cli.trix_config_sync import (
        _SKIP_DELETED_BY_CLIENT,
        sync_toolset_list_entries,
    )

    added, _ = sync_toolset_list_entries(client_config)
    assert added == ["platform_toolsets.telegram[mcp]"]
    data = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    assert "mcp" in data["platform_toolsets"]["telegram"]

    # Клиент вручную убирает строку "- mcp".
    lines = [
        line
        for line in client_config.read_text(encoding="utf-8").splitlines()
        if line.strip().split("#")[0].strip() != "- mcp"
    ]
    client_config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    data_after_removal = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    assert "mcp" not in data_after_removal["platform_toolsets"]["telegram"]

    added2, skipped2 = sync_toolset_list_entries(client_config)

    assert added2 == []
    assert _SKIP_DELETED_BY_CLIENT in _skipped_reasons(skipped2)
    data_final = yaml.safe_load(client_config.read_text(encoding="utf-8"))
    assert "mcp" not in data_final["platform_toolsets"]["telegram"], (
        "тулсет, сознательно удалённый клиентом, не должен воскресать"
    )


def test_real_template_delivers_cleanly_to_a_pre_spec20_machine():
    """Сквозной тест на настоящем шаблоне: имитируем машину, установленную
    до спеки 20 (в списке нет "mcp"), и проверяем, что миграция дописывает
    его же способом, каким сам шаблон его вводит — без единой затронутой
    существующей строки."""
    from hermes_cli.trix_config_sync import sync_toolset_list_entries

    template_text = TEMPLATE.read_text(encoding="utf-8")
    old_client_text = template_text.replace(
        "    - mcp               # агент подключает MCP-серверы по вашей просьбе в чате\n",
        "",
    )
    assert old_client_text != template_text, "шаблон уже не содержит ожидаемую строку"
    assert "- mcp" not in old_client_text

    return _run_against(old_client_text, sync_toolset_list_entries)


def _run_against(text: str, fn):
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.yaml"
        path.write_text(text, encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        added, skipped = fn(path)

        assert added == ["platform_toolsets.telegram[mcp]"], (added, skipped)
        after = path.read_text(encoding="utf-8")
        assert _old_lines_are_untouched(before, after)

        data = yaml.safe_load(after)
        assert "mcp" in data["platform_toolsets"]["telegram"]
        return added, skipped
