"""Правило Trix: в докер-песочнице спрашиваем клиента только про удаление.

Апстрим пропускает подтверждения целиком, когда терминал работает в
изолированном контейнере (``tools/approval.py::_should_skip_container_guards``)
— для ``chmod`` или ``curl | sh`` это правильно: ломать в контейнере нечего.
Но ``/workspace`` и ``/root`` — хостовые каталоги, примонтированные шлюзом;
``rm`` в них стирает данные клиента по-настоящему, а отката нет. Поэтому
удаляющие команды из пропуска исключаются и идут обычным путём подтверждения.

Пути не разбираем намеренно: рабочий каталог агента и есть ``/workspace``,
поэтому ``rm -rf старое/`` без всякого пути удаляет из рабочей папки.
Достаточно классифицировать саму команду.

Область — только ``docker``. ``singularity``/``modal``/``daytona``/
``vercel_sandbox`` — эфемерные удалённые песочницы без хостовых маунтов; там
подтверждение на удаление — чистый шум.

Что классифицирует этот модуль
------------------------------

**Терминал.** Команду целиком уже разбирает апстримный
``detect_dangerous_command``; нам остаётся сказать, «про удаление» ли
опознанный паттерн — ``is_sandbox_delete`` по описанию паттерна.

**Код.** ``execute_code`` (Python-скрипт) и однострочный payload после
``-c``/``-e`` апстримному детектору не видны: он — набор регулярок над
текстом ШЕЛЛ-команды и ничего не знает ни про ``shutil.rmtree``, ни про то,
что внутри ``os.system("…")`` лежит настоящая шелл-команда. Разбор кода —
три шага, ``detect_delete_in_code``:

1. **Префильтр по литеральным токенам** (``_DELETE_TOKENS``). Обычный скрипт
   без единого слова про удаление выходит здесь — за микросекунды и без
   единой регулярки по тексту.
2. **Один линейный проход** ``_split_code_and_literals`` делит текст на «код
   без строк и комментариев» и «строковые литералы».
3. **Две половины — разными инструментами.** По коду — идиомы того языка, на
   котором он написан (``DELETE_IDIOMS``). По каждому литералу —
   **апстримный** ``detect_dangerous_command``, по строке в десятки байт.

Третий шаг — суть конструкции. Раньше модуль вёз шесть собственных зеркал
апстримных шелл-паттернов (rm/xargs/find -exec/find -delete/git clean/git
reset) и собственный якорь на начало команды. Зеркала неизбежно отставали от
оригинала: ``os.system("sudo rm -rf /workspace")`` молчал, хотя ту же строку
в терминале апстрим спрашивал (его якорь знает про ``sudo``/``env``/``time``/
``nohup``/``exec``/``setsid``, а самодельный — нет). Теперь расхождение
«терминал спрашивает, ``execute_code`` молчит» структурно невозможно: обе
стороны судит ОДИН детектор, и любое его улучшение достаётся коду даром.

Цена шага 3 — не размер скрипта, а суммарная длина литералов, в которых
вообще встретилось слово про удаление: 18 КБ питоновского текста дорожали
именно потому, что скан шёл по всем 18 КБ.

Известные границы (осознанные, не пробелы):
- поиск по тексту, не по AST: ``self.unlink(node)`` в чужом смысле спросит;
- строковый литерал, ЦИТИРУЮЩИЙ опасную команду («Run: rm -rf /tmp/x»),
  спросит — ровно как та же строка в терминале; комментарии — не спросят;
- обфускация вида ``r""m -rf`` не переживёт префильтр (апстрим её умеет, но
  до него дело не дойдёт) — на терминальном пути она по-прежнему ловится.
"""

from __future__ import annotations

import re
from typing import Iterator

SANDBOX_DELETE_PATTERN_KEYS: frozenset[str] = frozenset({
    "delete in root path",
    "recursive delete",
    "recursive delete (long flag)",
    "recursive delete (flags after operands)",
    "xargs with rm",
    "find -exec/-execdir rm",
    "find -delete",
    "git reset --hard (destroys uncommitted changes)",
    "git clean with force (deletes untracked files)",
})

