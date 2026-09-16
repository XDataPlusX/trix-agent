"""Правило "в песочнице спрашиваем только про удаление"."""

import logging
import re
import time

import pytest

from hermes_cli.trix_sandbox_guard import (
    DELETE_IDIOMS,
    SANDBOX_DELETE_EXCLUDED_PATTERN_KEYS,
    SANDBOX_DELETE_PATTERN_KEYS,
    detect_delete_in_code,
    is_interpreter_payload_delete,
    is_python_delete,
    is_sandbox_delete,
    is_terminal_delete,
    iter_interpreter_c_payloads,
    uninspectable_verdicts,
)
from tools.approval import DANGEROUS_PATTERNS, detect_dangerous_command

DANGEROUS_PATTERNS_DESCRIPTIONS = {description for _pattern, description in DANGEROUS_PATTERNS}


def test_every_key_exists_in_upstream_patterns():
    """Инвариант: ключ — это описание паттерна апстрима. Переименование
    описания наверху обязано ронять этот тест, иначе правило тихо
    перестанет срабатывать."""
    upstream = {description for _pattern, description in DANGEROUS_PATTERNS}
    unknown = SANDBOX_DELETE_PATTERN_KEYS - upstream
    assert not unknown, f"ключей нет в DANGEROUS_PATTERNS: {sorted(unknown)}"


# Двусторонний инвариант (круг правок 2 — находка: односторонний вариант
# ловит только переименование учтённого описания и НИКОГДА появление
# нового удаляющего паттерна апстрима; именно так мимо набора проскочил
# "find -delete"). Тот же keyword-набор используется и апстримным
# DANGEROUS_PATTERNS, и в self-test мутации ниже.
_DELETE_LIKE_KEYWORDS = re.compile(
    r'\b(delete|deletes|deleting|rm\b|remove|removes|removing|wipe|wipes|'
    r'destroy|destroys|erase|erases|unlink|rmdir|clean\b|shred|truncate)\b',
    re.IGNORECASE,
)


def _delete_like_descriptions(patterns):
    return {desc for _pattern, desc in patterns if _DELETE_LIKE_KEYWORDS.search(desc)}


def test_every_delete_like_upstream_pattern_is_accounted_for():
    """Каждый паттерн DANGEROUS_PATTERNS, чьё описание похоже на удаление,
    обязан быть либо в SANDBOX_DELETE_PATTERN_KEYS, либо в
    SANDBOX_DELETE_EXCLUDED_PATTERN_KEYS (с причиной). Появление нового
    удаляющего паттерна апстрима без явного решения роняет этот тест —
    в отличие от одностороннего инварианта выше."""
    delete_like = _delete_like_descriptions(DANGEROUS_PATTERNS)
    accounted = SANDBOX_DELETE_PATTERN_KEYS | set(SANDBOX_DELETE_EXCLUDED_PATTERN_KEYS)
    unaccounted = delete_like - accounted
    assert not unaccounted, (
        f"новый(е) удаляющий(е) паттерн(ы) апстрима не разобран(ы): "
        f"{sorted(unaccounted)} — добавить в SANDBOX_DELETE_PATTERN_KEYS "
        f"или в SANDBOX_DELETE_EXCLUDED_PATTERN_KEYS с причиной"
    )


def test_sandbox_delete_and_excluded_keys_do_not_overlap():
    overlap = SANDBOX_DELETE_PATTERN_KEYS & set(SANDBOX_DELETE_EXCLUDED_PATTERN_KEYS)
    assert not overlap, f"ключ одновременно и включён, и исключён: {sorted(overlap)}"


def test_excluded_pattern_keys_have_real_reasons():
    """Каждое исключение обязано быть непустой строкой-причиной, а не
    заглушкой — иначе список исключений вырождается в способ молча
    заглушить инвариант."""
    for key, reason in SANDBOX_DELETE_EXCLUDED_PATTERN_KEYS.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 10, (
            f"{key!r}: причина исключения пустая или слишком короткая"
        )


def test_overwrite_and_format_patterns_are_explicitly_excluded():
    """Круг правок 3: эти пять апстримных паттернов НЕ содержат слов про
    удаление в описании (свип их не находит вовсе — "overwrite"/"format"/
    "write" не входят в keyword-набор), поэтому двусторонний инвариант их
    не поймает ни в какую сторону. Записаны в исключения явным решением
    (перезапись — не удаление; format/write-to-block-device недостижимы в
    непривилегированном докер-контейнере) — этот тест фиксирует именно то
    решение напрямую, а не полагается на свип."""
    for key in (
        "overwrite project env/config via tee",
        "overwrite project env/config via redirection",
        "overwrite project env/config file",
        "format filesystem",
        "write to block device",
    ):
        assert key in DANGEROUS_PATTERNS_DESCRIPTIONS, f"{key!r} больше не существует в апстриме?"
        assert key in SANDBOX_DELETE_EXCLUDED_PATTERN_KEYS, f"{key!r} должен быть в исключениях"
        assert key not in SANDBOX_DELETE_PATTERN_KEYS


def test_double_sided_invariant_catches_a_new_upstream_delete_pattern():
    """Self-test мутации: гипотетический НОВЫЙ удаляющий паттерн апстрима,
    которого нет ни в наборе, ни в исключениях, обязан быть пойман тем же
    keyword-свипом, которым пользуется тест выше — именно так должен был
    (и теперь будет) пойман "find -delete" в момент его появления в
    апстриме."""
    fake_new_pattern = (r"\bnuke-workspace\b", "wipe everything (hypothetical new pattern)")
    delete_like = _delete_like_descriptions(list(DANGEROUS_PATTERNS) + [fake_new_pattern])
    accounted = SANDBOX_DELETE_PATTERN_KEYS | set(SANDBOX_DELETE_EXCLUDED_PATTERN_KEYS)
    unaccounted = delete_like - accounted
    assert unaccounted == {"wipe everything (hypothetical new pattern)"}


