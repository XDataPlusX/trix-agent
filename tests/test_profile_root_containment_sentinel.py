"""Сторож: профиль в ``contained`` не пишет за пределы своего корня.

Это главный тест всей работы по изоляции. Остальные проверяют отдельные
резолверы — этот проверяет **инвариант**:

    для любого пути p, в который движок пишет во время работы профиля,
        p ⊂ PROFILE_ROOT  ∨  p ∈ SHARED_IMMUTABLE  ∨  p ∈ OS_RUNTIME

Способ проверки принципиален: мы не читаем исходники и не мокаем резолверы.
Мы запускаем настоящие потоки движка в **отдельном процессе с пустым
окружением** (``env -i``-эквивалент: только PATH, HOME, HERMES_HOME,
HERMES_ISOLATION), снимаем слепок файловой системы до и после и сравниваем.
Всё, что появилось вне корня и не входит в allowlist §6, — дефект.

Почему отдельный процесс, а не тот же. ``TMPDIR`` выставляется на входе в
процесс, до первого ``import tempfile``, потому что ``tempfile.gettempdir()``
кэширует значение при первом вызове. В уже запущенном pytest это давно
случилось, и проверять в нём — значит проверять не то, что работает у
клиента. Пустое окружение нужно по той же причине: оболочка разработчика с
``TMPDIR``/``XDG_*`` замаскировала бы ровно ту дыру, которую мы ищем.

Третий тест здесь — граница режима: в ``shared-host`` наружу уходят ровно
``HOME`` и ``TMPDIR`` хоста, и это тоже зафиксировано, чтобы никто не
«починил» обратную совместимость случайно.

Чего этот сторож не умеет. Он проверяет только те потоки, которые в него
вписаны, и про спавн, который никто не добавил, он молчит — зелёный прогон
читается как «проверено», хотя проверять было нечего. Так и вышло с
stdio-спавном MCP (RAF-161). Покрытие — то есть «все ли спавны нагрузки
профиля вообще зовут контракт» — снимает ``tests/trix/
test_profile_root_spawn_sites.py``, по исходникам. Два прибора нужны оба:
этот отвечает за поведение, тот — за полноту перечня.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Потоки, которые обязан пройти профиль. Каждый — то, что реально случается
# при работе бота: конфиг, логи, база сессий, песочница, выполнение команды,
# хранилище результатов, сборка окружения подпроцесса, выбор рабочей папки.
_FLOW_SCRIPT = r'''
import json, os, sys
from pathlib import Path

ROOT = Path(os.environ["HERMES_HOME"])
result = {"ok": [], "failed": [], "paths": {}, "writes": []}

# Аудит записи внутри самого процесса. Слепок каталога отвечает на вопрос
# "что появилось", но только там, куда мы смотрим; смотреть же на настоящие
# /tmp и настоящий дом оператора слепком нельзя — их одновременно правят
# чужие процессы, и сторож стал бы флаки. Аудит-хук отвечает на другой,
# более точный вопрос: "куда пытался писать ЭТОТ процесс", и видит literal
# /tmp, дом оператора и всё остальное без единого ложного срабатывания от
# соседей.
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_WRITES = set()

def _audit(event, args):
    try:
        if event == "open":
            path, mode, flags = args
            wants_write = False
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                wants_write = True
            if isinstance(flags, int) and (flags & _WRITE_FLAGS):
                wants_write = True
            if not wants_write:
                return
            target = path
        elif event in ("os.mkdir", "os.rmdir", "os.remove", "os.truncate",
                       "os.chmod", "os.chown", "os.utime"):
            target = args[0]
        elif event in ("os.rename", "os.link", "os.symlink",
                       "shutil.copyfile", "shutil.copymode", "shutil.move"):
            target = args[1]
        else:
            return
        if isinstance(target, int) or target is None:
            return
        _WRITES.add(os.fsdecode(os.fspath(target)))
    except Exception:
        pass

sys.addaudithook(_audit)

def step(name, fn):
    try:
        fn()
        result["ok"].append(name)
    except Exception as e:
        result["failed"].append([name, "%s: %s" % (type(e).__name__, e)])

def _resolvers():
    from hermes_constants import (get_profile_root, get_profile_home_dir,
        get_profile_tmp_dir, get_profile_workspace_dir, get_isolation_mode)
    result["paths"]["mode"] = get_isolation_mode()
    result["paths"]["root"] = str(get_profile_root())
    result["paths"]["home"] = str(get_profile_home_dir())
    result["paths"]["tmp"] = str(get_profile_tmp_dir())
    result["paths"]["workspace"] = str(get_profile_workspace_dir())

def _tempfile():
    import hermes_cli.main  # ставит TMPDIR на входе в процесс
    import tempfile
    result["paths"]["gettempdir"] = tempfile.gettempdir()
    fd, p = tempfile.mkstemp()
    os.close(fd)
    result["paths"]["mkstemp"] = p

def _subprocess_env():
    from hermes_constants import apply_subprocess_containment_env
    env = {"PATH": os.environ.get("PATH", "")}
    apply_subprocess_containment_env(env)
    result["paths"]["subprocess_env"] = env

def _mcp_subprocess_env():
    # Не контракт, а НАСТОЯЩЕЕ место вызова. Поток выше проверяет, что
    # `apply_subprocess_containment_env` ведёт себя правильно; он ничего не
    # говорит о том, зовут ли её вообще. Ровно в эту щель и провалился MCP
    # (RAF-158): он единственный собирает окружение allowlist-фильтром
    # поверх `os.environ`, а не копией родительского, и контракт к нему не
    # применялся. Сторож обязан смотреть на спавны, а не на хелпер.
    from tools.mcp_tool import _build_safe_env
    result["paths"]["mcp_subprocess_env"] = _build_safe_env(None)

def _config():
    from hermes_cli.config import load_config, save_config
    save_config(load_config())

def _logging():
    from hermes_logging import setup_logging
    setup_logging()

def _state_db():
    from hermes_state import SessionDB, _default_db_path
    result["paths"]["db"] = str(_default_db_path())
    db = SessionDB()
    db.create_session("sentinel-session", "test")

def _sandbox():
    from tools.environments.base import get_sandbox_dir
    result["paths"]["sandbox"] = str(get_sandbox_dir())

def _execute():
    from tools.environments.local import LocalEnvironment
    env = LocalEnvironment(cwd=str(ROOT / "workspace"))
    result["paths"]["env_temp_dir"] = str(env.get_temp_dir())
    r = env.execute("echo hi > sentinel_out.txt && pwd && echo $HOME && echo $TMPDIR")
    result["paths"]["execute"] = r.get("output", "")

def _result_storage():
    from tools.tool_result_storage import _resolve_storage_dir
    from tools.environments.local import LocalEnvironment
    result["paths"]["storage"] = str(_resolve_storage_dir(LocalEnvironment()))

def _cwd_placeholder():
    from gateway.cwd_placeholder import (resolve_placeholder_terminal_cwd,
                                         placeholder_home_fallback)
    out = {}
    for configured in ("", ".", "auto", "cwd"):
        out[configured or "<пусто>"] = resolve_placeholder_terminal_cwd(
            configured_cwd=configured, terminal_backend="local",
            messaging_cwd=None, docker_mount_cwd_to_workspace=False,
            home_fallback=placeholder_home_fallback())
    result["paths"]["cwd_placeholder"] = out

def _profile_create():
    from hermes_cli.profiles import create_profile
    result["paths"]["new_profile"] = str(create_profile("sentinelprof"))

# --- потоки, добавленные по доказанным дефектам независимого ревью -------

def _entrypoints():
    """Все четыре установленные точки входа, а не только та, что зовут в тестах.

    `hermes-agent` и `hermes-acp` ставятся рецептом наравне с `hermes`, но
    редирект TMPDIR был подключён только к двум входам из четырёх, и сессия
    целиком шла на настоящем /tmp.

    Каждый вход проверяется в СВОЁМ процессе. Это не перестраховка: первый
    же импорт выставляет TMPDIR на весь процесс, и три следующих входа
    получили бы чужой ответ как свой. Ровно так и выглядела бы дыра,
    которую этот поток обязан ловить.
    """
    import subprocess
    keep = ("PATH", "HOME", "HERMES_HOME", "PYTHONPATH", "PYTHONUNBUFFERED",
            "HERMES_NO_NETWORK", "HERMES_ISOLATION")
    child_env = {k: os.environ[k] for k in keep if k in os.environ}
    probe = (
        "import importlib, sys, tempfile, os;"
        "importlib.import_module(sys.argv[1]);"
        "fd, p = tempfile.mkstemp(); os.close(fd);"
        "print(tempfile.gettempdir()); print(p)"
    )
    out = {}
    for module in ("hermes_cli.main", "gateway.run", "run_agent", "acp_adapter.entry"):
        proc = subprocess.run(
            [sys.executable, "-c", probe, module],
            env=child_env, capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            raise RuntimeError("%s: %s" % (module, (proc.stderr or "")[-600:]))
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        out[module] = [lines[0], lines[1]]
    result["paths"]["entrypoints"] = out

# Синтетические учётные данные: форма настоящая, содержимое — нет. Ни один
# файл настоящего дома оператора тест не читает и не пишет.
_FAKE_LOGINS = {
    "home/.claude/.credentials.json": '{"accessToken": "SENTINEL-NOT-REAL"}',
    "home/.config/github-copilot/hosts.json":
        '{"github.com": {"oauth_token": "gho_SENTINELNOTREAL"}}',
    "home/.qwen/oauth_creds.json": '{"access_token": "SENTINEL-NOT-REAL"}',
    "home/.modal.toml": '[default]\ntoken_secret = "as-SENTINELNOTREAL"\n',
    "home/.codex/auth.json": '{"OPENAI_API_KEY": "sk-SENTINELNOTREAL"}',
}

def _seed_fake_logins(root):
    for rel, body in _FAKE_LOGINS.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

def _export():
    """Экспорт профиля не имеет права увезти приватный дом."""
    import tarfile
    from hermes_cli.profiles import create_profile, export_profile
    profile = Path(create_profile("sentinelexport"))
    _seed_fake_logins(profile)
    archive = Path(export_profile("sentinelexport", str(ROOT / "sentinel-export")))
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getnames()
        bodies = []
        for member in tf.getmembers():
            if not member.isfile():
                continue
            handle = tf.extractfile(member)
            if handle is not None:
                bodies.append(handle.read().decode("utf-8", errors="replace"))
    result["paths"]["export_archive"] = str(archive)
    result["paths"]["export_home_members"] = [
        m for m in members if "/home/" in m or m.endswith("/home")
    ]
    result["paths"]["export_leaked_secret"] = any(
        "SENTINELNOTREAL" in b or "SENTINEL-NOT-REAL" in b for b in bodies
    )

def _backup():
    """Бэкап по умолчанию остаётся внутри корня."""
    from argparse import Namespace
    import hermes_cli.backup as backup_mod
    _seed_fake_logins(ROOT)
    backup_mod.run_backup(Namespace(output=None))
    made = sorted(str(p) for p in (ROOT / "backups").glob("hermes-backup-*.zip"))
    result["paths"]["backup_archives"] = made

def _plugin_homes():
    """Плагины и внешние CLI адресуются домом профиля."""
    from hermes_constants import user_home_path
    out = {}
    from plugins.memory.openviking import _resolve_ovcli_config_path
    out["openviking"] = str(_resolve_ovcli_config_path())
    from plugins.memory.mem0._oss_providers import qdrant_default_path
    out["mem0_qdrant"] = str(qdrant_default_path())
    from hermes_cli.codex_runtime_plugin_migration import migrate
    out["codex_write"] = str(migrate({}, dry_run=True, discover_plugins=False).target_path)
    out["codex_read"] = str(user_home_path(".codex") / "config.toml")
    for rel in (".claude", ".hindsight", ".honcho", ".cua-driver"):
        out[rel] = str(user_home_path(rel))
    result["paths"]["plugin_homes"] = out

for name, fn in [
    ("resolvers", _resolvers), ("tempfile", _tempfile),
    ("subprocess_env", _subprocess_env),
    ("mcp_subprocess_env", _mcp_subprocess_env), ("config", _config),
    ("logging", _logging), ("state_db", _state_db), ("sandbox", _sandbox),
    ("execute", _execute), ("result_storage", _result_storage),
    ("cwd_placeholder", _cwd_placeholder), ("profile_create", _profile_create),
    ("entrypoints", _entrypoints), ("export", _export), ("backup", _backup),
    ("plugin_homes", _plugin_homes),
]:
    step(name, fn)

result["writes"] = sorted(_WRITES)
print("@@SENTINEL@@" + json.dumps(result))
'''


def _snapshot(*roots: Path) -> set[str]:
    """Множество всех путей под *roots*. Символические ссылки не разворачиваем:
    нас интересует, что СОЗДАНО, а не куда оно указывает."""
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            seen.add(dirpath)
            for name in dirnames + filenames:
                seen.add(os.path.join(dirpath, name))
    return seen


def _run_flows(*, root: Path, home: Path, cwd: Path, mode: str | None) -> dict:
    """Прогнать потоки в чистом процессе и вернуть их отчёт."""
    import json

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "HERMES_HOME": str(root),
        "PYTHONPATH": str(REPO_ROOT),
        # Без буферизации, иначе отчёт может не долететь при падении.
        "PYTHONUNBUFFERED": "1",
        # Тесты движка не должны ходить в сеть и дёргать телеметрию.
        "HERMES_NO_NETWORK": "1",
    }
    if mode is not None:
        env["HERMES_ISOLATION"] = mode

    proc = subprocess.run(
        [sys.executable, "-c", _FLOW_SCRIPT],
        env=env, cwd=str(cwd), capture_output=True, text=True, timeout=300,
    )
    marker = "@@SENTINEL@@"
    for line in (proc.stdout or "").splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise AssertionError(
        "Поток не отчитался.\nstdout:\n%s\nstderr:\n%s"
        % (proc.stdout[-4000:], proc.stderr[-4000:])
    )


def _allowlisted(path: str, *, home: Path) -> bool:
    """``True``, если путь законно живёт вне корня — allowlist §6.

    Список читается из ``hermes_constants``, чтобы у сторожа, доктора и
    документации был один источник, а не три расходящихся.
    """
    from hermes_constants import PROFILE_ROOT_OS_RUNTIME, PROFILE_ROOT_SHARED_IMMUTABLE

    name = os.path.basename(path)
    for entry, _why in (*PROFILE_ROOT_SHARED_IMMUTABLE, *PROFILE_ROOT_OS_RUNTIME):
        if entry.startswith("$"):
            continue
        if entry.startswith("~/"):
            candidate = str(home / entry[2:])
        else:
            candidate = entry
        if "*" in candidate:
            import fnmatch

            if fnmatch.fnmatch(path, candidate) or fnmatch.fnmatch(name, os.path.basename(candidate)):
                return True
            continue
        if path == candidate or path.startswith(candidate.rstrip("/") + "/"):
            return True
    return False


@pytest.fixture
def arena(tmp_path: Path) -> dict:
    root = tmp_path / "profile"
    home = tmp_path / "fakehome"
    cwd = tmp_path / "cwd"
    for d in (root, home, cwd):
        d.mkdir()
    return {"root": root, "home": home, "cwd": cwd, "tmp_path": tmp_path}


class TestContainedWritesNothingOutsideRoot:
    """Прогон А: инвариант §1.1, проверенный исполнением."""

    def test_flows_run_and_write_only_inside_the_root(self, arena):
        root, home, cwd = arena["root"], arena["home"], arena["cwd"]
        watched = (arena["tmp_path"],)

        before = _snapshot(*watched)
        report = _run_flows(root=root, home=home, cwd=cwd, mode="contained")
        after = _snapshot(*watched)

        assert report["ok"], f"ни один поток не прошёл: {report['failed']}"

        created = sorted(after - before)
        outside = [
            p for p in created
            if not p.startswith(str(root))
            and not p.startswith(str(cwd))   # USER_WORKSPACE: явный cwd
            and not _allowlisted(p, home=home)
        ]
        assert not outside, (
            "contained-профиль создал пути ВНЕ своего корня — это дефект "
            "изоляции.\nПотоки: %s\nПути:\n  %s"
            % (report["ok"], "\n  ".join(outside))
        )

    def test_every_flow_completes(self, arena):
        """Ноль записей наружу не считается, если потоки просто не выполнились."""
        report = _run_flows(
            root=arena["root"], home=arena["home"], cwd=arena["cwd"], mode="contained"
        )
        assert not report["failed"], (
            "потоки упали, поэтому «ничего не записано» ничего не доказывает: %s"
            % report["failed"]
        )

    def test_the_branches_really_point_inside_the_root(self, arena):
        root = arena["root"]
        paths = _run_flows(
            root=root, home=arena["home"], cwd=arena["cwd"], mode="contained"
        )["paths"]

        assert paths["mode"] == "contained"
        for key in ("home", "tmp", "workspace", "gettempdir", "mkstemp",
                    "db", "sandbox", "env_temp_dir", "storage"):
            assert paths[key].startswith(str(root)), (
                f"{key} = {paths[key]} — вне корня {root}"
            )

    def test_subprocesses_inherit_the_contained_environment(self, arena):
        root = arena["root"]
        env = _run_flows(
            root=root, home=arena["home"], cwd=arena["cwd"], mode="contained"
        )["paths"]["subprocess_env"]

        for var in ("HOME", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME",
                    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
            assert env.get(var, "").startswith(str(root)), (
                f"подпроцесс получил {var}={env.get(var)!r} — вне корня"
            )
        # Настоящий дом передаётся явно: по нему чинится скоуп установки.
        assert env["HERMES_REAL_HOME"] == str(arena["home"])
        # OS_RUNTIME не трогаем никогда.
        assert "XDG_RUNTIME_DIR" not in env
        assert "DBUS_SESSION_BUS_ADDRESS" not in env

    def test_the_mcp_spawn_site_is_contained_too(self, arena):
        """Тот же инвариант, но снятый с места вызова, а не с контракта.

        Поток выше доказывает, что контракт правильный. Этот — что его
        правда зовут на единственном спавне, который собирает окружение
        по-своему. Живой прогон §5.3 такое не ловит: echo-сервер отвечает
        одинаково при любом ``HOME``.
        """
        root = arena["root"]
        env = _run_flows(
            root=root, home=arena["home"], cwd=arena["cwd"], mode="contained"
        )["paths"]["mcp_subprocess_env"]

        for var in ("HOME", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME",
                    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
            assert env.get(var, "").startswith(str(root)), (
                f"MCP-подпроцесс получил {var}={env.get(var)!r} — вне корня"
            )
        assert env["HERMES_REAL_HOME"] == str(arena["home"])

    def test_the_shell_really_sees_the_contained_home_and_tmp(self, arena):
        """Не «мы собрали словарь», а «дочерний shell это увидел»."""
        root = arena["root"]
        output = _run_flows(
            root=root, home=arena["home"], cwd=arena["cwd"], mode="contained"
        )["paths"]["execute"]

        lines = [ln for ln in output.splitlines() if ln.strip()]
        assert len(lines) >= 3, f"неожиданный вывод команды: {output!r}"
        _pwd, shell_home, shell_tmp = lines[0], lines[1], lines[2]
        assert shell_home == str(root / "home")
        assert shell_tmp == str(root / "tmp")

    def test_placeholder_cwd_lands_in_the_workspace(self, arena):
        root = arena["root"]
        placeholder = _run_flows(
            root=root, home=arena["home"], cwd=arena["cwd"], mode="contained"
        )["paths"]["cwd_placeholder"]

        for key, value in placeholder.items():
            assert value == str(root / "workspace"), (
                f"cwd для {key} = {value} — ожидалась рабочая папка профиля"
            )


# Настоящий дом того, кто ГОНЯЕТ сюиту, и настоящий общий /tmp. Именно их
# сторож раньше не видел: он смотрел только в свою арену, а запись по
# литералу ``/tmp/...`` уходила мимо неё целиком.
#
# ``conftest`` подменяет HOME на изолированный каталог, поэтому "настоящий
# дом" берётся из ``pwd`` — это тот же ответ, что дал бы
# ``pwd.getpwuid(os.getuid()).pw_dir`` внутри движка, то есть ровно та
# форма, которую резолвер обязан перекрывать.
def _real_host_home() -> Path:
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _writes_outside(report: dict, *, arena: dict) -> list[str]:
    """Записи процесса, ушедшие за пределы дозволенного.

    Дозволено: корень профиля и вся арена теста, чекаут кода (SHARED_IMMUTABLE
    — ``__pycache__`` пишется именно туда) и allowlist §6.
    """
    root = str(arena["root"])
    arena_root = str(arena["tmp_path"])
    repo = str(REPO_ROOT)
    host_home = str(_real_host_home())
    outside: list[str] = []
    for raw in report.get("writes", []):
        path = os.path.abspath(raw)
        if path.startswith(arena_root) or path.startswith(root):
            continue
        if path.startswith(repo):
            continue
        if _allowlisted(path, home=arena["home"]):
            continue
        # Интересуют ровно две территории: общий /tmp и дом оператора.
        if path.startswith("/tmp/") or path == "/tmp":
            outside.append(path)
        elif path.startswith(host_home + "/"):
            outside.append(path)
    return sorted(set(outside))


class TestNothingReachesTheRealTmpOrTheRealHome:
    """Сторож смотрит туда, куда течёт по-настоящему.

    Раньше наблюдаемым множеством была одна арена теста (``tmp_path``).
    Запись по литералу ``/tmp/...`` уходила в настоящий общий ``/tmp`` —
    мимо арены, мимо сравнения слепков, мимо всего. Ровно этот класс
    дефектов второй храповик и существует ловить.

    Слепком настоящий ``/tmp`` и настоящий дом не проверить: их правят
    чужие процессы, и сторож стал бы флаки. Поэтому вопрос задаётся иначе —
    аудит-хуком внутри самого процесса: «куда пытался писать ЭТОТ процесс».
    """

    def test_contained_process_never_writes_to_the_shared_tmp_or_home(self, arena):
        report = _run_flows(
            root=arena["root"], home=arena["home"], cwd=arena["cwd"], mode="contained"
        )

        assert report["writes"], "аудит записи не собрал ни одного пути — сторож слеп"
        outside = _writes_outside(report, arena=arena)
        assert not outside, (
            "contained-процесс писал в общий /tmp или в дом оператора:\n  "
            + "\n  ".join(outside)
        )

    def test_every_installed_entrypoint_redirects_tempfile(self, arena):
        """Все четыре точки входа, а не две из четырёх."""
        root = arena["root"]
        entrypoints = _run_flows(
            root=root, home=arena["home"], cwd=arena["cwd"], mode="contained"
        )["paths"]["entrypoints"]

        assert set(entrypoints) == {
            "hermes_cli.main", "gateway.run", "run_agent", "acp_adapter.entry",
        }, entrypoints
        for module, (gettempdir, mkstemp) in entrypoints.items():
            assert gettempdir.startswith(str(root)), f"{module}: {gettempdir}"
            assert mkstemp.startswith(str(root)), f"{module}: {mkstemp}"

    def test_export_leaves_the_private_home_behind(self, arena):
        """Архив профиля не увозит ни ветви ``home/``, ни её содержимого."""
        paths = _run_flows(
            root=arena["root"], home=arena["home"], cwd=arena["cwd"], mode="contained"
        )["paths"]

        assert paths["export_home_members"] == [], paths["export_home_members"]
        assert paths["export_leaked_secret"] is False

    def test_backup_lands_inside_the_root(self, arena):
        root = arena["root"]
        archives = _run_flows(
            root=root, home=arena["home"], cwd=arena["cwd"], mode="contained"
        )["paths"]["backup_archives"]

        assert archives, "бэкап по умолчанию не создан"
        for path in archives:
            assert path.startswith(str(root)), path

    def test_plugin_and_cli_homes_follow_the_profile(self, arena):
        root = arena["root"]
        homes = _run_flows(
            root=root, home=arena["home"], cwd=arena["cwd"], mode="contained"
        )["paths"]["plugin_homes"]

        for name, path in homes.items():
            assert path.startswith(str(root)), f"{name} = {path} — вне корня {root}"
        # Codex: чтение и запись обязаны указывать в один каталог.
        assert (
            os.path.dirname(homes["codex_write"]) == os.path.dirname(homes["codex_read"])
        ), homes


class TestSharedHostIsUnchanged:
    """Прогон Б: граница режима. Чтобы обратную совместимость не «починили»."""

    def test_shared_host_keeps_the_real_home_and_the_host_tmp(self, arena):
        root, home = arena["root"], arena["home"]
        paths = _run_flows(
            root=root, home=home, cwd=arena["cwd"], mode="shared-host"
        )["paths"]

        assert paths["mode"] == "shared-host"
        assert paths["home"] == str(home), "shared-host обязан оставить настоящий дом"
        assert not paths["gettempdir"].startswith(str(root)), (
            "shared-host увёл TMPDIR под корень — это смена поведения апстрима"
        )
        env = paths["subprocess_env"]
        assert not any(
            str(v).startswith(str(root)) for v in env.values()
        ), f"shared-host протёк contained-переменными: {env}"
        mcp_env = paths["mcp_subprocess_env"]
        assert not any(
            str(v).startswith(str(root)) for v in mcp_env.values()
        ), f"shared-host протёк contained-переменными в MCP: {mcp_env}"

    def test_a_root_without_a_marker_stays_shared_host(self, arena):
        """Обратная совместимость по построению: ни маркера, ни переменной —
        поведение не меняется ни на байт."""
        paths = _run_flows(
            root=arena["root"], home=arena["home"], cwd=arena["cwd"], mode=None
        )["paths"]

        assert paths["mode"] == "shared-host"
        assert paths["home"] == str(arena["home"])

    def test_the_marker_alone_switches_the_mode(self, arena):
        """Режим уезжает вместе с каталогом: скопировали корень — скопировали
        решение. Переменная при этом не задана."""
        from hermes_constants import PROFILE_LAYOUT_MARKER, PROFILE_LAYOUT_VERSION

        (arena["root"] / PROFILE_LAYOUT_MARKER).write_text(
            f"{PROFILE_LAYOUT_VERSION}\n", encoding="utf-8"
        )
        paths = _run_flows(
            root=arena["root"], home=arena["home"], cwd=arena["cwd"], mode=None
        )["paths"]

        assert paths["mode"] == "contained"
        assert paths["home"] == str(arena["root"] / "home")
