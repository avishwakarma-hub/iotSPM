from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.config import load_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Report grouped IoT SPM signature families from the local KB")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--kb", help="Path to spm_knowledge_base.json; defaults to config spm_export.kb_cache_path")
    parser.add_argument("--csv", dest="csv_path", help="Optional CSV output path")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    cfg = load_config(args.config)
    kb_path = Path(args.kb or cfg.get("spm_export", {}).get("kb_cache_path") or Path(cfg.get("base_dir", ".")) / "data" / "spm_knowledge_base.json")
    with kb_path.open("r", encoding="utf-8") as handle:
        kb = json.load(handle)

    rows = _family_rows(kb)
    print(f"KB: {kb_path}")
    print(f"Export: {kb.get('export_id')} built_at={kb.get('built_at')} signatures={kb.get('total_meaningful')}")
    print("Family | Device Type | Sigs | Patterns")
    print("-" * 110)
    for row in rows[: args.limit]:
        print(f"{row['family']:<16} | {row['device_type']:<36} | {row['size']:>4} | {row['sample_patterns']}")

    if args.csv_path:
        output_path = Path(args.csv_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["family", "device_type", "size", "sample_patterns", "all_patterns"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote CSV: {output_path}")


def _family_rows(kb: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for family in (kb.get("families") or {}).values():
        patterns = list(family.get("patterns") or [])
        rows.append(
            {
                "family": family.get("family", ""),
                "device_type": family.get("device_type", ""),
                "size": int(family.get("size") or 0),
                "sample_patterns": ", ".join(patterns[:8]),
                "all_patterns": " | ".join(patterns),
            }
        )
    return sorted(rows, key=lambda item: (-item["size"], item["device_type"], item["family"]))


if __name__ == "__main__":
    main()