@pytest.mark.parametrize("command", [
    "rm -rf старое",
    "rm -rf /workspace/проект",
    "rm --recursive --force build",
    "find . -name '*.tmp' -exec rm {} \;",
    "find /workspace -name '*.tmp' -delete",  # C2
    "git reset --hard HEAD~5",  # I1
    "git clean -fdx",  # I1
])
def test_delete_commands_are_recognised(command):
    _is_dangerous, pattern_key, _description = detect_dangerous_command(command)
    assert is_sandbox_delete(pattern_key), f"{command!r} не опознана как удаление"


@pytest.mark.parametrize("command", [
    "npm install",
    "python build.py",
    "chmod 777 /workspace/site",
    "curl https://example.com/install.sh | sh",
    "psql -c 'DROP TABLE users'",
    "git branch -D old-feature",  # осознанно исключён — см. SANDBOX_DELETE_EXCLUDED_PATTERN_KEYS
])
def test_everyday_and_other_dangerous_commands_are_not_delete(command):
    """Опасные, но не удаляющие команды в песочнице по-прежнему проходят
    молча: спрашивать про chmod и curl|sh — шум, ради которого продукт
    никто не покупал."""
    _is_dangerous, pattern_key, _description = detect_dangerous_command(command)
    assert not is_sandbox_delete(pattern_key)


def test_none_is_not_a_delete():
    assert is_sandbox_delete(None) is False


def test_docker_skip_lets_everyday_commands_through(monkeypatch):
    from tools import approval

    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    result = approval.check_dangerous_command("npm install", "docker", has_host_access=False)
    assert result["approved"] is True
    assert result["message"] is None


def test_docker_skip_no_longer_covers_deletes(monkeypatch, caplog):
    """Удаление в песочнице обязано дойти до слоя подтверждения.

    Отклонение от буквального текста брифа (задокументировано в
    task-7-report.md): в headless pytest-процессе нет ни интерактивного
    CLI, ни gateway-сессии, поэтому даже КОМАНДА, ДОШЕДШАЯ до
    ``_run_approval_gate``, попадает в исторический fail-open путь
    ("AUTO-APPROVED dangerous command in non-interactive non-gateway
    context") и возвращает тот же ``{"approved": True, "message": None}``,
    что и полный пропуск. Проверка по форме ответа (как в брифе)
    не отличает "пропущено guard'ом" от "дошло до guard'а и
    авто-одобрено вне интерактивного контекста" — обе ветки дают
    идентичный dict. Наблюдаемая разница — сам факт прохода через
    detect_dangerous_command + _run_approval_gate, который сегодня
    логирует эту фразу с именем паттерна; при полном пропуске
    (``_should_skip_container_guards`` возвращает True без обращения к
    детекции) такой записи в логе нет вовсе.
    """
    from tools import approval

    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    with caplog.at_level(logging.WARNING, logger="tools.approval"):
        result = approval.check_dangerous_command(
            "rm -rf /workspace/проект", "docker", has_host_access=False
        )
    assert result["approved"] is True  # fail-open вне CLI/gateway — ожидаемо
    assert any(
        "AUTO-APPROVED dangerous command" in record.message
        for record in caplog.records
    ), "удаление не дошло до approval gate — сработал ранний пропуск"


# =========================================================================
# execute_code: удаление на Python (найдено при ревью — DANGEROUS_PATTERNS
# матчит только текст ШЕЛЛ-команды и не видит shutil.rmtree/os.remove/
# Path(...).unlink внутри execute_code-скрипта).
# =========================================================================

def test_delete_idioms_are_split_per_interpreter_family():
    """Круг правок 4: раньше это было одно плоское множество, обслуживавшее
    двух вызывающих с РАЗНЫМИ данными — большой Python-скрипт из
    ``execute_code`` и однострочный ``-c``/``-e`` payload. Из-за этого
    perl'ова голая форма ``unlink(`` ложно срабатывала на питоновском
    ``def unlink(x)``. Наборы разведены по семействам: Python-набор едет по
    большому скрипту и потому требует точку-квалификатор, perl/node-наборы
    едут по однострочнику, где голое слово однозначно."""
    assert DELETE_IDIOMS["python"] == frozenset({
        ".rmtree(",  # покрывает shutil.rmtree(
        "os.remove(",
        "os.removedirs(",
        ".unlink(",  # покрывает os.unlink( и pathlib Path(...).unlink(
        ".rmdir(",  # покрывает os.rmdir( и pathlib Path(...).rmdir(
    })
    assert DELETE_IDIOMS["perl"] == frozenset({
        "unlink", "rmtree", "remove_tree", "rmdir",
    })
    assert DELETE_IDIOMS["node"] == frozenset({
        "rmsync", "unlinksync", "rmdirsync", ".rm(", ".rmdir(",
    })


