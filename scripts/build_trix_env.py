#!/usr/bin/env python3
"""Досевает в клиентский шаблон .env весь остальной список переменных.

**Зачем.** То же решение владельца, что и для ``config.yaml``: клиент должен
видеть всё в одном файле. ``assets/config/trix.env.example`` перечислял 8
переменных из 151, и узнать про остальные было негде.

**Почему это дешевле, чем с config.yaml.** За ``.env`` не стоит никакого
``DEFAULT_CONFIG``: закомментированная переменная и отсутствующая — для
программы буквально одно и то же. Значит риска «заморозить дефолт» здесь нет
вовсе, а вся досеянная часть — чистая документация, которая ничего не
включает, пока клиент сам не снимет решётку.

**Источников ДВА, и ни один не полон сам по себе.** Проверено пересчётом:

* ``OPTIONAL_ENV_VARS`` — реестр, по которому работают мастер настройки и
  ``hermes config check``. Он следит за кодом, и из него собирается тело
  файла. Но 50 переменных, которые код читает и которые апстрим документирует
  (``GROQ_API_KEY``, ``TELEGRAM_WEBHOOK_*``, ``TERMINAL_SSH_*``), реестру
  неизвестны вовсе.
* ``.env.example`` апстрима — человеческий документ на 496 строк с
  пояснениями и ссылками. Но 190 переменных реестра он не упоминает, включая
  ровно те, которыми пользуемся мы сами: ``DEEPSEEK_API_KEY``,
  ``BRAVE_SEARCH_API_KEY``, ``TAVILY_API_KEY``, ``ZAI_CODING_PLAN_API_KEY``.
  Плюс в нём 11 ЖИВЫХ строк с поведенческими настройками
  (``TERMINAL_TIMEOUT``, ``BROWSER_*``, ``*_DEBUG``), строка под пароль root
  и 16 упоминаний имени апстрима — то есть скопировать его как есть нельзя.

Поэтому: **тело собирается из реестра, а апстримный пример работает вторым
источником полноты** — ``--check`` краснеет и на его переменные тоже, так что
пропасть молча они больше не могут. Всё, чего в файле нет, лежит в
:data:`EXCLUDED` с письменной причиной.

**Чего в клиентском файле нет по продуктовому решению.** Каналы связи, кроме
Telegram. Реестр знает 196 переменных категории ``messaging`` — Matrix,
Mattermost, iMessage, QQ, WeCom, Feishu, Zulip и так далее; это 664 строки
английского текста про каналы, которых в поставке нет. Продукт работает через
Telegram, поэтому из категории остаются ``TELEGRAM_*`` и общие для шлюза
``GATEWAY_*`` (см. :data:`KEPT_MESSAGING_PREFIXES`), а остальное заменяется
одним абзацем.

**Почему сборка идёт в изолированном HERMES_HOME.** Реестр не статичен:
``hermes_cli.config`` при импорте досыпает в него переменные ВСЕХ найденных
плагинов-провайдеров (``_inject_profile_env_vars``), а плагины ищутся не
только в репозитории, но и в ``$HERMES_HOME/plugins/model-providers``. Собери
шаблон на машине разработчика как есть — и его личные плагины уехали бы
клиенту. Поэтому перед импортом ``HERMES_HOME`` подменяется на пустой
временный каталог: в список попадают только провайдеры, лежащие в самом
репозитории, и результат одинаков у всех.

Запуск::

    python3 scripts/build_trix_env.py            # досеять и записать
    python3 scripts/build_trix_env.py --check    # только проверить
"""

from __future__ import annotations

import argparse
import atexit
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "assets" / "config" / "trix.env.example"

#: Апстримный пример — второй источник полноты, но НЕ источник текста:
#: копировать его целиком нельзя (11 живых поведенческих строк, строка под
#: пароль root, имя чужого продукта). Из него берутся только ИМЕНА, чтобы
#: переменная, которую знает апстрим и не знает реестр, не пропала молча.
UPSTREAM_EXAMPLE = REPO_ROOT / ".env.example"

