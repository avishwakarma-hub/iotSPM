from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from utils.review_filters import keep_review_row


def build_review_report(cfg: Dict[str, Any], spm_path: str | Path) -> Path:
    rows = _read_dicts(spm_path)
    rows = [row for row in rows if keep_review_row(row, cfg)]
    out_dir = Path(cfg["paths"]["reports_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{Path(spm_path).stem}.review.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "IoT UA Review"
    headers = [
        "Priority", "Hits", "Group Size", "Hardware Type", "Vendor", "Model", "Marketing Name",
        "Candidate Reason", "SPM Status", "KB Match", "KB RefID", "KB SMStat ID", "KB Signature ID",
        "KB Device Type", "KB Pattern", "KB Row Pattern", "KB Dependencies", "KB Match Terms",
        "KB Match Type", "KB Family", "KB Family Size", "Consolidation Note", "Action",
        "Suggested Signature Seed", "User-Agent",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="D9EAF7", end_color="D9EAF7", fill_type="solid")

    for idx, row in enumerate(rows, start=1):
        status = row.get("spm_detection_status", "")
        action = _suggest_action(status, row)
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
            row.get("kb_match", ""),
            row.get("kb_refid", ""),
            row.get("kb_smstat_id", ""),
            row.get("kb_signature_id", ""),
            row.get("kb_device_type", ""),
            row.get("kb_pattern", ""),
            row.get("kb_row_pattern", ""),
            row.get("kb_dependency_patterns", ""),
            row.get("kb_match_terms", ""),
            row.get("kb_match_type", ""),
            row.get("kb_family", ""),
            _to_int(row.get("kb_family_size")),
            _consolidation_note(row),
            action,
            _signature_seed(row.get("user_agent", "")),
            row.get("user_agent", ""),
        ])
    for col in ws.columns:
        max_len = min(max(len(str(cell.value or "")) for cell in col) + 2, 80)
        ws.column_dimensions[col[0].column_letter].width = max_len
    wb.save(output_path)
    return output_path


def _suggest_action(status: str, row: Dict[str, Any] | None = None) -> str:
    row = row or {}
    if row.get("kb_match") == "yes" and status == "not-present":
        return "review-existing-kb-match"
    if status in {"not-present", "detected-disabled"}:
        return "review-add-signature"
    if status == "spm-error":
        return "retry-spm/manual-check"
    return "already-covered"


def _signature_seed(ua: str) -> str:
    # Conservative seed: exact UA first. Reviewer can generalize safely.
    return ua


def _consolidation_note(row: Dict[str, Any]) -> str:
    if row.get("kb_match") != "yes":
        return ""
    family = str(row.get("kb_family") or "").strip()
    family_size = _to_int(row.get("kb_family_size"))
    pattern = str(row.get("kb_pattern") or "").strip()
    device_type = str(row.get("kb_device_type") or "").strip()
    if family and family_size > 1:
        return (
            f"Fits existing {device_type} family '{family}' with {family_size} SPM patterns; "
            "review whether to consolidate/broaden the family instead of adding a narrow one-off signature."
        )
    return f"Matches existing SPM pattern '{pattern}' ({device_type}); verify why live SPM status differs if marked not-present."


def _read_dicts(path: str | Path) -> List[Dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0
