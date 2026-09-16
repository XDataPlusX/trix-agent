"""Shared constants for Hermes Agent.

Import-safe module with no dependencies — can be imported from anywhere
without risk of circular imports.
"""

import os
import shutil
import stat
import sys
from contextvars import ContextVar, Token
from pathlib import Path


_profile_fallback_warned: bool = False
_UNSET = object()
_HERMES_HOME_OVERRIDE: ContextVar[str | object] = ContextVar(
    "_HERMES_HOME_OVERRIDE", default=_UNSET
)

# ── TUI busy-indicator styles ─────────────────────────────────────────
# Single source of truth shared by the CLI /indicator command, the TUI
# gateway config handler, and the /help command registry. Keep in sync
# with ``INDICATOR_STYLES`` / ``DEFAULT_INDICATOR_STYLE`` in
# ``ui-tui/src/app/interfaces.ts`` on the frontend side.
INDICATOR_STYLES: tuple[str, ...] = ("ascii", "emoji", "kaomoji", "unicode")
DEFAULT_INDICATOR_STYLE: str = "kaomoji"


def set_hermes_home_override(path: str | Path | None) -> Token:
    """Set a context-local Hermes home override and return its reset token.

    This is for in-process, per-task scoping.  It deliberately does not mutate
    ``os.environ`` because that is shared by every thread in the process.
    """
    value: str | object = _UNSET if path is None else str(path)
    return _HERMES_HOME_OVERRIDE.set(value)


def reset_hermes_home_override(token: Token) -> None:
    """Restore the previous context-local Hermes home override."""
    _HERMES_HOME_OVERRIDE.reset(token)


def get_hermes_home_override() -> str | None:
    """Return the active context-local Hermes home override, if any."""
    override = _HERMES_HOME_OVERRIDE.get()
    if override is _UNSET or not override:
        return None
    return str(override)


def _get_platform_default_hermes_home() -> Path:
    """Return the platform-native default Hermes home path."""
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def _hermes_home_from_env() -> Path:
    """Resolve HERMES_HOME from the process environment only.

    Reads the ``HERMES_HOME`` env var, falling back to the platform-native
    default.  Deliberately ignores the context-local override installed by
    :func:`set_hermes_home_override`, so this reflects the process/launch
    scope rather than a per-task profile.  Shared by :func:`get_hermes_home`
    and :func:`get_process_hermes_home` so the two never drift.
    """
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    return _get_platform_default_hermes_home()


def _warn_profile_fallback_once() -> None:
    """Warn once when falling back to the default home while a profile is active.

    Guard: if a non-default profile is sticky-active but ``HERMES_HOME`` is
    unset, the fallback to the default profile is almost certainly wrong.
    """
    global _profile_fallback_warned
    if _profile_fallback_warned:
        return
    try:
        fallback_home = _get_platform_default_hermes_home()
        active_path = fallback_home / "active_profile"
        active = active_path.read_text(encoding="utf-8").strip() if active_path.exists() else ""
    except (UnicodeDecodeError, OSError):
        active = ""
    if active and active != "default":
        _profile_fallback_warned = True
        # Write directly to stderr.  We intentionally do NOT route this
        # through ``logging`` because (a) this function is called at
        # module-import time from 30+ sites, often before logging is
        # configured, and (b) root-logger propagation would double-emit
        # on consoles where a StreamHandler is already attached.
        msg = (
            f"[HERMES_HOME fallback] HERMES_HOME is unset but active "
            f"profile is {active!r}. Falling back to {fallback_home}, which "
            f"is the DEFAULT profile — not {active!r}. Any data this "
            f"process writes will land in the wrong profile. The "
            f"subprocess spawner should pass HERMES_HOME explicitly "
            f"(see issue #18594)."
        )
        try:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
        except Exception:
            pass


def get_hermes_home() -> Path:
    """Return the Hermes home directory (default: platform-native path).

    Resolution order: context-local override (see
    :func:`set_hermes_home_override`) → ``HERMES_HOME`` env var → the
    platform-native default.  This is the single source of truth — all other
    copies should import this.

    When ``HERMES_HOME`` is unset but an ``active_profile`` file indicates
    a non-default profile is active, logs a loud one-shot warning to
    ``errors.log`` so cross-profile data corruption is diagnosable instead
    of silent.  Behavior is unchanged otherwise — we still return
    the platform-native default — because raising here would brick 30+ module-level
    callers that import this at load time.  Subprocess spawners are
    expected to propagate ``HERMES_HOME`` explicitly (see the systemd
    template in ``hermes_cli/gateway.py`` and the kanban dispatcher in
    ``hermes_cli/kanban_db.py``).  See https://github.com/NousResearch/hermes-agent/issues/18594.
    """
    override = get_hermes_home_override()
    if override:
        return Path(override)

    if not os.environ.get("HERMES_HOME", "").strip():
        _warn_profile_fallback_once()

    return _hermes_home_from_env()


def get_process_hermes_home() -> Path:
    """Return the Hermes home for the running process, ignoring task overrides.

    Unlike :func:`get_hermes_home`, this never follows the context-local
    override set by :func:`set_hermes_home_override`.  It resolves only the
    process ``HERMES_HOME`` env var (falling back to the platform default),
    so it reflects the scope the process was launched under **as long as
    nothing mutates ``os.environ`` in-process**.

    Use this for machine/process-level dashboard-owned assets — theme YAML,
    dashboard plugin manifests — that live under the server's launch home and
    must stay visible even while a request is scoped to another profile (e.g.
    the embedded ``/chat`` running under ``--open-profile``).  Do NOT use it
    for genuinely profile-scoped data (memories, backups, checkpoints,
    provider config) — those should keep following the override.
    """
    return _hermes_home_from_env()


def get_default_hermes_root() -> Path:
    """Return the root Hermes directory for profile-level operations.

    In standard deployments this is the platform-native Hermes home
    (``~/.hermes`` on POSIX, ``%LOCALAPPDATA%\\hermes`` on native Windows).

    In Docker or custom deployments where ``HERMES_HOME`` points outside
    ``~/.hermes`` (e.g. ``/opt/data``), returns ``HERMES_HOME`` directly
    — that IS the root.

    In profile mode where ``HERMES_HOME`` is ``<root>/profiles/<name>``,
    returns ``<root>`` so that ``profile list`` can see all profiles.
    Works both for standard (``~/.hermes/profiles/coder``) and Docker
    (``/opt/data/profiles/coder``) layouts.

    Import-safe — no dependencies beyond stdlib.
    """
    native_home = _get_platform_default_hermes_home()
    env_home = os.environ.get("HERMES_HOME", "")
    if not env_home:
        return native_home
    env_path = Path(env_home)
    try:
        env_path.resolve().relative_to(native_home.resolve())
        # HERMES_HOME is under ~/.hermes (normal or profile mode)
        return native_home
    except ValueError:
        pass

    # Docker / custom deployment.
    # Check if this is a profile path: <root>/profiles/<name>
    # If the immediate parent dir is named "profiles", the root is
    # the grandparent — this covers Docker profiles correctly.
    if env_path.parent.name == "profiles":
        return env_path.parent.parent

    # Not a profile path — HERMES_HOME itself is the root
    return env_path


# ── Один корень профиля (contained profile root) ──────────────────────
# Контракт и обоснование: docs/product/plans/2026-09-14-contained-profile-root-implementation.md
# Спека: docs/product/specs/2026-09-13-tenant-profile-workspace-layout.md
#
# Движок знает ОДИН generic PROFILE_ROOT для всего изменяемого и приватного.
# Слов tenant/multiplex здесь нет и быть не должно — обвязка приходит сверху.
# Всё знание о корне живёт в этом модуле: пять резолверов ниже — единственный
# источник правды, остальные модули их зовут и своих копий не заводят.

ISOLATION_SHARED_HOST = "shared-host"
ISOLATION_CONTAINED = "contained"
ISOLATION_MODES: tuple[str, ...] = (ISOLATION_SHARED_HOST, ISOLATION_CONTAINED)

# Маркер режима в корне профиля. Лежит рядом с данными, поэтому режим уезжает
# вместе с каталогом при копировании/переносе на другую машину (§2.1 п. 3).
PROFILE_LAYOUT_MARKER = ".layout-version"
PROFILE_LAYOUT_VERSION = 2

_ISOLATION_ALIASES: dict[str, str] = {
    "shared-host": ISOLATION_SHARED_HOST,
    "shared_host": ISOLATION_SHARED_HOST,
    "sharedhost": ISOLATION_SHARED_HOST,
    "shared": ISOLATION_SHARED_HOST,
    "host": ISOLATION_SHARED_HOST,
    "upstream": ISOLATION_SHARED_HOST,
    "contained": ISOLATION_CONTAINED,
    "contain": ISOLATION_CONTAINED,
    "isolated": ISOLATION_CONTAINED,
    "isolation": ISOLATION_CONTAINED,
}

# Плейсхолдеры cwd: «выбери дефолт», а не путь оператора, поэтому корень
# профиля они не отменяют.
#
# Единственное объявление на весь движок. Их было четыре — здесь, в мосте
# terminal.* (``cli.py``), в ``gateway/cwd_placeholder.py`` и в
# ``tui_gateway/server.py``, — и они РАСХОДИЛИСЬ: здесь сравнение шло в
# нижнем регистре, в остальных трёх — как есть. ``terminal.cwd: Auto`` в
# config.yaml давало два разных ответа на один вопрос: мост считал это
# названным оператором путём и уводил contained-профиль в каталог с именем
# ``Auto``, резолвер — плейсхолдером и отдавал рабочую папку.
CWD_PLACEHOLDER_WORDS = frozenset({".", "auto", "cwd"})

# Прежнее приватное имя: им пользуется код внутри этого модуля.
_CWD_PLACEHOLDER_WORDS = CWD_PLACEHOLDER_WORDS


def is_cwd_placeholder(value: str | None) -> bool:
    """``True``, если значение ``terminal.cwd`` — «выбери дефолт», а не путь.

    Функция, а не сравнение с множеством на каждой стороне: правило
    нормализации (пробелы по краям, регистр) обязано быть ОДНО. Четыре
    копии множества выучили его по-разному именно потому, что каждая
    сравнивала сама.
    """
    return str(value or "").strip().lower() in CWD_PLACEHOLDER_WORDS

# ── Allowlist §6: что законно ЖИВЁТ вне корня в contained ────────────
# Список закрытый. Его читают сторож в CI, проверка doctor и документация,
# поэтому он здесь, а не в трёх местах. Добавление пункта — отдельный
# коммит с обоснованием.
#
# Два класса, и они разные:
#   SHARED_IMMUTABLE — публичные машинные ассеты: одинаковые для всех
#     профилей, обновляются скоупом УСТАНОВКИ, во время работы профиля
#     только читаются.
#   OS_RUNTIME — интерфейсы ОС к процессу: юниты, сокеты, реестр команд.
#     Это каналы управления, а не данные профиля.

PROFILE_ROOT_SHARED_IMMUTABLE: tuple[tuple[str, str], ...] = (
    ("~/.hermes/hermes-agent", "код движка и venv — публичные, общие, обновляются скоупом установки"),
    ("/opt/hermes", "код движка при пакетной установке — та же публичная копия"),
    ("~/.cache/ms-playwright", "сборка браузера: публичный бинарник, пинится явно"),
    ("~/.agent-browser/browsers", "сборка браузера agent-browser 0.26+, там же публичная"),
    ("/opt/playwright", "сборка браузера в образе — публичная, только читается"),
    ("~/.cache/huggingface", "веса модели распознавания речи, предзалиты рецептом"),
    ("~/.cache/uv", "кэш установки пакетов, не профиля"),
    ("~/.npm/_cacache", "кэш установки npm-пакетов, предзалит рецептом"),
    ("~/.omo/runtime", "бинарник ast-grep: одна копия на пользователя ОС, только исполняется"),
)

PROFILE_ROOT_OS_RUNTIME: tuple[tuple[str, str], ...] = (
    ("~/.config/systemd/user", "юниты службы: интерфейс ОС, изоляция именем юнита"),
    ("$XDG_RUNTIME_DIR", "рантайм-каталог пользователя ОС"),
    ("/run/user", "он же по умолчанию, когда переменная не выставлена"),
    ("~/.local/bin", "реестр команд пользователя ОС, принадлежит установке"),
    ("$XDG_DATA_HOME/applications", "ярлык рабочего стола: реестр приложений ОС"),
    ("/var/lib/docker", "writable-слой песочницы; принадлежит демону, "
                        "но СОДЕРЖИТ след работы профиля — см. удаление профиля"),
    ("/tmp/trix_rpc_*.sock", "сокет code-exec: канал управления без данных"),
)

