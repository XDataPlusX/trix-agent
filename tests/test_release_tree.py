"""Дерево витрины: что попало, что не попало.

Тесты работают на временном репозитории, который создают сами. Настоящее
дерево продукта здесь не участвует: инвариант должен держаться на любом
репозитории, а не на снимке нашего.
"""

import subprocess
from pathlib import Path

import pytest

from hermes_cli.release_tree import (
    EXCLUDED_PATHS,
    WITHHELD_PATHS,
    build_release_tree,
    commit_release_tree,
    is_excluded,
    move_release_branch,
    tracked_files,
    verify_release_commit,
    verify_release_tree,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Крошечный репозиторий с файлом в каждом исключённом пути."""
    root = tmp_path / "src"
    root.mkdir()
    _git(root.parent, "init", "-q", "-b", "work", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")

    (root / "scripts").mkdir()
    (root / "assets" / "api").mkdir(parents=True)
    (root / "hermes_cli").mkdir()
    (root / "scripts" / "install.sh").write_text("#!/bin/sh\n")
    (root / "assets" / "api" / "model-catalog.json").write_text("{}\n")
    (root / "hermes_cli" / "main.py").write_text("x = 1\n")

    for rel in EXCLUDED_PATHS:
        path = root / rel
        if path.suffix:                       # scripts/install.ps1 и .cmd
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("secret\n")
        else:
            path.mkdir(parents=True, exist_ok=True)
            (path / "secret.txt").write_text("secret\n")

    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    return root


@pytest.fixture
def repo_with_withheld_paths(repo: Path) -> Path:
    """``repo`` плюс файлы из WITHHELD_PATHS плюс два пограничных случая.

    ``docs/README.md`` и ``skills/example/README.md`` НЕ входят в
    WITHHELD_PATHS -- у них другой путь, совпадающий с "README.md" только
    по имени файла на конце, а не по полному относительному пути. Правило
    обязано не задевать их.
    """
    for rel in WITHHELD_PATHS:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("временно не публикуется\n")

    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "README.md").write_text("не корневой README\n")
    (repo / "skills" / "example").mkdir(parents=True, exist_ok=True)
    (repo / "skills" / "example" / "README.md").write_text("README скилла\n")

    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "withheld paths + boundary readmes")
    return repo


def test_withheld_paths_are_not_copied_to_the_tree(
    repo_with_withheld_paths: Path, tmp_path: Path
):
    """Файл из «пока не отправляем» отслеживается в исходнике, но не
    попадает в собранное дерево витрины."""
    dest = tmp_path / "out"
    written = build_release_tree(repo_with_withheld_paths, "work", dest)
    for rel in WITHHELD_PATHS:
        assert not (dest / rel).exists(), rel
        assert rel not in written, rel


def test_withheld_paths_are_not_reported_as_missing(
    repo_with_withheld_paths: Path, tmp_path: Path
):
    """Отсутствие скрытых файлов в дереве -- ожидаемое, а не находка
    "пропали при сборке": проверка полноты обязана молчать."""
    dest = tmp_path / "out"
    build_release_tree(repo_with_withheld_paths, "work", dest)
    problems = verify_release_tree(
        dest, tracked_files(repo_with_withheld_paths, "work")
    )
    assert problems == [], problems


def test_required_files_survive_withholding_of_banner(
    repo_with_withheld_paths: Path, tmp_path: Path
):
    """Защита от слишком широкого правила: скрыт assets/banner.png, но
    обязательные файлы (в частности model-catalog.json) не задеты."""
    dest = tmp_path / "out"
    build_release_tree(repo_with_withheld_paths, "work", dest)
    assert (dest / "assets" / "api" / "model-catalog.json").is_file()
    assert (dest / "scripts" / "install.sh").is_file()


def test_readme_boundary_nested_readmes_are_not_withheld(
    repo_with_withheld_paths: Path, tmp_path: Path
):
    """README.md в корне скрыт, но docs/README.md и
    skills/example/README.md -- другие пути и обязаны доехать."""
    dest = tmp_path / "out"
    written = build_release_tree(repo_with_withheld_paths, "work", dest)
    assert (dest / "docs" / "README.md").is_file()
    assert (dest / "skills" / "example" / "README.md").is_file()
    assert "docs/README.md" in written
    assert "skills/example/README.md" in written


def test_excluded_paths_never_land_in_the_tree(repo: Path, tmp_path: Path):
    dest = tmp_path / "out"
    build_release_tree(repo, "work", dest)
    for rel in EXCLUDED_PATHS:
        assert not (dest / rel).exists(), rel


def test_product_files_do_land_in_the_tree(repo: Path, tmp_path: Path):
    dest = tmp_path / "out"
    written = build_release_tree(repo, "work", dest)
    assert "scripts/install.sh" in written
    assert "assets/api/model-catalog.json" in written
    assert (dest / "hermes_cli" / "main.py").read_text() == "x = 1\n"


def test_untracked_junk_is_not_copied(repo: Path, tmp_path: Path):
    """Источник — список файлов git, а не содержимое папки."""
    (repo / ".env").write_text("SECRET=1\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "junk.js").write_text("junk\n")

    dest = tmp_path / "out"
    build_release_tree(repo, "work", dest)

    assert not (dest / ".env").exists()
    assert not (dest / "node_modules").exists()


def test_build_release_tree_refuses_a_dirty_dest(repo: Path, tmp_path: Path):
    """"Собирается заново" -- значит не поверх остатков прошлой сборки."""
    dest = tmp_path / "out"
    build_release_tree(repo, "work", dest)

    with pytest.raises(ValueError):
        build_release_tree(repo, "work", dest)


def test_glob_style_exclusion_matches_by_prefix_boundary():
    """scripts/desktop-update* в install.sh -- глоб, а не точный путь.

    is_excluded обязан ловить scripts/desktop-update.ps1 (файл, а не
    каталог из списка EXCLUDED_PATHS), но не должен цеплять пути, которые
    просто начинаются с той же строки символов без разделителя "/" или ".".
    """
    assert is_excluded("scripts/desktop-update.ps1")
    assert not is_excluded("website-legacy/x.md")
    assert not is_excluded("docs/production.md")


def test_force_added_ignored_files_land_in_the_release_commit(repo: Path, tmp_path: Path):
    """.gitignore не должен вычёркивать файлы, отслеживаемые вопреки ему.

    ``commit_release_tree`` строит новый индекс с нуля, поэтому для
    ``git add`` все файлы в ``dest`` выглядят новыми -- и подпадают под
    правила игнорирования, даже если в рабочей ветке файл отслеживается
    (был добавлен через ``git add -f``).
    """
    (repo / ".gitignore").write_text("*.woff2\n")
    (repo / "web").mkdir()
    (repo / "web" / "font.woff2").write_bytes(b"binary-font-data")
    _git(repo, "add", "-f", ".gitignore", "web/font.woff2")
    _git(repo, "commit", "-qm", "add ignored-but-tracked font")

    dest = tmp_path / "out"
    written = build_release_tree(repo, "work", dest)
    sha = commit_release_tree(repo, dest, "release", "Trix Agent 0.1.0")

    committed = set(_git(repo, "ls-tree", "-r", "--name-only", sha).split())
    assert "web/font.woff2" in committed
    assert committed == set(written)


def test_verify_release_commit_matches_the_built_tree(repo: Path, tmp_path: Path):
    dest = tmp_path / "out"
    written = build_release_tree(repo, "work", dest)
    sha = commit_release_tree(repo, dest, "release", "Trix Agent 0.1.0")

    assert verify_release_commit(repo, sha, written) == []


def test_verify_release_commit_reports_a_file_missing_from_the_commit(repo: Path, tmp_path: Path):
    dest = tmp_path / "out"
    written = build_release_tree(repo, "work", dest)
    sha = commit_release_tree(repo, dest, "release", "Trix Agent 0.1.0")

    problems = verify_release_commit(repo, sha, written + ["ghost.txt"])
    assert any("ghost.txt" in p for p in problems), problems


def test_verify_release_commit_reports_a_file_extra_in_the_commit(repo: Path, tmp_path: Path):
    """Мутация на публикуемом артефакте, а не на каталоге dest."""
    dest = tmp_path / "out"
    written = build_release_tree(repo, "work", dest)
    (dest / "sneaked.txt").write_text("не из build_release_tree\n")
    sha = commit_release_tree(repo, dest, "release", "Trix Agent 0.1.0")

    problems = verify_release_commit(repo, sha, written)
    assert any("sneaked.txt" in p for p in problems), problems


def test_clean_tree_has_no_complaints(repo: Path, tmp_path: Path):
    dest = tmp_path / "out"
    build_release_tree(repo, "work", dest)
    assert verify_release_tree(dest, tracked_files(repo, "work")) == []


def test_excluded_path_is_reported(repo: Path, tmp_path: Path):
    """Мутация: кладём документы обратно -- проверка обязана покраснеть."""
    dest = tmp_path / "out"
    build_release_tree(repo, "work", dest)
    (dest / "docs" / "product").mkdir(parents=True)
    (dest / "docs" / "product" / "STATUS.md").write_text("наши слабые места\n")

    problems = verify_release_tree(dest, tracked_files(repo, "work"))
    assert any("docs/product" in p for p in problems), problems


def test_missing_required_file_is_reported(repo: Path, tmp_path: Path):
    dest = tmp_path / "out"
    build_release_tree(repo, "work", dest)
    (dest / "assets" / "api" / "model-catalog.json").unlink()

    problems = verify_release_tree(dest, tracked_files(repo, "work"))
    assert any("model-catalog.json" in p for p in problems), problems


def test_untracked_file_in_tree_is_reported(repo: Path, tmp_path: Path):
    dest = tmp_path / "out"
    build_release_tree(repo, "work", dest)
    (dest / "leaked.key").write_text("ключ\n")

    problems = verify_release_tree(dest, tracked_files(repo, "work"))
    assert any("leaked.key" in p for p in problems), problems


def test_export_ignore_dropped_file_is_reported_as_missing(repo: Path, tmp_path: Path):
    """IMPORTANT находка 3: пропажа при сборке не ловилась вовсе.

    Три прежних вопроса (лишний путь, нет обязательного, неотслеживаемый
    файл) не ловят НЕДОСТАЧУ: если ``git archive`` тихо не отдаёт
    отслеживаемый файл (``export-ignore`` в ``.gitattributes`` -- первый
    кандидат), его не будет ни в ``dest``, ни в списке ``written``, обе
    прежние сверки чисты, а клиент получает дырявое дерево.
    """
    (repo / ".gitattributes").write_text("hermes_cli/main.py export-ignore\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "export-ignore hermes_cli/main.py")

    dest = tmp_path / "out"
    written = build_release_tree(repo, "work", dest)

    assert "hermes_cli/main.py" not in written, (
        "export-ignore обязан не дать git archive отдать файл -- иначе "
        "сценарий находки не воспроизведён"
    )

    problems = verify_release_tree(dest, tracked_files(repo, "work"))
    assert any("hermes_cli/main.py" in p for p in problems), problems


def test_first_release_creates_a_rootless_commit(repo: Path, tmp_path: Path):
    dest = tmp_path / "out"
    build_release_tree(repo, "work", dest)
    sha = commit_release_tree(repo, dest, "release", "Trix Agent 0.1.0")
    move_release_branch(repo, "release", sha)

    assert _git(repo, "rev-parse", "release").strip() == sha
    parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).split()
    assert len(parents) == 1, "у первого релиза не должно быть родителя"


def test_second_release_continues_the_first(repo: Path, tmp_path: Path):
    first_dest = tmp_path / "out1"
    build_release_tree(repo, "work", first_dest)
    first = commit_release_tree(repo, first_dest, "release", "Trix Agent 0.1.0")
    move_release_branch(repo, "release", first)

    (repo / "hermes_cli" / "main.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two")

    second_dest = tmp_path / "out2"
    build_release_tree(repo, "work", second_dest)
    second = commit_release_tree(repo, second_dest, "release", "Trix Agent 0.2.0")
    move_release_branch(repo, "release", second)

    parents = _git(repo, "rev-list", "--parents", "-n", "1", second).split()
    assert parents[1:] == [first], "второй релиз обязан продолжать первый"


def test_release_history_never_contains_our_documents(repo: Path, tmp_path: Path):
    """Главное обещание спеки, проверенное на истории, а не на дереве."""
    dest = tmp_path / "out"
    build_release_tree(repo, "work", dest)
    sha = commit_release_tree(repo, dest, "release", "Trix Agent 0.1.0")
    move_release_branch(repo, "release", sha)

    touched = _git(repo, "log", "--name-only", "--pretty=format:", "release")
    assert "docs/product" not in touched


def test_commit_does_not_disturb_the_working_tree(repo: Path, tmp_path: Path):
    dest = tmp_path / "out"
    build_release_tree(repo, "work", dest)
    sha = commit_release_tree(repo, dest, "release", "Trix Agent 0.1.0")
    move_release_branch(repo, "release", sha)

    assert _git(repo, "status", "--porcelain").strip() == ""
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "work"


def test_commit_release_tree_does_not_move_the_branch(repo: Path, tmp_path: Path):
    """Important 2: движение ветки -- отдельный, явный шаг.

    Раньше ``commit_release_tree`` двигало ``refs/heads/<branch>``
    безусловно, последним действием внутри себя. Из-за этого отказ
    сверки, которая идёт уже ПОСЛЕ вызова, ничего не откатывал --
    забракованный коммит оставался вершиной ветки.
    """
    dest = tmp_path / "out"
    build_release_tree(repo, "work", dest)
    sha = commit_release_tree(repo, dest, "release", "Trix Agent 0.1.0")

    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "release"],
        capture_output=True,
    )
    assert result.returncode != 0, (
        "commit_release_tree не должна создавать/двигать refs/heads/release"
    )
    assert sha  # коммит тем не менее создан -- есть что публиковать после сверки


def test_failed_verification_leaves_the_branch_untouched_on_first_release(
    repo: Path, tmp_path: Path
):
    """Обязательный тест из раунда правок 2: сборка, коммит, отказ сверки,
    ветка НЕ сдвинулась -- на первом релизе её вообще не появилось.

    Без разделения ``commit_release_tree``/``move_release_branch``
    забракованный коммит остался бы вершиной ``refs/heads/release`` и уехал
    бы в публичную историю следующим релизом -- необратимо.
    """
    dest = tmp_path / "out"
    written = build_release_tree(repo, "work", dest)
    (dest / "sneaked.txt").write_text("не из build_release_tree\n")
    sha = commit_release_tree(repo, dest, "release", "Trix Agent 0.1.0")

    problems = verify_release_commit(repo, sha, written)
    assert problems, "сверка обязана найти лишний файл"

    # Вызывающий код (как в hermes_cli.release_tree._publish) обязан не
    # звать move_release_branch, раз сверка отказала -- и здесь мы её
    # сознательно не зовём, проверяя, что ветка от этого и не появилась.
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "release"],
        capture_output=True,
    )
    assert result.returncode != 0, "ветки release не должно существовать вовсе"


def test_failed_verification_leaves_the_branch_on_the_previous_release(
    repo: Path, tmp_path: Path
):
    """Тот же обязательный тест, но на втором релизе: ветка обязана остаться
    на предыдущем, подтверждённом релизе, а не сдвинуться на забракованный.
    """
    first_dest = tmp_path / "out1"
    write_first = build_release_tree(repo, "work", first_dest)
    first = commit_release_tree(repo, first_dest, "release", "Trix Agent 0.1.0")
    move_release_branch(repo, "release", first)
    assert verify_release_commit(repo, first, write_first) == []

    second_dest = tmp_path / "out2"
    written_second = build_release_tree(repo, "work", second_dest)
    (second_dest / "sneaked.txt").write_text("не из build_release_tree\n")
    second = commit_release_tree(repo, second_dest, "release", "Trix Agent 0.2.0")

    problems = verify_release_commit(repo, second, written_second)
    assert problems, "сверка обязана найти лишний файл"

    assert _git(repo, "rev-parse", "release").strip() == first, (
        "ветка обязана остаться на предыдущем релизе, а не на забракованном"
    )
