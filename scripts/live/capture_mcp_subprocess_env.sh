#!/usr/bin/env bash
# Снятие живого шага 6 §5.3: окружение stdio-подпроцесса MCP на стенде.
#
# Скрипт не «проверяет код» — он снимает свидетельства с работающей машины
# и складывает их четырьмя файлами, которые требует RAF-162:
#
#   outside.log   записи вне корня профиля, пойманные inotifywait (§5)
#   inotify.err   stderr того же inotifywait — пустой лог без него не улика
#   ps.txt        цепочка процессов: движок → watchdog → сам MCP-сервер
#   environ.txt   /proc/<pid>/environ подпроцесса, отфильтрованный по §2.5
#
# Порядок на стенде (три команды, между второй и третьей — работа в чате):
#
#   scripts/live/capture_mcp_subprocess_env.sh watch   ~/raf162
#   # hermes mcp add write-probe --command python3 \
#   #     --args $PWD/scripts/live/mcp_write_probe_server.py
#   # из Telegram: «покажи список MCP» и один вызов write_probe_file
#   scripts/live/capture_mcp_subprocess_env.sh capture ~/raf162
#   scripts/live/capture_mcp_subprocess_env.sh verify  ~/raf162
#
# `watch` обязан стартовать ДО подключения сервера: inotifywait не видит
# прошлого, и запись, сделанная до его старта, в `outside.log` не попадёт —
# получится пустой лог, который ничего не доказывает.
#
# Корень профиля берётся из --root, иначе из HERMES_HOME, иначе спрашивается
# у самого движка (`hermes_constants.get_profile_root`).

set -uo pipefail

PROBE_MARKER="${PROBE_MARKER:-mcp-write-probe}"
# Имя, по которому ищется процесс сервера. Переопределяется, если пробу
# положили под другим именем или снимают шаг на постороннем MCP-сервере.
PROBE_SCRIPT_NAME="${PROBE_SCRIPT_NAME:-mcp_write_probe_server.py}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

die() { echo "ошибка: $*" >&2; exit 1; }
note() { echo "  $*"; }

usage() {
  cat >&2 <<'EOF'
usage: capture_mcp_subprocess_env.sh <watch|capture|verify|stop> <outdir> [--root <корень профиля>]
EOF
  exit 2
}

# ─── корень профиля ──────────────────────────────────────────────────────────

resolve_root() {
  if [[ -n "${ROOT_ARG:-}" ]]; then
    echo "$ROOT_ARG"; return
  fi
  if [[ -n "${HERMES_HOME:-}" ]]; then
    echo "$HERMES_HOME"; return
  fi
  local python_bin=""
  for candidate in "$REPO_ROOT/.venv/bin/python" python3; do
    if command -v "$candidate" >/dev/null 2>&1 || [[ -x "$candidate" ]]; then
      python_bin="$candidate"; break
    fi
  done
  [[ -n "$python_bin" ]] || die "не нашёл python, чтобы спросить корень профиля; передайте --root"
  (cd "$REPO_ROOT" && "$python_bin" -c \
    'import hermes_constants; print(hermes_constants.get_profile_root())' 2>/dev/null) \
    || die "движок не назвал корень профиля; передайте --root"
}

# ─── watch ───────────────────────────────────────────────────────────────────

cmd_watch() {
  command -v inotifywait >/dev/null 2>&1 \
    || die "нет inotifywait (apt install inotify-tools) — без него шаг 6 не снимается"
  mkdir -p "$OUTDIR"
  local watch_home="${WATCH_HOME:-${HERMES_REAL_HOME:-$HOME}}"
  local root_escaped
  # Корень профиля исключаем из наблюдения: внутри него писать законно,
  # а поток событий оттуда утопит настоящие срабатывания.
  root_escaped="$(printf '%s' "$ROOT" | sed 's/[.[\*^$\/]/\\&/g')"

  # По умолчанию — дом оператора и системный `/tmp`, как в §5 плана.
  # `WATCH_DIRS` переопределяет набор: на машине, где `/tmp` забит чужими
  # каталогами, inotify упирается в лимит watch'ей и умирает на старте —
  # лучше сузить наблюдение осознанно, чем получить пустой лог.
  local targets=()
  if [[ -n "${WATCH_DIRS:-}" ]]; then
    read -r -a targets <<< "$WATCH_DIRS"
  else
    targets=("$watch_home")
    [[ -d "${WATCH_TMP:-/tmp}" ]] && targets+=("${WATCH_TMP:-/tmp}")
  fi

  echo "$ROOT" > "$OUTDIR/root.txt"
  : > "$OUTDIR/outside.log"
  : > "$OUTDIR/inotify.err"
  inotifywait -m -r -e create,modify,moved_to,attrib \
    --exclude "^${root_escaped}/" \
    "${targets[@]}" \
    >> "$OUTDIR/outside.log" 2>> "$OUTDIR/inotify.err" &
  local watcher=$!
  echo "$watcher" > "$OUTDIR/inotify.pid"

  # inotifywait отвечает "Watches established." в stderr; без этой строки
  # наблюдение не началось, и всё, что ляжет дальше, останется незамеченным.
  local waited=0
  while (( waited < 60 )); do
    grep -q "Watches established" "$OUTDIR/inotify.err" && break
    kill -0 "$watcher" 2>/dev/null || die "inotifywait умер на старте, см. $OUTDIR/inotify.err"
    sleep 0.5; waited=$((waited + 1))
  done
  grep -q "Watches established" "$OUTDIR/inotify.err" \
    || die "inotifywait не установил наблюдение за 30 с, см. $OUTDIR/inotify.err"

  note "наблюдение started (pid $watcher) за: ${targets[*]}"
  note "корень профиля (исключён из наблюдения): $ROOT"
  note "теперь подключайте MCP-сервер и проходите шаг 6"
}