# Места в коде, которые НАМЕРЕННО остаются на настоящем доме ОС. Сторож
# читает этот список отсюда, чтобы «почему тут Path.home()» имело один
# письменный ответ, а не одиннадцать устных.
OS_RUNTIME_SITES: tuple[str, ...] = (
    "hermes_cli/gateway.py",
    "hermes_cli/profiles.py",
    "hermes_cli/uninstall.py",
    "hermes_cli/update_cmd.py",
    "hermes_cli/setup_wizard/cli.py",
    "hermes_cli/trix_setup_service_check.py",
    "hermes_cli/trix_camofox_service.py",
    "hermes_cli/linux_desktop_entry.py",
    "hermes_cli/main.py",
    "gateway/status.py",
    "hermes_cli/gui_uninstall.py",
)

_isolation_warned: set[str] = set()


def reset_isolation_warnings() -> None:
    """Забыть уже показанные одноразовые предупреждения об изоляции.

    Нужно тестам: предупреждения одноразовые на процесс, а тестовые файлы
    внутри одного процесса проверяют их поштучно.
    """
    _isolation_warned.clear()


def _warn_isolation_once(key: str, message: str) -> None:
    """Напечатать предупреждение об изоляции ровно один раз за процесс.

    Пишем прямо в stderr по той же причине, что и
    :func:`_warn_profile_fallback_once`: этот модуль зовут на импорте
    десятки мест, задолго до настройки ``logging``.
    """
    if key in _isolation_warned:
        return
    _isolation_warned.add(key)
    try:
        sys.stderr.write(f"[hermes isolation] {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _env_value(name: str, env: dict[str, str] | None = None) -> str:
    """Прочитать переменную из переданного словаря, иначе из процесса."""
    if env is not None:
        val = env.get(name)
        if val is not None:
            return str(val).strip()
    return str(os.environ.get(name, "")).strip()


def _profile_root_str(env: dict[str, str] | None = None) -> str:
    """Строковый корень профиля: override → HERMES_HOME → платформенный дефолт.

    Отдельно от :func:`get_profile_root`, потому что резолверы окружения
    подпроцесса работают со словарём ``env``, а не с ``os.environ``.
    """
    override = get_hermes_home_override()
    if override:
        return override
    val = _env_value("HERMES_HOME", env)
    if val:
        return val
    return str(_get_platform_default_hermes_home())


# Маркер в корне не читается: файл есть, но версии из него не достать.
# Отдельное значение, потому что «нет файла» и «файл испорчен» — разные
# факты, и путать их нельзя (см. :func:`_read_layout_marker`).
MARKER_UNREADABLE = -1


def _read_layout_marker(root: str) -> int | None:
    """Вернуть версию раскладки из маркера в корне.

    Три исхода, и все три различимы:

    * ``None`` — файла нет. Это обычная старая установка, режим доопределяется
      следующими пунктами §2.1 и по умолчанию остаётся ``shared-host``.
    * число — версия раскладки.
    * :data:`MARKER_UNREADABLE` — файл ЕСТЬ, но содержимого нет или оно не
      разбирается. Раньше этот случай сливался с «файла нет», и оборванная
      запись маркера (полный диск, убитый процесс) тихо возвращала профиль
      в общий дом ОС. Fail-open ровно в ту сторону, где лежат чужие логины.
      Теперь это отдельный ответ, и решает по нему :func:`get_isolation_mode`.
    """
    path = os.path.join(root, PROFILE_LAYOUT_MARKER)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read(64).strip()
    except (OSError, UnicodeDecodeError):
        _warn_isolation_once(
            f"marker:{path}",
            f"{path}: маркер раскладки не читается; профиль остаётся "
            f"{ISOLATION_CONTAINED!r}. Проверьте файл.",
        )
        return MARKER_UNREADABLE
    if not raw:
        _warn_isolation_once(
            f"marker:{path}",
            f"{path}: маркер раскладки пуст — похоже на оборванную запись. "
            f"Профиль остаётся {ISOLATION_CONTAINED!r}; удалите файл, чтобы "
            f"вернуть {ISOLATION_SHARED_HOST!r}.",
        )
        return MARKER_UNREADABLE
    try:
        return int(raw)
    except ValueError:
        _warn_isolation_once(
            f"marker:{path}",
            f"{path}: не разобрать версию раскладки ({raw!r}); профиль "
            f"остаётся {ISOLATION_CONTAINED!r}. Проверьте файл.",
        )
        return MARKER_UNREADABLE


def read_layout_marker(root: str | os.PathLike) -> int | None:
    """Публичное чтение маркера раскладки — один разбор на весь движок.

    Раньше маркер разбирали в двух местах с разными правилами: здесь с
    лимитом чтения и предупреждением, в ``trix_layout.discover`` — без того
    и другого. Два разбора одного файла рано или поздно расходятся, и
    расходятся молча: инструмент оператора говорил бы одно, а рантайм
    делал другое.
    """
    return _read_layout_marker(str(root))


def write_layout_marker(root: str | os.PathLike, version: int | None = None) -> Path:
    """Записать маркер раскладки атомарно: временный файл рядом → ``rename``.

    Обычный ``write_text`` открывает файл на запись и усекает его. Обрыв
    между усечением и записью (полный диск, убитый процесс, падение
    питания) оставляет ПУСТОЙ маркер — а пустой маркер до этой работы
    означал «режим не определён» и возвращал профиль в общий дом ОС.
    Атомарная замена делает такое состояние недостижимым: читатель видит
    либо прежний файл, либо новый, третьего нет.

    Временный файл кладётся в тот же каталог (``rename`` атомарен только
    внутри одной ФС) и убирается за собой при любой ошибке.
    """
    # Импорт локальный: этот модуль зовут на бутстрапе, и лишних имён в его
    # глобальном пространстве быть не должно. ``dir=`` держит mkstemp в
    # корне профиля — ``gettempdir()`` он при этом не спрашивает.
    import tempfile

    root_path = Path(root)
    marker = root_path / PROFILE_LAYOUT_MARKER
    value = PROFILE_LAYOUT_VERSION if version is None else int(version)
    fd, tmp_name = tempfile.mkstemp(
        prefix=PROFILE_LAYOUT_MARKER + ".", suffix=".tmp", dir=str(root_path)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, str(marker))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return marker


def get_isolation_mode(env: dict[str, str] | None = None) -> str:
    """Вернуть режим изоляции профиля: ``shared-host`` или ``contained``.

    Порядок строгий:

    1. ``HERMES_ISOLATION`` — явное слово оператора, сильнее всего остального.
       Ключ ``isolation.mode`` из ``config.yaml`` мостится в эту же переменную
       (``cli.py`` и ``gateway/run.py``), поэтому отдельного пункта не нужно.
    2. Маркер ``<root>/.layout-version`` со значением ``2`` и старше. Он
       лежит рядом с данными, поэтому режим уезжает вместе с каталогом при
       копировании. Маркер, который есть, но не читается (оборванная запись,
       мусор, каталог вместо файла), тоже считается ``contained``: создание
       этого файла — осознанное действие, а «не разобрали» не повод вернуть
       профиль в общий дом ОС молча. Явная версия ``1`` — старая раскладка.
    3. Иначе ``shared-host``.

    Обратная совместимость по построению: любая существующая установка без
    маркера и без переменной попадает в пункт 3 и не меняет поведения ни на
    байт. Проверяется тестами, а не обещается:
    ``tests/trix/test_profile_root_backcompat.py``.

    **Почему здесь НЕТ автоопределения** (план §2.1 предлагал ещё два пункта,
    оба сняты по результатам исполнения кода):

    * ``is_container()`` — **ложно срабатывает на обычном хосте с Docker**.
      Её последняя проверка ищет маркеры ``docker``/``containerd`` в
      ``/proc/self/mountinfo``, а на хосте с запущенным Docker там лежат
      собственные overlay-монтирования демона (``/var/lib/docker/...``).
      Проверено исполнением 2026-09-14: на машине разработки без
      ``/.dockerenv`` и без cgroup-маркеров ``is_container()`` возвращает
      ``True``. Включать по ней containment означало бы молча переключить
      каждую клиентскую машину с docker-бэкендом терминала — ровно то, что
      запрещено первым инвариантом. Апстримный контейнерный путь при этом
      никуда не делся: ветка ``is_container()`` внутри
      :func:`get_subprocess_home` работает как работала.
    * раскладка ``<root>/profiles/<имя>`` — меняет поведение существующих
      многопрофильных установок на хосте. Это прямо зафиксировано апстримным
      тестом ``test_host_auto_keeps_real_home_when_profile_home_exists``:
      на хосте профиль НЕ прячет настоящие ``~/.ssh``, ``~/.gitconfig``.
      Новые профили получают containment маркером при создании, а не
      догадкой по форме пути.
    """
    raw = _env_value("HERMES_ISOLATION", env)
    if raw:
        mode = _ISOLATION_ALIASES.get(raw.lower())
        if mode:
            return mode
        _warn_isolation_once(
            "env:HERMES_ISOLATION",
            f"HERMES_ISOLATION={raw!r} — не режим; ожидается "
            f"{ISOLATION_SHARED_HOST!r} или {ISOLATION_CONTAINED!r}. "
            f"Значение игнорируется.",
        )

    root = _profile_root_str(env)
    marker = _read_layout_marker(root)
    if marker is None:
        return ISOLATION_SHARED_HOST
    # Испорченный маркер и маркер из будущего — оба в сторону contained.
    # «Не понял файл» не должно означать «пиши в общий дом».
    if marker == MARKER_UNREADABLE or marker >= PROFILE_LAYOUT_VERSION:
        return ISOLATION_CONTAINED
    return ISOLATION_SHARED_HOST


def is_contained(env: dict[str, str] | None = None) -> bool:
    """``True``, когда профиль обязан держать всё приватное внутри корня."""
    return get_isolation_mode(env) == ISOLATION_CONTAINED


def get_profile_root(env: dict[str, str] | None = None) -> Path:
    """Вернуть единый корень профиля — всё изменяемое и приватное живёт здесь.

    Сегодня это ровно :func:`get_hermes_home`. Отдельное имя существует, чтобы
    новый код выражал намерение («корень профиля»), а не деталь реализации
    («переменная HERMES_HOME»), и чтобы у контракта §1.1 плана был один адрес.
    ``get_hermes_home()`` намеренно НЕ переименовывается: 1776 вхождений и
    мёржи апстрима делают переименование неоправданно дорогим.
    """
    return Path(_profile_root_str(env))


def get_profile_home_dir(env: dict[str, str] | None = None) -> Path:
    """Вернуть ``HOME`` профиля — дом, который видят подпроцессы.

    ``contained`` → ``<root>/home``; ``shared-host`` → настоящий дом ОС.
    Каталог НЕ создаётся: это чистая функция, создание — дело
    :func:`ensure_profile_home_dir` в момент реального использования.
    """
    if is_contained(env):
        return get_profile_root(env) / "home"
    return Path(get_real_home(env))


def user_home_path(*parts: str, env: dict[str, str] | None = None) -> Path:
    """Вернуть путь под домом профиля: ``contained`` → ``<root>/home/<parts>``.

    Один хелпер для всех ПРИВАТНЫХ данных профиля, которые внешние CLI и
    плагины исторически держат под ``~``: учётные данные провайдеров
    (``~/.claude``, ``~/.codex``, ``~/.config/github-copilot``, ``~/.qwen``,
    ``~/.minimax``), состояние плагинов памяти (``~/.honcho``,
    ``~/.hindsight``), ключи песочниц (``~/.modal.toml``, ``~/.cua-driver``).
    Это данные ОДНОГО профиля, и в ``contained`` они обязаны уехать вместе с
    ним, иначе два профиля на машине видят чужие логины.

    В ``shared-host`` возвращает ровно то же, что ``Path.home() / parts`` —
    поведение апстрима не меняется.

    Чем это НЕ является: интерфейсы ОС (``~/.config/systemd/user``,
    ``~/.local/bin``, desktop entry, ``XDG_RUNTIME_DIR``) сюда не переводятся
    никогда — они принадлежат пользователю ОС и установке, а не профилю.
    Такие места помечены комментарием ``# OS_RUNTIME``.
    """
    return get_profile_home_dir(env).joinpath(*parts)


def get_profile_tmp_dir(env: dict[str, str] | None = None) -> Path:
    """Вернуть каталог временных файлов профиля.

    Порядок: явный ``HERMES_TMP_DIR`` → ``<root>/tmp`` в ``contained`` →
    внешний ``TMPDIR`` → ``/tmp``. Внешний ``TMPDIR`` в ``contained``
    сознательно игнорируется: иначе унаследованная от юнита или оболочки
    переменная уводила бы временные файлы профиля за корень.
    """
    explicit = _env_value("HERMES_TMP_DIR", env)
    if explicit:
        path = Path(explicit)
        _warn_if_outside_root("HERMES_TMP_DIR", path, env)
        return path
    if is_contained(env):
        return get_profile_root(env) / "tmp"
    external = _env_value("TMPDIR", env)
    return Path(external) if external else Path("/tmp")


# Приставки путей, по которым каталог опознаётся как ХОСТОВЫЙ: домашние
# каталоги POSIX и диск Windows. Одно объявление на движок — ``terminal_tool``
# импортирует отсюда же, иначе две копии разъезжаются.
HOST_CWD_PREFIXES = ("/Users/", "/home/", "C:\\", "C:/")

# Пути, которые существуют только ВНУТРИ песочницы: рабочая папка контейнера
# и его root. Зеркало ``tools/terminal_tool.py`` — там при монтировании cwd
# ровно эти две приставки не считаются хостовым каталогом.
_CONTAINER_ONLY_CWD_PREFIXES = ("/workspace", "/root")


def _terminal_cwd_is_host_path(value: str, env: dict[str, str] | None = None) -> bool:
    """Читается ли ``TERMINAL_CWD`` как путь на ХОСТЕ.

    ``terminal.cwd`` живёт в адресном пространстве бэкенда терминала, а не
    хоста. Для ``local`` это одно и то же. Для контейнерных и удалённых
    бэкендов — нет: поставляемый клиентский конфиг задаёт ``backend: docker``
    + ``cwd: /workspace``, и это путь внутри контейнера, которому на хосте не
    соответствует ничего. Читать его как хостовый — значит отчитываться о
    ветви профиля снаружи корня там, где её нет.

    Единственное исключение — ``docker`` с включённым
    ``docker_mount_cwd_to_workspace``: там ``terminal_tool`` берёт хостовый
    каталог, монтирует его и подменяет рабочий каталог контейнера на
    ``/workspace``. Агент в этом случае правда пишет на хост, и ветвь наружу
    корня настоящая — молчать о ней нельзя.
    """
    backend = (_env_value("TERMINAL_ENV", env) or "local").lower()
    if backend == "local":
        return True
    if backend != "docker":
        return False
    mount = _env_value("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", env).lower()
    if mount not in {"true", "1", "yes"}:
        return False
    # ``abspath`` — не косметика: ``terminal_tool`` нормализует кандидата
    # ровно так же, и относительный ``TERMINAL_CWD`` он раскрывает от текущего
    # каталога ХОСТА, после чего монтирует. Ограничься мы ``expanduser``,
    # такой путь не прошёл бы ``isabs`` — и доктор промолчал бы о ветви
    # наружу, которую терминал уже смонтировал.
    candidate = os.path.abspath(os.path.expanduser(value))
    if candidate.startswith(HOST_CWD_PREFIXES):
        return True
    return (
        not candidate.startswith(_CONTAINER_ONLY_CWD_PREFIXES)
        and os.path.isdir(candidate)
    )


def get_profile_workspace_dir(env: dict[str, str] | None = None) -> Path:
    """Вернуть рабочую папку профиля — канонический дефолт cwd.

    Явный ``terminal.cwd`` (мостится в ``TERMINAL_CWD``) выигрывает, но
    только когда он вообще про хост: это осознанный выбор оператора,
    ``USER_WORKSPACE`` в терминах §1.1. Слова ``.``/``auto``/``cwd`` —
    плейсхолдеры «выбери дефолт», а не путь. Путь в адресном пространстве
    песочницы (см. :func:`_terminal_cwd_is_host_path`) выбором оператора про
    хост не является: рабочая папка профиля тогда ``<root>/workspace``.
    """
    explicit = _env_value("TERMINAL_CWD", env)
    if (
        explicit
        and not is_cwd_placeholder(explicit)
        and _terminal_cwd_is_host_path(explicit, env)
    ):
        path = Path(explicit)
        _warn_if_outside_root("TERMINAL_CWD", path, env)
        return path
    return get_profile_root(env) / "workspace"


def _warn_if_outside_root(var: str, path: Path, env: dict[str, str] | None = None) -> None:
    """Предупредить один раз, если явное значение выводит ветвь за корень.

    Это не ошибка — режим ``custom`` из §1.3 плана ровно так и выглядит.
    Но оператор должен узнать об этом от движка, а не от аудита диска.
    """
    if not is_contained(env):
        return
    try:
        root = get_profile_root(env).resolve()
        if Path(os.path.abspath(path)).resolve().is_relative_to(root):
            return
    except (OSError, ValueError):
        return
    _warn_isolation_once(
        f"outside:{var}",
        f"{var}={path} лежит вне корня профиля {root} при contained-изоляции. "
        f"Эта ветвь данных профиля останется снаружи — так решил оператор.",
    )


def get_profile_root_tag(env: dict[str, str] | None = None) -> str:
    """Короткая устойчивая метка корня профиля для имён в общих каталогах.

    Нужна там, где путь обязан остаться коротким и снаружи корня (сокеты),
    но владельца надо уметь опознать: два профиля на одной машине не должны
    ни столкнуться именами, ни выглядеть безымянным мусором в ``/tmp``.
    """
    import hashlib

    root = os.path.normcase(os.path.abspath(_profile_root_str(env)))
    return hashlib.sha1(root.encode("utf-8", "surrogateescape")).hexdigest()[:12]


def get_runtime_socket_dir(env: dict[str, str] | None = None) -> str:
    """Вернуть каталог для unix-сокетов движка.

    Сокеты НЕ переезжают внутрь корня профиля, и это осознанно. Лимит
    ``sun_path`` — 108 байт; корень вида
    ``/home/user/.hermes/profiles/<имя>/tmp/`` съедает половину, и ``bind()``
    начинает падать на ровном месте. В терминах §6 плана сокет — это
    ``OS_RUNTIME``: канал управления без данных профиля, свой у каждого
    пользователя ОС. Имя при этом несёт метку корня
    (:func:`get_profile_root_tag`), чтобы владельца можно было опознать.

    Порядок: ``XDG_RUNTIME_DIR`` (приватный ``0700`` каталог пользователя,
    чистится при выходе из сессии) → ``/tmp``.
    """
    runtime_dir = _env_value("XDG_RUNTIME_DIR", env)
    if runtime_dir and os.path.isdir(runtime_dir) and os.access(runtime_dir, os.W_OK | os.X_OK):
        return runtime_dir
    return "/tmp"


def apply_process_tmpdir() -> str | None:
    """Направить ``TMPDIR`` самого процесса под корень профиля.

    Единственное место, где мы трогаем окружение СВОЕГО процесса, а не
    дочернего. Причина: ``tempfile.gettempdir()`` кэширует значение при
    первом вызове, а ``tempfile.*`` встречается в движке под две сотни раз —
    переводить поимённо нереально и не нужно. Точка входа знает свой профиль
    однозначно, поэтому зовётся она именно оттуда и как можно раньше.

    ``HOME`` процесса при этом НЕ меняется — никогда. Класс OS-интерфейсов
    (юниты systemd, ``~/.local/bin``, desktop entry) обязан остаться в
    настоящем доме, иначе установка ломается.

    Возвращает выставленный путь или ``None`` в ``shared-host``.
    """
    if not is_contained():
        return None
    path = str(ensure_profile_tmp_dir())
    for var in ("TMPDIR", "TMP", "TEMP"):
        os.environ[var] = path
    try:
        import tempfile

        # Сбросить кэш на случай, если gettempdir() уже успели позвать
        # раньше по цепочке импортов.
        tempfile.tempdir = None
    except Exception:
        pass
    return path


def restore_install_scope_home() -> str | None:
    """Вернуть процессу настоящий ``HOME`` ОС — для операций над УСТАНОВКОЙ.

    Скоуп установки — это скоуп пользователя ОС, а не профиля. Подключена
    функция ровно к тем точкам, которые этим скоупом и заняты, и список
    здесь — не пожелание, а проверяемый контракт
    (``tests/trix/test_profile_root_install_scope.py``):

    * ``hermes update`` — ``update_cmd._cmd_update_impl``;
    * ``hermes gateway install`` / ``uninstall`` — ``systemd_install``,
      ``systemd_uninstall``, ``launchd_install``, ``launchd_uninstall``
      через ``gateway._repair_install_scope_home``;
    * ``hermes uninstall`` — ``main.cmd_uninstall``.

    Установщик ``scripts/install.sh`` в этом списке отсутствует намеренно:
    это bash, он идёт в оболочке оператора и contained-окружения не видит
    никогда. Раньше докстринг называл его наравне с остальными, и получался
    контракт, часть которого невыполнима по построению.

    Их ``git``, ``uv`` и ``npm`` НЕ должны получать contained-окружение по
    двум причинам:

    * код, venv и их кэши — общие неизменяемые машинные ассеты, а не
      данные профиля;
    * рецепт установки специально предзаливает ``~/.cache/uv`` и
      ``~/.npm/_cacache`` (сотни мегабайт). С ``HOME=<root>/home`` каждый
      ``/update`` качал бы их заново на каждой машине.

    Где это стреляет по-настоящему: ``/update`` из чата запускает отдельный
    процесс ``hermes update`` из шлюза, и окружение этого процесса собрано
    шлюзом — то есть contained. Поэтому точка входа обновления зовёт эту
    функцию до первого подпроцесса и чинит ``HOME`` обратно из
    ``HERMES_REAL_HOME``, который в contained-окружении проставлен всегда.

    Возвращает восстановленный путь или ``None``, если чинить было нечего.
    """
    real_home = str(os.environ.get("HERMES_REAL_HOME", "")).strip()
    if not real_home:
        return None
    current = str(os.environ.get("HOME", "")).strip()
    if _norm_home_path(current) == _norm_home_path(real_home):
        return None
    if not os.path.isdir(real_home):
        return None
    os.environ["HOME"] = real_home
    # XDG идут за домом: иначе uv и npm разложат кэши по профильным
    # каталогам, а мы ровно этого и избегаем.
    for var in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        value = str(os.environ.get(var, "")).strip()
        if value and not _norm_home_path(value).startswith(_norm_home_path(real_home)):
            os.environ.pop(var, None)
    return real_home


def _bridge_isolation_mode(raw: str) -> str | None:
    """Записать разобранный режим в ``HERMES_ISOLATION``.

    Общее тело обоих мостов (по загруженному словарю и по файлу), чтобы
    правила валидации и приоритета жили в одном месте.
    """
    raw = str(raw or "").strip()
    if not raw:
        return None
    mode = _ISOLATION_ALIASES.get(raw.lower())
    if not mode:
        _warn_isolation_once(
            "config:isolation.mode",
            f"isolation.mode={raw!r} в config.yaml — не режим; ожидается "
            f"{ISOLATION_SHARED_HOST!r} или {ISOLATION_CONTAINED!r}. "
            f"Значение игнорируется.",
        )
        return None
    if os.environ.get("HERMES_ISOLATION", "").strip():
        return None
    os.environ["HERMES_ISOLATION"] = mode
    return mode


def bridge_isolation_config(config: dict | None) -> str | None:
    """Перенести ``isolation.mode`` из загруженного config.yaml в окружение.

    Один мост на оба входа (``cli.py`` и ``gateway/run.py``), чтобы они не
    разошлись. Ключ верхнего уровня, а не внутри ``terminal``: изоляция шире
    терминала — она про весь профиль.

    Зовётся уже после :func:`bridge_isolation_config_file`, поэтому обычно
    это no-op. Оставлен на месте намеренно: он видит конфиг ПОСЛЕ слияния с
    managed-overlay и проектным ``cli-config.yaml``, а файловый мост — нет.

    Уже выставленная переменная окружения НЕ перезаписывается: она источник
    №1 в §2.1 и главный аварийный выход (``HERMES_ISOLATION=shared-host`` в
    юните возвращает прежнее поведение без отката кода).

    Возвращает записанное значение или ``None``, если мостить было нечего.
    """
    if not isinstance(config, dict):
        return None
    section = config.get("isolation")
    if not isinstance(section, dict):
        return None
    return _bridge_isolation_mode(str(section.get("mode") or ""))


# Потолок чтения config.yaml на бутстрапе. Режим объявлен в первых строках
# файла; читать мегабайты ради двух слов незачем.
_ISOLATION_CONFIG_READ_LIMIT = 256 * 1024


def _strip_yaml_scalar(value: str) -> str:
    """Развернуть простой скалярный хвост ``key: <value>``.

    Снимает кавычки и хвостовой комментарий. Умышленно наивно: режим — это
    одно слово из закрытого списка, всё остальное всё равно отсеет
    :data:`_ISOLATION_ALIASES`.
    """
    value = value.strip()
    if value[:1] in {"'", '"'}:
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    head = value.split(" #", 1)[0]
    return head.strip().rstrip(",}").strip()


def _scan_isolation_mode(text: str) -> str | None:
    """Найти ``isolation.mode`` в тексте config.yaml без парсера YAML.

    Только блочная запись верхнего уровня — ровно то, что пишет шаблон
    профиля. Разбор нужен на бутстрапе, ДО починки venv и до первого
    стороннего импорта, поэтому PyYAML здесь звать нельзя.
    """
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] not in {" ", "\t"}:
            key, sep, tail = stripped.partition(":")
            in_section = bool(sep) and key.strip() == "isolation"
            # Поточная запись ``isolation: {mode: contained}`` — один
            # известный вариант, который стоит узнавать в лицо.
            if in_section and tail.strip().startswith("{"):
                body = tail.strip().lstrip("{")
                for part in body.split(","):
                    name, sub_sep, sub_val = part.partition(":")
                    if sub_sep and name.strip() == "mode":
                        return _strip_yaml_scalar(sub_val)
                in_section = False
            continue
        if not in_section:
            continue
        key, sep, tail = stripped.partition(":")
        if sep and key.strip() == "mode":
            return _strip_yaml_scalar(tail)
    return None


