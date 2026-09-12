"""Живая проверка ключа для провайдеров, которых нет в списке проверок.

`CREDENTIAL_PROBES` — четыре записи: OpenRouter, OpenAI, xAI, Gemini.
Ни одной из них не пользуется российский клиент: он берёт DeepSeek,
Z.AI/GLM, Kimi, Qwen. Поэтому мастер почти всем отвечал «Ключ провайдера
не проверялся автоматически» — честно, но бесполезно: ошибку в ключе
человек узнавал не на шаге ключа, а когда бот молчал.

Все эти провайдеры говорят по протоколу OpenAI и держат `GET
{base_url}/models` с заголовком `Authorization: Bearer`. Значит адрес
проверки выводится из каталога, и список вести руками не надо.

**Осторожность здесь важнее полноты.** Выведенная проверка — догадка об
адресе, а не курированная запись, поэтому она имеет право завалить
установку ТОЛЬКО при недвусмысленном отказе в самом ключе (401/403) или
пустом счёте (402). Всё остальное — 404 у провайдера без `/models`,
пятисотки, обрыв связи — обязано читаться как «проверить не удалось» и
пропускать клиента дальше. Иначе цена ошибки в догадке — человек с
исправным ключом не может закончить настройку, а это хуже, чем
непроверенный ключ.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Статусы, по которым выведенной проверке позволено НЕ пустить клиента.
# Каждый из них говорит о самом ключе, а не о том, угадали ли мы адрес.
CONCLUSIVE_REJECTIONS = frozenset({401, 402, 403})


def models_endpoint(base_url: str | None) -> str | None:
    """`{base_url}/models` — адрес, по которому спрашивают список моделей."""
    if not base_url or not isinstance(base_url, str):
        return None
    trimmed = base_url.strip().rstrip("/")
    if not trimmed.startswith(("http://", "https://")):
        return None
    return f"{trimmed}/models"


def _profile_for_env_var(env_var: str):
    try:
        import providers

        for profile in providers.list_providers():
            for candidate in getattr(profile, "env_vars", ()) or ():
                if candidate == env_var:
                    return profile
    except Exception:  # noqa: BLE001 — проверка ключа не должна падать из-за реестра
        logger.debug("Каталог провайдеров недоступен для выведенной проверки", exc_info=True)
    return None


def derived_probe_url(env_var: str, base_url: str | None = None) -> str | None:
    """Адрес выведенной проверки для *env_var*.

    *base_url* — адрес, который клиент ввёл на этом самом шаге: он
    главнее каталожного (клиент мог указать своё зеркало или выбрать
    «свой провайдер», которого в каталоге нет вовсе). Каталог —
    запасной вариант.
    """
    from_client = models_endpoint(base_url)
    if from_client:
        return from_client
    profile = _profile_for_env_var(env_var)
    if profile is None:
        return None
    return models_endpoint(getattr(profile, "base_url", None))
