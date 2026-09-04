#!/usr/bin/env bash
# Cut a Trix Agent release.
#
# The order is deliberate: upstream is merged into the work branch, the
# suite runs there, and only then does `release` move. A client receives
# changes when we decided to ship them, not when upstream merged something.
#
# On the test gate: this tree has pre-existing failures (optional deps that
# aren't installed, a sticky-bit check that needs a different filesystem).
# Demanding zero failures would never ship; ignoring failures would ship
# anything. So the gate is the one the spec actually states -- the set of
# failing tests must not GROW relative to a recorded baseline.
#
#   scripts/release_trix.sh --record-baseline   # once, on a known-good tree
#   scripts/release_trix.sh 0.1.0               # cut a release
#   scripts/release_trix.sh --dry-run 0.1.0     # everything except push/tag
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# The branch our work lives on. NOT `main`: this fork's `main` still points
# at the upstream commit we forked from, so releasing from it would ship a
# pristine upstream tree with none of this product in it.
WORK_BRANCH="${WORK_BRANCH:-xdata-agent}"
RELEASE_BRANCH="release"
RELEASE_REMOTE="${RELEASE_REMOTE:-release}"
UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
TAG_PREFIX="trix-v"
BASELINE_FILE="$REPO_ROOT/docs/product/known-test-failures.txt"

DRY_RUN=false
RECORD_BASELINE=false
SKIP_UPSTREAM_MERGE=false
VERSION=""

die() { printf '\033[0;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
say() { printf '\033[0;36m→ %s\033[0m\n' "$*"; }
ok()  { printf '\033[0;32m✓ %s\033[0m\n' "$*"; }

usage() {
    cat <<'USAGE'
usage: scripts/release_trix.sh [--dry-run] [--skip-upstream-merge] <version>
       scripts/release_trix.sh --record-baseline

  <version>                e.g. 0.1.0 -- tagged as trix-v0.1.0
  --dry-run                run every check, skip the tag and the push
  --skip-upstream-merge    do not fetch or merge upstream/main; release
                           exactly the tree already tested. For the
                           deliberate case of shipping precisely what the
                           suite ran against (e.g. a first release) -- not a
                           shortcut for skipping the merge routinely.
  --record-baseline        record the currently-failing tests as the
                           accepted baseline; do this only on a tree you
                           have reviewed

env: WORK_BRANCH (default xdata-agent), RELEASE_REMOTE (default release),
     UPSTREAM_REMOTE (default upstream)
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --skip-upstream-merge) SKIP_UPSTREAM_MERGE=true; shift ;;
        --record-baseline) RECORD_BASELINE=true; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) usage >&2; die "unknown option: $1" ;;
        *) [ -z "$VERSION" ] || die "unexpected argument: $1"; VERSION="$1"; shift ;;
    esac
done

# Интерпретатор выбирается ОДИН раз, здесь, до первого использования --
# и test-gate (collect_failures), и дерево витрины (release_tree), и
# разбор вывода раннера (release_gate) идут через один и тот же venv.
# Цепочка `A || B` здесь была бы дефектом: настоящий отказ (код 1)
# неотличим от "нет venv", и вторая попытка просто повторила бы тот же
# отказ.
if [ -x "$REPO_ROOT/venv/bin/python" ]; then
    RELEASE_PY="$REPO_ROOT/venv/bin/python"
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    RELEASE_PY="$REPO_ROOT/.venv/bin/python"
else
    RELEASE_PY="python3"
fi

# Файлы, которые baseline-recorder осознанно никогда не пишет в
# baseline (таймингово-чувствительные тесты — падают только под
# нагрузкой полного параллельного прогона, проходят в одиночку).
# Список живёт РОВНО в одном месте — hermes_cli/release_gate.py — не
# здесь: раньше bash носил собственную копию этого перечня, и она была
# вторым местом, с которым легко было молча разойтись. Тот же модуль
# использует список сам, разбирая вывод раннера ниже, так что gate и
# baseline-recorder гарантированно смотрят на один и тот же перечень.
FLAKY_NEVER_RECORD="$("$RELEASE_PY" -m hermes_cli.release_gate --never-record-list)"

