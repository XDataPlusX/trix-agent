"""Сторож витрины проверяет собранное дерево, а не состав рабочей ветки --
и сам скрипт релиза действительно останавливается ДО прогона сюиты.

``docs/product`` (и остальные шесть исключённых путей) ОБЯЗАНЫ жить и
отслеживаться в мастерской -- десктоп, сайт и наши документы продолжают
жить и работать у нас. Витрина отличается от мастерской фильтром при
сборке, а не составом рабочей ветки. Поэтому нормальное состояние
рабочей ветки (``docs/product`` отслеживается) обязано ПРОХОДИТЬ
``--check`` -- а красным сторож должен становиться на реальной
претензии к собранному дереву, а не на самом факте существования
``docs/product`` в git.

Тесты на ``--publish`` проверяют, что коммит создаётся и ветка переносится
только при чистой сверке (Important 2) -- отказ обязан оставить
``refs/heads/release`` нетронутой. Последний тест запускает НАСТОЯЩИЙ
``scripts/release_trix.sh`` (не только модуль под ним) на временном
репозитории: порядок «стоп до сюиты», ``|| die``, выбор интерпретатора и
связка commit->verify->tag покрыты только запуском, не чтением исходника.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from hermes_cli.release_tree import build_release_tree

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "release_trix.sh"
# sys.executable -- тот же интерпретатор, под которым идёт сама сюита, а не
# первый попавшийся python3 из PATH (на хосте с Homebrew это другая среда).
CHECKER = [sys.executable, "-m", "hermes_cli.release_tree", "--check"]
PUBLISHER = [sys.executable, "-m", "hermes_cli.release_tree", "--publish"]


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(REPO_ROOT)}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def test_check_passes_with_docs_product_tracked_and_filters_it_out(tmp_path: Path):
    repo = tmp_path / "src"
    repo.mkdir()
    _git(tmp_path, "init", "-q", "-b", "work", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "scripts").mkdir()
    (repo / "assets" / "api").mkdir(parents=True)
    (repo / "docs" / "product").mkdir(parents=True)
    (repo / "scripts" / "install.sh").write_text("#!/bin/sh\n")
    (repo / "assets" / "api" / "model-catalog.json").write_text("{}\n")
    (repo / "docs" / "product" / "STATUS.md").write_text("слабые места\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "normal state -- docs/product tracked")

    result = subprocess.run(
        [*CHECKER, "work"],
        cwd=str(repo), capture_output=True, text=True, env=_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # Не просто "проверка промолчала" -- фильтр действительно вырезал
    # docs/product из СОБРАННОГО дерева, что и делает витрину публикуемой.
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "tree"
        written = build_release_tree(repo, "work", dest)
        assert not (dest / "docs" / "product").exists()
        assert "docs/product/STATUS.md" not in written
        assert (dest / "scripts" / "install.sh").is_file()
        assert (dest / "assets" / "api" / "model-catalog.json").is_file()


def test_check_refuses_when_a_required_file_is_missing(tmp_path: Path):
    repo = tmp_path / "src"
    repo.mkdir()
    _git(tmp_path, "init", "-q", "-b", "work", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "install.sh").write_text("#!/bin/sh\n")
    # assets/api/model-catalog.json намеренно отсутствует -- без него
    # каталог моделей у клиента даёт 404 на своём же адресе.
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "missing the model catalog")

    result = subprocess.run(
        [*CHECKER, "work"],
        cwd=str(repo), capture_output=True, text=True, env=_env(),
    )
    assert result.returncode == 1
    assert "assets/api/model-catalog.json" in result.stdout + result.stderr


def test_check_with_out_leaves_the_tree_on_disk_for_manual_inspection(tmp_path: Path):
    """IMPORTANT находка 2: сухой прогон обязан оставлять что осматривать.

    Без ``--out`` ``--check`` собирает дерево в ``tempfile.TemporaryDirectory``,
    который удаляется сам -- спека §10 требует ручного осмотра собранного
    дерева на первом релизе, а осматривать нечего. С ``--out DIR`` дерево
    остаётся на диске по DIR после успешного завершения ``--check``.
    """
    repo = tmp_path / "src"
    repo.mkdir()
    _git(tmp_path, "init", "-q", "-b", "work", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "scripts").mkdir()
    (repo / "assets" / "api").mkdir(parents=True)
    (repo / "scripts" / "install.sh").write_text("#!/bin/sh\n")
    (repo / "assets" / "api" / "model-catalog.json").write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")

    out_dir = tmp_path / "inspect-me"
    result = subprocess.run(
        [*CHECKER, "work", "--out", str(out_dir)],
        cwd=str(repo), capture_output=True, text=True, env=_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert str(out_dir) in result.stdout, (
        "сообщение обязано называть путь, чтобы человеку было что открыть"
    )
    assert (out_dir / "scripts" / "install.sh").is_file()
    assert (out_dir / "assets" / "api" / "model-catalog.json").is_file()


def test_publish_commits_and_moves_the_branch(tmp_path: Path):
    """CLI-обёртка `--publish` целиком, не только функции под ней.

    Первый релиз: коммит создан, ветка на него переведена. Второй релиз
    (после изменения дерева): коммит продолжает первый.
    """
    repo = tmp_path / "src"
    repo.mkdir()
    _git(tmp_path, "init", "-q", "-b", "work", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "scripts").mkdir()
    (repo / "assets" / "api").mkdir(parents=True)
    (repo / "scripts" / "install.sh").write_text("#!/bin/sh\n")
    (repo / "assets" / "api" / "model-catalog.json").write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")

    result = subprocess.run(
        [*PUBLISHER, "work", "release", "0.1.0"],
        cwd=str(repo), capture_output=True, text=True, env=_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    first_sha = result.stdout.strip()
    assert first_sha
    assert _git(repo, "rev-parse", "release").strip() == first_sha

    (repo / "assets" / "api" / "model-catalog.json").write_text('{"v": 2}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two")

    result = subprocess.run(
        [*PUBLISHER, "work", "release", "0.2.0"],
        cwd=str(repo), capture_output=True, text=True, env=_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    second_sha = result.stdout.strip()
    assert second_sha != first_sha
    assert _git(repo, "rev-parse", "release").strip() == second_sha
    parents = _git(repo, "rev-list", "--parents", "-n", "1", second_sha).split()
    assert parents[1:] == [first_sha], "второй релиз обязан продолжать первый"


def test_publish_refuses_a_broken_tree_and_leaves_the_branch_untouched(tmp_path: Path):
    """Important 2 на уровне CLI: отказ `--publish` не двигает ветку.

    Первый релиз проходит нормально. Второй ломает дерево (обязательный
    файл пропал) -- `--publish` обязан отказать и НЕ трогать `release`.
    """
    repo = tmp_path / "src"
    repo.mkdir()
    _git(tmp_path, "init", "-q", "-b", "work", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "scripts").mkdir()
    (repo / "assets" / "api").mkdir(parents=True)
    (repo / "scripts" / "install.sh").write_text("#!/bin/sh\n")
    (repo / "assets" / "api" / "model-catalog.json").write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")

    result = subprocess.run(
        [*PUBLISHER, "work", "release", "0.1.0"],
        cwd=str(repo), capture_output=True, text=True, env=_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    first_sha = _git(repo, "rev-parse", "release").strip()

    (repo / "assets" / "api" / "model-catalog.json").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "break the tree")

    result = subprocess.run(
        [*PUBLISHER, "work", "release", "0.2.0"],
        cwd=str(repo), capture_output=True, text=True, env=_env(),
    )
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "Отказываюсь публиковать" in output
    assert "model-catalog.json" in output
    assert _git(repo, "rev-parse", "release").strip() == first_sha, (
        "ветка обязана остаться на предыдущем релизе"
    )


def test_publish_refuses_when_release_branch_points_into_work_history(tmp_path: Path):
    """CRITICAL находка 1: родитель релизного коммита берётся вслепую.

    ``commit_release_tree`` читает родителя как ``git rev-parse --verify
    refs/heads/<branch>``, что бы там ни лежало, а сверка (``verify_release_commit``)
    смотрит только дерево НОВОГО коммита -- предков не смотрит никто. Ровно
    найденный сценарий: живёт рабочая копия со старым скриптом, который
    делал ``git branch -f release <рабочая ветка>``; здесь тот же эффект
    воспроизведён напрямую ``git branch release work`` -- ``release``
    указывает прямо на вершину рабочей ветки с отслеживаемым
    ``docs/product``. Без проверки истории ``--publish`` построил бы поверх
    неё чистый коммит, сверка дерева прошла бы, и ``git push release
    release:release`` увёз бы всю ancestry рабочей ветки -- 460 коммитов с
    ``docs/product`` -- в публичный репозиторий необратимо.
    """
    repo = tmp_path / "src"
    repo.mkdir()
    _git(tmp_path, "init", "-q", "-b", "work", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "scripts").mkdir()
    (repo / "assets" / "api").mkdir(parents=True)
    (repo / "docs" / "product").mkdir(parents=True)
    (repo / "scripts" / "install.sh").write_text("#!/bin/sh\n")
    (repo / "assets" / "api" / "model-catalog.json").write_text("{}\n")
    (repo / "docs" / "product" / "STATUS.md").write_text("слабые места\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "работа с отслеживаемым docs/product")
    work_tip = _git(repo, "rev-parse", "work").strip()

    # Ровно найденный сценарий -- вручную (или старым скриптом) переведённая
    # ветка release на рабочую историю, а не отдельная релизная цепочка.
    _git(repo, "branch", "release", "work")

    result = subprocess.run(
        [*PUBLISHER, "work", "release", "0.1.0"],
        cwd=str(repo), capture_output=True, text=True, env=_env(),
    )
    assert result.returncode != 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "release" in output

    # release не должна была сдвинуться -- никакой новый коммит не создан
    # и не стал вершиной ветки поверх скомпрометированного родителя.
    assert _git(repo, "rev-parse", "release").strip() == work_tip, (
        "ветка release не должна была измениться после отказа"
    )


def test_script_stops_before_the_suite_on_an_unpublishable_tree(tmp_path: Path):
    """Настоящий ``scripts/release_trix.sh``, не только модуль под ним.

    Дерево непубликуемо (нет ``assets/api/model-catalog.json``). Скрипт
    обязан отказать на проверке дерева -- до мёржа upstream и до прогона
    сюиты. Фиктивный ``run_tests.sh`` оставляет файл-маркер, если его
    вообще вызвали -- по отсутствию файла видно, что сюита не запускалась.

    Раньше маркером была строка ``echo SUITE-ACTUALLY-RAN``, а проверка --
    "нет её в выводе release_trix.sh". Это никогда не могло покраснеть:
    ``collect_failures`` в самом скрипте (``scripts/release_trix.sh``)
    забирает вывод ``run_tests.sh`` командной подстановкой в переменную
    ``LAST_RUN_OUTPUT``, а не печатает его на stdout/stderr -- так что
    строка не появлялась бы в выводе, даже если сюиту реально запустили.
    Файл на диске такой лазейки не имеет: `touch` не зависит от того, что
    и куда скрипт-обёртка перенаправляет вывод дочернего процесса.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)

    _git(tmp_path, "init", "-q", "-b", "xdata-agent", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    # Настоящий скрипт -- REPO_ROOT в нём вычисляется от собственного
    # расположения файла, поэтому он обязан физически лежать под
    # <временный репозиторий>/scripts/, а не запускаться из другого места.
    shutil.copy2(RELEASE_SCRIPT, repo / "scripts" / "release_trix.sh")
    (repo / "scripts" / "release_trix.sh").chmod(0o755)

    suite_ran_marker = repo / "scripts" / "SUITE_RAN.marker"
    fake_runner = repo / "scripts" / "run_tests.sh"
    fake_runner.write_text(
        f"#!/usr/bin/env bash\ntouch '{suite_ran_marker}'\nexit 0\n"
    )
    fake_runner.chmod(0o755)

    # scripts/install.sh отслеживается, а обязательный
    # assets/api/model-catalog.json -- нет: дерево непубликуемо.
    (repo / "scripts" / "install.sh").write_text("#!/bin/sh\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    # Фиктивные remote'ы: без upstream скрипт откажет раньше по преflight-
    # причине (нет remote'а для мёржа), а release явно просят завести в
    # задании -- это не то, что мы здесь проверяем, `git remote get-url`
    # читает только конфиг и не ходит в сеть, так что реальный репозиторий
    # на другом конце не нужен.
    _git(repo, "remote", "add", "upstream", "https://example.invalid/upstream.git")
    _git(repo, "remote", "add", "release", "https://example.invalid/release.git")

    result = subprocess.run(
        ["bash", "scripts/release_trix.sh", "--dry-run", "0.0.1"],
        cwd=str(repo), capture_output=True, text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT),
            "WORK_BRANCH": "xdata-agent",
            "RELEASE_REMOTE": "release",
            "UPSTREAM_REMOTE": "upstream",
        },
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "model-catalog.json" in output, output
    assert not suite_ran_marker.exists(), (
        "run_tests.sh был вызван -- сюита запустилась раньше проверки дерева"
    )


def test_skip_upstream_merge_flag_skips_fetch_and_announces_it_loudly(tmp_path: Path):
    """--skip-upstream-merge: первый релиз выходит ровно проверенным деревом.

    Опорный факт, который делает этот тест исполнимым без сети и без
    настоящего апстрима: во временном репозитории remote'а ``upstream`` нет
    вовсе. Без флага скрипт обязан умереть на преflight-проверке этого
    remote'а ("нечем мёржить"), даже не дойдя до проверки дерева витрины.
    С флагом эта проверка вовсе не выполняется -- прогон уходит дальше и
    умирает на ДРУГОЙ, более поздней причине (здесь -- отсутствие
    baseline-файла), которая ничего не знает про апстрим.

    Различаем эти две смерти по СОДЕРЖИМОМУ вывода, а не по коду возврата --
    оба прогона возвращают ненулевой код:
      * без флага в выводе обязан быть след попытки работать с апстримом
        (сообщение о недостающем remote'е);
      * с флагом в выводе обязан быть громкий след ПРОПУСКА мёржа -- и
        обязано НЕ быть той самой апстрим-жалобы, потому что до неё дело
        уже не доходит.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)

    _git(tmp_path, "init", "-q", "-b", "xdata-agent", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    # Настоящий скрипт -- REPO_ROOT в нём вычисляется от собственного
    # расположения, поэтому он обязан физически лежать под
    # <временный репозиторий>/scripts/.
    shutil.copy2(RELEASE_SCRIPT, repo / "scripts" / "release_trix.sh")
    (repo / "scripts" / "release_trix.sh").chmod(0o755)

    # Публикуемое дерево -- в отличие от соседнего теста, здесь оно ОБЯЗАНО
    # пройти проверку витрины, чтобы флагованный прогон реально миновал
    # преflight-требование апстрима и добрался до следующей, более поздней
    # причины смерти (baseline), а не упал раньше по не относящемуся к делу
    # поводу.
    (repo / "assets" / "api").mkdir(parents=True)
    (repo / "scripts" / "install.sh").write_text("#!/bin/sh\n")
    (repo / "assets" / "api" / "model-catalog.json").write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    # Намеренно нет ни remote'а upstream, ни baseline-файла
    # (docs/product/known-test-failures.txt): это и даёт две разные точки
    # смерти без флага и с ним.
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "WORK_BRANCH": "xdata-agent",
        "RELEASE_REMOTE": "release",
        "UPSTREAM_REMOTE": "upstream",
    }

    without_flag = subprocess.run(
        ["bash", "scripts/release_trix.sh", "--dry-run", "0.0.1"],
        cwd=str(repo), capture_output=True, text=True, env=env,
    )
    out_without = without_flag.stdout + without_flag.stderr
    assert without_flag.returncode != 0, out_without
    # Точная преflight-жалоба на отсутствующий remote -- не просто слово
    # "upstream" мимоходом (оно, например, попадает и в подсказку usage()).
    assert "no 'upstream' remote" in out_without.lower(), (
        "без флага скрипт обязан умереть на попытке работать с апстримом:\n"
        + out_without
    )
    assert "no test baseline at" not in out_without.lower(), (
        "без флага скрипт не должен успеть дойти до проверки baseline -- "
        "он обязан умереть раньше, на remote'е upstream:\n" + out_without
    )

    with_flag = subprocess.run(
        ["bash", "scripts/release_trix.sh", "--skip-upstream-merge", "--dry-run", "0.0.1"],
        cwd=str(repo), capture_output=True, text=True, env=env,
    )
    out_with = with_flag.stdout + with_flag.stderr
    assert with_flag.returncode != 0, out_with

    # Не должен упасть на "неизвестная опция" -- если это единственная
    # причина смерти, флаг попросту не реализован.
    assert "unknown option" not in out_with.lower(), (
        "флаг --skip-upstream-merge не распознан скриптом:\n" + out_with
    )

    # Смерть сдвинулась дальше и стала другой по существу: точное
    # сообщение о missing baseline присутствует, а точная преflight-жалоба
    # на remote upstream -- нет (до неё вовсе не дошли, проверка
    # пропущена).
    assert "no test baseline at" in out_with.lower(), (
        "с флагом прогон обязан дойти до более поздней причины смерти "
        "(отсутствие baseline):\n" + out_with
    )
    assert "no 'upstream' remote" not in out_with.lower(), (
        "с флагом преflight-жалоба на remote upstream не должна звучать -- "
        "проверка вовсе не выполняется:\n" + out_with
    )
    assert "nothing to merge" not in out_with.lower(), out_with

    # Главное требование задания: пропуск мёржа виден ГРОМКО в самом
    # выводе, до того как прогон где-либо умрёт -- человек, читающий сухой
    # прогон, обязан увидеть, что апстрим не вливался, не заглядывая в код.
    # Проверяем законченную фразу, а не отдельное слово "upstream" или
    # "skip" -- оба мимоходом встречаются в usage() и в самом имени флага,
    # что маскирует нереализованное поведение.
    assert "апстрим не вливался" in out_with.lower(), (
        "с флагом в выводе обязана быть громкая, законченная фраза о "
        "пропуске мёржа апстрима:\n" + out_with
    )
