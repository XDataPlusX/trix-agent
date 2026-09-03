"""User-facing help strings must not point at upstream documentation.

This used to scan a hardcoded WATCHED list of five files -- the exact five
that a prior task had already fixed -- so it could never catch a doc-site
link anywhere else in the tree. It now scans every ``.py`` file in the repo
(excluding the venv, the test suite itself, and the separate marketing
website) for the doc-site host, so a new upstream link anywhere in the
product surfaces here instead of shipping silently.

A later review found survivors outside ``.py`` files entirely -- the
dashboard's docs iframe (``.tsx``), the installer scripts (``.sh``/``.ps1``/
``.cmd``), a shipped config example (``.yaml``), and desktop i18n strings
(``.ts``). The scan now covers those extensions too. Four categories of
upstream-owned infrastructure are deliberately out of scope (see the
exclusions/pins below, each with its own rationale): the marketing website
build config, the dev-only sandbox script, upstream's GitHub Actions
workflows, and functional API endpoints the product still talks to.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DOMAIN = "hermes-agent.nousresearch.com"

# ``.py`` was the original scope. ``.tsx``/``.ts`` cover the web dashboard
# and Electron desktop app; ``.yaml``/``.yml`` cover shipped config examples
# and plugin manifests; ``.sh``/``.ps1``/``.cmd`` cover the installer
# scripts across all three supported shells; ``.rs`` covers the Rust
# bootstrap installer (apps/bootstrap-installer) -- added after
# install_script.rs's raw.githubusercontent.com/NousResearch/hermes-agent
# survived Task 9 undetected because this scan couldn't see Rust at all
# (Task 9b, fix round 1).
SCANNED_EXTENSIONS = (".py", ".tsx", ".ts", ".yaml", ".yml", ".sh", ".ps1", ".cmd", ".rs")

EXCLUDED_DIR_NAMES = {
    ".venv",
    "venv",
    "tests",
    # DO NOT TOUCH: the separate marketing/docs website. We build it but
    # never publish it for this product, so it's not customer-facing.
    "website",
    # DO NOT TOUCH: upstream's own GitHub Actions CI (deploy-site.yml,
    # skills-index-freshness.yml, etc.) -- infrastructure for a pipeline
    # this fork doesn't run, not something a customer ever sees.
    ".github",
    # Never product source -- third-party packages and VCS internals can
    # legitimately contain the upstream domain in their own metadata.
    "node_modules",
    ".git",
    # Agent-tool scratch checkouts and older worktree locations: a git
    # worktree is a SECOND copy of this repository on disk, so the scan
    # reports every upstream URL twice -- once at a path nobody edits.
    # That noise is worse than useless here: this guard's whole value is
    # that a hit means "a customer could see this", and a hit inside a
    # scratch checkout means nothing of the kind.
    ".claude",
    ".worktrees",
}

# Relative-path *prefix* exclusion, checked separately from EXCLUDED_DIR_NAMES
# (which matches a single path part and can't express a two-segment
# boundary). Only ``.superpowers/sdd/`` -- NOT all of ``.superpowers/`` -- is
# skipped: that subdirectory carries its own blanket .gitignore
# (``.superpowers/sdd/.gitignore``), so nothing placed there can ever ship.
# A file dropped directly under ``.superpowers/`` (one level up) is NOT
# git-ignored -- `touch .superpowers/x.py` shows up as untracked, not
# ignored -- so it must stay in scope. The boundary exists because a
# scan-for-URL-strings task inevitably writes its own findings into a
# generated pin-list scratch file under sdd/, which would otherwise trip
# the very scan it documents. Do not widen this past ``sdd/`` without
# re-confirming the gitignore boundary still lines up.
EXCLUDED_PATH_PREFIXES = (
    ".superpowers/sdd/",
    # DO NOT TOUCH: the owner's working reference file
    # (docs/product/configs_for_user/config.yaml), not a customer surface.
    # `docs/product` as a whole is excluded from the client checkout by
    # install.sh's sparse-checkout pattern (`'!/docs/product'`), so nothing
    # under it ever reaches a client machine -- but this prefix is scoped
    # to just this one subdirectory, not all of docs/product/, so any
    # *other* doc under docs/product/ that DOES leak an upstream link
    # still trips this scan (e.g. a customer-facing doc accidentally
    # dropped there).
    "docs/product/configs_for_user/",
)

# Files that are upstream-owned developer/CI infrastructure, not customer
# surfaces, and are therefore skipped entirely (DO NOT TOUCH list, item:
# "scripts/dev-sandbox.sh -- developer tooling"). It spins up a local HTTP
# fixture that mimics hermes-agent.nousresearch.com to test the installer
# against a fake upstream server; it never runs on a customer's machine.
EXCLUDED_FILES = {
    "scripts/dev-sandbox.sh",
}

# Endpoints the product still talks to on purpose (skills catalog, account
# services). Documented in the spec, section 4.2.
ALLOWED = (
    "portal.nousresearch.com",
    "api.nousresearch.com",
    "inference-api.nousresearch.com",
    "gateway-gateway.nousresearch.com",
    "staging-nousresearch.com",
)

# Runtime functional dependencies on hermes-agent.nousresearch.com itself
# (as opposed to a *documentation* link printed for a human to click).
# These fetch data the product needs to work (model/skills catalog
# manifests) or identify the app to a third-party API (HTTP-Referer
# attribution). Blanking them like the doc links below would break real
# features with no working replacement -- there is no XDataPlus-hosted
# substitute for any of them yet. Each entry pins to the exact known line so
# any *other* change to these files (e.g. a new customer-facing print) still
# trips the scan below and has to be looked at.
#
# The Telegram managed-bot onboarding relay (setup.hermes-agent.nousresearch.com)
# used to be pinned here too, for both the CLI's automatic-setup path
# (hermes_cli/telegram_managed_bot.py) and the dashboard's QR onboarding
# endpoints (hermes_cli/web_server.py). Both were removed: Trix Agent's only
# supported way to connect Telegram is a token the client creates with
# @BotFather and pastes locally, so nothing in setup talks to a third party.
KNOWN_FUNCTIONAL_DEPENDENCIES = {
    "tools/skills_hub.py": (
        'HERMES_INDEX_URL = "https://hermes-agent.nousresearch.com/docs/api/skills-index.json"',
    ),
    "plugins/model-providers/ai-gateway/__init__.py": (
        '"HTTP-Referer": "https://hermes-agent.nousresearch.com",',
    ),
    "plugins/model-providers/fireworks/__init__.py": (
        '"HTTP-Referer": "https://hermes-agent.nousresearch.com",',
    ),
    "plugins/model-providers/kimi-coding/__init__.py": (
        '"HTTP-Referer": "https://hermes-agent.nousresearch.com",',
    ),
    "agent/auxiliary_client.py": (
        '"HTTP-Referer": "https://hermes-agent.nousresearch.com",',
    ),
    "agent/anthropic_adapter.py": (
        '"HTTP-Referer": "https://hermes-agent.nousresearch.com",',
    ),
    "scripts/build_model_catalog.py": (
        '"docs": "https://hermes-agent.nousresearch.com/docs/reference/model-catalog",',
    ),
}


def _iter_repo_scanned_files():
    for ext in SCANNED_EXTENSIONS:
        for path in REPO_ROOT.rglob(f"*{ext}"):
            rel = path.relative_to(REPO_ROOT)
            if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
                continue
            if str(rel) in EXCLUDED_FILES:
                continue
            if str(rel).replace("\\", "/").startswith(EXCLUDED_PATH_PREFIXES):
                continue
            yield path


def test_no_doc_site_links_anywhere_in_the_product():
    offenders = []
    for path in _iter_repo_scanned_files():
        rel = str(path.relative_to(REPO_ROOT))
        known = KNOWN_FUNCTIONAL_DEPENDENCIES.get(rel, ())
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(lines, 1):
            if DOMAIN not in line:
                continue
            if any(a in line for a in ALLOWED):
                continue
            if line.strip() in known:
                continue
            offenders.append(f"{rel}:{lineno}: {line.strip()[:120]}")

    assert offenders == [], (
        "doc-site links (or unrecognized hermes-agent.nousresearch.com "
        "references) left in:\n" + "\n".join(offenders)
    )


# --- Second scan: the upstream repository address ------------------------
#
# The scan above only ever looked for the documentation site. It never
# looked for the address of the repository itself -- and that's exactly
# what the installer used to clone from, and what a runtime fetch or a
# bootstrap download can still reach out to today.
#
# This used to be a tuple of host+path spellings
# ("github.com/NousResearch/hermes-agent", "github.com:NousResearch/...",
# then "raw.githubusercontent.com/NousResearch/..." after fix round 1).
# That approach lost three times in a row: raw.githubusercontent.com is a
# different HOST from github.com; ghcr.io and Docker Hub are different
# hosts again (and don't even use "github.com" as a prefix); and
# lowercase "github.com/nousresearch/hermes-agent" is a different STRING
# from the mixed-case marker even on the SAME host. A marker list only
# ever catches spellings someone already found -- every new surface the
# product grows (a container registry, a package registry, a REST call
# built from a constant, a copy-pasted lowercase URL) is just one more
# spelling of the SAME identity that the list hadn't seen yet.
#
# So this matches the identity itself -- the "NousResearch/hermes-agent"
# owner/repo slug -- case-insensitively, wherever it appears: any host
# (github.com, raw.githubusercontent.com, api.github.com, codeload,
# ghcr.io, Docker Hub, or no host at all -- a bare slug baked into a
# constant), in SSH or HTTPS form, in any case. The boundaries on both
# sides keep it from false-positiving on a DIFFERENT repo that merely
# shares part of the slug: the trailing ``(?![\w-])`` excludes a
# HuggingFace dataset named "NousResearch/hermes-agent-megascience-sft1",
# and the leading ``(?<![\w-])`` excludes an unrelated org name that
# happens to end the same way, e.g. "xnousresearch/hermes-agent".
#
# Links of the form .../issues/123 and .../pull/456, and GitHub's inline
# "owner/repo#123" shorthand for the same thing (heavily used in code
# comments across this tree, e.g. "NousResearch/hermes-agent#67052"), are
# deliberately NOT treated as violations -- they're references to
# upstream discussions inside code comments, never shipped or shown to a
# user.

UPSTREAM_REPO_IDENTITY = re.compile(r"(?<![\w-])nousresearch/hermes-agent(?![\w-])", re.IGNORECASE)
UPSTREAM_ISSUE_SHORTHAND = re.compile(r"hermes-agent#\d+", re.IGNORECASE)

# Files outside the shipped product: the marketing site build (never on a
# client VM), upstream's own CI, and the developer sandbox + its E2E
# harness (never runs on a client machine either).
EXCLUDED_FILES_REPO_SCAN = {
    "scripts/dev-sandbox.sh",
    "tests/install/install-update-e2e.sh",
    # Fix round 3: pure CI-internal commentary (which prebuilt Docker image a
    # dedicated CI job uses) with no functional dependency and nothing that
    # travels off this machine -- same class as dev-sandbox.sh above. Judgment
    # call, not a coordinator ruling: contrast with
    # scripts/contributor_audit.py and scripts/generate_conformance_vectors.py
    # below, which stay pinned rather than excluded because one is a real
    # upstream API dependency and the other stamps the slug into output
    # committed to another repository.
    "scripts/run_tests_parallel.py",
}

# Pins. Every line is waiting on the task that rewrites it; the list only
# shrinks as that work lands, and by the end should hold nothing but
# entries that are staying on purpose.
KNOWN_UPSTREAM_REPO_REFS = {
    # Third-party attribution headers -- left in place on purpose. Changing
    # the identifying URL risks the app losing whatever goodwill/rate-limit
    # allowance the provider extends to it under this identity (see
    # STATUS.md). Not part of this migration's scope.
    "tools/discord_tool.py": (
        '"User-Agent": "Hermes-Agent (https://github.com/NousResearch/hermes-agent)",',
    ),
    "plugins/image_gen/openrouter/__init__.py": (
        '"HTTP-Referer": "https://github.com/NousResearch/hermes-agent",',
    ),
    "optional-skills/research/osint-investigation/scripts/_http.py": (
        '"(+https://github.com/NousResearch/hermes-agent; "',
    ),
    # Task 10: the remaining desktop/plugin/release-script surfaces that
    # print or fetch the upstream repo URL. (Task 9 owned the desktop app's
    # own updater/remote logic, its onboarding docs link, and the About
    # release-notes link -- all fixed, pins removed.)
    # raw.githubusercontent.com offenders, surfaced only once the marker
    # above was widened to see that host (Task 7, fix round 1). Each is
    # pinned to the task that owns fixing it -- do not fix these here,
    # that would break those tasks' sequencing.
    #
    # Task 11 (done): the runtime model-catalog fallback fetch used to point
    # at raw.githubusercontent.com/NousResearch/hermes-agent/main/... . The
    # primary URL (hermes_cli/model_catalog.py DEFAULT_CATALOG_URL) now IS a
    # raw.githubusercontent.com/xdataplusx/trix-agent/... address, so the
    # separate fallback URL collapsed to a duplicate of the primary and was
    # removed rather than kept as dead weight (DEFAULT_CATALOG_FALLBACK_URLS
    # is now an empty tuple). No pin needed here anymore.
    # --- Fix round 2: surfaced only once the marker matched the identity ---
    # --- rather than a spelling (ghcr.io, Docker Hub, and a bare slug    ---
    # --- were each a NEW host/format the old marker list had never seen). -
    #
    # Task 18 (container-image naming, not yet started as of this task):
    # every one of these prints or references the upstream-published
    # container image name, not a git remote -- a different rebrand than
    # anything Task 7 touches, and out of scope here.
    "tools/browser_tool.py": (
        '"docker pull ghcr.io/nousresearch/hermes-agent:latest"',
        'print("       docker pull ghcr.io/nousresearch/hermes-agent:latest")',
    ),
    "hermes_cli/tools_config.py": (
        '"      docker pull ghcr.io/nousresearch/hermes-agent:latest"',
    ),
    "hermes_cli/config.py": (
        "- the published ``nousresearch/hermes-agent`` image bakes a ``docker``",
        'return "docker pull nousresearch/hermes-agent:latest"',
        "Hermes Agent runs as a published image (nousresearch/hermes-agent), not a",
        "docker pull nousresearch/hermes-agent:latest",
        "docker run --rm nousresearch/hermes-agent:latest --version",
        "tags at https://hub.docker.com/r/nousresearch/hermes-agent/tags",
    ),
    "docker-compose.windows.yml": (
        "image: nousresearch/hermes-agent:latest",
    ),
    # Deliberate, permanent -- ruled on by the product owner: the skills
    # catalogue keeps talking to Nous's servers because functionality
    # (customers actually getting the catalogue) beats concealment. Do not
    # "fix" this without a working XDataPlus-hosted replacement catalogue
    # endpoint AND an explicit decision to cut over.
    "tools/skills_hub.py": (
        'OFFICIAL_REPO = "NousResearch/hermes-agent"',
    ),
    # Deliberate, permanent -- release_source.py IS the single source of
    # truth this whole migration is built on (Task 1); this line is its own
    # docstring documenting the invariant that the ``upstream`` remote only
    # ever exists in the XDataPlus working repo, never on a client machine.
    # It is prose about the old repo, in a file nobody but a developer
    # reading source ever opens -- not a customer-facing reference to it.
    "hermes_cli/release_source.py": (
        "NousResearch/hermes-agent существует только в рабочем репозитории",
    ),
    # Deliberate, permanent -- this IS the fix, not a leak: the literal
    # install.sh's origin-repoint logic (Task 7, this task) matches against
    # to recognize "this checkout was cloned from upstream" and repoint it
    # to the release repository. Removing the string would remove the
    # detection.
    "scripts/install.sh": (
        "*NousResearch/hermes-agent*)",
    ),
    # Deliberate, permanent -- same fix, PowerShell side (Task 8, fix round
    # 1): install.ps1's own origin-repoint logic matches this identity to
    # recognize a pre-Task-8 checkout still pointed at upstream and repoint
    # it before the release-branch fetch. Removing the string removes the
    # detection and permanently strands every such install on upstream code.
    "scripts/install.ps1": (
        'if ($currentOrigin -like "*NousResearch/hermes-agent*") {',
    ),
    # Fix round 3: adjudicated per-file rather than treated as one class.
    # scripts/run_tests_parallel.py (a CI comment with no functional tie and
    # nothing that leaves this machine) moved to EXCLUDED_FILES_REPO_SCAN
    # above instead of staying pinned here.
    #
    # contributor_audit.py stays pinned, not excluded: it is a genuine
    # functional dependency on the real upstream repo (the `gh pr list
    # --repo` call actually has to name the repo whose PRs it audits) --
    # excluding the file would hide that dependency from this scan instead
    # of documenting it.
    "scripts/contributor_audit.py": (
        '"--repo", "NousResearch/hermes-agent",',
    ),
    # generate_conformance_vectors.py stays pinned, not excluded: the slug
    # is stamped as `oracle.repo` provenance into generated JSON vectors
    # that get committed into another repository, so -- unlike
    # run_tests_parallel.py's comment -- this string actually travels off
    # this machine.
    "scripts/generate_conformance_vectors.py": (
        '"repo": "NousResearch/hermes-agent",',
    ),
}


def test_no_upstream_repo_urls_in_the_product():
    offenders = []
    for path in _iter_repo_scanned_files():
        rel = str(path.relative_to(REPO_ROOT))
        if rel in EXCLUDED_FILES_REPO_SCAN:
            continue
        known = KNOWN_UPSTREAM_REPO_REFS.get(rel, ())
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(lines, 1):
            if not UPSTREAM_REPO_IDENTITY.search(line):
                continue
            if "/issues/" in line or "/pull/" in line:
                continue
            if UPSTREAM_ISSUE_SHORTHAND.search(line):
                continue
            if line.strip() in known:
                continue
            offenders.append(f"{rel}:{lineno}: {line.strip()[:120]}")

    assert offenders == [], (
        "unpinned NousResearch/hermes-agent repository URLs left in:\n"
        + "\n".join(offenders)
    )


def test_pin_list_has_no_dead_entries():
    """A pin with nothing left to cover is forgotten litter.

    Without this check the pin list only ever grows: a line gets fixed,
    its pin stays behind, and it silently permits some future merge to
    put the same literal right back in that spot.
    """
    stale = []
    for rel, pinned in KNOWN_UPSTREAM_REPO_REFS.items():
        path = REPO_ROOT / rel
        if not path.exists():
            stale.append(f"{rel}: file does not exist")
            continue
        content = path.read_text(encoding="utf-8")
        for pin in pinned:
            if pin not in content:
                stale.append(f"{rel}: pin no longer appears: {pin[:80]}")
    assert stale == [], "stale pins:\n" + "\n".join(stale)
