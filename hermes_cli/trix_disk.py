"""Место на диске клиентской машины — замер, отчёт и план уборки.

Почему это модуль шлюза, а не инструмент агента: в песочницу смонтированы
только /workspace, /root и кэш на чтение (tools/environments/docker.py).
Сам HERMES_HOME внутрь не проброшен, сокета Docker там нет — агент эти
файлы не видит и удалить их не может. Считает и убирает процесс на хосте,
которому они принадлежат.

Правила, которым подчинён каждый кусок ниже:

1. **Защита работает в обе стороны и переживает симлинки.** Кандидат
   защищён, если лежит внутри защищённого пути, содержит его в себе,
   равен ему, равен дому, пуст или не разрешается в путь внутри дома.
   Сравниваются и «как написано», и «куда ведёт»: если
   ``cache/documents`` увели симлинком наружу, ``cache`` обязан остаться
   защищённым, иначе страховка выключается ровно на той конфигурации,
   где она нужнее всего.
2. **Раскладка каталогов берётся у остального Hermes** — через
   ``get_hermes_dir(new, old)`` (легаси-каталог, если он непустой) и
   через ``TERMINAL_SANDBOX_DIR`` для рабочей папки. Метка для клиента
   ищется по РАЗРЕШЁННОМУ пути, поэтому она не может разъехаться с тем,
   что реально посчитано, ни в новой раскладке, ни в легаси.
3. **Числа либо сходятся, либо об этом сказано словами.** Категории
   считаются обходом с исключениями, а не вычитанием; «остальное» —
   вычитанием из занятого, но только той части данных агента, которая
   лежит на том же томе. Данные на другом томе, нечитаемые каталоги и
   расхождение баланса не зажимаются молча, а попадают в отчёт
   отдельными словами.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from hermes_constants import get_hermes_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Раскладка каталогов
# ---------------------------------------------------------------------------

# Пары (новый путь, легаси-имя) — те же, что в tools/credential_files.py
# (_CACHE_DIRS). get_hermes_dir() отдаёт легаси-каталог, если он непустой.
_DOCUMENTS: tuple[str, str] = ("cache/documents", "document_cache")

# Что можно убирать. Метки в этой таблице НЕТ: она ищется в
# _REMOVABLE_LABELS по тому пути, который реально посчитан, — и для новой
# раскладки, и для легаси. Поэтому перестановка любых путей местами не
# может развести метку и каталог.
_REMOVABLE_DIRS: tuple[tuple[str, str], ...] = (
    ("backups", "backups"),
    ("state-snapshots", "state-snapshots"),
    ("checkpoints", "checkpoints"),
    ("logs", "logs"),
    ("debug-reports", "debug-reports"),
    ("cache/images", "image_cache"),
    ("cache/audio", "audio_cache"),
    ("cache/videos", "video_cache"),
    ("cache/screenshots", "browser_screenshots"),
    ("cache/web", "web_cache"),
    ("cache/delegation", "delegation_cache"),
)

# Метка для клиента по пути относительно дома — ключами идут ОБА имени,
# новое и легаси. Одна метка может покрывать несколько каталогов
# (медиакэш) — в отчёте они складываются.
_REMOVABLE_LABELS: dict[str, str] = {
    "backups": "бэкапы",
    "state-snapshots": "снимки состояния",
    "checkpoints": "контрольные точки",
    "logs": "журналы",
    "debug-reports": "отчёты об ошибках",
    "cache/images": "медиакэш",
    "image_cache": "медиакэш",
    "cache/audio": "медиакэш",
    "audio_cache": "медиакэш",
    "cache/videos": "медиакэш",
    "video_cache": "медиакэш",
    "cache/screenshots": "медиакэш",
    "browser_screenshots": "медиакэш",
    "cache/web": "кэш веб-страниц",
    "web_cache": "кэш веб-страниц",
    "cache/delegation": "кэш подзадач",
    "delegation_cache": "кэш подзадач",
}

# Пути внутри HERMES_HOME, которые не предлагаются к удалению НИКОГДА.
# cache/documents (легаси: document_cache) — присланные клиентом файлы,
# они хранятся вечно по решению спеки 9. sandboxes — рабочая папка
# агента: она его, а не наша (и вдобавок принадлежит uid 0 на хосте,
# потому что контейнер работает от root внутри). images/attachments — то,
# что клиент прислал через десктоп и TUI. Реальные каталоги документов и
# песочницы дополнительно резолвятся в protected_paths(): на легаси-
# инсталле и при TERMINAL_SANDBOX_DIR они лежат не там, где написано.
PROTECTED_SUBPATHS: frozenset[str] = frozenset({
    "cache/documents",
    "document_cache",
    "sandboxes",
    "images",
    "attachments",
})

# Ниже этого порога уборку не предлагаем: обещание «освободить 0 МБ»
# клиент проверит один раз, ничего не увидит и перестанет верить отчёту.
_MIN_CLEANUP_BYTES: int = 100 * 1024 ** 2


@dataclass(frozen=True)
class RemovableItem:
    label: str
    path: Path
    bytes: int


@dataclass(frozen=True)
class DiskReport:
    total: int
    used: int
    free: int
    used_percent: float
    documents_bytes: int
    workspace_bytes: int
    sessions_bytes: int
    service_bytes: int
    other_bytes: int
    # Данные агента, лежащие НЕ на том томе, для которого посчитано
    # «занято»: перенесённые песочницы, документы на другом диске.
    elsewhere_bytes: int = 0
    # Сколько каталогов не удалось прочитать: числа занижены ровно на их
    # содержимое, и молчать об этом нельзя.
    unreadable: int = 0
    # Оговорки, которые клиент должен увидеть словами.
    notes: tuple[str, ...] = ()
    removable: list[RemovableItem] = field(default_factory=list)

    @property
    def client_bytes(self) -> int:
        """Файлы клиента: присланные документы плюс рабочая папка.

        Домашний каталог root внутри контейнера (``<песочница>/docker/
        <id>/home``) сюда НЕ входит — это кэши pip/npm агента, они идут в
        «служебное».
        """
        return self.documents_bytes + self.workspace_bytes

    @property
    def agent_bytes(self) -> int:
        """Всё, чем занят HERMES_HOME (и папка песочниц, где бы та ни лежала)."""
        return (
            self.documents_bytes
            + self.workspace_bytes
            + self.sessions_bytes
            + self.service_bytes
        )

    @property
    def local_agent_bytes(self) -> int:
        """Данные агента на том же томе, для которого посчитано «занято»."""
        return self.agent_bytes - self.elsewhere_bytes

    @property
    def removable_bytes(self) -> int:
        return sum(i.bytes for i in self.removable)


# ---------------------------------------------------------------------------
# Замер
# ---------------------------------------------------------------------------


class _Tally:
    """Байты по устройствам плюс счётчик нечитаемых мест."""

    __slots__ = ("by_device", "unreadable")

    def __init__(self) -> None:
        self.by_device: dict[int, int] = {}
        self.unreadable = 0

    @property
    def total(self) -> int:
        return sum(self.by_device.values())

    def on(self, device: int) -> int:
        return self.by_device.get(device, 0)

    def add(self, device: int, size: int) -> None:
        self.by_device[device] = self.by_device.get(device, 0) + size

    def merge(self, other: "_Tally") -> "_Tally":
        for device, size in other.by_device.items():
            self.add(device, size)
        self.unreadable += other.unreadable
        return self


def _device_of(path: str, st: os.stat_result) -> int:
    """Устройство, на котором лежит файл.

    Вынесено функцией, чтобы тест мог смоделировать второй том без
    монтирования: собрать настоящую вторую файловую систему в pytest
    нельзя, а поведение отчёта на двух томах проверить надо.
    """
    return st.st_dev


def _measure(
    root: Path,
    *,
    exclude: frozenset[str] = frozenset(),
    follow_depth: int = 0,
) -> _Tally:
    """Размер дерева по устройствам.

    Симлинк на месте самого ``root`` разворачивается: каталог, названный
    по имени, считается там, куда его увели, иначе клиент читает «0 МБ»
    про свои же файлы. Симлинки ВНУТРИ дерева не разворачиваются — иначе
    одни и те же байты приезжают в отчёт дважды; исключение —
    ``follow_depth`` верхних уровней (устройство песочниц принято
    выносить на отдельный диск именно симлинком).

    Пути из ``exclude`` (строки, как они встретятся при обходе)
    пропускаются: категории считаются обходом с исключениями, а не
    вычитанием одной из другой.
    """
    tally = _Tally()
    try:
        root_stat = os.stat(root)
    except FileNotFoundError:
        return tally
    except (OSError, RuntimeError):
        tally.unreadable += 1
        return tally
    if not os.path.isdir(root):
        tally.add(_device_of(str(root), root_stat), root_stat.st_size)
        return tally

    seen: set[str] = set()
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (OSError, RuntimeError):
            tally.unreadable += 1
            continue
        for entry in entries:
            if entry.path in exclude:
                continue
            try:
                follow = depth < follow_depth
                if entry.is_dir(follow_symlinks=follow):
                    if follow and entry.is_symlink():
                        real = _resolve(Path(entry.path))
                        if real is None or str(real) in seen:
                            continue
                        seen.add(str(real))
                    stack.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    tally.add(_device_of(entry.path, st), st.st_size)
            except (OSError, RuntimeError):
                tally.unreadable += 1
    return tally


def _measure_file(path: Path) -> _Tally:
    """Размер файла, названного по имени (симлинк разворачивается)."""
    tally = _Tally()
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return tally
    except (OSError, RuntimeError):
        tally.unreadable += 1
        return tally
    if os.path.isdir(path):
        return tally
    tally.add(_device_of(str(path), st), st.st_size)
    return tally


def _disk_usage(home: Path):
    """Занятость раздела, на котором лежит (или будет лежать) ``home``.

    У свежего профиля может не быть не только самого каталога, но и его
    родителя — поднимаемся до первого существующего предка.
    """
    probe = home
    for candidate in (home, *home.parents):
        if candidate.exists():
            probe = candidate
            break
    else:  # pragma: no cover — у пути всегда есть существующий предок
        probe = Path(home.anchor or ".")
    return probe, shutil.disk_usage(probe)


def _used_percent(usage) -> float:
    """Занятость раздела в процентах. Одна формула на весь модуль."""
    return round(usage.used / usage.total * 100, 1) if usage.total else 0.0


def partition_free_bytes(home: Path) -> Optional[int]:
    """Свободные байты раздела тем же одним системным вызовом.

    Нужны затем, что процент сам по себе о запасе не говорит: 80 % на
    диске в сто гигабайт — это двадцать свободных гигабайт, и советовать
    там «попросите диск побольше» бессмысленно. См. ``is_alarming``.
    """
    try:
        import shutil as _shutil

        return int(_shutil.disk_usage(str(home)).free)
    except Exception:
        return None


def partition_used_percent(home: Path) -> Optional[float]:
    """Занятость раздела ОДНИМ системным вызовом — без обхода дерева.

    ``shutil.disk_usage`` стоит микросекунды; обход всех данных агента —
    секунды, а на клиентской машине с раздутыми зависимостями десятки
    секунд. Порог предупреждения считается ровно по этому проценту,
    поэтому дешёвого замера достаточно, чтобы поймать пересечение: обход
    нужен, только чтобы ПОКАЗАТЬ, чем занято место, а не чтобы понять,
    что его мало.

    ``None`` — замер не удался; звать из-за этого дорогой обход не за чем.
    """
    try:
        _probe, usage = _disk_usage(home)
    except Exception:
        return None
    return _used_percent(usage)


def _documents_dir(home: Path) -> Path:
    new, old = _DOCUMENTS
    return get_hermes_dir(new, old, home=home)


def _sandbox_root(home: Path) -> Path:
    """Корень песочниц для данного дома.

    Повторяет разрешение ``tools.environments.base.get_sandbox_dir()``
    (TERMINAL_SANDBOX_DIR, иначе ``<home>/sandboxes``), но без двух его
    свойств, несовместимых с замером: тот хелпер берёт дом сам (мы
    считаем произвольный переданный дом) и создаёт каталог на диске
    (отчёт не должен ничего создавать).
    """
    custom = os.getenv("TERMINAL_SANDBOX_DIR", "").strip()
    if custom:
        return Path(custom).expanduser()
    return home / "sandboxes"


def _iter_client_workspaces(sandbox_root: Path) -> Iterator[Path]:
    """Рабочие папки клиента: ``<песочница>/<бэкенд>/<задача>/workspace``.

    Симлинки на уровнях бэкенда и задачи разворачиваются: перенос
    ``sandboxes/docker`` на отдельный диск — первое, что делают на
    забитом VPS, и «рабочая папка 0 МБ» после такого переноса — ложь.
    Соседний ``home`` — это ``/root`` контейнера (кэши pip/npm), он
    служебный и в «ваши файлы» не идёт (tools/environments/docker.py).
    """
    seen: set[str] = set()
    for backend in _iter_dirs(sandbox_root):
        for task in _iter_dirs(backend):
            workspace = task / "workspace"
            try:
                if not workspace.is_dir():
                    continue
            except (OSError, RuntimeError):
                continue
            real = _resolve(workspace)
            key = str(real or workspace)
            if key in seen:
                continue
            seen.add(key)
            yield workspace


def _iter_dirs(root: Path) -> Iterator[Path]:
    try:
        entries = sorted(root.iterdir())
    except (OSError, RuntimeError):
        return
    for entry in entries:
        try:
            if entry.is_dir():  # симлинк на этом уровне разворачивается
                yield entry
        except (OSError, RuntimeError):
            continue


def _sessions_db_name() -> str:
    """Имя файла БД разговоров — берём у hermes_state, а не набираем руками."""
    try:
        from hermes_state import DEFAULT_DB_PATH

        return DEFAULT_DB_PATH.name
    except Exception:  # pragma: no cover — модуль всегда импортируется
        return "state.db"


def _sessions_paths(home: Path) -> list[Path]:
    """БД разговоров плюс её WAL/SHM-спутники — они бывают крупнее самой БД."""
    name = _sessions_db_name()
    return [home / name, home / f"{name}-wal", home / f"{name}-shm"]


# ---------------------------------------------------------------------------
# Защита
# ---------------------------------------------------------------------------


def _resolve(path: Path) -> Optional[Path]:
    """resolve() без исключений. None = разрешить путь не удалось.

    RuntimeError ловится наравне с OSError: на цикле симлинков
    ``Path.resolve()`` бросает именно его, и без этого вместо отчёта
    клиент получает упавшую команду.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _shares_lineage(a: Path, b: Path) -> bool:
    """True, если один путь лежит внутри другого или они совпадают.

    Сравнение покомпонентное, поэтому ``cache/documents-old`` не считается
    роднёй ``cache/documents``.
    """
    ap, bp = a.parts, b.parts
    shared = min(len(ap), len(bp))
    return ap[:shared] == bp[:shared]