def bridge_isolation_config_file(env: dict[str, str] | None = None) -> str | None:
    """Прочитать ``isolation.mode`` прямо из ``<root>/config.yaml``.

    Нужен потому, что режим обязан быть известен РАНЬШЕ, чем процесс
    впервые попросит временный файл или дефолтный cwd, а полноценная
    загрузка конфига к этому моменту ещё не случилась. Раньше мост стоял на
    сотни строк позже :func:`apply_process_tmpdir`, и config-маршрут не
    покрывал ни TMPDIR процесса, ни cwd терминала: ``tempfile.gettempdir()``
    уже закэшировал ``/tmp``, а ветвь cwd уже спросила ``is_contained()`` и
    получила ``False``.

    Ничего не делает, когда ``HERMES_ISOLATION`` уже задана (источник №1)
    или когда оператор попросил ``--ignore-user-config``: пропускать
    пользовательский конфиг наполовину хуже, чем не пропускать вовсе.

    Возвращает записанный режим или ``None``.
    """
    if _env_value("HERMES_ISOLATION", env):
        return None
    if _env_value("HERMES_IGNORE_USER_CONFIG", env) == "1":
        return None
    path = os.path.join(_profile_root_str(env), "config.yaml")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read(_ISOLATION_CONFIG_READ_LIMIT)
    except (OSError, UnicodeDecodeError):
        return None
    raw = _scan_isolation_mode(text)
    if not raw:
        return None
    return _bridge_isolation_mode(raw)


