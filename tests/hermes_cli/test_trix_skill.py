"""The bundled product skill must be named trix-agent and stay upstream-free."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills/autonomous-ai-agents/trix-agent"


def _frontmatter() -> dict:
    text = SKILL_DIR.joinpath("SKILL.md").read_text(encoding="utf-8")
    _, fm, _ = text.split("---", 2)
    return yaml.safe_load(fm)


def test_skill_directory_exists():
    assert SKILL_DIR.is_dir()
    assert not (REPO_ROOT / "skills/autonomous-ai-agents/hermes-agent").exists()


def test_frontmatter_name_matches_directory():
    assert _frontmatter()["name"] == "trix-agent"


def test_frontmatter_has_no_upstream_branding():
    fm = _frontmatter()
    blob = f"{fm['description']} {fm['author']}"
    for word in ("Hermes", "Nous Research"):
        assert word not in blob


def test_skill_body_has_no_client_facing_upstream_references():
    offenders = []
    for path in SKILL_DIR.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        if "nousresearch.com" in text or "Hermes Agent" in text:
            offenders.append(str(path))
    assert offenders == [], f"upstream references left in: {offenders}"


def test_skill_never_points_the_agent_at_the_excluded_website_docs_tree():
    """`website/` is excluded from the client's sparse-checkout (see
    scripts/install.sh), and SKILL.md itself warns not to rely on it. A
    reference file pointing the agent there anyway sends it to a directory
    that doesn't exist on the machine it's actually running on."""
    offenders = []
    for path in SKILL_DIR.rglob("*.md"):
        if path.name == "SKILL.md":
            # SKILL.md's own warning against relying on website/docs is the
            # one legitimate mention -- it names the path to disclaim it,
            # not to send the agent there.
            continue
        if "website/docs" in path.read_text(encoding="utf-8"):
            offenders.append(str(path))
    assert offenders == [], f"dangling website/docs pointers left in: {offenders}"