def protected_paths(home: Path) -> tuple[Path, ...]:
    """Абсолютные пути, которые нельзя предлагать к удалению.

    Кроме статического списка имён — реально разрешённые каталоги
    документов и песочниц: на легаси-инсталле и при TERMINAL_SANDBOX_DIR
    они лежат по другим путям, и статический список сторожил бы пустоту.
    """
    paths = [home / name for name in PROTECTED_SUBPATHS]
    paths.append(_documents_dir(home))
    paths.append(_sandbox_root(home))
    return tuple(paths)


def is_protected(home: Path, candidate: Path) -> bool:
    """True, если путь нельзя предлагать к удалению.

    Проверяются обе формы каждого пути — «как написано» и «куда ведёт».
    Только разрешённой формы мало: если ``cache/documents`` уведён
    симлинком на другой том, то разрешённый защищённый путь лежит вне
    дома, родство с ``cache`` теряется, и страховка выключается на той
    самой конфигурации, ради которой её писали.

    Наружу достаточно вести ОДНОЙ форме. Каталог, уведённый симлинком
    (``logs -> /mnt/big/logs``), написан внутри дома, а ведёт наружу —
    и требование «обе формы наружу» выключало бы защиту ровно на нём.
    """
    resolved_home = _resolve(home)
    resolved = _resolve(candidate)
    if resolved_home is None or resolved is None:
        return True
    if resolved == resolved_home or candidate == home:
        return True
    if not _shares_lineage(resolved_home, resolved) or not _shares_lineage(home, candidate):
        return True  # вне дома — не наше, не трогаем
    for protected in protected_paths(home):
        real = _resolve(protected)
        if real is None:
            return True
        if _shares_lineage(protected, candidate) or _shares_lineage(real, resolved):
            return True
    return False


def removable_candidates(home: Path) -> list[tuple[str, Path]]:
    """Пары (метка, каталог), которые можно предлагать к уборке.

    Защищённые пути отсеиваются здесь же: список удаляемого — не
    единственный и не самый надёжный источник правды о том, что можно
    трогать.
    """
    seen: list[Path] = []
    result: list[tuple[str, Path]] = []
    for new_subpath, legacy in _REMOVABLE_DIRS:
        target = get_hermes_dir(new_subpath, legacy, home=home)
        if is_protected(home, target):
            continue
        # Сам кандидат — ссылка. Внутрь дома она или наружу, удалять по
        # ней нельзя: уборка снесла бы чужое дерево (``logs ->
        # hermes-agent``), а замер уже посчитал бы его как «журналы».
        # Пропуск здесь держит обещание и факт на одном списке.
        try:
            if target.is_symlink():
                continue
        except OSError:  # pragma: no cover — недоступный путь
            continue
        # Родство, а не равенство: симлинк checkpoints -> backups/sub
        # иначе попал бы в план вторым пунктом, и обещание «освободить N»
        # вышло бы вдвое больше, чем есть на диске. Ошибаться безопасно
        # только в сторону «пообещать меньше».
        resolved = _resolve(target) or target
        if any(_shares_lineage(prev, resolved) for prev in seen):
            continue
        seen.append(resolved)
        result.append((_label_for(home, target), target))
    return result


