"""Досеивает в уже существующий клиентский ``.env`` полный список переменных.

**Зачем.** Установщик и ``doctor --fix`` кладут ``assets/config/trix.env.example``
только когда ``.env`` у клиента ОТСУТСТВУЕТ. Значит полный список переменных
(сентябрь 2026, решение владельца — клиент должен видеть всё) доезжает только
до новых установок, а на работающей машине ``.env`` остаётся прежним. Найдено
живой проверкой на машине после релиза 0.1.30: ``config.yaml`` пришёл к полному
паритету, а ``.env`` остался на 54 строках.

**Почему это несравнимо проще, чем досев ``config.yaml``.** Там врезка идёт в
СЕРЕДИНУ структурированного документа, и пять кругов ревью ушло на то, чтобы
не сломать разбор чужой формы. Здесь дописывается ХВОСТ плоского файла, и
каждая дописанная строка — комментарий: за ``.env`` не стоит никакого
``DEFAULT_CONFIG``, поэтому закомментированная переменная и отсутствующая для
программы одно и то же. Поведение измениться не может по построению — это
проверяется тестом, а не обещается.

**Идемпотентность через маркер.** Признак «документация уже дописана» — строка
:data:`MARKER` в файле. Она же отделяет кураторскую часть шаблона от
сгенерированной (см. ``scripts/build_trix_env.py``), поэтому отдельного
состояния на диске не нужно: файл сам про себя всё говорит.

**Чего этот модуль НЕ делает.** Не трогает ни одной существующей строки, не
раскомментирует ничего, не пишет значений. Клиент, удаливший дописанный хвост,
получит его снова при следующем обновлении — в отличие от ``config.yaml``, где
удаление настройки уважается: там строка что-то меняет, а здесь это чистая
справка, и «клиент осознанно удалил справочник» не тот случай, ради которого
стоит вести отдельное состояние.
"""

from __future__ import annotations

import os
from pathlib import Path

from utils import atomic_write_text

#: Строка, с которой начинается сгенерированная часть шаблона. Она же —
#: признак «у этого клиента список уже есть».
MARKER = "# ==== ПОЛНЫЙ СПИСОК ОСТАЛЬНЫХ ПЕРЕМЕННЫХ (сгенерировано) ===="


def _dominant_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def sync_missing_env_documentation(env_path: Path, template_path: Path) -> int:
    """Дописать в ``env_path`` справочную часть шаблона.

    Возвращает число дописанных строк (``0`` — уже на месте, либо файл или
    шаблон недоступны). Ошибки глотаются: вызывается из ``hermes update``, и
    падение здесь не имеет права ронять обновление.
    """
    env_path = Path(env_path)
    template_path = Path(template_path)

    if env_path.exists() and not os.access(env_path, os.W_OK):
        # Файл, снятый клиентом в «только чтение», не переписываем — то же
        # правило, что у досева config.yaml.
        return 0

    try:
        with open(env_path, encoding="utf-8", newline="") as handle:
            client_text = handle.read()
        template_text = template_path.read_text(encoding="utf-8")
    except Exception:
        return 0

    if MARKER in client_text:
        return 0
    if MARKER not in template_text:
        return 0

    tail = template_text[template_text.index(MARKER):]
    separator = _dominant_newline(client_text)
    if separator != "\n":
        tail = tail.replace("\n", separator)

    joiner = "" if client_text.endswith(("\n", "\r")) else separator
    new_text = client_text + joiner + separator + tail

    try:
        atomic_write_text(env_path, new_text, newline="", preserve_mode=True)
    except Exception:
        return 0
    return len(tail.splitlines()) + 1
