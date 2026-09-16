"""Уборка временного каталога профиля в режиме contained.

Зачем это существует. В ``shared-host`` временные файлы лежат в ``/tmp``,
и их убирает ОС: systemd-tmpfiles или перезагрузка. В ``contained``
``TMPDIR`` уезжает в ``<root>/tmp`` — внутрь корня профиля, который живёт на
обычном диске и никем не чистится. Без уборки первый же профиль с активным
браузером и распаковками за месяц съедает свободное место, и это проявится
не как «кончился /tmp», а как «бот молчит».

Правила обхода здесь жёстче обычных, потому что чистильщик удаляет файлы:

* только ``os.scandir(follow_symlinks=False)`` и ``os.lstat`` — внутрь
  символических ссылок не спускаемся никогда;
* ссылка удаляется КАК ССЫЛКА (``unlink``), её цель не трогается;
* перед каждым удалением путь разрешается и проверяется на принадлежность
  ``<root>/tmp``: подменённый симлинком каталог не может увести уборку
  наружу;
* записи моложе десяти минут не удаляются никогда, даже под квотой — иначе
  уборка отнимет файл у работающей прямо сейчас операции. Тот же принцип,
  что у уборки debug-отчётов.

Функция :func:`sweep` чистая: получает корень, возраст, потолок и «сейчас»,
ничего не читает из конфига и не пишет в логи. Это позволяет проверять её
исполнением на ``tmp_path``, а не мокая время.
"""

from __future__ import annotations

import errno
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# Файлы моложе этого возраста неприкосновенны: их почти наверняка держит
# работающая операция, и удаление обернётся невоспроизводимой ошибкой у
# клиента вместо освобождённых мегабайт.
MIN_AGE_SECONDS = 600

DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_MAX_SIZE_MB = 2048


@dataclass
class SweepResult:
    """Что уборка сделала — для лога и для тестов."""

    removed_files: int = 0
    removed_dirs: int = 0
    removed_links: int = 0
    freed_bytes: int = 0
    remaining_bytes: int = 0
    skipped_outside: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def removed(self) -> int:
        return self.removed_files + self.removed_dirs + self.removed_links


@dataclass
class _Entry:
    path: str
    is_dir: bool
    is_link: bool
    size: int
    mtime: float


def _safe_under(root: Path, path: str) -> bool:
    """``True``, если путь после разрешения ссылок всё ещё внутри корня."""
    try:
        return Path(os.path.realpath(path)).is_relative_to(Path(os.path.realpath(root)))
    except (OSError, ValueError):
        return False


def _collect(root: Path, result: SweepResult) -> list[_Entry]:
    """Собрать дерево под корнем, ни разу не пройдя сквозь симлинк."""
    entries: list[_Entry] = []
    stack: list[str] = [str(root)]
    while stack:
        current = stack.pop()
        try:
            scan = os.scandir(current)
        except OSError as exc:
            result.errors.append(f"{current}: {exc}")
            continue
        with scan:
            for item in scan:
                try:
                    stat = item.stat(follow_symlinks=False)
                except OSError as exc:
                    result.errors.append(f"{item.path}: {exc}")
                    continue
                is_link = item.is_symlink()
                is_dir = item.is_dir(follow_symlinks=False)
                entries.append(
                    _Entry(
                        path=item.path,
                        is_dir=is_dir,
                        is_link=is_link,
                        size=0 if is_dir and not is_link else stat.st_size,
                        mtime=stat.st_mtime,
                    )
                )
                # Внутрь ссылки не спускаемся — даже если она указывает на
                # каталог внутри корня. Обход по ссылкам зацикливается и,
                # хуже, выводит уборку наружу.
                if is_dir and not is_link:
                    stack.append(item.path)
    return entries


def _remove(root: Path, entry: _Entry, result: SweepResult) -> bool:
    if not _safe_under(root, entry.path):
        # Симлинк — единственный случай, когда realpath уходит наружу и
        # удаление всё равно законно: мы удаляем ссылку, а не цель.
        if not entry.is_link:
            result.skipped_outside.append(entry.path)
            return False
    try:
        if entry.is_link:
            os.unlink(entry.path)
            result.removed_links += 1
        elif entry.is_dir:
            os.rmdir(entry.path)
            result.removed_dirs += 1
        else:
            os.unlink(entry.path)
            result.removed_files += 1
            result.freed_bytes += entry.size
        return True
    except OSError as exc:
        # Каталог не опустел — штатный исход, а не отказ: внутри остался
        # файл, пощажённый по возрасту, ровно как задумано. Раньше это
        # попадало в ``errors``, и «освободить место» отчитывалось клиенту
        # ошибкой о том, что уборка сработала правильно.
        if exc.errno == errno.ENOTEMPTY:
            return False
        result.errors.append(f"{entry.path}: {exc}")
        return False