def _label_for(home: Path, target: Path) -> str:
    """Метка ищется по тому пути, который реально посчитан.

    Ключами в _REMOVABLE_LABELS лежат и новые, и легаси-имена, поэтому на
    легаси-инсталле метка «бэкапы» не может оказаться над снимками
    состояния — она берётся из имени самого каталога, а не из позиции в
    таблице.
    """
    try:
        key = target.relative_to(home).as_posix()
    except ValueError:
        key = target.name
    return _REMOVABLE_LABELS.get(key, target.name)


# ---------------------------------------------------------------------------
# Сборка отчёта
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TreeScan:
    """Дорогая половина замера: обход дерева. Только её и кэшируем.

    Занятость раздела сюда НЕ входит намеренно. ``shutil.disk_usage`` —
    один системный вызов за микросекунды, а обход всех данных агента —
    секунды и десятки секунд. Заморозив их вместе, мы получали
    неподвижный процент на заполняющемся диске, непроизнесённый переход
    через порог предупреждения и «старые» числа после того, как место
    освободил сам агент по просьбе клиента, — то есть клиент читал бы,
    что агент соврал. Дешёвое считается на каждый вызов.

    ``frozen=True`` держит только сами поля. Содержимое ``_Tally`` и
    список ``removable`` изменяемы, а объект после кэширования общий для
    всех потоков: ``merge()`` или ``append()`` по нему испортит кэш всем
    и молча. Сегодня все слияния происходят внутри ``_scan_tree`` до
    возврата — читайте готовый ``_TreeScan`` только на чтение.
    """

    documents: "_Tally"
    workspace: "_Tally"
    sessions: "_Tally"
    service: "_Tally"
    removable: list[RemovableItem]
    notes: tuple[str, ...]


def _scan_tree(home: Path) -> _TreeScan:
    """Обойти данные агента: размеры по категориям и план уборки."""
    notes: list[str] = []

    documents_dir = _documents_dir(home)
    sandbox_root = _sandbox_root(home)
    workspaces = list(_iter_client_workspaces(sandbox_root))
    session_files = _sessions_paths(home)

    documents = _measure(documents_dir)
    workspace = _Tally()
    for path in workspaces:
        workspace.merge(_measure(path))
    sessions = _Tally()
    for path in session_files:
        sessions.merge(_measure_file(path))

    # «Служебное» — обходом с исключениями, а не вычитанием: hermes-agent/,
    # node/, bin/ на реальной машине крупнее всех кэшей вместе взятых, и
    # без них разбивка не сходится с «занято».
    excluded = frozenset(
        str(p) for p in [documents_dir, sandbox_root, *workspaces, *session_files]
    )
    service = _measure(home, exclude=excluded)
    # Песочницы считаются отдельным корнем: их принято выносить симлинком
    # на другой том, и обычный обход дома их бы не увидел.
    service.merge(_measure(sandbox_root, exclude=excluded, follow_depth=2))

    removable: list[RemovableItem] = []
    for label, target in removable_candidates(home):
        size = _measure(target).total
        if size > 0:
            removable.append(RemovableItem(label=label, path=target, bytes=size))

    if not removable and _covers_everything(home, sandbox_root):
        notes.append(
            "Каталог песочниц (TERMINAL_SANDBOX_DIR) накрывает весь рабочий "
            "каталог агента, поэтому убирать нечего: проверьте настройку."
        )

    unreadable = (
        documents.unreadable + workspace.unreadable
        + sessions.unreadable + service.unreadable
    )
    if unreadable:
        notes.append(
            f"Часть каталогов прочитать не удалось ({unreadable}) — настоящие "
            "размеры больше показанных."
        )

    return _TreeScan(
        documents=documents,
        workspace=workspace,
        sessions=sessions,
        service=service,
        removable=removable,
        notes=tuple(notes),
    )


def build_report(
    home: Path,
    *,
    scan: Optional[_TreeScan] = None,
) -> DiskReport:
    """Отчёт целиком. ``scan`` — уже сделанный обход (см. ``cached_report``).

    Всё, что зависит от занятости раздела — процент, свободно, «остальное»,
    порог предупреждения, — считается здесь и сейчас, даже когда обход
    пришёл из кэша.
    """
    scan = _scan_tree(home) if scan is None else scan
    documents, workspace = scan.documents, scan.workspace
    sessions, service = scan.sessions, scan.service

    probe, usage = _disk_usage(home)
    try:
        root_device = _device_of(str(probe), os.stat(probe))
    except (OSError, RuntimeError):
        root_device = -1

    notes: list[str] = list(scan.notes)
    unreadable = (
        documents.unreadable + workspace.unreadable
        + sessions.unreadable + service.unreadable
    )

    agent_total = documents.total + workspace.total + sessions.total + service.total
    local = (
        documents.on(root_device) + workspace.on(root_device)
        + sessions.on(root_device) + service.on(root_device)
    )
    elsewhere = agent_total - local
    if elsewhere:
        notes.append(
            f"Из данных агента {_size(elsewhere)} лежит на другом диске — "
            "в «занято» этого раздела они не входят."
        )

    other = usage.used - local
    if other < 0:
        # Баланс не сошёлся: файлы менялись во время подсчёта, жёсткие
        # ссылки, разрежённые файлы. Молча зажать в ноль нельзя — клиент
        # увидит «данные агента 40 КБ» при «занято 20 КБ» и не поймёт.
        other = 0
        notes.append(
            "Данные агента не помещаются в «занято» этого раздела: цифры "
            "ниже могли разойтись, пока шёл подсчёт."
        )

    used_percent = _used_percent(usage)

    return DiskReport(
        total=usage.total,
        used=usage.used,
        free=usage.free,
        used_percent=used_percent,
        documents_bytes=documents.total,
        workspace_bytes=workspace.total,
        sessions_bytes=sessions.total,
        service_bytes=service.total,
        other_bytes=other,
        elsewhere_bytes=elsewhere,
        unreadable=unreadable,
        notes=tuple(notes),
        removable=scan.removable,
    )


def _covers_everything(home: Path, sandbox_root: Path) -> bool:
    """True, если каталог песочниц накрывает сам дом (или совпадает с ним)."""
    real_home = _resolve(home) or home
    real_sandbox = _resolve(sandbox_root) or sandbox_root
    return _shares_lineage(real_sandbox, real_home) and len(real_sandbox.parts) <= len(
        real_home.parts
    )


# ---------------------------------------------------------------------------
# Кэш замера
# ---------------------------------------------------------------------------

# Обход всех данных агента стоит около полутора секунд на 2.6 ГБ, а на
# клиентской машине с раздутыми зависимостями — десятки секунд. Команду
# дёргают из Telegram, и всё это время человек смотрит в пустой чат. Тот
# же приём и по той же причине уже стоит в
# ``tools/terminal_tool.py::_check_disk_usage_warning``.
#
# Кэшируется ТОЛЬКО обход (``_TreeScan``). Занятость раздела — один
# системный вызов, её замораживать не за что и вредно: см. докстроку
# ``_TreeScan``.
#
# Две минуты — длина одного обмена репликами: повторный вопрос в том же
# разговоре отвечается мгновенно, а вопрос спустя несколько минут уже
# меряется заново. Уборка сбрасывает кэш явно (см.
# ``invalidate_report_cache`` и её вызов в ``clean``): иначе клиент сразу
# после уборки прочитал бы доуборочные числа и решил бы, что не
# сработало.
_REPORT_CACHE_TTL: float = 120.0

# Ключ — дом, а не процесс: один шлюз обслуживает несколько профилей, и
# кэш одного не должен отвечать за другой.
_report_cache: dict[str, tuple[float, "_TreeScan"]] = {}

# Поколение кэша. Сброс его увеличивает; обход, начатый до увеличения,
# своё в кэш уже не кладёт.
#
# Без этого счётчика обход и уборка гоняются, и выигрывает уборка —
# точнее, проигрывает клиент. Обход начался, идёт свои десятки секунд;
# посреди него приходит ``/disk clean`` (``busy_policy="dispatch"``
# специально это разрешает), сносит файлы и чистит кэш; обход
# заканчивается и кладёт в опустевший кэш ДОуборочный снимок. Дальше на
# две минуты клиент читает три противоречащих ответа подряд: уборка
# сказала «освобождено 30 МБ», следующий ``/disk`` показывает те же
# доуборочные числа и зовёт «сократить на 30 МБ — команда /disk clean»,
# а она отвечает «служебных файлов не нашлось».
#
# Счётчик один на все дома, а не по одному на каждый: уборка в одном
# профиле обесценит незавершённый обход в другом, но цена ошибки —
# лишний обход, а не ложные числа, и рассуждать о едином счётчике проще.
_cache_generation: int = 0