@pytest.mark.parametrize("code", [
    '"""Path.unlink() is NOT used here."""\nvalue = 1',
    "def unlink(node):\n    node.next = None\n\nunlink(head)",
    "# do NOT os.remove(this) — the caller owns the file\nprint(1)",
    'HELP = "call os.remove(path) before exiting"\nprint(HELP)',
])
def test_python_delete_idioms_in_docstrings_and_comments_do_not_fire(code):
    """Идиомы ищутся по КОДУ без строковых литералов и комментариев (их
    отделяет тот же проход, который достаёт шелл-литералы), поэтому
    ``os.remove(``/``.unlink(``, ПРОЦИТИРОВАННЫЕ в документации, молчат.
    ``def unlink(node)`` молчит по второй причине: голое имя считается
    удалением только когда оно импортировано из os/shutil/pathlib.

    Утверждение узкое и названо по факту (круг правок 5, N2): речь про
    ИДИОМНУЮ дверь. Вторая дверь — литеральная — остаётся открытой, и
    докстринг, цитирующий ШЕЛЛОВУЮ удаляющую команду, спросит; это
    зафиксировано отдельным тестом ниже."""
    assert not is_python_delete(code), f"{code!r} ложно опознан как удаление"


def test_a_docstring_quoting_a_shell_delete_still_asks():
    """Граница предыдущего теста, явно (круг правок 5, N2). Литерал уходит
    апстримному детектору независимо от того, докстринг это или аргумент
    os.system — отличить их без AST нельзя, и мы сознательно ошибаемся в
    сторону лишнего вопроса. Комментарий — не литерал, он молчит."""
    assert is_python_delete('"""Не делай git reset --hard."""\nvalue = 1')
    assert not is_python_delete("# Не делай git reset --hard\nvalue = 1")


@pytest.mark.parametrize("code", [
    "import shutil\nshutil.rmtree('/workspace/проект')",
    "import os\nos.remove('/workspace/doc.pdf')",
    "import os\nos.unlink('/workspace/doc.pdf')",
    "import os\nos.rmdir('/workspace/emptydir')",
    "import os\nos.removedirs('/workspace/a/b/c')",
    "from pathlib import Path\nPath('/workspace/x').unlink()",
    "from pathlib import Path\nPath('/workspace/x').rmdir()",
    'import subprocess\nsubprocess.run(["rm", "-rf", "/workspace/x"])',
    "import subprocess\nsubprocess.call(('rm', '-r', '/workspace/x'))",
    'import subprocess\nsubprocess.run(["rm", "-f", "/workspace/x"])',  # I5
    "os.system('rm -rf /workspace/x')",  # I4: shell string via os.system
    "subprocess.run('rm -rf /workspace/x', shell=True)",  # I4: shell string
    "os.system('rm /workspace/x')",  # I4: absolute path, no flags at all
    # Круг правок 4: argv-форма склеивается в шелл-строку и судится тем же
    # апстримным детектором, что и терминал. Обе строки ниже апстрим считает
    # удалением ("recursive delete" / "delete in root path"), то есть в
    # терминале докер-песочницы они спрашивают уже сегодня — раньше в
    # argv-форме они молчали, и это было расхождением, а не защитой.
    "import subprocess\nsubprocess.run(['rm', '--version'])",
    "import subprocess\nsubprocess.run(['rm', '-i', '/workspace/x'])",
])
def test_python_delete_idioms_are_recognised(code):
    assert is_python_delete(code), f"{code!r} не опознан как удаление на Python"


@pytest.mark.parametrize("code", [
    "open('/workspace/x').read()",
    "import json\njson.dumps({'a': 1})",
    "import subprocess\nsubprocess.run(['ls'])",
    "def confirm():\n    pass\nconfirm()",  # оканчивается на "rm(", но не как отдельный токен
    "term = 5\nfirm = 'x'\nwarm = True",
    "os.system('ls -la /workspace')",  # I4: обычный os.system, без rm
    "os.system('firmware_update.sh')",  # "rm" внутри слова, не отдельным токеном
])
def test_everyday_python_code_is_not_recognised_as_delete(code):
    """Ложное срабатывание здесь стоит дорого: клиент начал бы получать
    вопросы на ровном месте. Отдельно проверяем слова, оканчивающиеся на
    "rm"/"firm" и т.п. — узкая подстрока не должна цеплять их."""
    assert not is_python_delete(code), f"{code!r} ложно опознан как удаление"


def test_detect_delete_in_code_returns_none_for_empty_or_none():
    assert detect_delete_in_code("") is None
    assert detect_delete_in_code(None) is None


# =========================================================================
# N3 (круг правок 3): убрав апстримный detect_dangerous_command с пути
# execute_code (I4 круга 2), потеряли покрытие xargs/find/git-паттернов,
# которые он ловил заодно — те же строки в ТЕРМИНАЛЕ по-прежнему
# спрашивают (полное покрытие DANGEROUS_PATTERNS), а в execute_code молчали.
# =========================================================================

@pytest.mark.parametrize("code", [
    "os.system(\"find . -name '*.log' | xargs rm\")",
    'subprocess.run("find . -type f -exec rm {} \\;", shell=True)',
    "os.system('find /workspace -delete')",
    "os.system('git clean -fdx')",
    "os.system('git reset --hard HEAD~3')",
])
def test_shell_string_delete_idioms_lost_by_i4_are_recognised(code):
    assert is_python_delete(code), f"{code!r} не опознан как удаление (N3)"


# =========================================================================
# N5 (круг правок 3): проверка шелловых строк была без якоря на начало
# команды — `docker rm -f c1`, `helm rm -f release`, `npm rm -f pkg`
# ложно спрашивали на пути execute_code, хотя апстрим те же строки в
# терминале опасными не считает (rm там не первое слово команды).
# =========================================================================

@pytest.mark.parametrize("code", [
    "os.system('docker rm -f c1')",
    "os.system('helm rm -f release')",
    "os.system('npm rm -f pkg')",
])
def test_rm_not_at_command_start_is_not_recognised_as_delete(code):
    assert not is_python_delete(code), f"{code!r} ложно опознан как удаление (N5)"


