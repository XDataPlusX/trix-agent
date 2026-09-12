"""Смена НАШЕГО умолчания доезжает до клиента; правка клиента — навсегда его.

Правило владельца 2026-09-10: «изменение клиента превыше всего, но если он
ничего не менял, то мы меняем за него».

**Почему это нельзя решить сравнением с шаблоном.** Значение клиента,
отличающееся от шаблона, означает ровно две несовместимые вещи: клиент
правил ключ ЛИБО мы подняли умолчание, а клиент остался на прежнем.
Первое трогать нельзя, второе — обязаны. Отличить их можно единственным
способом: помнить, что мы этой машине ОТГРУЖАЛИ.

Отсюда sidecar :data:`BASELINE_STATE_FILENAME` — путь → одна из двух
отметок:

``{"shipped": <значение>}``
    последнее умолчание, которое мы этой машине отдали. Совпадает с тем,
    что лежит у клиента, — значит клиент ключ не трогал.

``{"owned_by_client": true}``
    клиент этот ключ правил. Навсегда: следующая смена умолчания его тоже
    не коснётся. Это буквальная запись слов владельца, а не осторожность.

**Почему отдельный файл, а не sidecar досева.** У ``trix_config_sync``
формат «путь → когда дописан», и он проверяет принадлежность словарю
(``if путь in seeded``). Вложить туда второй смысл значило бы сломать
собственную проверку досева ради экономии одного файла.

**Первый прогон на уже работающей машине ничего не меняет.** Отгруженного
значения мы не помним, а угадать нельзя: ошибка в одну сторону перетрёт
правку клиента. Поэтому ключи, совпавшие с шаблоном, запоминаются как
наши, а разошедшиеся сразу объявляются клиентскими. По-настоящему
механизм заработает со СЛЕДУЮЩЕЙ смены умолчания — то же свойство, что у
любой правки пути обновления: клиент обновляется кодом предыдущей версии
(``STATUS_release`` §6a).

**Правится только СКАЛЯР и только на его собственной строке.** Список или
словарь одной строкой не переписать, не рискуя файлом, — такой путь
уходит в ``skipped`` с причиной, как это делает досев. Комментарии,
отступы и хвостовые пометки сохраняются: конфиг клиента — документ с
русскими объяснениями, ради которых он и существует, а не набор ключей.

**Результат перечитывается до записи.** Переписанный текст разбирается
YAML-ом и сверяется: изменилось ровно то, что собирались менять, и ничего
больше. Не сошлось — файл остаётся прежним.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import yaml

from utils import atomic_write_text

from hermes_cli.trix_config_sync import (
    _dominant_newline,
    _find_key_line,
    _indent_of,
    _read_raw_text,
)

BASELINE_STATE_FILENAME = "trix_config_baselines.json"

_SKIP_NOT_SCALAR = (
    "значение не скаляр — список или словарь одной строкой не переписать"
)
_SKIP_LINE_NOT_FOUND = "строку ключа не удалось найти в клиентском файле"
_SKIP_INLINE_PARENT = "родитель задан инлайн — спуститься к ключу нечем"
_SKIP_VERIFY_FAILED = (
    "переписанный файл не разобрался или изменил не только этот ключ — "
    "оставлен прежним"
)

# None намеренно считается скаляром: в конфиге он означает «не задано»
# (`display.streaming: None` — «следовать общей настройке»), и это
# полноценное умолчание, которое мы вправе поменять.
_SCALAR_TYPES = (str, int, float, bool, type(None))


def _is_scalar(value: Any) -> bool:
    # bool — подкласс int, отдельная ветка не нужна; проверка на
    # контейнеры важнее: пустой список тоже не скаляр.
    return isinstance(value, _SCALAR_TYPES)


def _state_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / BASELINE_STATE_FILENAME


def _load_state() -> dict:
    """Отсутствующий, битый или не-UTF-8 файл — не ошибка, а «ничего не помним».

    ``except Exception`` намеренно широкий, как и у досева: контракт
    «любая проблема с sidecar → пустая память», и эта функция вызывается
    до общего ``try`` ниже.
    """
    try:
        raw = _state_path().read_text(encoding="utf-8")
    except Exception:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _save_state(data: dict) -> None:
    """Best-effort: неудача записи не отменяет уже сделанную правку файла."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path,
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    except Exception:
        pass


