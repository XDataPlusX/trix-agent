"""Проверка доктора: раскладка профиля и его изоляция.

Живёт в своём модуле, а в `doctor.py` уходит один вызов — по той же
причине, что `trix_numpy_check.py` и `trix_setup_service_check.py`: доктор
это ~3300 строк, которые мы регулярно тянем сверху, и каждая наша функция
внутри него оплачивается конфликтом при мёрже.

Что проверяется:

* режим изоляции и откуда он взялся — оператор должен видеть это, не
  перечитывая код;
* ветви профиля (HOME подпроцессов, TMPDIR, рабочая папка) действительно
  внутри корня; вывод ветви наружу — не ошибка, а режим ``custom``, но о
  нём предупреждаем: чаще всего это забытая переменная в юните;
* **тип файловой системы под корнем**. Это единственная проверка здесь,
  которая может дать `fail`. SQLite в режиме WAL — а на нём живёт
  ``state.db`` — на сетевой ФС повреждается: блокировки NFS/CIFS/9p не
  дают тех гарантий, на которые WAL рассчитывает. В ``shared-host`` это
  предупреждение (так живут и сегодня), в ``contained`` — отказ, потому
  что там под корень уезжает ВСЁ состояние профиля сразу.
"""

from __future__ import annotations

import os
from pathlib import Path

# Согласовано с `_WAL_INCOMPAT_MARKERS` в hermes_state.py: там тот же
# список решает, можно ли включать WAL.
_NETWORK_FS_MARKERS: tuple[str, ...] = (
    "nfs", "nfs4", "cifs", "smb", "smb2", "smb3", "9p", "vboxsf", "afs", "ncpfs",
)
_FUSE_PREFIX = "fuse."
# Эти fuse-варианты — локальные и безопасные; отказывать по ним нельзя.
_FUSE_LOCAL_OK: tuple[str, ...] = ("fuse.gvfsd-fuse", "fuse.portal", "fuse.snapfuse")


def _iter_mounts(mountinfo: str | os.PathLike = "/proc/self/mountinfo") -> list[tuple[str, str]]:
    """Вернуть пары (точка монтирования, тип ФС) из mountinfo.

    Формат: поля до разделителя ``-`` переменной длины, после него идут тип
    ФС, источник и опции. Поэтому ищем разделитель, а не фиксированный
    индекс. Пробелы в путях экранированы восьмеричными последовательностями.
    """
    mounts: list[tuple[str, str]] = []
    try:
        with open(mountinfo, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                try:
                    sep = parts.index("-")
                except ValueError:
                    continue
                if len(parts) <= sep + 1 or sep < 5:
                    continue
                point = parts[4].replace("\\040", " ").replace("\\011", "\t")
                mounts.append((point, parts[sep + 1]))
    except OSError:
        return []
    return mounts


def filesystem_for(path: str | os.PathLike, mountinfo: str | os.PathLike = "/proc/self/mountinfo") -> str | None:
    """Вернуть тип ФС, на которой лежит путь (самая длинная точка монтирования)."""
    mounts = _iter_mounts(mountinfo)
    if not mounts:
        return None
    try:
        target = Path(os.path.realpath(path))
    except OSError:
        return None
    best: tuple[int, str] | None = None
    for point, fstype in mounts:
        try:
            mount_path = Path(point)
            if target == mount_path or target.is_relative_to(mount_path):
                depth = len(mount_path.parts)
                if best is None or depth > best[0]:
                    best = (depth, fstype)
        except (ValueError, OSError):
            continue
    return best[1] if best else None


def is_network_filesystem(fstype: str | None) -> bool:
    """``True`` для ФС, на которых WAL SQLite небезопасен."""
    if not fstype:
        return False
    name = fstype.strip().lower()
    if name in _NETWORK_FS_MARKERS:
        return True
    if name.startswith(_FUSE_PREFIX) and name not in _FUSE_LOCAL_OK:
        return True
    return False


def _describe_mode_source() -> str:
    """Сказать человеческим языком, ОТКУДА взялся текущий режим."""
    from hermes_constants import (
        PROFILE_LAYOUT_MARKER,
        PROFILE_LAYOUT_VERSION,
        _read_layout_marker,
        get_profile_root,
    )

    if os.environ.get("HERMES_ISOLATION", "").strip():
        return "переменная HERMES_ISOLATION (или ключ isolation.mode)"
    root = str(get_profile_root())
    if _read_layout_marker(root) == PROFILE_LAYOUT_VERSION:
        return f"маркер {PROFILE_LAYOUT_MARKER} в корне профиля"
    return "умолчание (ни переменной, ни маркера)"


def check_layout_containment(issues: list, should_fix: bool = False) -> int:
    """Проверить раскладку профиля. Возвращает число починенного (всегда 0).

    Ничего не чинит намеренно: перенос данных — работа явной команды
    миграции, а не побочный эффект `doctor --fix`. Доктор здесь только
    называет состояние.
    """
    from hermes_cli.doctor import _section, check_fail, check_info, check_ok, check_warn
    from hermes_constants import (
        get_isolation_mode,
        get_profile_home_dir,
        get_profile_root,
        get_profile_tmp_dir,
        get_profile_workspace_dir,
        is_contained,
    )

    _section("Profile Layout")

    mode = get_isolation_mode()
    root = get_profile_root()
    check_ok(f"режим изоляции: {mode}", f"({_describe_mode_source()})")
    check_info(f"корень профиля: {root}")

    contained = is_contained()
    branches = {
        "HOME подпроцессов": get_profile_home_dir(),
        "временные файлы": get_profile_tmp_dir(),
        "рабочая папка": get_profile_workspace_dir(),
    }

    if contained:
        outside: list[str] = []
        try:
            real_root = Path(os.path.realpath(root))
        except OSError:
            real_root = Path(root)
        for label, branch in branches.items():
            try:
                inside = Path(os.path.realpath(branch)).is_relative_to(real_root)
            except (OSError, ValueError):
                inside = False
            if inside:
                check_ok(f"{label} внутри корня", f"({branch})")
            else:
                outside.append(f"{label} → {branch}")
                check_warn(f"{label} ВНЕ корня", f"({branch})")
        if outside:
            check_info(
                "это режим custom: так решил оператор явной переменной или "
                "ключом конфига. Если не решал — проверьте юнит шлюза."
            )
            issues.append(
                "Часть данных профиля лежит вне его корня: "
                + "; ".join(outside)
            )
    else:
        check_info(
            "contained выключен — HOME, временные файлы и рабочая папка "
            "общие с хостом, как в апстриме"
        )
        for label, branch in branches.items():
            check_info(f"{label}: {branch}")

    fstype = filesystem_for(root)
    if fstype is None:
        check_info("тип файловой системы под корнем определить не удалось")
    elif is_network_filesystem(fstype):
        detail = (
            f"({fstype}: блокировки этой ФС не дают гарантий, на которые "
            f"рассчитан WAL SQLite — state.db повредится)"
        )
        if contained:
            check_fail("корень профиля на сетевой файловой системе", detail)
            issues.append(
                f"Корень профиля {root} лежит на {fstype}. В режиме contained туда "
                f"уезжает всё состояние профиля, включая state.db — перенесите "
                f"корень на локальный диск."
            )
        else:
            check_warn("корень профиля на сетевой файловой системе", detail)
    else:
        check_ok(f"файловая система под корнем: {fstype}", "(локальная)")

    return 0
