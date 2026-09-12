"""Где лежит пользовательская команда `hermes` — один ответ на всю кодовую базу.

`scripts/install.sh` кладёт лаунчер в три разных каталога в зависимости от
раскладки установки (`get_command_link_dir`), и **доктор обязан спрашивать
про тот же каталог**. Раньше он этого не делал: комментарий в
`hermes_cli/doctor.py` утверждал «mirrors install.sh logic», а код знал
только про Termux и `~/.local/bin` — ветку FHS-установки от root он не
воспроизводил.

Цена этого расхождения на машине клиента: установка root-овая, лаунчер
лежит в `/usr/local/bin/hermes` и прекрасно работает, а рецепт cloud-init
намеренно удаляет `~/.local/bin/hermes` как след пользовательской установки.
Доктор отвечал «✗ ~/.local/bin/hermes not found» и заводил пункт «Missing
~/.local/bin/hermes symlink — run 'hermes doctor --fix'» — постоянную
неисправность, которой нет, и совет, который создал бы ровно то, что рецепт
удалил. Проверено исполнением на trix-testing7.ru 2026-09-04.

Функция чистая и не читает окружение сама: всё, от чего зависит ответ,
передаётся аргументами — иначе её нельзя было бы проверить, не притворяясь
другой операционной системой (см. «Don't fake the host OS» в CLAUDE.md).
"""

from __future__ import annotations

from pathlib import Path, PurePath

# Совпадает с литералом install.sh (`resolve_install_layout`): корневая
# установка на Linux уезжает в FHS-раскладку, а команда — в /usr/local/bin.
FHS_INSTALL_DIR = PurePath("/usr/local/lib/hermes-agent")
FHS_COMMAND_LINK_DIR = PurePath("/usr/local/bin")


def expected_command_link_dir(
    project_root: Path | PurePath | str,
    *,
    home: Path | PurePath | str,
    termux_prefix: str | None = None,
) -> tuple[Path, str]:
    """Вернуть (каталог лаунчера, как его показать пользователю).

    Порядок ветвей — тот же, что в ``get_command_link_dir`` установщика:
    Termux, затем FHS-раскладка, затем пользовательская.

    ``termux_prefix`` — значение ``$PREFIX``, но только когда окружение
    действительно Termux; вызывающий решает это сам.
    """
    if termux_prefix:
        return Path(termux_prefix) / "bin", "$PREFIX/bin"

    if PurePath(project_root) == FHS_INSTALL_DIR:
        return Path(FHS_COMMAND_LINK_DIR), str(FHS_COMMAND_LINK_DIR)

    return Path(home) / ".local" / "bin", "~/.local/bin"
