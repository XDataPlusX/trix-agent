#!/usr/bin/env bash
# Pick the release tags the install/update E2E should update FROM.
#
# Emits a JSON array of tag names on stdout, suitable for a GitHub Actions
# matrix (`fromJSON`). Choosing at runtime rather than hardcoding keeps the
# matrix honest as releases land: a pinned list silently stops covering the
# newest release the day after it ships, and pins the "oldest" forever even
# after it stops being a version anyone still runs.
#
# Selection: the newest tag, the oldest tag, and evenly spaced tags in between.
# Newest catches "did the last release break updating?", oldest is the longest
# upgrade jump anyone can still make, and the spread samples the migrations in
# between (config-schema bumps, venv layout changes, dependency floors).
#
# Usage:
#   scripts/sandbox/pick-release-tags.sh [--count N] [--repo DIR]
#
#   --count   how many tags to emit (default 5, minimum 1). Fewer tags than
#             requested emits all of them.
#   --repo    repository to read tags from (default: this checkout).
#
# Reads tags from the local checkout, so it needs one fetched with tags
# (actions/checkout with fetch-depth: 0, or `fetch-tags: true`). A shallow
# checkout has no tags and this exits non-zero rather than silently emitting an
# empty matrix.
#
# Two release schemes are recognised, and only release tags: the repo also
# carries backup/* and one-off tags that are not releases.
#
#   trix-vMAJOR.MINOR.PATCH   the Trix fork's scheme (scripts/release_trix.sh)
#   vYYYY.M.D[.N]             upstream hermes-agent's scheme
#
# A repository cuts one or the other. When trix-v* tags exist they win
# outright: the two schemes are unrelated version lines, and letting `sort -V`
# interleave them would pick an "oldest" from the wrong product.

set -euo pipefail

COUNT=5
# Default to the repository containing this script, resolved through its real
# path so a symlinked or copied script still reads the checkout it lives in
# rather than whatever repo the caller happens to be standing in.
REPO=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --count)
      [ "$#" -ge 2 ] || { echo 'error: --count needs a value' >&2; exit 1; }
      COUNT="$2"; shift 2 ;;
    --repo)
      [ "$#" -ge 2 ] || { echo 'error: --repo needs a value' >&2; exit 1; }
      REPO="$2"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; exit 1 ;;
  esac
done
case "$COUNT" in
  ''|*[!0-9]*) echo "error: --count must be a positive integer: $COUNT" >&2; exit 1 ;;
esac
[ "$COUNT" -ge 1 ] || { echo 'error: --count must be at least 1' >&2; exit 1; }

# Resolve the script's own location through symlinks, then ask git which
# worktree that path belongs to. Deriving the repo from the script rather than
# from $PWD means a copied script cannot silently report a different checkout's
# tags, and --show-toplevel keeps it correct when invoked from a subdirectory.
if [ -z "$REPO" ]; then
  script_path="${BASH_SOURCE[0]}"
  if command -v readlink >/dev/null 2>&1; then
    script_path="$(readlink -f "$script_path" 2>/dev/null || printf '%s' "$script_path")"
  fi
  script_dir="$(cd "$(dirname "$script_path")" && pwd)"
  REPO="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$script_dir")"
fi

# sort -V orders v2026.4.8 before v2026.4.13 (numeric), which a plain
# lexicographic sort gets wrong.
#
# Read with a `while read` loop rather than `mapfile`: mapfile is bash 4+, and
# macOS still ships bash 3.2, so a maintainer on a Mac could not run this
# script at all -- including to check the behaviour before a first release.
# Fail early and by name if $REPO is not a readable git repository, rather
# than letting `git tag` fail inside collect_tags and reporting it as the
# very different "no release tags found" (which sends the reader off to check
# fetch-depth for a path that is simply wrong).
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || {
  echo "error: not a git repository: $REPO" >&2
  exit 1
}

collect_tags() {  # $1 = git tag glob, $2 = extended regexp for the scheme
  # `|| true`: grep exits 1 when nothing matches, which is an ordinary
  # answer here ("this repo does not use that scheme"), not a failure.
  git -C "$REPO" tag --list "$1" | grep -E "$2" | sort -V || true
}

tags=()
while IFS= read -r _tag; do
  [ -n "$_tag" ] && tags+=("$_tag")
done < <(collect_tags 'trix-v*' '^trix-v[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$')

if [ "${#tags[@]}" -eq 0 ]; then
  while IFS= read -r _tag; do
    [ -n "$_tag" ] && tags+=("$_tag")
  done < <(collect_tags 'v*' '^v[0-9]{4}\.[0-9]+\.[0-9]+(\.[0-9]+)?$')
fi

total="${#tags[@]}"
if [ "$total" -eq 0 ]; then
  echo "error: no release tags found in $REPO" >&2
  echo '       Looked for trix-vX.Y.Z and vYYYY.M.D[.N].' >&2
  echo '       A shallow clone has no tags: fetch with tags (actions/checkout' >&2
  echo '       with fetch-depth: 0, or fetch-tags: true).' >&2
  echo '       Before a first release there are genuinely none -- that is why' >&2
  echo '       install-e2e only runs this on a release tag push, where the tag' >&2
  echo '       being pushed is itself in the checkout.' >&2
  exit 1
fi

if [ "$total" -le "$COUNT" ]; then
  picked=("${tags[@]}")
elif [ "$COUNT" -eq 1 ]; then
  # One slot means the newest release; there is no span to spread across.
  picked=("${tags[$((total - 1))]}")
else
  # Evenly spaced indices across [0, total-1], endpoints included, so the
  # oldest and newest are always present and the rest are spread between them.
  picked=()
  for slot in $(seq 0 $((COUNT - 1))); do
    # Round to nearest rather than truncate, so the spacing does not bunch
    # toward the oldest end.
    index=$(( (slot * (total - 1) * 2 + (COUNT - 1)) / ((COUNT - 1) * 2) ))
    candidate="${tags[$index]}"
    # Guard against a duplicate if rounding lands twice on the same tag.
    case " ${picked[*]-} " in
      *" $candidate "*) continue ;;
    esac
    picked+=("$candidate")
  done
fi

printf '['
for i in "${!picked[@]}"; do
  [ "$i" -eq 0 ] || printf ','
  printf '"%s"' "${picked[$i]}"
done
printf ']\n'
