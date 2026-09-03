"""Досеивает недостающие секции клиентского шаблона в уже существующий
``config.yaml`` (спека 9, задача 11).

**Почему нужен этот модуль.** Установщик и ``doctor --fix`` кладут
``assets/config/trix-config.yaml`` только когда ``config.yaml`` у клиента
ОТСУТСТВУЕТ — уже существующий файл никто и никогда не трогает. Значит всё,
что спека 9 добавила в шаблон (``display``, ``approvals``, ``platform_hints``,
``gateway``, ``terminal.docker_extra_args`` и т.д.), не доезжает ни до одной
уже установленной машины.

**Почему врезка построчная, а не слияние словарей.** Комментарии — русская
документация настроек — живут только в ТЕКСТЕ шаблона, а не в разобранном
``dict``. Любой путь через ``yaml.safe_load`` + ``yaml.safe_dump`` (или
слияние словарей с последующей сериализацией) дописал бы недостающий ключ
без единого слова объяснения, ради которого клиентский конфиг вообще
существует, а заодно переформатировал бы весь файл и стёр КАЖДЫЙ
существующий комментарий. Поэтому :func:`sync_missing_client_sections`
работает с текстом файла напрямую: находит недостающий путь через
``yaml.safe_load`` (сравнение по ключам — значение клиента всегда
побеждает), а сам блок для вставки вырезает из ТЕКСТА шаблона вместе с его
комментариями и вставляет в текст клиентского файла, не трогая ни одной
существующей строки.

Владелец сформулировал это как жёсткое требование: старый файл обязан быть
подпоследовательностью нового (см. ``test_not_a_single_existing_line_changes``
в ``tests/hermes_cli/test_trix_config_sync.py``) — diff содержит только
добавления, ни одного удаления и ни одного изменения строки. Это относится и
к байтам, не только к содержимому строк: перевод строки клиентского файла
(``\\r\\n`` или ``\\n``) и присутствие/отсутствие завершающего перевода строки
сохраняются как есть — вставленные блоки склеиваются ДОМИНИРУЮЩИМ
разделителем клиентского файла, а не жёстко зашитым ``\\n``.

Поддерживается глубина в два уровня: отсутствующий корневой ключ (весь его
блок дописывается в конец ДОКУМЕНТА — см. :func:`_root_insert_index`, это
не всегда конец файла) и отсутствующий ключ второго уровня внутри
уже существующего у клиента родителя (блок вставляется сразу после
последней строки родительского блока). Этого достаточно для всего, что
добавляет спека 9. Путь, который не вставился — глубже двух уровней, или
вставка была бы небезопасна текстово — не вставляется наугад: он уходит в
``skipped`` вместе с ПРИЧИНОЙ (см. :data:`_SKIP_TOO_DEEP`,
:data:`_SKIP_INLINE_PARENT`, :data:`_SKIP_AMBIGUOUS_INDENT`), чтобы
вызывающий код мог её показать, а не только назвать путь.

**Инвариант, ради которого написан весь этот раздел: НИКОГДА НЕ
ПРОТИВОРЕЧИТЬ УЖЕ СУЩЕСТВУЮЩЕЙ ФОРМЕ КЛИЕНТА.** Формулировка важна
дословно, потому что три круга ревью подряд она звучала иначе — «все
решения о форме выводятся ИЗ КЛИЕНТСКОГО ФАЙЛА» — и в таком виде
разрешала брать решение из ЛЮБОЙ строки клиентского файла. Ею стала
строка-комментарий: :func:`_client_child_indent` считала минимальный
отступ по всем непустым строкам блока, комментарии включительно, и на
обычном двухпробельном файле с одним криво отбитым комментарием
(``display:`` → `` # note`` → ``  tool_progress: "off"``) вставка уходила
на отступ комментария, после чего ``yaml.safe_load`` падал и конфиг
клиента переставал читаться вообще. Отступ комментария в YAML не значит
НИЧЕГО — парсер его не видит, — то есть комментарий формы не несёт и
противоречить ему нечему. Из правильной формулировки это следует сразу, из
прежней — нет.

Из инварианта в его нынешнем виде однообразно решаются все три случая,
которые раньше требовали отдельных правил:

- у клиентского блока есть НАСТОЯЩИЕ дети (содержательные строки — не
  комментарии и не пустые) → форма есть, отступ берётся из них
  (:func:`_client_child_indent`), а блок шаблона переотбивается
  (:func:`_reindent_block`) под клиентскую дельту;
- блок пуст или содержит ТОЛЬКО комментарии → формы нет, противоречить
  нечему → заполняем на отступе шаблона (это сознательно отменяет решение
  круга 3, отправлявшее пустой блок в ``skipped``: тогда причиной было
  «отступ вывести не из чего», а верная причина — «выводить нечего,
  потому что и противоречить нечему»);
- у родителя есть INLINE-значение (``display: null``, ``~``, ``{}``,
  ``{a: 1}``, любой скаляр) → форма ЕСТЬ, и она вставке противоречит:
  значение ключа уже полностью задано правее двоеточия → ``skipped``
  (:func:`_is_safe_block_parent_line`, круг 2).

Тем же инвариантом решается и ПОЗИЦИЯ вставки корневого блока: клиентский
файл, заканчивающийся маркером конца документа (``...``), задаёт форму
«документ здесь закончился», и дописывание секций ПОСЛЕ маркера сделало бы
из одного документа два (``expected '<document start>'``). Поэтому корневой
блок вставляется ПЕРЕД маркером (:func:`_root_insert_index`), а не в
физический конец файла.

Прочие решения о форме, выведенные из клиента, — разделитель строк
(:func:`_dominant_newline`), наличие/отсутствие завершающего перевода
строки, позиция вставки на втором уровне (конец блока родителя В
КЛИЕНТСКОМ ТЕКСТЕ). Общий симптом всех четырёх пойманных регрессий был
один и тот же: ``yaml.safe_load`` после досева бросает ``expected <block
end>, but found ...``/``expected '<document start>'`` и файл клиента не
читается вообще никем. Следующая непредвиденная форма клиентского файла
должна ловиться этим же инвариантом: любая новая проверка, которая решает
«как и куда записать», обязана спросить «есть ли у клиента форма, которой
это противоречит», а форму искать только в том, что парсер реально видит —
не в комментариях и не в пустых строках.

**Последний рубеж — проверка РЕЗУЛЬТАТА перед записью.** Всё, что описано
выше, — построчный сканер, и у него есть предел, который никакой новой
регуляркой не сдвинуть: сканер может разойтись с парсером в том, КАКАЯ
СТРОКА ВЛАДЕЕТ КЛЮЧОМ. Продолжение многострочного кавычечного скаляра,
стоящее на колонке 0, текстом неотличимо от корневого ключа::

    agent:
      system_prompt_extra: "Ты Trix.
    web:
    Всегда отвечай по-русски."
    web:
      search_backend: ddgs

``web:`` на третьей строке принадлежит СТРОКЕ, но сканер видит корневой
ключ и врезает в него. С двойными кавычками файл после этого не парсится;
с одинарными — хуже: файл парсится, а значение клиента молча переписано, и
этого не видит ни инвариант «ни одна строка не изменилась» (строки-то все
на месте), ни свёртка по формам клиентского файла.

Инвариант «не противоречить существующей форме» здесь не спасает сам по
себе: он применяется к той форме, которую УВИДЕЛ СКАНЕР, и не отвечает на
вопрос, ту ли строку имел в виду парсер. Поэтому перед записью
:func:`_verify` разбирает получившийся текст обратно и сверяет с тем, что
у клиента было: каждый ключ обязан уцелеть, каждое значение — совпасть
дословно (:func:`_preserves_client_values`). Не сошлось — не записывается
НИЧЕГО, а пути уходят в ``skipped`` с ``_SKIP_VERIFY_FAILED``. Отдельно
проверяется, что врезанный путь действительно доехал до разобранного
конфига (:func:`_path_exists`): врезка, легшая в текстовый блок, которым
парсер не владеет (дублированный корневой ключ), ничего не меняет — такой
путь исключается и делается второй проход, а в ``skipped`` уходит
``_SKIP_NO_EFFECT``.

Это единственная проверка в модуле, которая смотрит на РЕЗУЛЬТАТ, а не на
текст, — и единственная, которая поймала бы все четыре предыдущих круга
(разделитель строк, инлайн-родитель, отступ детей, маркер конца
документа), не расширяя ни одной регулярки. Шестую форму она поймает тоже,
какой бы та ни была: чтобы пройти мимо, врезка обязана оставить каждый
клиентский ключ и каждое клиентское значение дословно на месте — а это и
есть требование владельца целиком.

**Принятое ограничение.** Разделители строк только CR (классический Mac,
до OS X) не поддерживаются: :func:`_dominant_newline` различает ``\r\n``
и ``\n``, ``str.splitlines`` разобьёт CR-файл, и он будет пересобран с
``\n``. Чинить сознательно не стали — таких файлов в обиходе нет.

**Вставка второго уровня возможна ТОЛЬКО под «голым» блочным ключом.**
``yaml.safe_load`` разбирает ``display: null``, ``display: ~``,
``display: {}`` и ``display: {cleanup_progress: true}`` как словарь/None
ровно так же, как ``display:`` без ничего после двоеточия — но текстово
это разные строки. Вставить блочных потомков (строки с бо́льшим отступом)
под строкой, где справа от двоеточия уже стоит скаляр/``null``/``~``/
flow-отображение, — невалидный YAML (``expected <block end>, but found
...``): значение ключа уже полностью задано на этой же строке. Поэтому
перед вставкой на второй уровень проверяется сама строка родителя В
КЛИЕНТСКОМ ТЕКСТЕ (:func:`_is_safe_block_parent_line`) — она обязана
заканчиваться двоеточием и, самое большее, пробелами/комментарием.
Если нет — путь уходит в ``skipped`` с причиной ``_SKIP_INLINE_PARENT``,
файл не трогается.

**Отметки о досеянном (anti-resurrection).** Признак «нужно дописать» — это
отсутствие ключа у клиента. Без доп. состояния это ловит и намеренное
удаление: клиент стёр `container_memory`, которую мы же и дописали в
прошлый раз, — и следующий `doctor --fix`/`hermes update` тут же вписывает
её обратно, бесконечно воюя с клиентом. Решение — sidecar-файл под
``get_hermes_home()`` с отметкой «путь X дописан тогда-то»: путь, УЖЕ
отмеченный как дописанный, при повторном отсутствии больше не
восстанавливается. Список путей спеки 9 не зашивается явно — это было бы
вторым местом, которое пришлось бы не забыть обновить при каждой будущей
правке шаблона (ровно тот парный дефект, который мы и чиним). Отметка
самоподдерживающаяся и переживает собственное отсутствие/порчу так же
мягко, как сам досев.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from utils import atomic_write_text

_COMMENT_RE = re.compile(r"^\s*#")
# Публичное имя: doctor.py импортирует его через границу модуля, чтобы
# показать клиенту, где лежит sidecar-отметка. Приватное имя за этой
# границей было бы просто нарушением инкапсуляции без выгоды.
SEEDED_STATE_FILENAME = "trix_config_sync_state.json"

# Единственный досеиваемый путь, который config.yaml'ом НЕ ограничивается:
# ``-p`` докера подставляется только при СОЗДАНИИ контейнера
# (``tools/environments/docker.py``), а песочница у клиента долгоживущая.
DOCKER_PORTS_PATH = "terminal.docker_extra_args"

_SANDBOX_RECREATE_NOTICE = (
    "Порты для показа сделанного дописаны в config.yaml, но в уже работающей "
    "песочнице их нет: докер открывает порты только при создании контейнера. "
    "Подсказка агенту при этом действует сразу — он начнёт называть "
    "собеседнику публичный адрес, который не откроется. Пересоздайте "
    "песочницу (рабочая папка не пострадает — она лежит на диске машины, а не "
    "в контейнере)."
)


def sandbox_recreate_notice(added) -> Optional[str]:
    """Текст «пересоздайте песочницу», если досев тронул порты, иначе None.

    Один текст на обе точки подключения (``hermes update`` и
    ``hermes doctor --fix``): расхождение формулировок между ними — это
    расхождение в том, что клиенту сказали про одно и то же состояние.
    """
    return _SANDBOX_RECREATE_NOTICE if DOCKER_PORTS_PATH in (added or ()) else None

# Причины, по которым путь ушёл в `skipped` вместо вставки. Внешним кодом
# (doctor.py, update_cmd.py) печатаются как есть — по-русски, без
# дальнейшей расшифровки.
_SKIP_TOO_DEEP = "путь глубже двух уровней — вставка на такой глубине не поддерживается"
_SKIP_INLINE_PARENT = "у родителя в клиентском файле инлайн-значение, а не голый блочный ключ"
_SKIP_AMBIGUOUS_INDENT = "отступ детей нельзя надёжно вывести из клиентского файла"
_SKIP_PARENT_NOT_FOUND = (
    "родителя не удалось найти в тексте клиентского файла — форма строки "
    "ключа не распознана (пробел перед двоеточием, кавычки, BOM?)"
)
_SKIP_NOT_IN_TEMPLATE = "блок не удалось вырезать из текста шаблона"
_SKIP_DELETED_BY_CLIENT = "уже дописывался ранее и был удалён клиентом — не воскрешаем"
_SKIP_VERIFY_FAILED = (
    "проверка результата перед записью не прошла: врезка изменила бы то, "
    "что у клиента уже было — не записано ничего"
)
_SKIP_NO_EFFECT = (
    "врезка не изменила бы разобранное значение (текстовый блок не тот, "
    "которым владеет парсер) — не записано ничего"
)

# Строка, похожая на обычный YAML-ключ блочного отображения ("key:" или
# "key: value"). Используется, чтобы отличить настоящих потомков-ключей от
# flow-отображения/элемента списка/чего угодно ещё, продолжающего значение
# родителя на следующей строке той же вложенности.
_KEY_LINE_RE = re.compile(r"^[A-Za-z0-9_.\-]+:(\s|$)")

# Маркер конца документа YAML: "..." на нулевом отступе (допускается
# хвостовой комментарий). Всё, что дописано ПОСЛЕ него, — уже второй
# документ в потоке, и ``yaml.safe_load`` на таком файле падает с
# ``expected '<document start>'``. Клиентский файл, который им
# заканчивается, задаёт форму «документ здесь закончился», и корневые
# блоки обязаны вставляться ПЕРЕД маркером.
_DOC_END_RE = re.compile(r"^\.\.\.\s*(#.*)?$")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find_key_line(lines: list, key: str, indent: int, start: int, end: int) -> Optional[int]:
    """Найти строку ``key:`` на заданном отступе в ``lines[start:end]``."""
    pattern = re.compile(r"^" + " " * indent + re.escape(str(key)) + r":(\s|$)")
    for i in range(start, end):
        if pattern.match(lines[i]):
            return i
    return None


def _block_extent(lines: list, key_idx: int, indent: int) -> tuple:
    """Вернуть ``(start, end)`` блока, начинающегося строкой ``lines[key_idx]``.

    ``start`` включает непосредственно предшествующие строки-комментарии на
    том же отступе (без разрыва пустой строкой). ``end`` — индекс,
    исключающий, после последней строки с бо́льшим отступом (пустые строки
    внутри блока сохраняются, а пустая строка перед следующим блоком — нет).
    """
    start = key_idx
    i = key_idx - 1
    while i >= 0:
        line = lines[i]
        stripped = line.strip()
        if stripped and _COMMENT_RE.match(line) and _indent_of(line) == indent:
            start = i
            i -= 1
            continue
        break

    last_content = key_idx
    i = key_idx + 1
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip() == "":
            i += 1
            continue
        if _indent_of(line) > indent:
            last_content = i
            i += 1
            continue
        break

    return start, last_content + 1


def _child_indent(lines: list, parent_idx: int, parent_end: int) -> Optional[int]:
    """Отступ прямых потомков блока, начинающегося в ``parent_idx``."""
    for i in range(parent_idx + 1, parent_end):
        line = lines[i]
        if line.strip() == "":
            continue
        return _indent_of(line)
    return None


def _is_safe_block_parent_line(line: str, key: str, indent: int) -> bool:
    """True когда ``line`` — «голый» блочный ключ, под который можно
    безопасно вставить блочных потомков (строки с бо́льшим отступом).

    ``display: null``, ``display: ~``, ``display: {}`` и
    ``display: {cleanup_progress: true}`` — всё это валидные строки, где
    значение ключа уже полностью задано ПРАВЕЕ двоеточия на этой же
    строке. Дописать под такой строкой ещё и блочных потомков —
    невалидный YAML (``expected <block end>, but found ...``): парсер
    видит скаляр/flow-отображение, а затем внезапный более глубокий
    отступ, который к этому значению не относится. Безопасно вставлять
    можно только когда справа от двоеточия нет ничего, кроме пробелов и,
    может быть, комментария — то есть когда сама строка уже говорит
    «значение — ниже, в виде блока».
    """
    pattern = re.compile(r"^" + " " * indent + re.escape(str(key)) + r":\s*(#.*)?$")
    return bool(pattern.match(line))


def _is_shape_bearing(line: str) -> bool:
    """True для строки, которая ДЛЯ ПАРСЕРА что-то значит.

    Пустая строка и строка-комментарий формы не несут: YAML-парсер их не
    видит вовсе, отступ комментария не обязан совпадать ни с чем и ничего
    не задаёт. Это ровно то место, где неточная формулировка инварианта
    («решение выводится из клиентского файла») позволяла комментарию стать
    источником решения о форме — и ломала конфиг клиента.
    """
    return line.strip() != "" and not _COMMENT_RE.match(line)


def _client_block_has_shape(lines: list, parent_idx: int, parent_end: int) -> bool:
    """Есть ли у клиентского блока форма, которой вставка может противоречить.

    Форму задают только содержательные строки. Блок пустой или состоящий
    ТОЛЬКО из комментариев формы не имеет — противоречить нечему, значит
    заполнять его безопасно (на отступе шаблона).
    """
    return any(_is_shape_bearing(lines[i]) for i in range(parent_idx + 1, parent_end))


def _client_child_indent(lines: list, parent_idx: int, parent_end: int) -> Optional[int]:
    """Отступ прямых потомков — выведенный из СОДЕРЖАТЕЛЬНЫХ строк клиентского блока.

    Вызывается только когда :func:`_client_block_has_shape` истинна, то
    есть содержательная строка в блоке точно есть.

    Комментарии и пустые строки в расчёт НЕ берутся: отступ комментария в
    YAML не значит ничего, и минимальный отступ, посчитанный вместе с
    комментариями, уводил вставку на уровень, которого у детей нет —
    ``display:`` → `` # note`` → ``  tool_progress: "off"`` давал 1 вместо
    2, после чего ``yaml.safe_load`` падал на всём файле.

    Возвращает ``None``, когда форма есть, но вставке она противоречит:

    - минимальный отступ содержательных строк равен 0. Родитель у всех
      вызывающих сидит на отступе 0 (глубина ограничена двумя уровнями),
      так что «потомок» на отступе 0 потомком быть не может. Это же
      значение даёт строка с ТАБУЛЯЦИЕЙ: :func:`_indent_of` считает только
      ведущие пробелы, поэтому табулированная строка молча читается как 0 —
      отказ от нуля превращает это в честное «вывести нельзя» вместо
      ошибочного «отступ 0 годится»;
    - хотя бы одна содержательная строка на минимальном отступе не похожа
      на обычный ``key:`` (например ``{cleanup_progress: true}`` —
      flow-отображение, продолжающее значение родителя на следующей
      строке, а не блочный потомок; или элемент списка ``- ...``).

    Более глубоко вложенные строки (списки, вложенные словари под одним из
    потомков) отступ не портят и не проверяются — они часть значения
    потомка, а не сами потомки.
    """
    min_indent = None
    for i in range(parent_idx + 1, parent_end):
        line = lines[i]
        if not _is_shape_bearing(line):
            continue
        indent = _indent_of(line)
        if min_indent is None or indent < min_indent:
            min_indent = indent
    if not min_indent:
        return None

    for i in range(parent_idx + 1, parent_end):
        line = lines[i]
        if not _is_shape_bearing(line) or _indent_of(line) != min_indent:
            continue
        if not _KEY_LINE_RE.match(line.strip()):
            return None
    return min_indent


def _root_insert_index(lines: list) -> int:
    """Индекс, ПЕРЕД которым дописывается новый корневой блок.

    Обычно это физический конец файла. Но если клиентский документ
    закрыт маркером ``...``, то конец файла — уже за пределами документа:
    дописанная там секция стала бы вторым документом в потоке, и
    ``yaml.safe_load`` перестал бы читать файл целиком. Позиция вставки —
    такое же решение о форме, как отступ и разделитель строк, и выводится
    она из структуры клиентского документа, а не из привычки «дописываем в
    конец».

    Индекс не кэшируется вызывающим кодом, а пересчитывается перед каждой
    вставкой: врезка второго уровня могла сдвинуть маркер.
    """
    for i, line in enumerate(lines):
        if _DOC_END_RE.match(line):
            return i
    return len(lines)


def _reindent_block(block: list, from_indent: int, to_indent: int) -> list:
    """Переотбить строки блока, вырезанного из шаблона, под клиентский отступ.

    Сдвигает КАЖДУЮ непустую строку на одну и ту же дельту
    (``to_indent - from_indent``) — относительная вложенность внутри блока
    (списки, более глубоко вложенные ключи) сохраняется, потому что все
    строки блока изначально отступлены не меньше, чем ``from_indent``.
    Пустые строки не трогаются.
    """
    delta = to_indent - from_indent
    if delta == 0:
        return list(block)
    result = []
    for line in block:
        if line.strip() == "":
            result.append(line)
            continue
        new_indent = max(0, _indent_of(line) + delta)
        result.append(" " * new_indent + line.lstrip(" "))
    return result


def _path_exists(data, parts: list) -> bool:
    """Разрешается ли путь (список ключей) в разобранном отображении."""
    cur = data
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _preserves_client_values(old, new) -> bool:
    """Уцелело ли ДОСЛОВНО всё, что у клиента уже было.

    Это единственная проверка в модуле, которая смотрит не на текст, а на
    РЕЗУЛЬТАТ: то, что получилось после врезки, разбирается обратно и
    сравнивается с тем, что клиент имел до неё. Каждый ключ обязан
    сохраниться, и каждое значение — совпасть дословно. Новые ключи
    появляться можно (ради них всё и затевается), менять существующие —
    нельзя.

    Единственное послабление: клиентский ``None`` (голый ``key:`` без
    содержимого) законно превращается в словарь — это и есть заполнение
    пустого блока. ``None`` -> скаляр/список по-прежнему считается порчей.

    Сравнение типов строгое (``type(old) is type(new)``), иначе ``True``
    прошло бы за ``1``, а ``1`` — за ``1.0``.
    """
    if isinstance(old, dict):
        if not isinstance(new, dict):
            return False
        for key, oval in old.items():
            if key not in new:
                return False
            if not _preserves_client_values(oval, new[key]):
                return False
        return True
    if old is None:
        return new is None or isinstance(new, dict)
    if type(old) is not type(new):
        return False
    return bool(old == new)


def _verify(new_text: str, client_data: dict, added: list):
    """Проверить РЕЗУЛЬТАТ врезки перед тем, как что-либо записать.

    Возвращает список путей, которые текстово врезаны, но до разобранного
    конфига не доехали (пустой список — всё в порядке), либо ``None``,
    если результат вообще нельзя принимать: не парсится, или врезка
    изменила что-то из того, что у клиента уже было.

    **Зачем эта проверка существует.** Всё остальное в модуле — построчный
    сканер, и он может разойтись с парсером в том, КАКАЯ строка владеет
    ключом. Многострочный кавычечный скаляр, продолжение которого стоит на
    колонке 0, текстом неотличим от корневого ключа::

        agent:
          system_prompt_extra: "Ты Trix.
        web:
        Всегда отвечай по-русски."
        web:
          search_backend: ddgs

    ``web:`` на второй строке принадлежит СТРОКЕ, а сканер видит корневой
    ключ и врезает в него. С двойными кавычками файл после этого не
    парсится; с одинарными — парсится, но значение клиента молча
    переписано, и ни «ни одна строка не изменилась», ни свёртка по формам
    этого не видят: строки-то все на месте.

    Ловить такое шестым текстовым спецкейсом бессмысленно — форм всегда
    найдётся ещё одна. Ловится это только разбором того, что получилось, и
    сверкой с тем, что было. Эта же проверка поймала бы все четыре
    предыдущих круга (разделитель, инлайн-родитель, отступ, маркер
    документа), не расширяя ни одной регулярки.
    """
    try:
        new_data = yaml.safe_load(new_text)
    except Exception:
        return None
    if not isinstance(new_data, dict):
        return None
    if not _preserves_client_values(client_data, new_data):
        return None
    return [path for path in added if not _path_exists(new_data, path.split("."))]


def _collect_missing_paths(tdata: dict, cdata, prefix: str = "") -> list:
    """Пути (через точку), присутствующие в ``tdata``, но не в ``cdata``.

    Ключевое свойство: рекурсия спускается в подсловарь только когда ключ
    ЕСТЬ у клиента и является словарём и там, и там — а значит для любого
    возвращённого пути глубины >= 2 гарантированно, что все его предки уже
    существуют у клиента как словари. Отсутствующий целиком родитель
    возвращается ОДНИМ путём (без рекурсии внутрь) — его блок вставляется
    целиком, как единое целое.

    Клиентский ``None`` на месте словаря (``display:`` без содержимого)
    трактуется как пустой словарь — иначе пустая секция блокировала бы
    досев всего, что должно быть под ней.
    """
    missing = []
    if not isinstance(cdata, dict):
        cdata = {}
    for key, tval in tdata.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in cdata:
            missing.append(path)
            continue
        if isinstance(tval, dict):
            cval = cdata.get(key)
            if cval is None:
                cval = {}
            if not isinstance(cval, dict):
                # Клиент переопределил структуру (у него скаляр/список там,
                # где шаблон ожидает словарь) — прививать в это нечего и
                # опасно, оставляем как есть целиком.
                continue
            missing.extend(_collect_missing_paths(tval, cval, path))
        # tval не словарь и ключ у клиента есть — лист уже на месте.
    return missing


def _dominant_newline(text: str) -> str:
    """Разделитель строк, преобладающий в тексте — ``"\\r\\n"`` или ``"\\n"``."""
    crlf = text.count("\r\n")
    lf_only = text.count("\n") - crlf
    return "\r\n" if crlf > lf_only else "\n"


def _read_raw_text(path: Path) -> str:
    """Прочитать файл, сохранив исходные символы конца строки буквально.

    ``Path.read_text()`` в универсальном текстовом режиме превращает
    ``\\r\\n`` в ``\\n`` на чтении — после этого различить, каким
    разделителем пользовался клиентский файл, уже нельзя. ``newline=""``
    отключает это преобразование.
    """
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _seeded_state_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / SEEDED_STATE_FILENAME


def _load_seeded_paths() -> dict:
    """Пути, уже дописанные досевом ранее — их отсутствие больше не чинится.

    Отсутствующий, битый, нечитаемый или НЕ-UTF-8 файл — не ошибка: значит,
    ничего ещё не отмечено, досев продолжает работать как обычно (просто
    без защиты от воскрешения удалённого клиентом ключа на этот конкретный
    прогон). ``except Exception`` намеренно широкий — контракт этой функции
    «любая проблема с sidecar-файлом -> {}», и она вызывается ДО общего
    ``try`` в :func:`sync_missing_client_sections`, так что любое
    исключение отсюда иначе улетело бы наружу и уронило бы весь досев
    целиком, а не только защиту от воскрешения.
    """
    path = _seeded_state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_seeded_paths(data: dict) -> None:
    """Best-effort — неудача не должна отменять уже сделанный досев."""
    try:
        path = _seeded_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path,
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_missing_client_sections(config_path: Path, template_path: Path) -> tuple:
    """Дописать в ``config_path`` то, чего нет, но есть в ``template_path``.

    Сравнение — по наличию ключа, не по значению: значение клиента всегда
    побеждает, дописывается только отсутствующее. Путь, однажды дописанный
    (см. модульный докстринг про sidecar-отметку), при последующем удалении
    клиентом больше не восстанавливается.

    Возвращает ``(added, skipped)``: ``added`` — список дописанных путей;
    ``skipped`` — список пар ``(путь, причина)`` для путей, которые НЕ
    вставлялись (глубже двух уровней; родитель с инлайн-значением; отступ
    детей нельзя надёжно вывести из клиентского файла — см. модульный
    докстринг и ``_SKIP_*`` константы). Оба пустых списка означает «всё на
    месте» (в том числе при любой ошибке — нечитаемый/неразбираемый шаблон,
    нечитаемый или защищённый от записи клиентский файл — файл в этом
    случае остаётся нетронутым).
    """
    config_path = Path(config_path)
    template_path = Path(template_path)

    try:
        client_text = _read_raw_text(config_path)
    except Exception:
        # НЕ ``except OSError``: не-UTF-8 клиентский файл бросает
        # ``UnicodeDecodeError`` — потомка ``ValueError``, а не ``OSError``, —
        # и этот вызов стоит ДО общего ``try`` ниже, так что исключение
        # улетело бы наружу. Контракт модуля «любая ошибка -> пустой
        # результат, файл не тронут» должен выполняться буквально для любой
        # ошибки. Ровно этот же пробел уже чинился в ``_load_seeded_paths``
        # (круг 2) — здесь он оставался.
        return [], []

    # Уважать защиту, которую клиент поставил руками: read-only файл не
    # переписывается, даже если каталог, где он лежит, доступен на запись
    # (атомарная замена создаёт временный файл в каталоге — ей достаточно
    # прав на каталог, а не на сам файл).
    if not os.access(config_path, os.W_OK):
        return [], []

    try:
        template_text = _read_raw_text(template_path)
    except Exception:
        return [], []

    try:
        client_data = yaml.safe_load(client_text)
    except yaml.YAMLError:
        return [], []
    try:
        template_data = yaml.safe_load(template_text)
    except yaml.YAMLError:
        return [], []

    if client_data is None:
        client_data = {}
    if not isinstance(client_data, dict) or not isinstance(template_data, dict):
        return [], []

    try:
        missing_paths = _collect_missing_paths(template_data, client_data)
    except Exception:
        return [], []

    if not missing_paths:
        return [], []

    template_lines = template_text.splitlines()
    client_sep = _dominant_newline(client_text)
    had_trailing_newline = client_text.endswith(("\n", "\r"))
    client_lines = client_text.splitlines()

    try:
        # Inside the try, not before it: _load_seeded_paths() is hardened
        # to never raise, but keeping the call inside this guard is a
        # second line of defense against exactly the "helper call sits
        # outside the general try" class of bug.
        seeded = _load_seeded_paths()

        def _splice(exclude: set) -> tuple:
            """Один проход врезки. Чистая функция от ``client_lines``: сам
            список не мутируется, возвращается новый.

            ``exclude`` — пути, которые этот проход не трогает вовсе.
            Используется вторым проходом, когда проверка результата
            показала, что какая-то врезка не дошла до парсера.
            """
            lines = list(client_lines)
            added = []
            skipped = []

            for path in missing_paths:
                if path in exclude:
                    continue
                parts = path.split(".")

                if len(parts) > 2:
                    skipped.append((path, _SKIP_TOO_DEEP))
                    continue

                if path in seeded:
                    # Клиент сознательно стёр то, что мы уже дописывали —
                    # уважаем это, не воскрешаем. Причина попадает в
                    # ``skipped``, а не теряется молча: без неё саппорт не
                    # может объяснить, почему настройка не возвращается.
                    skipped.append((path, _SKIP_DELETED_BY_CLIENT))
                    continue

                if len(parts) == 1:
                    key = parts[0]
                    root_idx = _find_key_line(template_lines, key, 0, 0, len(template_lines))
                    if root_idx is None:
                        skipped.append((path, _SKIP_NOT_IN_TEMPLATE))
                        continue
                    b_start, b_end = _block_extent(template_lines, root_idx, 0)
                    block = template_lines[b_start:b_end]
                    # Не «в конец файла», а «в конец ДОКУМЕНТА»: маркер `...`
                    # закрывает документ, и всё после него — второй документ.
                    insert_at = _root_insert_index(lines)
                    to_insert = list(block)
                    if insert_at > 0 and lines[insert_at - 1].strip() != "":
                        to_insert.insert(0, "")
                    lines[insert_at:insert_at] = to_insert
                    added.append(path)
                    continue

                # Второй уровень: parts == [key, subkey].
                # _collect_missing_paths гарантирует, что client_data[key]
                # уже существует как словарь (или как None, что мы трактуем
                # как пустой словарь) — но ТЕКСТОВО у клиента родитель может
                # быть чем угодно: голым блочным ключом, скаляром/null/~ на
                # той же строке, или flow-отображением ``{}``/``{...}``, или
                # с детьми, отбитыми другим отступом, чем в шаблоне. Каждая
                # из этих форм — своя причина для skipped, а форматирование
                # вставки (сам отступ) выводится ТОЛЬКО из клиентского
                # блока, никогда из шаблона.
                key, subkey = parts
                parent_idx_c = _find_key_line(lines, key, 0, 0, len(lines))
                if parent_idx_c is None:
                    # Разобранный конфиг говорит, что родитель есть, а
                    # построчный сканер его не нашёл: ``terminal :`` с
                    # пробелом перед двоеточием, ``"terminal":`` в кавычках,
                    # BOM в начале файла. Раньше это был молчаливый
                    # ``continue`` — оператор видел «конфигурация актуальна»
                    # при недоехавших настройках и без единой строчки
                    # диагностики.
                    skipped.append((path, _SKIP_PARENT_NOT_FOUND))
                    continue
                if not _is_safe_block_parent_line(lines[parent_idx_c], key, 0):
                    skipped.append((path, _SKIP_INLINE_PARENT))
                    continue

                _, parent_end_c = _block_extent(lines, parent_idx_c, 0)
                if _client_block_has_shape(lines, parent_idx_c, parent_end_c):
                    # У блока есть настоящие дети — их отступ и есть форма,
                    # которой вставка не имеет права противоречить.
                    client_child_indent = _client_child_indent(
                        lines, parent_idx_c, parent_end_c
                    )
                    if client_child_indent is None:
                        skipped.append((path, _SKIP_AMBIGUOUS_INDENT))
                        continue
                else:
                    # Блок пуст или в нём только комментарии — формы, которой
                    # можно противоречить, нет. Отступ комментария парсер не
                    # видит, поэтому решением он быть не может; берём отступ
                    # шаблона (ниже, `client_child_indent is None` ->
                    # переотбивка с нулевой дельтой).
                    client_child_indent = None

                parent_idx_t = _find_key_line(template_lines, key, 0, 0, len(template_lines))
                if parent_idx_t is None:
                    skipped.append((path, _SKIP_NOT_IN_TEMPLATE))
                    continue
                _, parent_end_t = _block_extent(template_lines, parent_idx_t, 0)
                template_child_indent = _child_indent(template_lines, parent_idx_t, parent_end_t)
                if template_child_indent is None:
                    skipped.append((path, _SKIP_NOT_IN_TEMPLATE))
                    continue
                sub_idx = _find_key_line(
                    template_lines, subkey, template_child_indent, parent_idx_t + 1, parent_end_t
                )
                if sub_idx is None:
                    skipped.append((path, _SKIP_NOT_IN_TEMPLATE))
                    continue
                sub_start, sub_end = _block_extent(template_lines, sub_idx, template_child_indent)
                block = template_lines[sub_start:sub_end]
                target_indent = (
                    template_child_indent if client_child_indent is None else client_child_indent
                )
                block = _reindent_block(block, template_child_indent, target_indent)

                lines[parent_end_c:parent_end_c] = block
                added.append(path)

            return lines, added, skipped

        def _render(lines: list) -> str:
            return client_sep.join(lines) + (client_sep if had_trailing_newline else "")

        # --- проход 1 --------------------------------------------------
        lines, added, skipped = _splice(set())
        if not added:
            return [], skipped

        # --- проверка РЕЗУЛЬТАТА, а не текста ---------------------------
        # Всё, что выше, — построчный сканер, и он в принципе может
        # разойтись с парсером в том, КАКАЯ строка владеет ключом:
        # продолжение многострочного кавычечного скаляра, стоящее на
        # колонке 0, текстом неотличимо от корневого ключа. Ни один
        # текстовый спецкейс этого класса не закрывает — закрывает только
        # разбор того, что получилось, и сверка с тем, что было.
        new_text = _render(lines)
        verified = _verify(new_text, client_data, added)
        if verified is None:
            return [], skipped + [(path, _SKIP_VERIFY_FAILED) for path in added]

        # --- проход 2, если какая-то врезка не дошла до парсера ---------
        ineffective = verified
        if ineffective:
            lines, added, skipped = _splice(set(ineffective))
            skipped += [(path, _SKIP_NO_EFFECT) for path in ineffective]
            if not added:
                return [], skipped
            new_text = _render(lines)
            verified = _verify(new_text, client_data, added)
            if verified is None or verified:
                # Второй проход тоже не сходится — больше не гадаем.
                return [], skipped + [(path, _SKIP_VERIFY_FAILED) for path in added]

        atomic_write_text(config_path, new_text, newline="", preserve_mode=True)

        # Отмечаем только то, что ДЕЙСТВИТЕЛЬНО доехало до разобранного
        # конфига: путь, чья врезка легла в текстовый блок, которым парсер
        # не владеет, доставленным не считается и остаётся кандидатом на
        # следующий прогон.
        newly_seeded = {path: _now_iso() for path in added}
        for path in added:
            if "." in path:
                continue
            tval = template_data.get(path)
            if isinstance(tval, dict):
                # Один уровень вложенности внутрь только что дописанного
                # корневого блока тоже отмечаем — иначе следующий прогон
                # увидит существующего родителя и попробует довставить его
                # подключ по отдельности (второй уровень), не зная, что тот
                # уже приезжал вместе с целым блоком и мог быть сознательно
                # удалён клиентом.
                for subkey in tval:
                    newly_seeded.setdefault(f"{path}.{subkey}", newly_seeded[path])

        if newly_seeded:
            seeded.update(newly_seeded)
            _save_seeded_paths(seeded)

        return added, skipped
    except Exception:
        # Досев вызывается из `hermes update` и `doctor --fix` — падение
        # здесь не имеет права ронять их. Файл ещё не тронут: единственная
        # запись (atomic_write_text) идёт только после того, как весь цикл
        # выше отработал без исключений.
        return [], []
