"""Мастер записывает цепочку поисковиков, а не один движок.

До этой правки мастер писал в `config.yaml` одну строку:

    web:
      search_backend: ddgs

Цепочка запасных (`tools/web_tools.py::_run_search_backend_chain`)
принимает список и при одной строке не включается вообще. То есть в день,
когда выбранный поисковик перестанет отвечать, поиск у клиента просто
умрёт — без запасного варианта и без объяснения.

Что DuckDuckGo когда-нибудь перестанет отвечать — не предположение.
Снято 2026-09-05 на клиентской машине: SearXNG, поднятый там же, получил
от duckduckgo `CAPTCHA`, от brave — «Suspended: too many requests», от
startpage — `Suspended: CAPTCHA`. DuckDuckGo — единственный вариант из
предлагаемых, который **скрейпит** выдачу, а не ходит в официальное API;
он и сломается первым.

**В хвост цепочки попадает только то, что не требует действий клиента.**
Дописать туда платный движок нельзя: ключа к нему у клиента может не
быть, и цепочка молча тратила бы время на заведомо мёртвого кандидата.
Поэтому хвост — это бесключевые движки, и ничего больше.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

# Движки, которым не нужен ни ключ, ни отдельная служба: их можно ставить
# в хвост любому выбору клиента, ничего у него не спрашивая.
KEYLESS_FALLBACKS: tuple[str, ...] = ("ddgs",)


def build_search_chain(
    chosen: str,
    extra_keyless: Sequence[str] = (),
) -> list[str]:
    """Выбор клиента плюс бесключевой хвост, без повторов.

    Выбор клиента ВСЕГДА первый: цепочка — это запасной выход, а не
    подмена решения. *extra_keyless* — движки, которые на этой машине
    доступны без ключа помимо базовых (например поднятый клиентом
    SearXNG, который мастер к этому моменту уже нащупал живым).

    Пустой *chosen* даёт пустой список: писать «цепочку» из одного
    только хвоста, когда клиент ничего не выбирал, значит принять
    решение за него.
    """
    primary = (chosen or "").strip().lower()
    if not primary:
        return []

    chain = [primary]
    for name in (*KEYLESS_FALLBACKS, *extra_keyless):
        candidate = (name or "").strip().lower()
        if candidate and candidate not in chain:
            chain.append(candidate)
    return chain


def primary_backend(value: Any) -> str:
    """Первый движок цепочки — то, что мастер показывает как выбор клиента.

    Форма мастера оперирует одним именем, а в конфиге теперь может лежать
    список. Без этой функции возврат клиента в мастер отдал бы в поле
    выбора список целиком и выбор бы потерялся.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def chain_is_meaningful(chain: Iterable[str]) -> bool:
    """Есть ли в цепочке хоть один запасной вариант.

    Один элемент — это не цепочка, а прежнее поведение под другим типом.
    """
    return len(list(chain)) > 1