# Мутации кэша под замком, сам обход — вне его. Обход под замком запер бы
# ``/disk clean`` ровно на те десятки секунд, ради которых всё это и
# делается (и тест на гонку встал бы намертво).
_cache_lock = threading.Lock()


def _now() -> float:
    """Монотонные часы одной точкой — их подменяют тесты срока годности."""
    return time.monotonic()


def _cache_key(home: Path) -> str:
    return str(_resolve(home) or home)


def invalidate_report_cache(home: Optional[Path] = None) -> None:
    """Забыть обход: для одного дома или целиком (``home=None``).

    Поколение увеличивается всегда — в том числе для дома, которого в
    кэше ещё нет: обход по нему может идти прямо сейчас.
    """
    global _cache_generation
    key = None if home is None else _cache_key(home)
    with _cache_lock:
        _cache_generation += 1
        if key is None:
            _report_cache.clear()
        else:
            _report_cache.pop(key, None)


# Сколько раз перемерять, если за время обхода прошла уборка. Два — это
# «одна попытка плюс один повтор», не цикл: при частых уборках цикл
# закружился бы, а клиент так и не получил бы ответа.
_SCAN_ATTEMPTS: int = 2


def _cached_scan(home: Path, *, ttl: float) -> "_TreeScan":
    """Обход с коротким кэшем — для того, кто ждёт ответа в чате.

    Если за время обхода прошла уборка, результат обесценен: он описывает
    файлы, которых на диске уже нет. Отдать его нельзя — клиент прочтёт
    подряд «освобождено 150 МБ», отчёт с доуборочными числами и
    приглашением «сократить на 150 МБ — команда /disk clean», а потом
    «служебных файлов не нашлось». Три несогласующихся сообщения, после
    которых числам не верят вообще. Поэтому такой обход не кладётся в
    кэш И не отдаётся: меряем заново.

    Повтор один. Если уборка успела пройти и во время повтора, отдаём
    что померили: ещё один круг ничего не гарантирует, а человек всё это
    время смотрит в пустой чат.
    """
    key = _cache_key(home)
    with _cache_lock:
        cached = _report_cache.get(key)
        generation = _cache_generation
    if cached is not None and _now() - cached[0] < ttl:
        return cached[1]

    for _ in range(_SCAN_ATTEMPTS):
        scan = _scan_tree(home)
        with _cache_lock:
            if _cache_generation == generation:
                _report_cache[key] = (_now(), scan)
                return scan
            # Уборка прошла между снятием поколения и этой строкой.
            # Берём новое поколение и меряем ещё раз — если попытки есть.
            generation = _cache_generation
    return scan


def cached_report(home: Path, *, ttl: float = _REPORT_CACHE_TTL) -> DiskReport:
    """Отчёт: обход из кэша, занятость раздела — свежая на каждый вызов."""
    return build_report(home, scan=_cached_scan(home, ttl=ttl))


# ---------------------------------------------------------------------------
# Текст для клиента
# ---------------------------------------------------------------------------

# Куда клиенту идти, когда своими силами не решается. Одна константа на
# два текста — отчёт о забитом диске и провалившуюся уборку: клиент
# читает их подряд, и адрес в них обязан быть один.
_ASK_THE_OWNER = "Напишите тем, кто выдал вам эту машину"


def _size(value: int) -> str:
    """Размер по-человечески, одной точностью на весь отчёт.

    До десяти единиц — один знак после запятой, дальше целое; хвостовой
    «.0» не печатается, чтобы «5 ГБ» и «12 ГБ» стояли рядом одинаково.
    Ноль — это «0 КБ», а не «0 МБ»: клиент не должен читать «сократить на
    0 МБ» там, где речь о килобайтах.
    """
    for unit, scale in (("ГБ", 1024 ** 3), ("МБ", 1024 ** 2), ("КБ", 1024)):
        if value >= scale:
            amount = value / scale
            text = f"{amount:.1f}" if amount < 10 else f"{amount:.0f}"
            return f"{text.removesuffix('.0')} {unit}"
    return "0 КБ"


def group_removable(items: list[RemovableItem]) -> list[tuple[str, int]]:
    """Пункты плана уборки, сложенные по метке, от крупных к мелким.

    Клиент видит, что именно предлагается убрать: обещание «сократить на
    N» без расшифровки — то же самое «занято 40 ГБ» в миниатюре.
    """
    totals: dict[str, int] = {}
    for item in items:
        totals[item.label] = totals.get(item.label, 0) + item.bytes
    return sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))


# Из чего складывается «служебное». Вторая по величине строка отчёта не
# может стоять без единого слова расшифровки, а перечислять её содержимое
# по факту нельзя: там же лежат node/, bin/ и сам код агента.
_SERVICE_HINT = "журналы, кэши, бэкапы, файлы контейнеров, сам агент"

# Аргумент, которого у команды нет. Отчёт всё равно показывается — он
# полезен сам по себе, а отказ вместо ответа клиенту помочь не может, —
# но не молча: набравший `/disk cleen` прочтёт отчёт как «уборка прошла,
# убирать было нечего», перестанет ждать, и следующим шагом будет
# молчащий бот на кончившемся диске. Это ровно тот сценарий, ради
# которого команда и написана, — промолчать здесь дороже всего.
UNRECOGNIZED_ARGUMENT = (
    "Такого аргумента у /disk нет — показываю, чем занято место. "
    "Убрать служебное — команда /disk clean."
)


# Запас, при котором говорить «мало места» не о чем, какой бы процент ни
# показывал раздел. Продукт вместе с образом песочницы занимает около
# полутора гигабайт; десять — это заведомо спокойно.
#
# Порог в одних процентах врёт на обоих концах. На диске в сто гигабайт
# 80 % — двадцать свободных гигабайт, и клиент читал там «места остаётся
# немного» и совет просить диск побольше. На диске в двадцать гигабайт те
# же 80 % — четыре свободных, и это уже тесно. Процент отвечает на вопрос
# «насколько заполнено», а клиенту важно «сколько ещё влезет».
_DEFAULT_MIN_FREE_BYTES: int = 10 * 1024 ** 3

def is_alarming(
    used_percent: float,
    warn_percent: float,
    free_bytes: Optional[int] = None,
    min_free_bytes: int = _DEFAULT_MIN_FREE_BYTES,
) -> bool:
    """Занято столько, что об этом надо сказать словами.

    ОДНО сравнение на две стороны: по нему решает почасовая проверка
    (``warn_level``) и по нему же печатается тревога в отчёте
    (``format_report``). Два сравнения разъехались бы на границе, и клиент
    прочёл бы в ``/disk`` «места почти нет» в тот самый час, когда
    почасовая проверка промолчала.

    Граница считается тревожной: «занято ровно 80 %» при пороге 80 — это
    уже повод сказать, а не последний спокойный час.

    ``free_bytes`` — запас в байтах. Пока его больше ``min_free_bytes``,
    тревоги нет, каким бы ни был процент: иначе на большом диске клиент
    читает «места остаётся немного» и совет просить диск побольше, имея
    десятки свободных гигабайт. Найдено клиентом на живой машине
    2026-09-04. ``None`` — запас неизвестен, судим по проценту, как
    раньше.
    """
    if free_bytes is not None and free_bytes >= min_free_bytes:
        return False
    return used_percent >= warn_percent


# Тревога в теле отчёта. Одно сообщение — один уровень тревоги: если
# заголовок уже назвал уровень (предупреждение задачи 7), тело не имеет
# права называть свой. Мягкий заголовок «остаётся немного» со срочным телом
# «места почти нет: занято 82 %» — два разных диагноза в одном сообщении, а
# срочный заголовок «почти кончилось» с тем же телом — повтор одного и того
# же дважды. И то и другое обесценивает оба уровня, ради различимости
# которых заголовки и заведены (см. ``_WARN_HEAD``/``_URGENT_HEAD``).
#
# Поэтому тело печатает ПОСЛЕДСТВИЕ, а уровень называет ровно один раз:
# заголовок, если он есть, иначе — сама эта строка (обычный ``/disk``
# приходит без заголовка, и там сказать про тревогу больше некому).
_ALARM_STANDALONE = (
    "⚠️ Места почти нет: занято {percent:.0f} %. Когда оно кончится, я не "
    "смогу ни сохранять присланные файлы, ни продолжать разговоры."
)
_ALARM_UNDER_URGENT_HEAD = (
    "Когда место кончится, я не смогу ни сохранять присланные файлы, ни "
    "продолжать разговоры."
)
_ALARM_UNDER_SOFT_HEAD = (
    "Работе это пока не мешает. Но место лучше освободить заранее, а не "
    "тогда, когда его не останется совсем."
)