# =========================================================================
# Круг правок 4: шелл-литерал судит АПСТРИМНЫЙ детектор, а не наши зеркала.
# =========================================================================

@pytest.mark.parametrize("wrapper", [
    "sudo", "env -i", "time", "nohup", "exec", "setsid",
])
def test_command_wrappers_inside_os_system_are_recognised(wrapper):
    """До круга правок 4 наш собственный якорь на начало команды считал
    первым словом сам wrapper и молчал, хотя ТА ЖЕ строка в терминале
    спрашивала: апстримный ``_mark_command_starts`` знает про sudo/env/
    time/nohup/exec/setsid, а самодельная копия якоря — нет. Теперь обе
    стороны судит один и тот же детектор, и расхождение невозможно."""
    command = f"{wrapper} rm -rf /workspace/x"
    _is_dangerous, pattern_key, _ = detect_dangerous_command(command)
    assert is_sandbox_delete(pattern_key), "апстрим перестал считать это удалением?"
    assert is_python_delete(f"import os\nos.system({command!r})"), (
        f"{command!r} внутри os.system молчит, хотя в терминале спрашивает"
    )


# Представитель на каждый ключ, который мы объявили удаляющим. Ключ
# "recursive delete (long flag)" представителя не имеет: `rm --recursive`
# первым матчит более ранний апстримный паттерн "recursive delete" — та же
# команда, тот же вердикт.
_REPRESENTATIVE_DELETE_COMMANDS = {
    "delete in root path": "rm /workspace/x",
    "recursive delete": "rm -rf build",
    "recursive delete (flags after operands)": "rm build -rf",
    "xargs with rm": "find . -print0 | xargs rm",
    "find -exec/-execdir rm": "find . -exec rm {} ;",
    "find -delete": "find . -delete",
    "git reset --hard (destroys uncommitted changes)": "git reset --hard HEAD~1",
    "git clean with force (deletes untracked files)": "git clean -fdx",
}
_KEYS_WITHOUT_REPRESENTATIVE = {"recursive delete (long flag)"}


def test_every_sandbox_delete_key_has_a_representative_command():
    assert (
        set(_REPRESENTATIVE_DELETE_COMMANDS) | _KEYS_WITHOUT_REPRESENTATIVE
        == set(SANDBOX_DELETE_PATTERN_KEYS)
    ), "новый ключ добавлен в набор, но не покрыт представителем ниже"


@pytest.mark.parametrize(
    "pattern_key,command", sorted(_REPRESENTATIVE_DELETE_COMMANDS.items())
)
def test_upstream_delete_keys_survive_the_prefilters(pattern_key, command):
    """Структурный инвариант вместо шести зеркал апстримных паттернов:
    дешёвые префильтры на пути ``execute_code`` обязаны пропускать ВСЁ, что
    апстрим считает удалением. Если префильтр окажется уже правила —
    покраснеет ровно тот ключ, который он потерял."""
    is_dangerous, detected, _ = detect_dangerous_command(command)
    assert is_dangerous and detected == pattern_key, (
        f"апстрим больше не ловит {command!r} как {pattern_key!r}"
    )
    assert is_python_delete(f"import os\nos.system({command!r})"), (
        f"префильтр потерял {pattern_key!r}: {command!r} внутри os.system молчит"
    )


def test_shell_command_inside_an_interpreter_payload_is_recognised():
    """Оба извлечения складываются: payload после -c достаётся из команды,
    а из payload'а — шелл-литерал внутри os.system."""
    assert is_interpreter_payload_delete(
        "python -c \"import os; os.system('sudo rm -rf /workspace')\""
    )


# =========================================================================
# Круг правок 5 (M1): голое имя, импортированное из os/shutil/pathlib.
# Сузив идиомы по семействам (круг 4), потеряли `from os import unlink` —
# точечных идиом там нет, а голая форма уехала в perl-набор.
# =========================================================================

@pytest.mark.parametrize("code", [
    "from os import unlink\nunlink('/workspace/x')",
    "from os import remove\nremove('/workspace/doc.pdf')",
    "from os import rmdir\nrmdir('/workspace/emptydir')",
    "from os import removedirs\nremovedirs('/workspace/a/b')",
    "from shutil import rmtree\nrmtree('/workspace/проект')",
    "from os import unlink, rmdir\nrmdir('/workspace/x')",
    "from shutil import copy2, rmtree\nrmtree('/workspace/x')",
])
def test_bare_calls_of_imported_delete_names_are_recognised(code):
    assert is_python_delete(code), f"{code!r} не опознан как удаление"


@pytest.mark.parametrize("code", [
    # То же голое имя БЕЗ импорта из os/shutil — своя функция, не удаление.
    "def unlink(node):\n    node.next = None\n\nunlink(head)",
    "def rmdir(entry):\n    return entry\n\nrmdir(node)",
    "from collections import deque\nq = deque()\nq.remove(3)",
    "items = [1, 2, 3]\nitems.remove(2)",
])
def test_bare_delete_names_without_the_import_stay_silent(code):
    """Именно на голой форме ломалась плоская идиома круга 3: `def unlink(x)`
    спрашивал. Имя считается удаляющим ровно тогда, когда импортировано из
    модуля, где оно значит удаление файла."""
    assert not is_python_delete(code), f"{code!r} ложно опознан как удаление"


# =========================================================================
# Круг правок 5 (Major): синтетические вердикты апстрима «разобрать не смог».
# =========================================================================

