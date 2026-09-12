"""Запуск обновления и чтение его исхода — общая часть для всех дверей.

Обновление продукта на машине клиента запускается из двух мест: команда
`/update` в чате (``gateway/slash_commands.py``) и кнопка «Обновить» в
мастере настройки (``hermes_cli/setup_wizard/update_view.py``). Обе двери
делают одно и то же и обязаны видеть один и тот же исход, поэтому
механика живёт здесь, а не в каждой из них.

Форма механики продиктована тем, что обновление **убивает того, кто его
запустил**: `hermes update` перезапускает шлюз, а на машине клиента — ещё
и сам мастер (оба systemd-юнита живут в одной установке). Поэтому:

* процесс отвязывается (`setsid`), иначе он умрёт вместе с родителем на
  первом же перезапуске и оставит установку наполовину обновлённой;
* исход пишется не в память, а в три файла в ``$HERMES_HOME``, потому что
  спросить о нём придёт уже ДРУГОЙ процесс — тот, что поднялся после
  перезапуска;
* признак «идёт прямо сейчас» — это отсутствие файла с кодом возврата при
  наличии файла вывода, а не запись в памяти.

Имена файлов те же, что у чатовой команды (``.update_output.txt`` и
``.update_exit_code``): один запуск — один исход, независимо от того,
через какую дверь его начали. Файл ``.update_pending.json`` здесь
НЕ трогается: он принадлежит чатовой команде и означает «кому в
мессенджере отправить ответ». Мастер, записав его, заставил бы шлюз
писать в чат о том, чего клиент в чате не просил.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from hermes_constants import get_hermes_home

#: Сколько последних строк вывода отдаём наружу. Полный лог обновления —
#: сотни строк про pip и npm; клиенту он не нужен, а поддержке хватает
#: хвоста, где и живут ошибки.
TAIL_LINES = 40

#: Отличает «не передали» от «передали None» в :func:`build_launcher_argv`.
_MISSING = object()


def output_path() -> Path:
    return get_hermes_home() / ".update_output.txt"


def exit_code_path() -> Path:
    return get_hermes_home() / ".update_exit_code"


def from_version_path() -> Path:
    return get_hermes_home() / ".update_from_version"


def installed_version() -> str | None:
    """Версия, стоящая на машине ПРЯМО СЕЙЧАС, без кэша.

    Кэшированный вариант (``banner.get_latest_release_tag``) держит ответ
    на весь процесс, а нам нужно сравнить «до» и «после» — причём
    спрашивать будет процесс, переживший перезапуск или не переживший
    его. Кэш здесь дал бы ложное «ничего не изменилось».
    """
    try:
        from hermes_cli.config import get_project_root
        from hermes_cli.release_source import resolve_local_release_tag

        root = get_project_root()
        return resolve_local_release_tag(str(root)) if root else None
    except Exception:
        return None


def resolve_hermes_command() -> list | None:
    """Argv, которым запускается продукт: `hermes`, иначе модуль в venv.

    Повторяет порядок ``gateway/run.py::_resolve_hermes_bin`` — на машине
    клиента срабатывает первый вариант (`/usr/local/bin/hermes`), второй
    нужен там, где шима на PATH нет.
    """
    found = shutil.which("hermes")
    if found:
        return [found]
    try:
        import importlib.util

        if importlib.util.find_spec("hermes_cli") is not None:
            return [sys.executable, "-m", "hermes_cli.main"]
    except Exception:
        pass
    return None


@dataclass(frozen=True)
class UpdateState:
    """Состояние обновления, каким его видит пришедший спросить процесс."""

    #: idle — никто не запускал (или исход уже забрали);
    #: running — запущено и ещё не кончилось;
    #: done — кончилось успешно; failed — кончилось с ошибкой.
    state: str
    exit_code: int | None = None
    tail: str = ""
    #: True — версия сменилась; False — была уже последняя; None — не знаем
    #: (обновление ещё идёт или версию не удалось определить).
    changed: bool | None = None

    @property
    def finished(self) -> bool:
        return self.state in ("done", "failed")


def read_update_state() -> UpdateState:
    """Прочитать исход по файлам. Безопасно вызывать когда угодно."""
    out, code = output_path(), exit_code_path()
    if not out.exists():
        return UpdateState("idle")

    tail = ""
    try:
        lines = out.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-TAIL_LINES:])
    except OSError:
        pass

    if not code.exists():
        return UpdateState("running", tail=tail)

    raw = ""
    try:
        raw = code.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass
    try:
        rc = int(raw)
    except ValueError:
        # Файл есть, а числа в нём нет: процесс успел создать его и умер
        # раньше записи. Это не успех — честнее показать неудачу, чем
        # соврать «готово».
        return UpdateState("failed", exit_code=None, tail=tail)

    changed = None
    before = ""
    try:
        before = from_version_path().read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if before:
        now = installed_version() or ""
        # Пустая «текущая» — это «не смогли спросить», а не «не менялась»:
        # соврать «у вас и так последняя» хуже, чем промолчать.
        changed = (now != before) if now else None

    return UpdateState(
        "done" if rc == 0 else "failed", exit_code=rc, tail=tail, changed=changed
    )


def is_running() -> bool:
    return read_update_state().state == "running"


class UpdateAlreadyRunning(RuntimeError):
    """Обновление уже идёт — второе запускать нельзя.

    Два `hermes update` на одном рабочем дереве дерутся за git-индекс и
    оставляют установку в состоянии, из которого клиент сам не выйдет.
    """


class HermesCommandNotFound(RuntimeError):
    """Нечем запустить обновление — не найден ни шим, ни модуль."""


def start_detached_update(extra_args: list | None = None) -> list:
    """Запустить обновление отвязанным процессом. Вернуть его argv.

    Не ждёт завершения и ничего не печатает: результат забирается позже
    через :func:`read_update_state`, возможно — уже другим процессом.
    """
    if is_running():
        raise UpdateAlreadyRunning

    cmd = resolve_hermes_command()
    if not cmd:
        raise HermesCommandNotFound

    argv = [*cmd, "update", *(extra_args or [])]

    out, code = output_path(), exit_code_path()
    # Порядок важен: сначала убираем прошлый код возврата, потом создаём
    # файл вывода. Обратный порядок оставил бы окно, в котором прошлый
    # успех виден рядом с новым запуском, и мастер показал бы «готово»
    # через секунду после нажатия кнопки.
    code.unlink(missing_ok=True)
    out.write_text("", encoding="utf-8")
    # Версию «до» пишем ДО запуска: после него сравнивать будет уже не с
    # чем — обновление перепишет рабочее дерево.
    from_version_path().write_text(installed_version() or "", encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"

    quoted = " ".join(_shquote(part) for part in argv)
    script = (
        f"{quoted} > {_shquote(str(out))} 2>&1; "
        # Не `status=$?`: в zsh `status` — read-only. Строка переживает
        # копирование в оболочку, где это важно.
        f"rc=$?; printf '%s' \"$rc\" > {_shquote(str(code))}"
    )

    launcher = build_launcher_argv(script)
    subprocess.Popen(
        launcher,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
        cwd=_update_cwd(),
    )
    return argv


def build_launcher_argv(script: str, *, systemd_run: str | None = _MISSING,
                        setsid: str | None = _MISSING) -> list:
    """Как именно запускать обновление, чтобы оно пережило перезапуск.

    Отвязать процесс через ``setsid`` НЕДОСТАТОЧНО. Измерено на живой
    машине 2026-09-09: отвязанный потомок пользовательской службы
    убивается вместе с ней, потому что systemd по умолчанию
    (``KillMode=control-group``) шлёт сигнал всей группе, а не одному
    главному процессу. А обновление последним делом перезапускает как
    раз ту службу, из которой его и запустили, — мастер настройки.
    Получалось бы так: обновление доходит до конца, само себя убивает и
    не успевает записать код возврата, а страница показывает «Обновляю…»
    вечно.

    Поэтому предпочитаем ``systemd-run --user``: он отдаёт команду
    менеджеру служб, тот заводит для неё ОТДЕЛЬНУЮ временную службу, и
    перезапуск мастера её не касается. ``--collect`` убирает её за собой,
    ``KillMode=process`` — чтобы и её собственная остановка не била по
    потомкам.

    ``setsid`` остаётся запасным путём: он неверен ровно в одном случае
    (перезапуск своей же службы), но лучше, чем ничего, там, где
    пользовательской шины нет вовсе.
    """
    if systemd_run is _MISSING:
        systemd_run = shutil.which("systemd-run")
    if setsid is _MISSING:
        setsid = shutil.which("setsid")

    if systemd_run:
        return [
            systemd_run, "--user", "--collect", "--quiet",
            f"--unit=trix-update-{os.getpid()}-{int(time.time())}",
            "--property=KillMode=process",
            "bash", "-c", script,
        ]
    if setsid:
        return [setsid, "bash", "-c", script]
    return ["bash", "-c", script]


def _update_cwd() -> str | None:
    """Каталог, из которого запускается обновление.

    Задаётся ЯВНО, а не наследуется от того, кто нажал кнопку. Иначе
    обновление зависит от случайного места запуска родителя, и часть
    шагов молча ломается: наблюдалось 2026-09-09 на стенде, где процесс
    унаследовал ``/root`` (права 0700, чужой пользователь) — и обновление
    зависимостей упало на попытке прочитать ``/root/uv.toml``, оставив
    распознавание речи на прежней версии с бодрым «✓ Code updated!» в
    конце. Установочное дерево — единственный каталог, который для
    обновления заведомо и читаем, и осмыслен: там же лежит git, ради
    которого всё и затевается.
    """
    try:
        from hermes_cli.config import get_project_root

        root = get_project_root()
        if root and Path(root).is_dir():
            return str(root)
    except Exception:
        pass
    return None


def _shquote(part: str) -> str:
    import shlex

    return shlex.quote(part)


def clear_update_state() -> None:
    """Забыть прошлый исход, чтобы следующий опрос сказал «idle».

    Нужно ровно там, где исход уже показан человеку: иначе мастер будет
    показывать позапрошлое обновление при каждом заходе на страницу.
    """
    output_path().unlink(missing_ok=True)
    exit_code_path().unlink(missing_ok=True)
    from_version_path().unlink(missing_ok=True)