# Collect the set of failing tests as a sorted, comparable list.
#
# The runner itself exits non-zero in the normal case (tests failed --
# exactly what we are here to measure), so its status is deliberately
# discarded under `set -e`.
#
# The raw grep this used to be ("FAILED [^ ]+" over the whole output) could
# not tell a real failure from two things the runner's own output already
# explains: a file the runner retried once and which PASSED on that retry
# (printed under "=== ⚠ N FLAKY files ..." purely as a record of the first,
# superseded attempt -- not a failure), and a file in FLAKY_NEVER_RECORD
# (never written to the baseline on purpose, so it can only ever show up as
# a "new failure" in the comparison below). Recognizing either needs the
# structure of the output, not a single-line pattern -- see
# hermes_cli/release_gate.py for why and how.
#
# Raw runner output from the last collect_failures call. The baseline path
# needs more than the real failures: see uncollected_files below.
LAST_RUN_OUTPUT=""

collect_failures() {
    set +e +o pipefail
    LAST_RUN_OUTPUT="$(scripts/run_tests.sh 2>&1)"
    set -e -o pipefail
    printf '%s\n' "$LAST_RUN_OUTPUT" | "$RELEASE_PY" -m hermes_cli.release_gate --parse-failures
}

# Files that never produced a single test — an import error, usually a
# dependency missing from THIS machine's venv rather than anything wrong with
# the tree. They are invisible in the FAILED list because a file that cannot
# be imported has no test ids to report.
#
# Harmless in an ordinary run. Poison in a baseline: recorded as accepted,
# they would teach the release gate that a whole subsystem is expected to be
# broken, and it would never mention it again. That is exactly how thirteen
# files' worth of MCP coverage went unwatched on this machine — the venv was
# simply missing the `dev` extra.

# Разбор живёт в hermes_cli.release_gate (parse_uncollected), а не второй
# копией в sed: секцию сводки надо читать по границам, а не однострочным
# шаблоном, и ровно та же логика нужна обоим путям -- записи базовой линии
# и релизу. Копия в bash была бы непроверяемой, а тест на неё пришлось бы
# писать чтением исходника -- запрещённый в этом проекте антипаттерн.
uncollected_files() {
    printf '%s\n' "$LAST_RUN_OUTPUT" \
        | "$RELEASE_PY" -m hermes_cli.release_gate --parse-uncollected \
        || true
}

