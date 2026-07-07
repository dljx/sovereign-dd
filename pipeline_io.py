"""Shared pipeline file I/O — atomic writes + fail-loud ledger loading.

Used by both scout.py and gems.py (gems used to back-import these from scout,
which is how the two pipelines' file handling stayed in sync only by luck).
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically (tmp file + os.replace) so a crash mid-write can't
    corrupt a dedup/notify window file — a corrupt file used to load as {} and
    re-debate + re-alert everything."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_ledger(path: Path, label: str) -> dict:
    """Load a dedup/notify ledger. Missing file → {} (legit first run). A file
    that EXISTS but is corrupt or the wrong shape raises instead of silently
    starting fresh — a truncated CI cache or bad restore used to load as {}
    and re-debate + re-alert the entire rotation window in one run. Failing
    loud turns that into a workflow alert; delete the file to reset on purpose."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(
            f"{label} ledger at {path} is corrupt ({e}) — refusing to silently "
            f"start fresh; delete the file to reset deliberately") from e
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{label} ledger at {path} parsed to {type(data).__name__}, expected "
            f"dict — refusing to silently start fresh")
    return data