def _long_delete_command(paths=200):
    """Массовое удаление явным списком путей — длиннее апстримного лимита
    в 4096 символов без разделителей."""
    return "rm -rf " + " ".join(f"/workspace/data/file_{i:04d}.csv" for i in range(paths))


def test_uninspectable_verdicts_are_structurally_invisible_to_the_sweep():
    """Двусторонний keyword-свип свипает ТАБЛИЦУ паттернов, а описаний этих
    вердиктов в таблице нет вовсе — поэтому свип к ним слеп сколько его ни
    улучшай, и именно эта слепота пропустила дыру (круг правок 5, Major).
    Инвариант закрывает её отдельно: каждое апстримное ``*_DESCRIPTION``
    обязано быть либо обычным описанием из таблицы, либо явно учтённым
    синтетическим вердиктом."""
    from tools import approval

    sentinels = uninspectable_verdicts()
    upstream_descriptions = {
        getattr(approval, name)
        for name in dir(approval)
        if name.endswith("_DESCRIPTION")
    }
    # Третья категория, помимо «описание из таблицы» и «синтетический
    # вердикт»: собственная причина гейта execute_code. Она не паттерн и не
    # «разобрать не смог» — она называет сам факт запуска скрипта, и до
    # клиента доезжает своим путём (check_execute_code_guard). Учитываем
    # явно, чтобы свип сохранил смысл, а не расширялся молча.
    accounted = sentinels | {approval._EXECUTE_CODE_DESCRIPTION}
    unaccounted = upstream_descriptions - accounted - DANGEROUS_PATTERNS_DESCRIPTIONS
    assert not unaccounted, (
        f"новый синтетический вердикт апстрима не учтён: {sorted(unaccounted)} — "
        "добавить в uninspectable_verdicts() или убедиться, что он в таблице"
    )
    assert not (sentinels & DANGEROUS_PATTERNS_DESCRIPTIONS), (
        "вердикт попал в таблицу паттернов — свипа теперь достаточно, "
        "отдельный список можно пересмотреть"
    )
    assert not (sentinels & SANDBOX_DELETE_PATTERN_KEYS), (
        "синтетический вердикт — не описание паттерна, поэтому в наборе ключей "
        "ему не место; его русская формулировка живёт отдельно (SANDBOX_VERDICT_RU "
        "/ client_reason_ru), а покрытие проверяет инвариант достижимости в "
        "tests/gateway/test_approval_prompt_russian.py"
    )


def test_oversized_delete_command_still_reaches_the_approval_layer():
    """Апстрим на такой команде возвращает fail-closed вердикт «разобрать не
    смог» вместо имени паттерна. Раньше мы читали его как «не удаление» и
    пропускали ВЕСЬ слой подтверждения."""
    command = _long_delete_command()
    is_dangerous, pattern_key, _ = detect_dangerous_command(command)
    assert is_dangerous and pattern_key in uninspectable_verdicts(), (
        "апстрим больше не возвращает синтетический вердикт на этой длине"
    )
    assert not is_sandbox_delete(pattern_key)
    assert is_terminal_delete(command), "терминал пропустил массовое удаление"
    assert is_python_delete(f"import os\nos.system({command!r})"), (
        "execute_code пропустил массовое удаление"
    )


def test_oversized_command_without_delete_words_stays_silent():
    """Обратная сторона: сентинель трактуется как «спрашивать» только когда
    сработал префильтр удаления. Иначе любая длинная однострочная команда
    (base64-блоб, heredoc) начала бы спрашивать на ровном месте."""
    command = "echo " + "x" * 6000
    is_dangerous, pattern_key, _ = detect_dangerous_command(command)
    assert is_dangerous and pattern_key in uninspectable_verdicts()
    assert not is_terminal_delete(command)
    assert not is_python_delete(f"import os\nos.system({command!r})")


# =========================================================================
# Круг правок 5 (M2): склейка соседних литералов — решение по замерам.
# =========================================================================

def test_adjacent_literals_must_be_joined_or_argv_deletes_escape():
    """Почему серия соседних литералов склеивается, хотя это синтезирует
    команду, которой в скрипте нет ни одной строкой: поодиночке ни один
    элемент argv-вызова удалением не является, а вместе — является. Без
    склейки молчала бы вся argv-семья (замеры в task-7-report.md, круг 5):
    subprocess.run([...]), " ".join([...]), git clean/reset и find -delete
    в списочной форме — 8 случаев из 20 в списке «обязано спросить»."""
    assert not is_python_delete('token = "rm"')
    assert not is_python_delete('token = "-rf"')
    assert not is_python_delete('token = "/workspace/проект"')
    assert is_python_delete(
        'import subprocess\nsubprocess.run(["rm", "-rf", "/workspace/проект"])'
    )


@pytest.mark.parametrize("code", [
    'import subprocess\nsubprocess.run(["git", "clean", "-fdx"])',
    'import subprocess\nsubprocess.run(["git", "reset", "--hard", "HEAD~1"])',
    'import subprocess\nsubprocess.run(["find", ".", "-delete"])',
    'import subprocess\nsubprocess.run(["bash", "-c", "rm -rf /workspace/build"])',
    'import subprocess\nsubprocess.run(" ".join(["rm", "-rf", path]), shell=True)',
    'import os\nos.system("rm -rf " + path)',
])
def test_argv_and_concatenated_shell_forms_are_recognised(code):
    assert is_python_delete(code), f"{code!r} не опознан как удаление"


