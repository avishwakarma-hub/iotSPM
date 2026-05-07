from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill


def build_review_report(cfg: Dict[str, Any], spm_path: str | Path) -> Path:
    rows = _read_dicts(spm_path)
    out_dir = Path(cfg["paths"]["reports_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{Path(spm_path).stem}.review.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "IoT UA Review"
    headers = [
        "Priority", "Hits", "Group Size", "Hardware Type", "Vendor", "Model", "Marketing Name",
        "Candidate Reason", "SPM Status", "Action", "Suggested Signature Seed", "User-Agent",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="D9EAF7", end_color="D9EAF7", fill_type="solid")

    for idx, row in enumerate(rows, start=1):
        status = row.get("spm_detection_status", "")
        action = "review-add-signature" if status in {"not-present", "detected-disabled"} else "already-covered"
        ws.append([
            idx,
            _to_int(row.get("total_group_hits")),
            _to_int(row.get("group_size")),
            row.get("hardware_type", ""),
            row.get("device_vendor", ""),
            row.get("device_model", ""),
            row.get("marketing_name", ""),
            row.get("iot_candidate_reason", ""),
            status,
            action,
            _signature_seed(row.get("user_agent", "")),
            row.get("user_agent", ""),
        ])
    for col in ws.columns:
        max_len = min(max(len(str(cell.value or "")) for cell in col) + 2, 80)
        ws.column_dimensions[col[0].column_letter].width = max_len
    wb.save(output_path)
    return output_path


def _signature_seed(ua: str) -> str:
    # Conservative seed: exact UA first. Reviewer can generalize safely.
    return ua


def _read_dicts(path: str | Path) -> List[Dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0
