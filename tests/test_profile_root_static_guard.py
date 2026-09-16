"""Храповик: мёрж апстрима не должен молча вернуть дыру в изоляции.

Задача этого теста — не найти дефект сегодня, а не дать появиться новому
завтра. Изоляция профиля держится на том, что приватные данные ищутся через
``user_home_path()``/``get_profile_tmp_dir()``, а не через ``Path.home()`` и
литерал ``/tmp``. Апстрим про наш контракт не знает: очередной мёрж принесёт
новый файл со старым способом, никакой тест его не заметит, и режим
``contained`` начнёт протекать в месте, о котором никто не вспомнит.

Поэтому здесь зафиксировано **базовое множество** мест, где такие обращения
были на момент реализации, с ЧИСЛОМ вхождений в каждом файле. Правило одно:

    ни одно число не может вырасти, и новых файлов не появляется

Счёт именно по вхождениям, а не по файлам. Пофайловый храповик держал
только периметр: любой из девяноста файлов базового множества мог набрать
сколько угодно НОВЫХ ``Path.home()`` и не уронить ни одного теста — а
именно эти файлы апстрим и правит чаще всего.

Тест падает, когда появляется файл вне множества или когда в знакомом файле
становится больше обращений. Это не значит «ты сделал плохо» — это значит,
что для нового места нужно принять решение и записать его:

* приватные данные профиля → перевести на ``user_home_path()``;
* интерфейс ОС (юнит, ``~/.local/bin``, desktop entry, сокет) → пометить
  комментарием ``# OS_RUNTIME`` и внести файл в
  ``hermes_constants.OS_RUNTIME_SITES``;
* общий неизменяемый машинный ассет → allowlist §6 в
  ``hermes_constants.PROFILE_ROOT_SHARED_IMMUTABLE``.

Файл, в котором таких мест стало меньше, обязан приехать в базовое
множество с новым числом; файл, избавившийся от них полностью, — исчезнуть
из него. Тест ругается и на это: храповик, который не подтягивают, перестаёт
держать.

Перегенерировать базовое множество после осознанной правки:

    python3 tests/test_profile_root_static_guard.py --rewrite-baseline
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests._source_tree import walk_files

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"

HOME_BASELINE = DATA_DIR / "profile_root_home_sites_baseline.txt"
TMP_BASELINE = DATA_DIR / "profile_root_tmp_literal_baseline.txt"

# Каталоги вне контракта: тесты описывают поведение (в том числе
# shared-host, где `Path.home()` — правильный ответ), а `evals`/`scripts`
# не участвуют в работе профиля у клиента.
_SKIP_TOP_LEVEL = {"tests", "evals", "scripts", "node_modules", ".venv", "venv", ".git"}

# Все способы спросить у ОС, где дом пользователя. Их больше, чем
# ``Path.home()``: сторож, знающий одну форму, охраняет одну форму.
#
#   * ``Path.home()`` и ``os.path.expanduser("~...")`` -- очевидные;
#   * ``.expanduser()`` в любой форме -- ``Path("~").expanduser()`` не
#     содержит слова ``home`` вообще и проходил мимо старого шаблона;
#   * ``os.environ["HOME"]`` / ``.get("HOME")`` -- чтение переменной в
#     обход резолвера;
#   * ``pwd.getpwuid(os.getuid()).pw_dir`` -- дом из /etc/passwd, который
#     не видит ни HOME, ни профиля, и потому опаснее всех остальных.
_HOME_RE = re.compile(
    r"""
      Path\.home\(\)
    | expanduser\(\s*["']~
    | \.expanduser\(\s*\)
    | environ\s*(?:\[\s*["']HOME["']\s*\]|\.get\(\s*["']HOME["'])
    | pwd\.getpwuid\(
    | \.pw_dir\b
    """,
    re.VERBOSE,
)
_TMP_RE = re.compile(r"""["']/tmp[/"']""")


def _tracked_python_files() -> list[Path] | None:
    """Пути ``*.py``, которые РЕАЛЬНО лежат в репозитории, по данным git.

    Возвращает ``None``, если git недоступен (распакованный архив без
    ``.git``) -- тогда вызывающий откатывается на обход дерева.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", "*.py"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [Path(p) for p in result.stdout.decode("utf-8").split("\0") if p]


def _prune_untracked_scan(rel_dir: Path) -> bool:
    """Каталоги, в которые обходу-запасному варианту заходить незачем.

    Ровно те же два условия, что фильтруют результат ниже, но заданные ДО
    спуска: ``_SKIP_TOP_LEVEL`` -- только верхний уровень (``plugins/foo/
    tests/`` храповик по-прежнему считает своим), ``node_modules``/
    ``__pycache__``/``.venv`` -- на любой глубине.
    """
    return (len(rel_dir.parts) == 1 and rel_dir.name in _SKIP_TOP_LEVEL) or (
        rel_dir.name in {"node_modules", "__pycache__", ".venv"}
    )


def _iter_engine_sources() -> list[Path]:
    """Только отслеживаемые исходники -- не всё, что валяется в дереве.

    Храповик описывает состав РЕПОЗИТОРИЯ: что принесёт мёрж апстрима. Обход
    рабочего каталога вместо этого описывал состав МАШИНЫ. Разница не
    теоретическая: ``scripts/release_trix.sh`` на время прогона распаковывает
    собранный sdist в ``hermes_agent-<версия>/`` в корне репозитория, и обход
    видел каждый файл движка дважды -- второй раз под неизвестным базовому
    множеству префиксом. Замерено 2026-09-14: полный релизный гейт покраснел
    на 79 "новых" файлах вида ``hermes_agent-0.20.0/agent/...``, хотя те же
    тесты в одиночку проходили -- каталог существует только во время гейта.

    Отбор по git не ослабляет храповик: неотслеживаемый файл не приедет
    мёржем, не уедет клиенту и вообще не является частью дерева, за составом
    которого храповик следит. Зато результат перестаёт зависеть от мусора
    сборки, оставшегося в рабочем каталоге.
    """
    tracked = _tracked_python_files()
    candidates = (
        [REPO_ROOT / rel for rel in tracked]
        if tracked is not None
        # Fallback when git can't answer. walk_files() so this rung doesn't
        # inherit the RAF-171 race: rglob descends into every excluded
        # directory before filtering, and under the gate's parallel workers
        # it raises FileNotFoundError on a __pycache__ that vanished.
        else [path for _, path in walk_files(REPO_ROOT, match="*.py",
                                             prune=_prune_untracked_scan)]
    )

    out: list[Path] = []
    for path in candidates:
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in _SKIP_TOP_LEVEL:
            continue
        if any(part in {"node_modules", "__pycache__", ".venv"} for part in rel.parts):
            continue
        out.append(path)
    return out


def _load_baseline(path: Path) -> dict[str, int]:
    """Базовое множество как ``путь -> число вхождений``.

    Формат строки: ``<путь><TAB><число>``. Строка без числа читается как
    ``1`` -- это переходная форма старого, пофайлового множества, и
    оставлена читаемой сознательно: молча уронить чужой файл со старым
    форматом хуже, чем принять его по минимуму.
    """
    entries: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rel, _, raw_count = line.partition("\t")
        try:
            count = int(raw_count.strip()) if raw_count.strip() else 1
        except ValueError:
            count = 1
        entries[rel.strip()] = count
    return entries


def _occurrences(pattern: re.Pattern) -> dict[str, int]:
    """Сколько раз шаблон встречается в каждом отслеживаемом исходнике."""
    found: dict[str, int] = {}
    for path in _iter_engine_sources():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = len(pattern.findall(text))
        if hits:
            found[str(path.relative_to(REPO_ROOT))] = hits
    return found


def _render_baseline(header: list[str], counts: dict[str, int]) -> str:
    lines = list(header)
    for rel in sorted(counts):
        lines.append(f"{rel}\t{counts[rel]}")
    return "\n".join(lines) + "\n"


def _ratchet(pattern: re.Pattern, baseline_path: Path, what: str) -> None:
    baseline = _load_baseline(baseline_path)
    current = _occurrences(pattern)

    added = sorted(set(current) - set(baseline))
    assert not added, (
        f"Новые файлы с {what} вне базового множества:\n  "
        + "\n  ".join(f"{rel} ({current[rel]})" for rel in added)
        + "\n\nЭто место должно принять решение об изоляции профиля:\n"
        "  • приватные данные профиля → hermes_constants.user_home_path()\n"
        "    (временный каталог → get_profile_tmp_dir())\n"
        "  • интерфейс ОС → комментарий # OS_RUNTIME + запись в\n"
        "    hermes_constants.OS_RUNTIME_SITES\n"
        "  • общий неизменяемый ассет → PROFILE_ROOT_SHARED_IMMUTABLE\n"
        f"После этого допишите файл в {baseline_path.name}, если место\n"
        "остаётся сознательно."
    )

    grew = sorted(
        (rel, baseline[rel], current[rel])
        for rel in current
        if rel in baseline and current[rel] > baseline[rel]
    )
    assert not grew, (
        f"В этих файлах стало БОЛЬШЕ мест с {what}:\n  "
        + "\n  ".join(f"{rel}: было {was}, стало {now}" for rel, was, now in grew)
        + "\n\nФайл уже в базовом множестве — это не разрешение добавлять\n"
        "в него новые такие места, а запись о тех, что были. Новое место\n"
        "либо переводится на резолвер, либо помечается и объясняется."
    )

    shrank = sorted(
        (rel, baseline[rel], current.get(rel, 0))
        for rel in baseline
        if current.get(rel, 0) < baseline[rel]
    )
    assert not shrank, (
        f"В этих файлах {what} стало меньше — подтяните "
        f"{baseline_path.name}, иначе храповик перестанет держать:\n  "
        + "\n  ".join(f"{rel}: было {was}, стало {now}" for rel, was, now in shrank)
        + f"\n\n  python3 {Path(__file__).name} --rewrite-baseline"
    )


class TestStaticRatchet:
    def test_no_new_files_reach_for_the_os_home(self):
        _ratchet(_HOME_RE, HOME_BASELINE, "Path.home()/expanduser('~')")

    def test_no_new_files_hardcode_the_shared_tmp(self):
        _ratchet(_TMP_RE, TMP_BASELINE, "литералом /tmp")


class TestAllowlistIsSingleSourced:
    """Allowlist и список OS_RUNTIME живут в одном месте — иначе сторож,
    доктор и документация начнут расходиться, и каждый будет прав по-своему."""

    def test_the_allowlist_is_importable_and_documented(self):
        from hermes_constants import (
            OS_RUNTIME_SITES,
            PROFILE_ROOT_OS_RUNTIME,
            PROFILE_ROOT_SHARED_IMMUTABLE,
        )

        assert PROFILE_ROOT_SHARED_IMMUTABLE and PROFILE_ROOT_OS_RUNTIME
        for entry, why in (*PROFILE_ROOT_SHARED_IMMUTABLE, *PROFILE_ROOT_OS_RUNTIME):
            assert entry and isinstance(entry, str)
            # Обоснование обязательно: пункт без причины нельзя ни проверить,
            # ни когда-либо убрать.
            assert why and len(why) > 10, f"пункт allowlist {entry!r} без обоснования"
        assert OS_RUNTIME_SITES

    def test_os_runtime_sites_exist_on_disk(self):
        """Список мест ОС не должен ссылаться на удалённые файлы — иначе он
        тихо перестанет что-либо описывать."""
        from hermes_constants import OS_RUNTIME_SITES

        missing = [s for s in OS_RUNTIME_SITES if not (REPO_ROOT / s).exists()]
        assert not missing, f"OS_RUNTIME_SITES ссылается на несуществующие файлы: {missing}"

    def test_os_runtime_sites_are_marked_in_the_code(self):
        """Каждый файл из списка обязан нести комментарий ``# OS_RUNTIME``.

        Смысл: человек, читающий ``Path.home()`` в этом файле, должен найти
        письменный ответ «почему тут не резолвер» рядом, а не в чужой голове.
        """
        from hermes_constants import OS_RUNTIME_SITES

        unmarked = []
        for site in OS_RUNTIME_SITES:
            path = REPO_ROOT / site
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "OS_RUNTIME" not in text:
                unmarked.append(site)
        assert not unmarked, (
            "Файлы объявлены интерфейсами ОС, но в коде это не написано — "
            "добавьте комментарий # OS_RUNTIME к соответствующим местам:\n  "
            + "\n  ".join(unmarked)
        )



def _rewrite_baselines() -> None:
    """Перегенерировать оба базовых множества по текущему дереву.

    Нужен после ОСОЗНАННОЙ правки — когда мест стало меньше и храповик
    просит подтянуть запись. Отдельной командой, а не автоматически:
    базовое множество, которое чинит себя само, не храповик.
    """
    for pattern, path, what in (
        (_HOME_RE, HOME_BASELINE, "Path.home() / expanduser / HOME / pw_dir"),
        (_TMP_RE, TMP_BASELINE, "литералом /tmp"),
    ):
        header = [
            "# Базовое множество храповика изоляции профиля.",
            f"# Файлы движка и число мест с {what}.",
            "# Числа могут только УМЕНЬШАТЬСЯ — см. tests/test_profile_root_static_guard.py.",
        ]
        path.write_text(_render_baseline(header, _occurrences(pattern)), encoding="utf-8")
        print(f"{path.name}: {len(_occurrences(pattern))} файлов")


if __name__ == "__main__":
    import sys

    if "--rewrite-baseline" in sys.argv:
        _rewrite_baselines()
    else:
        print(__doc__)