#: Строка, после которой начинается досеянная часть. Всё до неё —
#: кураторская часть, её скрипт не трогает; всё после — перегенерируется.
MARKER = "# ==== ПОЛНЫЙ СПИСОК ОСТАЛЬНЫХ ПЕРЕМЕННЫХ (сгенерировано) ===="

#: Префиксы категории ``messaging``, которые в клиентском файле остаются.
#: ``TELEGRAM_`` — канал поставки; ``GATEWAY_`` — общие настройки шлюза,
#: которые к конкретному каналу не привязаны.
KEPT_MESSAGING_PREFIXES = ("TELEGRAM_", "GATEWAY_")

#: Причина, по которой из категории ``messaging`` остаётся один Telegram.
#: Она же печатается в клиентский файл вместо 664 строк.
CHANNELS_REASON = (
    "продукт работает через Telegram, и остальные каналы связи в поставку "
    "не входят. Агент их умеет — Slack, Matrix, почта и ещё полтора десятка; "
    "если понадобится, это делается на нашей стороне, а не строкой в этом "
    "файле"
)

#: Групповые исключения: причина → имена. Группами, а не по одному, потому
#: что причина у них буквально общая, а пятьдесят копий одного и того же
#: текста никто бы не вычитал. Развёрнутое отображение {имя: причина} —
#: ниже, :data:`EXCLUDED`; на него опираются тесты полноты.
_EXCLUDED_GROUPS: dict[str, tuple] = {
    (
        "настройка поведения, а не секрет: по правилу проекта в .env живут "
        "только ключи и пароли, а всё остальное — в config.yaml. Живая "
        "строка здесь ещё и вводила бы в заблуждение: у terminal.* файл "
        "config.yaml теперь перекрывает .env, и клиент правил бы строку, "
        "которая ни на что не влияет"
    ): (
        "AGENT_BROWSER_ARGS",
        "BROWSERBASE_ADVANCED_STEALTH",
        "BROWSERBASE_PROXIES",
        "BROWSER_INACTIVITY_TIMEOUT",
        "BROWSER_SESSION_TIMEOUT",
        "BUZZ_POLL_INTERVAL",
        "CONTEXT_COMPRESSION_ENABLED",
        "CONTEXT_COMPRESSION_THRESHOLD",
        "ELEVENLABS_STT_BASE_URL",
        "GROQ_BASE_URL",
        "HERMES_DOCKER_BINARY",
        "HERMES_HUMAN_DELAY_MAX_MS",
        "HERMES_HUMAN_DELAY_MIN_MS",
        "HERMES_HUMAN_DELAY_MODE",
        "IMAGE_TOOLS_DEBUG",
        "MOA_TOOLS_DEBUG",
        "STT_ELEVENLABS_MODEL",
        "STT_GROQ_MODEL",
        "STT_OPENAI_BASE_URL",
        "STT_OPENAI_MODEL",
        "TERMINAL_CWD",
        "TERMINAL_DOCKER_IMAGE",
        "TERMINAL_ENV",
        "TERMINAL_LIFETIME_SECONDS",
        "TERMINAL_MODAL_IMAGE",
        "TERMINAL_SINGULARITY_IMAGE",
        "TERMINAL_TIMEOUT",
        "VISION_TOOLS_DEBUG",
        "WEB_TOOLS_DEBUG",
    ),
    (
        "пароль root в открытом файле на машине клиента. Владелец решил "
        "убрать sudo из продукта (NOPASSWD снят, в код sudo не добавляем) — "
        "показывать клиенту готовую строку под пароль значит приглашать "
        "вернуть то, что мы сняли"
    ): ("SUDO_PASSWORD",),
    (
        "терминал агента работает в контейнере на той же машине. Удалённый "
        "бэкенд по SSH в поставку не входит, а адрес, пользователь, порт и "
        "путь к ключу — это ещё и настройки, а не секреты"
    ): (
        "TERMINAL_SSH_HOST",
        "TERMINAL_SSH_KEY",
        "TERMINAL_SSH_PORT",
        "TERMINAL_SSH_USER",
    ),
    (
        "режим вебхука требует публичного адреса и открытого порта наружу. "
        "Поставка забирает сообщения опросом (при необходимости через "
        "TELEGRAM_PROXY), и адрес с портом здесь — настройки, а не секреты"
    ): (
        "TELEGRAM_WEBHOOK_PORT",
        "TELEGRAM_WEBHOOK_SECRET",
        "TELEGRAM_WEBHOOK_URL",
    ),
    (
        "навык не входит в поставку: показывать клиенту строки под чужой "
        "биржевой счёт в файле, который он открывает при первом запуске, "
        "значит предлагать то, чего у него нет"
    ): ("HYPERLIQUID_API_URL", "HYPERLIQUID_USER_ADDRESS"),
    (
        "бот-личность на GitHub для публикации навыков — в поставку не "
        "входит. Для повышенного лимита обращений хватает GITHUB_TOKEN, он "
        "в списке есть"
    ): (
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
    ),
    (
        "подключение к браузеру, которым управляет другое приложение на той "
        "же машине. У клиента браузер свой, и это к тому же идентификаторы "
        "сессии, а не секреты"
    ): ("CAMOFOX_ADOPT_EXISTING_TAB", "CAMOFOX_SESSION_KEY", "CAMOFOX_USER_ID"),
    (
        "переменную больше не читает никто: апстрим сам пишет в своём "
        "примере, что строка оставлена для справки, а модель живёт в "
        "config.yaml (model.default)"
    ): ("LLM_MODEL",),
    CHANNELS_REASON: (
        "EMAIL_IMAP_PORT",
        "EMAIL_POLL_INTERVAL",
        "GOOGLE_CHAT_ALLOW_ALL_USERS",
        "GOOGLE_CHAT_HOME_CHANNEL_NAME",
    ),
}