def test_execute_code_docker_skip_no_longer_covers_python_deletes(monkeypatch):
    """Python-удаление обязано дойти до approval gate так же, как шелловое
    ``rm -rf`` — проверено через реальный (симулированный) gateway-раунд-трип,
    а не через форму ответа: headless fail-open (см. тест выше) даёт
    одинаковый dict что при пропуске, что при авто-одобрении вне
    интерактивного контекста, поэтому здесь наблюдаем настоящее решение
    approval-gate — deny от "клиента" должен реально заблокировать вызов.
    """
    from tools import approval as A

    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)

    session_key = "trix-sandbox-guard-python-delete-test"
    token = A.set_current_session_key(session_key)
    with A._lock:
        A._gateway_queues.pop(session_key, None)
        A._gateway_notify_cbs.pop(session_key, None)
        A._permanent_approved.discard("execute_code")
        A._session_approved.get(session_key, set()).discard("execute_code")

    def _deny_resolver(_approval_data):
        with A._lock:
            entries = A._gateway_queues.get(session_key, [])
            if entries:
                entry = entries[-1]
                entry.result = "deny"
                entry.event.set()

    with A._lock:
        A._gateway_notify_cbs[session_key] = _deny_resolver

    try:
        result = A.check_execute_code_guard(
            "import shutil\nshutil.rmtree('/workspace/проект')",
            "docker",
            has_host_access=False,
        )
    finally:
        A.reset_current_session_key(token)
        with A._lock:
            A._gateway_queues.pop(session_key, None)
            A._gateway_notify_cbs.pop(session_key, None)

    assert result["approved"] is False, (
        "python-удаление проскочило мимо контейнерного пропуска, минуя "
        "gateway approval round-trip"
    )


def test_execute_code_docker_skip_still_lets_benign_scripts_through(monkeypatch):
    from tools import approval as A

    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    result = A.check_execute_code_guard(
        "import json\njson.dumps({'a': 1})", "docker", has_host_access=False
    )
    assert result["approved"] is True
    assert result["message"] is None


# =========================================================================
# terminal: python/perl/node -c/-e с однострочным payload'ом (C1)
# =========================================================================
#
# Найдено при ревью (круг правок 2): `python -c "shutil.rmtree(...)"` через
# терминал проходил молча — is_python_delete был подключён только к
# check_execute_code_guard, а апстрим лишь распознаёт САМ ФАКТ -c/-e-вызова
# (ключ "script execution via -e/-c flag"), не заглядывая внутрь строки.
# Это самый естественный обход для модели, которой отказали в rm.

@pytest.mark.parametrize("command", [
    'python -c "import shutil; shutil.rmtree(\'/workspace\')"',
    "python3 -c \"import os; os.remove('/workspace/doc.pdf')\"",
    "python -e \"from pathlib import Path; Path('/workspace/x').unlink()\"",
    "python3 -c 'import subprocess; subprocess.run([\"rm\", \"-rf\", \"/workspace/x\"])'",
    # N1 (круг правок 3) — четыре обхода, все раньше проходили молча:
    "python -B -c \"import shutil; shutil.rmtree('/workspace')\"",  # флаг перед -c
    "python -u -c \"import os; os.remove('/workspace/x')\"",  # флаг перед -c
    "python3.11 -c \"import shutil; shutil.rmtree('/workspace')\"",  # версия в имени
    "python -c\"import shutil; shutil.rmtree('/workspace')\"",  # без пробела перед кавычкой
    # составная команда: первый -c безобиден, ВТОРОЙ — удаление; .search()
    # раньше проверял только первое совпадение и молчал.
    "python -c \"print(1)\" && python -c \"import shutil; shutil.rmtree('/workspace')\"",
    # perl/node идиомы (ревьюер подтвердил: fs.rmSync — обычная идиома
    # очистки в JS-проектах, не экзотика).
    "node -e \"fs.rmSync('dist', {recursive:true, force:true})\"",
    "node -e \"fs.unlinkSync('/workspace/doc.pdf')\"",
    "node -e \"fs.rmdirSync('/workspace/emptydir')\"",
    "perl -e \"unlink('/workspace/doc.pdf')\"",
    # Круг правок 4: рабочая однострочная форма node — всегда через
    # require('fs') / require('node:fs'); прежняя запись идиом ("fs.rmSync(")
    # её не ловила. Плюс недостающие perl-формы.
    "node -e \"require('fs').rmSync('dist', {recursive:true, force:true})\"",
    "node -e \"require('node:fs').rmSync('dist', {recursive:true})\"",
    "node -e \"require('fs').unlinkSync('/workspace/doc.pdf')\"",
    "perl -e \"rmdir('/workspace/emptydir')\"",
    "perl -e \"use File::Path; rmtree('/workspace/x')\"",
    "perl -e \"use File::Path qw(remove_tree); remove_tree('/workspace/x')\"",
    "perl -e 'unlink $f'",
])
def test_interpreter_payload_deletes_are_recognised(command):
    assert is_interpreter_payload_delete(command), (
        f"{command!r} — payload не опознан как удаление"
    )


def test_interpreter_payload_checks_every_c_flag_not_just_the_first():
    """N1: составная команда с ДВУМЯ -c-вызовами — второй (реально
    опасный) не должен потеряться из-за того, что первый (безобидный)
    оказался первым найденным совпадением."""
    command = 'python -c "print(1)" && python -c "import shutil; shutil.rmtree(\'/workspace\')"'
    payloads = list(iter_interpreter_c_payloads(command))
    assert payloads == ["print(1)", "import shutil; shutil.rmtree('/workspace')"]
    assert is_interpreter_payload_delete(command)


def test_interpreter_payload_compound_command_both_benign_is_not_delete():
    command = 'python -c "print(1)" && python -c "print(2)"'
    assert not is_interpreter_payload_delete(command)