# Русские формулировки для окна подтверждения (задача 8). Ключи — те же.
SANDBOX_DELETE_RU: dict[str, str] = {
    "delete in root path": "удаление в корневом каталоге",
    "recursive delete": "удаление каталога вместе со всем содержимым",
    "recursive delete (long flag)": "удаление каталога вместе со всем содержимым",
    "recursive delete (flags after operands)": "удаление каталога вместе со всем содержимым",
    "xargs with rm": "массовое удаление найденных файлов",
    "find -exec/-execdir rm": "массовое удаление найденных файлов",
    "find -delete": "массовое удаление найденных файлов",
    "git reset --hard (destroys uncommitted changes)": "удаление незакоммиченных изменений",
    "git clean with force (deletes untracked files)": "удаление неотслеживаемых файлов",
}

# Описания, которые доезжают до окна подтверждения клиента, НО приходят не из
# таблицы паттернов апстрима, — поэтому им нет места в SANDBOX_DELETE_RU:
# ключи того словаря обязаны совпадать с SANDBOX_DELETE_PATTERN_KEYS (на этом
# стоят инварианты guard'а), а эти четыре описания (одно литералом ниже, три
# — привязкой к атрибутам ``tools.approval`` в ``_verdict_ru``) паттернами не
# являются вовсе. Отдельный словарь — не косметика: он держит два множества
# раздельными, вместо того чтобы размывать смысл первого.
#
# Все четыре лежат на самых вероятных путях удаления у клиента:
#   * execute_code — модель удаляет из питона чаще, чем через ``rm``;
#     тулсет code_execution у клиента включён;
#   * ``python -c "…rmtree…"`` — то же самое, но одной строкой в терминале;
#   * два вердикта «разобрать не смог» — длинный ``rm -rf`` с двумя сотнями
#     путей (ровно то, что модель пишет на «почисти вот эти файлы»); мы сами
#     пропускаем их наружу в ``_verdict_asks``.
#
# Формулировки — про ПОСЛЕДСТВИЕ для клиента, а не про механику: клиенту
# нечего делать со словами «parser limit» и «-c flag», ему надо понять, что
# именно исчезнет, если он нажмёт «Разрешить».
SANDBOX_VERDICT_RU: dict[str, str] = {
    "script execution via -e/-c flag": "запуск однострочной программы, которая удаляет файлы",
}


def _verdict_ru() -> dict[str, str]:
    """``SANDBOX_VERDICT_RU`` плюс строки, чьи имена принадлежат апстриму.

    Описания синтетических вердиктов и описание ``execute_code`` берём
    атрибутами ``tools.approval``, а не копиями строк: переименование
    наверху обязано ронять нас с AttributeError, а не тихо возвращать
    клиенту английский абзац. Ленивая функция, а не модульный словарь, —
    импорт ``tools.approval`` на уровне модуля закольцевал бы загрузку
    (approval сам импортирует этот guard).
    """
    from tools import approval

    long_delete = "массовое удаление: команда слишком длинная, чтобы разобрать её целиком"
    return {
        **SANDBOX_VERDICT_RU,
        approval._PARSER_LIMIT_DESCRIPTION: long_delete,
        approval._MALFORMED_EXEC_DESCRIPTION: (
            "удаление внутри команды, которую не удалось разобрать целиком"
        ),
        approval._EXECUTE_CODE_DESCRIPTION: (
            "запуск скрипта, который удаляет файлы"
        ),
    }


# Разделитель, которым ``tools/approval.py::check_all_command_guards`` склеивает
# описания ВСЕХ сработавших предупреждений в одну строку прямо перед отправкой
# клиенту (``"; ".join(...)``). Склейки нет ни в одном словаре — и не может
# быть: её состав зависит от команды. Поэтому переводим по частям.
_COMBINED_SEPARATOR = "; "


def _reason_part_ru(description: str) -> "str | None":
    """Русская формулировка ОДНОГО описания или ``None``, если её нет."""
    if description in SANDBOX_DELETE_RU:
        return SANDBOX_DELETE_RU[description]
    try:
        return _verdict_ru().get(description)
    except Exception:  # pragma: no cover — импорт апстрима не должен ронять окно
        return None