#: Переменные, которых в клиентском шаблоне не будет намеренно.
#: Отображение {имя: причина} — не множество: причина обязана быть у каждой,
#: иначе список тихо превратится в свалку. Собирается из
#: :data:`_EXCLUDED_GROUPS`; переменные каналов связи досыпаются в него по
#: правилу, а не руками, — см. :func:`is_excluded`.
EXCLUDED = {
    name: reason for reason, names in _EXCLUDED_GROUPS.items() for name in names
}

#: Переменные, которые код читает и апстрим документирует, а реестр не знает.
#: Вписаны руками и по-русски. Формат тот же, что у записи реестра, — их
#: просто досыпают в список перед сборкой.
#:
#: Всё, что реестру неизвестно, — либо здесь, либо в :data:`EXCLUDED`:
#: ``--check`` сверяется с апстримным примером и не даст умолчать ни одну.
EXTRA = {
    "GROQ_API_KEY": {
        "category": "tool",
        "description": (
            "Ключ Groq — бесплатное распознавание речи в голосовых "
            "сообщениях. Без него голос распознаётся на самой машине "
            "(медленнее) или не распознаётся вовсе."
        ),
        "url": "https://console.groq.com/keys",
    },
    "TELEGRAM_CRON_THREAD_ID": {
        "category": "messaging",
        "description": (
            "Номер темы в чате-форуме, куда складывать результаты задач по "
            "расписанию. Нужен, только если чат из строки "
            "TELEGRAM_HOME_CHANNEL разбит на темы: без него ответы уходят в "
            "общую ленту."
        ),
    },
}


def is_excluded(name: str, meta: dict) -> bool:
    """Не попадает ли переменная в клиентский файл.

    Два правила: поимённое исключение и продуктовое — «каналы связи, кроме
    Telegram». Второе правило именно правило, а не 182 строки в таблице:
    оно должно накрывать и тот канал, который апстрим добавит завтра.
    """
    if name in EXCLUDED:
        return True
    if meta.get("category") == "messaging":
        return not name.startswith(KEPT_MESSAGING_PREFIXES)
    return False


def exclusion_reason(name: str, meta: dict) -> str:
    """Причина исключения — для теста «дыра без обоснования»."""
    if name in EXCLUDED:
        return EXCLUDED[name]
    if is_excluded(name, meta):
        return CHANNELS_REASON
    return ""


