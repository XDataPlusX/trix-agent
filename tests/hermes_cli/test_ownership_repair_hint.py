"""Предупреждение о чужом каталоге обязано давать выход, а не тупик.

Когда `hermes update` не может почистить `__pycache__` из-за прав, он
честно об этом говорит (это уже проверено отдельно). Но честность без
подсказки оставляет клиента наедине с проблемой, которую он не заводил:
дерево стало смешанным по владельцу из-за шагов установки, работавших от
root. Здесь проверяется, что вывод содержит команду, которую можно
скопировать и выполнить, и что она устоит на неудобных путях и именах.
"""

from pathlib import Path

import pytest

from hermes_cli.trix_ownership_hint import (
    ownership_repair_command,
    ownership_repair_lines,
)


def test_command_returns_the_tree_to_the_named_owner():
    cmd = ownership_repair_command("/usr/local/lib/hermes-agent", "user")
    assert cmd == "sudo chown -R user:user /usr/local/lib/hermes-agent"


def test_command_accepts_a_path_object():
    """Вызов идёт из кода, который держит каталог как Path, не как строку."""
    from_path = ownership_repair_command(Path("/opt/trix"), "client")
    from_str = ownership_repair_command("/opt/trix", "client")
    assert from_path == from_str


@pytest.mark.parametrize(
    "hostile",
    [
        "/opt/trix agent",          # пробел
        "/opt/$(whoami)",           # подстановка команды
        "/opt/trix;rm -rf /",       # разделитель команд
        "/opt/trix'\"quoted",       # обе кавычки разом
    ],
)
def test_hostile_paths_stay_a_single_argument(hostile):
    """Путь установки не мы выбираем — экранирование обязано держать удар.

    Команду печатают, чтобы человек её скопировал: если путь развалится
    на несколько аргументов или подставит чужую команду, скопировавший
    выполнит не то, что написано.
    """
    cmd = ownership_repair_command(hostile, "user")
    assert cmd.startswith("sudo chown -R user:user ")
    argument = cmd[len("sudo chown -R user:user "):]

    import shlex

    assert shlex.split(argument) == [hostile]


def test_hostile_owner_name_is_escaped_too():
    cmd = ownership_repair_command("/opt/trix", "od d")
    import shlex

    parts = shlex.split(cmd)
    assert parts[:3] == ["sudo", "chown", "-R"]
    assert parts[3] == "od d:od d"


def test_lines_carry_the_runnable_command():
    lines = ownership_repair_lines("/usr/local/lib/hermes-agent", "user")
    joined = "\n".join(lines)
    assert ownership_repair_command("/usr/local/lib/hermes-agent", "user") in joined


def test_lines_warn_about_the_password_prompt():
    """Команда просит пароль машины — про это надо предупредить заранее."""
    joined = "\n".join(ownership_repair_lines("/opt/trix", "user")).lower()
    assert "пароль" in joined


def test_lines_are_indented_to_sit_under_the_warning():
    """Блок печатается внутри отчёта об обновлении, а не с края экрана."""
    for line in ownership_repair_lines("/opt/trix", "user"):
        assert line.startswith("    ")


def test_update_prints_the_command_when_a_directory_cannot_be_cleared(
    tmp_path, capsys, monkeypatch
):
    """Сквозная проверка: непочищаемый каталог доводит команду до экрана.

    Отдельные проверки выше говорят про строку. Эта — про то, что строка
    доходит до клиента именно в том месте, где он про проблему узнаёт:
    в отчёте `hermes update` о чистке байт-кода.
    """
    import os

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("от root права не запрещают ничего — ветка отказа недостижима")

    from hermes_cli import main as hermes_main

    root = tmp_path / "hermes-agent"
    locked_parent = root / "gateway"
    cache = locked_parent / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "run.cpython-311.pyc").write_bytes(b"stale")

    # Каталог, который нельзя менять — ровно то, что даёт чужой владелец.
    original_mode = locked_parent.stat().st_mode
    os.chmod(locked_parent, 0o555)
    try:
        hermes_main._clear_bytecode_cache(root)
        printed = capsys.readouterr().out
    finally:
        os.chmod(locked_parent, original_mode)

    assert "sudo chown -R" in printed
    assert str(root) in printed