def client_reason_ru(description: str) -> str:
    """Русская формулировка причины для окна подтверждения клиента.

    Единственная точка, которую зовёт адаптер: ищет сначала среди описаний
    паттернов удаления, потом среди описаний, приходящих мимо таблицы
    паттернов. Незнакомое описание возвращается как есть — это путь
    оператора, где английский текст апстрима лучше приблизительного
    перевода.

    До клиента доезжает не только одиночное описание. Когда вместе с
    паттерном удаления срабатывает сканер Tirith (обычное дело на просьбе
    «прибери логи»: ``find … -delete`` видят оба), точка сборки склеивает их
    через ``"; "`` — и такой строки нет ни в одном словаре, поэтому клиент
    видел английский абзац сканера вместо фразы про последствие. Поэтому
    склейка разбирается по разделителю и переводится по частям; части, для
    которых перевода нет (находка сканера), остаются как есть — это тот же
    операторский путь, только внутри одной строки. Сборка обратно тем же
    разделителем побайтово обратима, так что непереведённые части не
    сдвигаются ни на байт.

    Поиск целой строки идёт ПЕРВЫМ и по-прежнему: описание ``execute_code``
    само содержит ``"; "``, и слепой разбор развалил бы его на два незнакомых
    куска.
    """
    whole = _reason_part_ru(description)
    if whole is not None:
        return whole
    if _COMBINED_SEPARATOR not in description:
        return description
    return _COMBINED_SEPARATOR.join(
        _reason_part_ru(part) or part
        for part in description.split(_COMBINED_SEPARATOR)
    )

# Апстримные паттерны DANGEROUS_PATTERNS, чьё описание говорит про удаление,
# но которые мы СОЗНАТЕЛЬНО не считаем sandbox-delete — с причиной для
# каждого. Двусторонний инвариант в тестах требует, чтобы каждый
# delete-подобный паттерн апстрима был либо в SANDBOX_DELETE_PATTERN_KEYS,
# либо здесь: иначе новый удаляющий паттерн апстрима молча проскочит мимо,
# как однажды уже проскочил "find -delete".
SANDBOX_DELETE_EXCLUDED_PATTERN_KEYS: dict[str, str] = {
    # Механизмы, которых нет в linux-образе докер-песочницы Trix.
    "Windows cmd destructive delete": "Windows-only (cmd.exe); докер-песочница — linux-контейнер, cmd.exe там не выполняется вовсе.",
    "Windows PowerShell destructive delete": "PowerShell (pwsh) кроссплатформенный, но его нет в образе докер-песочницы Trix.",
    "PowerShell destructive delete (Remove-Item)": "PowerShell (pwsh) кроссплатформенный, но его нет в образе докер-песочницы Trix.",
    "Windows destructive delete (recursive/quiet switch)": "Windows-only (cmd.exe); докер-песочница — linux-контейнер, cmd.exe там не выполняется вовсе.",
    "wipe disk (Clear-Disk)": "PowerShell-only, уровень диска, а не файла в /workspace; его нет в образе песочницы.",
    "wipe free space (cipher /w)": "Windows-only (cipher.exe); его нет в образе песочницы.",
    "delete volume shadow copies (vssadmin)": "Windows-only (vssadmin.exe); его нет в образе песочницы.",
    "delete backups (wbadmin)": "Windows-only (wbadmin.exe); его нет в образе песочницы.",
    "registry delete (reg delete)": "Windows-реестр — не файловая система /workspace; reg.exe нет в образе песочницы.",
    "registry value delete (Remove-ItemProperty -Force)": "PowerShell/Windows-реестр — не файловая система /workspace; его нет в образе песочницы.",
    "stop/delete service (sc)": "Windows service control (sc.exe) — не файловая система /workspace; его нет в образе песочницы.",
    # Данные в БД, а не файлы в хостовом bind-mount — вне модели угрозы.
    "SQL DELETE without WHERE": "стирает строки в БД, а не файлы в bind-mount каталоге /workspace.",
    "SQL TRUNCATE": "стирает строки в БД, а не файлы в bind-mount каталоге /workspace.",
    # git branch -D стирает ССЫЛКУ, не файлы; коммит остаётся в reflog.
    "git branch force delete": "удаляет git-ссылку (не файлы); восстановимо через reflog; рутинное действие.",
    "git branch force delete (long flags)": "удаляет git-ссылку (не файлы); восстановимо через reflog; рутинное действие.",
    "git branch force delete (long flags, force-first)": "удаляет git-ссылку (не файлы); восстановимо через reflog; рутинное действие.",
    # Не находятся keyword-свипом (в описании нет слова про удаление) —
    # записаны явным решением, чтобы это не выглядело дырой в обходе свипа.
    "overwrite project env/config via tee": "перезапись файла — обычная работа агента (правка конфига), не удаление; владелец решил не спрашивать про это в рамках задачи 7.",
    "overwrite project env/config via redirection": "перезапись файла — обычная работа агента (правка конфига), не удаление; владелец решил не спрашивать про это в рамках задачи 7.",
    "overwrite project env/config file": "перезапись файла (cp/mv/install) — обычная работа агента, не удаление; владелец решил не спрашивать про это в рамках задачи 7.",
    "format filesystem": "требует доступа к сырому блочному устройству — недостижимо в непривилегированном докер-контейнере без device passthrough.",
    "write to block device": "требует доступа к сырому блочному устройству — недостижимо в непривилегированном докер-контейнере без device passthrough.",
}


