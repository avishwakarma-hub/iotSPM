from __future__ import annotations

import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

from pipeline.artifacts import atomic_write_rows
from utils.db import Database
from utils.progress import ProgressReporter
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

    def analyze(self, user_agent: str) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        kb_match = self._kb_match(user_agent)
        digest = ua_hash(user_agent)
        cached = self.db.cache_get("spm_cache", digest)
        if cached:
            return cached["detection_status"], json.loads(cached["matches_json"]), kb_match

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
        matches = response.json().get("data", {}).get("matches", [])
        status = classify_matches(matches)
        self.db.cache_spm(digest, user_agent, status, matches)
        return status, matches, kb_match

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
    analyzer = SpmAnalyzer(cfg, db)
    workers = int(cfg.get("spm", {}).get("workers", 10))
    out_dir = Path(cfg["paths"]["reports_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{Path(enriched_path).stem}.spm.csv"
    partial_path = output_path.with_suffix(output_path.suffix + ".partial")
    fieldnames = [
        "total_group_hits", "hit_count", "group_size", "hardware_type", "device_vendor", "device_model",
        "marketing_name", "iot_candidate_reason", "spm_detection_status", "spm_match_count",
        "kb_match", "kb_refid", "kb_device_type", "kb_pattern", "kb_family", "kb_family_size", "kb_export_id",
        "user_agent", "spm_matches_json",
    ]
    continue_on_error = bool(cfg.get("spm", {}).get("continue_on_error", True))

    output_rows: List[Dict[str, Any]] = _read_partial_rows(partial_path)
    completed_uas = {row.get("user_agent", "") for row in output_rows if row.get("spm_detection_status")}
    pending_rows = [row for row in rows if row.get("user_agent", "") not in completed_uas]
    if progress:
        resume_note = f"; resuming {len(output_rows)} completed" if output_rows else ""
        progress.info("spm", f"checking {len(rows)} IoT candidate UA groups with {workers} workers{resume_note}")
        progress.update("spm", len(output_rows), len(rows), force=True)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(analyzer.analyze, row["user_agent"]): row for row in pending_rows}
        for completed, future in enumerate(as_completed(future_map), start=len(output_rows) + 1):
            row = future_map[future]
            try:
                status, matches, kb_match = future.result()
            except Exception as exc:
                if not continue_on_error:
                    raise
                status = "spm-error"
                matches = [{"error": str(exc), "error_type": exc.__class__.__name__}]
                kb_match = {"matched": False}
            row["spm_detection_status"] = status
            row["spm_match_count"] = len(matches)
            _apply_kb_match(row, kb_match)
            row["spm_matches_json"] = json.dumps(matches, ensure_ascii=False)
            output_rows.append(row)
            _append_partial_row(partial_path, row, fieldnames)
            if progress:
                progress.update("spm", completed, len(rows), status)

    output_rows.sort(key=lambda item: int(item.get("total_group_hits") or 0), reverse=True)
    atomic_write_rows(output_path, output_rows, fieldnames)
    partial_path.unlink(missing_ok=True)
    return output_path


def _apply_kb_match(row: Dict[str, Any], kb_match: Dict[str, Any]) -> None:
    row["kb_match"] = "yes" if kb_match.get("matched") else "no"
    row["kb_refid"] = kb_match.get("refid", "")
    row["kb_device_type"] = kb_match.get("title", "")
    row["kb_pattern"] = kb_match.get("pattern", "")
    row["kb_family"] = kb_match.get("family", "")
    row["kb_family_size"] = kb_match.get("family_size", "")
    row["kb_export_id"] = kb_match.get("export_id", "")


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