if [ "$RECORD_BASELINE" = true ]; then
    [ -z "$VERSION" ] || die "--record-baseline takes no version"
    say "Running the suite to record the accepted baseline (slow)..."
    tmp="$(mktemp)"
    collect_failures > "$tmp"

    # Refuse to freeze an environment gap into the contract. A file that did
    # not import contributes no FAILED lines, so recording would quietly
    # produce a baseline that looks clean while a whole area of the product
    # goes unchecked from then on.
    uncollected="$(uncollected_files)"
    if [ -n "$uncollected" ]; then
        rm -f "$tmp"
        printf '\033[0;31m✗ %s\033[0m\n' \
            "These test files did not run at all (import/collection error):" >&2
        printf '%s\n' "$uncollected" | sed 's/^/    /' >&2
        die "Refusing to record a baseline while files fail to import.
  A missing dependency here becomes 'expected to fail' forever, and the gate
  stops watching that area entirely. Restore the environment first:

    uv sync --locked --python 3.11 --extra all --extra dev \\
        --extra anthropic --extra mistral --extra fal --extra modal \\
        --extra daytona --extra hindsight --extra parallel-web

  (that is the exact command CI uses), then run --record-baseline again.
  If a file genuinely cannot import on any machine, fix or delete it — do
  not record around it."
    fi

    # $tmp already went through hermes_cli.release_gate above: both a file
    # that failed once and passed on the runner's own retry, and a file in
    # FLAKY_NEVER_RECORD, are excluded before we ever see them here (counts
    # for both were already printed to the terminal by that call). No
    # second filtering pass belongs in bash -- it would just be a second
    # place that could disagree with the first.
    kept="$(cat "$tmp")"
    {
        echo "# Tests that already fail on a tree we consider good."
        echo "# Regenerate with: scripts/release_trix.sh --record-baseline"
        echo "# Recorded $(date -u +%Y-%m-%dT%H:%M:%SZ) from $(git rev-parse --short HEAD)"
        echo "#"
        echo "# Timing-sensitive files are filtered out by the recorder itself"
        echo "# (FLAKY_NEVER_RECORD in hermes_cli/release_gate.py): they pass in"
        echo "# isolation, so baking them in would teach the gate to accept a"
        echo "# real failure anywhere in the same file. Excluded on that basis:"
        printf '%s\n' "$FLAKY_NEVER_RECORD" | sed 's/^/#   /'
        echo "#"
        echo "# Record only on a machine whose venv matches CI. A missing"
        echo "# optional dependency turns into 'expected to fail' forever: the"
        echo "# 2026-08-16 baseline carried 98 such entries, and the gate"
        echo "# forgave every one of them until 2026-09-01."
        printf '%s\n' "$kept"
    } > "$BASELINE_FILE"
    rm -f "$tmp"
    ok "Baseline recorded: $(grep -vc '^#' "$BASELINE_FILE") failing tests (dropped counts above, from hermes_cli.release_gate)"
    echo "  Review $BASELINE_FILE and commit it."
    exit 0
fi

[ -n "$VERSION" ] || { usage >&2; exit 1; }
TAG="${TAG_PREFIX}${VERSION}"

# --- preflight ------------------------------------------------------------

# Uncommitted changes to TRACKED files are a hard stop: they are work that
# would silently not ship. Untracked files only warn -- they cannot reach a
# release (only committed content is pushed), and every checkout carries a
# few local ones, so blocking on them would just train people to ignore the
# check. The warning still catches "I forgot to `git add` the new file".
[ -z "$(git status --porcelain --untracked-files=no)" ] \
    || die "Uncommitted changes to tracked files. Commit or stash them first."

untracked="$(git ls-files --others --exclude-standard)"
if [ -n "$untracked" ]; then
    printf '\033[0;33m! Untracked files present (they will NOT be released):\033[0m\n'
    printf '%s\n' "$untracked" | sed 's/^/    /'
    printf '  If any of these belong in the release, Ctrl-C and git add them.\n'
fi

git rev-parse --verify "refs/tags/$TAG" >/dev/null 2>&1 \
    && die "Tag $TAG already exists."

git rev-parse --verify "$WORK_BRANCH" >/dev/null 2>&1 \
    || die "Work branch '$WORK_BRANCH' does not exist."

# --skip-upstream-merge существует для ОДНОГО конкретного случая: выпустить
# ровно то дерево, что уже прогнала сюита, не вливая апстрим прямо перед
# необратимой публикацией. Это решение владельца для первого релиза
# продукта (замер 2026-09-02: 5634 невлитых коммита апстрима, никогда не
# прогонявшихся вместе с нашим кодом -- влить их сейчас значило бы либо
# опубликовать непроверенное дерево, либо обесценить baseline из 40 записей,
# либо разбирать конфликты под давлением публикации, которую нельзя
# отменить). Это НЕ лазейка для ленивых релизов -- мёрж апстрима остаётся
# отдельной работой со своей приёмкой, и объявление об этом звучит громко
# именно потому, что решение "не вливать" каждый раз должно быть осознанным.
if [ "$SKIP_UPSTREAM_MERGE" = true ]; then
    printf '\033[1;33m! --skip-upstream-merge: апстрим НЕ вливался в этот релиз.\033[0m\n'
    # Число невлитых коммитов узнаём БЕЗ обращения к сети -- только по уже
    # существующим локальным remote-tracking ссылкам, если они есть. Именно
    # это и делает флаг возможным без сети: мы явно не ходим в сеть здесь,
    # иначе он был бы неотличим от обычного мёржа с лишним шагом.
    if git rev-parse --verify "refs/remotes/${UPSTREAM_REMOTE}/main" >/dev/null 2>&1; then
        behind_known="$(git rev-list --count "HEAD..${UPSTREAM_REMOTE}/main" 2>/dev/null || true)"
    else
        behind_known=""
    fi
    if [ -n "$behind_known" ]; then
        printf '\033[1;33m  Известных без обращения к сети невлитых коммитов апстрима: %s.\033[0m\n' "$behind_known"
    else
        printf '\033[1;33m  Мёрж пропущен по явному требованию -- без сети посчитать отставание нельзя.\033[0m\n'
    fi
