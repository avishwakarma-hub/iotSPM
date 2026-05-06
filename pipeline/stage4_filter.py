from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, Tuple

from utils.config import load_yaml
from utils.ua_normalizer import UaFilter, UaRecord, dedupe_records, read_ua_csv


def filter_and_dedupe(cfg: Dict[str, Any], csv_path: str | Path) -> Tuple[Path, Dict[str, Any]]:
    blocklist = load_yaml("config/ua_blocklist.yaml")
    records = read_ua_csv(csv_path)
    ua_filter = UaFilter(cfg, blocklist)
    kept = []
    reject_reasons: Dict[str, int] = {}
    for record in records:
        keep, reason = ua_filter.keep_pre_deviceatlas(record)
        if keep:
            kept.append(record)
        else:
            reject_reasons[reason] = reject_reasons.get(reason, 0) + 1

    grouped = dedupe_records(kept)
    out_dir = Path(cfg["paths"]["cleaned_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{Path(csv_path).stem}.cleaned.csv"
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["total_group_hits", "hit_count", "group_size", "group_key", "user_agent"],
        )
        writer.writeheader()
        for item in grouped:
            writer.writerow(item)
    stats = {
        "input_records": len(records),
        "pre_deviceatlas_kept": len(kept),
        "deduped_groups": len(grouped),
        "reject_reasons": reject_reasons,
    }
    return output_path, stats
