from __future__ import annotations

import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

from pipeline.artifacts import atomic_write_rows
from utils.db import Database
from utils.progress import ProgressReporter
from utils.review_filters import excluded_hardware_types, keep_review_row
from utils.spm_kb import SpmKnowledgeBase, load_knowledge_base
from utils.ua_normalizer import ua_hash


IOT_TICKETS = {
    "IoT.Device.DigitalSignageMediaPlayer", "IoT.Device.Projector", "IoT.Device.GamesConsole",
    "IoT.Device.Datacollectionterminal", "IoT.Device.CellularGateway", "IoT.Device.TelematicsControlUnit",
    "IoT.Device.BrailleTablet", "IoT.Device.SecurityHub", "IoT.Device.VRHeadset",
    "IoT.Device.VehicleMultimediaSystem", "IoT.Device.GeolocationTracker", "IoT.SmartHome.Gen",
    "IoT.Miscellaneous.Gen", "IoT.Adware.Gen32", "IoT.IndustryControlDevice.Gen2",
    "IoT.IndustryControlDevice.Gen", "IoT.3DPrinter.Gen", "IoT.NetworkingDevice.Gen",
    "IoT.IPCamera.Gen", "IoT.IPPhone.Gen", "IoT.SmartGlass.Gen", "IoT.SmartTV.Gen",
    "IoT.Smartwatch.Gen", "IoT.DVR.Gen", "IoT.MedicalDevice.Gen", "IoT.Printer.Gen",
    "IoT.WirelessHotspot.Gen", "IoT.SetTopBox.Gen", "IoT.PaymentTerminal.Gen", "IoT.MediaPlayer.Gen",
    "IoT.eReader.Gen", "IoT.DigitalHomeAsistant.Gen",
}


class SpmAnalyzer:
    def __init__(self, cfg: Dict[str, Any], db: Database):
        self.cfg = cfg.get("spm", {})
        self.full_cfg = cfg
        self.db = db
        self.kb = self._load_kb()

    def analyze(self, user_agent: str) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
        kb_match = self._kb_match(user_agent)
        digest = ua_hash(user_agent)
        cached = self.db.cache_get("spm_cache", digest)
        cache_policy = self._cache_policy()
        if cached and cache_policy.use_cache and not cache_policy.is_expired(cached["created_at"]):
            matches = json.loads(cached["matches_json"])
            status = cached["detection_status"]
            audit = _build_kb_live_audit(kb_match, status, matches, source="cache", cache_created_at=cached["created_at"])
            return status, matches, kb_match, audit

        matches = self._live_analyze(user_agent)
        status = classify_matches(matches)
        self.db.cache_spm(digest, user_agent, status, matches)
        source = "live-cache-expired" if cached else "live"
        if cached and not cache_policy.use_cache:
            source = "live-cache-bypassed"
        audit = _build_kb_live_audit(kb_match, status, matches, source=source, cache_created_at=cached["created_at"] if cached else "")
        return status, matches, kb_match, audit

    def _live_analyze(self, user_agent: str) -> List[Dict[str, Any]]:
        url = f"{self.cfg['url'].rstrip('/')}/api/tools/coverage/spm-analyze-file/"
        attempts = max(1, int(self.cfg.get("request_retries", 3)) + 1)
        retry_statuses = {int(code) for code in self.cfg.get("retry_status_codes", [429, 500, 502, 503, 504])}
        backoff_seconds = float(self.cfg.get("retry_backoff_seconds", 5))
        response = None
        for attempt in range(1, attempts + 1):
            try:
                response = requests.post(
                    url,
                    json={"user_agent": "custom", "url": "https://example.com", "custom_useragent": user_agent},
                    headers={
                        "accept": "application/json",
                        "Authorization": f"Api-Key {self.cfg['api_key']}",
                        "Content-Type": "application/json",
                    },
                    timeout=int(self.cfg.get("request_timeout_seconds", 45)),
                )
                response.raise_for_status()
                break
            except requests.HTTPError:
                if response is not None and response.status_code not in retry_statuses:
                    raise
                if attempt >= attempts:
                    raise
                sleep_for = backoff_seconds * attempt
                time.sleep(sleep_for)
            except (requests.Timeout, requests.ConnectionError):
                if attempt >= attempts:
                    raise
                sleep_for = backoff_seconds * attempt
                time.sleep(sleep_for)
        if response is None:
            raise RuntimeError("SPM request did not return a response")
        return response.json().get("data", {}).get("matches", [])

    def _cache_policy(self) -> "SpmCachePolicy":
        return SpmCachePolicy(
            use_cache=bool(self.cfg.get("use_cache", True)) and not bool(self.cfg.get("force_live_check", False)),
            ttl_days=_optional_positive_int(self.cfg.get("cache_ttl_days")),
        )

    def _load_kb(self) -> SpmKnowledgeBase | None:
        export_cfg = self.full_cfg.get("spm_export", {})
        if not export_cfg.get("enabled", True) or not export_cfg.get("use_in_stage6", True):
            return None
        kb_path = export_cfg.get("kb_cache_path")
        if not kb_path:
            kb_path = Path(self.full_cfg.get("base_dir", ".")) / "data" / "spm_knowledge_base.json"
        return load_knowledge_base(kb_path)

    def _kb_match(self, user_agent: str) -> Dict[str, Any]:
        if not self.kb:
            return {"matched": False}
        match = self.kb.match(user_agent)
        if not match:
            return {"matched": False, "export_id": self.kb.data.get("export_id", "")}
        return match