#: Описания, переписанные по-русски. Берутся вместо реестровых.
#:
#: Причина не в переводе как таковом — длинный хвост списка остаётся на
#: английском намеренно, объяснять клиенту ключ к Azure Foundry по-русски
#: смысла не больше, чем по-английски. Переписаны две группы описаний:
#:
#: 1. те, в ТЕКСТЕ которых стоит имя чужого продукта — клиентский файл не
#:    место для рекламы того, что мы не продаём, и для имени системы,
#:    которой у клиента нет;
#: 2. раздел Telegram целиком — это канал поставки, клиент читает его
#:    строки первыми, и «dev only» английским мелким шрифтом на выключателе
#:    «пускать кого угодно» — плохой способ предупредить.
#:
#: Имена самих переменных при этом не меняются никогда — их читает код.
#:
#: Переопределение мёртвой переменной — тихий мусор: текст, который никто
#: никогда не увидит, потому что сама строка в файл не попадает. Такое
#: ловит ``test_every_description_override_reaches_the_file``; одиннадцать
#: штук пришлось убрать, когда из файла ушли каналы связи.
DESCRIPTION_OVERRIDES = {
    "NOUS_BASE_URL": "Адрес другого портала моделей (нужен свой аккаунт там).",
    "VERTEX_CREDENTIALS_PATH": (
        "Путь к JSON сервисного аккаунта Google Cloud для Vertex AI (Gemini). "
        "Vertex работает по OAuth2, а не по постоянному ключу: агент сам "
        "выписывает короткоживущие токены по этому файлу. Если не указан — "
        "берётся GOOGLE_APPLICATION_CREDENTIALS, затем ADC "
        "(gcloud auth application-default login). Проект и регион — в "
        "config.yaml, раздел vertex."
    ),
    "AZURE_FOUNDRY_BASE_URL": (
        "Адрес Azure Foundry. Обычно задаётся командой выбора модели, а не "
        "руками."
    ),
    "FIRECRAWL_GATEWAY_URL": (
        "Адрес шлюза Firecrawl. Нужен только при подписке на сторонний "
        "тариф, который такой шлюз предоставляет — при обычном ключе "
        "Firecrawl не нужен."
    ),
    "TOOL_GATEWAY_DOMAIN": (
        "Домен общего шлюза инструментов — из того же стороннего тарифа, "
        "что и строка выше. При обычных ключах не нужен."
    ),
    "TOOL_GATEWAY_SCHEME": (
        "Схема адреса того же шлюза (по умолчанию https; http — только для "
        "локальной отладки)."
    ),
    "TOOL_GATEWAY_USER_TOKEN": (
        "Токен доступа к тому же шлюзу инструментов. Обычно не нужен: если "
        "тариф подключён, токен берётся из сохранённого входа."
    ),
    "PORCUPINE_ACCESS_KEY": (
        "Ключ Picovoice для платного распознавателя ключевого слова "
        "(Porcupine, слово-активатор 'Hey Hermes'). По умолчанию работает "
        "бесплатный openWakeWord — ключ не нужен."
    ),
    "GATEWAY_ALLOW_ALL_USERS": (
        "Пускать к агенту кого угодно, не спрашивая (true/false, по "
        "умолчанию false). Включать не нужно: тогда любой, кто найдёт "
        "бота, сможет им пользоваться от вашего имени и за ваш счёт. "
        "Кому можно — перечисляется в TELEGRAM_ALLOWED_USERS в начале "
        "файла."
    ),
    "TELEGRAM_ALLOW_ALL_USERS": (
        "Пускать в Telegram кого угодно, не спрашивая (true/false, по "
        "умолчанию false). Оставьте выключенным: список тех, кому можно, "
        "задаётся строкой TELEGRAM_ALLOWED_USERS в начале файла."
    ),
    "TELEGRAM_HOME_CHANNEL_NAME": (
        "Как называть в сообщениях чат из строки TELEGRAM_HOME_CHANNEL. "
        "Чисто для читаемости: если не заполнено, агент показывает номер "
        "чата."
    ),
    "GATEWAY_PROXY_URL": (
        "Адрес удалённого сервера агента, которому пересылать сообщения "
        "(режим посредника). Когда указан, местный шлюз занимается только "
        "мессенджерами, а всю работу делает удалённая машина. Настраивается "
        "и в config.yaml, gateway.proxy_url."
    ),
    "GATEWAY_PROXY_KEY": (
        "Токен для того же удалённого сервера. Должен совпадать с "
        "API_SERVER_KEY на нём."
    ),
}