else
    git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1 \
        || die "No '$UPSTREAM_REMOTE' remote -- nothing to merge Hermes from."
fi

if [ "$DRY_RUN" = false ]; then
    git remote get-url "$RELEASE_REMOTE" >/dev/null 2>&1 || die \
"No '$RELEASE_REMOTE' remote. Add the release repository first:
    git remote add $RELEASE_REMOTE git@github.com:<owner>/<repo>.git"
fi

# Что именно уедет клиенту -- единственное место, где этот вопрос имеет
# верный ответ. Прежний сторож смотрел `git ls-files docs/product` в
# рабочем дереве, а публиковалась ИСТОРИЯ ветки: оба его собственных
# совета проверку проходили и всё равно публиковали 144 коммита с
# нашими документами.
say "Проверяю дерево витрины..."
# RELEASE_PY выбран один раз, в самом начале скрипта (см. выше) -- здесь
# только используется.
"$RELEASE_PY" -m hermes_cli.release_tree --check "$WORK_BRANCH" \
    || die "Дерево витрины не прошло проверку (см. выше)."
ok "Дерево витрины проверено"

# Свежесть uv.lock — НАША забота, не клиентской машины.
#
# Установщик ставит зафиксированный набор через `uv sync --frozen`: ровно
# то, что записано в uv.lock, с хешами, без повторного разрешения. Раньше
# там стоял `--locked`, то есть УТВЕРЖДЕНИЕ «замок всё ещё актуален», и
# оно проверялось у клиента против живого индекса PyPI. С upstream-овским
# `exclude-newer = "14 days"` окно сравнения съезжает каждый день, поэтому
# утверждение падало у клиента перемежающимся образом (живой прогон
# 2026-09-04), а установка молча теряла проверку хешей.
#
# Проверка переехала сюда — туда, где за ней стоит действие (`uv lock`).
# Предупреждение, а не отказ, по двум причинам: она требует сети (а
# --skip-upstream-merge существует ровно для выпуска без неё), и устаревший
# замок клиенту не вредит — `--frozen` поставит записанное в любом случае.
if [ -f "$REPO_ROOT/uv.lock" ]; then
    say "Проверяю свежесть uv.lock..."
    if command -v uv >/dev/null 2>&1; then
        if uv lock --check >/dev/null 2>&1; then
            ok "uv.lock актуален"
        else
            printf '\033[1;33m! uv.lock разошёлся с pyproject.toml (или окно exclude-newer съехало).\033[0m\n'
            printf '\033[1;33m  Клиент получит ЗАПИСАННЫЙ набор (uv sync --frozen) — это не отказ.\033[0m\n'
            printf '\033[1;33m  Освежить: uv lock && git commit uv.lock\033[0m\n'
        fi
    else
        printf '\033[1;33m! uv не найден — свежесть uv.lock не проверена.\033[0m\n'
    fi
fi

[ -f "$BASELINE_FILE" ] || die \
"No test baseline at $BASELINE_FILE.
Record one on a tree you trust:  scripts/release_trix.sh --record-baseline"

say "Releasing $TAG from '$WORK_BRANCH'"
git checkout -q "$WORK_BRANCH"

# --- merge upstream -------------------------------------------------------

