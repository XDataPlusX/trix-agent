"""Test-relation: every search backend named in the curated config template
(assets/config/trix-config.yaml, ``web.search_backend``/``web.extract_backend``/
``web.backend``) must be satisfied EITHER by a package that
``install_deps()``'s default tier actually installs (core ``dependencies``
plus the resolved contents of the ``all`` extra in ``pyproject.toml`` --
NOT just "declared somewhere in some extra", since an extra nothing
installs by default covers nothing) OR by an explicit install step in
``scripts/install.sh``.

This is deliberately a *relation* between two data sources, not a snapshot
of the string "ddgs": if the template's backend value ever changes, or a
future backend is added, this test re-derives what needs covering from the
template itself rather than hardcoding an expected package name to assert
against.

Why this matters: ``ddgs`` is picked as ``web.search_backend`` precisely
because it needs neither an API key nor a side service (see
``docs/product/specs/2026-08-17-trix-agent-standard-build-design.md`` §4.2).
But ``_get_capability_backend()`` in ``tools/web_tools.py`` (lines 287-308)
does not error when the named backend's package is missing -- it silently
falls through to the ``firecrawl`` default and hands the client "buy a
Firecrawl key / log into Nous Portal" instead of a working DuckDuckGo
search. Picking ``ddgs`` in the config template and never installing the
``ddgs`` package therefore isn't "search is off", it's "search quietly
advertises someone else's paid product" -- worse than doing nothing. The
connection between "this backend is named in the template" and "this
backend's package actually gets installed by default" must hold as code,
not just in the author's head.

Being merely declared under an opt-in extra (``exa``, ``firecrawl``,
``parallel-web``) does NOT satisfy this relation: those extras are
lazy-install-only (see the policy comment on `all` in pyproject.toml) and
nothing in install_deps()'s default tier pulls them in, so naming one of
them as ``web.search_backend`` reproduces the exact silent-degrade defect
this test exists to catch. ``ddgs`` itself is declared as its own extra
too (so `hermes tools`' "ddgs" post_setup step, hermes_cli/tools_config.py,
and this installer share one version pin, see tools/lazy_deps.py), but it
is NOT in `all` either -- its coverage comes entirely from
scripts/install.sh explicitly installing it (see
`_INSTALLER_SEARCH_BACKEND_FUNCTIONS` below), which is the behavior this
test actually exercises. NOT a runtime self-heal in the DDGS provider's
own search(): unlike exa/firecrawl/parallel, DDGSWebSearchProvider.
is_available() (plugins/web/ddgs/provider.py) gates on the very import a
lazy install would need to repair, so search() never gets a chance to
call it with the package genuinely missing -- see that module's docstring,
and tests/hermes_cli/test_search_preflight.py /
tests/test_install_sh_ddgs_preflight.py for the install-time check that
closes the resulting gap instead.

The "installed by installer" half is proven by running the REAL
``install_ddgs_search_backend()`` shell function (extracted verbatim from
install.sh, same technique as test_install_sh_config_template_choice.py)
with ``uv`` faked to record what it was asked to install -- never a
source-text grep over install.sh.
"""

from __future__ import annotations

import re
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
TRIX_CONFIG = REPO_ROOT / "assets" / "config" / "trix-config.yaml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Bridges "backend name found in the config template" to "the install.sh
# function responsible for installing its package", for backends that take
# the installer-install route rather than being pulled in by the default
# `all` extra. A backend named in the template that has NEITHER default-tier
# pyproject.toml coverage NOR an entry here fails the relation test below --
# exactly the class of defect this test exists to catch (see module
# docstring).
_INSTALLER_SEARCH_BACKEND_FUNCTIONS = {
    "ddgs": "install_ddgs_search_backend",
}


def _search_backends_named_in_template() -> set[str]:
    with open(TRIX_CONFIG, encoding="utf-8") as fh:
        template = yaml.safe_load(fh)
    web = template.get("web") or {}
    backends = set()
    for key in ("search_backend", "extract_backend", "backend"):
        value = web.get(key)
        if value:
            backends.add(str(value).lower())
    return backends


