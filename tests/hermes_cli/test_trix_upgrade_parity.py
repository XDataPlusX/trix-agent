"""«У всех одинаково»: обновлённая машина = свежая установка.

Требование владельца (сентябрь 2026) при переходе на полный ``config.yaml``:
клиент, который уже работает, после ``hermes update`` обязан получить тот же
набор настроек и те же пояснения, что получает свежая установка. Не «почти
те же» — те же.

Путь обновления собран здесь из тех же двух шагов, которые делает
``hermes update`` (см. ``hermes_cli/update_cmd.py``): досев отсутствующих
секций (``trix_config_sync``) и миграции (``migrate_config``, внутри —
перенос правок пояснений). Отправная точка — шаблон ПОСЛЕДНЕГО РЕЛИЗА из
git, то есть буквально тот файл, который лежит на машинах клиентов.

Почему тест сквозной, а не по частям: до этого каждый шаг был покрыт
отдельно и каждый был зелёным, а вместе они давали файл, у которого шапка
противоречила содержимому и не хватало десяти секций. Паритет — свойство
цепочки, и проверять его можно только цепочкой.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix-config.yaml"


def _paths(data, prefix=()):
    out = []
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        path = prefix + (key,)
        out.append(path)
        out.extend(_paths(value, path))
    return out


@pytest.fixture
def upgraded(tmp_path, monkeypatch, previous_release_config_template):
    """Машина на прошлом релизе, прогнанная через шаги ``hermes update``."""
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text(previous_release_config_template, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli.config import migrate_config
    from hermes_cli.trix_config_sync import sync_missing_client_sections

    added, skipped = sync_missing_client_sections(config, TEMPLATE)
    migrate_config(interactive=False, quiet=True)
    return config, added, skipped


def test_the_chain_actually_did_work(upgraded):
    """Страховка от пустой проверки.

    Если отправная точка соскользнёт на уже обновлённый файл (так и вышло,
    когда точкой был `HEAD`), досеивать станет нечего, и ВСЕ проверки ниже
    пройдут, не проверив ничего. Поэтому цепочка обязана сначала доказать,
    что она вообще работала.
    """
    _config, added, _skipped = upgraded
    assert len(added) > 200, (
        f"досев доставил всего {len(added)} путей — отправная точка похожа на "
        "уже обновлённый файл, и проверки паритета ничего не проверяют"
    )


def test_nothing_is_skipped_on_the_way(upgraded):
    """Ни один путь не остаётся недоставленным."""
    _config, _added, skipped = upgraded
    assert skipped == [], f"досев оставил пропуски: {skipped}"


def test_the_upgraded_machine_has_every_setting_a_fresh_install_has(upgraded):
    config, _added, _skipped = upgraded
    fresh = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    client = yaml.safe_load(config.read_text(encoding="utf-8"))

    missing = [p for p in _paths(fresh) if p not in set(_paths(client))]
    assert not missing, (
        f"обновлённой машине не хватает {len(missing)} настроек против свежей "
        f"установки: {['.'.join(map(str, p)) for p in missing[:15]]}"
    )


def test_the_upgrade_changes_no_value_the_client_already_had(upgraded, previous_release_config_template):
    """Значения прошлого релиза сохраняются дословно."""
    config, _added, _skipped = upgraded
    before = yaml.safe_load(previous_release_config_template)
    after = yaml.safe_load(config.read_text(encoding="utf-8"))

    def _same(old, new, path=()):
        problems = []
        for key, oval in (old or {}).items():
            dotted = ".".join(map(str, path + (key,)))
            if key == "_config_version":
                continue  # метка схемы обязана продвинуться, это и есть миграция
            assert key in new, f"{dotted} исчез при обновлении"
            nval = new[key]
            if isinstance(oval, dict):
                problems += _same(oval, nval if isinstance(nval, dict) else {}, path + (key,))
            elif oval != nval:
                problems.append(f"{dotted}: {oval!r} -> {nval!r}")
        return problems

    drift = _same(before, after)
    assert not drift, f"обновление поменяло значения клиента: {drift}"


def test_the_upgraded_file_does_not_contradict_itself(upgraded):
    """Пояснения догнали содержимое.

    Старая шапка обещала «здесь только то, что отличается от значений по
    умолчанию» — над файлом, в котором теперь все секции. Это не косметика:
    по этому тексту клиент судит, что вообще можно менять.
    """
    config, _added, _skipped = upgraded
    text = config.read_text(encoding="utf-8")
    assert "только то, что отличается" not in text
    assert "Здесь ВСЕ настройки агента" in text


def test_the_upgraded_file_names_no_other_product(upgraded):
    config, _added, _skipped = upgraded
    text = config.read_text(encoding="utf-8")
    assert "Гермес" not in text
    assert "hermes_cli/trix_menu.py" not in text, "внутренний путь репозитория"


def test_the_curated_explanations_survive_the_upgrade(upgraded):
    """Наши пояснения на месте — включая секцию, которая раньше исчезала.

    ``platforms.telegram.extra.command_menu`` — голый ключ с двадцатью
    строками объяснения под ним. До исправления ``_strip_default_values``
    он вылетал из файла при ЛЮБОЙ миграции, унося документацию с собой.
    """
    config, _added, _skipped = upgraded
    text = config.read_text(encoding="utf-8")
    data = yaml.safe_load(text)

    assert "Меню команд Telegram" in text
    assert "platforms" in data, "секция с голым ключом снова исчезла при миграции"
    assert "command_menu" in data["platforms"]["telegram"]["extra"]

    for fragment in (
        "Ресурсы песочницы рассчитаны",
        "Поиск через DuckDuckGo",
        "Трое суток молчания",
        "Твоя постоянная рабочая папка",
    ):
        assert fragment in text, f"пояснение пропало при обновлении: {fragment!r}"


def test_the_schema_version_advances_to_current(upgraded):
    config, _added, _skipped = upgraded
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert data["_config_version"] == DEFAULT_CONFIG["_config_version"]
