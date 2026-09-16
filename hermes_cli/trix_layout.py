"""Переход профиля в ``contained`` — разведка, маркер, откат.

Что здесь на самом деле мигрирует. Переход ``shared-host → contained``
для существующего корня **не перемещает данные**: состояние профиля и так
лежит под корнем, кэши перестраиваются сами, ``/tmp`` не переносится по
определению, а чужие логины из настоящего дома копировать нельзя (молчаливый
перенос учётных данных — отдельный класс аварий: оператор перестаёт понимать,
чьим ключом ходит агент).

Поэтому миграция — это ровно три вещи:

1. записать маркер ``.layout-version``;
2. создать ветви ``home/``, ``tmp/``, ``workspace/`` (``0700``), если их нет;
3. **сказать вслух**, что изменится для этого профиля.

Третий пункт — главный. После переключения профиль перестанет видеть логины,
которые лежат в настоящем доме ОС: ``~/.claude``, ``~/.codex``,
``~/.config/github-copilot`` и соседей. Файлы никуда не денутся — их просто
будут искать в другом месте, и агенту придётся авторизоваться заново. Узнать
об этом полагается ДО переключения, а не по молчащему боту после.

Отсюда контракт флагов, и он ровно такой, как написано в справке:

* без флагов и с ``--dry-run`` — только отчёт, диск не трогается. Флаг
  существует, чтобы намерение «посмотреть» можно было записать в рецепт
  словом, а не умолчанием;
* ``--apply`` — явное намерение. Полный отчёт печатается ПЕРЕД первой
  записью, отказ preflight (симлинк в ветви, работающий шлюз) оставляет
  корень нетронутым;
* ``--dry-run`` вместе с ``--apply`` или ``--rollback`` — отказ: два
  взаимоисключающих намерения нельзя разрешить угадыванием.

Чего здесь сознательно НЕТ: вопроса в терминал. Команда живёт в рецептах и
в чужой автоматизации, и интерактив сломал бы их молча. Прежний докстринг
обещал, что ``--apply`` требует пройденного ``--dry-run``; этого не было
никогда, а «помнить» между запусками команде негде и незачем.

Обратная сторона симметрична и поэтому проста: ``--rollback`` удаляет маркер
и только те каталоги, которые создал сам и которые остались пустыми. Никакой
«обратной миграции данных» не существует, потому что не было и прямой. Это и
есть аварийный выход, и он работает всегда.

Разведка (:func:`discover`) читает только ``exists()``/``is_dir()`` и НИКОГДА
не открывает найденные файлы: в них учётные данные, и у уборщика нет причин
знать их содержимое.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from hermes_constants import (
    ISOLATION_CONTAINED,
    MARKER_UNREADABLE,
    PROFILE_LAYOUT_MARKER,
    PROFILE_LAYOUT_VERSION,
    get_isolation_mode,
    get_real_home,
    read_layout_marker,
    write_layout_marker,
)

# Журнал миграции: что именно мы создали и когда. Откат читает его и не
# трогает ничего, чего не создавал.
MIGRATION_JOURNAL = ".layout-migration.json"

# Ветви профиля, которые обязаны существовать в contained. Значение —
# зачем ветвь нужна; текст идёт в отчёт dry-run, чтобы оператор читал
# объяснение, а не список путей.
_BRANCHES: tuple[tuple[str, str], ...] = (
    ("home", "HOME подпроцессов: внешние CLI, браузер, MCP"),
    ("tmp", "TMPDIR профиля вместо общего /tmp"),
    ("workspace", "рабочая папка — дефолт cwd терминала"),
)

# Приватные данные профиля, которые внешние инструменты исторически держат в
# доме ОС. После переключения профиль будет искать их под корнем — то есть
# перестанет видеть эти. Список согласован с переводом сайтов на
# ``user_home_path()`` (фаза 3); менять его надо ВМЕСТЕ с ними, иначе отчёт
# начнёт врать.
_PRIVATE_HOME_SITES: tuple[tuple[str, str], ...] = (
    (".claude/.credentials.json", "вход в Claude Code"),
    (".codex/auth.json", "вход в Codex CLI"),
    (".config/github-copilot/hosts.json", "вход в GitHub Copilot"),
    (".minimax/credentials.json", "вход в MiniMax"),
    (".qwen/oauth_creds.json", "вход в Qwen"),
    (".honcho/config.json", "настройки памяти Honcho"),
    (".hindsight/config.json", "настройки памяти Hindsight"),
    (".modal.toml", "ключ песочницы Modal"),
    (".cua-driver", "драйвер песочницы CUA"),
)


@dataclass
class BranchState:
    """Состояние одной ветви профиля."""

    name: str
    purpose: str
    path: Path
    exists: bool
    is_dir: bool
    nonempty: bool
    # Ветвь — симлинк. Отдельное поле, потому что «ссылка на месте» и
    # «каталог на месте» — разные факты: по ссылке наружу изоляция
    # отменяется целиком, а отчёт до этой работы называл её «есть, пустая».
    is_symlink: bool = False
    # Куда ведёт ссылка (как записано, без разворачивания). Нужен отчёту:
    # оператор должен увидеть путь, а не узнать, что «что-то не так».
    link_target: str | None = None


@dataclass
class LayoutReport:
    """Что разведка нашла в корне. Только факты, без решений."""

    root: Path
    mode: str
    marker_version: int | None
    branches: list[BranchState] = field(default_factory=list)
    external_tmpdir: str | None = None
    real_home: Path | None = None
    # Приватные сайты в настоящем доме: (относительный путь, зачем он нужен)
    losing_sites: list[tuple[str, str]] = field(default_factory=list)
    journal_exists: bool = False

    @property
    def already_contained(self) -> bool:
        """Совпадает с решением рантайма, включая испорченный маркер."""
        if self.marker_version is None:
            return False
        return (
            self.marker_version == MARKER_UNREADABLE
            or self.marker_version >= PROFILE_LAYOUT_VERSION
        )

    def missing_branches(self) -> list[BranchState]:
        return [b for b in self.branches if not b.exists]

    def symlinked_branches(self) -> list[BranchState]:
        """Ветви, подменённые симлинком, — блокер переключения."""
        return [b for b in self.branches if b.is_symlink]


def _is_nonempty_dir(path: Path) -> bool:
    """``True``, если это каталог и в нём есть хоть одна запись.

    Симлинки не разворачиваем: нас интересует то, что лежит в корне, а не
    то, куда из корня ведёт ссылка.
    """
    try:
        if not path.is_dir() or path.is_symlink():
            return False
        with os.scandir(path) as entries:
            for _ in entries:
                return True
    except OSError:
        return False
    return False


def discover(
    root: str | os.PathLike,
    *,
    env: dict[str, str] | None = None,
    home: str | os.PathLike | None = None,
) -> LayoutReport:
    """Собрать состояние корня: режим, ветви, что профиль перестанет видеть.

    Ничего не пишет и не открывает файлы — только проверяет существование.
    ``home`` позволяет тесту подставить свой «настоящий дом» вместо дома ОС.

    Весь отчёт — про ``root``, включая строку режима. Раньше маркер читался
    у переданного корня, а режим спрашивался у ``get_isolation_mode()`` без
    ``env``, то есть у корня ТЕКУЩЕГО ПРОЦЕССА. На запросе про чужой профиль
    (``hermes profile migrate-layout <имя>``) отчёт противоречил сам себе:
    «Режим сейчас: shared-host» и тут же «Маркер: .layout-version = 2».
    Поэтому режим спрашивается у среды, в которой корнем профиля объявлен
    именно ``root``. Приоритет явного ``HERMES_ISOLATION`` это не трогает:
    слово оператора действует на весь процесс, а значит и на этот профиль.
    """
    root_path = Path(root)
    env_map = dict(env) if env is not None else None
    # Среда для вопроса о режиме: та же, но корень профиля — спрошенный.
    mode_env = {
        **(env_map if env_map is not None else os.environ),
        "HERMES_HOME": str(root_path),
    }

    # Один разбор маркера на весь движок: инструмент оператора обязан
    # читать файл теми же правилами, что и рантайм.
    marker_version = read_layout_marker(root_path)

    branches: list[BranchState] = []
    for name, purpose in _BRANCHES:
        path = root_path / name
        is_symlink = path.is_symlink()
        link_target: str | None = None
        if is_symlink:
            try:
                link_target = os.readlink(path)
            except OSError:
                link_target = None
        exists = path.exists() or is_symlink
        branches.append(
            BranchState(
                name=name,
                purpose=purpose,
                path=path,
                exists=exists,
                # Симлинк — не каталог, даже если ведёт в каталог: ветвь
                # профиля обязана БЫТЬ каталогом в корне.
                is_dir=path.is_dir() and not is_symlink,
                nonempty=_is_nonempty_dir(path),
                is_symlink=is_symlink,
                link_target=link_target,
            )
        )

    if home is not None:
        real_home = Path(home)
    else:
        try:
            real_home = Path(get_real_home(env_map))
        except Exception:
            real_home = None

    losing: list[tuple[str, str]] = []
    if real_home is not None:
        for rel, why in _PRIVATE_HOME_SITES:
            candidate = real_home / rel
            try:
                if candidate.exists():
                    losing.append((rel, why))
            except OSError:
                continue

    source = env_map if env_map is not None else os.environ
    external_tmpdir = str(source.get("TMPDIR", "") or "").strip() or None

    return LayoutReport(
        root=root_path,
        mode=get_isolation_mode(mode_env),
        marker_version=marker_version,
        branches=branches,
        external_tmpdir=external_tmpdir,
        real_home=real_home,
        losing_sites=losing,
        journal_exists=(root_path / MIGRATION_JOURNAL).exists(),
    )


def render_report(report: LayoutReport, *, applied: bool = False) -> str:
    """Человеческий отчёт разведки. Русский, потому что читает его оператор."""
    lines: list[str] = []
    lines.append(f"Корень профиля: {report.root}")
    lines.append(f"Режим сейчас:   {report.mode}")
    if report.marker_version is None:
        lines.append(f"Маркер:         нет ({PROFILE_LAYOUT_MARKER} отсутствует)")
    elif report.marker_version == MARKER_UNREADABLE:
        lines.append(
            f"Маркер:         {PROFILE_LAYOUT_MARKER} есть, но не читается — "
            f"профиль считается {ISOLATION_CONTAINED}"
        )
    else:
        lines.append(f"Маркер:         {PROFILE_LAYOUT_MARKER} = {report.marker_version}")

    lines.append("")
    lines.append("Ветви профиля:")
    for branch in report.branches:
        if branch.is_symlink:
            mark = "СИМЛИНК"
        elif not branch.exists:
            mark = "будет создана" if not applied else "создана"
        elif not branch.is_dir:
            mark = "есть, НЕ каталог"
        elif branch.nonempty:
            mark = "есть, непустая"
        else:
            mark = "есть, пустая"
        lines.append(f"  {branch.name + '/':<12} {mark:<16} — {branch.purpose}")

    symlinked = report.symlinked_branches()
    if symlinked:
        lines.append("")
        lines.append("СТОП. Ветвь корня подменена симлинком:")
        for branch in symlinked:
            target = branch.link_target or "(цель не читается)"
            lines.append(f"  {branch.name}/ -> {target}")
        lines.append("")
        lines.append("Изоляция по такой ветви отменяется целиком: всё, что")
        lines.append("профиль считает своим, уедет по ссылке. Переключение")
        lines.append("отклонено — замените ссылку настоящим каталогом.")

    if report.external_tmpdir:
        lines.append("")
        lines.append(
            f"TMPDIR снаружи: {report.external_tmpdir} — в contained его заменит "
            f"{report.root / 'tmp'}"
        )

    if report.losing_sites:
        lines.append("")
        lines.append("ВАЖНО. После переключения профиль перестанет видеть эти входы")
        lines.append("в домашнем каталоге ОС — файлы останутся на месте, но искать")
        lines.append("их будут под корнем профиля, и авторизоваться придётся заново:")
        for rel, why in report.losing_sites:
            lines.append(f"  ~/{rel:<38} — {why}")
        lines.append("")
        lines.append("Скопировать их автоматически мы отказываемся сознательно:")
        lines.append("молчаливый перенос чужих учётных данных делает непонятным,")
        lines.append("чьим ключом ходит агент. Перенесите нужное руками, если надо.")
    else:
        lines.append("")
        lines.append("Входов провайдеров в домашнем каталоге ОС не найдено —")
        lines.append("терять при переключении нечего.")

    return "\n".join(lines)


def _readlink_or_none(path: Path) -> str | None:
    """Куда ведёт ссылка, как записано. ``None``, если прочитать нельзя."""
    try:
        return os.readlink(path)
    except OSError:
        return None


def _gateway_running(root: Path) -> int | None:
    """Вернуть pid работающего шлюза этого профиля, или ``None``.

    Переключать раскладку под работающим шлюзом нельзя: он уже держит
    открытыми ``state.db`` и временные файлы по старым путям, и половина
    процесса будет жить в одной раскладке, половина — в другой.
    """
    pid_file = root / "gateway.pid"
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        pid = int(raw.split()[0])
    except (ValueError, IndexError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid  # существует, но чужой — всё равно работает
    except OSError:
        return None
    return pid


def apply_migration(
    root: str | os.PathLike,
    *,
    now: float | None = None,
) -> dict:
    """Записать маркер и создать недостающие ветви. Вернуть журнал.

    Идемпотентна: повторный вызов на уже переключённом корне ничего не
    создаёт и журнал не переписывает — иначе откат забыл бы, что каталоги
    были созданы первым прогоном.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"Корень профиля не найден: {root_path}")

    pid = _gateway_running(root_path)
    if pid is not None:
        raise RuntimeError(
            f"Шлюз профиля работает (pid {pid}). Остановите его перед сменой "
            f"раскладки: смена под работающим процессом оставит его половину "
            f"в старой раскладке."
        )

    # Fail-closed по симлинкам — ДО любой записи. Ветвь-симлинк означает,
    # что всё приватное уедет по ссылке, а корень профиля останется пустой
    # витриной. Проверяем до маркера, чтобы отказ не оставил корень
    # наполовину переключённым.
    symlinked = [
        (name, _readlink_or_none(root_path / name))
        for name, _purpose in _BRANCHES
        if (root_path / name).is_symlink()
    ]
    if symlinked:
        details = ", ".join(
            f"{name}/ -> {target or '(цель не читается)'}" for name, target in symlinked
        )
        raise RuntimeError(
            f"Ветвь корня подменена симлинком: {details}. Изоляция по такой "
            f"ветви отменяется целиком — всё, что профиль считает своим, "
            f"уедет по ссылке. Замените ссылку настоящим каталогом и "
            f"повторите."
        )

    journal_path = root_path / MIGRATION_JOURNAL
    existing: dict = {}
    if journal_path.exists():
        try:
            existing = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}

    created: list[str] = list(existing.get("created_dirs") or [])
    for name, _purpose in _BRANCHES:
        path = root_path / name
        if path.exists():
            continue
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if name not in created:
            created.append(name)

    marker_path = root_path / PROFILE_LAYOUT_MARKER
    marker_existed = marker_path.exists()
    # Атомарно: оборванная запись маркера раньше оставляла пустой файл, а
    # пустой маркер возвращал профиль в общий дом ОС.
    write_layout_marker(root_path)

    journal = {
        "version": PROFILE_LAYOUT_VERSION,
        "applied_at": now if now is not None else time.time(),
        "created_dirs": created,
        # Если маркер уже лежал до нас, откат его не удаляет: значит корень
        # был переключён не этой командой, и решение принимали не мы.
        "created_marker": bool(existing.get("created_marker", not marker_existed)),
    }
    journal_path.write_text(
        json.dumps(journal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        os.chmod(journal_path, 0o600)
    except OSError:
        pass
    return journal


def rollback_migration(root: str | os.PathLike) -> dict:
    """Снять маркер и удалить созданные нами ПУСТЫЕ каталоги.

    Непустую ветвь не трогаем никогда: в ``home/`` к этому моменту уже могут
    лежать настоящие логины профиля, и «откат раскладки» не повод их стереть.
    Возвращает отчёт о сделанном.
    """
    root_path = Path(root)
    journal_path = root_path / MIGRATION_JOURNAL
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        journal = {}

    pid = _gateway_running(root_path)
    if pid is not None:
        raise RuntimeError(
            f"Шлюз профиля работает (pid {pid}). Остановите его перед откатом "
            f"раскладки."
        )

    removed: list[str] = []
    kept: list[str] = []
    for name in journal.get("created_dirs") or []:
        # Имя из журнала — не путь: не позволяем журналу увести удаление за
        # пределы корня, даже если его подменили.
        if name not in {branch for branch, _ in _BRANCHES}:
            continue
        path = root_path / name
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            path.rmdir()
            removed.append(name)
        except OSError:
            kept.append(name)  # непустая — так и должно быть

    marker_removed = False
    if journal.get("created_marker", True):
        try:
            (root_path / PROFILE_LAYOUT_MARKER).unlink()
            marker_removed = True
        except OSError:
            marker_removed = False

    journal_path.unlink(missing_ok=True)
    return {
        "removed_dirs": removed,
        "kept_nonempty": kept,
        "marker_removed": marker_removed,
    }


def cmd_migrate_layout(args) -> int:
    """``hermes profile migrate-layout`` — разведка, переключение, откат.

    Без флагов печатает отчёт и ничего не делает: у команды, которая меняет
    место хранения логинов, «сделать» не может быть поведением по умолчанию.
    """
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name

    name = getattr(args, "profile_name", None)
    if name:
        root = get_profile_dir(normalize_profile_name(name))
    else:
        from hermes_constants import get_profile_root

        root = get_profile_root()

    if not Path(root).is_dir():
        print(f"Ошибка: корень профиля не найден: {root}")
        return 1

    do_apply = bool(getattr(args, "apply", False))
    do_rollback = bool(getattr(args, "rollback", False))
    do_dry_run = bool(getattr(args, "dry_run", False))
    if do_apply and do_rollback:
        print("Ошибка: --apply и --rollback вместе не имеют смысла.")
        return 1
    # `--dry-run` был объявлен в парсере и не читался нигде. Флаг, который
    # ничего не делает, хуже отсутствующего: он обещает. Теперь он и есть
    # явная форма поведения по умолчанию — и потому несовместим с обоими
    # флагами, которые пишут на диск.
    if do_dry_run and (do_apply or do_rollback):
        other = "--apply" if do_apply else "--rollback"
        print(
            f"Ошибка: --dry-run и {other} вместе не имеют смысла: первый "
            f"обещает ничего не менять, второй меняет."
        )
        return 1

    if do_rollback:
        try:
            result = rollback_migration(root)
        except RuntimeError as exc:
            print(f"Ошибка: {exc}")
            return 1
        print(f"Корень профиля: {root}")
        if result["marker_removed"]:
            print(f"✓ Маркер {PROFILE_LAYOUT_MARKER} снят — профиль снова shared-host.")
        else:
            print(
                f"  Маркер {PROFILE_LAYOUT_MARKER} оставлен: его ставили не этой "
                f"командой."
            )
        for branch in result["removed_dirs"]:
            print(f"✓ Удалён пустой каталог {branch}/")
        for branch in result["kept_nonempty"]:
            print(f"  Каталог {branch}/ оставлен — он не пуст, данные не трогаем.")
        return 0

    report = discover(root)

    if not do_apply:
        print(render_report(report))
        print()
        if report.already_contained:
            print("Профиль уже переключён. Повторный --apply ничего не изменит.")
        else:
            print("Это разведка, на диске ничего не изменено.")
            print("Переключить:  hermes profile migrate-layout --apply")
        print("Вернуть назад: hermes profile migrate-layout --rollback")
        return 0

    # Preflight ПЕРЕД записью, а не после неё. Раньше «профиль перестанет
    # видеть эти входы» печаталось уже с маркером на диске: в выводе строки
    # стояли правильно, а на диске — нет. Предупреждение после события не
    # предупреждение.
    print(render_report(report))
    print()

    try:
        apply_migration(root)
    except (RuntimeError, FileNotFoundError, OSError) as exc:
        print(f"Ошибка: {exc}")
        print("На диске ничего не изменено.")
        return 1

    after = discover(root)
    for branch in after.branches:
        if branch.name in {b.name for b in report.missing_branches()}:
            print(f"✓ Создан каталог {branch.name}/")
    print(f"✓ Профиль переключён в {ISOLATION_CONTAINED}.")
    print("Вернуть назад: hermes profile migrate-layout --rollback")
    return 0
