"""Имена переменных окружения провайдеров — из каталога, а не из памяти.

`doctor` отвечает на вопрос «ключ вообще настроен?», сверяя `.env` со
списком имён. Список был зашит руками — 27 записей — и отстал от
каталога, который мастер показывает клиенту (35 провайдеров). Любой
клиент, выбравший провайдера вне зашитого списка, получал на полностью
рабочей машине:

    ⚠ No API key found in ~/.hermes/.env
    1. Run 'hermes setup' to configure API keys

а следом — итоговое «часть неполадок исправить самостоятельно не
удалось» в мастере. То есть последнее, что человек видел после «Готово»,
было ложной тревогой о ключе, который он только что ввёл.

Поймано на живой машине 2026-09-05: клиент выбрал Z.AI Coding Plan,
ключ записан в `ZAI_CODING_PLAN_API_KEY`, агент отвечает — а `doctor`
сообщал, что ключа нет, потому что именно этого имени в списке не было.

Чинится не дописыванием ещё одного имени: пока список живёт отдельно от
каталога, он будет отставать снова после каждого нового провайдера.
Спрашиваем каталог.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def catalog_provider_env_names() -> set[str]:
    """Имена переменных всех зарегистрированных провайдеров.

    Пустое множество, если реестр почему-то не поднялся: `doctor` —
    диагностика, она не имеет права падать из-за собственного справочника.
    Вызывающий складывает результат со своим статическим списком, так что
    отказ здесь возвращает поведение к прежнему, а не ломает его.
    """
    try:
        import providers
    except Exception:  # noqa: BLE001 — диагностика важнее причины отказа
        logger.debug("Каталог провайдеров недоступен для doctor", exc_info=True)
        return set()

    names: set[str] = set()
    try:
        profiles = providers.list_providers()
    except Exception:  # noqa: BLE001
        logger.debug("list_providers() не отработал для doctor", exc_info=True)
        return set()

    for profile in profiles:
        for env_var in getattr(profile, "env_vars", ()) or ():
            if isinstance(env_var, str) and env_var.strip():
                names.add(env_var.strip())
    return names


def provider_env_names(static_hints: tuple[str, ...] | set[str]) -> set[str]:
    """Каталог плюс статический список.

    Статический список не выбрасывается: в нём живут записи, которых в
    реестре провайдеров нет и быть не может — адреса вместо ключей
    (`OPENAI_BASE_URL`, `ACTUAL_BASE_URL`) и исторические имена, которые
    могли остаться в `.env` у давно поставленных машин.
    """
    return set(static_hints) | catalog_provider_env_names()
