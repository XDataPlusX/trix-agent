"""Решение владельца по пункту C ревью RAF-150: новый профиль — contained.

`hermes profile create` ставит маркер раскладки, поэтому на обновлённой
машине каждый заведённый профиль сразу изолирован. Ревью назвало это
опасным, пока живы блокеры A и B; оба закрыты в этой же ветке
(`736e2b55f`, `8de12d9ea`), и владелец решил поведение оставить:
**новые профили contained, shared-host — только для унаследованных
установок.**

Решение записано тестом, а не только в документе. Если кто-то снимет
запись маркера из `create_profile()` — или, наоборот, начнёт ставить его
существующим корням — покраснеет здесь.
"""

from pathlib import Path

import pytest

import hermes_constants
from hermes_cli.profiles import create_profile


@pytest.fixture()
def legacy_install(monkeypatch, tmp_path) -> Path:
    """Унаследованная установка: корень без маркера, то есть shared-host."""
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    for var in ("HERMES_ISOLATION", "KUBERNETES_SERVICE_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(hermes_constants, "_container_detected", False, raising=False)
    monkeypatch.setattr(hermes_constants, "is_container", lambda: False)
    hermes_constants.reset_isolation_warnings()
    return root


def _mode_of(monkeypatch, root: Path) -> str:
    """Режим ЭТОГО корня — так, как его прочтёт процесс с таким HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(root))
    hermes_constants.reset_isolation_warnings()
    return hermes_constants.get_isolation_mode()


def test_new_profile_gets_the_marker(legacy_install, monkeypatch):
    """Решение владельца: свежесозданный профиль — contained по построению."""
    profile_dir = create_profile("newclient", no_alias=True)

    marker = profile_dir / hermes_constants.PROFILE_LAYOUT_MARKER
    assert marker.exists(), "новый профиль обязан получить маркер раскладки"
    assert marker.read_text(encoding="utf-8").strip() == "2"
    assert _mode_of(monkeypatch, profile_dir) == "contained"


def test_legacy_root_stays_shared_host(legacy_install, monkeypatch):
    """Заведение клиента не имеет права переключить старый корень.

    Первый запрет плана: поведение УЖЕ существующих установок не меняется
    само. Маркер корню ставит только `migrate-layout --apply`, то есть рука
    оператора.
    """
    create_profile("newclient", no_alias=True)

    assert not (legacy_install / hermes_constants.PROFILE_LAYOUT_MARKER).exists()
    assert _mode_of(monkeypatch, legacy_install) == "shared-host"


def test_full_clone_inherits_the_source_layout(legacy_install, monkeypatch):
    """`--clone-all` копирует корень целиком — вместе с отсутствием маркера.

    Клон обязан вести себя как оригинал: иначе «клонировал профиль» тихо
    переносило бы логины в другое место.
    """
    source = create_profile("source", no_alias=True)
    source_marker = source / hermes_constants.PROFILE_LAYOUT_MARKER
    assert source_marker.exists(), "предусловие теста: маркер ставится при создании"
    source_marker.unlink()

    clone = create_profile("clone", clone_from="source", clone_all=True, no_alias=True)

    assert not (clone / hermes_constants.PROFILE_LAYOUT_MARKER).exists()
    assert _mode_of(monkeypatch, clone) == "shared-host", (
        "форма пути <root>/profiles/<имя> режим не выбирает — только маркер; "
        "поэтому клон корня без маркера остаётся shared-host"
    )