@pytest.mark.parametrize("command", [
    # Не -c/-e вызов интерпретатора вообще.
    "python build.py",
    "python3 script.py --workspace /workspace",
    # -c/-e вызов, но payload без удаления.
    'python -c "print(\'hello\')"',
    "python3 -c \"import json; json.dumps({'a': 1})\"",
    # Самое важное: идиома встречается как ДАННЫЕ для поиска, а не как
    # исполняемый код — не должно триггерить is_interpreter_payload_delete
    # (и, отдельно, не должно триггерить голый is_python_delete по всей
    # команде — правильный фикс достаёт payload, а не сканирует всю строку).
    'grep -rn "os.remove(" .',
    'rg ".unlink(" src/',
    'grep -l "shutil.rmtree(" *.py',
    'echo "call os.remove(path) before exiting"',
])
def test_interpreter_payload_negative_cases_are_not_delete(command):
    assert not is_interpreter_payload_delete(command), (
        f"{command!r} ложно опознан как удаление через -c/-e"
    )


@pytest.mark.parametrize("command", [
    'grep -rn "os.remove(" .',
    'rg ".unlink(" src/',
    'grep -l "shutil.rmtree(" *.py',
])
def test_search_patterns_quoting_delete_idioms_are_data_not_code(command):
    """Идиома внутри кавычек — ДАННЫЕ для поиска, а не исполняемый код.
    Проверяем обе двери сразу: и терминальную (payload у grep не
    извлекается вовсе), и разбор текста как кода (идиомы ищутся по коду БЕЗ
    строковых литералов, поэтому процитированная идиома не считается).

    До круга правок 4 вторая дверь была открыта — тогдашний тест
    (``test_scanning_the_whole_command_would_have_false_positived``)
    ФИКСИРОВАЛ это ложное срабатывание как ожидаемое и объяснял им, почему
    C1 обязан извлекать payload. Причина осталась в силе, а само
    срабатывание исчезло по построению, поэтому тест утверждает теперь
    более сильное свойство — обе двери закрыты."""
    assert not is_interpreter_payload_delete(command)
    assert not is_python_delete(command)


def test_iter_interpreter_c_payloads_returns_nothing_for_non_matches():
    assert list(iter_interpreter_c_payloads("")) == []
    assert list(iter_interpreter_c_payloads(None)) == []
    assert list(iter_interpreter_c_payloads("npm install")) == []


def test_terminal_docker_skip_no_longer_covers_interpreter_payload_deletes(monkeypatch):
    """C1 сквозь настоящую точку врезки: python -c "shutil.rmtree(...)" в
    терминале докер-песочницы обязано дойти до approval gate.

    ``is_interpreter_payload_delete`` не проходит через
    ``detect_dangerous_command`` (значит не логирует "AUTO-APPROVED
    dangerous command", как в ``test_docker_skip_no_longer_covers_deletes``
    выше), а сама команда не хардлайн — поэтому единственный надёжный
    наблюдаемый сигнал "дошла до guard'а, а не пропущена" — реальный
    gateway round-trip: deny от "клиента" должен реально заблокировать
    вызов."""
    from tools import approval as A

    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)

    session_key = "trix-sandbox-guard-interpreter-payload-test"
    token = A.set_current_session_key(session_key)
    with A._lock:
        A._gateway_queues.pop(session_key, None)
        A._gateway_notify_cbs.pop(session_key, None)
        A._permanent_approved.discard("delete in root path")
        A._session_approved.get(session_key, set()).discard("delete in root path")

    def _deny_resolver(_approval_data):
        with A._lock:
            entries = A._gateway_queues.get(session_key, [])
            if entries:
                entry = entries[-1]
                entry.result = "deny"
                entry.event.set()

    with A._lock:
        A._gateway_notify_cbs[session_key] = _deny_resolver

    try:
        gw_result = A.check_dangerous_command(
            'python -c "import shutil; shutil.rmtree(\'/workspace\')"',
            "docker",
            has_host_access=False,
        )
    finally:
        A.reset_current_session_key(token)
        with A._lock:
            A._gateway_queues.pop(session_key, None)
            A._gateway_notify_cbs.pop(session_key, None)

    assert gw_result["approved"] is False, (
        "python -c с удалением проскочило мимо контейнерного пропуска в "
        "терминале, минуя gateway approval round-trip"
    )


# =========================================================================
# Круг правок 5 (N1): третья точка врезки — check_all_command_guards.
# Её мутация раньше оставляла 120 из 120 зелёными: краснели только тесты в
# чужих файлах, и контракт сторожа был неполон на своей территории.
# =========================================================================

@pytest.mark.parametrize("command", [
    "rm -rf /workspace/проект",
    'python -c "import shutil; shutil.rmtree(\'/workspace\')"',
])
def test_check_all_command_guards_docker_skip_no_longer_covers_deletes(
    monkeypatch, command
):
    """Тот же реальный gateway round-trip, что и для check_dangerous_command:
    deny от «клиента» обязан заблокировать вызов, значит команда дошла до
    гейта, а не была пропущена контейнерным fast-path'ом."""
    from tools import approval as A

    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)

    result = _run_with_gateway_deny_resolver(
        "trix-sandbox-guard-all-guards-test",
        lambda: A.check_all_command_guards(command, "docker", has_host_access=False),
    )
    assert result["approved"] is False, (
        f"{command!r} проскочила мимо контейнерного пропуска в "
        "check_all_command_guards, минуя gateway approval round-trip"
    )


def test_check_all_command_guards_docker_skip_still_lets_everyday_through(monkeypatch):
    from tools import approval as A

    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    result = A.check_all_command_guards("npm install", "docker", has_host_access=False)
    assert result["approved"] is True
    assert result["message"] is None