if [ "$SKIP_UPSTREAM_MERGE" = true ]; then
    # Само решение и объявление о нём уже прозвучали громко в preflight
    # (см. выше) -- здесь только короткая отметка, что шаг реально
    # пропущен, а не забыт.
    say "Пропускаю fetch/merge апстрима (--skip-upstream-merge, см. предупреждение выше)."
else
    say "Fetching $UPSTREAM_REMOTE..."
    git fetch --quiet "$UPSTREAM_REMOTE"

    behind="$(git rev-list --count "HEAD..${UPSTREAM_REMOTE}/main")"
    if [ "$behind" -gt 0 ]; then
        say "Merging $behind upstream commit(s)..."
        git merge --no-edit "${UPSTREAM_REMOTE}/main" \
            || die "Merge conflict. Resolve it, commit, and re-run."
        ok "Upstream merged"
    else
        ok "Already current with upstream"
    fi
fi

# --- test gate ------------------------------------------------------------

say "Running the suite (this takes a while)..."
current="$(mktemp)"; trap 'rm -f "$current"' EXIT
collect_failures > "$current"

# Файл, который не отработал, не даёт ни одной строки FAILED -- значит
# сравнение с базовой линией показывает совпадение, хотя целый файл
# тестов не выполнился. На пути --record-baseline это уже проверялось
# (см. uncollected_files выше); на релизном не проверялось никем, и это
# ровно та же дыра, только с другой стороны: там она портила контракт,
# здесь -- выпускает непроверенное дерево.
#
# Причин две, и обе настоящие. Сорванный импорт -- как правило, нехватка
# зависимости в venv ЭТОЙ машины. Файловый таймаут -- файл собрался и
# начал считать тесты, но не уложился в отведённое время; замерено
# 2026-09-04 на приёмке спеки 16: tests/hermes_cli/test_doctor.py прошёл
# за 51 секунду в одном полном прогоне и был убит на 600 секундах в
# другом, а в одиночку проходит целиком. Раннер называет оба случая в
# своей сводке "files where no tests ran"; гейт до сих пор эту сводку на
# релизном пути не читал.
uncollected="$(uncollected_files)"
if [ -n "$uncollected" ]; then
    rm -f "$current"
    printf '\033[0;31m✗ %s\033[0m\n' \
        "Эти файлы тестов не отработали (импорт не удался или файловый таймаут):" >&2
    printf '%s\n' "$uncollected" | sed 's/^/    /' >&2
    die "Отказываюсь выпускать релиз: часть сюиты не выполнилась.
  Строк FAILED такой файл не даёт, поэтому сверка с базовой линией его не
  видит и прогон выглядит зелёным. Разберитесь с каждым:

    scripts/run_tests.sh <файл>

  Прошёл в одиночку -- значит файловый таймаут под нагрузкой: поднимите
  порог (--file-timeout / HERMES_TEST_FILE_TIMEOUT) или разгрузите машину
  и повторите. Не прошёл -- это настоящая поломка либо нехватка
  зависимости в venv. Обходить этот отказ перезаписью базовой линии
  нельзя: recorder откажется ровно по той же причине."
fi
ok "Все файлы сюиты отработали"

baseline="$(mktemp)"; grep -v '^#' "$BASELINE_FILE" | sort -u > "$baseline"
new_failures="$(comm -23 "$current" "$baseline" || true)"
fixed="$(comm -13 "$current" "$baseline" || true)"
rm -f "$baseline"

if [ -n "$new_failures" ]; then
    printf '\033[0;31m✗ New test failures since the baseline:\033[0m\n' >&2
    printf '%s\n' "$new_failures" | sed 's/^/    /' >&2
    die "Refusing to release. Fix them, or re-record the baseline if they are genuinely accepted."
fi
ok "No new failures against the baseline"

if [ -n "$fixed" ]; then
    say "These baseline failures now pass -- consider re-recording:"
    printf '%s\n' "$fixed" | sed 's/^/    /'
fi

# --- cut it ---------------------------------------------------------------

