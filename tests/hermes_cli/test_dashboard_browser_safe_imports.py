"""Static dashboard tests for browser-safe @nous-research/ui imports."""
from pathlib import Path

from tests._source_tree import walk_files


WEB_SRC = Path(__file__).resolve().parents[2] / "web" / "src"


def test_dashboard_does_not_import_nous_ui_root_barrel():
    offenders = []
    # walk_files(), not rglob: see tests/_source_tree.py (RAF-171).
    for ext in ("*.tsx", "*.ts"):
        for _, path in walk_files(WEB_SRC, match=ext):
            content = path.read_text(encoding="utf-8")
            if 'from "@nous-research/ui"' in content or "from '@nous-research/ui'" in content:
                offenders.append(str(path.relative_to(WEB_SRC)))

    assert offenders == []