@dataclass(frozen=True)
class SpmCachePolicy:
    use_cache: bool = True
    ttl_days: int | None = None

    def is_expired(self, created_at: Any) -> bool:
        if self.ttl_days is None:
            return False
        created = _parse_datetime(created_at)
        if created is None:
            return True
        return datetime.now(timezone.utc) - created > timedelta(days=self.ttl_days)


def classify_matches(matches: List[Dict[str, Any]]) -> str:
    if not matches:
        return "not-present"
    status = "not-present"
    for sig in matches:
        title = str(sig.get("public_title", ""))
        info = str(sig.get("info", ""))
        if not _is_iot_sig(title):
            continue
        if "IoT-User Agent-Released" in info:
            return "detected-released"
        if "IoT-User Agent-Reviewed" in info:
            return "detected-reviewed"
        if "IoT-User Agent-Disabled" in info:
            status = "detected-disabled"
    return status


def _build_kb_live_audit(
    kb_match: Dict[str, Any],
    live_status: str,
    live_matches: List[Dict[str, Any]],
    *,
    source: str,
    cache_created_at: Any = "",
) -> Dict[str, Any]:
    kb_detected = bool(kb_match.get("matched"))
    live_iot_matches = [_summarize_live_match(sig) for sig in live_matches if _is_iot_sig(str(sig.get("public_title", "")))]
    live_detected = live_status != "not-present" and bool(live_iot_matches)
    if kb_detected and live_detected:
        verdict = "kb-and-live-agree-detected"
    elif not kb_detected and not live_detected:
        verdict = "kb-and-live-agree-not-present"
    elif kb_detected and not live_detected:
        verdict = "kb-only-review-match-logic-or-export-difference"
    else:
        verdict = "live-only-likely-review-mode-or-kb-miss"
    return {
        "spm_result_source": source,
        "spm_cache_created_at": cache_created_at or "",
        "kb_live_verdict": verdict,
        "live_iot_match_count": len(live_iot_matches),
        "live_iot_matches": live_iot_matches,
    }


def _summarize_live_match(sig: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": sig.get("id", ""),
        "refid": sig.get("refid", ""),
        "public_title": sig.get("public_title", ""),
        "info": sig.get("info", ""),
    }


def _is_iot_sig(title: str) -> bool:
    return (
        title.startswith("IoT.Device")
        or (title.startswith("IoT.") and title.endswith(".Categorization"))
        or (title.startswith("IoT.") and title.endswith(".Gen"))
        or title in IOT_TICKETS
    )


