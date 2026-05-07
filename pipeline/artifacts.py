from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List


def path_exists(path: str | Path | None) -> bool:
    """Return True only when a configured artifact path points to a file."""

    return bool(path) and Path(path).is_file()


def should_reuse_artifact(path: str | Path | None, force: bool = False) -> bool:
    """Decide whether a stage can skip work and reuse its previous output."""

    return not force and path_exists(path)


def atomic_write_rows(output_path: str | Path, rows: Iterable[Dict[str, Any]], fieldnames: List[str]) -> None:
    """Write CSV rows via a temp file then atomically move into place.

    If a process dies mid-stage, the previous good output remains intact and a
    future restart can decide whether to reuse it or rebuild it.
    """

    import csv

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, output_path)
