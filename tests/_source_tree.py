"""Walking the repository from a test, without racing the other 18 workers.

``Path.rglob`` applies the caller's exclusion filter to the paths it has
already **yielded**, so a guard that writes

    for path in REPO_ROOT.rglob("*.py"):
        if any(part in EXCLUDED_DIR_PARTS for part in path.parts):
            continue

still descends into every excluded directory — it only throws the results
away afterwards. That costs two things. The visible one is wasted work. The
one that actually broke a gate is a live race: ``scripts/run_tests.sh`` runs
the suite as ~18 concurrent ``python -m pytest <file>`` subprocesses, those
neighbours create and delete ``__pycache__`` all over the tree, and
``os.scandir`` eventually opens a directory that vanished between the
listing of its parent and the descent into it. ``rglob`` does not swallow
that ``FileNotFoundError``; it raises it at the caller, mid-iteration.

Measured in RAF-170 on ``dea06d039``:
``test_no_raw_config_yaml_reads_outside_owner_modules`` died on
``tools/computer_use/__pycache__`` on the first attempt and passed on the
retry. Reproduced deliberately (RAF-171) with one churn process against one
scanner: **31 of 85 scans raised**, every one of them inside a directory
``EXCLUDED_DIR_PARTS`` already named. The failure has nothing to do with the
content being checked, and it gets likelier with every added worker.

``walk_files()`` closes both halves:

  * ``prune`` is consulted **before** the descent, so an excluded subtree is
    never entered — no wasted work, and no window in which it can vanish;
  * ``__pycache__`` and friends are pruned unconditionally (see
    ``CHURN_DIRS``) because no source scan wants them and they are precisely
    what the neighbours churn;
  * a directory that disappears anyway is swallowed rather than raised, so a
    scanner racing something outside ``CHURN_DIRS`` degrades to "that
    subtree wasn't there" instead of erroring out.

The set of files yielded is otherwise identical to the ``rglob`` form it
replaces — pruning a directory the caller would have filtered out anyway
cannot change the result — and it is now sorted, so a guard's offender list
stops depending on ``os.scandir`` order.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from fnmatch import fnmatchcase
from pathlib import Path

# Never worth descending into, and the churn that started all this. Pruning
# these can't drop a source file: they hold build/scan artefacts, not inputs.
CHURN_DIRS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".ty_cache"}
)


# What ``rglob`` did with a directory it could not list, measured on CPython
# 3.11.15 by injecting each error into ``os.scandir``:
#
#   FileNotFoundError  -> propagated to the caller   (the RAF-170 failure)
#   PermissionError    -> swallowed silently, subtree skipped
#
# ``walk_files`` keeps the second half exactly as it was. That asymmetry looks
# untidy, but tightening it here would be a second, unrelated change smuggled
# into a race fix: this repo really does grow root-owned directories in the
# working tree (docker containment runs leave them behind), and a guard that
# went from "skipped that subtree" to "failed the gate" would trade the flake
# in this ticket for a different false red. If unreadable directories should
# fail a scan, that is its own decision, made once, for all the guards.
_SKIPPABLE = (FileNotFoundError, PermissionError)


def _skip_unreadable(exc: OSError) -> None:
    """``os.walk`` error hook: reproduce ``rglob``'s tolerances, minus the race.

    A directory that vanished mid-walk is the race this module exists for; an
    unreadable one is what ``rglob`` already ignored. Anything else — a broken
    mount, an I/O error — is re-raised, so this stays a fix for the race rather
    than a blanket "ignore the filesystem".
    """
    if not isinstance(exc, _SKIPPABLE):
        raise exc


def walk_files(
    root: Path,
    *,
    match: str = "*.py",
    prune: Callable[[Path], bool] | None = None,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(relative_path, absolute_path)`` for files under *root*.

    ``match`` is an ``fnmatch`` pattern applied to the file **name**, and
    always case-sensitively (``fnmatchcase``): plain ``fnmatch`` folds case
    on Windows/macOS, and the checked-in ratchet baselines these guards
    compare against were generated on Linux.

    ``prune`` receives each candidate directory's path *relative to root* and
    returns ``True`` to skip that whole subtree. It is called before the
    descent, so returning ``True`` means the directory is never opened.
    """
    root = Path(root)
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(
        root, onerror=_skip_unreadable, followlinks=False
    ):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in CHURN_DIRS
            and not (prune is not None and prune(rel_dir / name))
        )
        for name in sorted(filenames):
            if fnmatchcase(name, match):
                yield rel_dir / name, root / rel_dir / name


def prune_names(names) -> Callable[[Path], bool]:
    """Prune any directory whose own name is in *names*, at any depth.

    The ``any(part in EXCLUDED for part in rel.parts)`` filter, moved to
    where it can actually stop the walk.
    """
    names = frozenset(names)
    return lambda rel_dir: rel_dir.name in names


def prune_top_level(names) -> Callable[[Path], bool]:
    """Prune only *top-level* directories named in *names*.

    The ``rel.parts[0] in EXEMPT`` filter. Deliberately not the same as
    :func:`prune_names`: a guard that exempts top-level ``tests/`` still
    wants to scan ``plugins/foo/tests/``, and widening that here would
    silently shrink what the guard covers.
    """
    names = frozenset(names)
    return lambda rel_dir: len(rel_dir.parts) == 1 and rel_dir.name in names