if [ "$DRY_RUN" = true ]; then
    # Спека §10 требует, чтобы первый выпуск делался через --dry-run с
    # РУЧНЫМ ОСМОТРОМ собранного дерева -- смотреть было не на что: этот
    # проход раньше заканчивался здесь, а единственная сборка дерева выше
    # шла во временный каталог, который удаляется сам. Собираем ещё раз,
    # уже после мёржа upstream (то есть на том же ref, с которого пошёл бы
    # настоящий --publish), и оставляем на диске.
    dry_run_out="$(mktemp -d)/tree"
    "$RELEASE_PY" -m hermes_cli.release_tree --check "$WORK_BRANCH" --out "$dry_run_out" \
        || die "Дерево витрины не прошло повторную проверку перед сухим прогоном."
    ok "Dry run complete. Собранное дерево витрины лежит для ручного осмотра здесь: $dry_run_out"
    ok "Ничего не закоммичено и не запушено -- реальный запуск (без --dry-run) построит поверх '$RELEASE_BRANCH', тегирует $TAG и запушит в '$RELEASE_REMOTE'."
    exit 0
fi

# Сборка, коммит и перенос ветки живут в одном месте -- hermes_cli.release_tree
# --publish -- а не дублируются здесь heredoc'ом: копия в bash была
# непроверяемой, и именно в ней раньше пряталась инверсия порядка (ветка
# двигалась внутри commit_release_tree ДО сверки коммита). Теперь перенос
# ветки -- последний шаг --publish, и он выполняется только после того, как
# verify_release_commit подтвердит созданный коммит.
say "Собираю и публикую дерево витрины..."
sha="$("$RELEASE_PY" -m hermes_cli.release_tree --publish "$WORK_BRANCH" "$RELEASE_BRANCH" "$VERSION")" \
    || die "Дерево витрины не удалось опубликовать (см. выше)."
ok "Дерево витрины закоммичено: $sha"
git tag -a "$TAG" -m "Trix Agent $VERSION" "$RELEASE_BRANCH"

say "Publishing to '$RELEASE_REMOTE'..."
# Ветка витрины по замыслу -- ОДИН коммит на версию без предков (см.
# hermes_cli/release_tree.py: наружу не уходит рабочая история). Сирота не
# является потомком предыдущего релиза, поэтому обычная перемотка для неё
# невозможна В ПРИНЦИПЕ, а не в неудачном случае. Первый релиз прошёл
# только потому, что ветки на той стороне ещё не существовало; второй
# упирался бы в "non-fast-forward" всегда.
#
# Почему это безопасно: предыдущий релиз остаётся достижим по своему тегу
# (`trix-v*` не двигаются никогда), то есть перенос ветки ничего не теряет
# -- он только меняет, что клиент получит СЛЕДУЮЩИМ `hermes update`.
#
# --force-with-lease, а не --force: если витрина уехала с тех пор, как мы
# её видели (чужая публикация, ручная правка), пуш обязан отказаться, а не
# затереть. Для этого сначала обновляем своё представление об удалённой
# ветке -- без fetch аренда сравнивается с пустотой и вырождается в
# обычный --force.
git fetch --quiet "$RELEASE_REMOTE" "$RELEASE_BRANCH" 2>/dev/null || true
lease_ref="$(git rev-parse --verify --quiet "refs/remotes/${RELEASE_REMOTE}/${RELEASE_BRANCH}" || true)"
if [ -n "$lease_ref" ]; then
    git push --force-with-lease="${RELEASE_BRANCH}:${lease_ref}" \
        "$RELEASE_REMOTE" "${RELEASE_BRANCH}:${RELEASE_BRANCH}"
else
    # Ветки на витрине ещё нет -- первый релиз. Аренду не на что опереть,
    # и принуждение не нужно: перематывать нечего.
    git push "$RELEASE_REMOTE" "${RELEASE_BRANCH}:${RELEASE_BRANCH}"
fi
git push "$RELEASE_REMOTE" "$TAG"

ok "Released $TAG"
echo
echo "  Clients pick this up with: hermes update"