def is_sandbox_delete(pattern_key: str | None) -> bool:
    """True, если опознанный апстримом паттерн — про удаление данных.

    Ровно множество ключей, у которых есть русская формулировка в
    ``SANDBOX_DELETE_RU`` — задача 8 опирается на это соответствие.
    """
    return bool(pattern_key) and pattern_key in SANDBOX_DELETE_PATTERN_KEYS


# Апстрим умеет вернуть вердикт, означающий не «это команда X», а «разобрать
# не смог»: команда длиннее 4096 символов без разделителей или неразбираемый
# exec-payload. Он трактует это как fail-closed («fails closed rather than
# allowing an uninspected suffix to execute»), и эти описания в
# DANGEROUS_PATTERNS НЕ ЛЕЖАТ — значит двусторонний keyword-свип по таблице
# паттернов слеп к ним структурно, сколько его ни улучшай. Именно на этом
# ``rm -rf`` с двумя сотнями явных путей (6 КБ одной командой — ровно то, что
# модель пишет на «почисти вот эти файлы») проходил мимо подтверждения.
# Разрешаем их как «спрашивать», но только когда префильтр удаления сработал:
# иначе любая длинная безобидная однострочная команда начала бы спрашивать.
# Имена берём атрибутами апстрима, а не копией строк: переименование наверху
# уронит нас с AttributeError, а не тихо отключит правило.
_UNINSPECTABLE_VERDICTS: frozenset[str] | None = None


def uninspectable_verdicts() -> frozenset[str]:
    """Синтетические вердикты апстрима «разобрать не смог»."""
    global _UNINSPECTABLE_VERDICTS
    if _UNINSPECTABLE_VERDICTS is None:
        from tools import approval

        _UNINSPECTABLE_VERDICTS = frozenset({
            approval._PARSER_LIMIT_DESCRIPTION,
            approval._MALFORMED_EXEC_DESCRIPTION,
        })
    return _UNINSPECTABLE_VERDICTS


def _verdict_asks(pattern_key: str | None, text: str) -> bool:
    """Обязан ли этот апстримный вердикт дойти до подтверждения."""
    if is_sandbox_delete(pattern_key):
        return True
    return (
        bool(pattern_key)
        and pattern_key in uninspectable_verdicts()
        and _mentions_deletion(text)
    )


# --- шаг 1: префильтр ------------------------------------------------------
#
# Литеральные подстроки, без регулярок. Каждый токен обязан покрывать
# что-то конкретное, иначе он лишний:
#   "rm"     — rm/rmtree/rmdir/rmSync, xargs … rm, find -exec rm
#   "unlink" — os.unlink/Path.unlink/perl unlink/fs.unlinkSync
#   "remove" — os.remove/os.removedirs/File::Path::remove_tree
#   "delete" — find -delete
#   "clean"  — git clean -f*
#   "reset"  — git reset --hard
# Полнота проверяется тестом-инвариантом: каждый ключ из
# SANDBOX_DELETE_PATTERN_KEYS и каждая идиома обязаны содержать токен.
_DELETE_TOKENS: tuple[str, ...] = ("rm", "unlink", "remove", "delete", "clean", "reset")


def _mentions_deletion(text: str) -> bool:
    """Дешёвый префильтр: встречается ли в тексте хоть одно слово про удаление."""
    lowered = text.lower()
    return any(token in lowered for token in _DELETE_TOKENS)


# --- шаг 2: код отдельно, строковые литералы отдельно ----------------------
#
# Один линейный проход. Тройные кавычки — первыми, иначе одиночные съедят
# их начало. Комментарий (#…) распознаём, чтобы ВЫБРОСИТЬ: он не код и не
# шелл-строка, а самый частый источник ложных срабатываний.
_LITERAL_OR_COMMENT_RE = re.compile(
    r'"""[\s\S]*?"""'
    r"|'''[\s\S]*?'''"
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*'"
    r'|\#[^\n]*'
)


