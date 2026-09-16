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


def test_vanished_failures_file_is_a_refusal_not_a_green_release(tmp_path: Path):
    """Потерянный файл падений обязан остановить релиз, а не позеленить его.

    Гейт открывает файл со списком падений ДО прогона сюиты, а читает его
    ПОСЛЕ -- подстановкой команд ``"$(cat "$current")"``. Подстановка
    маскирует код возврата ``cat``: файл, удалённый во время прогона, даёт
    на сверку пустую строку, неотличимую от честного "падений нет".
    Измерено 2026-09-14: TMPDIR указывал на scratch-каталог уже завершённого
    прогона, каталог подчистили на середине сюиты -- гейт вернул exit 0 и
    объявил починенными все записи базовой линии разом, включая известные
    падения, которые обязаны были остаться красными.

    С тех пор бухгалтерия гейта переехала из TMPDIR сюиты в собственный
    каталог (``test_gate_bookkeeping_survives_a_hostile_tmpdir`` проверяет,
    что чистка TMPDIR его больше не достаёт). Сторож при этом обязан
    остаться: причин потерять файл больше одной, и любая из них не должна
    читаться как "падений нет". Поэтому фиктивная сюита здесь целится
    прямо в него -- это единственный способ воспроизвести потерю теперь,
    когда случайная чистка до него не дотягивается.

    Проверяем исполнением, а не чтением исходника: без сторожа этот прогон
    завершается успехом ("No new failures"), со сторожем -- отказом.
    """
    repo = _gate_repo(tmp_path)

    # Сюита честно отработала и напечатала сводку -- всё, что отличает этот
    # прогон от зелёного, это исчезнувший файл гейта.
    result = _run_gate(
        repo, tmp_path,
        'find "$(git rev-parse --git-dir)" -name current-failures.txt -delete '
        '2>/dev/null || true\n'
        f'echo "{GOOD_SUMMARY}"\n'
        'exit 0\n',
    )

    output = _assert_refused(result, "потеря файла падений")
    assert "исчез во время прогона сюиты" in output, (
        "отказ обязан назвать НАСТОЯЩУЮ причину -- потерю измерения, а не "
        "что-то соседнее:\n" + output
    )


# ---------------------------------------------------------------------------
# Раннер умер до сводки: гейт обязан отказать, а не позеленеть
# ---------------------------------------------------------------------------

# Настоящая сводка раннера. Гейт читает её как доказательство того, что
# прогон дошёл до конца; всё, что ниже, — сценарии, где её нет.
GOOD_SUMMARY = (
    "=== Summary: 3 files, 120 tests passed, 0 failed "
    "(100% complete) in 4.2s (8 workers) ==="
)


