"""Полный шаблон и мост ``terminal.*`` → переменные окружения.

Переход на полный клиентский шаблон (сентябрь 2026, решение владельца)
задел этот мост не очевидным образом, и оба следствия проверяются здесь
исполнением, а не чтением кода.

``apply_terminal_config_to_env`` считает ключ «явным», если он есть в СЫРОМ
``config.yaml``; явный ключ перекрывает уже стоящую переменную окружения, а
унаследованный от ``DEFAULT_CONFIG`` — только доливает отсутствующую. Пока
шаблон был дельтой, явными были пять наших ключей, и любую другую
``TERMINAL_*`` из ``.env`` файл молча проигрывал. В полном шаблоне явны все
29 — то есть:

1. **Файл стал главнее ``.env``.** Это то самое правило, которое шаблон уже
   объявляет клиенту в разделе про таймауты Телеграма («настройка, которую
   видно в файле, не должна молча проигрывать строке из .env»), и до
   полного конфига оно для ``terminal.*`` не выполнялось.
2. **Поведение песочницы при этом не изменилось ни на байт** — потому что
   значения, которые теперь выписаны в файле, и есть те дефолты, которые
   раньше подставлялись из кода. Это главное, что проверяет второй тест:
   полный конфиг обязан быть документацией существующего поведения, а не
   его тихой сменой.

Отдельно проверяется, что пять булевых настроек, которые теперь ЭКСПОРТИРУЮТСЯ
(раньше их в окружении не было вовсе), читаются правильно. Питон
сериализует ``False`` как строку ``"False"``, и потребитель вида
``bool(os.getenv(...))`` увидел бы в ней истину — в случае
``docker_mount_cwd_to_workspace`` это открыло бы рабочий каталог хоста в
песочницу. Все потребители сравнивают через ``.lower() in {...}``; тест
держит это свойство под наблюдением со стороны наблюдаемого поведения.
"""

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


@pytest.fixture
def client_home(tmp_path, monkeypatch):
    """Временный HERMES_HOME с ПОСТАВЛЯЕМЫМ шаблоном в роли config.yaml."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _bridged_env(extra_env: dict) -> dict:
    """Мост в СВОЙ словарь, никогда в ``os.environ`` процесса.

    Вызов без аргумента (`apply_terminal_config_to_env()`) пишет прямо в
    `os.environ`, а `monkeypatch` такие записи откатить не может — значения
    `TERMINAL_*` протекали бы из теста в тест внутри файла.
    """
    from hermes_cli.config import apply_terminal_config_to_env

    env = dict(os.environ)
    env.update(extra_env)
    apply_terminal_config_to_env(env=env)
    return env


def _sandbox_config(env: dict) -> dict:
    """Эффективный конфиг песочницы, собранный из ПЕРЕДАННОГО окружения."""
    from tools.terminal_tool import _get_env_config

    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        return _get_env_config()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_the_file_wins_over_a_terminal_env_var(client_home):
    """Значение из файла не проигрывает строке из .env."""
    env = _bridged_env({"TERMINAL_TIMEOUT": "60", "TERMINAL_DOCKER_IMAGE": "mine:latest"})

    assert env["TERMINAL_TIMEOUT"] == "180", (
        "TERMINAL_TIMEOUT из .env перекрыл значение, выписанное в config.yaml — "
        "клиент читает в файле одно, а работает другое"
    )
    assert env["TERMINAL_DOCKER_IMAGE"] != "mine:latest"


def test_our_own_decisions_reach_the_sandbox(client_home):
    """Наши продуктовые решения доезжают до эффективного конфига песочницы."""
    cfg = _sandbox_config(_bridged_env({}))

    assert cfg["env_type"] == "docker"
    assert cfg["cwd"] == "/workspace"
    assert cfg["container_memory"] == 3072
    assert cfg["container_cpu"] == 2
    assert "18000-18009:18000-18009" in cfg["docker_extra_args"]


def test_the_full_config_does_not_open_the_host_cwd_into_the_sandbox(client_home):
    """``docker_mount_cwd_to_workspace: false`` обязан остаться ложью.

    Он экспортируется строкой ``"False"``. Потребитель, читающий её как
    ``bool("False")``, получил бы истину и смонтировал рабочий каталог хоста
    в ``/workspace`` — ровно та подмена, из-за которой этот ключ вообще под
    присмотром.
    """
    env = _bridged_env({})
    assert env["TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE"] == "False"
    assert _sandbox_config(env)["docker_mount_cwd_to_workspace"] is False


@pytest.mark.parametrize(
    "env_var, config_key, expected",
    [
        ("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "docker_mount_cwd_to_workspace", False),
        ("TERMINAL_DOCKER_NETWORK", "docker_network", True),
        ("TERMINAL_CONTAINER_PERSISTENT", "container_persistent", True),
        ("TERMINAL_DOCKER_RUN_AS_HOST_USER", "docker_run_as_host_user", False),
    ],
)
def test_python_style_booleans_survive_the_round_trip(
    client_home, env_var, config_key, expected
):
    """``True``/``False`` с большой буквы читаются как булевы, не как строки."""
    env = _bridged_env({})
    assert env[env_var] in {"True", "False"}, (
        f"{env_var}={env[env_var]!r} — тест написан под питоновскую запись; "
        "если формат сменился, проверьте потребителей заново"
    )
    assert _sandbox_config(env)[config_key] is expected