# ── Выбор корня профиля ДО первого вопроса о режиме ─────────────────────
#
# Режим изоляции — свойство КОРНЯ: он читается из ``<корень>/.layout-version``
# и ``<корень>/config.yaml``. Значит, вопрос «какой корень» обязан быть решён
# раньше вопроса «какой режим», иначе ответ относится к чужому каталогу.
#
# У ``hermes`` корень выбирается двумя способами — флагом ``-p``/``--profile``
# и липким ``<root>/active_profile``, — и оба разбираются в
# ``hermes_cli.main._apply_profile_override()``, которая физически не может
# стоять раньше: ей нужен ``hermes_cli.profiles``, то есть импорт пакета, а
# бутстрап обязан отработать ДО любого импорта, который дотянется до
# ``tempfile.gettempdir()`` (кэш на весь процесс).
#
# Поэтому разбор argv и чтение липкого файла живут здесь — на stdlib, без
# единого импорта из ``hermes_cli``. ``_apply_profile_override()`` зовёт эти
# же функции, так что двух разных ответов на один вопрос быть не может.

# Зеркало ``hermes_cli.profiles._PROFILE_ID_RE``. Держится строкой, а не
# импортом, ровно по причине выше; расхождение ловит
# ``tests/trix/test_profile_root_bootstrap.py``.
_PROFILE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"

# Флаги ``hermes``, у которых есть значение: их значение нельзя спутать с
# именем профиля. Список повторяет ``hermes_cli.main``.
_PROFILE_SCAN_VALUE_FLAGS = frozenset({
    "-z", "--oneshot",
    "-m", "--model",
    "--provider",
    "-t", "--toolsets",
    "-r", "--resume",
    "-s", "--skills",
    "--usage-file",
    "--in",
})
_PROFILE_SCAN_OPTIONAL_VALUE_FLAGS = frozenset({"-c", "--continue"})


def _inside_mcp_add_args(argv: list[str], index: int) -> bool:
    """``True``, когда argv дошёл до ``hermes mcp add ... --args <чужой argv>``.

    За этой границей флаги принадлежат дочерней MCP-команде (у Docker MCP
    Toolkit есть свой ``--profile``), а не выбору профиля Hermes.
    """
    try:
        mcp_index = argv.index("mcp", 0, index)
        argv.index("add", mcp_index + 1, index)
    except ValueError:
        return False
    return True


def scan_profile_flag(argv: list[str]) -> tuple[str | None, int, int | None]:
    """Найти ``-p``/``--profile`` в argv точки входа.

    Возвращает ``(имя, сколько_элементов_снять, индекс)``. Чистая функция:
    ни окружения, ни файловой системы. Флаг исторически работал и после
    подкоманды (``hermes chat -p coder``), поэтому скан широкий — с двумя
    исключениями: область ``mcp add --args`` и значения флагов из
    ``_PROFILE_SCAN_VALUE_FLAGS``.

    Значения, которые не могут быть именем профиля (``-p no:xdist`` от
    pytest), отсеиваются здесь же — вызывающий не должен передавать их
    дальше в резолвер, который обязан на них ругаться и выходить.
    """
    import re

    profile_name: str | None = None
    consume = 0
    profile_index: int | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            break
        if arg == "--args" and _inside_mcp_add_args(argv, i):
            break
        if arg in {"--profile", "-p"} and i + 1 < len(argv):
            profile_name = argv[i + 1]
            consume = 2
            profile_index = i
            break
        if arg.startswith("--profile="):
            profile_name = arg.split("=", 1)[1]
            consume = 1
            profile_index = i
            break
        if "=" not in arg and arg in _PROFILE_SCAN_VALUE_FLAGS and i + 1 < len(argv):
            i += 2
        elif (
            "=" not in arg
            and arg in _PROFILE_SCAN_OPTIONAL_VALUE_FLAGS
            and i + 1 < len(argv)
            and not argv[i + 1].startswith("-")
        ):
            i += 2
        else:
            i += 1

    if profile_name is not None and consume == 2:
        if not re.match(_PROFILE_ID_PATTERN, profile_name):
            return None, 0, None

    return profile_name, consume, profile_index


def sticky_profile_name() -> str | None:
    """Имя профиля из ``<корень>/active_profile``, если он выбран липко.

    ``default`` и пустая строка — это «корень и есть профиль», то есть
    None. Исключение ``HERMES_S6_SUPERVISED_CHILD``: у надзираемого слота
    ``gateway-default`` личность фиксирована, и следовать липкому выбору
    ему нельзя — иначе переключение активного профиля молча уводит
    дефолтный шлюз в чужой корень.
    """
    if os.environ.get("HERMES_S6_SUPERVISED_CHILD"):
        return None
    try:
        active_path = get_default_hermes_root() / "active_profile"
        name = active_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not name or name == "default":
        return None
    return name


def selected_profile_root(argv: list[str] | None = None) -> str | None:
    """Корень профиля, выбранного этим запуском, или ``None``.

    ``None`` значит «ничего менять не надо»: профиль не назван, либо
    ``HERMES_HOME`` уже указывает на конкретный профиль и ему следует
    верить (контракт наследования дочерним процессом, issue #22502).

    Каталога не создаёт и на несуществующий профиль не ругается — ошибку
    печатает CLI, а бутстрапу положено промолчать.
    """
    import re

    argv = list(sys.argv[1:]) if argv is None else list(argv)
    name, consume, _index = scan_profile_flag(argv)

    if name is None:
        # ``HERMES_HOME`` уже конкретный профиль — верим ему. Если же он
        # указывает на корень (systemd пишет ``HERMES_HOME=/root/.hermes``),
        # липкий выбор всё ещё сильнее: пользователь мог переключиться
        # через ``hermes profile use``.
        env_home = os.environ.get("HERMES_HOME", "").strip()
        if env_home and Path(env_home).parent.name == "profiles":
            return None
        name = sticky_profile_name()
        if name is None:
            return None

    canon = str(name).strip()
    if canon.casefold() == "default":
        return None
    canon = canon.lower()
    if not re.match(_PROFILE_ID_PATTERN, canon):
        return None

    # Зеркало ``hermes_cli.profiles.get_profile_dir``: именованные профили
    # лежат ровно в ``<корень>/profiles/<имя>``.
    candidate = get_default_hermes_root() / "profiles" / canon
    try:
        if candidate.is_dir():
            return str(candidate)
    except OSError:
        return None

    # ``sudo hermes -p <имя>``: хранилище профилей принадлежит тому, кто
    # позвал sudo, а не root.
    if consume:
        return sudo_user_profile_root(canon)
    return None


def sudo_user_profile_root(name: str) -> str | None:
    """Корень профиля в домашнем каталоге позвавшего ``sudo``, если он есть.

    ``sudo hermes -p <имя>``: root выполняет привилегированное действие
    (установка, запуск службы), а хранилище профилей при этом принадлежит
    тому пользователю ОС, который позвал ``sudo``. Разбор флага случается
    раньше argparse, поэтому ``--run-as-user`` ещё недоступен, и лучший
    доступный сигнал — ``SUDO_USER``.

    Обращение к дому пользователя ОС здесь намеренное: вопрос ровно про
    него — чьё это хранилище профилей. Живёт функция в одном месте, потому
    что ответ нужен дважды: бутстрапу изоляции (до любого импорта
    ``hermes_cli``) и ``hermes_cli.main._apply_profile_override``.
    """
    if name == "default":
        return None
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if not sudo_user or sudo_user == "root":
        return None
    try:
        import pwd

        home = Path(pwd.getpwnam(sudo_user).pw_dir)
    except Exception:
        return None
    candidate = home / ".hermes" / "profiles" / name
    try:
        if candidate.is_dir():
            return str(candidate)
    except OSError:
        return None
    return None


def bootstrap_profile_root(argv: list[str] | None = None) -> str | None:
    """Выставить ``HERMES_HOME`` по профилю, выбранному этим запуском.

    Возвращает записанный путь или ``None``, когда менять нечего.
    Идемпотентна: повторный вызов на том же argv запишет то же значение.
    """
    root = selected_profile_root(argv)
    if root is None:
        return None
    os.environ["HERMES_HOME"] = root
    return root


def bootstrap_process_isolation(
    *, select_profile_root: bool = False
) -> tuple[str | None, str | None]:
    """Полный бутстрап изоляции для точки входа — один вызов, строгий порядок.

    0. корень профиля (только при ``select_profile_root``);
    1. режим из ``config.yaml`` → ``HERMES_ISOLATION``;
    2. только после этого ``TMPDIR`` самого процесса.

    Порядок и есть весь смысл функции: шаг 2 читает режим, шаг 1 его
    объявляет, а шаг 0 отвечает на вопрос «режим ЧЕГО». Зовётся из всех
    четырёх установленных точек входа (``hermes``, ``gateway``,
    ``hermes-agent``, ``hermes-acp``) как можно раньше — до импорта
    ``tempfile``.

    ``select_profile_root`` задаёт только ``hermes``: это единственная
    точка входа, которая сама выбирает профиль (флагом ``-p`` и липким
    ``active_profile``). Шлюз, ``hermes-agent`` и ``hermes-acp`` запускает
    надзиратель, который обязан передать ``HERMES_HOME`` явно (issue
    #18594), и додумывать за него профиль здесь нельзя — иначе
    переключение активного профиля молча уводило бы чужой процесс.

    Ни один шаг не имеет права помешать процессу стартовать: в худшем
    случае временные файлы останутся там же, где были до этой работы.

    Возвращает ``(режим_из_конфига, выставленный_TMPDIR)``.
    """
    mode: str | None = None
    tmpdir: str | None = None
    if select_profile_root:
        try:
            bootstrap_profile_root()
        except Exception:
            pass
    try:
        mode = bridge_isolation_config_file()
    except Exception:
        pass
    try:
        tmpdir = apply_process_tmpdir()
    except Exception:
        pass
    return mode, tmpdir


def ensure_profile_home_dir(env: dict[str, str] | None = None) -> Path:
    """Вернуть ``HOME`` профиля, создав каталог при необходимости (``0700``).

    Отдельно от чистого :func:`get_profile_home_dir`, потому что создать
    каталог обязан тот, кто собирается им пользоваться. В ``contained`` это
    критично: :func:`_profile_home_path` включает профильный дом ФАКТОМ
    наличия каталога, и без создания режим молча выродился бы в shared-host.
    """
    path = get_profile_home_dir(env)
    if is_contained(env):
        _ensure_private_dir(path)
    return path


def ensure_profile_tmp_dir(env: dict[str, str] | None = None) -> Path:
    """Вернуть каталог временных файлов профиля, создав его (``0700``)."""
    path = get_profile_tmp_dir(env)
    _ensure_private_dir(path)
    return path


def ensure_profile_workspace_dir(env: dict[str, str] | None = None) -> Path:
    """Вернуть рабочую папку профиля, создав её (``0700``)."""
    path = get_profile_workspace_dir(env)
    _ensure_private_dir(path)
    return path