def run_spm_check(
    cfg: Dict[str, Any],
    db: Database,
    enriched_path: str | Path,
    progress: ProgressReporter | None = None,
) -> Path:
    rows = _read_dicts(enriched_path)
    rows = [row for row in rows if row.get("is_iot_candidate") == "yes"]
    excluded_hardware = excluded_hardware_types(cfg)
    if excluded_hardware:
        rows = [row for row in rows if str(row.get("hardware_type", "")).strip().casefold() not in excluded_hardware]
    analyzer = SpmAnalyzer(cfg, db)
    workers = int(cfg.get("spm", {}).get("workers", 10))
    out_dir = Path(cfg["paths"]["reports_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{Path(enriched_path).stem}.spm.csv"
    partial_path = output_path.with_suffix(output_path.suffix + ".partial")
    fieldnames = [
        "total_group_hits", "hit_count", "group_size", "hardware_type", "device_vendor", "device_model",
        "marketing_name", "iot_candidate_reason", "spm_detection_status", "spm_match_count",
        "spm_result_source", "spm_cache_created_at",
        "kb_live_verdict", "live_iot_match_count", "live_iot_matches_json",
        "kb_match", "kb_refid", "kb_smstat_id", "kb_signature_id", "kb_device_type", "kb_pattern", "kb_row_pattern",
        "kb_dependency_refids", "kb_dependency_patterns", "kb_match_terms", "kb_match_type",
        "kb_family", "kb_family_size", "kb_export_id",
        "user_agent", "spm_matches_json",
    ]
    continue_on_error = bool(cfg.get("spm", {}).get("continue_on_error", True))

    partial_rows: List[Dict[str, Any]] = _read_partial_rows(partial_path)
    output_rows: List[Dict[str, Any]] = [row for row in partial_rows if keep_review_row(row, cfg)]
    completed_uas = {row.get("user_agent", "") for row in partial_rows if row.get("spm_detection_status")}
    completed_count = len(partial_rows)
    pending_rows = [row for row in rows if row.get("user_agent", "") not in completed_uas]
    if progress:
        resume_note = f"; resuming {completed_count} completed" if completed_count else ""
        progress.info("spm", f"checking {len(rows)} IoT candidate UA groups with {workers} workers{resume_note}")
        progress.update("spm", completed_count, len(rows), force=True)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(analyzer.analyze, row["user_agent"]): row for row in pending_rows}
        for completed, future in enumerate(as_completed(future_map), start=completed_count + 1):
            row = future_map[future]
            try:
                status, matches, kb_match, live_audit = future.result()
            except Exception as exc:
                if not continue_on_error:
                    raise
                status = "spm-error"
                matches = [{"error": str(exc), "error_type": exc.__class__.__name__}]
                kb_match = {"matched": False}
                live_audit = {
                    "spm_result_source": "error",
                    "spm_cache_created_at": "",
                    "kb_live_verdict": "live-check-error",
                    "live_iot_match_count": 0,
                    "live_iot_matches": [],
                }
            row["spm_detection_status"] = status
            row["spm_match_count"] = len(matches)
            _apply_kb_match(row, kb_match)
            _apply_live_audit(row, live_audit)
            row["spm_matches_json"] = json.dumps(matches, ensure_ascii=False)
            _append_partial_row(partial_path, row, fieldnames)
            if keep_review_row(row, cfg):
                output_rows.append(row)
            if progress:
                progress.update("spm", completed, len(rows), status)

    output_rows.sort(key=lambda item: int(item.get("total_group_hits") or 0), reverse=True)
    atomic_write_rows(output_path, output_rows, fieldnames)
    partial_path.unlink(missing_ok=True)
    return output_path


def _apply_kb_match(row: Dict[str, Any], kb_match: Dict[str, Any]) -> None:
    row["kb_match"] = "yes" if kb_match.get("matched") else "no"
    row["kb_refid"] = kb_match.get("refid", "")
    row["kb_smstat_id"] = kb_match.get("smstat_id", "")
    row["kb_signature_id"] = kb_match.get("signature_id", "")
    row["kb_device_type"] = kb_match.get("title", "")
    row["kb_pattern"] = kb_match.get("pattern", "")
    row["kb_row_pattern"] = kb_match.get("row_pattern", "")
    row["kb_dependency_refids"] = _json_list(kb_match.get("dependency_refids") or [])
    row["kb_dependency_patterns"] = _json_list(kb_match.get("dependency_patterns") or [])
    row["kb_match_terms"] = _json_list(kb_match.get("match_terms") or [])
    row["kb_match_type"] = kb_match.get("match_type", "")
    row["kb_family"] = kb_match.get("family", "")
    row["kb_family_size"] = kb_match.get("family_size", "")
    row["kb_export_id"] = kb_match.get("export_id", "")


def _apply_live_audit(row: Dict[str, Any], live_audit: Dict[str, Any]) -> None:
    row["spm_result_source"] = live_audit.get("spm_result_source", "")
    row["spm_cache_created_at"] = live_audit.get("spm_cache_created_at", "")
    row["kb_live_verdict"] = live_audit.get("kb_live_verdict", "")
    row["live_iot_match_count"] = live_audit.get("live_iot_match_count", "")
    row["live_iot_matches_json"] = _json_list(live_audit.get("live_iot_matches") or [])


def _json_list(value: Any) -> str:
    if not value:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _optional_positive_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_partial_rows(partial_path: Path) -> List[Dict[str, Any]]:
    if not partial_path.is_file():
        return []
    return _read_dicts(partial_path)


def _append_partial_row(partial_path: Path, row: Dict[str, Any], fieldnames: List[str]) -> None:
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not partial_path.exists() or partial_path.stat().st_size == 0
    with partial_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())


def _read_dicts(path: str | Path) -> List[Dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
