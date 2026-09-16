"""Фаза 1.1/1.5 плана 2026-09-14: как выбирается режим изоляции.

Порядок источников — §2.1 плана, строгий:
    1. HERMES_ISOLATION
    2. isolation.mode из config.yaml (мостится в ту же переменную — тест моста
       отдельно, в test_profile_root_bridge.py)
    3. маркер <root>/.layout-version == 2
    4. is_container()
    5. HERMES_HOME вида <root>/profiles/<name>
    6. иначе shared-host

Тесты исполняют резолвер, а не читают исходник.
"""

import hermes_constants


def _clear(monkeypatch):
    for var in ("HERMES_ISOLATION", "HERMES_HOME", "KUBERNETES_SERVICE_HOST"):
        monkeypatch.delenv(var, raising=False)
    # is_container() кэширует результат в модуле на всю жизнь процесса.
    monkeypatch.setattr(hermes_constants, "_container_detected", False, raising=False)
    monkeypatch.setattr(hermes_constants, "is_container", lambda: False)
    hermes_constants.reset_isolation_warnings()


def test_env_var_wins(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_ISOLATION", "contained")
    assert hermes_constants.get_isolation_mode() == "contained"

    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    assert hermes_constants.get_isolation_mode() == "shared-host"


def test_env_var_beats_marker(monkeypatch, tmp_path):
    """Явное слово оператора сильнее маркера на диске."""
    _clear(monkeypatch)
    root = tmp_path / "profile"
    root.mkdir()
    (root / ".layout-version").write_text("2\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_ISOLATION", "shared-host")
    assert hermes_constants.get_isolation_mode() == "shared-host"


def test_env_var_aliases(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    for raw in ("CONTAINED", " contained ", "contain", "isolated"):
        monkeypatch.setenv("HERMES_ISOLATION", raw)
        assert hermes_constants.get_isolation_mode() == "contained", raw
    for raw in ("shared_host", "SHARED-HOST", "host", "shared"):
        monkeypatch.setenv("HERMES_ISOLATION", raw)
        assert hermes_constants.get_isolation_mode() == "shared-host", raw


def test_garbage_env_var_falls_through_and_warns(monkeypatch, tmp_path, capsys):
    """Мусор в переменной не должен молча включать contained."""
    _clear(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_ISOLATION", "yes-please")
    assert hermes_constants.get_isolation_mode() == "shared-host"
    assert "HERMES_ISOLATION" in capsys.readouterr().err


def test_marker_selects_contained(monkeypatch, tmp_path):
    _clear(monkeypatch)
    root = tmp_path / "profile"
    root.mkdir()
    (root / ".layout-version").write_text("2\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    assert hermes_constants.get_isolation_mode() == "contained"


def test_marker_version_one_is_shared_host(monkeypatch, tmp_path):
    _clear(monkeypatch)
    root = tmp_path / "profile"
    root.mkdir()
    (root / ".layout-version").write_text("1", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    assert hermes_constants.get_isolation_mode() == "shared-host"


def test_garbage_marker_stays_contained_with_warning(monkeypatch, tmp_path, capsys):
    """Мусор в маркере — contained с предупреждением, а не тихий откат.

    Раньше здесь стоял ``shared-host``: «не разобрали файл» приравнивалось
    к «файла нет». Разница в том, что файла нет у каждой старой установки,
    а испорченный файл бывает ровно у той, где маркер писали — то есть
    contained включали осознанно. Оборванная запись (полный диск, убитый
    процесс) давала ровно этот случай, и профиль молча уходил писать логины
    в общий дом ОС. Подробности и остальные формы порчи —
    ``test_profile_root_marker_failclosed.py``.
    """
    _clear(monkeypatch)
    root = tmp_path / "profile"
    root.mkdir()
    (root / ".layout-version").write_text("две штуки", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    assert hermes_constants.get_isolation_mode() == "contained"
    assert ".layout-version" in capsys.readouterr().err


def test_container_detection_does_not_enable_containment(monkeypatch, tmp_path):
    """is_container() НЕ включает contained — она врёт на хосте с Docker.

    Её последняя проверка ищет маркеры docker/containerd в
    /proc/self/mountinfo, а на хосте с запущенным демоном там лежат его
    собственные overlay-монтирования. Проверено исполнением 2026-09-14:
    на машине разработки без /.dockerenv она возвращает True. Если бы режим
    зависел от неё, каждая клиентская машина с docker-бэкендом терминала
    молча переехала бы в contained на ближайшем обновлении.
    """
    _clear(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(hermes_constants, "is_container", lambda: True)
    assert hermes_constants.get_isolation_mode() == "shared-host"


def test_profiles_layout_does_not_enable_containment(monkeypatch, tmp_path):
    """Форма пути <root>/profiles/<имя> — тоже не повод.

    Апстримный тест test_host_auto_keeps_real_home_when_profile_home_exists
    прямо описывает обратное поведение для такой раскладки на хосте:
    профиль не прячет настоящие ~/.ssh и ~/.gitconfig. Новые профили
    получают containment маркером при создании, а не догадкой.
    """
    _clear(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root" / "profiles" / "coder"))
    assert hermes_constants.get_isolation_mode() == "shared-host"


def test_plain_host_install_is_shared_host(monkeypatch, tmp_path):
    """Главный случай обратной совместимости: одиночная установка на хосте."""
    _clear(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert hermes_constants.get_isolation_mode() == "shared-host"
    assert hermes_constants.is_contained() is False


def test_no_hermes_home_at_all_is_shared_host(monkeypatch):
    _clear(monkeypatch)
    assert hermes_constants.get_isolation_mode() == "shared-host"


def test_env_dict_argument_is_honored(monkeypatch, tmp_path):
    """Резолверы окружения подпроцесса передают словарь, а не os.environ."""
    _clear(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    env = {"HERMES_ISOLATION": "contained", "HERMES_HOME": str(tmp_path / "other")}
    assert hermes_constants.get_isolation_mode(env) == "contained"
    assert hermes_constants.get_isolation_mode() == "shared-host"
