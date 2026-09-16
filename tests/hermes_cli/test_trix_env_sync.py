"""Досев справочной части ``.env`` на уже работающие машины.

Пробел найден живой проверкой после релиза 0.1.30: ``config.yaml`` пришёл к
полному паритету со свежей установкой, а ``.env`` остался на 54 строках —
шаблон ``.env`` кладётся только когда файла нет вовсе.
"""

import os
import stat
from pathlib import Path

import pytest
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix.env.example"

#: Кураторская часть, какой она уехала клиенту в прошлых релизах.
_OLD_CLIENT_ENV = (
    "# Trix Agent — секреты и ключи.\n"
    "\n"
    "TELEGRAM_BOT_TOKEN=\n"
    "TELEGRAM_ALLOWED_USERS=\n"
    "OPENROUTER_API_KEY=my-real-key\n"
    "\n"
    "# TELEGRAM_PROXY=\n"
    "NO_PROXY=localhost,127.0.0.1\n"
)


@pytest.fixture
def client_env(tmp_path):
    path = tmp_path / ".env"
    path.write_text(_OLD_CLIENT_ENV, encoding="utf-8")
    return path


def test_the_full_list_reaches_an_existing_machine(client_env):
    from hermes_cli.trix_env_sync import MARKER, sync_missing_env_documentation

    added = sync_missing_env_documentation(client_env, TEMPLATE)

    assert added > 400, f"дописано всего {added} строк"
    text = client_env.read_text(encoding="utf-8")
    assert MARKER in text
    # выборочно: переменные из разных групп доехали
    for name in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "NOTION_API_KEY"):
        assert f"# {name}=" in text, name


def test_not_a_single_existing_line_changes(client_env):
    """Старый файл — префикс нового: только добавления."""
    from hermes_cli.trix_env_sync import sync_missing_env_documentation

    before = client_env.read_text(encoding="utf-8")
    sync_missing_env_documentation(client_env, TEMPLATE)

    assert client_env.read_text(encoding="utf-8").startswith(before)


def test_nothing_the_client_filled_in_is_touched(client_env):
    """Значения клиента — дословно те же, и новых активных строк нет.

    Это главное свойство: за ``.env`` не стоит DEFAULT_CONFIG, поэтому
    закомментированная переменная и отсутствующая — одно и то же, и досев по
    построению не может изменить поведение. Проверяется, а не обещается.
    """
    from hermes_cli.trix_env_sync import sync_missing_env_documentation

    before = dotenv_values(str(client_env))
    sync_missing_env_documentation(client_env, TEMPLATE)
    after = dotenv_values(str(client_env))

    assert after == before, f"набор активных переменных изменился: {after}"
    assert after["OPENROUTER_API_KEY"] == "my-real-key"


def test_every_appended_line_is_a_comment(client_env):
    """Ни одной живой строки в дописанном хвосте."""
    from hermes_cli.trix_env_sync import sync_missing_env_documentation

    before = client_env.read_text(encoding="utf-8")
    sync_missing_env_documentation(client_env, TEMPLATE)
    appended = client_env.read_text(encoding="utf-8")[len(before):]

    live = [
        line
        for line in appended.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not live, f"в дописанном хвосте живые строки: {live[:5]}"


def test_a_second_run_changes_nothing(client_env):
    from hermes_cli.trix_env_sync import sync_missing_env_documentation

    assert sync_missing_env_documentation(client_env, TEMPLATE) > 0
    after_first = client_env.read_text(encoding="utf-8")
    assert sync_missing_env_documentation(client_env, TEMPLATE) == 0
    assert client_env.read_text(encoding="utf-8") == after_first


def test_a_fresh_install_is_left_alone(tmp_path):
    """У свежей установки список уже есть — досев молчит."""
    from hermes_cli.trix_env_sync import sync_missing_env_documentation

    path = tmp_path / ".env"
    path.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    assert sync_missing_env_documentation(path, TEMPLATE) == 0
    assert path.read_text(encoding="utf-8") == before


def test_crlf_file_gets_no_stray_newline(tmp_path):
    from hermes_cli.trix_env_sync import sync_missing_env_documentation

    path = tmp_path / ".env"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(_OLD_CLIENT_ENV.replace("\n", "\r\n"))

    assert sync_missing_env_documentation(path, TEMPLATE) > 0

    data = path.read_bytes()
    assert b"\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b""), "появился голый \\n"


def test_a_file_without_a_trailing_newline_still_gets_a_clean_join(tmp_path):
    from hermes_cli.trix_env_sync import MARKER, sync_missing_env_documentation

    path = tmp_path / ".env"
    path.write_text("OPENROUTER_API_KEY=x", encoding="utf-8")

    assert sync_missing_env_documentation(path, TEMPLATE) > 0

    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "OPENROUTER_API_KEY=x"
    assert MARKER in text
    assert dotenv_values(str(path))["OPENROUTER_API_KEY"] == "x"


def test_a_read_only_env_is_left_untouched(client_env):
    from hermes_cli.trix_env_sync import sync_missing_env_documentation

    before = client_env.read_bytes()
    client_env.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    if os.access(client_env, os.W_OK):
        pytest.skip("запущено с правами, для которых 444 не запрет")

    assert sync_missing_env_documentation(client_env, TEMPLATE) == 0
    assert client_env.read_bytes() == before


def test_a_missing_file_or_template_is_a_no_op(tmp_path):
    from hermes_cli.trix_env_sync import sync_missing_env_documentation

    assert sync_missing_env_documentation(tmp_path / "nope", TEMPLATE) == 0
    path = tmp_path / ".env"
    path.write_text("A=1\n", encoding="utf-8")
    assert sync_missing_env_documentation(path, tmp_path / "no-template") == 0
    assert path.read_text(encoding="utf-8") == "A=1\n"


def test_a_value_the_client_already_set_still_wins_after_the_sync(tmp_path, monkeypatch):
    """Дописанный закомментированный дубль инертен и не перехватывает запись.

    После досева в файле может оказаться и живая строка клиента, и её
    закомментированный дубль из справочной части — так бывает для переменной,
    которой нет в нашей кураторской части (её мастер дописывает в конец сам).
    Свойство, на котором это держится: ``_env_line_defines_key`` матчит только
    строки БЕЗ решётки, а живая строка клиента стоит выше дописанного хвоста.
    Пинуем это явно: иначе запись значения могла бы уехать в комментарий, а
    настоящее значение остаться прежним.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import get_env_path, load_env, save_env_value
    from hermes_cli.trix_env_sync import sync_missing_env_documentation

    env_path = get_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    # Переменная, которой в кураторской части НЕТ: её мастер дописал сам.
    env_path.write_text("NOTION_API_KEY=secret-preexisting\n", encoding="utf-8")

    assert sync_missing_env_documentation(env_path, TEMPLATE) > 0
    text = env_path.read_text(encoding="utf-8")
    assert "# NOTION_API_KEY=" in text, "дубль в справочной части ожидается"

    assert load_env().get("NOTION_API_KEY") == "secret-preexisting"

    save_env_value("NOTION_API_KEY", "secret-new")

    assert load_env().get("NOTION_API_KEY") == "secret-new"
    lines = env_path.read_text(encoding="utf-8").splitlines()
    live = [l for l in lines if l.startswith("NOTION_API_KEY=")]
    assert live == ["NOTION_API_KEY=secret-new"], (
        f"живых строк должно остаться ровно одна: {live}"
    )
