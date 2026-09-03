"""Сборка дерева, которое уезжает клиенту.

Ветка витрины не является продолжением рабочей: она собирается заново на
каждый релиз. Источник — ``git archive`` рабочей ветки, то есть список
ОТСЛЕЖИВАЕМЫХ файлов. Ни `.venv`, ни `node_modules`, ни локальный `.env`
попасть сюда не могут физически, а не по договорённости.

Исключённые пути отсеиваются ДО записи на диск: файл, которого мы не
хотим отдавать, не должен существовать даже мгновение.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# Ровно то, что исключает установщик (scripts/install.sh:1436-1439).
# Состояние на диске у клиента от этого не меняется -- меняется то, что
# он качает и что видит зашедший в публичный репозиторий.
EXCLUDED_PATHS: tuple[str, ...] = (
    "docs/product",
    "website",
    "apps/desktop",
    "apps/bootstrap-installer",
    "scripts/desktop-update",
    "scripts/install.ps1",
    "scripts/install.cmd",
)

# Без этих двух витрина нерабочая: установщик тянется по первому пути,
# каталог моделей ходит за вторым по своему же адресу.
REQUIRED_PATHS: tuple[str, ...] = (
    "scripts/install.sh",
    "assets/api/model-catalog.json",
)

# ВРЕМЕННО, не по основаниям EXCLUDED_PATHS выше (те вообще не нужны на
# машине клиента) -- решение владельца от 2026-09-03. Первая страница
# публичного репозитория Trix Agent сейчас -- апстримная страница "Hermes
# Agent" с этим баннером и ссылками на сайт документации апстрима и его
# Discord. Ребрендинг -- отдельный проект (спека 3 его
# измерила и отложила); до него обложку витрины не публикуем. Список
# снимается целиком, когда ребрендинг закроет разрыв -- не дописывайте в
# него точечные фиксы, для этого он и не EXCLUDED_PATHS.
WITHHELD_PATHS: tuple[str, ...] = (
    "README.md",
    "README.es.md",
    "README.ur-pk.md",
    "README.zh-CN.md",
    "assets/banner.png",
)


def _matches_any(rel: str, prefixes: tuple[str, ...]) -> bool:
    """Путь совпадает с одним из ``prefixes`` -- как сам путь, как что-то
    под ним, или как файл с этим именем как основой (install.sh:1439
    исключает ``scripts/desktop-update*`` глобом, а не точным путём к
    каталогу; та же граница нужна и WITHHELD_PATHS, чтобы "README.md" не
    цепляло "docs/README.md" -- у него другой путь, совпадение только по
    имени файла на конце)."""
    for prefix in prefixes:
        if (
            rel == prefix
            or rel.startswith(prefix + "/")
            or rel.startswith(prefix + ".")
        ):
            return True
    return False


def is_excluded(rel: str) -> bool:
    """Попадает ли путь под исключение установщика (см. EXCLUDED_PATHS)."""
    return _matches_any(rel, EXCLUDED_PATHS)


def is_withheld(rel: str) -> bool:
    """Попадает ли путь под временное решение владельца не публиковать
    (см. WITHHELD_PATHS)."""
    return _matches_any(rel, WITHHELD_PATHS)


def is_hidden_from_release(rel: str) -> bool:
    """Не должен уехать в дерево витрины -- по любой из двух причин сразу.

    Сборка (``build_release_tree``) и проверка полноты
    (``verify_release_tree``) обязаны использовать именно этот предикат,
    а не только ``is_excluded``: иначе WITHHELD_PATHS будет отсутствовать
    в собранном дереве (как и задумано), но проверка полноты объявит его
    "пропавшим при сборке" -- ровно та находка, ради которой существует
    сверка ``export-ignore``.

    НЕ используется проверкой истории релизной ветки
    (``_validate_release_lineage``): та стережёт обещание install.sh на
    УЖЕ ОПУБЛИКОВАННЫХ коммитах, а README/баннер там могли законно лежать
    ДО того, как это временное решение было принято. Расширь она свой
    предикат на WITHHELD_PATHS -- заблокировала бы публикацию навсегда
    задним числом, из-за истории, которую уже не переписать.
    """
    return is_excluded(rel) or is_withheld(rel)


def build_release_tree(repo_root: Path, ref: str, dest: Path) -> list[str]:
    """Разложить в ``dest`` отслеживаемые файлы ``ref`` минус исключённые.

    Возвращает отсортированный список относительных путей файлов.
    """
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(
            f"dest должен быть пустым или не существовать -- дерево витрины "
            f"собирается заново на каждый релиз, а не поверх остатков "
            f"прошлой сборки: {dest}"
        )
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", str(repo_root), "archive", "--format=tar", ref],
        capture_output=True,
        check=True,
    )

    # filter="data" появился не во всех 3.11.x; без него tarfile ругается
    # предупреждением на 3.12+. Спрашиваем у самого модуля.
    extract_kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}

    written: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r|") as tar:
        for member in tar:
            if is_hidden_from_release(member.name):
                continue
            tar.extract(member, path=dest, **extract_kwargs)
            if member.isfile():
                written.append(member.name)
    return sorted(written)


def _ls_tree_paths(repo_root: Path, ref: str, *, blobs_only: bool) -> set[str]:
    """Пути из ``git ls-tree -r -z <ref>``, без git'овского экранирования имён.

    ``-z`` здесь не только "раздели NUL-байтом вместо перевода строки" --
    это ЕЩЁ и отключение quotepath. Без него git оборачивает в кавычки и
    экранирует октально любое имя с небезопасным для терминала байтом:
    ``docs/product/Слабые места.md`` превращается в строку, начинающуюся
    с символа ``"``, а не с ``docs/product/``. ``is_excluded()`` делает
    ``rel.startswith(prefix + "/")`` -- при таком экранировании сравнение
    молча никогда не совпадает, и путь перестаёт быть "исключённым" для
    всех сверок, что читают список через эту функцию. В этом репозитории
    такие имена реальны -- кириллические заголовки под ``docs/product/``.

    ``blobs_only`` отсеивает gitlink'и (подмодули, тип ``commit``, режим
    ``160000``): ``git archive`` их не архивирует вообще, там нет ни
    файла, ни каталога с содержимым, только ссылка на чужой коммит.
    Сравнивать собранное дерево с ожиданием "gitlink -> файл" значило бы
    гарантированно и заведомо ложно кричать про пропажу. Симлинков это не
    касается -- они такой же blob, просто с режимом ``120000``.
    """
    listing = subprocess.run(
        ["git", "-C", str(repo_root), "ls-tree", "-r", "-z", ref],
        capture_output=True, check=True,
    )
    paths: set[str] = set()
    for record in listing.stdout.split(b"\0"):
        if not record:
            continue
        meta, sep, path = record.partition(b"\t")
        if not sep or not path:
            continue
        fields = meta.split()
        if blobs_only and not (len(fields) >= 2 and fields[1] == b"blob"):
            continue
        paths.add(path.decode("utf-8", "surrogateescape"))
    return paths


def tracked_files(repo_root: Path, ref: str) -> set[str]:
    """Множество путей blob-объектов (обычных файлов и симлинков), отслеживаемых в ``ref``."""
    return _ls_tree_paths(repo_root, ref, blobs_only=True)


def verify_release_tree(dest: Path, tracked: set[str]) -> list[str]:
    """Претензии к собранному дереву. Пустой список -- публиковать можно.

    Четыре разных вопроса, и каждый уже однажды имел неверный ответ:
    не уехало ли лишнее, не потерялось ли обязательное (из двух файлов,
    без которых витрина нерабочая), не просочился ли файл, которого нет
    в git, и -- четвёртый, добавленный отдельно от первых трёх -- не
    ПРОПАЛ ли отслеживаемый файл вовсе. Первые три вопроса ловят лишнее
    и подмену; они не ловят недостачу: если ``git archive`` тихо
    отбрасывает файл (первый кандидат -- ``export-ignore`` в
    ``.gitattributes``), его не будет ни в ``dest``, ни в коммите, обе
    прежние сверки чисты, а клиент получает дырявое дерево.
    """
    problems: list[str] = []

    for rel in EXCLUDED_PATHS:
        if (dest / rel).exists():
            problems.append(f"исключённый путь попал в дерево витрины: {rel}")

    for rel in WITHHELD_PATHS:
        if (dest / rel).exists():
            problems.append(
                f"путь из временно скрытых (WITHHELD_PATHS, решение "
                f"владельца) попал в дерево витрины: {rel}"
            )

    for rel in REQUIRED_PATHS:
        if not (dest / rel).is_file():
            problems.append(f"обязательный файл отсутствует в дереве витрины: {rel}")

    expected = {rel for rel in tracked if not is_hidden_from_release(rel)}
    seen: set[str] = set()
    for path in sorted(dest.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(dest).as_posix()
        seen.add(rel)
        if rel not in tracked:
            problems.append(f"файл не отслеживается в рабочей ветке: {rel}")

    missing = sorted(expected - seen)
    if missing:
        problems.append(
            "отслеживаемые файлы пропали при сборке дерева витрины (проверьте "
            ".gitattributes на export-ignore): " + ", ".join(missing)
        )

    return problems


def verify_release_commit(repo_root: Path, sha: str, expected: list[str]) -> list[str]:
    """Претензии к уже созданному коммиту витрины. Пустой список -- ок.

    ``verify_release_tree`` смотрит на каталог ``dest`` ДО коммита -- это
    дешёвая проверка, но не гарантия: сам ``git add`` внутри
    ``commit_release_tree`` может отбросить часть файлов (например,
    из-за .gitignore), и тогда содержимое коммита разойдётся с тем, что
    лежало на диске. Эта функция сверяет ПУБЛИКУЕМЫЙ артефакт -- дерево
    самого коммита -- со списком, который вернул ``build_release_tree``.
    """
    committed = _ls_tree_paths(repo_root, sha, blobs_only=False)
    expected_set = set(expected)

    problems: list[str] = []

    missing = sorted(expected_set - committed)
    if missing:
        problems.append(
            "файлы собранного дерева не попали в коммит витрины: "
            + ", ".join(missing)
        )

    extra = sorted(committed - expected_set)
    if extra:
        problems.append(
            "в коммите витрины есть файлы, которых не было в собранном дереве: "
            + ", ".join(extra)
        )

    return problems


def _run_git(repo_root: Path, args: list[str], env: dict[str, str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root), env=env,
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def commit_release_tree(
    repo_root: Path, dest: Path, branch: str, message: str
) -> str:
    """Создать коммит с содержимым ``dest`` поверх текущей вершины ``branch``.

    Родитель коммита -- ``refs/heads/<branch>`` НА МОМЕНТ ВЫЗОВА. Ветка сама
    НЕ переносится -- это отдельный шаг, ``move_release_branch``, и его
    обязан вызывать вызывающий код ПОСЛЕ того, как ``verify_release_commit``
    подтвердит созданный коммит.

    Раньше это была одна функция, и перенос ветки был её последним шагом
    безусловно. Из-за этого забракованный коммит (сверка отказала уже
    ПОСЛЕ того, как ветка на него передвинута) всё равно оставался вершиной
    ``refs/heads/release``: следующий успешный релиз брал его родителем
    (см. код ниже, который читает ``refs/heads/<branch>``) и увозил в
    публичную историю вместе с собой -- необратимо, ровно то, ради чего
    писалась вся эта проверка. Разделение делает правильный порядок
    структурным, а не тем, что вызывающий обязан не забыть.

    Работает через отдельный индекс (``GIT_INDEX_FILE``) и plumbing, а не
    через ``git checkout``: рабочее дерево мастерской при релизе не должно
    шевелиться вовсе -- релиз идёт из той же папки, где мы работаем.
    """
    index = dest.parent / f".release-index-{branch}"
    if index.exists():
        index.unlink()

    env = dict(
        os.environ,
        GIT_DIR=str((repo_root / ".git").resolve()),
        GIT_WORK_TREE=str(dest.resolve()),
        GIT_INDEX_FILE=str(index.resolve()),
    )

    try:
        # --force: без него файлы, отслеживаемые в рабочей ветке вопреки
        # .gitignore (через git add -f), для этого свежего индекса выглядят
        # обычными новыми файлами и вычёркиваются игнор-правилами -- тихо,
        # без ошибки и без расхождения, которое заметил бы verify_release_tree
        # (он смотрит на dest, а не на то, что реально легло в коммит).
        _run_git(dest, ["add", "-A", "--force"], env)
        tree = _run_git(dest, ["write-tree"], env)

        parent = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=str(repo_root), env=env,
            capture_output=True, text=True,
        ).stdout.strip()

        args = ["commit-tree", tree, "-m", message]
        if parent:
            args += ["-p", parent]
        return _run_git(dest, args, env)
    finally:
        if index.exists():
            index.unlink()


def move_release_branch(repo_root: Path, branch: str, sha: str) -> None:
    """Перенести ``refs/heads/<branch>`` на ``sha``.

    Вызывать ТОЛЬКО после того, как ``verify_release_commit(repo_root, sha,
    ...)`` вернул пустой список. До этого момента ``sha`` -- непроверенный
    коммит, и делать его достижимым через ref означает рисковать увезти его
    в публичную историю следующим релизом, даже если сам он не был отправлен
    (см. docstring ``commit_release_tree``).
    """
    env = dict(os.environ, GIT_DIR=str((repo_root / ".git").resolve()))
    _run_git(repo_root, ["update-ref", f"refs/heads/{branch}", sha], env)


def _check(repo_root: Path, ref: str, out: Path | None = None) -> int:
    """Собрать дерево витрины и напечатать претензии.

    Ничего не публикует и ничего не коммитит: это проверка, которую скрипт
    релиза делает ДО прогона сюиты. Исключённые пути ОБЯЗАНЫ жить и
    отслеживаться в мастерской -- десктоп, сайт и наши документы продолжают
    жить и работать у нас. Витрина отличается от мастерской фильтром при
    сборке, а не составом рабочей ветки, поэтому здесь проверяется
    СОБРАННОЕ дерево, а не то, что отслеживается в ``ref``.

    Без ``out`` дерево собирается во временный каталог и удаляется вместе
    с ним -- проверять больше нечего, смотреть в нём нечего. С ``out``
    дерево остаётся на диске по указанному пути: спека §10 требует, чтобы
    первый релиз делался через ручной осмотр собранного дерева, а
    осматривать удалённый ``tempfile.TemporaryDirectory`` невозможно.
    """
    if out is not None:
        build_release_tree(repo_root, ref, out)
        problems = verify_release_tree(out, tracked_files(repo_root, ref))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "tree"
            build_release_tree(repo_root, ref, dest)
            problems = verify_release_tree(dest, tracked_files(repo_root, ref))

    if problems:
        print("Дерево витрины публиковать нельзя:")
        for problem in problems:
            print(f"    {problem}")
        return 1
    if out is not None:
        print(f"Дерево витрины проверено: публиковать можно. Собрано в {out}")
    else:
        print("Дерево витрины проверено: публиковать можно.")
    return 0


def _validate_release_lineage(repo_root: Path, ref: str, branch: str) -> list[str]:
    """Претензии к ТЕКУЩЕМУ состоянию ``refs/heads/<branch>`` до публикации.

    ``commit_release_tree`` берёт родителя нового коммита вслепую --
    ``git rev-parse --verify refs/heads/<branch>``, что бы там ни лежало.
    ``verify_release_commit`` сверяет только дерево НОВОГО коммита, ничьих
    предков не смотрит, а ``git push release release:release`` отправляет
    всё, достижимое из ref -- то есть всю ancestry. Один ручной
    ``git branch -f release <рабочая ветка>`` (или сохранившийся в дереве
    старый скрипт, который так и делал) -- и следующий ``--publish``
    построит поверх этого чистый коммит, сверка дерева пройдёт, а в
    публичный репозиторий уедут все коммиты рабочей истории целиком,
    включая ``docs/product``. Отсюда -- отдельная проверка ДО создания
    коммита, а не полагание на сверку уже созданного дерева.

    Пустой список -- публиковать можно. Если ``refs/heads/<branch>`` ещё
    не существует (первый релиз), проверять нечего и опасности нет --
    коммит будет создан без родителя.
    """
    release_ref = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet",
         f"refs/heads/{branch}"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not release_ref:
        return []

    problems: list[str] = []

    # 1. refs/heads/<branch> не должна быть частью истории публикуемого
    # ref'а. Легитимная релизная ветка -- отдельная цепочка orphan-коммитов
    # (commit_release_tree строит их через commit-tree с деревом, никогда
    # не совпадающим с рабочим), поэтому она НИКОГДА не является предком
    # рабочей ветки. Если она им является -- это не релизная история, а
    # чей-то ручной перевод ветки на рабочую.
    is_ancestor = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor",
         release_ref, ref],
        capture_output=True,
    )
    if is_ancestor.returncode == 0:
        problems.append(
            f"refs/heads/{branch} указывает на {release_ref} -- это часть "
            f"рабочей истории {ref}, а не отдельная релизная цепочка. "
            f"Публикация поверх неё увела бы всю ancestry рабочей ветки "
            f"(включая docs/product) в публичный репозиторий. Что делать: "
            f"убедиться, что ветка {branch} указывает на настоящий "
            f"последний релизный коммит (или, если её не было, удалить: "
            f"git branch -D {branch}), и повторить публикацию."
        )
        return problems  # дальше по этой ветке смотреть уже нечего

    # 2. Каждый коммит существующей истории релизной ветки обязан быть чист
    # от исключённых путей -- проверка на ИСТОРИИ ветки, а не на дереве
    # одного нового коммита. Цепочка "один коммит на версию" короткая,
    # проверка стоит копейки, а утверждает обещание спеки §5 на истории.
    commits = subprocess.run(
        ["git", "-C", str(repo_root), "rev-list", release_ref],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    for sha in commits:
        for rel in _ls_tree_paths(repo_root, sha, blobs_only=False):
            if is_excluded(rel):
                problems.append(
                    f"коммит {sha} в истории {branch} содержит исключённый "
                    f"путь: {rel}"
                )

    return problems


def _publish(repo_root: Path, ref: str, branch: str, version: str) -> int:
    """Собрать, закоммитить и опубликовать дерево витрины на ``branch``.

    Порядок -- lineage -> build -> verify_release_tree -> commit ->
    verify_release_commit -> перенос ветки -- жил дважды: здесь и в
    20-строчном heredoc внутри ``scripts/release_trix.sh``. Копия в bash
    была непроверяемой (скрипт, гоняющий сюиту, нельзя протестировать), а
    сама последовательность -- ровно то место, где раньше был перевёрнутый
    порядок (см. docstring ``commit_release_tree``): ветка обязана
    двигаться СТРОГО после того, как ``verify_release_commit`` подтвердит
    коммит, иначе непроверенный коммит становится вершиной ветки и может
    уехать в публичную историю следующим релизом.
    """
    lineage_problems = _validate_release_lineage(repo_root, ref, branch)
    if lineage_problems:
        print("Отказываюсь публиковать: ветка витрины непубликуема.", file=sys.stderr)
        for problem in lineage_problems:
            print(f"    {problem}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "tree"
        written = build_release_tree(repo_root, ref, dest)
        problems = verify_release_tree(dest, tracked_files(repo_root, ref))
        if problems:
            print("Отказываюсь публиковать: дерево витрины непубликуемо.", file=sys.stderr)
            for problem in problems:
                print(f"    {problem}", file=sys.stderr)
            return 1

        sha = commit_release_tree(repo_root, dest, branch, f"Trix Agent {version}")

        # Проверять каталог и публиковать коммит -- разные вопросы. `git add`
        # подчиняется .gitignore, поэтому между деревом и коммитом файлы
        # теряются молча. Ветка ниже переносится, только если сверка чиста.
        problems = verify_release_commit(repo_root, sha, written)
        if problems:
            print(
                f"Отказываюсь публиковать: коммит {sha} разошёлся с собранным деревом.",
                file=sys.stderr,
            )
            for problem in problems:
                print(f"    {problem}", file=sys.stderr)
            return 1

        move_release_branch(repo_root, branch, sha)

    print(sha)
    return 0


def _cli(argv: list[str] | None = None) -> int:
    """``python3 -m hermes_cli.release_tree --check <ref> [--out DIR]``
    или ``--publish <ref> <branch> <version>``.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="hermes_cli.release_tree")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", metavar="REF")
    group.add_argument("--publish", nargs=3, metavar=("REF", "BRANCH", "VERSION"))
    parser.add_argument(
        "--out", metavar="DIR", default=None,
        help="только с --check: оставить собранное дерево в DIR вместо "
             "временного каталога, чтобы его можно было осмотреть руками",
    )
    args = parser.parse_args(argv)

    repo_root = Path.cwd()
    if args.check is not None:
        out = Path(args.out) if args.out else None
        return _check(repo_root, args.check, out=out)

    if args.out is not None:
        parser.error("--out допустим только вместе с --check")

    ref, branch, version = args.publish
    return _publish(repo_root, ref, branch, version)


if __name__ == "__main__":
    raise SystemExit(_cli())
