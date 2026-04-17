from __future__ import annotations

import re
from pathlib import Path

_PRESETS_DIR = Path(__file__).parent.parent / "close_pr_presets"


def load_close_pr_presets() -> list[dict[str, str]]:
    """Return preset close messages loaded from markdown files in close_pr_presets/.

    Each .md file's first line may be a markdown heading (``# Title``) used as
    the display name; otherwise the filename stem (digits-prefix stripped,
    hyphens/underscores replaced with spaces, title-cased) is used as the name.
    Files are sorted by filename so numbering them controls display order.
    Returns a list of ``{"name": str, "body": str}`` dicts (files with empty
    bodies after stripping are skipped).
    """
    if not _PRESETS_DIR.is_dir():
        return []
    presets = []
    for path in sorted(_PRESETS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        lines = text.splitlines()
        if lines[0].startswith("# "):
            name = lines[0][2:].strip()
            body = "\n".join(lines[1:]).strip()
        else:
            stem = re.sub(r"^\d+-", "", path.stem)
            name = stem.replace("-", " ").replace("_", " ").title()
            body = text
        if body:
            presets.append({"name": name, "body": body})
    return presets
