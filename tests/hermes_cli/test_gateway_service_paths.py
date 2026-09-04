import pytest
from unittest.mock import patch


def test_service_path_skips_nonexistent_node_modules(tmp_path):
    """Service PATH should not include node_modules/.bin if it doesn't exist."""
    from hermes_cli.gateway import _build_service_path_dirs
    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        dirs = _build_service_path_dirs(project_root=tmp_path)
    node_modules_bin = str(tmp_path / "node_modules" / ".bin")
    assert node_modules_bin not in dirs


def test_service_path_includes_node_modules_when_present(tmp_path):
    """Service PATH should include node_modules/.bin when it exists."""
    nm_bin = tmp_path / "node_modules" / ".bin"
    nm_bin.mkdir(parents=True)
    from hermes_cli.gateway import _build_service_path_dirs
    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        dirs = _build_service_path_dirs(project_root=tmp_path)
    assert str(nm_bin) in dirs




# ---------------------------------------------------------------------------
# Юнит обязан быть воспроизводимым.
#
# Найдено на живой машине 2026-09-04: `hermes gateway status` сразу после
# установки печатал «Installed gateway service definition is outdated»,
# хотя юнит написан минуту назад. Различие было ровно в строке PATH.
# Юнит писался из-под `runuser` с урезанным окружением, а сверялся в
# обычном сеансе, где `shutil.which("node")` находил node — и добавлял
# каталог, которого в установленном юните не было.
#
# Видимое следствие — ложное «устарел» и переписывание юнита на каждом
# перезапуске. Настоящее — вместе со строкой PATH так же случайно
# решалось, КАКОЙ node достанется шлюзу навсегда.
#
# Проверка — инвариант: она не знает, какой каталог правильный, и не
# фиксирует содержимое PATH. Она утверждает только то, что и должно быть
# верно всегда: один и тот же продукт на одной и той же машине обязан
# порождать один и тот же юнит, кто бы его ни запускал.
# ---------------------------------------------------------------------------


def test_generated_unit_does_not_depend_on_the_callers_path(monkeypatch):
    from hermes_cli import gateway

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    lean = gateway.generate_systemd_unit(system=False)

    monkeypatch.setenv(
        "PATH", "/opt/some/other/bin:/usr/local/bin:/usr/bin:/bin"
    )
    rich = gateway.generate_systemd_unit(system=False)

    assert lean == rich, (
        "юнит зависит от PATH вызывающего — значит «устарел» будет срабатывать "
        "ложно, а какой node достанется шлюзу, решит случай"
    )


def test_node_lookup_uses_the_given_search_path_not_the_environment(tmp_path, monkeypatch):
    """Каталог node берётся из заданного перечня, а не из окружения."""
    from hermes_cli import gateway

    planted = tmp_path / "planted-bin"
    planted.mkdir()
    node = planted / "node"
    node.write_text("#!/bin/sh\n")
    node.chmod(0o755)

    stray = tmp_path / "stray-bin"
    stray.mkdir()
    stray_node = stray / "node"
    stray_node.write_text("#!/bin/sh\n")
    stray_node.chmod(0o755)

    # В окружении — посторонний node. В заданном перечне — нужный.
    monkeypatch.setenv("PATH", str(stray))
    entries: list[str] = []
    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        gateway._append_node_dir_for_service(entries, search_path=str(planted))

    assert str(planted) in entries, entries
    assert str(stray) not in entries, (
        "взят node из окружения вызывающего — ровно тот дефект, который чинится"
    )