def _ensure_private_dir(path: Path) -> None:
    """Создать каталог с правами ``0700``, молча пережив гонку и отказ ФС."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def get_optional_skills_dir(default: Path | None = None) -> Path:
    """Return the optional-skills directory, honoring package-manager wrappers.

    Packaged installs may ship ``optional-skills`` outside the Python package
    tree and expose it via ``HERMES_OPTIONAL_SKILLS``.
    """
    override = os.getenv("HERMES_OPTIONAL_SKILLS", "").strip()
    if override:
        return Path(override)
    if default is not None:
        return default
    return get_hermes_home() / "optional-skills"


def get_optional_mcps_dir(default: Path | None = None) -> Path:
    """Return the optional-mcps directory, honoring package-manager wrappers.

    Mirrors :func:`get_optional_skills_dir` for the MCP catalog (Nous-approved
    Model Context Protocol servers shipped with the repo but disabled by
    default). Packaged installs may ship ``optional-mcps`` outside the Python
    package tree and expose it via ``HERMES_OPTIONAL_MCPS``.
    """
    override = os.getenv("HERMES_OPTIONAL_MCPS", "").strip()
    if override:
        return Path(override)
    if default is not None:
        return default
    return get_hermes_home() / "optional-mcps"


def get_bundled_skills_dir(default: Path | None = None) -> Path:
    """Return the bundled skills directory for source and packaged installs.

    Resolution order:
        1. ``HERMES_BUNDLED_SKILLS`` env var (Nix wrapper / explicit override)
        2. Caller-supplied ``default`` (typically the source-checkout path)
        3. ``<HERMES_HOME>/skills`` last-resort
    """
    override = os.getenv("HERMES_BUNDLED_SKILLS", "").strip()
    if override:
        return Path(override)
    if default is not None:
        return default
    return get_hermes_home() / "skills"


def get_hermes_dir(
    new_subpath: str,
    old_name: str,
    *,
    home: Path | None = None,
) -> Path:
    """Resolve a Hermes subdirectory with backward compatibility.

    New installs get the consolidated layout (e.g. ``cache/images``).
    Existing installs that already have the old path (e.g. ``image_cache``)
    keep using it — no migration required.

    A bare empty ``<old_name>/`` directory does **not** count as "the
    legacy install is in use" — install scaffolds, manual ``mkdir`` work,
    and cleared-then-abandoned locations all create empty stubs that
    would otherwise silently shadow real data populated at
    ``<new_subpath>/``. See #27602 for the pairing-store regression where
    a dormant empty ``pairing/`` orphaned approved-user data in
    ``platforms/pairing/``.

    Args:
        new_subpath: Preferred path relative to HERMES_HOME (e.g. ``"cache/images"``).
        old_name: Legacy path relative to HERMES_HOME (e.g. ``"image_cache"``).
        home: Optional explicit Hermes home. Profile-aware callers that manage
            more than one home in the same process use this instead of
            temporarily mutating the process or context-local HERMES_HOME.

    Returns:
        Absolute ``Path`` — legacy location if it exists with content,
        otherwise the new location.
    """
    home = home or get_hermes_home()
    old_path = home / old_name
    if _legacy_path_has_content(old_path):
        return old_path
    return home / new_subpath


def iter_hermes_node_dirs(home: Path | None = None) -> list[Path]:
    """Return Hermes-managed Node.js directories in preferred lookup order.

    Windows installs from ``scripts/install.ps1`` unpack portable Node directly
    into ``%LOCALAPPDATA%\\hermes\\node``. POSIX installs use
    ``$HERMES_HOME/node/bin``. Include both shapes on every platform so mixed
    or migrated installs still work.
    """
    root = home or get_hermes_home()
    dirs = [root / "node"]
    bin_dir = root / "node" / "bin"
    # NOTE: keep this ordering in sync with hermesManagedNodePathEntries() in
    # apps/desktop/electron/backend-env.ts — the Electron main process is Node
    # and cannot import this module, so the platform-ordering rule is mirrored
    # there (once; main.ts imports it rather than keeping its own copy).
    if sys.platform == "win32":
        return dirs + [bin_dir]
    return [bin_dir] + dirs


def _candidate_node_command_names(command: str) -> list[str]:
    base = Path(command).name
    if sys.platform != "win32" or "." in base:
        return [base]
    if base.lower() == "npm":
        # Prefer npm.cmd. PowerShell may block npm.ps1 by execution policy, and
        # CreateProcess cannot launch a bare .ps1 the way it can launch .cmd.
        return ["npm.cmd", "npm.exe", "npm"]
    if base.lower() == "npx":
        return ["npx.cmd", "npx.exe", "npx"]
    if base.lower() == "node":
        return ["node.exe", "node"]
    return [f"{base}.cmd", f"{base}.exe", base]


_HERMES_NODE_TARGET_MAJOR = int(os.environ.get("HERMES_NODE_TARGET_MAJOR", "22"))
_managed_node_heal_attempted = False
_NODE_BOOTSTRAP_SCRIPT = Path(__file__).resolve().parent / "scripts" / "lib" / "node-bootstrap.sh"


def node_tool_runnable(path: str | None) -> bool:
    """Return True only when *path* is a Node/npm/npx binary that actually runs.

    Hermes-managed Node trees live under ``$HERMES_HOME/node`` (or a profile's
    ``HERMES_HOME``). A partial upgrade or interrupted install can leave
    ``bin/npm`` behind while ``lib/cli.js`` is missing — the wrapper exists but
    immediately throws ``MODULE_NOT_FOUND``. ``find_hermes_node_executable``
    used to trust file presence alone, so ``hermes update`` would pick that
    broken npm and fail the Node refresh / web UI build.

    Probe with ``--version`` (same pattern as :func:`agent_browser_runnable`) so
    broken managed wrappers are detected before use.
    """
    if not path:
        return False
    candidate = Path(path)
    if sys.platform == "win32":
        if not candidate.is_file():
            return False
    elif not os.path.exists(path) or not os.access(path, os.X_OK):
        return False

    import subprocess

    try:
        from hermes_cli._subprocess_compat import windows_hide_flags

        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            timeout=10,
            env=with_hermes_node_path(),
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    return result.returncode == 0


def hermes_managed_node_tree_present(home: Path | None = None) -> bool:
    """Return True when any Hermes-managed node/npm/npx shim exists on disk."""
    names = set()
    for command in ("node", "npm", "npx"):
        names.update(_candidate_node_command_names(command))
    for directory in iter_hermes_node_dirs(home):
        for name in names:
            candidate = directory / name
            if candidate.is_file() and (
                sys.platform == "win32" or os.access(candidate, os.X_OK)
            ):
                return True
    return False


def _heal_managed_node_windows() -> bool:
    """Redownload the portable Node zip into ``%HERMES_HOME%\\node`` on Windows."""
    import re
    import tempfile
    import urllib.request
    import zipfile

    arch = (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE", "")).lower()
    if arch in ("amd64", "x86_64"):
        node_arch = "x64"
    elif arch == "arm64":
        node_arch = "arm64"
    elif arch in ("x86",):
        node_arch = "x86"
    else:
        return False

    home = get_hermes_home()
    index_url = f"https://nodejs.org/dist/latest-v{_HERMES_NODE_TARGET_MAJOR}.x/"
    try:
        with urllib.request.urlopen(index_url, timeout=60) as response:
            index_html = response.read().decode("utf-8", errors="replace")
    except OSError:
        return False

    match = re.search(
        rf"node-v{_HERMES_NODE_TARGET_MAJOR}\.\d+\.\d+-win-{node_arch}\.zip",
        index_html,
    )
    if not match:
        return False

    zip_name = match.group(0)
    download_url = f"{index_url}{zip_name}"
    try:
        with urllib.request.urlopen(download_url, timeout=300) as response:
            zip_bytes = response.read()
    except OSError:
        return False

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            zip_path = tmp_path / zip_name
            zip_path.write_bytes(zip_bytes)
            extract_dir = tmp_path / "extract"
            extract_dir.mkdir()
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(extract_dir)
            extracted = next(extract_dir.glob("node-v*"), None)
            if extracted is None or not extracted.is_dir():
                return False
            target = home / "node"
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(extracted), str(target))
    except OSError:
        return False

    return node_tool_runnable(str(target / "node.exe"))


def _bootstrap_managed_node_posix() -> bool:
    """Install a fresh managed Node under ``$HERMES_HOME/node`` on POSIX.

    Shells out to ``_nb_install_bundled_node`` in ``scripts/lib/node-bootstrap.sh``
    (the same pinned-nodejs.org path ``install.sh`` uses), so the resulting
    tree matches what a normal install would have produced. Runs with
    ``HERMES_NODE_SKIP_LINKS=1`` so the user's own node/npm on PATH is not
    shadowed by ``~/.local/bin`` symlinks.
    """
    if not _NODE_BOOTSTRAP_SCRIPT.is_file():
        return False

    import subprocess

    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{_NODE_BOOTSTRAP_SCRIPT}" && _nb_install_bundled_node',
            ],
            env={
                **os.environ,
                "HERMES_HOME": str(get_hermes_home()),
                # Private provisioning: do not symlink node/npm/npx into
                # ~/.local/bin — the user has their own toolchain on PATH and
                # this tree must not shadow it.
                "HERMES_NODE_SKIP_LINKS": "1",
            },
            capture_output=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def bootstrap_hermes_managed_node() -> str | None:
    """Install a Hermes-managed Node tree and return its npm path.

    Used when the only Node/npm on the machine belongs to the user (system,
    nvm, brew, Nix) and cannot satisfy the repo's ``engines`` requirements —
    Hermes never modifies a toolchain it does not own, so instead it provisions
    its own tree under ``$HERMES_HOME/node`` (the same tree a fresh install
    creates) and works with that.

    Returns the managed npm executable path on success, ``None`` on failure.
    No-ops (returning the existing npm) when a healthy managed tree is already
    present.
    """
    existing = find_hermes_node_executable("npm")
    if existing:
        return existing

    if sys.platform == "win32":
        ok = _heal_managed_node_windows()
    else:
        ok = _bootstrap_managed_node_posix()
    if not ok:
        return None

    for directory in iter_hermes_node_dirs():
        for name in _candidate_node_command_names("npm"):
            candidate = directory / name
            if candidate.is_file() and (
                sys.platform == "win32" or os.access(candidate, os.X_OK)
            ):
                resolved = str(candidate)
                if node_tool_runnable(resolved):
                    return resolved
    return None


def heal_hermes_managed_node() -> bool:
    """Redownload Hermes-managed Node when the tree exists but is broken.

    Runs at most once per process. POSIX installs shell out to
    ``heal_managed_node`` in ``scripts/lib/node-bootstrap.sh``; Windows
    downloads the portable zip directly (same source as ``install.ps1``).
    """
    global _managed_node_heal_attempted
    if _managed_node_heal_attempted:
        return False
    if not hermes_managed_node_tree_present():
        return False
    _managed_node_heal_attempted = True

    if sys.platform == "win32":
        return _heal_managed_node_windows()

    if not _NODE_BOOTSTRAP_SCRIPT.is_file():
        return False

    import subprocess

    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{_NODE_BOOTSTRAP_SCRIPT}" && heal_managed_node',
            ],
            env={**os.environ, "HERMES_HOME": str(get_hermes_home())},
            capture_output=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _managed_node_tree_outdated(home: Path | None = None) -> bool:
    """Return True when the managed tree's node runs but is below the target major.

    An outdated managed Node (e.g. a 22 tree from an older install) heals the
    same way a broken one does: :func:`find_hermes_node_executable` triggers
    the once-per-process heal, which redownloads
    ``latest-v{_HERMES_NODE_TARGET_MAJOR}.x`` — so existing users are upgraded
    on next launch, not just on the next installer re-run. Mirrors
    ``_nb_managed_node_outdated`` in ``scripts/lib/node-bootstrap.sh``.
    """
    import subprocess

    for directory in iter_hermes_node_dirs(home):
        for name in _candidate_node_command_names("node"):
            candidate = directory / name
            if not candidate.is_file() or (
                sys.platform != "win32" and not os.access(candidate, os.X_OK)
            ):
                continue
            try:
                from hermes_cli._subprocess_compat import windows_hide_flags

                result = subprocess.run(
                    [str(candidate), "--version"],
                    capture_output=True,
                    timeout=10,
                    creationflags=windows_hide_flags(),
                )
                major = int(result.stdout.decode().strip().lstrip("v").split(".")[0])
            except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
                return False  # broken, not outdated — the runnable probe handles it
            return major < _HERMES_NODE_TARGET_MAJOR
    return False


def find_hermes_node_executable(command: str) -> str | None:
    """Return a Hermes-managed Node/npm executable path, healing broken trees.

    Outdated trees (node major below ``_HERMES_NODE_TARGET_MAJOR``) heal the
    same way broken ones do — the once-per-process heal redownloads the target
    major, upgrading existing users on next launch rather than next reinstall.
    When the heal fails (offline, download error), an outdated-but-runnable
    tree is still returned: old Node beats no Node.
    """
    names = _candidate_node_command_names(command)

    def _first_runnable() -> tuple[str | None, bool]:
        broken = False
        for directory in iter_hermes_node_dirs():
            for name in names:
                candidate = directory / name
                if candidate.is_file() and (
                    sys.platform == "win32" or os.access(candidate, os.X_OK)
                ):
                    resolved = str(candidate)
                    if node_tool_runnable(resolved):
                        return resolved, broken
                    broken = True
        return None, broken

    resolved, broken_present = _first_runnable()
    needs_heal = broken_present or (
        resolved is not None and _managed_node_tree_outdated()
    )
    if needs_heal and heal_hermes_managed_node():
        healed, _ = _first_runnable()
        if healed:
            return healed
    return resolved


def find_node_executable_on_path(command: str) -> str | None:
    """Return a Node/npm executable from PATH with Windows shim ordering.

    ``shutil.which("npm")`` can resolve an extensionless npm shim before the
    ``.cmd`` shim on Windows. Python's CreateProcess cannot execute that shim
    directly, so prefer the launchable variants explicitly for Hermes-owned
    subprocesses.
    """
    if sys.platform != "win32":
        return shutil.which(command)

    command_str = str(command)
    has_path_separator = any(
        sep and sep in command_str for sep in (os.sep, os.altsep, "/", "\\")
    )
    if has_path_separator:
        return command_str if Path(command_str).is_file() else None

    for name in _candidate_node_command_names(command_str):
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                continue
            candidate = Path(directory) / name
            if candidate.is_file():
                return str(candidate)
    return None


def find_node_executable(command: str) -> str | None:
    """Resolve a Node.js command, preferring healthy Hermes-managed installs.

    This is for Hermes-owned subprocesses that should not be broken by a bad,
    missing, or elevation-triggering system Node/npm on PATH. When a managed
    tree exists but cannot be healed, returns ``None`` instead of falling back
    to system npm on PATH.
    """
    managed = find_hermes_node_executable(command)
    if managed:
        return managed
    if hermes_managed_node_tree_present():
        return None
    return find_node_executable_on_path(command)


def with_hermes_node_path(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return *env* with Hermes-managed Node directories prepended to PATH."""
    merged = dict(os.environ if env is None else env)
    existing = merged.get("PATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    managed = [str(path) for path in iter_hermes_node_dirs() if path.is_dir()]
    for entry in reversed(managed):
        if entry not in parts:
            parts.insert(0, entry)
    merged["PATH"] = os.pathsep.join(parts)
    return merged


def agent_browser_runnable(path: str | None) -> bool:
    """Return True only when *path* is an agent-browser CLI that actually runs.

    A bare presence check (``shutil.which`` / ``Path.exists``) is not enough:
    agent-browser's npm ``postinstall`` re-points a *global* install symlink
    (e.g. ``/opt/homebrew/bin/agent-browser``) at our local
    ``node_modules/agent-browser/bin/...`` binary, which then disappears on the
    next ``hermes update`` — leaving a **dangling symlink** that ``which`` still
    reports but exec fails on with exit 127 (issue #48521). Callers that trust
    such a path silently break every browser tool.

    This validates the candidate by resolving it to a real, executable file and
    running ``--version`` with a short timeout. Returns True only on a clean
    (exit 0) run, so a dead/wrong-arch/hung binary is rejected and the caller
    can fall through to the next resolution candidate.

    Special cases:
      * ``None`` / empty → False.
      * The ``"npx agent-browser"`` fallback form (contains a space, not a real
        file) → True; npx resolves and validates the package at run time, so
        there is nothing to stat here.
    """
    if not path:
        return False
    # The npx fallback is a two-token command string, not a filesystem path.
    if " " in path and path.split()[0].endswith("npx"):
        return True
    # exists() follows symlinks — a dangling link returns False here, so we
    # never even spawn a subprocess for the broken-link case.
    if not os.path.exists(path) or not os.access(path, os.X_OK):
        return False
    import subprocess

    try:
        from hermes_cli._subprocess_compat import windows_hide_flags

        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            timeout=10,
            env=with_hermes_node_path(),
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    return result.returncode == 0


def _legacy_path_has_content(path: Path) -> bool:
    """Return ``True`` iff ``path`` exists and has content worth honouring.

    A populated *directory* (any entry inside) counts. A non-directory
    file at ``path`` also counts — the consumer presumably wrote it.
    An empty directory does **not** count, so a stale empty
    legacy stub falls through to the new layout. If the path cannot be
    inspected (``PermissionError`` on ``stat``/``iterdir``, or any other
    ``OSError`` short of "not found"), assume occupied so we don't
    accidentally orphan legacy data. Only a genuine
    ``FileNotFoundError`` counts as absent.

    Symlinks are resolved before judging content: a symlink pointing at a
    populated directory (or any existing non-directory target) counts, but
    a **dangling** symlink (broken target) does **not** — it must not be
    allowed to shadow populated new-layout data, matching the old
    ``exists()`` gate's behaviour for broken links.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        # PermissionError on a parent, or any other inspection failure:
        # treat as occupied rather than silently orphaning legacy data.
        return True
    if stat.S_ISLNK(st.st_mode):
        # Resolve the link's target. A dangling symlink has no content and
        # must not shadow the new layout; a valid one is judged on its target.
        try:
            target_st = path.stat()  # follows the link
        except FileNotFoundError:
            return False  # dangling symlink → fall through to new layout
        except OSError:
            return True  # can't resolve → assume occupied, don't orphan data
        if not stat.S_ISDIR(target_st.st_mode):
            return True
        # target is a directory — fall through to the iterdir() emptiness check
    elif not stat.S_ISDIR(st.st_mode):
        return True
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    except OSError:
        return True
    return True


def display_hermes_home() -> str:
    """Return a user-friendly display string for the current HERMES_HOME.

    Uses ``~/`` shorthand for readability::

        default:  ``~/.hermes``
        profile:  ``~/.hermes/profiles/coder``
        custom:   ``/opt/hermes-custom``

    Use this in **user-facing** print/log messages instead of hardcoding
    ``~/.hermes``.  For code that needs a real ``Path``, use
    :func:`get_hermes_home` instead.
    """
    home = get_hermes_home()
    try:
        return "~/" + str(home.relative_to(Path.home()))
    except ValueError:
        return str(home)


def secure_parent_dir(path: Path) -> None:
    """Chmod ``0o700`` on the parent directory of *path*, but only if safe.

    Refuses to chmod ``/`` or any top-level directory (resolved parent with
    fewer than 3 parts, i.e. ``/`` or any direct child like ``/usr``) to
    prevent catastrophic host bricking when ``HERMES_HOME`` or other path
    env vars resolve to an unexpected location.

    See https://github.com/NousResearch/hermes-agent/issues/25821.
    """
    parent = path.parent.resolve()
    # Refuse root and its direct children (/usr, /home, /var, /tmp, …).
    if parent == Path("/") or len(parent.parts) < 3:
        return
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass


def _norm_home_path(path: str | None) -> str:
    """Return a comparable absolute path string, or ``""`` for empty input."""
    raw = (path or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(raw)))
    except Exception:
        return os.path.normcase(raw)


def _profile_home_path(env: dict[str, str] | None = None) -> str | None:
    """Return ``{HERMES_HOME}/home`` when the profile-home directory exists.

    В режиме ``contained`` профильный дом определён РЕЖИМОМ, а не наличием
    каталога на диске: возвращаем путь всегда. Иначе до первого создания
    каталога ``get_real_home()`` считала бы ``<root>/home`` законным
    кандидатом в настоящий дом и «чинила» бы HOME сама в себя.
    В ``shared-host`` поведение прежнее — дом включается фактом наличия
    каталога, и это апстримный контракт.
    """
    hermes_home = get_hermes_home_override() or (env or {}).get("HERMES_HOME") or os.getenv("HERMES_HOME")
    if not hermes_home:
        return None
    profile_home = os.path.join(hermes_home, "home")
    if os.path.isdir(profile_home):
        return profile_home
    if is_contained(env):
        return profile_home
    return None


def _is_profile_home(candidate: str | None, profile_home: str | None) -> bool:
    return bool(candidate and profile_home and _norm_home_path(candidate) == _norm_home_path(profile_home))


def _iter_real_home_candidates(env: dict[str, str] | None = None) -> list[str]:
    """Return likely OS-user home candidates in trust order."""
    env = env or {}
    candidates: list[str] = []
    explicit = str(env.get("HERMES_REAL_HOME") or os.getenv("HERMES_REAL_HOME", "")).strip()
    if explicit:
        candidates.append(explicit)
    home = str(env.get("HOME") or os.getenv("HOME", "")).strip()
    if home:
        candidates.append(home)
    try:
        import pwd

        pw_home = pwd.getpwuid(os.getuid()).pw_dir.strip()  # windows-footgun: ok — POSIX-only module inside try/except
        if pw_home:
            candidates.append(pw_home)
    except Exception:
        pass
    userprofile = str(env.get("USERPROFILE") or os.getenv("USERPROFILE", "")).strip()
    if userprofile:
        candidates.append(userprofile)
    drive = str(env.get("HOMEDRIVE") or os.getenv("HOMEDRIVE", "")).strip()
    path = str(env.get("HOMEPATH") or os.getenv("HOMEPATH", "")).strip()
    if drive and path:
        candidates.append(f"{drive}{path}" if path.startswith(("\\", "/")) else os.path.join(drive, path))
    expanded = os.path.expanduser("~")
    if expanded and expanded != "~":
        candidates.append(expanded)
    return candidates


def get_real_home(env: dict[str, str] | None = None) -> str:
    """Return the OS user's real home directory, avoiding Hermes profile HOME.

    ``HERMES_HOME`` scopes Hermes state. ``HOME`` is reserved for the OS/user
    account and the many external CLIs that store credentials under ``~``.
    If a parent process is already running with ``HOME={HERMES_HOME}/home``,
    this helper repairs back to the account home when possible.
    """
    profile_home = _profile_home_path(env)
    seen: set[str] = set()
    for candidate in _iter_real_home_candidates(env):
        key = _norm_home_path(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        if not _is_profile_home(candidate, profile_home):
            return candidate
    return "/tmp"


def get_subprocess_home(env: dict[str, str] | None = None) -> str | None:
    """Return a subprocess ``HOME`` override, if one should be applied.

    Policy is controlled by ``terminal.home_mode`` (bridged to
    ``TERMINAL_HOME_MODE``):

    * ``auto`` (default): host installs keep the real user HOME; containers use
      ``{HERMES_HOME}/home`` for persistent state. If a host parent already has
      HOME pointed at the profile home, repair subprocesses back to real HOME.
    * ``real``: always prefer the real OS-user HOME.
    * ``profile``: use ``{HERMES_HOME}/home`` when it exists, preserving the
      older strict per-profile tool-config isolation.

    В режиме ``contained`` (см. :func:`get_isolation_mode`) ``auto`` и
    ``profile`` дают ``<root>/home``, каталог создаётся при необходимости, а
    апстримный «ремонт назад» на настоящий дом выключен: в contained HOME под
    корнем — это цель, а не авария. ``real`` остаётся явным переопределением
    оператора и однократно предупреждает (таблица §2.4 плана).
    """
    env = env or {}
    mode = str(env.get("TERMINAL_HOME_MODE") or os.getenv("TERMINAL_HOME_MODE", "auto")).strip().lower() or "auto"
    if mode in {"isolated", "profile_home", "profile-home"}:
        mode = "profile"
    if mode in {"host", "user", "real_home", "real-home"}:
        mode = "real"

    if is_contained(env):
        if mode != "real":
            # Каталог обязан существовать: `_profile_home_path` включает
            # профильный дом ФАКТОМ наличия каталога, и без создания
            # contained молча выродился бы в shared-host.
            return str(ensure_profile_home_dir(env))
        _warn_isolation_once(
            "home_mode:real",
            "terminal.home_mode=real при contained-изоляции: HOME подпроцессов "
            "выведен за корень профиля оператором. Учётные данные внешних CLI "
            "останутся в настоящем доме ОС и будут общими для всех профилей.",
        )

    profile_home = _profile_home_path(env)

    if mode == "profile":
        return profile_home

    real_home = get_real_home(env)
    current_home = str(env.get("HOME") or os.getenv("HOME", "")).strip()
    if mode == "real":
        return real_home if _norm_home_path(real_home) != _norm_home_path(current_home) else None

    if profile_home and is_container():
        return profile_home
    if _is_profile_home(current_home, profile_home):
        return real_home if _norm_home_path(real_home) != _norm_home_path(current_home) else None
    return None


def _iter_machine_browser_roots(env: dict[str, str] | None = None) -> list[str]:
    """Вернуть машинные каталоги сборок Chromium в порядке проверки.

    Один перечень на весь движок: ``tools/browser_tool.py`` импортирует его
    отсюда, чтобы источник был один и пин (:func:`apply_machine_browser_env`)
    не разошёлся с поиском браузера.

    Каталоги выводятся из НАСТОЯЩЕГО дома ОС, а не из дома профиля: сборка
    браузера — публичный машинный ассет (``SHARED_IMMUTABLE`` в терминах §6
    плана), она одна на машину и внутрь корня не переезжает.
    """
    roots: list[str] = []
    explicit = _env_value("PLAYWRIGHT_BROWSERS_PATH", env)
    if explicit and explicit != "0":
        roots.append(explicit)
    home = get_real_home(env)
    roots.append(os.path.join(home, ".cache", "ms-playwright"))
    # agent-browser 0.26 кладёт Chrome for Testing сюда, а не в кэш
    # Playwright — проверено исполнением на чистой клиентской машине
    # (см. `_chromium_search_roots` в tools/browser_tool.py).
    roots.append(os.path.join(home, ".agent-browser", "browsers"))
    if sys.platform == "darwin":
        roots.append(os.path.join(home, "Library", "Caches", "ms-playwright"))
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        roots.append(os.path.join(local, "ms-playwright"))
    return roots


def _find_agent_browser_executable(root: str) -> str | None:
    """Найти исполняемый ``chrome`` в каталоге вида ``chrome-<версия>/``."""
    try:
        entries = sorted(os.listdir(root), reverse=True)
    except OSError:
        return None
    for entry in entries:
        if not entry.startswith("chrome-"):
            continue
        for name in ("chrome", "chrome.exe", "headless_shell"):
            candidate = os.path.join(root, entry, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def apply_machine_browser_env(env: dict[str, str]) -> None:
    """Приколоть подпроцессу путь к машинной сборке браузера.

    Зачем это обязательно в ``contained``. Рецепт установки кладёт Chromium в
    домашний каталог ОС и экспортирует ``PLAYWRIGHT_BROWSERS_PATH`` только на
    время установки; юнит шлюза её не пишет. Сам движок браузер найдёт — его
    собственный ``HOME`` не меняется, — но **node-подпроцесс** браузера с
    ``HOME=<root>/home`` не найдёт ничего, и браузер перестанет работать у
    каждого клиента. Поэтому путь передаётся явно.

    Уже заданные оператором значения не перезаписываются.
    """
    want_playwright = not (env.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    want_agent = not (env.get("AGENT_BROWSER_EXECUTABLE_PATH") or "").strip()
    if not want_playwright and not want_agent:
        return
    for root in _iter_machine_browser_roots(env):
        if not root or not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        if want_playwright and any(
            e.startswith(("chromium-", "chromium_headless_shell-")) for e in entries
        ):
            env["PLAYWRIGHT_BROWSERS_PATH"] = root
            want_playwright = False
        if want_agent:
            executable = _find_agent_browser_executable(root)
            if executable:
                env["AGENT_BROWSER_EXECUTABLE_PATH"] = executable
                want_agent = False
        if not want_playwright and not want_agent:
            return


def apply_subprocess_containment_env(env: dict[str, str]) -> None:
    """Применить к *env* контракт изоляции профиля (на месте).

    Единственная точка, через которую движок собирает окружение дочерних
    процессов: её зовут семь конструкторов (`tools/environments/local.py` ×4,
    `tools/code_execution_tool.py`, `agent/copilot_acp_client.py` — через
    историческое имя `apply_subprocess_home_env`, и `tools/mcp_tool.py`),
    других нет. Поэтому правка здесь закрывает все спавны разом — внешние CLI,
    браузер, MCP, cron, шелл.

    Перечень выше — не гарантия сам по себе: stdio-спавн MCP собирал окружение
    мимо контракта (RAF-161), и сторож этого не видел, потому что снимал
    инвариант с функции, а не с мест вызова. Теперь спавны держат два сторожа:
    `tests/test_profile_root_containment_sentinel.py` — поведение (настоящий
    спавн в contained-арене), `tests/trix/test_profile_root_spawn_sites.py` —
    полноту (по исходникам: всякое собранное `env=` в зоне нагрузки профиля
    приходит из функции, которая зовёт контракт). Второй и отвечает за то,
    чтобы перечень выше не отстал: он падает на спавне, которого в нём нет.

    В ``shared-host`` поведение прежнее: добавляется только
    ``HERMES_REAL_HOME`` и, если так решил апстримный контракт, ``HOME``.

    В ``contained`` дополнительно (§2.5 плана):

    * ``XDG_CACHE_HOME`` / ``CONFIG`` / ``DATA`` / ``STATE`` — под дом профиля,
      иначе внешние CLI разложат состояние мимо переназначенного ``HOME``;
    * ``TMPDIR`` / ``TMP`` / ``TEMP`` — под корень;
    * ``HERMES_OSINT_CACHE`` — ручка уже была в скилле, её никто не выставлял;
    * пин машинного браузера (см. :func:`apply_machine_browser_env`).

    ``XDG_RUNTIME_DIR`` и ``DBUS_SESSION_BUS_ADDRESS`` НЕ трогаются: это
    интерфейс ОС к процессу (``OS_RUNTIME``), а не данные профиля.
    """
    real_home = get_real_home(env)
    if real_home:
        env["HERMES_REAL_HOME"] = real_home
    home = get_subprocess_home(env)
    if home:
        env["HOME"] = home

    if not is_contained(env):
        return

    root = get_profile_root(env)
    # XDG выставляем только когда HOME действительно переехал под корень.
    # Если оператор оставил настоящий дом (home_mode=real), XDG обязаны
    # следовать за ним — иначе состояние разъедется по двум домам сразу.
    profile_home = get_profile_home_dir(env)
    if home and _norm_home_path(home) == _norm_home_path(str(profile_home)):
        env["XDG_CACHE_HOME"] = str(profile_home / ".cache")
        env["XDG_CONFIG_HOME"] = str(profile_home / ".config")
        env["XDG_DATA_HOME"] = str(profile_home / ".local" / "share")
        env["XDG_STATE_HOME"] = str(profile_home / ".local" / "state")

    tmp_dir = str(ensure_profile_tmp_dir(env))
    for var in ("TMPDIR", "TMP", "TEMP"):
        env[var] = tmp_dir

    if not (env.get("HERMES_OSINT_CACHE") or "").strip():
        env["HERMES_OSINT_CACHE"] = str(root / "cache" / "osint")

    apply_machine_browser_env(env)


def apply_subprocess_home_env(env: dict[str, str]) -> None:
    """Apply Hermes' subprocess HOME contract to *env* in-place.

    Историческое имя. Оставлено потому, что его зовут шесть конструкторов
    окружения и существующие тесты; новый код зовёт
    :func:`apply_subprocess_containment_env`, у которого имя честно описывает
    объём работы.
    """
    apply_subprocess_containment_env(env)


VALID_REASONING_EFFORTS = (
    "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
)


def parse_reasoning_effort(effort) -> dict | None:
    """Parse a reasoning effort level into a config dict.

    Valid levels: "none", "minimal", "low", "medium", "high", "xhigh", "max",
    "ultra".
    Returns None when the input is empty or unrecognized (caller uses default).
    Returns {"enabled": False} for "none" (aliases: "false", "disabled", and
    YAML boolean False — users write ``reasoning_effort: false``/``off``/``no``
    in config.yaml and YAML hands us a bool, which must mean disabled, not
    "fall back to the default and keep thinking").
    Returns {"enabled": True, "effort": <level>} for valid effort levels.
    """
    if effort is False:
        return {"enabled": False}
    if effort is None or effort is True:
        return None
    effort = str(effort)
    if not effort.strip():
        return None
    effort = effort.strip().lower()
    if effort in {"none", "false", "disabled"}:
        return {"enabled": False}
    if effort in VALID_REASONING_EFFORTS:
        return {"enabled": True, "effort": effort}
    return None


def _canonical_model_variants(model: str) -> list[str]:
    """Generate bounded spelling variants for tolerant override matching.

    Model names mix two types of separators:
    - **Word separators**: dashes between words (``claude-opus``)
    - **Version separators**: dots or dashes between version digits (``4.5``, ``4-5``)

    The tricky case is that ``.`` appears in BOTH roles (word sep in some
    spellings, version sep in others), so a blanket ``.replace('.', '-')``
    is lossy — it collapses version dots into dashes and no later step
    recovers the canonical form (``claude-opus-4.5``).

    Strategy: generate a small set of base forms, then apply version-dot
    recovery to EACH of them. This ensures symmetry:
    ``claude-opus-4.5``, ``claude-opus-4-5``, and ``claude-opus.4.5`` all
    produce the same variant set.

    Steps:
    1. Exact input
    2. Dots/dashes cross-substitution on the entire string
    3. Version-dot recovery applied to ALL derivatives
    4. Strip provider/aggregator prefix → bare model variants
    5. Apply version-dot recovery to bare derivatives
    6. Prepend known provider/aggregator prefixes

    Duplicates removed in insertion order (exact always wins).
    """
    import re

    # Version-dot regexes — digit-separator-digit interconversion
    _dash_to_dot = lambda s: re.sub(r'(\d)-(\d)', r'\1.\2', s)
    _dot_to_dash = lambda s: re.sub(r'(\d)\.(\d)', r'\1-\2', s)

    seen = set()
    variants = []

    def _add(v):
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    def _add_with_derivatives(s):
        """Add s plus its dots↔dashes and version-dot derivatives."""
        _add(s)
        all_dashed = s.replace('.', '-')
        _add(all_dashed)
        all_dotted = s.replace('-', '.')
        _add(all_dotted)
        # Version-dot recovery on each base form
        _add(_dash_to_dot(s))
        _add(_dot_to_dash(s))
        _add(_dash_to_dot(all_dashed))
        _add(_dot_to_dash(all_dotted))

    # 1-3. Base variants for the full string
    _add_with_derivatives(model)

    # Split by / to handle provider prefix
    parts = model.split('/')

    # 4. Bare model variants (strip provider/aggregator prefix)
    if len(parts) >= 2:
        bare = parts[-1]
        _add_with_derivatives(bare)

    # Strip aggregator only (3+ parts)
    # e.g. "openrouter/anthropic/claude-opus-4.5" → "anthropic/claude-opus-4.5"
    if len(parts) >= 3:
        _add_with_derivatives('/'.join(parts[1:]))

    # 5. Prepend known provider prefixes to bare variants
    known_providers = (
        'anthropic', 'openai', 'google', 'openrouter', 'groq', 'mistral',
        'xai', 'cohere', 'perplexity', 'together', 'fireworks', 'deepseek',
    )
    bare_variants = [v for v in variants if '/' not in v]
    for v in bare_variants:
        for provider in known_providers:
            _add(f"{provider}/{v}")

    # Prepend aggregator to single-slash variants
    single_slash_variants = [v for v in variants if v.count('/') == 1]
    known_aggregators = ('openrouter', 'opencode', 'fireworks', 'groq', 'together')
    for v in single_slash_variants:
        for agg in known_aggregators:
            _add(f"{agg}/{v}")

    return variants


def resolve_per_model_reasoning_effort(model: str, overrides: dict | None) -> dict | None:
    """Lookup a per-model reasoning_effort override with spelling-tolerance.

    Args:
        model: The model string (any spelling — exact, normalized, bare,
               with provider prefix, etc.)
        overrides: The dict of per-model overrides from
                   agent.reasoning_overrides in config.yaml. Keys can be
                   any sensible spelling of the model name.

    Returns:
        The parsed reasoning_config dict if a match is found,
        None otherwise (caller should fall back to global reasoning_effort).

    Resolution order:
    1. Exact match
    2. Dots ↔ dashes variants
    3. Strip provider prefix (bare model name only)
    4. Strip aggregator prefix (middle segment only)
    5. Prepend known aggregator prefixes to bare/single-slash variants

    First non-None parse_reasoning_effort result wins.
    """
    if not overrides or not isinstance(overrides, dict) or not model:
        return None

    for variant in _canonical_model_variants(model):
        if variant in overrides:
            result = parse_reasoning_effort(overrides[variant])
            if result is not None:
                return result

    return None


def resolve_reasoning_config(cfg: dict | None, model: str = "") -> dict | None:
    """Resolve the effective reasoning config for *model* from a config dict.

    Single chokepoint for reasoning-effort resolution, shared by every
    surface (CLI startup, messaging gateway, Desktop/TUI, cron, ``/model``
    switch, fallback activation). Priority:

    1. Per-model override from ``agent.reasoning_overrides``
       (spelling-tolerant — see :func:`resolve_per_model_reasoning_effort`)
    2. Global ``agent.reasoning_effort`` — the raw value is passed through
       so a YAML boolean ``False`` (``reasoning_effort: false``/``off``/
       ``no``) means "thinking disabled", never silently re-enabled.

    Session-scoped overrides (gateway ``/reasoning --session``) are resolved
    by the caller BEFORE this function — they always win.

    Args:
        cfg: A loaded config dict (any of the three loaders' shapes — only
             the ``agent`` and ``model`` sections are read).
        model: The effective model for this surface/session. When empty,
               it is derived from the config's ``model`` section (string
               form, or a dict's ``default``/``model`` keys).

    Returns:
        The parsed reasoning config dict, or None when unset/unrecognized
        (caller uses the provider default).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    agent_cfg = cfg.get("agent")
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}

    if not model:
        model_cfg = cfg.get("model")
        if isinstance(model_cfg, str):
            model = model_cfg.strip()
        elif isinstance(model_cfg, dict):
            model = str(
                model_cfg.get("default") or model_cfg.get("model") or ""
            ).strip()
        else:
            model = ""

    overrides = agent_cfg.get("reasoning_overrides") or {}
    per_model = resolve_per_model_reasoning_effort(model, overrides)
    if per_model is not None:
        return per_model

    # Global fallback — keep the raw value; coercing with ``or ""`` turns a
    # YAML boolean False into "", silently re-enabling thinking for users
    # who explicitly disabled it.
    effort = agent_cfg.get("reasoning_effort", "")
    result = parse_reasoning_effort(effort)
    if effort and str(effort).strip() and result is None:
        import logging
        logging.getLogger(__name__).warning(
            "Unknown reasoning_effort '%s', using default (medium)", effort
        )
    return result