def _literal_body(token: str) -> str:
    if token[:3] in ('"""', "'''"):
        return token[3:-3]
    return token[1:-1]


def _split_code_and_literals(code: str) -> tuple[str, list[str]]:
    """Разделить текст на код (без строк и комментариев) и шелл-кандидатов.

    Кандидат — это содержимое строкового литерала. Идущие подряд литералы,
    разделённые только запятыми и пробелами, склеиваются через пробел: так
    ``subprocess.run(["rm", "-rf", "/workspace/x"])`` превращается в
    ``rm -rf /workspace/x`` — форму, которую апстримный детектор понимает.
    Без склейки argv-вызов был бы тремя безобидными строками (именно этот
    пробел раньше приходилось закрывать отдельной регуляркой).
    """
    code_parts: list[str] = []
    candidates: list[str] = []
    run: list[str] = []
    pos = 0

    for match in _LITERAL_OR_COMMENT_RE.finditer(code):
        gap = code[pos:match.start()]
        code_parts.append(gap)
        pos = match.end()
        token = match.group(0)
        # Всё, что не «только запятые и пробелы», обрывает серию литералов.
        if run and (token.startswith("#") or gap.strip(" \t\r\n,")):
            candidates.append(" ".join(run))
            run = []
        if not token.startswith("#"):
            run.append(_literal_body(token))

    code_parts.append(code[pos:])
    if run:
        candidates.append(" ".join(run))
    return " ".join(code_parts), candidates


# --- шаг 3: идиомы по языку + апстримный детектор по литералам -------------
#
# Идиомы разведены по семействам намеренно: раньше плоское множество
# обслуживало двух вызывающих с разными данными, и perl'ова голая форма
# ``unlink(`` ложно срабатывала на питоновском ``def unlink(x)``. Python-набор
# едет по большому скрипту, поэтому он самый узкий (только
# точка-квалифицированные вызовы); perl/node-наборы едут по однострочному
# ``-c``/``-e`` payload'у, где голое слово однозначно.
DELETE_IDIOMS: dict[str, frozenset[str]] = {
    # Python-набор — только точка-квалифицированные вызовы: .rmtree( ловит и
    # shutil.rmtree(, .unlink( / .rmdir( — и os.*, и pathlib. Голое имя сюда
    # не берём (своя функция `def unlink(x)` не должна спрашивать) — его
    # закрывает _imported_delete_call ниже.
    "python": frozenset({".rmtree(", "os.remove(", "os.removedirs(", ".unlink(", ".rmdir("}),
    "perl": frozenset({"unlink", "rmtree", "remove_tree", "rmdir"}),
    # node: рабочая однострочная форма всегда через require('fs') /
    # require('node:fs'), поэтому якоримся на имя метода, а не на "fs.".
    "node": frozenset({"rmsync", "unlinksync", "rmdirsync", ".rm(", ".rmdir("}),
}


def _match_idiom(lowered_code: str, family: str) -> str | None:
    for idiom in DELETE_IDIOMS.get(family, ()):
        if idiom in lowered_code:
            return f"{family}: {idiom}"
    return None


# `from os import unlink` / `from shutil import rmtree` — дальше вызов идёт по
# ГОЛОМУ имени, которого ни одна точечная идиома не видит. Голое имя само по
# себе брать нельзя: `def unlink(node)` — обычная своя функция. Берём ровно
# тогда, когда имя ИМПОРТИРОВАНО из модуля, где оно значит удаление файла.
_PYTHON_DELETE_NAMES = ("remove", "removedirs", "unlink", "rmdir", "rmtree")
_FROM_IMPORT_RE = re.compile(r'\bfrom\s+(?:os|shutil|pathlib)\s+import\s+([^\n]+)')
_BARE_CALL_RES = {
    name: re.compile(r'(?<![\w.])' + name + r'\s*\(') for name in _PYTHON_DELETE_NAMES
}


def _imported_delete_call(lowered_code: str) -> str | None:
    if "import" not in lowered_code:
        return None
    for match in _FROM_IMPORT_RE.finditer(lowered_code):
        for chunk in match.group(1).split(","):
            parts = chunk.strip().strip("()").split()
            name = parts[0] if parts else ""
            if name in _BARE_CALL_RES and _BARE_CALL_RES[name].search(lowered_code):
                return f"python: from-import {name}("
    return None


