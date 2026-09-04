"""Регрессия 7685f124cb: `hermes update` подтягивал новый код, но продолжал
называть клиента прежним релизом.

`git fetch origin <branch>` с явным refspec тегов НЕ приносит — это его
штатное поведение, а не баг git. А автоподхват тегов (то, что обычно
спасает в этой ситуации) на поверхностном (`--depth 1`) клоне не
срабатывает. Значит после обновления `hermes_cli.release_source.
resolve_local_release_tag` (её же зовёт `hermes --version`) находит
ближайший ИМЕЮЩИЙСЯ локально тег — старый — и врёт про версию.

Первая редакция правки добавляла шаг `git fetch --depth 1 origin
refs/tags/trix-v*:refs/tags/trix-v*` ДО `git merge --ff-only`. Это само по
себе поймало другой, более тяжёлый регресс: поверхностная выборка тега,
указывающего на уже полученный коммит, кладёт ЕМУ собственную
поверхностную прививку, и следующий `merge --ff-only` отказывается
(«refusing to merge unrelated histories»), хотя объект уже есть локально.
На практике это загоняло КАЖДОЕ обновление в запасную ветку
`git reset --hard` — разрушительный сброс вместо безобидной перемотки,
там, где до этого ff-only исправно проходил. Правку переделали: шаг с
тегами теперь идёт ПОСЛЕ успешного `merge --ff-only` / `reset --hard`, то
есть уже не может помешать слиянию (`hermes_cli/update_cmd.py`, комментарий
«Релизные теги — ПОСЛЕ применения обновления»).

Тест гоняет НАСТОЯЩИЙ git на локальных репозиториях (сеть не участвует):
«витрина» играет роль origin, от неё делается поверхностный клон в
состоянии релиза 0.1.2 (как после установки), витрина продвигается до
0.1.3 (и в третьем тесте — затем до 0.1.4), и на клоне выполняется ровно та
последовательность git-команд в ТОМ ЖЕ порядке, что выполняет
`hermes update`. Проверяется:

- обе стороны версии — с шагом тегов она становится новой, без него
  застревает на старой;
- что слияние на каждом шаге прошло именно быстрой перемоткой
  (`merge --ff-only` вернул 0), а не запасным `reset --hard` — это и есть
  утверждение, ради которого переделан порядок: если шаг тегов случайно
  вернут на прежнее место ДО слияния, здесь должно покраснеть;
- что два обновления подряд (0.1.2 → 0.1.3 → 0.1.4) оба проходят
  перемоткой и оба называют верную версию — иначе поверхностная прививка
  от первого шага тегов могла бы накопиться и сломать второе обновление.
"""

import subprocess
from pathlib import Path

import pytest

from hermes_cli.release_source import RELEASE_BRANCH, RELEASE_TAG_PREFIX, resolve_local_release_tag

TAG_V2 = f"{RELEASE_TAG_PREFIX}0.1.2"
TAG_V3 = f"{RELEASE_TAG_PREFIX}0.1.3"
TAG_V4 = f"{RELEASE_TAG_PREFIX}0.1.4"

_IDENTITY = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]

_TAGS_REFSPEC = "refs/tags/trix-v*:refs/tags/trix-v*"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Запускает git и требует успеха — молчаливый провал шага подготовки
    здесь всегда значит сломанный тест, а не сценарий, который мы изучаем."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"git {' '.join(args)} упал:\n{result.stderr}"
    return result