def is_termux() -> bool:
    """Return True when running inside a Termux (Android) environment.

    Checks ``TERMUX_VERSION`` (set by Termux) or the Termux-specific
    ``PREFIX`` path.  Import-safe — no heavy deps.
    """
    prefix = os.getenv("PREFIX", "")
    return bool(os.getenv("TERMUX_VERSION") or "com.termux/files/usr" in prefix)


_wsl_detected: bool | None = None


def is_wsl() -> bool:
    """Return True when running inside WSL (Windows Subsystem for Linux).

    Checks ``/proc/version`` for the ``microsoft`` marker that both WSL1
    and WSL2 inject.  Result is cached for the process lifetime.
    Import-safe — no heavy deps.
    """
    global _wsl_detected
    if _wsl_detected is not None:
        return _wsl_detected
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            _wsl_detected = "microsoft" in f.read().lower()
    except Exception:
        _wsl_detected = False
    return _wsl_detected


def windows_path_to_wsl(path: str) -> str | None:
    """Convert a Windows drive path (``C:\\...``) to its ``/mnt/<drive>/...`` form."""
    import re

    match = re.match(r"^([A-Za-z]):[\\/](.*)$", str(path or "").strip())
    if not match:
        return None
    drive = match.group(1).lower()
    tail = match.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{tail}"