def _scalar_paths(node: Any, prefix: tuple = ()) -> list:
    """Все пути шаблона, ведущие к скаляру. Порядок — обхода, он же порядок отчёта."""
    out = []
    if isinstance(node, dict):
        for key, value in node.items():
            out.extend(_scalar_paths(value, prefix + (str(key),)))
    elif prefix:
        out.append((prefix, node))
    return out


def _lookup(data: Any, path: tuple) -> tuple:
    """``(есть ли путь, значение)`` — отличает отсутствие от значения None."""
    node = data
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _dotted(path: tuple) -> str:
    return ".".join(path)


def _locate_line(lines: list, path: tuple) -> Optional[int]:
    """Номер строки, на которой задан ключ ``path``.

    Спуск идёт по КЛИЕНТСКОМУ тексту уровень за уровнем, сужая окно поиска
    границами родительского блока. Поиск по имени ключа без спуска нашёл бы
    первый попавшийся ``enabled`` — а их в конфиге десятки.
    """
    start, end = 0, len(lines)
    indent = 0
    idx = None
    for depth, key in enumerate(path):
        idx = _find_key_line(lines, key, indent, start, end)
        if idx is None:
            return None
        if depth == len(path) - 1:
            return idx
        # Родитель обязан быть голым блочным ключом: у `parent: {a: 1}`
        # спускаться некуда, значение задано целиком на той же строке.
        after_colon = lines[idx].split(":", 1)[1].strip()
        if after_colon and not after_colon.startswith("#"):
            return None
        start = idx + 1
        end = len(lines)
        for j in range(idx + 1, len(lines)):
            line = lines[j]
            if line.strip() and not line.lstrip().startswith("#"):
                if _indent_of(line) <= indent:
                    end = j
                    break
        indent = _indent_of(lines[idx]) + 1
        # Точный отступ детей берём с первой содержательной строки блока.
        for j in range(start, end):
            line = lines[j]
            if line.strip() and not line.lstrip().startswith("#"):
                indent = _indent_of(line)
                break
    return idx


def _dump_scalar(value: Any) -> str:
    """Скаляр в том виде, в каком его пишет YAML, без переводов строк."""
    text = yaml.safe_dump(
        value, default_flow_style=True, allow_unicode=True, width=10**6
    ).strip()
    # safe_dump скаляра даёт «value\n...» — хвост документа нам не нужен.
    if text.endswith("\n..."):
        text = text[: -len("\n...")]
    return text.strip()


def _rewrite_scalar(lines: list, idx: int, value: Any) -> list:
    """Заменить значение на строке ``idx``, сохранив отступ и хвостовой комментарий."""
    line = lines[idx]
    head, _, tail = line.partition(":")
    comment = ""
    stripped = tail.strip()
    if stripped.startswith("#"):
        comment = "  " + stripped
    else:
        # Хвостовой комментарий отделяем только по ` #`: решётка внутри
        # кавычек — часть значения, а не комментарий.
        marker = tail.find(" #")
        if marker != -1:
            comment = "  " + tail[marker:].strip()
    out = list(lines)
    out[idx] = f"{head}: {_dump_scalar(value)}{comment}"
    return out


def _verify(new_text: str, before: dict, expected: dict) -> bool:
    """Разобрался ли итог и изменилось ли ровно ожидаемое."""
    try:
        after = yaml.safe_load(new_text)
    except Exception:
        return False
    if not isinstance(after, dict):
        return False
    for path, value in expected.items():
        found, got = _lookup(after, path)
        if not found or got != value:
            return False
    # Ничего, кроме ожидаемого, измениться не должно.
    for path, value in _scalar_paths(before):
        if path in expected:
            continue
        found, got = _lookup(after, path)
        if not found or got != value:
            return False
    return True


