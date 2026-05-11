from __future__ import annotations

from typing import Any, Dict


DEFAULT_EXCLUDED_HARDWARE_TYPES = ["Mobile Phone"]
DEFAULT_EXCLUDED_DETECTION_STATUSES = ["detected-released"]


def excluded_hardware_types(cfg: Dict[str, Any]) -> set[str]:
    values = cfg.get("spm", {}).get("exclude_hardware_types", DEFAULT_EXCLUDED_HARDWARE_TYPES)
    return _casefold_set(values)


def excluded_detection_statuses(cfg: Dict[str, Any]) -> set[str]:
    values = cfg.get("spm", {}).get("exclude_detection_statuses", DEFAULT_EXCLUDED_DETECTION_STATUSES)
    return _casefold_set(values)


def keep_review_row(row: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    hardware = str(row.get("hardware_type", "")).strip().casefold()
    status = str(row.get("spm_detection_status", "")).strip().casefold()
    return hardware not in excluded_hardware_types(cfg) and status not in excluded_detection_statuses(cfg)


def _casefold_set(values: Any) -> set[str]:
    if not isinstance(values, list):
        values = [values]
    return {str(value).strip().casefold() for value in values if str(value or "").strip()}