def _package_names_installed_by_default() -> set[str]:
    """Package names install_deps()'s default tier actually installs.

    That tier is core `[project.dependencies]` plus `.[all]` (via
    `uv sync --extra all` / `uv pip install -e ".[all]"` -- see
    install_deps() in install.sh). `all`'s entries are references like
    `"hermes-agent[cron]"`; resolve those recursively into their extras'
    specs rather than just pattern-matching the extra *name* "all" as a
    literal package (it isn't one).

    Deliberately NOT "any name that appears under any
    optional-dependencies key" -- an extra nothing installs by default
    (`exa`, `firecrawl`, `ddgs`, ...) covers nothing here, even though the
    package name is technically "in pyproject.toml somewhere". That is the
    exact gap this helper exists to close (see module docstring).
    """
    with open(PYPROJECT, "rb") as fh:
        data = tomllib.load(fh)
    project = data.get("project", {})
    optional = project.get("optional-dependencies", {})

    specs: list[str] = list(project.get("dependencies", []))

    def _resolve_extra(extra_name: str, seen: set[str]) -> None:
        if extra_name in seen:
            return
        seen.add(extra_name)
        for entry in optional.get(extra_name, []):
            ref = re.match(r"^\s*hermes-agent\[([\w-]+)\]\s*$", entry)
            if ref:
                _resolve_extra(ref.group(1), seen)
            else:
                specs.append(entry)

    _resolve_extra("all", set())

    names = set()
    for spec in specs:
        m = re.match(r"^\s*([A-Za-z0-9_.-]+)", spec)
        if m:
            names.add(m.group(1).lower())
    return names


def _installed_by_default(backend: str, default_packages: set[str]) -> bool:
    return any(
        pkg == backend or pkg.startswith(f"{backend}-") or pkg.replace("-", "") == backend
        for pkg in default_packages
    )


_HARNESS_PRELUDE = """
set -e
DISTRO=ubuntu
INSTALL_DIR={install_dir!r}
USE_VENV=true
UV_CMD={uv_cmd!r}
log_info() {{ :; }}
log_warn() {{ :; }}
log_success() {{ :; }}
"""


def _run_installer_function(fn_name: str, tmp_path: Path) -> str:
    """Extracts and runs the real install.sh function `fn_name` with `uv`
    faked to log every invocation to a file. Returns the logged text (empty
    string if the function never invoked the fake uv)."""
    install_dir = tmp_path / "install"
    install_dir.mkdir(parents=True)
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir(parents=True)
    uv_log = tmp_path / "uv_invocations.log"
    uv_log.write_text("", encoding="utf-8")

    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {str(uv_log)!r}\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    script = (
        _HARNESS_PRELUDE.format(install_dir=str(install_dir), uv_cmd=str(fake_uv))
        + f"""eval "$(sed -n '/^{fn_name}()/,/^}}/p' {str(INSTALL_SH)!r})"
{fn_name}
"""
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )
    assert result.returncode == 0, (
        f"{fn_name}() must not fail the installer even with a stubbed "
        f"uv.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return uv_log.read_text(encoding="utf-8")


def _installed_by_installer(backend: str, tmp_path: Path) -> bool:
    fn_name = _INSTALLER_SEARCH_BACKEND_FUNCTIONS.get(backend)
    if fn_name is None:
        return False
    invocations = _run_installer_function(fn_name, tmp_path)
    return backend in invocations.lower()


class TestSearchBackendCoverage:
    def test_every_named_backend_is_covered(self, tmp_path):
        backends = _search_backends_named_in_template()
        assert backends, "expected at least one search backend named in trix-config.yaml"

        default_packages = _package_names_installed_by_default()
        uncovered = []
        for backend in sorted(backends):
            covered = _installed_by_default(backend, default_packages) or _installed_by_installer(
                backend, tmp_path / backend
            )
            if not covered:
                uncovered.append(backend)

        assert not uncovered, (
            f"search backend(s) named in trix-config.yaml with no coverage: "
            f"{uncovered} -- neither installed by default (core deps + the "
            f"`all` extra) nor installed by scripts/install.sh. Picking one "
            f"of these as web.search_backend silently degrades web_search "
            f"to the firecrawl default instead of erroring "
            f"(tools/web_tools.py:287-308)."
        )


@pytest.mark.parametrize("backend", sorted(_search_backends_named_in_template()))
def test_backend_named_in_template_is_installed_by_default_or_installer(backend, tmp_path):
    default_packages = _package_names_installed_by_default()
    covered = _installed_by_default(backend, default_packages) or _installed_by_installer(
        backend, tmp_path
    )
    assert covered, (
        f"search backend {backend!r} named in trix-config.yaml's web.* "
        f"settings is neither installed by default (core deps + the `all` "
        f"extra) nor installed by scripts/install.sh"
    )