def apply_default_changes(config_path: Path, template_path: Path) -> tuple:
    """Довести до клиента смену НАШИХ умолчаний, не тронув его правок.

    Возвращает ``(updated, skipped)``: ``updated`` — точечные пути, чьё
    значение приведено к новому умолчанию; ``skipped`` — пары
    ``(путь, причина)``. Любая ошибка (нечитаемый шаблон, защищённый от
    записи конфиг, неразбираемый итог) означает «файл не тронут» и пустой
    ``updated`` — как и у досева.
    """
    config_path = Path(config_path)
    template_path = Path(template_path)

    try:
        client_text = _read_raw_text(config_path)
        template_data = yaml.safe_load(_read_raw_text(template_path))
        client_data = yaml.safe_load(client_text)
    except Exception:
        return [], []
    if not isinstance(template_data, dict) or not isinstance(client_data, dict):
        return [], []

    state = _load_state()
    updated: list = []
    skipped: list = []
    pending: dict = {}
    new_state = dict(state)

    for path, template_value in _scalar_paths(template_data):
        found, client_value = _lookup(client_data, path)
        if not found:
            # Отсутствующее дописывает trix_config_sync — не наша работа.
            continue
        key = _dotted(path)
        record = state.get(key)

        if record is None:
            # Первая встреча: гадать, кто менял, нельзя.
            new_state[key] = (
                {"shipped": template_value}
                if client_value == template_value
                else {"owned_by_client": True}
            )
            continue

        if record.get("owned_by_client"):
            continue

        if "shipped" not in record:
            new_state[key] = {"owned_by_client": True}
            continue

        shipped = record["shipped"]
        if client_value != shipped:
            # Клиент правил ключ после того, как мы его отгрузили. Навсегда его.
            new_state[key] = {"owned_by_client": True}
            continue
        if template_value == shipped:
            continue
        if not _is_scalar(template_value) or not _is_scalar(client_value):
            skipped.append((key, _SKIP_NOT_SCALAR))
            continue
        pending[path] = template_value

    if not pending:
        _save_state(new_state)
        return updated, skipped

    if not os.access(config_path, os.W_OK):
        # Клиент защитил файл руками — уважаем, как это делает досев.
        _save_state(new_state)
        return [], skipped

    newline = _dominant_newline(client_text)
    lines = client_text.splitlines()
    applied: dict = {}
    for path, value in pending.items():
        idx = _locate_line(lines, path)
        if idx is None:
            skipped.append((_dotted(path), _SKIP_LINE_NOT_FOUND))
            continue
        lines = _rewrite_scalar(lines, idx, value)
        applied[path] = value

    if not applied:
        _save_state(new_state)
        return [], skipped

    trailing = newline if client_text.endswith(("\n", "\r")) else ""
    new_text = newline.join(lines) + trailing

    if not _verify(new_text, client_data, applied):
        for path in applied:
            skipped.append((_dotted(path), _SKIP_VERIFY_FAILED))
        _save_state(new_state)
        return [], skipped

    try:
        atomic_write_text(config_path, new_text)
    except Exception:
        _save_state(new_state)
        return [], skipped

    for path, value in applied.items():
        key = _dotted(path)
        updated.append(key)
        new_state[key] = {"shipped": value}
    _save_state(new_state)
    return updated, skipped


def updated_summary(updated: list, *, max_items: int = 6) -> str:
    """Человеческая строка для отчёта обновления. Пусто — пустая строка."""
    if not updated:
        return ""
    shown = ", ".join(updated[:max_items])
    if len(updated) > max_items:
        shown += f" и ещё {len(updated) - max_items}"
    return f"настройки приведены к новым умолчаниям: {shown}"
