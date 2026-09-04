"""Camofox поднимается службой, а не советом в консоли.

Живёт здесь, а не в `tools_config.py`, по той же причине, что
`trix_support.py` и `trix_setup_service_check.py`: наша логика — в нашем
модуле, а в апстримный файл уходит один вызов. `tools_config.py` мы
регулярно тянем сверху, и каждая наша функция внутри него оплачивается
конфликтом при каждом мёрже.

**Зачем это вообще.** Установочный шаг Camofox ставил npm-пакет и
ПЕЧАТАЛ инструкцию: «запустите `npx @askjo/camofox-browser`». Инструкция
адресована человеку с консолью — а у клиента её нет по устройству
продукта: ни SSH, ни терминала, единственная дверь это мастер настройки.
Проверено исполнением на клиентской машине 2026-09-04: после выбора
Camofox в мастере пакет не появился, порт 9377 никто не слушал, и
браузерные инструменты в этом режиме были обречены на сетевую ошибку при
первом же вызове. То есть вариант, который мастер предлагает наравне с
остальными, довести до рабочего состояния было нельзя в принципе.

**Почему именно пользовательская служба.** Ровно так на этой машине уже
живут шлюз и сам мастер: `systemd --user` + `linger`, переживает
перезагрузку, перезапускается при падении. Второго механизма заводить
незачем, а «запустить в фоне и забыть» не переживает ни ребута, ни
падения.

Сервер запускается ТОЛЬКО когда клиент выбрал Camofox. Он не бесплатный:
первый старт тянет движок Camoufox (~300 МБ) и держит память — по
`deployment-requirements.md` он заменяет Chromium, а не добавляется к
нему.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

SERVICE_NAME = "trix-camofox.service"
DEFAULT_PORT = 9377


@dataclass(frozen=True)
class CamofoxServiceResult:
    """Что вышло из попытки поднять службу."""

    ok: bool
    message: str


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME


def _server_binary(project_root: Path) -> Path | None:
    """Бинарь из установленного пакета, а НЕ `npx`.

    `npx @askjo/camofox-browser` — то, что печаталось в инструкции, и для
    юнита это плохой выбор: он идёт в сеть при каждом старте и молча
    падает, когда сети нет. Пакет к этому моменту уже установлен рядом,
    так что запускаем ровно его.

    Имя бинаря снято с живой машины (пакет 1.14.0 объявляет
    `bin: {"camofox-browser": ...}`), а не угадано.
    """
    candidate = project_root / "node_modules" / ".bin" / "camofox-browser"
    return candidate if candidate.exists() else None


def unit_file_text(binary: Path, node_dir: Path | None, port: int = DEFAULT_PORT) -> str:
    """Текст юнита.

    `node_dir` подмешивается в PATH, потому что бинарь — это js-скрипт с
    шебангом на `node`, и у пользовательской службы PATH куда беднее, чем
    у интерактивной сессии. Это тот же класс, из-за которого юнит шлюза
    когда-то «плавал» по тому, какой node подвернулся, — здесь путь
    называется явно.
    """
    path_line = ""
    if node_dir is not None:
        path_line = f'Environment="PATH={node_dir}:/usr/local/bin:/usr/bin:/bin"\n'
    return f"""[Unit]
Description=Trix Agent Camofox browser server
After=network-online.target

[Service]
Type=simple
Environment="CAMOFOX_PORT={port}"
{path_line}ExecStart={binary}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _systemctl_user(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _port_answers(port: int, deadline_s: float) -> bool:
    """Ждём, пока сервер действительно ответит.

    Проверяем не «служба запустилась», а «сервер отвечает»: `active` у
    `Type=simple` означает лишь то, что процесс стартовал, а Camofox ещё
    поднимает виртуальный дисплей и прогревает браузер — на живой машине
    это заняло около полутора секунд после старта, но на первом запуске
    туда добавляется загрузка движка.
    """
    import socket

    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(2)
    return False


def ensure_camofox_service(
    project_root: Path,
    port: int = DEFAULT_PORT,
    wait_s: float = 60.0,
) -> CamofoxServiceResult:
    """Поставить и запустить службу Camofox; вернуть честный итог.

    Никогда не бросает: это установочный шаг мастера, и его провал не
    должен ронять настройку целиком — клиент увидит предупреждение и
    сохранённые настройки, а не пустой экран.
    """
    try:
        binary = _server_binary(project_root)
        if binary is None:
            return CamofoxServiceResult(
                ok=False,
                message="Пакет Camofox не установлен — запускать нечего.",
            )

        node_dir: Path | None = None
        try:
            from hermes_cli.tools_config import find_node_executable

            node_bin = find_node_executable("node")
            if node_bin:
                node_dir = Path(node_bin).parent
        except Exception:
            node_dir = None

        path = unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unit_file_text(binary, node_dir, port), encoding="utf-8")

        _systemctl_user("daemon-reload")
        result = _systemctl_user("enable", "--now", SERVICE_NAME, timeout=60.0)
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            return CamofoxServiceResult(
                ok=False,
                message=(
                    "Служба Camofox записана, но systemd её не запустил: "
                    + (detail[0] if detail else "причина неизвестна")
                ),
            )

        if _port_answers(port, wait_s):
            return CamofoxServiceResult(
                ok=True,
                message=f"Camofox запущен службой и отвечает на порту {port}.",
            )
        return CamofoxServiceResult(
            ok=False,
            message=(
                f"Служба Camofox запущена, но порт {port} не ответил за "
                f"{int(wait_s)} с. Первый старт тянет движок Camoufox "
                "(~300 МБ) — возможно, он ещё качается."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — см. докстринг
        return CamofoxServiceResult(
            ok=False, message=f"Не удалось поднять службу Camofox: {exc}"
        )
