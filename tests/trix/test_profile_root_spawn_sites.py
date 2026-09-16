"""Сторож покрытия: каждый спавн пользовательской нагрузки зовёт контракт.

Это второй прибор, а не дубль первого, и разница принципиальна.

``tests/test_profile_root_containment_sentinel.py`` запускает настоящие потоки
движка и проверяет **поведение**: то, что контракт
``apply_subprocess_containment_env`` собирает окружение правильно, и что
конкретные спавны его правда зовут. Он намеренно не читает исходники.

Но именно поэтому он не умеет отвечать на вопрос «а все ли спавны мы в него
вписали». Для спавна, который никто не добавил в перечень потоков, он зелёный
— он про него не знает. Ровно в эту щель провалился stdio-спавн MCP (RAF-161):
контракт был правильный, поток ``subprocess_env`` — зелёный, а
``tools/mcp_tool._build_safe_env`` собирала окружение мимо контракта, и
MCP-сервер в ``contained`` получал настоящий дом ОС.

Починить тот спавн — не то же самое, что закрыть щель: следующий такой спавн
она пропустит так же молча. Поэтому здесь проверяется **покрытие**, и
единственным доступным способом — по исходникам:

    для каждого места в зоне, которое передаёт дочернему процессу
    собранный словарь окружения (``env=...``),
        это окружение приходит из функции, которая зовёт контракт
        ∨ место внесено в ``_ALLOWLIST`` с причиной

Зона — не всё дерево. Движок спавнит ещё ~130 процессов (git, uv, npm,
установщики, самообновление), и они намеренно живут в доме самого движка:
требовать от них контракт — значит выдать сотню ложных блокеров и тем
обесценить прибор. Зона — модули, которые запускают **нагрузку профиля**:
то, что ходит в сеть от имени пользователя, пишет токены и кэши и потому
обязано лежать внутри корня профиля.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Модули, которые спавнят нагрузку профиля. Не «все, кто зовёт subprocess», а
# те, чей ребёнок работает от имени пользователя и складывает его состояние.
_ZONE = (
    "tools/mcp_tool.py",
    "tools/environments/local.py",
    "tools/code_execution_tool.py",
    "tools/process_registry.py",
    "tools/browser_tool.py",
    "tools/voice_mode.py",
    "tools/computer_use/cua_backend.py",
    "tools/computer_use/doctor.py",
    "tools/computer_use/permissions.py",
    "agent/copilot_acp_client.py",
    "cron/scheduler.py",
    "hermes_cli/bang_shell.py",
    "plugins/web/ddgs/provider.py",
)

# Конструкции, которые отдают окружение дочернему процессу.
_SPAWN = frozenset({
    "Popen", "run", "call", "check_call", "check_output",
    "create_subprocess_exec", "create_subprocess_shell",
    "StdioServerParameters", "execve", "execvpe", "spawnve",
})

# Контракт изоляции и его историческое имя (алиас делегирует в него же).
_CONTRACT = frozenset({
    "apply_subprocess_containment_env",
    "apply_subprocess_home_env",
})

# Места в зоне, которым контракт не нужен — с причиной, а не «потому что
# падает». Ключ — (модуль, объемлющая функция), чтобы запись пережила сдвиг
# строк, но не пережила переезд спавна в другую функцию.
_ALLOWLIST: dict[tuple[str, str], str] = {
    ("tools/code_execution_tool.py", "_probe_python"): (
        "Проба интерпретатора: `python -c 'import sys; sys.exit(...)'`. "
        "Ничего не пишет и живёт 5 секунд. Окружение здесь — не сборка, а "
        "сохранение семантики `env=None` для делегированного ребёнка "
        "(`delegated_child_subprocess_env` возвращает None вне делегирования). "
        "Пин дома сделал бы пробу непохожей на сам запуск."
    ),
}

_MAX_HOPS = 3


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _functions(tree: ast.AST) -> dict[str, list[ast.AST]]:
    out: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, []).append(node)
    return out


def _imported_functions(tree: ast.AST) -> dict[str, list[ast.AST]]:
    """Сборщики окружения, импортированные из других модулей движка.

    Общий сборщик ``_sanitize_subprocess_env`` живёт в
    ``tools/environments/local.py``, а зовут его из шести модулей — почти
    всегда локальным ``from ... import`` внутри функции. Без этого шага
    каждый такой вызов выглядел бы как «функция не найдена».

    Идём именно по импортам модуля, а не по общему индексу имён: одинаковое
    имя в чужом модуле не должно случайно оправдать спавн.
    """
    out: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module or node.level:
            continue
        path = REPO_ROOT / (node.module.replace(".", "/") + ".py")
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - дерево читаемо
            continue
        defined = _functions(ast.parse(source))
        for alias in node.names:
            for fn in defined.get(alias.name, []):
                out.setdefault(alias.asname or alias.name, []).append(fn)
    return out


def _reaches_contract(fn: ast.AST, funcs: dict, depth: int = 0,
                      seen: frozenset[str] = frozenset()) -> bool:
    """Зовёт ли *fn* контракт сама или через функцию своего модуля."""
    if depth > _MAX_HOPS:
        return False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in _CONTRACT:
            return True
        if name and name not in seen and name in funcs:
            for callee in funcs[name]:
                if _reaches_contract(callee, funcs, depth + 1, seen | {name}):
                    return True
    return False


def _scope_chain(tree: ast.AST, lineno: int) -> list[ast.AST]:
    """Объемлющие функции для строки — от внутренней к внешней.

    Без этого вложенная функция, которая замыкается на словарь из внешней
    (``browser_env`` в ``tools/browser_tool.py``), выглядит как нарушение.
    """
    chain = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno <= lineno <= (node.end_lineno or node.lineno)
    ]
    chain.sort(key=lambda n: n.lineno, reverse=True)
    return chain


def _resolve(expr: ast.AST, tree: ast.AST, funcs: dict,
             chain: list[ast.AST]) -> tuple[bool | None, str]:
    """(прошло ли окружение через контракт, как именно)."""
    if isinstance(expr, ast.Call):
        name = _call_name(expr)
        for callee in funcs.get(name, []):
            if _reaches_contract(callee, funcs):
                return True, f"{name}() зовёт контракт"
        if name in funcs:
            return False, f"{name}() контракт не зовёт"
        return None, f"{name}() определена вне модуля"

    if isinstance(expr, ast.IfExp):
        # `env=safe_env if safe_env else None` — важна непустая ветка.
        return _resolve(expr.body, tree, funcs, chain)

    if isinstance(expr, ast.Name):
        for scope in chain:
            for node in ast.walk(scope):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == expr.id:
                        ok, how = _resolve(node.value, tree, funcs, chain)
                        if ok:
                            return True, f"{expr.id} = {how}"
        for scope in chain:
            if _reaches_contract(scope, funcs):
                return True, f"{scope.name}() применяет контракт на месте"
        return False, f"{expr.id}: контракт не найден ни на одном пути"

    return None, "выражение не разбирается"


def _violations(source: str, rel: str) -> list[tuple[str, str]]:
    """Места в *source*, где собранное окружение уходит мимо контракта."""
    tree = ast.parse(source)
    funcs = {**_imported_functions(tree), **_functions(tree)}
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) not in _SPAWN:
            continue
        for kw in node.keywords:
            if kw.arg != "env":
                continue
            if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                continue
            chain = _scope_chain(tree, node.lineno)
            enclosing = chain[0].name if chain else "<модуль>"
            if (rel, enclosing) in _ALLOWLIST:
                continue
            ok, how = _resolve(kw.value, tree, funcs, chain)
            if ok is not True:
                where = f"{rel}:{node.lineno} ({enclosing})"
                found.append((where, f"env={ast.unparse(kw.value)} — {how}"))
    return found


class TestEverySpawnSiteGoesThroughTheContract:

    @pytest.mark.parametrize("rel", _ZONE)
    def test_the_zone_module_is_still_there(self, rel):
        """Переименовали модуль — прибор обязан упасть, а не опустеть.

        Молчащий сторож хуже отсутствующего: зелёный прогон читается как
        «спавны проверены», хотя проверять было нечего.
        """
        assert (REPO_ROOT / rel).is_file(), (
            f"{rel} из зоны спавнов пропал. Модуль переехал — обнови _ZONE, "
            f"иначе спавны из него больше никто не смотрит."
        )

    @pytest.mark.parametrize("rel", _ZONE)
    def test_the_spawn_site_env_comes_from_the_contract(self, rel):
        """Собранное окружение уходит ребёнку только через контракт."""
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        found = _violations(source, rel)
        assert not found, (
            "Спавн нагрузки профиля собирает окружение мимо контракта "
            "`apply_subprocess_containment_env` — в `contained` ребёнок "
            "получит настоящий дом ОС (RAF-161):\n"
            + "\n".join(f"  {where}: {why}" for where, why in found)
        )

    def test_the_guard_actually_catches_a_spawn_that_skips_the_contract(self):
        """Проба покрытия: прибор ловит новый спавн, а не только знакомый.

        Без неё «нарушений нет» неотличимо от «резолвер всё считает
        нормальным». Синтетический модуль повторяет форму дефекта MCP:
        своя сборка окружения по allowlist из `os.environ` и передача её
        в дочерний процесс.
        """
        leaky = (
            "import os, subprocess\n"
            "def _build_safe_env(user_env):\n"
            "    env = {k: v for k, v in os.environ.items() if k == 'HOME'}\n"
            "    if user_env:\n"
            "        env.update(user_env)\n"
            "    return env\n"
            "def spawn(cfg):\n"
            "    safe_env = _build_safe_env(cfg)\n"
            "    return subprocess.Popen(['server'], env=safe_env)\n"
        )
        assert _violations(leaky, "synthetic/leaky.py"), (
            "Сторож не увидел спавн, который собирает окружение мимо "
            "контракта — то есть не увидел бы и дефект RAF-161."
        )

        pinned = leaky.replace(
            "    if user_env:",
            "    from hermes_constants import apply_subprocess_containment_env\n"
            "    apply_subprocess_containment_env(env)\n"
            "    if user_env:",
        )
        assert not _violations(pinned, "synthetic/pinned.py"), (
            "Сторож считает нарушением спавн, который контракт зовёт — "
            "с таким резолвером он будет давать ложные блокеры."
        )
