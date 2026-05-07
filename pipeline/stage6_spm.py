from __future__ import annotations

import csv
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

from pipeline.artifacts import atomic_write_rows
from utils.db import Database
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
        self.db = db

    def analyze(self, user_agent: str) -> Tuple[str, List[Dict[str, Any]]]:
        digest = ua_hash(user_agent)
        cached = self.db.cache_get("spm_cache", digest)
        if cached:
            return cached["detection_status"], json.loads(cached["matches_json"])

        url = f"{self.cfg['url'].rstrip('/')}/api/tools/coverage/spm-analyze-file/"
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
        matches = response.json().get("data", {}).get("matches", [])
        status = classify_matches(matches)
        self.db.cache_spm(digest, user_agent, status, matches)
        return status, matches


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


def run_spm_check(cfg: Dict[str, Any], db: Database, enriched_path: str | Path) -> Path:
    rows = _read_dicts(enriched_path)
    rows = [row for row in rows if row.get("is_iot_candidate") == "yes"]
    analyzer = SpmAnalyzer(cfg, db)
    workers = int(cfg.get("spm", {}).get("workers", 10))
    out_dir = Path(cfg["paths"]["reports_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{Path(enriched_path).stem}.spm.csv"

    output_rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(analyzer.analyze, row["user_agent"]): row for row in rows}
        for future in as_completed(future_map):
            row = future_map[future]
            status, matches = future.result()
            row["spm_detection_status"] = status
            row["spm_match_count"] = len(matches)
            row["spm_matches_json"] = json.dumps(matches, ensure_ascii=False)
            output_rows.append(row)

    output_rows.sort(key=lambda item: int(item.get("total_group_hits") or 0), reverse=True)
    fieldnames = [
        "total_group_hits", "hit_count", "group_size", "hardware_type", "device_vendor", "device_model",
        "marketing_name", "iot_candidate_reason", "spm_detection_status", "spm_match_count", "user_agent", "spm_matches_json",
    ]
    atomic_write_rows(output_path, output_rows, fieldnames)
    return output_path


def _read_dicts(path: str | Path) -> List[Dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