def wsl_unc_path_to_posix(path: str) -> str | None:
    """Convert a Windows WSL UNC path (``\\\\wsl.localhost\\<distro>\\...`` or the
    legacy ``\\\\wsl$\\...``) to a POSIX path inside the distro."""
    import re

    normalized = str(path or "").strip().replace("/", "\\")
    match = re.match(r"^\\\\wsl(?:\.localhost|\$)\\[^\\]+\\(.*)$", normalized, re.IGNORECASE)
    if not match:
        return None
    tail = match.group(1).replace("\\", "/")
    return f"/{tail}" if tail else "/"


def translate_cwd_for_wsl_backend(cwd: str) -> str:
    """Normalize a cross-boundary cwd when Hermes itself runs inside WSL.

    A Windows-host UI (native picker / drive path / ``\\\\wsl.localhost\\`` UNC)
    can hand the WSL backend a path it can't ``chdir`` into. Map it to the POSIX
    equivalent so the picker, sidebar, and sessions all agree on the workspace.
    No-op off WSL and for paths that are already POSIX.
    """
    if not is_wsl():
        return cwd
    for translator in (wsl_unc_path_to_posix, windows_path_to_wsl):
        translated = translator(cwd)
        if translated is not None:
            return translated
    return cwd


_container_detected: bool | None = None