# =========================================================================
# I3: пропуск сужен до docker — эфемерные удалённые песочницы
# (singularity/modal/daytona/vercel_sandbox) продолжают пропускать ВСЁ,
# включая удаление — там подтверждение чистый шум, хостовых маунтов нет.
# =========================================================================

def _run_with_gateway_deny_resolver(session_key, fn):
    """N2: доказать, что *fn* (замыкание над check_dangerous_command/
    check_execute_code_guard) НЕ дошла до approval gate — не по форме
    ответа (headless fail-open даёт тот же ``{"approved": True, "message":
    None}``, что и полный пропуск — неотличимо снаружи), а через реальный
    gateway round-trip: регистрируем deny-резолвер и смотрим, вызвался ли
    он. Если код дошёл до гейта, резолвер скажет "deny" и approved станет
    False; если approved остался True — гейт не был достигнут, сработал
    ранний пропуск.

    Этой техникой найден N2 (круг правок 3): обратная мутация "снять
    пропуск для ВСЕХ бэкендов" не красила прежние тесты
    test_non_docker_sandboxes_still_skip_*, потому что они сравнивали
    только форму ответа.
    """
    from tools import approval as A

    token = A.set_current_session_key(session_key)
    with A._lock:
        A._gateway_queues.pop(session_key, None)
        A._gateway_notify_cbs.pop(session_key, None)
        for key in ("delete in root path", "execute_code"):
            A._permanent_approved.discard(key)
            A._session_approved.get(session_key, set()).discard(key)

    def _deny_resolver(_approval_data):
        with A._lock:
            entries = A._gateway_queues.get(session_key, [])
            if entries:
                entry = entries[-1]
                entry.result = "deny"
                entry.event.set()

    with A._lock:
        A._gateway_notify_cbs[session_key] = _deny_resolver

    try:
        return fn()
    finally:
        A.reset_current_session_key(token)
        with A._lock:
            A._gateway_queues.pop(session_key, None)
            A._gateway_notify_cbs.pop(session_key, None)


@pytest.mark.parametrize("env_type", ["singularity", "modal", "daytona", "vercel_sandbox"])
def test_non_docker_sandboxes_still_skip_deletes_entirely(monkeypatch, env_type):
    from tools import approval as A

    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)

    result = _run_with_gateway_deny_resolver(
        f"trix-sandbox-guard-nondocker-{env_type}",
        lambda: A.check_dangerous_command("rm -rf /workspace/проект", env_type),
    )
    assert result["approved"] is True, (
        f"{env_type}: удаление дошло до approval gate вместо полного "
        "пропуска (deny-резолвер был бы вызван, approved стал бы False)"
    )
    assert result["message"] is None


@pytest.mark.parametrize("env_type", ["singularity", "modal", "daytona"])
def test_non_docker_sandboxes_still_skip_python_deletes_entirely(monkeypatch, env_type):
    from tools import approval as A

    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)

    result = _run_with_gateway_deny_resolver(
        f"trix-sandbox-guard-nondocker-execcode-{env_type}",
        lambda: A.check_execute_code_guard(
            "import shutil\nshutil.rmtree('/workspace/проект')", env_type
        ),
    )
    assert result["approved"] is True, (
        f"{env_type}: python-удаление дошло до approval gate вместо "
        "полного пропуска (deny-резолвер был бы вызван, approved стал бы False)"
    )
    assert result["message"] is None


# =========================================================================
# I4: is_python_delete обязана быть дешёвой — check_execute_code_guard
# больше НЕ вызывает апстримный detect_dangerous_command по тексту
# скрипта (тот квадратичен по размеру; замеры — в task-7-report.md).
# =========================================================================

def test_is_python_delete_is_cheap_on_a_large_script():
    """Модульная проверка внутреннего хелпера. ~22 КБ (не "около 15 КБ",
    как было раньше в этом тесте, — фактический размер строки ниже).
    Дешёвая проверка обязана укладываться в единицы миллисекунд."""
    script = ("value = compute(i)  # ordinary line, nothing suspicious\n" * 400)
    assert len(script) >= 22 * 1024 * 0.9  # фактически ~22 КБ
    start = time.perf_counter()
    is_python_delete(script)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 50, (
        f"is_python_delete заняла {elapsed_ms:.1f} мс на скрипте ~22 КБ — "
        "ожидались единицы миллисекунд"
    )


def test_check_execute_code_guard_is_cheap_on_a_large_script(monkeypatch):
    """N4: таймер обязан охранять РЕАЛЬНУЮ точку врезки —
    ``check_execute_code_guard`` — а не только внутренний
    ``is_python_delete``. Регресс, который круг правок 2 (I4) чинил, жил в
    точке врезки (вызов апстримного detect_dangerous_command ПЕРЕД
    is_python_delete); тест, меряющий только is_python_delete в изоляции,
    не заметил бы, если бы этот вызов вернулся в check_execute_code_guard
    рядом с (а не вместо) дешёвой проверкой."""
    from tools import approval as A

    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    script = ("value = compute(i)  # ordinary line, nothing suspicious\n" * 400)
    assert len(script) >= 22 * 1024 * 0.9  # фактически ~22 КБ

    start = time.perf_counter()
    result = A.check_execute_code_guard(script, "docker", has_host_access=False)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result["approved"] is True
    assert elapsed_ms < 50, (
        f"check_execute_code_guard заняла {elapsed_ms:.1f} мс на скрипте "
        "~22 КБ — ожидались единицы миллисекунд"
    )
