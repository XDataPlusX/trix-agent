"""The walk that guard tests use must not race the other 18 workers.

Two things are pinned here. First that :func:`walk_files` actually has the
properties its callers depend on — it prunes *before* descending, and it
survives a directory that disappears mid-walk. Second, a ratchet: no test may
go back to ``REPO_ROOT.rglob(...)``, because that form is what broke the gate
in RAF-170 and it is trivially copy-pasteable.

Background in ``tests/_source_tree.py``. Short version: ``rglob`` filters the
paths it has already yielded, so the caller's exclusion list never stops the
descent; ``scripts/run_tests.sh`` runs ~18 concurrent pytest subprocesses that
create and delete ``__pycache__`` all over the tree; ``os.scandir`` opens one
that vanished and ``rglob`` raises ``FileNotFoundError`` at the caller.
"""

from __future__ import annotations

import ast
import errno
import os
from pathlib import Path

import pytest

from tests._source_tree import prune_names, prune_top_level, walk_files

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = REPO_ROOT / "tests"


def _tree(root: Path) -> None:
    """A miniature repo: one real source dir, one excluded dir, one cache."""
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("", encoding="utf-8")
    (root / "pkg" / "__pycache__").mkdir()
    (root / "pkg" / "__pycache__" / "mod.pyc").write_bytes(b"")
    (root / "pkg" / "tests").mkdir()
    (root / "pkg" / "tests" / "test_mod.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_top.py").write_text("", encoding="utf-8")
    (root / "top.py").write_text("", encoding="utf-8")


def _scandir_spy(monkeypatch) -> list[Path]:
    """Record every directory the walk actually opens."""
    seen: list[Path] = []
    real = os.scandir

    def spy(path=".", *args, **kwargs):
        seen.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", spy)
    return seen


def test_excluded_directories_are_never_entered(tmp_path, monkeypatch):
    """The point of the fix: pruning happens before the descent.

    A filter applied to yielded paths still walks the excluded tree, which is
    both wasted work and the window the race lives in.
    """
    _tree(tmp_path)
    seen = _scandir_spy(monkeypatch)

    found = {str(rel) for rel, _ in walk_files(tmp_path, prune=prune_names({"tests"}))}

    assert found == {"top.py", os.path.join("pkg", "mod.py")}
    opened = {p.name for p in seen}
    assert "tests" not in opened, "walked into an excluded directory anyway"
    assert "__pycache__" not in opened, "walked into __pycache__ anyway"


def test_prune_top_level_leaves_nested_dirs_of_the_same_name(tmp_path):
    """``rel.parts[0] in EXEMPT`` is not ``name in EXEMPT``.

    Collapsing the two would silently shrink what a guard covers:
    ``plugins/foo/tests/`` is in scope for guards that only exempt the
    top-level ``tests/``.
    """
    _tree(tmp_path)

    found = {
        str(rel).replace("\\", "/")
        for rel, _ in walk_files(tmp_path, prune=prune_top_level({"tests"}))
    }

    assert found == {"top.py", "pkg/mod.py", "pkg/tests/test_mod.py"}


def test_directory_that_vanishes_mid_walk_does_not_raise(tmp_path, monkeypatch):
    """The RAF-170 failure itself, made deterministic.

    A neighbouring pytest subprocess removes a directory between the listing
    of its parent and the descent into it. ``rglob`` propagates that;
    ``walk_files`` treats it as "that subtree wasn't there".
    """
    _tree(tmp_path)
    victim = tmp_path / "pkg"
    real = os.scandir

    def vanishing(path=".", *args, **kwargs):
        if Path(path) == victim:
            raise FileNotFoundError(2, "No such file or directory", str(victim))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", vanishing)

    found = {str(rel) for rel, _ in walk_files(tmp_path, prune=prune_names({"tests"}))}
    assert found == {"top.py"}


def _scandir_raising(monkeypatch, victim: Path, exc: OSError):
    real = os.scandir

    def failing(path=".", *args, **kwargs):
        if Path(path) == victim:
            raise exc
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", failing)


def test_unreadable_directory_is_skipped_exactly_as_rglob_skipped_it(
    tmp_path, monkeypatch
):
    """Not an oversight — the behaviour being replaced.

    Measured on CPython 3.11.15: ``rglob`` propagates ``FileNotFoundError``
    (the bug) but swallows ``PermissionError`` and skips the subtree. Only the
    first half is this ticket's to change. Failing on unreadable directories
    would be a real tightening, and this repo grows root-owned ones on its own
    (docker containment runs), so it would swap one false gate red for another.
    """
    _tree(tmp_path)
    victim = tmp_path / "pkg"
    _scandir_raising(
        monkeypatch, victim, PermissionError(13, "Permission denied", str(victim))
    )

    found = {str(rel) for rel, _ in walk_files(tmp_path, prune=prune_names({"tests"}))}
    assert found == {"top.py"}


