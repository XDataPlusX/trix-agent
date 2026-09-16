#!/usr/bin/env bash
# Установлено наше или чужое.
#
# Рецепт cloud-init полгода проверял только то, что появилась команда
# `hermes` -- её даёт и апстримный Hermes, поэтому подмена установщика
# дожила до живого прогона незамеченной. Здесь проверяется происхождение.
set -eu

usage() { echo "usage: verify_install_is_trix.sh <install_dir>" >&2; exit 2; }
[ "$#" -eq 1 ] || usage
DIR="$1"

[ -d "$DIR/.git" ] || { echo "FAIL: $DIR не является git-установкой" >&2; exit 1; }

# stderr от git НЕ выбрасывается. Раньше здесь стояло `2>/dev/null || true`,
# и любой отказ git превращался в пустой ORIGIN, то есть в вердикт
# "установлен не Trix Agent" -- самый громкий вердикт этого скрипта -- на
# исправной установке.
#
# Ровно это и происходит на клиентской машине: рецепт отдаёт каталог
# установки пользователю `user`, после чего git объявляет его "dubious
# ownership" для всех остальных, включая root. Поддержка, зашедшая по SSH,
# читала "FAIL: установлен не Trix Agent. origin: (не задан)" на машине, где
# всё в порядке (проверено на trix-testing7.ru 2026-09-04). Рецепт теперь
# объявляет каталог safe.directory, но на УЖЕ установленных машинах этой
# записи нет, и вердикт должен называть настоящую причину.
ORIGIN_ERR="$(mktemp)"
trap 'rm -f "$ORIGIN_ERR"' EXIT
ORIGIN="$(git -C "$DIR" remote get-url origin 2>"$ORIGIN_ERR" || true)"
if [ -z "$ORIGIN" ] && grep -q 'dubious ownership' "$ORIGIN_ERR" 2>/dev/null; then
    echo "FAIL: git отказался читать $DIR -- каталог принадлежит другому пользователю (dubious ownership)." >&2
    echo "      Это НЕ значит, что установлен чужой продукт: происхождение просто не удалось прочитать." >&2
    echo "      Починить на машине один раз, от root:" >&2
    echo "          git config --system --add safe.directory $DIR" >&2
    echo "      либо повторить проверку от владельца каталога: sudo -u \"$(stat -c %U "$DIR" 2>/dev/null || echo user)\" $0 $DIR" >&2
    exit 1
fi
NORM="$(printf '%s' "$ORIGIN" | tr '[:upper:]' '[:lower:]')"
case "$NORM" in
    *xdataplusx/trix-agent*) ;;
    *)
        echo "FAIL: установлен не Trix Agent. origin: ${ORIGIN:-(не задан)}" >&2
        exit 1
        ;;
esac

# Тег -- это только метка версии, не признак происхождения: та проверка
# уже прошла выше. Клиентский клон делается `--depth 1 --branch release`
# и видит тег, только пока тот указывает на вершину ветки -- любой коммит
# в release после тегирования уносит тег из клона на честной установке.
# Останавливать деплой из-за этого нельзя (обрыв здесь дорог: не
# выполнятся extras, сборка дашборда, linger, bootstrap мастера), поэтому
# отсутствие тега -- предупреждение, не отказ.
TAG="$(git -C "$DIR" describe --tags --abbrev=0 --match 'trix-v*' 2>/dev/null || true)"
if [ -z "$TAG" ]; then
    echo "WARN: в установке нет релизного тега trix-v* -- origin наш, версия не определена" >&2
    echo "OK: Trix Agent (версия не определена)"
    exit 0
fi

echo "OK: Trix Agent $TAG"
