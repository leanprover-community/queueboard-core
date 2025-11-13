from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any


def fixtures_dir() -> Path:
    """Return the canonical tests/fixtures directory.

    This resolves relative to this helpers.py file so tests can import and
    locate fixtures even if the test files move into subpackages.
    """
    return Path(__file__).resolve().parent / "fixtures"


def supported_timeline(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    supported = {
        "LabeledEvent",
        "UnlabeledEvent",
        "ReadyForReviewEvent",
        "ConvertToDraftEvent",
        "ReopenedEvent",
        "ClosedEvent",
        "HeadRefForcePushedEvent",
    }
    return [n for n in nodes if isinstance(n, dict) and n.get("__typename") in supported]