def sweep(
    root: str | os.PathLike,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    max_size_mb: float = DEFAULT_MAX_SIZE_MB,
    now: float | None = None,
) -> SweepResult:
    """Убрать ``<root>/tmp``: сначала по возрасту, потом до потолка размера.

    Args:
        root: корень профиля; чистится его подкаталог ``tmp``.
        max_age_days: старше этого — удаляется. ``0`` отключает уборку по
            возрасту (и это же аварийный выключатель всей фазы).
        max_size_mb: потолок суммарного размера. При превышении удаляются
            самые старые записи, пока размер не уложится. ``0`` — без квоты.
        now: «сейчас» в секундах epoch; параметр существует ради тестов.

    Returns:
        :class:`SweepResult` — что удалено, сколько освобождено, что
        пропущено как вышедшее за корень.
    """
    result = SweepResult()
    tmp_root = Path(root) / "tmp"
    if not tmp_root.is_dir() or tmp_root.is_symlink():
        return result
    now = time.time() if now is None else now

    entries = _collect(tmp_root, result)
    alive: list[_Entry] = []

    # 1. Возраст. Каталоги удаляются после своего содержимого, поэтому
    #    обрабатываем длинные пути первыми.
    age_cutoff = now - max_age_days * 86400 if max_age_days > 0 else None
    fresh_cutoff = now - MIN_AGE_SECONDS
    for entry in sorted(entries, key=lambda e: len(Path(e.path).parts), reverse=True):
        too_young = entry.mtime > fresh_cutoff
        expired = age_cutoff is not None and entry.mtime < age_cutoff
        if expired and not too_young and _remove(tmp_root, entry, result):
            continue
        alive.append(entry)

    # 2. Квота. Каталоги места не занимают — считаем и удаляем только
    #    файлы и ссылки, самые старые первыми.
    if max_size_mb > 0:
        limit = int(max_size_mb * 1024 * 1024)
        files = [e for e in alive if not e.is_dir or e.is_link]
        total = sum(e.size for e in files)
        if total > limit:
            for entry in sorted(files, key=lambda e: e.mtime):
                if total <= limit:
                    break
                if entry.mtime > fresh_cutoff:
                    continue
                size = entry.size
                if _remove(tmp_root, entry, result):
                    total -= size
        result.remaining_bytes = total
    else:
        result.remaining_bytes = sum(e.size for e in alive if not e.is_dir or e.is_link)

    return result


def sweep_config(config: dict | None) -> tuple[float, float]:
    """Достать ручки уборки из ``isolation:`` конфига, с дефолтами."""
    section = {}
    if isinstance(config, dict):
        raw = config.get("isolation")
        if isinstance(raw, dict):
            section = raw

    def _num(key: str, default: float) -> float:
        try:
            value = float(section.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default

    return (
        _num("tmp_max_age_days", DEFAULT_MAX_AGE_DAYS),
        _num("tmp_max_size_mb", DEFAULT_MAX_SIZE_MB),
    )


def sweep_profile_tmp_if_due(config: dict | None = None, *, force: bool = False) -> SweepResult | None:
    """Убрать временный каталог профиля не чаще раза в сутки.

    Зовётся при старте шлюза и из почасового тика проверки диска. Отметка
    хранится в ``<root>/tmp/.last-sweep``: внутри самого убираемого
    каталога, потому что уборка и отметка обязаны уезжать и удаляться
    вместе с корнем. Возвращает ``None``, если убирать нечего или рано.
    """
    try:
        from hermes_constants import get_profile_root, is_contained
    except Exception:
        return None
    if not is_contained():
        return None

    root = get_profile_root()
    marker = root / "tmp" / ".last-sweep"
    now = time.time()
    if not force:
        try:
            if now - marker.stat().st_mtime < 86400:
                return None
        except OSError:
            pass

    max_age_days, max_size_mb = sweep_config(config)
    if max_age_days <= 0 and max_size_mb <= 0:
        return None

    result = sweep(root, max_age_days=max_age_days, max_size_mb=max_size_mb, now=now)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass
    return result
