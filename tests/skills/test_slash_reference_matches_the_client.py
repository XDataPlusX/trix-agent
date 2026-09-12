"""Справочник команд не должен обещать то, чего шлюз не пропустит.

Найдено живым прогоном 2026-09-09: на вопрос «сколько я трачу» агент
посоветовал клиенту `/insights`. Команда заблокирована — шлюз её не
исполняет, в меню Telegram её нет (проверено через `getMyCommands`:
32 команды, этой среди них нет). Агент её не выдумал: она была написана
в его собственном справочнике, вместе с ещё двумя десятками
заблокированных и полутора десятками консольных.

То есть блокировка в коде работала, а документация к ней — нет. Клиенту
это выглядит так: агент уверенно называет команду, клиент её набирает и
получает отказ.

Поэтому справочник строится из того же источника, что и блокировка, и
этот тест держит их вместе. Инвариант, а не снимок: он не перечисляет
команды поимённо, а сверяет два множества — добавление новой команды в
продукт его не ломает, а вот расхождение справочника с реальностью
ломает сразу.
"""

import re
from pathlib import Path

import pytest

REFERENCE = (
    Path(__file__).resolve().parent.parent.parent
    / "skills" / "autonomous-ai-agents" / "trix-agent"
    / "references" / "slash-commands.md"
)


@pytest.fixture(scope="module")
def listed() -> set:
    """Команды, перечисленные в справочнике как рабочие.

    Берутся только строки блоков с описаниями (начинаются с ``/``), а не
    любые упоминания в прозе: абзац «чего в этом списке нет» специально
    называет `/start`, `/debug` и `/sethome`, и он не должен ломать
    разбор.
    """
    out = set()
    for line in REFERENCE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^/([a-z][a-z0-9_-]*)", line)
        if m:
            out.add(m.group(1))
    return out


@pytest.fixture(scope="module")
def client_commands() -> set:
    from hermes_cli.commands import COMMAND_REGISTRY
    from hermes_cli.trix_menu import DISABLED_COMMANDS

    return {
        c.name for c in COMMAND_REGISTRY
        if not getattr(c, "cli_only", False) and c.name not in DISABLED_COMMANDS
    }


def test_reference_promises_nothing_the_gateway_blocks(listed):
    """Главное утверждение файла."""
    from hermes_cli.trix_menu import DISABLED_COMMANDS

    offending = sorted(listed & set(DISABLED_COMMANDS))
    assert not offending, (
        "справочник называет заблокированные команды — клиент наберёт их "
        f"и получит отказ: {offending}"
    )


def test_reference_promises_nothing_that_needs_a_console(listed):
    """У клиента нет консоли, значит команды хоста ему не предлагают."""
    from hermes_cli.commands import COMMAND_REGISTRY

    cli_only = {c.name for c in COMMAND_REGISTRY if getattr(c, "cli_only", False)}
    offending = sorted(listed & cli_only)
    assert not offending, f"справочник называет консольные команды: {offending}"


def test_reference_does_not_lag_behind_the_product(listed, client_commands):
    """Обратная сторона: команда появилась, а справочника про неё нет.

    Это мягче предыдущих (агент просто не расскажет о возможности), но
    именно так справочник и устаревает — молча.
    """
    missing = sorted(client_commands - listed)
    assert not missing, f"клиенту доступны команды, которых нет в справочнике: {missing}"


def test_the_two_sets_are_exactly_equal(listed, client_commands):
    assert listed == client_commands