# Второй, более узкий фильтр — уже по короткому литералу, перед вызовом
# апстримного детектора. Все девять паттернов из SANDBOX_DELETE_PATTERN_KEYS
# начинаются с \brm, \bxargs …\brm, \bfind или \bgit, поэтому литерал без
# одного из этих СЛОВ апстрим удалением не признает никогда — вызывать его
# незачем. Фильтр не может быть строже оригинала (он следует из него), а
# отсекает главное: прозу вроде "confirm the removal", где "rm" сидит внутри
# слова. Инвариант в тестах держит соответствие по каждому ключу.
_SHELL_CANDIDATE_RE = re.compile(r'\b(?:rm|find|git)\b', re.IGNORECASE)


def _upstream_delete_verdict(shell_command: str) -> str | None:
    """Прогнать ОДИН шелл-литерал через апстримный детектор."""
    if not _SHELL_CANDIDATE_RE.search(shell_command):
        return None
    from tools.approval import detect_dangerous_command

    is_dangerous, pattern_key, _description = detect_dangerous_command(shell_command)
    if is_dangerous and _verdict_asks(pattern_key, shell_command):
        return f"shell literal: {pattern_key}"
    return None


def detect_delete_in_code(code: str, family: str = "python") -> str | None:
    """Вернуть опознанную идиому удаления в тексте кода, иначе None."""
    if not code or not _mentions_deletion(code):
        return None
    bare_code, candidates = _split_code_and_literals(code)
    lowered_bare = bare_code.lower()
    hit = _match_idiom(lowered_bare, family)
    if hit is None and family == "python":
        hit = _imported_delete_call(lowered_bare)
    if hit:
        return hit
    for candidate in dict.fromkeys(candidates):
        verdict = _upstream_delete_verdict(candidate)
        if verdict:
            return verdict
    return None


def is_python_delete(code: str) -> bool:
    """Точка входа ``check_execute_code_guard``: там код всегда Python."""
    return detect_delete_in_code(code) is not None


# --- терминал: python/perl/node -c/-e с однострочным payload'ом ------------
#
# Апстрим распознаёт САМ ФАКТ -c/-e-вызова (ключ "script execution via
# -e/-c flag"), но внутрь строки не заглядывает — а внутри лежит код, а не
# шелл. Достаём payload'ы и разбираем каждый как код на языке своего
# интерпретатора. Именно payload, а не команду целиком: в
# ``grep -rn "os.remove(" .`` та же подстрока — данные для поиска.
_INTERPRETER_C_FLAG_RE = re.compile(
    r'''\b(?P<interp>python[\w.]*|perl|node)(?:\s+-\S+)*\s+(?:-c|-e)\s*'''
    r'''(?P<quote>['"])(?P<payload>.*?)(?P=quote)''',
    re.IGNORECASE | re.DOTALL,
)


def _iter_interpreter_payloads(command: str) -> Iterator[tuple[str, str]]:
    if not command:
        return
    for match in _INTERPRETER_C_FLAG_RE.finditer(command):
        interp = match.group("interp").lower()
        family = "python" if interp.startswith("python") else interp
        yield family, match.group("payload")


def iter_interpreter_c_payloads(command: str) -> Iterator[str]:
    """Все payload'ы, переданные python/perl/node через -c/-e.

    Составная команда (``python -c "print(1)" && python -c "…rmtree…"``)
    содержит несколько вызовов — проверять нужно каждый, не только первый.
    """
    for _family, payload in _iter_interpreter_payloads(command):
        yield payload


def is_interpreter_payload_delete(command: str) -> bool:
    """True, если ЛЮБОЙ -c/-e payload внутри команды удаляет данные."""
    return any(
        detect_delete_in_code(payload, family) is not None
        for family, payload in _iter_interpreter_payloads(command)
    )


def is_terminal_delete(command: str) -> bool:
    """Единственная точка входа для терминальных guard'ов approval.py.

    Команду целиком разбирает апстрим; нам остаётся классифицировать
    опознанный паттерн и отдельно заглянуть в ``-c``/``-e`` payload.
    """
    from tools.approval import detect_dangerous_command

    is_dangerous, pattern_key, _description = detect_dangerous_command(command)
    if is_dangerous and _verdict_asks(pattern_key, command):
        return True
    return is_interpreter_payload_delete(command)