#: Что из таблицы исключений печатается в самом клиентском файле.
#:
#: Клиенту нужны не пятьдесят объяснений, а ответ на три вопроса, которые он
#: задаст, не найдя строки: где остальные мессенджеры, где настройки
#: поведения и где строка под пароль. Остальные причины — разработческие, их
#: место в :data:`EXCLUDED`, а не в файле, который читает клиент.
NOTICES = (
    f"Каналы связи, кроме Telegram, здесь не перечислены: {CHANNELS_REASON}.",
    "Настроек поведения здесь нет вовсе — ни таймаутов, ни отладки, ни "
    "порогов сжатия. В этом файле живут только ключи и пароли, всё "
    "остальное — в config.yaml рядом.",
    "Строки под пароль root тут тоже нет, и это намеренно: sudo с машины "
    "снят, а пароль в открытом файле прочитает любой, у кого есть доступ к "
    "машине.",
)

#: Порядок и русские заголовки групп. Категории берутся из реестра.
GROUPS = (
    ("provider", "Ключи провайдеров моделей", None),
    (
        "tool",
        "Ключи инструментов",
        "поиск, извлечение страниц, картинки, браузер, распознавание речи",
    ),
    (
        "messaging",
        "Telegram и общие настройки связи",
        "самое нужное — в начале файла; здесь то, что требуется редко",
    ),
    ("skill", "Ключи для отдельных навыков", None),
    (
        "setting",
        "Служебные",
        "обычные настройки поведения живут не здесь, а в config.yaml — "
        "в этом файле только секреты",
    ),
)


_isolated_home_cache = None


def _isolated_home() -> str:
    """Пустой временный HERMES_HOME, один на процесс.

    Каталог живёт до конца процесса (его нельзя удалить раньше: HERMES_HOME
    кэшируется модульными константами на импорте), поэтому убирается через
    ``atexit`` — иначе каждый прогон, включая каждый тест, который дёргает
    реестр, оставлял бы на диске каталог ``trix-env-build-*``.
    """
    global _isolated_home_cache
    if _isolated_home_cache is None:
        _isolated_home_cache = tempfile.mkdtemp(prefix="trix-env-build-")
        atexit.register(shutil.rmtree, _isolated_home_cache, ignore_errors=True)
    return _isolated_home_cache


def _registry() -> dict:
    """Реестр переменных с досыпанными провайдерами репозитория.

    ``HERMES_HOME`` подменяется до импорта: модульные константы кэшируют его
    на импорте, а поиск плагинов-провайдеров лезет в
    ``$HERMES_HOME/plugins/model-providers``.
    """
    os.environ["HERMES_HOME"] = _isolated_home()
    # Импорт именно hermes_cli.config, а не config_defaults: инъекция
    # провайдеров происходит как побочный эффект импорта первого.
    from hermes_cli.config import OPTIONAL_ENV_VARS

    # Копия, а не сам реестр: EXTRA — наш список для файла, и досыпать его
    # в общий объект значило бы подсунуть мастеру настройки переменные,
    # которых он не знает.
    return {**OPTIONAL_ENV_VARS, **EXTRA}


def _upstream_names() -> set:
    """Имена из апстримного ``.env.example`` — второй источник полноты.

    Файл лежит в репозитории; если его вдруг нет (усечённая поставка),
    проверка просто теряет второй источник, а не падает.
    """
    try:
        return _declared_names(UPSTREAM_EXAMPLE.read_text(encoding="utf-8"))
    except OSError:
        return set()


def _declared_names(text: str) -> set:
    """Имена переменных, уже упомянутые в тексте — живыми или под решёткой."""
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))


def _curated_part(text: str) -> str:
    """Кураторская часть — всё до маркера."""
    idx = text.find(MARKER)
    if idx == -1:
        return text.rstrip("\n") + "\n"
    return text[:idx].rstrip("\n") + "\n"