def _git_allow_fail(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Как _git, но без assert. Нужен для `merge --ff-only`: у него по
    определению есть право не получиться при настоящем расхождении истории,
    и вызывающая сторона сама решает, что делать с провалом."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _init_showcase(showcase: Path) -> None:
    """«Витрина» исполняет роль origin. Клонировать её будем по file://,
    поэтому bare-репозиторий не нужен — --depth всё равно будет действовать
    по-настоящему только через file://, а не потому, что репозиторий bare."""
    _git("init", "-q", "-b", RELEASE_BRANCH, cwd=showcase)
    (showcase / "f.txt").write_text("one\n")
    _git("add", "f.txt", cwd=showcase)
    _git(*_IDENTITY, "commit", "-q", "-m", "one", cwd=showcase)
    _git("tag", TAG_V2, cwd=showcase)


def _release_showcase(showcase: Path, tag: str, content: str) -> None:
    """Очередной релиз на витрине — то, что появляется на origin, пока
    клиент ещё стоит на предыдущем."""
    (showcase / "f.txt").write_text(content + "\n")
    _git("add", "f.txt", cwd=showcase)
    _git(*_IDENTITY, "commit", "-q", "-m", content, cwd=showcase)
    _git("tag", tag, cwd=showcase)


def _clone_shallow(showcase: Path, dest: Path) -> None:
    """Клонирует поверхностно, как при установке.

    Два момента, без которых воспроизведение развалится:

    - По голому локальному пути git считает клон «локальным» и молча
      игнорирует `--depth` (`--depth is ignored in local clones; use
      file:// instead`) — значит клон получится НЕ поверхностным, и весь
      сценарий с автоподхватом тегов будет проверять не то. Отсюда
      `file://`.
    - `--no-local` обязателен даже при `file://`: без него git всё равно
      может развернуть клон через хардлинки на объекты витрины, и часть
      поведения поверхностного fetch (в том числе то, что ловят тесты
      ниже) маскируется общим object store.
    """
    _git(
        "clone", "-q", "--no-local", "--depth", "1",
        "--branch", RELEASE_BRANCH,
        f"file://{showcase}", str(dest),
        cwd=showcase.parent,
    )


def _apply_update_fetch_sequence(client: Path, *, fetch_tags: bool) -> bool:
    """Ровно та последовательность git-команд и ровно в том порядке, что
    выполняет `hermes update` (hermes_cli/update_cmd.py): узкий
    `fetch origin <branch>` → `merge --ff-only origin/<branch>` (с откатом
    на `reset --hard` при настоящем расхождении) → и ТОЛЬКО ПОСЛЕ этого,
    если `fetch_tags`, отдельный `fetch --depth 1` по refspec
    `refs/tags/trix-v*` (правка 7685f124cb, во второй редакции).

    Порядок здесь — предмет теста, а не деталь реализации: шаг тегов стоит
    последним именно потому, что стоя перед слиянием он ломает
    `merge --ff-only` (см. модульный docstring). Возвращает True, если
    слияние прошло быстрой перемоткой (returncode `merge --ff-only` == 0) —
    вызывающая сторона утверждает это явно, чтобы регресс порядка не
    проскочил незамеченным через один только запасной `reset --hard`.
    """
    _git("fetch", "origin", RELEASE_BRANCH, cwd=client)

    merge = _git_allow_fail("merge", "--ff-only", f"origin/{RELEASE_BRANCH}", cwd=client)
    fast_forwarded = merge.returncode == 0
    if not fast_forwarded:
        _git("reset", "--hard", f"origin/{RELEASE_BRANCH}", cwd=client)

    if fetch_tags:
        _git("fetch", "--depth", "1", "origin", _TAGS_REFSPEC, cwd=client)

    return fast_forwarded


def _is_shallow(repo: Path) -> bool:
    return _git("rev-parse", "--is-shallow-repository", cwd=repo).stdout.strip() == "true"


@pytest.fixture
def showcase_and_client(tmp_path):
    """Общее состояние «сразу после установки» для тестов ниже: витрина на
    релизе 0.1.2 и её поверхностный клон, затем витрина продвигается до
    0.1.3 (как будто вышел новый релиз, пока клиент об этом не знает)."""
    showcase = tmp_path / "showcase"
    showcase.mkdir()
    _init_showcase(showcase)

    client = tmp_path / "client"
    _clone_shallow(showcase, client)

    # Контроль состояния «после установки»: если это не так, дальнейшие
    # проверки ничего не доказывают.
    assert _is_shallow(client)
    assert resolve_local_release_tag(str(client)) == TAG_V2

    _release_showcase(showcase, TAG_V3, "two")
    return showcase, client


def test_update_fetch_sequence_picks_up_new_release_tag(showcase_and_client):
    """С шагом fetch-тегов (правка 7685f124cb) клиент после обновления
    называет себя новым релизом, слияние прошло быстрой перемоткой (а не
    запасным сбросом), и клон остаётся поверхностным — починка не расшивает
    его, то есть не отменяет ту цену, ради которой поверхностный клон вообще
    существует."""
    _showcase, client = showcase_and_client

    fast_forwarded = _apply_update_fetch_sequence(client, fetch_tags=True)

    # Именно это утверждение и есть смысл переделки порядка: если шаг тегов
    # снова передвинут ДО слияния, `merge --ff-only` откажется («refusing to
    # merge unrelated histories»), сценарий уйдёт в запасной `reset --hard`,
    # и эта строка покраснеет — раньше, чем успеет всплыть версия.
    assert fast_forwarded, "слияние должно пройти перемоткой, а не запасным reset --hard"

    assert resolve_local_release_tag(str(client)) == TAG_V3
    assert _is_shallow(client)


def test_update_without_tag_fetch_step_stays_on_old_version(showcase_and_client):
    """Обратная сторона — доказательство, что тест действительно что-то
    ловит: без шага, добавленного 7685f124cb, `git fetch origin <branch>` с
    явным refspec тегов не приносит, и `resolve_local_release_tag` продолжает
    находить только 0.1.2, хотя рабочее дерево уже стоит на коммите релиза
    0.1.3. Это и есть исходный баг из коммита: клиент, обновившись, врёт о
    своей версии."""
    _showcase, client = showcase_and_client

    fast_forwarded = _apply_update_fetch_sequence(client, fetch_tags=False)

    assert fast_forwarded
    assert resolve_local_release_tag(str(client)) == TAG_V2


def test_two_updates_in_a_row_both_fast_forward_and_report_correct_version(showcase_and_client):
    """Защита от накопления: если бы шаг тегов стоял до слияния, прививка от
    ПЕРВОГО обновления не давала бы о себе знать, пока клиент не попробует
    обновиться ВТОРОЙ раз — тогда предыдущий тег уже привит поверхностно, и
    следующий `merge --ff-only` ломается снова. Прогоняем 0.1.2 → 0.1.3 →
    0.1.4 подряд и требуем перемотку и верную версию на каждом шаге, плюс
    поверхностность клона в конце."""
    showcase, client = showcase_and_client

    # Первое обновление: 0.1.2 -> 0.1.3.
    assert _apply_update_fetch_sequence(client, fetch_tags=True)
    assert resolve_local_release_tag(str(client)) == TAG_V3

    # Второй релиз на витрине, пока клиент уже обновился до 0.1.3.
    _release_showcase(showcase, TAG_V4, "three")

    # Второе обновление подряд: 0.1.3 -> 0.1.4.
    assert _apply_update_fetch_sequence(client, fetch_tags=True)
    assert resolve_local_release_tag(str(client)) == TAG_V4

    assert _is_shallow(client)
