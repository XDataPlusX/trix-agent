"""Справочник: как получить ключ и есть ли у сервиса бесплатный тариф.

Один источник для двух вещей сразу:

* подсказка «как получить ключ» под полем ввода в мастере настройки —
  клиент видит поле «Ключ Tavily API», ссылку на чужой англоязычный сайт
  и остаётся с ним один на один;
* наш собственный справочник по сервисам, который мы ведём: где бесплатный
  тариф есть, где его нет, где просят карту.

Раньше рядом с полем стояла только ссылка. Скриншоты чужих панелей мы
сознательно не показываем: это чужой интерфейс, его перерисуют и нас не
спросят, а картинка протухнет молча — она не упадёт и не сломает тест, она
просто начнёт врать. Текст протухает так же, но правится за минуту,
читается вслух по телефону, ничего не весит и переводится.

**«Не проверено» — полноправное значение, а не пробел.** Клиенту нельзя
показывать «бесплатный тариф есть», если мы этого не проверяли: он
потратит время на регистрацию и упрётся в карту. Поэтому у каждой записи
есть дата проверки, а незаполненное поле рендерится честной фразой
«мы не проверяли», а не молчанием и не догадкой.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Ответ на «есть ли бесплатный вариант».
FREE_YES = "yes"        # бесплатный тариф есть и им можно пользоваться постоянно
FREE_TRIAL = "trial"    # бесплатны только стартовые кредиты / пробный период
FREE_NO = "no"          # бесплатного варианта нет вовсе
FREE_UNKNOWN = "unknown"  # мы не проверяли

# Ответ на «просят ли карту».
CARD_YES = "yes"
CARD_NO = "no"
CARD_UNKNOWN = "unknown"

_VALID_FREE = frozenset({FREE_YES, FREE_TRIAL, FREE_NO, FREE_UNKNOWN})
_VALID_CARD = frozenset({CARD_YES, CARD_NO, CARD_UNKNOWN})


@dataclass(frozen=True)
class KeyGuide:
    """Что мы знаем про получение ключа у одного сервиса."""

    service: str
    """Человеческое имя сервиса — так, как его называет он сам."""

    steps: tuple[str, ...] = ()
    """Три-четыре шага. Каждый — одно действие, без вложенных условий."""

    free: str = FREE_UNKNOWN
    free_note: str = ""
    """Лимит словами, как его называет сам сервис. Пусто — лимит не знаем."""

    card: str = CARD_UNKNOWN
    key_url: str = ""
    """Страница, на которой создают ключ (не главная сайта)."""

    checked: str = ""
    """Дата проверки, ГГГГ-ММ-ДД. Пусто — не проверяли ни разу."""

    source: str = ""
    """Откуда взяты сведения о тарифе — чтобы проверять было по чему."""

    russian: bool = False
    """Российский ли сервис.

    От этого зависит формулировка про карту. Для клиента из России
    «нужна банковская карта» у зарубежного сервиса и у российского —
    это два разных препятствия: первое он, скорее всего, не преодолеет
    своей картой, второе преодолеет обычной. Сказать просто «карта»
    значит дать ему попробовать и упереться."""

    def __post_init__(self) -> None:
        if self.free not in _VALID_FREE:
            raise ValueError(f"{self.service}: неизвестное значение free={self.free!r}")
        if self.card not in _VALID_CARD:
            raise ValueError(f"{self.service}: неизвестное значение card={self.card!r}")
        if self.free != FREE_UNKNOWN and not self.checked:
            raise ValueError(
                f"{self.service}: утверждение о тарифе без даты проверки. "
                "Непроверенное показывать клиенту нельзя."
            )


# Ключ переменной окружения -> справка. Пополняется по мере проверки:
# запись без даты проверки честно скажет клиенту, что тариф мы не смотрели.
GUIDES: dict[str, KeyGuide] = {}


def guide_for(env_key: str) -> KeyGuide | None:
    """Справка по переменной окружения, если она у нас есть."""
    return GUIDES.get((env_key or "").strip())


def _free_line(guide: KeyGuide) -> str:
    if guide.free == FREE_YES:
        base = "Бесплатный тариф есть"
        return f"{base}: {guide.free_note}." if guide.free_note else base + "."
    if guide.free == FREE_TRIAL:
        base = "Бесплатны только стартовые кредиты"
        return f"{base}: {guide.free_note}." if guide.free_note else base + "."
    if guide.free == FREE_NO:
        return "Бесплатного тарифа нет — сервис платный."
    return "Есть ли бесплатный тариф — мы не проверяли."


def _card_wording(guide: KeyGuide) -> str:
    """«Карта» для зарубежного сервиса — не та же карта, что у российского.

    Оплата у зарубежных сервисов идёт за границу и в долларах, поэтому
    клиенту нужна карта иностранного банка. Мы НЕ утверждаем, что
    российскую карту отвергнут — этого мы не проверяли; мы называем, чем
    оплата пройдёт наверняка.
    """
    if guide.russian:
        return "Потребуется банковская карта."
    return "Потребуется банковская карта иностранного банка."


def _card_line(guide: KeyGuide) -> str:
    if guide.card == CARD_YES:
        return _card_wording(guide)
    if guide.card == CARD_NO:
        return "Карта не потребуется."
    if guide.free in (FREE_YES, FREE_TRIAL):
        # Вопрос про карту имеет смысл только там, где есть что получать
        # бесплатно. На платном сервисе он не задаётся.
        return "Просят ли карту — мы не уточняли."
    return ""


def badge_for(env_key: str) -> str:
    """Короткая плашка к строке каталога — видна ДО выбора сервиса.

    Апстримные плашки (`badge` в схеме плагина) для этого не годятся: они
    английские и расходятся с фактами. У Brave там стоит «free», хотя
    сервис требует карту; у Tavily — «paid», хотя 1000 запросов в месяц
    бесплатны и карта не нужна. То есть каталог уводил клиента ОТ лучшего
    бесплатного варианта.

    Пустая строка, когда тариф не проверен: догадка на плашке хуже, чем
    её отсутствие — плашку читают мельком и запоминают как факт.
    """
    guide = guide_for(env_key)
    if guide is None or guide.free == FREE_UNKNOWN:
        return ""
    if guide.free == FREE_NO:
        return "платно"
    if guide.free == FREE_TRIAL:
        return "бесплатные кредиты на старт"
    if guide.card == CARD_NO:
        return "бесплатно, без карты"
    if guide.card == CARD_YES:
        return "бесплатно, но нужна карта"
    return "есть бесплатный тариф"


def guide_payload(env_key: str) -> dict | None:
    """Готовый к отрисовке словарь для мастера, или ``None``.

    Собирается на сервере, чтобы страница не занималась склейкой фраз и
    чтобы формулировки жили в одном месте с данными.
    """
    guide = guide_for(env_key)
    if guide is None:
        return None

    # Одна строка выносится НАРУЖУ раскрывашки и видна всегда: та, из-за
    # которой клиент зря потратит время, если узнает о ней слишком поздно.
    # Строка каталога может называться «Free» (так называет свой тариф сам
    # Brave), а карту привязать всё равно придётся — узнать об этом надо
    # до регистрации, а не после.
    warning = ""
    if guide.free == FREE_NO:
        # Для платного сервиса главное — что он платный; необходимость
        # карты из этого и так следует.
        warning = "Бесплатного тарифа нет — сервис платный."
        if not guide.russian:
            warning += " Оплата картой иностранного банка."
    elif guide.card == CARD_YES:
        warning = _card_wording(guide)

    notes = []
    free_line = _free_line(guide)
    if free_line != warning:
        notes.append(free_line)
    card_line = _card_line(guide)
    if card_line and card_line != warning:
        notes.append(card_line)

    return {
        "service": guide.service,
        "steps": list(guide.steps),
        "notes": notes,
        "warning": warning,
        "key_url": guide.key_url,
        "checked": guide.checked,
    }


def as_table() -> str:
    """Справочник целиком, одной таблицей — для чтения человеком.

    Смотреть так::

        python -m hermes_cli.trix_key_guides

    Данные живут здесь, в коде, а не в отдельном документе, ровно по одной
    причине: рядом стоят проверки, которые не дают записи разъехаться с
    каталогом мастера и не дают заявить тариф без даты проверки. Документ
    рядом с кодом такого не умеет — он тихо устаревает.
    """
    _FREE_RU = {
        FREE_YES: "есть",
        FREE_TRIAL: "кредиты",
        FREE_NO: "нет",
        FREE_UNKNOWN: "?",
    }
    _CARD_RU = {CARD_YES: "да", CARD_NO: "нет", CARD_UNKNOWN: "?"}

    head = f"{'Переменная':<26} {'Сервис':<18} {'Бесплатно':<10} {'Карта':<6} Проверено"
    lines = [head, "-" * len(head)]
    for env_key in sorted(GUIDES):
        g = GUIDES[env_key]
        lines.append(
            f"{env_key:<26} {g.service:<18} {_FREE_RU[g.free]:<10} "
            f"{_CARD_RU[g.card]:<6} {g.checked or '—'}"
        )
        if g.free_note:
            lines.append(f"{'':<26} └ {g.free_note}")
    if not GUIDES:
        lines.append("(пусто)")
    return "\n".join(lines)



def _steps(domain: str) -> tuple[str, ...]:
    """Три общих шага с названным доменом сервиса.

    Названия кнопок в чужих панелях сознательно не пишем: мы их не
    проверяли, а неверное название хуже отсутствия — клиент будет искать
    несуществующую кнопку и решит, что делает что-то не так. Точный адрес
    страницы ключей едет отдельным полем и проверен.
    """
    return (
        f"Откройте страницу ключей {domain} по ссылке ниже — она попросит "
        "войти или зарегистрироваться.",
        "После входа создайте на этой странице новый ключ.",
        "Скопируйте ключ целиком и вставьте в поле выше. Обычно он показывается "
        "один раз.",
    )


_CHECKED = "2026-09-06"

GUIDES.update(
    {
        # --- поиск и чтение страниц ---------------------------------------
        "TAVILY_API_KEY": KeyGuide(
            service="Tavily",
            steps=_steps("tavily.com"),
            free=FREE_YES,
            # «Кредит» — не то же самое, что запрос: обычный поиск стоит
            # 1 кредит, углублённый — 2. Писать «1000 запросов» было бы
            # обещанием, которого сервис не даёт.
            free_note="1000 кредитов в месяц: обычный поиск — 1 кредит, углублённый — 2",
            card=CARD_NO,
            key_url="https://app.tavily.com/home",
            checked=_CHECKED,
            source="https://docs.tavily.com/documentation/api-credits",
        ),
        "FIRECRAWL_API_KEY": KeyGuide(
            service="Firecrawl",
            steps=_steps("firecrawl.dev"),
            free=FREE_YES,
            free_note="1000 страниц в месяц",
            card=CARD_NO,
            key_url="https://www.firecrawl.dev/signin?view=signup",
            checked=_CHECKED,
            source="https://www.firecrawl.dev/pricing",
        ),
        "BRAVE_SEARCH_API_KEY": KeyGuide(
            service="Brave Search",
            steps=_steps("search.brave.com"),
            free=FREE_YES,
            free_note="5 долларов в месяц, но карту привязать придётся",
            card=CARD_YES,
            key_url="https://api-dashboard.search.brave.com/register",
            checked=_CHECKED,
            source="https://api-dashboard.search.brave.com/documentation/quickstart",
        ),
        "EXA_API_KEY": KeyGuide(
            service="Exa",
            steps=_steps("exa.ai"),
            free=FREE_YES,
            free_note="20 долларов при регистрации и 10 долларов в месяц",
            card=CARD_UNKNOWN,
            key_url="https://dashboard.exa.ai/api-keys",
            checked=_CHECKED,
            source="https://exa.ai/pricing",
        ),
        "PARALLEL_API_KEY": KeyGuide(
            service="Parallel",
            steps=_steps("parallel.ai"),
            free=FREE_YES,
            free_note="5 долларов в месяц, сгорают в конце месяца",
            card=CARD_YES,
            key_url="https://platform.parallel.ai",
            checked=_CHECKED,
            source="https://parallel.ai/blog/free-tier-parallel",
        ),
        # --- голос ---------------------------------------------------------
        "ELEVENLABS_API_KEY": KeyGuide(
            service="ElevenLabs",
            steps=_steps("elevenlabs.io"),
            free=FREE_YES,
            free_note="10 000 символов озвучки в месяц",
            card=CARD_NO,
            key_url="https://elevenlabs.io/app/sign-up",
            checked=_CHECKED,
            source="https://join.elevenlabs.io/developer-api",
        ),
        "GROQ_API_KEY": KeyGuide(
            service="Groq",
            steps=_steps("groq.com"),
            free=FREE_YES,
            free_note="бесплатный тариф с лимитом запросов в минуту и в сутки",
            card=CARD_UNKNOWN,
            key_url="https://console.groq.com/keys",
            checked=_CHECKED,
            source="https://console.groq.com/docs/rate-limits",
        ),
        "NEXARA_API_KEY": KeyGuide(
            service="Nexara",
            russian=True,
            steps=_steps("nexara.ru"),
            free=FREE_YES,
            free_note="200 минут распознавания новым пользователям",
            card=CARD_UNKNOWN,
            key_url="https://app.nexara.ru",
            checked=_CHECKED,
            source="https://nexara.ru",
        ),
        "MISTRAL_API_KEY": KeyGuide(
            service="Mistral",
            steps=_steps("mistral.ai"),
            free=FREE_YES,
            free_note="бесплатный режим на их платформе; лимиты видны только в кабинете",
            card=CARD_UNKNOWN,
            key_url="https://console.mistral.ai",
            checked=_CHECKED,
            source="https://docs.mistral.ai/admin/user-management-finops/tier",
        ),
        # --- изображения и видео -------------------------------------------
        "DEEPINFRA_API_KEY": KeyGuide(
            service="DeepInfra",
            steps=_steps("deepinfra.com"),
            free=FREE_NO,
            card=CARD_YES,
            key_url="https://deepinfra.com/dash/api_keys",
            checked=_CHECKED,
            source="https://deepinfra.com/pricing",
        ),
        "KREA_API_KEY": KeyGuide(
            service="Krea",
            steps=_steps("krea.ai"),
            free=FREE_NO,
            card=CARD_YES,
            key_url="https://www.krea.ai/settings/api-tokens",
            checked=_CHECKED,
            source="https://www.krea.ai/docs/developers/api-keys-and-billing",
        ),
        "FAL_KEY": KeyGuide(
            service="fal.ai",
            steps=_steps("fal.ai"),
            key_url="https://fal.ai/dashboard/keys",
        ),
        # --- модели ---------------------------------------------------------
        "GEMINI_API_KEY": KeyGuide(
            service="Google AI Studio",
            steps=_steps("aistudio.google.com"),
            free=FREE_YES,
            free_note="бесплатный старт с лимитами; точные цифры видны в кабинете",
            card=CARD_UNKNOWN,
            key_url="https://aistudio.google.com",
            checked=_CHECKED,
            source="https://ai.google.dev/pricing",
        ),
        "OPENROUTER_API_KEY": KeyGuide(
            service="OpenRouter",
            steps=_steps("openrouter.ai"),
            free=FREE_YES,
            free_note="бесплатные модели: 20 запросов в минуту и 50 в сутки",
            card=CARD_UNKNOWN,
            key_url="https://openrouter.ai/keys",
            checked=_CHECKED,
            source="https://openrouter.ai/docs/api-reference/limits",
        ),
        "OPENAI_API_KEY": KeyGuide(
            service="OpenAI",
            steps=_steps("platform.openai.com"),
            key_url="https://platform.openai.com/signup",
        ),
        "VOICE_TOOLS_OPENAI_KEY": KeyGuide(
            service="OpenAI",
            steps=_steps("platform.openai.com"),
            key_url="https://platform.openai.com/signup",
        ),
    }
)
if __name__ == "__main__":  # pragma: no cover - ручной просмотр
    print(as_table())