#: Слово, похожее на присваивание: ``deliver=a2a``, ``KEY=value``.
_LOOKS_LIKE_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _wrap(prefix: str, body: str, width: int = 74) -> list:
    """Перенести текст по ширине, не начиная строку словом с ``=``.

    Тонкость, найденная тестом: строка комментария, начинающаяся со слова
    вида ``deliver=a2a.``, читается разборщиком .env как ПРИСВАИВАНИЕ —
    достаточно снять решётку, и появится переменная ``deliver``, которую не
    читает никто. Английские описания в реестре такие слова содержат
    (``deliver=a2a``, ``true/false``), и перенос по ширине рано или поздно
    выносил одно из них в начало строки.

    Поэтому такое слово никогда не начинает строку: оно остаётся на
    предыдущей, даже если та выйдет чуть шире ``width``. Текст при этом не
    меняется ни на символ — двигается только место переноса.
    """
    out, line = [], prefix
    for word in body.split():
        too_long = len(line) + 1 + len(word) > width
        if too_long and line != prefix and not _LOOKS_LIKE_ASSIGNMENT.match(word):
            out.append(line)
            line = prefix + word
        else:
            line = f"{line} {word}" if line != prefix else prefix + word
    if line != prefix:
        out.append(line)
    return out


def _generate(already: set) -> list:
    registry = _registry()

    lines = [
        MARKER,
        "#",
        "# Ниже — все остальные переменные, которые понимает агент. Каждая",
        "# закомментирована: пока решётка на месте, переменной для программы",
        "# не существует. Чтобы включить — уберите решётку и впишите значение.",
        "#",
        "# Правьте только кураторскую часть выше; этот список пересобирается",
        "# командой python3 scripts/build_trix_env.py.",
    ]
    for notice in NOTICES:
        lines.append("#")
        lines += _wrap("# ", notice)

    for category, title, note in GROUPS:
        entries = [
            (name, meta)
            for name, meta in registry.items()
            if meta.get("category") == category
            and name not in already
            and not is_excluded(name, meta)
        ]
        if not entries:
            continue
        lines += ["", "# " + "─" * 74, f"# {title} ({len(entries)})"]
        if note:
            lines += _wrap("# ", note)
        lines.append("# " + "─" * 74)
        for name, meta in entries:
            lines.append("")
            description = (
                DESCRIPTION_OVERRIDES.get(name) or meta.get("description") or ""
            ).strip()
            if description:
                lines += _wrap("# ", description)
            url = meta.get("url")
            if url:
                lines.append(f"#   {url}")
            lines.append(f"# {name}=")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO_ROOT))

    text = TEMPLATE.read_text(encoding="utf-8")

    if args.check:
        registry = _registry()
        declared = _declared_names(text)
        # Два источника: реестр (следит за кодом) и апстримный пример (знает
        # то, чего реестр не знает). Имя из второго источника реестру
        # незнакомо, поэтому категории у него нет — правило про каналы связи
        # к нему не применяется, и умолчать его можно только через EXCLUDED.
        missing = [
            name
            for name in registry
            if name not in declared and not is_excluded(name, registry[name])
        ]
        missing += [
            name
            for name in sorted(_upstream_names())
            if name not in declared and name not in registry and name not in EXCLUDED
        ]
        if not missing:
            print(
                f"шаблон .env полон: реестр ({len(registry)}) и апстримный "
                "пример разобраны до последней переменной"
            )
            return 0
        print(f"в шаблоне .env не хватает {len(missing)}:", file=sys.stderr)
        for name in missing[:40]:
            print("   " + name, file=sys.stderr)
        print(
            "\nдовести: python3 scripts/build_trix_env.py — либо вписать "
            "причину в EXCLUDED",
            file=sys.stderr,
        )
        return 1

    curated = _curated_part(text)
    already = _declared_names(curated)
    generated = _generate(already)
    TEMPLATE.write_text(curated + "\n" + "\n".join(generated) + "\n", encoding="utf-8")

    total = len(TEMPLATE.read_text(encoding="utf-8").splitlines())
    listed = len(_declared_names(TEMPLATE.read_text(encoding="utf-8")))
    print(f"шаблон .env: {total} строк, переменных упомянуто {listed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
