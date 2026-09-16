"""Каталог лаунчера `hermes` должен совпадать с тем, куда его кладёт install.sh.

Инвариант, а не снимок: проверяется соответствие между раскладкой установки
и каталогом команды — то самое соответствие, которое разошлось и дало
клиенту постоянную несуществующую неисправность (см. модульный докстринг
`hermes_cli/command_link.py`).
"""

from pathlib import Path, PurePath

from hermes_cli.command_link import (
    FHS_COMMAND_LINK_DIR,
    FHS_INSTALL_DIR,
    expected_command_link_dir,
)


def test_fhs_install_looks_for_the_command_where_install_sh_puts_it():
    directory, display = expected_command_link_dir(FHS_INSTALL_DIR, home="/home/user")
    assert directory == Path(FHS_COMMAND_LINK_DIR)
    assert display == str(FHS_COMMAND_LINK_DIR)
    # Именно этого не было: доктор спрашивал про домашний каталог, а рецепт
    # cloud-init удаляет там ссылку намеренно.
    assert ".local" not in str(directory)


def test_user_scoped_install_still_looks_in_home():
    directory, display = expected_command_link_dir(
        "/home/user/.hermes/hermes-agent", home="/home/user"
    )
    assert directory == Path("/home/user/.local/bin")
    assert display == "~/.local/bin"


def test_termux_prefix_wins_over_every_other_layout():
    # Порядок ветвей тот же, что у get_command_link_dir() установщика:
    # Termux проверяется первым, даже если путь установки выглядит как FHS.
    directory, display = expected_command_link_dir(
        FHS_INSTALL_DIR,
        home="/home/user",
        termux_prefix="/data/data/com.termux/files/usr",
    )
    assert directory == Path("/data/data/com.termux/files/usr/bin")
    assert display == "$PREFIX/bin"


def test_pure_paths_are_accepted_as_well_as_strings():
    a, _ = expected_command_link_dir(PurePath(FHS_INSTALL_DIR), home=Path("/home/user"))
    b, _ = expected_command_link_dir(str(FHS_INSTALL_DIR), home="/home/user")
    assert a == b
