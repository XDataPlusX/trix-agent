"""Режим изоляции обязан быть известен ДО первого временного файла.

Два независимых дефекта, один и тот же класс — «редирект опоздал»:

* ``apply_process_tmpdir()`` был подключён к двум точкам входа из четырёх,
  а ``hermes-agent`` и ``hermes-acp`` ставятся рецептом наравне с ``hermes``;
* ``isolation.mode`` из ``config.yaml`` мостился в ``HERMES_ISOLATION`` на
  сотни строк ПОЗЖЕ редиректа, а ``tempfile.gettempdir()`` кэширует ответ
  на весь процесс.

Поэтому здесь проверяется не «функция вызвана», а куда реально ложится файл
в чистом процессе (``env -i``). shared-host проверяется тем же способом:
у него не должно измениться ничего.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import hermes_constants

REPO_ROOT = Path(__file__).resolve().parents[2]

# Точки входа из ``[project.scripts]``. Все четыре ставятся рецептом, все
# четыре умеют создавать временные файлы.
ENTRYPOINT_MODULES = [
    "hermes_cli.main",
    "gateway.run",
    "run_agent",
    "acp_adapter.entry",
]


def _probe(env: dict, code: str, *, expect_ok: bool = True) -> str:
    full = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT), **env}
    out = subprocess.run(
        [sys.executable, "-c", code], env=full, capture_output=True, text=True, timeout=180
    )
    if expect_ok:
        assert out.returncode == 0, out.stderr[-3000:]
    return out.stdout.strip()


@pytest.fixture
def arena(tmp_path: Path) -> dict[str, Path]:
    profile = tmp_path / "profile"
    profile.mkdir()
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    return {"profile": profile, "home": fake_home}


def _base_env(arena: dict[str, Path]) -> dict[str, str]:
    return {"HOME": str(arena["home"]), "HERMES_HOME": str(arena["profile"])}


# ── 2. Точки входа ──────────────────────────────────────────────────────

@pytest.mark.timeout(240)
@pytest.mark.parametrize("module", ENTRYPOINT_MODULES)
def test_entrypoint_import_redirects_tempfile(arena, module: str) -> None:
    """Импорт точки входа уводит ``tempfile`` под корень — и файл тоже.

    Проверяем и ``gettempdir()``, и результат ``mkstemp()``: кэш можно
    сбросить, а уже созданный файл — нет.
    """
    printed = _probe(
        {**_base_env(arena), "HERMES_ISOLATION": "contained"},
        f"import {module};"
        "import tempfile, os;"
        "fd, path = tempfile.mkstemp();"
        "os.close(fd);"
        "print(tempfile.gettempdir());"
        "print(path)",
    )
    lines = printed.splitlines()
    assert len(lines) == 2, printed
    for line in lines:
        assert Path(line).is_relative_to(arena["profile"]), f"{module}: {line}"


@pytest.mark.timeout(240)
@pytest.mark.parametrize("module", ENTRYPOINT_MODULES)
def test_entrypoint_import_leaves_shared_host_alone(arena, module: str) -> None:
    """В shared-host ни одна точка входа не трогает ``tempfile``."""
    printed = _probe(
        {**_base_env(arena), "HERMES_ISOLATION": "shared-host"},
        f"import {module};import tempfile;print(tempfile.gettempdir())",
    )
    assert printed == "/tmp", printed


# ── 3. Мост isolation.mode из config.yaml ───────────────────────────────

@pytest.mark.timeout(240)
@pytest.mark.parametrize("module", ENTRYPOINT_MODULES)
def test_config_mode_reaches_tempfile_before_first_use(arena, module: str) -> None:
    """Режим объявлен ТОЛЬКО в config.yaml — редирект всё равно успевает.

    Воспроизведение RAF-147: раньше ``mkstemp()`` в этом сценарии
    приземлялся в настоящий ``/tmp``, потому что мост стоял после редиректа.
    """
    (arena["profile"] / "config.yaml").write_text(
        "model: test\nisolation:\n  mode: contained\n", encoding="utf-8"
    )
    printed = _probe(
        _base_env(arena),
        f"import {module};"
        "import tempfile, os;"
        "fd, path = tempfile.mkstemp();"
        "os.close(fd);"
        "print(tempfile.gettempdir());"
        "print(path)",
    )
    lines = printed.splitlines()
    assert len(lines) == 2, printed
    for line in lines:
        assert Path(line).is_relative_to(arena["profile"]), f"{module}: {line}"


@pytest.mark.timeout(240)
def test_config_mode_reaches_default_cwd(arena) -> None:
    """Дефолтный cwd терминала в contained — рабочая папка профиля.

    Тот же дефект с другого конца: ветвь cwd в ``cli.py`` спрашивала
    ``is_contained()`` раньше моста и всегда получала ``False``.
    """
    (arena["profile"] / "config.yaml").write_text(
        "isolation:\n  mode: contained\nterminal:\n  backend: local\n", encoding="utf-8"
    )
    printed = _probe(
        _base_env(arena),
        "import cli;import os;print(os.environ.get('TERMINAL_CWD',''))",
    )
    assert printed, "TERMINAL_CWD не выставлен"
    assert Path(printed).is_relative_to(arena["profile"]), printed


@pytest.mark.timeout(240)
def test_repeat_config_load_does_not_walk_cwd_out_of_the_root(arena) -> None:
    """Повторная загрузка конфига не возвращает cwd наружу.

    ``load_cli_config()`` сама экспортирует ``TERMINAL_CWD``, а ветвь cwd
    трактовала выставленную переменную как «оператор назвал каталог». На
    втором вызове оператором оказывались мы сами — и contained-профиль
    уезжал обратно в ``os.getcwd()``.
    """
    (arena["profile"] / "config.yaml").write_text(
        "isolation:\n  mode: contained\nterminal:\n  backend: local\n", encoding="utf-8"
    )
    printed = _probe(
        _base_env(arena),
        "import cli, os;"
        "cli.load_cli_config();cli.load_cli_config();"
        "print(os.environ.get('TERMINAL_CWD',''))",
    )
    assert Path(printed).is_relative_to(arena["profile"]), printed


@pytest.mark.timeout(240)
def test_inherited_terminal_cwd_still_wins(arena, tmp_path: Path) -> None:
    """Названный оператором каталог остаётся аварийным выходом."""
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (arena["profile"] / "config.yaml").write_text(
        "isolation:\n  mode: contained\nterminal:\n  backend: local\n", encoding="utf-8"
    )
    printed = _probe(
        {**_base_env(arena), "TERMINAL_CWD": str(chosen)},
        "import cli, os;cli.load_cli_config();print(os.environ.get('TERMINAL_CWD',''))",
    )
    assert printed == str(chosen), printed


@pytest.mark.timeout(240)
def test_shared_host_cwd_is_untouched(arena) -> None:
    """В shared-host дефолтный cwd — по-прежнему ``os.getcwd()``."""
    (arena["profile"] / "config.yaml").write_text(
        "terminal:\n  backend: local\n", encoding="utf-8"
    )
    printed = _probe(
        _base_env(arena),
        "import cli, os;cli.load_cli_config();print(os.environ.get('TERMINAL_CWD',''))",
    )
    assert printed == str(REPO_ROOT), printed


@pytest.mark.timeout(240)
def test_config_shared_host_stays_the_default(arena) -> None:
    """config.yaml без секции isolation — поведение не меняется ни на байт."""
    (arena["profile"] / "config.yaml").write_text("model: test\n", encoding="utf-8")
    printed = _probe(
        _base_env(arena),
        "import hermes_cli.main;import tempfile, os;"
        "print(os.environ.get('HERMES_ISOLATION', '<unset>'));"
        "print(tempfile.gettempdir())",
    )
    assert printed.splitlines() == ["<unset>", "/tmp"], printed


@pytest.mark.timeout(240)
def test_config_can_pin_shared_host_against_the_marker(arena) -> None:
    """Явный ``shared-host`` в config.yaml сильнее маркера раскладки."""
    (arena["profile"] / ".layout-version").write_text(
        f"{hermes_constants.PROFILE_LAYOUT_VERSION}\n", encoding="utf-8"
    )
    (arena["profile"] / "config.yaml").write_text(
        "isolation:\n  mode: shared-host\n", encoding="utf-8"
    )
    printed = _probe(
        _base_env(arena),
        "import hermes_cli.main;import tempfile;print(tempfile.gettempdir())",
    )
    assert printed == "/tmp", printed


@pytest.mark.timeout(240)
def test_env_var_still_wins_over_config(arena) -> None:
    """``HERMES_ISOLATION`` — источник №1 и аварийный выход."""
    (arena["profile"] / "config.yaml").write_text(
        "isolation:\n  mode: contained\n", encoding="utf-8"
    )
    printed = _probe(
        {**_base_env(arena), "HERMES_ISOLATION": "shared-host"},
        "import hermes_cli.main;import tempfile;print(tempfile.gettempdir())",
    )
    assert printed == "/tmp", printed


# ── Чтение config.yaml в обход загрузчика ───────────────────────────────

@pytest.fixture
def bare_root(monkeypatch, tmp_path: Path) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir()
    for var in ("HERMES_ISOLATION", "HERMES_IGNORE_USER_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    hermes_constants.reset_isolation_warnings()
    return profile


@pytest.mark.parametrize(
    "body, expected",
    [
        ("isolation:\n  mode: contained\n", "contained"),
        ("isolation:\n  mode: shared-host\n", "shared-host"),
        ("isolation:\n  mode: 'contained'  # комментарий\n", "contained"),
        ('isolation:\n  mode: "contained"\n', "contained"),
        ("isolation: {mode: contained}\n", "contained"),
        ("model: x\nisolation:\n\n  # пусто\n  mode: contained\nterminal:\n  cwd: .\n", "contained"),
        # Секция есть, ключа нет.
        ("isolation:\n  other: 1\n", None),
        # Ключ есть, но не на верхнем уровне — не наш.
        ("terminal:\n  isolation:\n    mode: contained\n", None),
        ("model: x\n", None),
        ("", None),
    ],
)
def test_config_file_isolation_reader(bare_root: Path, body: str, expected) -> None:
    (bare_root / "config.yaml").write_text(body, encoding="utf-8")
    assert hermes_constants.bridge_isolation_config_file() == expected
    if expected:
        assert os.environ["HERMES_ISOLATION"] == expected
    else:
        assert "HERMES_ISOLATION" not in os.environ


def test_config_file_reader_ignores_garbage_mode(bare_root: Path, capsys) -> None:
    (bare_root / "config.yaml").write_text("isolation:\n  mode: полная\n", encoding="utf-8")

    assert hermes_constants.bridge_isolation_config_file() is None
    assert "HERMES_ISOLATION" not in os.environ
    assert "isolation.mode" in capsys.readouterr().err


def test_config_file_reader_respects_ignore_user_config(
    bare_root: Path, monkeypatch
) -> None:
    """``--ignore-user-config`` пропускает и режим изоляции тоже."""
    (bare_root / "config.yaml").write_text("isolation:\n  mode: contained\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "1")

    assert hermes_constants.bridge_isolation_config_file() is None
    assert "HERMES_ISOLATION" not in os.environ


def test_config_file_reader_survives_a_missing_file(bare_root: Path) -> None:
    assert hermes_constants.bridge_isolation_config_file() is None


def test_config_file_reader_survives_broken_yaml(bare_root: Path) -> None:
    """Сломанный config.yaml не имеет права уронить точку входа."""
    (bare_root / "config.yaml").write_text("isolation:\n  mode: [\n", encoding="utf-8")

    assert hermes_constants.bridge_isolation_config_file() is None


def test_bootstrap_returns_mode_and_tmpdir(bare_root: Path) -> None:
    (bare_root / "config.yaml").write_text("isolation:\n  mode: contained\n", encoding="utf-8")

    mode, tmpdir = hermes_constants.bootstrap_process_isolation()

    assert mode == "contained"
    assert tmpdir == str(bare_root / "tmp")


def test_bootstrap_is_a_noop_in_shared_host(bare_root: Path) -> None:
    assert hermes_constants.bootstrap_process_isolation() == (None, None)


# ── 2b. Именованный профиль выбран БЕЗ HERMES_HOME ──────────────────────
#
# Всё выше задаёт дочернему процессу ``HERMES_HOME`` — так входит только
# юнит шлюза (``Environment="HERMES_HOME=..."``). Интерактивный CLI, cron и
# любой вызов ``hermes`` из терминала входят иначе: переменной нет, а корень
# профиля выбирается флагом ``-p`` или липким ``active_profile``. Пока этот
# выбор не сделан, режим читается у ``~/.hermes`` — не у того корня, в
# который процесс на самом деле будет писать.
#
# Цена ошибки — та же, что и в остальном файле: ``tempfile.gettempdir()``
# кэширует ответ на весь процесс, и опоздавший режим не применится уже
# никогда.

PROFILE_SELECTORS: dict[str, str] = {
    # Липкий выбор: ``hermes profile use acme`` и дальше голый ``hermes``.
    "sticky": "",
    "flag": "import sys; sys.argv = ['hermes', '-p', 'acme', 'chat']\n",
    "flag_equals": "import sys; sys.argv = ['hermes', '--profile=acme', 'chat']\n",
    # Флаг после подкоманды — исторически поддержанная форма.
    "flag_after_command": "import sys; sys.argv = ['hermes', 'chat', '-p', 'acme']\n",
}


@pytest.fixture
def named_arena(tmp_path: Path) -> dict[str, Path]:
    """Настоящий дом ОС, корень ``~/.hermes`` и один именованный профиль.

    ``HERMES_HOME`` намеренно не задаётся ни в одном тесте этого раздела.
    """
    os_home = tmp_path / "oshome"
    root = os_home / ".hermes"
    profile = root / "profiles" / "acme"
    profile.mkdir(parents=True)
    return {"os_home": os_home, "root": root, "profile": profile}


def _mark_contained(root: Path) -> None:
    (root / ".layout-version").write_text(
        f"{hermes_constants.PROFILE_LAYOUT_VERSION}\n", encoding="utf-8"
    )


def _select(arena: dict[str, Path], selector: str) -> str:
    """Собрать код пробы: выбор профиля + импорт точки входа."""
    if selector == "sticky":
        (arena["root"] / "active_profile").write_text("acme\n", encoding="utf-8")
    return PROFILE_SELECTORS[selector]


_REPORT = (
    "import hermes_cli.main;"
    "import os, tempfile, hermes_constants as hc;"
    "fd, path = tempfile.mkstemp();"
    "os.close(fd);"
    "print(os.environ.get('HERMES_HOME', '<unset>'));"
    "print(hc.is_contained());"
    "print(tempfile.gettempdir());"
    "print(path)"
)


@pytest.mark.timeout(240)
@pytest.mark.parametrize("selector", sorted(PROFILE_SELECTORS))
def test_named_profile_marker_reaches_tempfile(named_arena, selector: str) -> None:
    """Маркер лежит в профиле — редирект обязан успеть до первого файла.

    Воспроизведение из повторного ревью: ``is_contained()`` отвечал
    ``True``, а ``gettempdir()`` оставался ``/tmp``, потому что корень
    профиля выставлялся на шестьсот строк позже бутстрапа.
    """
    _mark_contained(named_arena["profile"])
    printed = _probe(
        {"HOME": str(named_arena["os_home"])},
        _select(named_arena, selector) + _REPORT,
    )
    home, contained, tmpdir, made = printed.splitlines()

    assert home == str(named_arena["profile"]), printed
    assert contained == "True", printed
    assert Path(tmpdir).is_relative_to(named_arena["profile"]), printed
    assert Path(made).is_relative_to(named_arena["profile"]), printed


@pytest.mark.timeout(240)
@pytest.mark.parametrize("selector", sorted(PROFILE_SELECTORS))
def test_named_profile_config_route_reaches_tempfile(named_arena, selector: str) -> None:
    """Режим объявлен в config.yaml ПРОФИЛЯ, а не корня.

    Файловый мост читает ``<корень>/config.yaml``. Пока корнем считается
    ``~/.hermes``, конфиг именованного профиля не читает никто.
    """
    (named_arena["profile"] / "config.yaml").write_text(
        "isolation:\n  mode: contained\n", encoding="utf-8"
    )
    printed = _probe(
        {"HOME": str(named_arena["os_home"])},
        _select(named_arena, selector) + _REPORT,
    )
    home, contained, tmpdir, made = printed.splitlines()

    assert home == str(named_arena["profile"]), printed
    assert contained == "True", printed
    assert Path(tmpdir).is_relative_to(named_arena["profile"]), printed
    assert Path(made).is_relative_to(named_arena["profile"]), printed


@pytest.mark.timeout(240)
def test_named_profile_without_a_marker_stays_on_tmp(named_arena) -> None:
    """Профиль без маркера — shared-host: ``/tmp`` как и был."""
    printed = _probe(
        {"HOME": str(named_arena["os_home"])},
        _select(named_arena, "sticky") + _REPORT,
    )
    home, contained, tmpdir, made = printed.splitlines()

    assert home == str(named_arena["profile"]), printed
    assert contained == "False", printed
    assert tmpdir == "/tmp", printed
    assert made.startswith("/tmp/"), printed


@pytest.mark.timeout(240)
def test_root_marker_does_not_contain_the_named_profile(named_arena) -> None:
    """Режим берётся у выбранного корня, а не у ``~/.hermes``.

    Обратная сторона того же дефекта: маркер в корне увёл бы ``TMPDIR`` в
    ``~/.hermes/tmp``, пока данные пишутся в профиль.
    """
    _mark_contained(named_arena["root"])
    printed = _probe(
        {"HOME": str(named_arena["os_home"])},
        _select(named_arena, "sticky") + _REPORT,
    )
    home, contained, tmpdir, made = printed.splitlines()

    assert home == str(named_arena["profile"]), printed
    assert contained == "False", printed
    assert tmpdir == "/tmp", printed
    assert made.startswith("/tmp/"), printed


@pytest.mark.timeout(240)
def test_sticky_default_keeps_the_root_as_the_profile(named_arena) -> None:
    """``active_profile = default`` — корень и есть профиль."""
    _mark_contained(named_arena["root"])
    (named_arena["root"] / "active_profile").write_text("default\n", encoding="utf-8")
    printed = _probe({"HOME": str(named_arena["os_home"])}, _REPORT)
    _home, contained, tmpdir, made = printed.splitlines()

    assert contained == "True", printed
    assert Path(tmpdir).is_relative_to(named_arena["root"]), printed
    assert Path(made).is_relative_to(named_arena["root"]), printed


@pytest.mark.timeout(240)
def test_supervised_child_does_not_follow_the_sticky_profile(named_arena) -> None:
    """Слот ``gateway-default`` под s6 держит свой корень, а не липкий выбор.

    Исключение существует в ``_apply_profile_override`` (иначе переключение
    активного профиля молча уводило бы дефолтный шлюз в чужой профиль), и
    бутстрап обязан его повторять — иначе два места отвечают на один вопрос
    по-разному.
    """
    _mark_contained(named_arena["profile"])
    (named_arena["root"] / "active_profile").write_text("acme\n", encoding="utf-8")
    printed = _probe(
        {
            "HOME": str(named_arena["os_home"]),
            "HERMES_HOME": str(named_arena["root"]),
            "HERMES_S6_SUPERVISED_CHILD": "1",
        },
        _REPORT,
    )
    home, contained, tmpdir, _made = printed.splitlines()

    assert home == str(named_arena["root"]), printed
    assert contained == "False", printed
    assert tmpdir == "/tmp", printed


@pytest.mark.timeout(240)
def test_profile_flag_is_still_stripped_from_argv(named_arena) -> None:
    """Флаг по-прежнему не доезжает до argparse."""
    _mark_contained(named_arena["profile"])
    printed = _probe(
        {"HOME": str(named_arena["os_home"])},
        _select(named_arena, "flag") + "import hermes_cli.main;import sys;print(sys.argv)",
    )

    assert printed == "['hermes', 'chat']", printed


@pytest.mark.timeout(240)
def test_dash_p_that_is_not_a_profile_name_changes_nothing(named_arena) -> None:
    """``-p no:xdist`` — не выбор профиля, и корень от него не меняется."""
    _mark_contained(named_arena["profile"])
    printed = _probe(
        {"HOME": str(named_arena["os_home"])},
        "import sys; sys.argv = ['hermes', '-p', 'no:xdist']\n" + _REPORT,
    )
    home, contained, tmpdir, _made = printed.splitlines()

    assert home == "<unset>", printed
    assert contained == "False", printed
    assert tmpdir == "/tmp", printed


@pytest.mark.timeout(240)
def test_unknown_profile_name_does_not_redirect_anything(named_arena) -> None:
    """Несуществующий профиль не уводит ``TMPDIR`` в несозданный каталог.

    Ошибку про несуществующий профиль печатает CLI — бутстрап обязан
    промолчать и ничего не создавать.
    """
    _mark_contained(named_arena["root"])
    printed = _probe(
        {"HOME": str(named_arena["os_home"])},
        "import sys; sys.argv = ['hermes', '-p', 'missing']\n"
        "import hermes_constants as hc; hc.bootstrap_process_isolation();"
        "import os, tempfile;"
        "print(os.environ.get('HERMES_HOME', '<unset>'));"
        "print(tempfile.gettempdir());"
        "print((hc.get_default_hermes_root() / 'profiles' / 'missing').exists())",
    )
    home, tmpdir, created = printed.splitlines()

    assert home == "<unset>", printed
    assert Path(tmpdir) == named_arena["root"] / "tmp", printed
    assert created == "False", printed