def _gate_repo(tmp_path: Path) -> Path:
    """Минимальный репозиторий, на котором ``release_trix.sh`` доходит до гейта.

    Дерево витрины публикуемо, базовая линия пуста — значит единственное,
    что отделяет зелёный прогон от красного, это поведение раннера.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)

    _git(tmp_path, "init", "-q", "-b", "xdata-agent", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    shutil.copy2(RELEASE_SCRIPT, repo / "scripts" / "release_trix.sh")
    (repo / "scripts" / "release_trix.sh").chmod(0o755)

    (repo / "assets" / "api").mkdir(parents=True)
    (repo / "scripts" / "install.sh").write_text("#!/bin/sh\n")
    (repo / "assets" / "api" / "model-catalog.json").write_text("{}\n")

    (repo / "docs" / "product").mkdir(parents=True)
    (repo / "docs" / "product" / "known-test-failures.txt").write_text("")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "remote", "add", "release", "https://example.invalid/release.git")
    return repo


def _run_gate(repo: Path, tmp_path: Path, runner_body: str) -> subprocess.CompletedProcess:
    """Подставить фиктивный ``run_tests.sh`` и прогнать настоящий гейт."""
    fake_runner = repo / "scripts" / "run_tests.sh"
    fake_runner.write_text("#!/usr/bin/env bash\n" + runner_body)
    fake_runner.chmod(0o755)

    gate_tmp = tmp_path / "gate_tmp"
    gate_tmp.mkdir(exist_ok=True)

    return subprocess.run(
        ["bash", "scripts/release_trix.sh", "--skip-upstream-merge", "--dry-run", "0.0.1"],
        cwd=str(repo), capture_output=True, text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT),
            "WORK_BRANCH": "xdata-agent",
            "RELEASE_REMOTE": "release",
            "UPSTREAM_REMOTE": "upstream",
            "TMPDIR": str(gate_tmp),
        },
    )


def _assert_refused(result: subprocess.CompletedProcess, why: str) -> str:
    output = result.stdout + result.stderr
    assert result.returncode != 0, f"{why}: гейт выпустил дерево\n{output}"
    assert "No new failures" not in output, (
        f"{why}: гейт объявил дерево чистым\n{output}"
    )
    return output


def test_runner_dying_before_its_summary_is_a_refusal(tmp_path: Path):
    """Раннер умер, не досчитав: ни строк FAILED, ни сводки, ни зелёного.

    Это второй вход в ту же дыру, что и исчезнувший файл падений.
    ``collect_failures`` глушила код возврата ``run_tests.sh`` (``set +e``)
    и НИГДЕ его не проверяла. Раннер, умерший до своей сводки — сломанный
    venv, OOM, полный диск, — не даёт ни одной строки ``FAILED``, значит
    список падений пуст; заголовка ``=== N files where no tests ran`` тоже
    нет, значит сторож несобранных файлов молчит. Итог: «✓ Все файлы сюиты
    отработали» → «✓ No new failures» → exit 0 на непроверенном дереве.
    """
    repo = _gate_repo(tmp_path)
    result = _run_gate(
        repo, tmp_path,
        'echo "▶ starting suite"\n'
        'echo "collecting ..."\n'
        'exit 1\n',
    )
    output = _assert_refused(result, "раннер умер до сводки")
    assert "сводк" in output.lower(), (
        "отказ обязан назвать настоящую причину — отсутствие сводки:\n" + output
    )


def test_oom_like_exit_is_a_refusal(tmp_path: Path):
    """Убит по памяти (137 = 128+SIGKILL) — измерения нет."""
    repo = _gate_repo(tmp_path)
    result = _run_gate(
        repo, tmp_path,
        'echo "▶ starting suite"\n'
        'exit 137\n',
    )
    _assert_refused(result, "OOM-подобный выход")


def test_sigterm_like_exit_is_a_refusal(tmp_path: Path):
    """Прибит снаружи (143 = 128+SIGTERM) — даже со сводкой на руках.

    Сводка могла быть напечатана до сигнала, а часть файлов — не
    отработать. Сигнал сильнее сводки.
    """
    repo = _gate_repo(tmp_path)
    result = _run_gate(
        repo, tmp_path,
        f'echo "{GOOD_SUMMARY}"\n'
        'exit 143\n',
    )
    _assert_refused(result, "SIGTERM-подобный выход")


def test_empty_runner_output_is_a_refusal(tmp_path: Path):
    """Ни байта вывода и код 0 — это не «сюита прошла»."""
    repo = _gate_repo(tmp_path)
    result = _run_gate(repo, tmp_path, "exit 0\n")
    _assert_refused(result, "пустой вывод раннера")


def test_a_complete_green_run_still_releases(tmp_path: Path):
    """Сторож не имеет права ломать нормальный зелёный прогон."""
    repo = _gate_repo(tmp_path)
    result = _run_gate(repo, tmp_path, f'echo "{GOOD_SUMMARY}"\nexit 0\n')

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "No new failures" in output, output


def test_ordinary_test_failures_still_reach_the_baseline_comparison(tmp_path: Path):
    """Раннер вышел с 1, но досчитал: это измерение, а не смерть.

    Ровно тот случай, ради которого код возврата и глушили. Отказ обязан
    прийти от сверки с базовой линией и назвать сам упавший тест.
    """
    repo = _gate_repo(tmp_path)
    result = _run_gate(
        repo, tmp_path,
        'echo "FAILED tests/test_thing.py::test_broken - AssertionError"\n'
        'echo "=== Summary: 3 files, 119 tests passed, 1 failed '
        '(100% complete) in 4.2s (8 workers) ==="\n'
        'exit 1\n',
    )
    output = _assert_refused(result, "настоящее падение теста")
    assert "test_broken" in output, (
        "отказ обязан назвать упавший тест, а не общую формулировку:\n" + output
    )


def test_a_real_sized_green_run_still_releases(tmp_path: Path):
    """Сводка найдена в НАСТОЯЩЕМ по объёму выводе, а не в трёх строках.

    Сторож сводки читал её так::

        printf '%s\\n' "$LAST_RUN_OUTPUT" | grep -q '^=== Summary: '

    ``grep -q`` выходит на первом совпадении и закрывает трубу. Если за
    сводкой остался неотданный вывод, ``printf`` получает SIGPIPE и
    возвращает 141, а ``set -o pipefail`` отдаёт код всего конвейера по
    самому правому ненулевому — то есть по мёртвому ``printf``, а не по
    нашедшему совпадение ``grep``. Успешный поиск читался как «сводки
    нет».

    Заметить это на коротком выводе нельзя: пока он помещается в буфер
    трубы (64 КиБ), ``printf`` успевает отдать всё до выхода ``grep``, и
    SIGPIPE не случается. Все остальные тесты сторожа печатают несколько
    строк и потому проходили. На живом прогоне вывод был 607 КиБ со
    сводкой на 3331-й строке из 6944 — и гейт отказался выпускать дерево,
    сводка у которого была на месте.

    Поэтому здесь вывод заведомо больше буфера трубы, а сводка — в
    середине: сторож обязан её увидеть.
    """
    repo = _gate_repo(tmp_path)
    result = _run_gate(
        repo, tmp_path,
        'for i in $(seq 1 4000); do\n'
        '  echo "tests/test_pad_$i.py ................................ [ 50%]"\n'
        'done\n'
        f'echo "{GOOD_SUMMARY}"\n'
        'for i in $(seq 1 4000); do\n'
        '  echo "tests/test_tail_$i.py ............................... [100%]"\n'
        'done\n'
        'exit 0\n',
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        "сводка есть — объём вывода не повод отказать в релизе:\n" + output[-4000:]
    )
    assert "No new failures" in output, output[-4000:]


def test_gate_bookkeeping_survives_a_hostile_tmpdir(tmp_path: Path):
    """Сюита, вычищающая TMPDIR, больше не уносит измерение гейта.

    Сторож исчезнувшего файла превратил этот дефект в отказ, но причину не
    убрал: служебный файл гейта лежал в TMPDIR сюиты, то есть там, куда
    пишут тесты и куда смотрит любая внешняя чистка. Отказ вместо
    ложно-зелёного — правильно, но релиз всё равно не проходил.

    Здесь фиктивная сюита сносит ``$TMPDIR`` целиком и честно
    отчитывается. Гейт обязан дойти до конца.
    """
    repo = _gate_repo(tmp_path)
    result = _run_gate(
        repo, tmp_path,
        'rm -rf "$TMPDIR"/* 2>/dev/null || true\n'
        f'echo "{GOOD_SUMMARY}"\n'
        'exit 0\n',
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        "чистка TMPDIR сюитой не должна ронять гейт:\n" + output
    )
    assert "No new failures" in output, output