# ─── capture ─────────────────────────────────────────────────────────────────

find_probe_pid() {
  # Кого ищем: сам сервер, а не watchdog. `tools/mcp_tool.py` подменяет argv
  # на `python -m tools.mcp_stdio_watchdog -- <настоящая команда>`, так что
  # настоящий сервер — внук движка, и имя скрипта есть в обеих командных
  # строках.
  #
  # Чего не делаем: не верим `pgrep -f` на слово. Он матчит подстроку по всей
  # командной строке, поэтому в кандидаты попадает и оболочка, в чьём `-c`
  # это имя просто упомянуто (`hermes mcp add … --args …/<имя>`, ssh-строка,
  # соседний скрипт). Снять `environ` с такой оболочки — получить чужой
  # `HOME=/home/<оператор>` и ложный отказ шага. Поэтому: имя обязано
  # совпасть с ЦЕЛЫМ полем argv, а argv[0] не должен быть оболочкой.
  local pid candidates=()
  for pid in $(pgrep -f "$PROBE_SCRIPT_NAME" 2>/dev/null); do
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    local fields=() field matched=0
    mapfile -t fields < <(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null) || continue
    (( ${#fields[@]} )) || continue
    printf '%s\n' "${fields[@]}" | grep -q 'mcp_stdio_watchdog' && continue
    for field in "${fields[@]}"; do
      if [[ "$field" == "$PROBE_SCRIPT_NAME" || "$field" == */"$PROBE_SCRIPT_NAME" ]]; then
        matched=1; break
      fi
    done
    (( matched )) || continue
    case "$(basename -- "${fields[0]}")" in
      bash|sh|dash|zsh|fish|ssh|pgrep) continue ;;
    esac
    candidates+=("$pid")
  done

  if (( ${#candidates[@]} > 1 )); then
    # Чаще всего это недобитый сервер от прошлой попытки. Молча взять первый
    # — значит снять свидетельство не с того процесса; пусть оператор решит.
    {
      echo "нашёл несколько живых процессов $PROBE_SCRIPT_NAME:"
      ps -o pid,ppid,lstart,cmd -p "$(IFS=,; echo "${candidates[*]}")"
      echo "укажите нужный: PROBE_PID=<pid> $0 capture <outdir>"
    } >&2
    return 1
  fi
  (( ${#candidates[@]} == 1 )) || return 1
  echo "${candidates[0]}"
}

cmd_capture() {
  mkdir -p "$OUTDIR"
  local pid="${PROBE_PID:-}"
  if [[ -z "$pid" ]]; then
    pid="$(find_probe_pid)" \
      || die "не нашёл ровно один живой процесс $PROBE_SCRIPT_NAME — сервер не подключён, уже завершился, или их несколько (см. выше)"
  fi
  [[ -r "/proc/$pid/environ" ]] || die "нет доступа к /proc/$pid/environ (процесс умер или чужой пользователь)"
  echo "$pid" > "$OUTDIR/probe.pid"

  # Цепочка предков до init: движок → watchdog → сервер.
  {
    echo "# снято $(date -Is), подпроцесс MCP pid=$pid"
    local chain=() cursor="$pid"
    while [[ -n "$cursor" && "$cursor" != "0" && "$cursor" != "1" ]]; do
      chain+=("$cursor")
      cursor="$(awk '/^PPid:/{print $2}' "/proc/$cursor/status" 2>/dev/null)"
    done
    ps -o pid,ppid,user,lstart,cmd -p "$(IFS=,; echo "${chain[*]}")" 2>/dev/null
  } > "$OUTDIR/ps.txt"

  {
    echo "# /proc/$pid/environ, фильтр §2.5 плана 2026-09-14"
    tr '\0' '\n' < "/proc/$pid/environ" \
      | grep -E '^(HOME|TMPDIR|TMP|TEMP|HERMES_REAL_HOME|HERMES_OSINT_CACHE)=|^XDG_' \
      | sort
  } > "$OUTDIR/environ.txt"

  note "pid подпроцесса: $pid"
  note "ps.txt и environ.txt сняты в $OUTDIR"
  sed 's/^/    /' "$OUTDIR/environ.txt"
}

# ─── verify ──────────────────────────────────────────────────────────────────

inside_root() { [[ "$1" == "$ROOT" || "$1" == "$ROOT"/* ]]; }

cmd_verify() {
  local failures=0
  [[ -s "$OUTDIR/environ.txt" ]] || die "нет $OUTDIR/environ.txt — сначала capture"

  # 1. Окружение подпроцесса.
  env_value() { grep -m1 "^$1=" "$OUTDIR/environ.txt" | cut -d= -f2-; }

  local home
  home="$(env_value HOME)"
  echo "окружение подпроцесса:"
  if [[ "$home" == "$ROOT/home" ]]; then
    note "HOME=$home — внутри корня  ✓"
  else
    note "HOME=$home — ОЖИДАЛОСЬ $ROOT/home  ✗"; failures=$((failures + 1))
  fi
  local var value
  for var in TMPDIR TMP TEMP XDG_CACHE_HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME; do
    value="$(env_value "$var")"
    if [[ -z "$value" ]]; then
      note "$var не выставлен  ✗"; failures=$((failures + 1)); continue
    fi
    if inside_root "$value"; then
      note "$var=$value — внутри корня  ✓"
    else
      note "$var=$value — СМОТРИТ НАРУЖУ  ✗"; failures=$((failures + 1))
    fi
  done

  # 2. Файлы, которые сервер реально создал (его собственный манифест).
  local manifest="$ROOT/home/.$PROBE_MARKER/manifest.json"
  [[ -f "$manifest" ]] || manifest="$(find "$ROOT" -name manifest.json -path "*$PROBE_MARKER*" -print -quit 2>/dev/null)"
  echo "файлы сервера:"
  if [[ -n "$manifest" && -f "$manifest" ]]; then
    cp "$manifest" "$OUTDIR/manifest.json"
    local path
    while IFS= read -r path; do
      [[ -n "$path" ]] || continue
      if inside_root "$path"; then
        note "$path  ✓"
      else
        note "$path — ВНЕ КОРНЯ  ✗"; failures=$((failures + 1))
      fi
    done < <(python3 -c '
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for item in data.get("written", []):
    print(item)
' "$manifest")
  else
    note "манифест не найден под $ROOT — сервер не писал или писал наружу  ✗"
    failures=$((failures + 1))
  fi

  # 3. Улики снаружи.
  echo "наблюдение снаружи корня:"
  if [[ -s "$OUTDIR/inotify.err" ]] && grep -q "Watches established" "$OUTDIR/inotify.err"; then
    local hits
    hits="$(grep -c -- "$PROBE_MARKER" "$OUTDIR/outside.log" 2>/dev/null || true)"
    hits="${hits:-0}"
    if [[ "$hits" == "0" ]]; then
      note "в outside.log нет ни одной записи с меткой $PROBE_MARKER  ✓"
    else
      note "в outside.log $hits записей с меткой $PROBE_MARKER — изоляция течёт  ✗"
      grep -- "$PROBE_MARKER" "$OUTDIR/outside.log" | head -20 | sed 's/^/      /'
      failures=$((failures + 1))
    fi
    note "всего строк в outside.log: $(wc -l < "$OUTDIR/outside.log")"
  else
    note "наблюдение не подтверждено (нет 'Watches established' в inotify.err) — пустой outside.log уликой не считается  ✗"
    failures=$((failures + 1))
  fi

  echo
  if (( failures == 0 )); then
    echo "ИТОГ: шаг 6 §5.3 снят, расхождений нет. Файлы: $OUTDIR/{environ,ps,outside}.log|.txt"
    return 0
  fi
  echo "ИТОГ: расхождений — $failures. Это блокер релиза (§5.3), а не заметка."
  return 1
}

cmd_stop() {
  local pid_file="$OUTDIR/inotify.pid"
  [[ -f "$pid_file" ]] || { note "нечего останавливать"; return 0; }
  local pid; pid="$(cat "$pid_file")"
  if kill "$pid" 2>/dev/null; then
    note "inotifywait $pid остановлен"
  else
    note "inotifywait $pid уже не жив"
  fi
  rm -f "$pid_file"
}

# ─── разбор аргументов ───────────────────────────────────────────────────────

(( $# >= 2 )) || usage
ACTION="$1"; OUTDIR="$2"; shift 2
ROOT_ARG=""
while (( $# )); do
  case "$1" in
    --root) ROOT_ARG="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
OUTDIR="$(mkdir -p "$OUTDIR" && cd "$OUTDIR" && pwd)"
ROOT="$(resolve_root)"
ROOT="${ROOT%/}"
[[ -n "$ROOT" ]] || die "корень профиля пуст"

case "$ACTION" in
  watch) cmd_watch ;;
  capture) cmd_capture ;;
  verify) cmd_verify ;;
  stop) cmd_stop ;;
  *) usage ;;
esac