def is_container() -> bool:
    """Return True when running inside a container.

    Recognizes Docker (``/.dockerenv``), Podman (``/run/.containerenv``),
    and — via ``/proc/1/cgroup`` — the docker/podman/lxc cgroup-v1 markers.

    cgroup v2 collapses ``/proc/1/cgroup`` to a single ``0::/`` line with no
    runtime marker, so containerd/CRI-O runtimes (the common case on
    Kubernetes/k3s) were previously missed. To cover those, also check:
      * ``KUBERNETES_SERVICE_HOST`` env var — set in every Kubernetes pod.
      * ``kubepods`` / ``containerd`` / ``crio`` markers in ``/proc/1/cgroup``.
      * the same markers in ``/proc/self/mountinfo`` (cgroup-v2 fallback).

    Result is cached for the process lifetime.  Import-safe — no heavy deps.

    See: NousResearch/hermes-agent#47111
    """
    global _container_detected
    if _container_detected is not None:
        return _container_detected
    if os.path.exists("/.dockerenv"):
        _container_detected = True
        return True
    if os.path.exists("/run/.containerenv"):
        _container_detected = True
        return True
    # Kubernetes always injects this into pod containers; absent on hosts.
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        _container_detected = True
        return True
    _CGROUP_MARKERS = ("docker", "podman", "/lxc/", "kubepods", "containerd", "crio")
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as f:
            cgroup = f.read()
            if any(marker in cgroup for marker in _CGROUP_MARKERS):
                _container_detected = True
                return True
    except OSError:
        pass
    # cgroup v2: /proc/1/cgroup is just "0::/" with no marker. The container
    # runtime still shows up in the mount table, so scan mountinfo as a last
    # resort — но ТОЛЬКО строку корня («/»).
    #
    # Почему не по всему файлу, как было раньше: на ЛЮБОЙ машине, которая сама
    # запускает контейнеры, в mountinfo висят оверлеи её собственного демона
    # (`/var/lib/docker/rootfs/...`, `/var/lib/containerd/...`). Подстрока
    # `containerd` находилась там всегда, и хост объявлял себя контейнером. Для
    # `hermes doctor` это не косметика: он печатал «Running inside a container»
    # и ЦЕЛИКОМ пропускал диагностику терминального бэкенда (Vercel, Docker,
    # SSH, Daytona) — то есть молчал ровно там, где его спрашивали.
    #
    # Признак «мы ВНУТРИ» — не наличие рантайма на машине, а то, что наш
    # собственный корень смонтирован этим рантаймом. В containerd/CRI-O
    # контейнере строка `/` — overlay с путями снапшотов рантайма; на хосте это
    # обычная ФС на реальном устройстве.
    #
    # Проверяются ВСЕ строки с точкой монтирования `/`, а не первая: корень
    # может быть перемонтирован, и тогда действующая запись — не та, что
    # встретилась раньше. Ложных срабатываний это не добавляет — чужие оверлеи
    # демона монтируются не в `/`.
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as f:
            for line in f:
                fields = line.split()
                # mountinfo: id parent major:minor root MOUNTPOINT ...
                if len(fields) < 5 or fields[4] != "/":
                    continue
                if any(marker in line for marker in ("kubepods", "containerd", "crio")):
                    _container_detected = True
                    return True
    except OSError:
        pass
    _container_detected = False
    return False


# ─── Well-Known Paths ─────────────────────────────────────────────────────────


def get_config_path() -> Path:
    """Return the path to ``config.yaml`` under HERMES_HOME.

    Replaces the ``get_hermes_home() / "config.yaml"`` pattern repeated
    in 7+ files (skill_utils.py, hermes_logging.py, hermes_time.py, etc.).
    """
    return get_hermes_home() / "config.yaml"


def get_skills_dir() -> Path:
    """Return the path to the skills directory under HERMES_HOME."""
    return get_hermes_home() / "skills"



def get_env_path() -> Path:
    """Return the path to the ``.env`` file under HERMES_HOME."""
    return get_hermes_home() / ".env"


# ─── Network Preferences ─────────────────────────────────────────────────────


def apply_ipv4_preference(force: bool = False) -> None:
    """Monkey-patch ``socket.getaddrinfo`` to prefer IPv4 connections.

    On servers with broken or unreachable IPv6, Python tries AAAA records
    first and hangs for the full TCP timeout before falling back to IPv4.
    This affects httpx, requests, urllib, the OpenAI SDK — everything that
    uses ``socket.getaddrinfo``.

    When *force* is True, patches ``getaddrinfo`` so that calls with
    ``family=AF_UNSPEC`` (the default) resolve as ``AF_INET`` instead,
    skipping IPv6 entirely.  If no A record exists, falls back to the
    original unfiltered resolution so pure-IPv6 hosts still work.

    Safe to call multiple times — only patches once.
    Set ``network.force_ipv4: true`` in ``config.yaml`` to enable.
    """
    if not force:
        return

    import socket

    # Guard against double-patching
    if getattr(socket.getaddrinfo, "_hermes_ipv4_patched", False):
        return

    _original_getaddrinfo = socket.getaddrinfo

    def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family == 0:  # AF_UNSPEC — caller didn't request a specific family
            try:
                return _original_getaddrinfo(
                    host, port, socket.AF_INET, type, proto, flags
                )
            except socket.gaierror:
                # No A record — fall back to full resolution (pure-IPv6 hosts)
                return _original_getaddrinfo(host, port, family, type, proto, flags)
        return _original_getaddrinfo(host, port, family, type, proto, flags)

    _ipv4_getaddrinfo._hermes_ipv4_patched = True  # type: ignore[attr-defined]
    socket.getaddrinfo = _ipv4_getaddrinfo  # type: ignore[assignment]


# ─── Streaming Response Constants ────────────────────────────────────────────

# Response ID for partial stream stubs used during error recovery
PARTIAL_STREAM_STUB_ID = "partial-stream-stub"

FINISH_REASON_LENGTH = "length"


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE_URL}/models"

AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"


# ─── Venv layout ─────────────────────────────────────────────────────────────

def venv_bin_dir(venv_dir, *, windows: bool | None = None) -> Path:
    """Directory holding a venv's executables (``Scripts`` / ``bin``).

    Canonical helper for venv layout. This was open-coded in seven places
    across four ``hermes_cli`` modules using three different Windows
    predicates (``platform.system()``, ``is_windows()``, ``_is_windows()``);
    each new call site had to re-derive it, and #76091 shipped an eighth copy
    because the correct behaviour lived 2400 lines away in another function.
    A few sites outside ``hermes_cli`` (``tools/code_execution_tool.py``,
    ``agent/lsp/install.py``, ``agent/lsp/servers.py``) still hand-roll it —
    convert them as they are touched.

    *windows* lets a caller pass its own platform verdict. Several callers
    resolve this through predicates the test-suite patches to exercise
    Windows paths on Linux CI (``hermes_cli.main._is_windows`` and friends);
    reading ``sys.platform`` unconditionally here would silently drop those
    paths out of coverage. Defaults to the host platform.

    The path is returned unconditionally — callers legitimately differ on
    whether a missing venv is an error, so existence checking stays with them.
    """
    if windows is None:
        windows = sys.platform == "win32"
    return Path(venv_dir) / ("Scripts" if windows else "bin")


def venv_python_path(venv_dir, *, windows: bool | None = None) -> Path:
    """Path to the Python interpreter inside *venv_dir* (may not exist)."""
    if windows is None:
        windows = sys.platform == "win32"
    return venv_bin_dir(venv_dir, windows=windows) / (
        "python.exe" if windows else "python"
    )


# ─── Partial-update diagnostics ──────────────────────────────────────────────

# Top-level packages/modules that ship as part of Hermes itself. An ImportError
# naming one of these means our own tree is inconsistent; anything else is a
# third-party problem with different remediation. Single source of truth —
# `hermes_cli.update_cmd`'s post-update probe consumes this same set so the
# guard that BLOCKS and the hint that EXPLAINS can never disagree.
FIRST_PARTY_MODULE_ROOTS = frozenset(
    {
        "agent",
        "acp_adapter",
        "cli",
        "cron",
        "gateway",
        "model_tools",
        "plugins",
        "providers",
        "tools",
        "toolsets",
        "run_agent",
        "tui_gateway",
        "utils",
    }
)


def is_first_party_module(name: str | None) -> bool:
    """True when *name* is a module that ships with Hermes.

    Matches on the first dotted segment against an exact set — a substring or
    ``startswith`` test would also claim third-party ``agents``, ``agentops``,
    and ``toolsets_x``.
    """
    root = str(name).split(".")[0] if name else ""
    if not root:
        return False
    return root in FIRST_PARTY_MODULE_ROOTS or root.startswith("hermes_")


def partial_update_hint(exc: BaseException) -> list[str]:
    """Return recovery guidance lines when *exc* looks like a half-updated tree.

    An interrupted or partially-applied update can leave the checkout with new
    files in one package and stale files in another. Every file still parses,
    so nothing is corrupt in the usual sense — but a module that imports a name
    added in the same release from a sibling that wasn't refreshed dies with
    ``ImportError: cannot import name 'X' from 'y'`` on every startup.

    Users hit this as an opaque crash with no indication that the *install*,
    rather than their config, is the problem — and `hermes update` is exactly
    the command they need but are least likely to trust after a failed update.
    Return the guidance so callers can print it alongside the raw error.

    Returns an empty list for unrelated exceptions, so callers can splat it
    unconditionally.
    """
    if not isinstance(exc, ImportError):
        return []
    # A missing third-party dependency is a different problem (bad venv, missing
    # extra) with different remediation, so don't claim a partial update.
    if isinstance(exc, ModuleNotFoundError):
        return []
    name = getattr(exc, "name", None)
    if not is_first_party_module(name):
        return []
    return [
        "",
        "This looks like a partially-updated install: one module was refreshed "
        "and a related one was not.",
        "Re-run the update to bring the whole tree to the same version:",
        "    hermes update",
        "If that also fails, contact XDataPlus support.",
    ]
