#!/usr/bin/env python3
"""stdio MCP-сервер, который ПИШЕТ, — для живого шага 6 §5.3.

Зачем он есть
=============

Живой шаг 6 §5.3 в заходе RAF-158 прошёл на JSON-RPC echo-сервере и не
доказал ничего: echo отвечает одинаково при любом ``HOME`` и ничего никуда
не пишет. Под этим «зелёным» шагом лежал настоящий дефект — stdio MCP
собирал окружение мимо контракта изоляции (RAF-158, `d00574e69`).

Этот сервер устроен ровно наоборот: он ничего не вычисляет и ничего не
эхо-шит, он **раскладывает файлы по тем путям, которые контракт изоляции
обязан переназначить** — ``$HOME``, ``$TMPDIR`` (через `tempfile`, то есть
так же, как это сделает любой чужой сервер) и все четыре ``XDG_*``. Если
контракт не применён, файлы лягут в дом оператора и `inotifywait` из §5
увидит их в `outside.log`. Если применён — всё окажется внутри корня
профиля.

Зависимостей нет — только стандартная библиотека, чтобы сервер поднимался
на чистом стенде без `pip install` и без сети (иначе OSV-преflight в
`tools/mcp_tool.py` пришлось бы ждать ради ничего).

Как подключить на стенде
========================

    hermes mcp add write-probe --command python3 \
        --args <repo>/scripts/live/mcp_write_probe_server.py

Дальше — `scripts/live/capture_mcp_subprocess_env.sh`, он снимает `ps`,
`/proc/<pid>/environ` и сверяет манифест с корнем профиля.

Что сервер пишет
================

При старте, до первого ответа по протоколу:

* ``$HOME/.mcp-write-probe/startup.json`` — pid, ppid, снимок окружения;
* временный файл через ``tempfile.mkstemp()`` — это и есть проверка
  ``TMPDIR``/``TMP``/``TEMP``;
* ``<каждый XDG_*>/mcp-write-probe/probe.txt``;
* ``$HOME/.mcp-write-probe/manifest.json`` — полный список того, что он
  создал, с абсолютными путями. Манифест читает скрипт снятия.

Инструмент ``write_probe_file`` делает то же самое ещё раз, уже по вызову
из чата (шаг 6 требует один вызов инструмента), и дописывает пути в
манифест. Инструмент ``probe_report`` возвращает pid и окружение, которое
сервер видит у себя, — это перекрёстная сверка к ``/proc/<pid>/environ``,
а не замена ей: доверенное свидетельство снимается снаружи.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SERVER_NAME = "mcp-write-probe"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

# Каталог, который сервер заводит у себя в `$HOME`. Имя намеренно приметное
# и уникальное: по нему скрипт снятия ищет следы в `outside.log`.
PROBE_DIR_NAME = ".mcp-write-probe"
TMP_PREFIX = "mcp-write-probe-"

# Переменные, ради которых всё и затевалось: контракт изоляции обязан
# переназначить каждую из них (`hermes_constants.apply_subprocess_containment_env`).
WATCHED_ENV_VARS = (
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "HERMES_REAL_HOME",
    "HERMES_OSINT_CACHE",
)

_written: list[str] = []


def _log(message: str) -> None:
    """Диагностика уходит в stderr: stdout занят протоколом."""
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def _probe_dir() -> Path:
    return Path(os.path.expanduser("~")) / PROBE_DIR_NAME


def _env_snapshot() -> dict[str, str | None]:
    return {var: os.environ.get(var) for var in WATCHED_ENV_VARS}


def _record(path: Path) -> str:
    resolved = str(path)
    _written.append(resolved)
    return resolved


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return _record(path)


def _write_temp_file(label: str) -> str:
    """Временный файл через `tempfile` — так его сделает и чужой сервер.

    Важно, что путь не собирается руками из ``$TMPDIR``: проверяем ровно то
    поведение, которое будет у настоящего MCP-сервера, а `tempfile` смотрит
    на ``TMPDIR``/``TEMP``/``TMP`` в таком порядке сам.
    """
    fd, name = tempfile.mkstemp(prefix=f"{TMP_PREFIX}{label}-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{SERVER_NAME} {label} pid={os.getpid()} at {time.time():.0f}\n")
    return _record(Path(name))


def _write_xdg_files(label: str) -> dict[str, str]:
    """По файлу в каждый заданный ``XDG_*``.

    Если переменная не выставлена, сервер её не выдумывает: отсутствие —
    это тоже результат, и скрипт снятия отличит «нет переменной» от
    «переменная смотрит наружу».
    """
    result: dict[str, str] = {}
    for var in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        base = os.environ.get(var)
        if not base:
            continue
        target = Path(base) / SERVER_NAME / f"{label}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"{var}={base} pid={os.getpid()} label={label}\n", encoding="utf-8"
        )
        result[var] = _record(target)
    return result


def _write_manifest() -> str:
    """Манифест — единственный файл, который читает скрипт снятия."""
    return _write_json(
        _probe_dir() / "manifest.json",
        {
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "env": _env_snapshot(),
            "cwd": os.getcwd(),
            "written": _written,
            "updated_at": time.time(),
        },
    )


def _do_writes(label: str) -> dict[str, Any]:
    """Один проход записи: `$HOME`, `$TMPDIR`, все `XDG_*`."""
    home_file = _write_json(
        _probe_dir() / f"{label}.json",
        {
            "label": label,
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "env": _env_snapshot(),
            "at": time.time(),
        },
    )
    temp_file = _write_temp_file(label)
    xdg_files = _write_xdg_files(label)
    manifest = _write_manifest()
    return {
        "label": label,
        "pid": os.getpid(),
        "home_file": home_file,
        "temp_file": temp_file,
        "xdg_files": xdg_files,
        "manifest": manifest,
        "env": _env_snapshot(),
    }


# ─── протокол ────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "write_probe_file",
        "description": (
            "Создать файлы в $HOME, $TMPDIR и всех XDG_* каталогах сервера и "
            "вернуть их абсолютные пути. Проба изоляции подпроцессов."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Метка в имени файлов (по умолчанию tool-call).",
                }
            },
            "required": [],
        },
    },
    {
        "name": "probe_report",
        "description": (
            "Вернуть pid сервера и значения HOME/TMPDIR/XDG_*, которые он видит "
            "у себя, вместе со списком уже созданных файлов."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


def _text_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}
        ],
        "isError": False,
    }


def _handle(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        requested = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
        return {
            "protocolVersion": requested,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "write_probe_file":
            label = str(arguments.get("label") or "tool-call")
            # Имя файла идёт в путь — не пускаем туда сепараторы.
            label = label.replace("/", "_").replace("\\", "_").replace("..", "_")[:64]
            return _text_result(_do_writes(label))
        if name == "probe_report":
            return _text_result(
                {
                    "pid": os.getpid(),
                    "ppid": os.getppid(),
                    "env": _env_snapshot(),
                    "written": _written,
                    "manifest": str(_probe_dir() / "manifest.json"),
                }
            )
        raise LookupError(f"unknown tool: {name}")
    raise LookupError(f"unknown method: {method}")


def _serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _log(f"не разобрал строку: {exc}")
            continue

        method = message.get("method", "")
        request_id = message.get("id")
        if request_id is None:
            # Нотификация (`notifications/initialized` и подобные) — ответа нет.
            continue

        try:
            result = _handle(method, message.get("params") or {})
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except LookupError as exc:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": str(exc)},
            }
        except Exception as exc:  # pragma: no cover — дальше в лог, не в падение
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
            }
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def main() -> int:
    # Пишем ДО протокола: даже если клиент отвалится на рукопожатии, следы
    # в файловой системе уже есть и `inotifywait` их увидел.
    startup = _do_writes("startup")
    _log(
        "старт: pid={pid} HOME={home} TMPDIR={tmp} manifest={manifest}".format(
            pid=os.getpid(),
            home=os.environ.get("HOME"),
            tmp=os.environ.get("TMPDIR"),
            manifest=startup["manifest"],
        )
    )
    return _serve()


if __name__ == "__main__":
    sys.exit(main())
