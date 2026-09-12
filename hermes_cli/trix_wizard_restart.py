"""После обновления мастер настройки обязан начать работать по новому коду.

`hermes update` перезапускает шлюз — и только его. Мастер настройки живёт
в собственной службе `trix-setup.service`, которая по замыслу (спека 8)
остаётся открытой навсегда: клиенту, у которого через полгода отвалится
прокси, нужен тот же адрес и те же учётные данные. Из-за этого служба
держит в памяти код той версии, при которой её запустили.

Замерено на живой машине 2026-09-05: мастер стартовал в 01:58 при
установке, код обновлялся в 02:39 и дважды позже — **три обновления
подряд до мастера не дошли**. Видно это было по тому, что живая проверка
ключа провайдера, добавленная в одном из них, продолжала молчать: ответ
мастера нёс `key_checked: false`, хотя вызванная руками проверка на той
же машине отвечала `200`. После перезапуска службы — `key_checked: true`.

То есть любая правка в мастере не доезжала до клиента вообще никогда:
шлюз обновлялся, мастер — нет.

Перезапуск делается ТОЛЬКО когда служба уже работает: поднимать её тем,
кто её сознательно закрыл (`hermes setup-wizard close`), мы не имеем
права.
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

_UNIT_NAME = "trix-setup.service"
_TIMEOUT = 30


def _systemctl(verb: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["systemctl", "--user", verb, _UNIT_NAME],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def wizard_is_running() -> bool:
    """Работает ли служба мастера прямо сейчас.

    Пустой stdout — это «не смогли спросить» (systemctl не достучался до
    пользовательской шины), а не «выключено»; в обоих случаях трогать
    нечего, поэтому обе ветки дают False.
    """
    result = _systemctl("is-active")
    if result is None:
        return False
    return result.stdout.strip() == "active"


def restart_wizard_after_update() -> list[str]:
    """Перезапустить мастера, если он работает. Вернуть строки отчёта.

    Пусто, когда мастер не запущен: на машине без него отчёт об
    обновлении не должен обрастать строчками про службу, которой нет.

    Никогда не бросает: обновление уже прошло, и неудача перезапуска не
    имеет права выглядеть его провалом.
    """
    try:
        if not wizard_is_running():
            return []
        result = _systemctl("restart")
    except Exception:  # noqa: BLE001 — см. докстринг
        logger.debug("Перезапуск мастера настройки не отработал", exc_info=True)
        return []

    if result is not None and result.returncode == 0:
        return ["  ✓ Мастер настройки перезапущен — он тоже работает по новому коду."]
    return [
        "  ⚠ Мастер настройки не удалось перезапустить — он продолжит работать "
        "по прежнему коду.",
        "    Настройки от этого не портятся. Починка: "
        "systemctl --user restart trix-setup",
    ]
