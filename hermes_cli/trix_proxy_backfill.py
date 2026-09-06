"""Досев прокси-переменных в ``.env`` уже установленной машины.

Спека 17, Ruling 10. Мастер настройки с этой же спеки пишет прокси во
все восемь имён сразу, но клиент, настроивший прокси РАНЬШЕ, остался с
одним ``HTTPS_PROXY``. Его песочница ходит через прокси по https и
напрямую по http — молча, потому что видимая половина работает.

Досев дописывает недостающие имена тем же адресом. Никогда не
перезаписывает то, что клиент уже задал: единственная операция —
«ключа нет, значит добавить».

Почему отдельный модуль, а не ветка в ``trix_config_sync``: тот владеет
``config.yaml`` и его текстовой структурой (отступы, комментарии,
«клиент удалил — не воскрешаем»). ``.env`` — плоский файл секретов с
другим писателем (``save_env_value``) и другими правилами, и смешивать
их разборы в одном модуле значит связать два несвязанных формата.
"""

from __future__ import annotations

import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

# Откуда копируем -> куда, если пусто. Порядок фиксирован ради
# предсказуемого отчёта; ALL_PROXY намеренно НЕ выводится из HTTPS_PROXY:
# SOCKS-адрес не взаимозаменяем с http-прокси, и угадывать его нельзя.
_DERIVED_FROM: Dict[str, str] = {
    "HTTP_PROXY": "HTTPS_PROXY",
    "http_proxy": "HTTPS_PROXY",
    "https_proxy": "HTTPS_PROXY",
    "no_proxy": "NO_PROXY",
}


def plan_proxy_backfill(env: Dict[str, str]) -> Dict[str, str]:
    """Вернуть {имя: значение} для дописывания в ``.env``.

    Чистая функция над разобранным содержимым ``.env`` — вся логика
    решения живёт здесь, чтобы её можно было проверить без файловой
    системы и без записи в чужой каталог.

    Пустая строка считается заданным значением: клиент, написавший
    ``HTTP_PROXY=``, выключил его сознательно, и досев обязан молчать.
    """
    plan: Dict[str, str] = {}
    for target, source in _DERIVED_FROM.items():
        if target in env:
            continue
        value = env.get(source)
        if not value:
            continue
        plan[target] = value
    return plan


def backfill_proxy_env() -> List[str]:
    """Дописать недостающие прокси-имена в ``.env``. Вернуть их список.

    Идемпотентна: второй запуск возвращает пустой список.
    """
    from hermes_cli.config import load_env, save_env_value

    plan = plan_proxy_backfill(load_env())
    written: List[str] = []
    for name, value in plan.items():
        try:
            save_env_value(name, value)
            written.append(name)
        except Exception:
            # Досев — улучшение, а не условие работы: не сумели дописать
            # одно имя, продолжаем с остальными и не роняем обновление.
            logger.warning("не удалось дописать %s в .env", name, exc_info=True)
    if written:
        logger.info("досев прокси в .env: %s", ", ".join(sorted(written)))
    return written


def backfill_notice(written: List[str]) -> str | None:
    """Строка клиенту о том, что досеяно, или ``None``, если ничего."""
    if not written:
        return None
    return (
        "Настройки прокси дополнены: "
        + ", ".join(sorted(written))
        + ". Без них команды агента внутри песочницы ходили в сеть "
        "напрямую, мимо вашего прокси."
    )