def _alarm_line(used_percent: float, headline_level: Optional[str]) -> str:
    """Строка тревоги под уже сказанным (или не сказанным) заголовком."""
    if headline_level == WARN_URGENT:
        return _ALARM_UNDER_URGENT_HEAD
    if headline_level == WARN_SOFT:
        return _ALARM_UNDER_SOFT_HEAD
    return _ALARM_STANDALONE.format(percent=used_percent)


def format_report(
    report: DiskReport,
    *,
    warn_percent: float = 80.0,
    min_cleanup_bytes: int = _MIN_CLEANUP_BYTES,
    min_free_bytes: int = _DEFAULT_MIN_FREE_BYTES,
    headline_level: Optional[str] = None,
) -> str:
    """Текст для Telegram: без колонок и выравнивания пробелами.

    Обычное сообщение рендерится пропорциональным шрифтом, поэтому любые
    столбцы в нём рассыпаются. Каждая строка самодостаточна: «подпись —
    размер».

    ``headline_level`` — уровень тревоги, УЖЕ названный заголовком поверх
    этого отчёта (``format_warning``). ``None`` означает «заголовка нет,
    тревогу называет тело»: так приходит обычный ответ на ``/disk``.
    """
    lines = [
        f"💾 Диск: занято {_size(report.used)} из {_size(report.total)} "
        f"({report.used_percent:.0f} %), свободно {_size(report.free)}.",
        "",
        f"Данные агента — {_size(report.agent_bytes)}",
        f"• присланные документы — {_size(report.documents_bytes)}",
        f"• рабочая папка — {_size(report.workspace_bytes)}",
        f"• разговоры — {_size(report.sessions_bytes)}",
        f"• служебное — {_size(report.service_bytes)} ({_SERVICE_HINT})",
        "",
    ]

    # Разбивки «Остального» на образы Docker и всё прочее здесь нет и не
    # было: ``docker_bytes`` никем не передавался, ветка была недостижима в
    # продукте, и клиент всегда читал слитую строку. Числа при этом верны —
    # «Остальное» считается вычитанием, и образы Docker в него входят.
    lines.append(
        f"Остальное — {_size(report.other_bytes)} "
        "(система, программы, образы Docker)"
    )

    for note in report.notes:
        lines.append("")
        lines.append(note)

    cleanup_helps = report.removable_bytes >= min_cleanup_bytes
    if is_alarming(report.used_percent, warn_percent, report.free, min_free_bytes):
        lines.append("")
        lines.append(_alarm_line(report.used_percent, headline_level))
        if cleanup_helps:
            lines.append(
                f"Служебное можно сократить на {_size(report.removable_bytes)} — "
                "команда /disk clean:"
            )
            for label, size in group_removable(report.removable):
                lines.append(f"• {label} — {_size(size)}")
            lines.append("Ваши документы и рабочая папка не тронутся.")
        else:
            lines.append(
                "Своими силами это не решается: место занято не мной, убирать "
                f"у себя мне почти нечего. {_ASK_THE_OWNER}, — нужен диск "
                "побольше или уборка на самом сервере."
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Уборка
# ---------------------------------------------------------------------------

# `docker image prune` без `-a` сносит только безымянные (dangling) образы.
# Образ песочницы отмечен тегом и остаётся: полная очистка (`-a`) утащила бы
# его вместе с мусором (~1 ГБ), и следующая команда клиента ждала бы две
# минуты, пока образ качается заново. `docker system prune` не годится по
# соседней причине: он сносит остановленные контейнеры, а песочница
# переживает остановку и хранит внутри установленные пакеты.
DOCKER_PRUNE_ARGV: tuple[str, ...] = ("docker", "image", "prune", "--force")

_DOCKER_LABEL = "образы Docker"

# Docker печатает освобождённое человеческим размером: «Total reclaimed
# space: 1.245GB». Десятичные единицы — как у самого docker.
_RECLAIMED_UNITS: dict[str, int] = {
    "b": 1,
    "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
}


@dataclass(frozen=True)
class CleanResult:
    """Итог уборки. ``freed_bytes`` — измеренное, а не обещанное."""

    freed_bytes: int
    removed_labels: list[str]
    errors: list[str]


def _remove_tree(path: Path) -> None:
    """Удалить одну запись: файл, ссылку или каталог с содержимым.

    Вынесено отдельной функцией ради подмены в тестах. ``shutil.rmtree``
    по симлинкам не ходит: вложенная ссылка удаляется как ссылка, её цель
    остаётся на месте.
    """
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


# Отказ уборки — словами, а не кодом. ``str(OSError)`` даёт
# ``[Errno 13] Permission denied: '/var/…/logs/gateway.log'``: клиенту
# непонятен английский код и бесполезен абсолютный путь на хосте, а модуль
# терял голос ровно на том пути, ради которого написан (забитый диск,
# чужие права, root-овые каталоги песочницы). Подробности уходят в журнал.
_CLEAN_PROBLEM_TEXTS: dict[int, str] = {
    errno.EACCES: "не хватило прав",
    errno.EPERM: "не хватило прав",
    errno.EBUSY: "файл занят программой",
    errno.EROFS: "диск открыт только на чтение",
    errno.ENOTEMPTY: "каталог не опустел",
    errno.ENOTDIR: "на пути оказался файл вместо каталога",
    errno.ELOOP: "путь закольцован ссылками",
}
_CLEAN_PROBLEM_UNKNOWN = "система не дала это убрать"


def _clean_problem(exc: OSError, path: Path) -> Optional[str]:
    """Отказ по-русски. ``None`` — это не отказ.

    ``ENOENT`` не отказ, а достигнутая цель: файла уже нет. Так выглядит
    вторая одновременная уборка — она сносит то, что первая уже снесла, и
    прежний код называл каждый такой успех неудачей, выкладывая клиенту
    стену ``[Errno 2]`` с путями хоста.
    """
    code = getattr(exc, "errno", None)
    if code == errno.ENOENT:
        logger.debug("уборка: %s уже отсутствует", path)
        return None
    logger.warning("уборка: не удалось убрать %s: %s", path, exc)
    return _CLEAN_PROBLEM_TEXTS.get(code, _CLEAN_PROBLEM_UNKNOWN)


def _problem_summary(problems: list[str]) -> str:
    """Один отказ на каталог, а не строка на каждый файл.

    Клиент читает это в мессенджере: перечисление одинаковых причин по
    файлам не добавляет ему ничего, кроме длины.
    """
    seen: list[str] = []
    for text in problems:
        if text not in seen:
            seen.append(text)
    summary = "; ".join(seen)
    if len(problems) > 1:
        summary += f" (файлов: {len(problems)})"
    return summary


def _clear(path: Path) -> list[str]:
    """Вычистить содержимое каталога, оставив сам каталог.

    Каталог остаётся намеренно: шлюз держит ``logs/gateway.log``
    открытым, и снос каталога целиком уронил бы следующую ротацию на
    несуществующем пути. Заодно сохраняются права и владелец.

    Отказ на одной записи не отменяет остальные — их причины возвращаются
    списком (по-русски, см. ``_clean_problem``), а не бросаются наверх.
    """
    if not path.is_dir():
        try:
            _remove_tree(path)
        except OSError as exc:
            problem = _clean_problem(exc, path)
            return [problem] if problem else []
        return []

    try:
        entries = list(os.scandir(path))
    except OSError as exc:
        problem = _clean_problem(exc, path)
        return [problem] if problem else []

    problems: list[str] = []
    for entry in entries:
        try:
            _remove_tree(Path(entry.path))
        except OSError as exc:
            problem = _clean_problem(exc, Path(entry.path))
            if problem:
                problems.append(problem)
    return problems


def _forms(path: Path) -> tuple[Path, ...]:
    """Обе формы пути: «как написано» и «куда ведёт»."""
    resolved = _resolve(path)
    if resolved is None or resolved == path:
        return (path,)
    return (path, resolved)


def _touches_protected(home: Path, path: Path) -> bool:
    """Родство с защищёнными путями, посчитанное независимо от ``is_protected``.

    Намеренный дубль: одна проверка защиты в одном месте уже однажды
    пропустила опечатку в таблице путей. Здесь другой способ сравнения
    (``is_relative_to`` вместо покомпонентного родства) и другой вход,
    поэтому поломка одной реализации не выключает вторую.
    """
    for protected in protected_paths(home):
        if _resolve(protected) is None:
            return True
        for a in _forms(protected):
            for b in _forms(path):
                if a == b or a.is_relative_to(b) or b.is_relative_to(a):
                    return True
    return False


def _stays_inside_home(home: Path, path: Path) -> bool:
    """True, если путь — строгий потомок дома в ОБЕИХ формах.

    Строже родства из ``_shares_lineage``: тот считает роднёй и предка
    дома, поэтому ссылка на корень тома его проходит.
    """
    resolved_home = _resolve(home)
    resolved = _resolve(path)
    if resolved_home is None or resolved is None:
        return False
    # Обе половины ловят «кандидат — это сам дом»; отличает их только
    # путь, ВЕРНУВШИЙСЯ в дом обходом (``home/logs/..``), — его ловит
    # вторая. Первую отдельным входом не проверить: при ``path == home``
    # обе стороны второй сравнивают результат одного и того же
    # ``_resolve`` на одном значении, так что она срабатывает всегда.
    # Оставлена как утверждение о намерении, а не как рабочая ветка.
    if path == home or resolved == resolved_home:
        return False
    return path.is_relative_to(home) and resolved.is_relative_to(resolved_home)


def _refusal(home: Path, path: Path) -> Optional[str]:
    """Причина отказа удалять путь, по-русски. None — можно удалять.

    Проверки намеренно перекрываются: список кандидатов правится руками,
    и опечатка в нём не должна стоить клиенту документов. Отказ не
    проглатывается — он попадает в отчёт словами.
    """
    if is_protected(home, path):
        return "путь защищён — это файлы клиента или их каталог"
    if _touches_protected(home, path):
        return "путь пересекается с защищённым каталогом"
    if not _stays_inside_home(home, path):
        return "путь ведёт за пределы каталога агента"
    try:
        if path.is_symlink():
            return "это ссылка, а не каталог — удалять по ней нельзя"
    except OSError as exc:
        # Тот же голос, что и у отказов самой уборки: ни английского кода,
        # ни абсолютного пути хоста в тексте клиенту. ``None`` (путь исчез)
        # — не отказ: убирать уже нечего, и ``_clear`` ниже это увидит.
        return _clean_problem(exc, path)
    return None


# Уборка одного дома — по одной за раз. Две одновременные считали одно и то
# же дважды: каждая отчитывалась ПОЛНЫМ объёмом (7,6 МБ и 7,6 МБ при
# реальных 7,6 на диске), сумма выходила вдвое больше правды, и обе
# вдобавок выкладывали стену ошибок с путями — вторая натыкалась на
# файлы, снесённые первой, и называла чужой успех своей неудачей.
#
# Ждущая вторая после замка честно отчитывается «убирать было нечего»:
# отдельного текста для этого не нужно — ``format_clean_result`` уже умеет
# такой итог. Ждать её заставляет замок, а не таймаут: уборка идёт секунды,
# а альтернатива — отказ, который клиент прочтёт как «команда не сработала».
#
# Замок в памяти процесса, а не файловый: у клиента один шлюз, и обе
# уборки живут в нём (мид-ран ``/disk clean`` плюс её повтор). Тот же
# выбор, что и у ``_cache_lock`` выше. Межпроцессный случай (CLI на хосте
# параллельно шлюзу) остаётся незакрытым — записан в отчёте.
_clean_locks: dict[str, threading.Lock] = {}
_clean_locks_guard = threading.Lock()


def _clean_lock(home: Path) -> threading.Lock:
    """Замок этого дома. У каждого профиля свой: уборка в одном не обязана
    ждать уборку в другом."""
    key = _cache_key(home)
    with _clean_locks_guard:
        lock = _clean_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _clean_locks[key] = lock
        return lock


def clean(home: Path, *, docker_prune=None) -> CleanResult:
    """Убрать служебное — по одной уборке на дом за раз (см. ``_clean_lock``)."""
    with _clean_lock(home):
        return _clean_once(home, docker_prune=docker_prune)


def _clean_once(home: Path, *, docker_prune=None) -> CleanResult:
    """Убрать служебное. Файлы клиента не трогаются ни при каких условиях.

    Список берётся у ``removable_candidates()`` — тот же, по которому
    отчёт обещал клиенту освобождаемый объём, и уже отфильтрованный
    защитой. Перед каждым удалением он проверяется заново (см.
    ``_refusal``): между «что предложили» и «что сносим» не должно быть
    ни одного пути, прошедшего только одну проверку.

    ``freed_bytes`` — разница замеров до и после, а не обещанный размер:
    частичный отказ обязан уменьшить число, иначе клиент на забитом диске
    получит обещаний больше, чем места.

    ``docker_prune`` — необязательный вызов очистки Docker. Готовый
    безопасный вариант — ``docker_prune`` из этого же модуля; полная
    очистка (``-a``) снесла бы образ песочницы, и следующая команда
    клиента ждала бы его повторной загрузки.
    """
    freed = 0
    removed: list[str] = []
    errors: list[str] = []

    for label, path in removable_candidates(home):
        try:
            present = path.exists() or path.is_symlink()
        except OSError:
            present = True
        if not present:
            continue

        refusal = _refusal(home, path)
        if refusal is not None:
            errors.append(f"{label}: {refusal}")
            continue

        before = _measure(path).total
        problems = _clear(path)
        after = _measure(path).total

        if problems:
            errors.append(f"{label}: {_problem_summary(problems)}")

        gained = before - after
        if gained > 0:
            freed += gained
            if label not in removed:
                removed.append(label)

    if docker_prune is not None:
        try:
            gained = int(docker_prune() or 0)
        except Exception as exc:  # noqa: BLE001 — уборка не должна падать
            errors.append(f"{_DOCKER_LABEL}: {exc}")
        else:
            if gained > 0:
                freed += gained
                removed.append(_DOCKER_LABEL)

    # Замер этого дома устарел в ту же секунду. Сбрасывается безусловно, а
    # не только при freed > 0: частичный успех и полный отказ тоже могли
    # изменить дерево (часть записей снесена, часть — нет).
    invalidate_report_cache(home)

    return CleanResult(freed_bytes=freed, removed_labels=removed, errors=errors)


def _parse_reclaimed(output: str) -> int:
    """Байты из строки «Total reclaimed space: 1.245GB». 0 — не разобрали."""
    import re

    match = re.search(
        r"reclaimed space:\s*([\d.,]+)\s*([A-Za-z]+)", output, re.IGNORECASE
    )
    if not match:
        return 0
    scale = _RECLAIMED_UNITS.get(match.group(2).lower())
    if scale is None:
        return 0
    try:
        return int(float(match.group(1).replace(",", ".")) * scale)
    except ValueError:  # pragma: no cover — цифры уже отобраны регуляркой
        return 0


def docker_prune(run=None) -> int:
    """Убрать безымянные образы Docker; вернуть освобождённые байты.

    Выполняет ровно ``DOCKER_PRUNE_ARGV`` — без флага полной очистки (см.
    комментарий к константе). Не бросает: на машине без Docker убирать
    нечего, и уборка файлов не должна из-за этого останавливаться.
    """
    if run is None:  # pragma: no cover — в тестах всегда подменяется
        import subprocess

        def run(argv, **kwargs):
            return subprocess.run(argv, **kwargs)

    try:
        done = run(
            list(DOCKER_PRUNE_ARGV),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception:  # noqa: BLE001 — нет docker, нет прав, таймаут
        return 0
    if getattr(done, "returncode", 1) != 0:
        return 0
    return _parse_reclaimed(getattr(done, "stdout", "") or "")


def format_clean_result(result: CleanResult) -> str:
    """Итог уборки словами. Первая фраза — про результат, не про команду.

    Полный провал заканчивается тем же советом, что и отчёт о забитом
    диске (``_ASK_THE_OWNER``): «убрать не удалось» без продолжения
    оставляет клиента ровно там же, откуда он писал команду. Частичный
    успех этот совет не получает — «своими силами не решается» рядом с
    «освобождено 3 ГБ» было бы неправдой.
    """
    if not result.removed_labels and not result.errors:
        return "Служебных файлов, которые стоит убрать, не нашлось."

    lines: list[str] = []
    failed = result.freed_bytes <= 0
    if not failed:
        lines.append(f"🧹 Освобождено {_size(result.freed_bytes)}.")
        if result.removed_labels:
            lines.append("Убрано: " + ", ".join(result.removed_labels) + ".")
    else:
        lines.append("Убрать ничего не удалось.")
    lines.append("Ваши документы и рабочая папка не тронуты.")
    if result.errors:
        lines.append("Не удалось убрать: " + "; ".join(result.errors))
    if failed:
        lines.append(
            f"Своими силами это не решается. {_ASK_THE_OWNER}, — убрать эти "
            "файлы нужно на самом сервере."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Наблюдение: порог предупреждения и месячная сводка
# ---------------------------------------------------------------------------

# Отметки о том, что клиенту уже сказано. Лежат в HERMES_HOME, а не в
# памяти процесса: шлюз перезапускается (обновление, systemd, упавший
# адаптер), и после каждого перезапуска клиент получал бы то же
# предупреждение заново — ровно тот шум, ради отсутствия которого всё это
# и написано.
_STATE_FILE = "trix_disk_state.json"

# Месяц — тридцать суток. Календарный месяц здесь ничего не улучшает
# (сводка не привязана к числу), а тянет за собой часовые пояса и переводы
# часов.
_MONTH_SECONDS: int = 30 * 24 * 3600

# Значения по умолчанию продублированы литералами, а не прочитаны из
# DEFAULT_CONFIG: модуль намеренно не зависит от hermes_cli.config — его
# зовут и из шлюза, и из CLI. Расхождение двух копий ловится тестом
# отношением («каждый ключ корня disk что-то меняет»), а не снимком.
_DEFAULT_WARN_PERCENT: float = 80.0
_DEFAULT_URGENT_PERCENT: float = 90.0
# Насколько ниже порога надо упасть, чтобы то же предупреждение снова
# имело право прозвучать. Одна десятая процентного пункта — это двадцать
# мегабайт на стогигабайтном диске, то есть один присланный документ:
# без запаса обычная работа агента качала бы занятость через порог весь
# день (79.9 → 80.0 → 79.9 → 80.1 давало четыре одинаковых сообщения за
# четыре часа).
_DEFAULT_REARM_PERCENT: float = 3.0
# Потолок на повтор ОДНОГО И ТОГО ЖЕ предупреждения, даже если занятость
# честно ходит через порог туда-обратно с большим размахом.
_DEFAULT_REPEAT_AFTER_HOURS: float = 24.0


@dataclass(frozen=True)
class DiskThresholds:
    """Пороги из ``config.yaml``.

    В ``.env`` им не место: это поведенческие настройки, а не секреты.
    """

    warn_percent: float = _DEFAULT_WARN_PERCENT
    urgent_percent: float = _DEFAULT_URGENT_PERCENT
    rearm_percent: float = _DEFAULT_REARM_PERCENT
    repeat_after_hours: float = _DEFAULT_REPEAT_AFTER_HOURS
    min_cleanup_bytes: int = _MIN_CLEANUP_BYTES
    min_free_bytes: int = _DEFAULT_MIN_FREE_BYTES


def _number(raw, fallback: float) -> float:
    """Число из конфига или из отметки, иначе ``fallback``.

    Строка «восемьдесят» в config.yaml и мусор в файле отметок не имеют
    права уронить почасовую уборку шлюза: с ней в одном потоке живут
    чистка кэшей, куратор и авто-архивация сессий.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return fallback
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")):
        return fallback
    return value


def disk_thresholds(config: Optional[dict] = None) -> DiskThresholds:
    """Пороги из корня ``disk``. ``config=None`` — прочитать живой конфиг."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            config = {}
    section = config.get("disk") if isinstance(config, dict) else None
    if not isinstance(section, dict):
        section = {}

    warn = _number(section.get("warn_percent"), _DEFAULT_WARN_PERCENT)
    urgent = _number(section.get("urgent_percent"), _DEFAULT_URGENT_PERCENT)
    rearm = _number(section.get("rearm_percent"), _DEFAULT_REARM_PERCENT)
    repeat = _number(section.get("repeat_after_hours"), _DEFAULT_REPEAT_AFTER_HOURS)
    min_mb = _number(section.get("min_cleanup_mb"), _MIN_CLEANUP_BYTES / 1024 ** 2)
    min_free_gb = _number(
        section.get("min_free_gb"), _DEFAULT_MIN_FREE_BYTES / 1024 ** 3
    )
    return DiskThresholds(
        warn_percent=warn,
        min_free_bytes=int(min_free_gb * 1024 ** 3),
        # Срочный порог ниже мягкого означал бы «🔴 почти кончилось» с
        # первого же предупреждения — клиент перестал бы их различать, и
        # настоящая тревога потерялась бы среди ранних.
        urgent_percent=max(warn, urgent),
        rearm_percent=max(0.0, rearm),
        repeat_after_hours=max(0.0, repeat),
        min_cleanup_bytes=int(max(0.0, min_mb) * 1024 ** 2),
    )


# ---------------------------------------------------------------------------
# Состояние
# ---------------------------------------------------------------------------

# Отметки, которые НЕ УДАЛОСЬ записать на диск. Условие срабатывания
# совпадает с тем, о чём мы предупреждаем: на кончившемся диске запись
# падает по нехватке места. Без этого запаса «один раз на пересечение»
# превращалось бы в двадцать четыре «место почти кончилось» в сутки —
# ровно в главном сценарии, ради которого всё написано.
#
# Память процесса перезапуск не переживает; это меньшее из зол и о каждой
# неудачной записи громко сказано в журнале.
_state_fallback: dict[str, dict] = {}


def load_state(home: Path) -> dict:
    """Отметки о сказанном. Битый или отсутствующий файл — пустой словарь.

    Если последняя запись не удалась, отдаётся запомненное в памяти
    процесса: оно новее того, что осталось на диске.

    Худшее последствие потерянного файла — лишнее предупреждение и лишняя
    сводка. Худшее последствие исключения отсюда — упавшая почасовая
    уборка шлюза целиком. Цена несравнима, поэтому ловится всё, а не
    только ``json.JSONDecodeError``.
    """
    remembered = _state_fallback.get(_cache_key(home))
    if remembered is not None:
        return dict(remembered)
    try:
        raw = json.loads((home / _STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(home: Path, state: dict) -> bool:
    """Записать отметки целиком или не записать вовсе. ``False`` — не вышло.

    Через временный файл и ``os.replace``: обрывок JSON на месте отметок
    прочитался бы как «клиенту ещё ничего не говорили» — то есть выпустил
    бы лишнее предупреждение и лишнюю сводку ровно там, где мы их глушим.

    Неудача НЕ проглатывается: она пишется в журнал предупреждением и
    отметки запоминаются в памяти процесса. Молчаливая неудача здесь —
    это спам клиенту каждый час на кончившемся диске и пустой журнал у
    владельца машины.
    """
    key = _cache_key(home)
    target = home / _STATE_FILE
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except Exception as exc:
        _state_fallback[key] = dict(state)
        logger.warning(
            "Проверка диска: отметки не записались в %s (%s: %s) — держу их "
            "в памяти процесса, до перезапуска повторов не будет",
            target, type(exc).__name__, exc,
        )
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    # Диск снова пишется — он и есть источник правды.
    _state_fallback.pop(key, None)
    return True


def _wall_clock(now_ts: Optional[float] = None) -> float:
    """Стенные часы. Монотонные тут не годятся: отметки переживают
    перезапуск процесса, а монотонные часы — нет."""
    return time.time() if now_ts is None else float(now_ts)


def _timestamp(state: dict, key: str) -> Optional[float]:
    """Отметка времени, если она осмысленная. Мусор — как её нет."""
    raw = state.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _window_open(state: dict, key: str, now: float, window_seconds: float) -> bool:
    """Прошло ли ``window_seconds`` с отметки ``key``.

    Отметки нет (или она мусор) — окно открыто: этого ещё не случалось.
    Подставлять сюда ноль нельзя, иначе на часах, стоящих в нуле, «никогда
    не говорили» читалось бы как «только что сказали».

    Отметка в БУДУЩЕМ считается просроченной: часы могли уйти вперёд, а
    потом их поправили, и иначе такая отметка заперла бы всё на длину
    сдвига (при часах на год вперёд — на год).
    """
    stamp = _timestamp(state, key)
    if stamp is None:
        return True
    elapsed = now - stamp
    return elapsed < 0 or elapsed >= window_seconds


# ---------------------------------------------------------------------------
# Порог
# ---------------------------------------------------------------------------

# Уровни предупреждения, от мягкого к срочному. Решение об уровне
# отделено от текста намеренно: уровень считается по ОДНОМУ проценту
# занятости (микросекунды), а текст требует обхода всех данных агента
# (секунды, на раздутой машине — десятки). Разделив их, почасовая
# проверка платит за обход только в тот час, когда клиенту есть что
# показать.
WARN_SOFT = "soft"
WARN_URGENT = "urgent"

# Взведён ли уровень и когда он последний раз звучал.
_LEVEL_KEYS: dict[str, tuple[str, str]] = {
    WARN_SOFT: ("soft_armed", "last_soft_ts"),
    WARN_URGENT: ("urgent_armed", "last_urgent_ts"),
}

# Что гасится вместе с уровнем: жёлтое «остаётся немного» сразу после
# красного «почти кончилось» клиент прочтёт как поломку.
_LEVELS_AT_OR_BELOW: dict[str, tuple[str, ...]] = {
    WARN_SOFT: (WARN_SOFT,),
    WARN_URGENT: (WARN_SOFT, WARN_URGENT),
}


def _armed(state: dict, level: str) -> bool:
    """Взведён ли уровень. Мусор в отметке — считаем взведённым.

    Ошибаться здесь надо в сторону «сказать»: непрочитанное
    предупреждение о диске стоит дороже одного лишнего.
    """
    raw = state.get(_LEVEL_KEYS[level][0])
    return raw if isinstance(raw, bool) else True


def refresh_warn_arming(
    state: dict,
    used_percent: Optional[float],
    *,
    warn_percent: float = _DEFAULT_WARN_PERCENT,
    urgent_percent: float = _DEFAULT_URGENT_PERCENT,
    rearm_percent: float = _DEFAULT_REARM_PERCENT,
) -> None:
    """Взвести уровни заново, если диск разгрузили ЗАМЕТНО ниже порога.

    Гистерезис. Без запаса падение на одну десятую процентного пункта —
    двадцать мегабайт, один присланный документ — снова разрешает то же
    самое предупреждение, и обычная работа агента качает занятость через
    порог весь день. Клиент прочитает четвёртое одинаковое жёлтое
    сообщение, перестанет читать — и не прочитает красное.

    Бухгалтерия дешёвая (одно сравнение) и обязана идти КАЖДЫЙ час, в том
    числе в тот, когда обхода дерева не будет.
    """
    if used_percent is None:
        return
    pct = float(used_percent)
    for level, threshold in ((WARN_SOFT, warn_percent), (WARN_URGENT, urgent_percent)):
        if pct < threshold - rearm_percent:
            state[_LEVEL_KEYS[level][0]] = True


def warn_level(
    used_percent: Optional[float],
    state: dict,
    *,
    warn_percent: float = _DEFAULT_WARN_PERCENT,
    urgent_percent: float = _DEFAULT_URGENT_PERCENT,
    repeat_after_hours: float = _DEFAULT_REPEAT_AFTER_HOURS,
    free_bytes: Optional[int] = None,
    min_free_bytes: int = _DEFAULT_MIN_FREE_BYTES,
    now_ts: Optional[float] = None,
) -> Optional[str]:
    """``WARN_URGENT`` / ``WARN_SOFT`` / ``None`` — что сказать прямо сейчас.

    Читает состояние, но НЕ меняет его: отметку ставит ``mark_warned``, и
    только после того, как сообщение действительно ушло клиенту.

    Считает ровно по занятости раздела — тому числу, что даёт
    ``partition_used_percent`` одним системным вызовом. Обход дерева нужен,
    только чтобы показать разбивку, а не чтобы понять, что места мало.

    ``used_percent=None`` (замер не удался) — молчим.
    """
    if used_percent is None:
        return None
    now = _wall_clock(now_ts)
    pct = float(used_percent)
    window = repeat_after_hours * 3600
    for level, threshold in ((WARN_URGENT, urgent_percent), (WARN_SOFT, warn_percent)):
        if not is_alarming(pct, threshold, free_bytes, min_free_bytes):
            continue
        if not _armed(state, level):
            continue
        if not _window_open(state, _LEVEL_KEYS[level][1], now, window):
            continue
        return level
    return None


def mark_warned(state: dict, level: str, now_ts: Optional[float] = None) -> None:
    """Записать, что предупреждение уровня ``level`` показано клиенту."""
    now = _wall_clock(now_ts)
    for candidate in _LEVELS_AT_OR_BELOW[level]:
        state[_LEVEL_KEYS[candidate][0]] = False
    state[_LEVEL_KEYS[level][1]] = now


# Первая строка предупреждения. Клиент видит её в уведомлении Telegram, не
# разворачивая сообщение, поэтому мягкое и срочное обязаны читаться
# по-разному: одинаковый текст на 82 % и на 95 % обесценивает оба.
_WARN_HEAD = "🟡 Места на диске остаётся немного."
_URGENT_HEAD = "🔴 Место на диске почти кончилось."


def format_warning(
    level: str,
    report: DiskReport,
    *,
    warn_percent: float = _DEFAULT_WARN_PERCENT,
    min_cleanup_bytes: int = _MIN_CLEANUP_BYTES,
    min_free_bytes: int = _DEFAULT_MIN_FREE_BYTES,
) -> str:
    """Текст предупреждения выбранного уровня.

    Уровень передаётся в тело: одно сообщение — один уровень тревоги.
    Раньше тело звало ``format_report`` без него и печатало свою тревогу по
    собственному порогу — мягкий заголовок «остаётся немного» и строкой ниже
    «⚠️ Места почти нет: занято 82 %» в одном сообщении.
    """
    head = _URGENT_HEAD if level == WARN_URGENT else _WARN_HEAD
    return head + "\n\n" + format_report(
        report,
        warn_percent=warn_percent,
        min_cleanup_bytes=min_cleanup_bytes,
        min_free_bytes=min_free_bytes,
        headline_level=level,
    )


def should_warn(
    report: DiskReport,
    state: dict,
    *,
    warn_percent: float = _DEFAULT_WARN_PERCENT,
    urgent_percent: float = _DEFAULT_URGENT_PERCENT,
    rearm_percent: float = _DEFAULT_REARM_PERCENT,
    repeat_after_hours: float = _DEFAULT_REPEAT_AFTER_HOURS,
    min_cleanup_bytes: int = _MIN_CLEANUP_BYTES,
    now_ts: Optional[float] = None,
) -> Optional[str]:
    """Текст предупреждения по готовому отчёту, иначе ``None``.

    Взведение, решение, отметка и текст в одном вызове — для того, у кого
    отчёт уже на руках. Почасовая проверка ходит длинным путём
    (``refresh_warn_arming`` и ``warn_level`` до обхода, ``mark_warned``
    только после доставки), чтобы не платить за обход впустую и не гасить
    предупреждение, которое до клиента не дошло.
    """
    refresh_warn_arming(
        state,
        report.used_percent,
        warn_percent=warn_percent,
        urgent_percent=urgent_percent,
        rearm_percent=rearm_percent,
    )
    level = warn_level(
        report.used_percent,
        state,
        warn_percent=warn_percent,
        urgent_percent=urgent_percent,
        repeat_after_hours=repeat_after_hours,
        now_ts=now_ts,
    )
    if level is None:
        return None
    mark_warned(state, level, now_ts)
    return format_warning(
        level, report, warn_percent=warn_percent, min_cleanup_bytes=min_cleanup_bytes
    )


# ---------------------------------------------------------------------------
# Месячная сводка
# ---------------------------------------------------------------------------

# Первая строка месячной сводки — она приходит на спокойном диске, и по
# ней клиент должен сразу понять, что это плановое письмо, а не тревога.
MONTHLY_HEAD = "📅 Ежемесячная сводка: чем занято место на диске."

# Отметка последней ПОПЫТКИ показать сводку. Сводка не срочная, а её текст
# стоит полного обхода дерева: недоставленную не повторяем каждый час.
_MONTHLY_TRY_KEY = "last_monthly_try_ts"


def _monthly_mark(state: dict) -> Optional[float]:
    """Отметка последней сводки, если она осмысленная. Мусор — как её нет."""
    return _timestamp(state, "last_monthly_ts")


def stamp_monthly(state: dict, now_ts: Optional[float] = None) -> None:
    """Отметить, что сводка только что показана (или что отсчёт пошёл)."""
    state["last_monthly_ts"] = _wall_clock(now_ts)
    state.pop(_MONTHLY_TRY_KEY, None)


def stamp_monthly_attempt(state: dict, now_ts: Optional[float] = None) -> None:
    """Отметить неудачную попытку показать сводку."""
    state[_MONTHLY_TRY_KEY] = _wall_clock(now_ts)


def start_monthly_countdown(state: dict, now_ts: Optional[float] = None) -> bool:
    """Начать отсчёт до первой сводки, если он ещё не начат.

    ``True``, если отметка поставлена сейчас. Первая сводка приходит через
    месяц, а не в первый же час после установки: клиент только что настроил
    машину и впервые написал боту — технический отчёт, которого он не
    просил, читается не заботой, а сбоем, и первое впечатление о продукте
    портится на ровном месте. Через месяц есть что показывать.

    Предупреждение о пороге это НЕ задерживает: оно по делу и работает с
    первой секунды (см. ``warn_level``).

    Битая отметка начинает отсчёт заново — так она не запирает сводку
    навсегда и не выпускает лишнюю немедленно. Отметка В БУДУЩЕМ (часы
    шли вперёд, потом их поправили) тоже: иначе сводка была бы заперта на
    всю длину сдвига — при часах на год вперёд не пришла бы ни через два
    месяца, ни через одиннадцать.
    """
    now = _wall_clock(now_ts)
    last = _monthly_mark(state)
    if last is not None and last <= now:
        return False
    stamp_monthly(state, now)
    return True


def should_report_monthly(
    state: dict,
    now_ts: Optional[float] = None,
    *,
    retry_after_hours: float = _DEFAULT_REPEAT_AFTER_HOURS,
) -> bool:
    """Пора ли слать месячную сводку.

    Без отметки — не пора: отсчёт ещё не начат (см.
    ``start_monthly_countdown``).

    Недоставленная сводка не повторяется каждый час: её текст стоит
    полного обхода дерева, а сама она не срочная — в отличие от
    предупреждения, которое повторяется на следующем же часу.
    """
    last = _monthly_mark(state)
    if last is None:
        return False
    now = _wall_clock(now_ts)
    if (now - last) < _MONTH_SECONDS:
        return False
    return _window_open(state, _MONTHLY_TRY_KEY, now, retry_after_hours * 3600)
