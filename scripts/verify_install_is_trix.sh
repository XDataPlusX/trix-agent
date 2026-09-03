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

ORIGIN="$(git -C "$DIR" remote get-url origin 2>/dev/null || true)"
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