def test_a_real_filesystem_fault_still_propagates(tmp_path, monkeypatch):
    """Tolerating the race must not turn into ignoring the filesystem.

    An I/O error is a broken machine, not a neighbour being fast, and a guard
    that silently scans nothing passes for the wrong reason.
    """
    _tree(tmp_path)
    victim = tmp_path / "pkg"
    _scandir_raising(monkeypatch, victim, OSError(errno.EIO, "I/O error", str(victim)))

    with pytest.raises(OSError, match="I/O error"):
        list(walk_files(tmp_path))


def test_match_applies_to_the_file_name(tmp_path):
    _tree(tmp_path)
    found = {str(rel).replace("\\", "/") for rel, _ in walk_files(tmp_path, match="test_*.py")}
    assert found == {"tests/test_top.py", "pkg/tests/test_mod.py"}


def test_missing_root_yields_nothing(tmp_path):
    """Callers used to guard this with ``if not root.is_dir(): continue``."""
    assert list(walk_files(tmp_path / "nope")) == []


# ---------------------------------------------------------------------------
# Ratchet
# ---------------------------------------------------------------------------

_RGLOB_HINT = (
    "Walking a repo directory with .rglob() / .glob('**/...') races the ~18 "
    "parallel pytest "
    "subprocesses that scripts/run_tests.sh spawns: rglob descends into the "
    "__pycache__ they create and delete, and raises FileNotFoundError "
    "mid-iteration (RAF-170/RAF-171). Filtering the yielded paths does not "
    "help -- the walk has already entered the directory.\n"
    "Use instead:\n"
    "    from tests._source_tree import walk_files, prune_names\n"
    "    for rel, path in walk_files(ROOT, match='*.py', prune=prune_names(EXCLUDED)):"
)


def _assignments(tree: ast.Module):
    """Every ``name = <expr>`` in the file, at any nesting depth."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            yield [t for t in node.targets if isinstance(t, ast.Name)], node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                yield [node.target], node.value


def _repo_rooted_names(tree: ast.Module) -> set[str]:
    """Names that denote a directory inside this repository.

    Two sources, and the second one is why this is a fixpoint rather than a
    single pass over ``tree.body``:

      * module-level constants (``REPO_ROOT``, ``SKILLS``, ``GATEWAY_DIR``);
      * anything derived from ``<module>.__file__``, at any nesting depth.

    The narrower "module-level names only" version of this check shipped
    first and reported zero offenders — while
    ``tests/hermes_cli/test_update_zip_two_phase.py`` was still walking the
    installed package via a function-local ``pkg = Path(hermes_cli.__file__)
    .parent`` and still dying on ``hermes_cli/__pycache__``. The 18-worker run
    caught it, not the guard. An instrument that reports "clean" has to be
    shown to be able to see, so: if you widen this file's exclusions, re-run
    the suite at ``-j 18`` rather than trusting the green.

    A local ``tmp_path``/``target``/``tree`` is not repo-rooted — nothing else
    is writing to a fixture's tempdir, so an rglob there races nobody.
    """
    rooted: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            rooted |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            rooted.add(node.target.id)

    assignments = list(_assignments(tree))
    changed = True
    while changed:
        changed = False
        for targets, value in assignments:
            if not targets:
                continue
            refs = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
            from_dunder_file = any(
                isinstance(n, ast.Attribute) and n.attr == "__file__"
                for n in ast.walk(value)
            )
            if not (from_dunder_file or (refs & rooted)):
                continue
            for target in targets:
                if target.id not in rooted:
                    rooted.add(target.id)
                    changed = True
    return rooted


def _recursive_walks_of_a_repo_path(tree: ast.Module) -> list[int]:
    """Line numbers of recursive globs rooted at a repository directory.

    ``rglob(...)`` and ``glob("**/...")`` are the same call — ``rglob(p)`` is
    documented as ``glob("**/" + p)`` — so a ratchet that only knows the first
    spelling is one rename away from useless.
    """
    rooted = _repo_rooted_names(tree)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr == "rglob":
            pass
        elif func.attr == "glob":
            first = node.args[0] if node.args else None
            pattern = first.value if isinstance(first, ast.Constant) else None
            if not (isinstance(pattern, str) and "**" in pattern):
                continue
        else:
            continue
        # Unwrap `ROOT / "skills"` down to `ROOT`.
        base = func.value
        while isinstance(base, ast.BinOp):
            base = base.left
        if isinstance(base, ast.Name) and base.id in rooted:
            hits.append(node.lineno)
    return hits


def test_no_test_walks_a_repo_directory_with_a_recursive_glob():
    offenders: list[str] = []
    for rel, path in walk_files(TESTS_ROOT, match="*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        rel_str = (Path("tests") / rel).as_posix()
        offenders += [
            f"{rel_str}:{lineno}"
            for lineno in _recursive_walks_of_a_repo_path(tree)
        ]

    assert not offenders, (
        "recursive glob over a repository directory:\n  "
        + "\n  ".join(offenders)
        + "\n\n"
        + _RGLOB_HINT
    )
