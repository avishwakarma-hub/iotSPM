from __future__ import annotations

import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

from utils.config import load_yaml
from utils.db import Database
from utils.ua_normalizer import ua_hash


class DeviceAtlasMapper:
    def __init__(self, cfg: Dict[str, Any], db: Database):
        self.cfg = cfg
        self.db = db
        api_dir = Path(cfg.get("paths", {}).get("deviceatlas_python_api_dir", ""))
        if api_dir.exists() and str(api_dir) not in sys.path:
            sys.path.insert(0, str(api_dir))
        self.api = self._load_api()

    def _load_api(self):
        try:
            from mobi.mtld.da.device.device_api import DeviceApi  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "DeviceAtlas API import failed. Install deviceatlas-enterprise-python-3.2.1/API "
                "with `pip install -e .` or verify deviceatlas_python_api_dir."
            ) from exc
        json_file = Path(self.cfg["paths"]["deviceatlas_json"])
        if not json_file.exists():
            raise FileNotFoundError(f"DeviceAtlas JSON data file not found: {json_file}")
        api = DeviceApi()
        api.load_data_from_file(str(json_file))
        return api

    def map_user_agent(self, user_agent: str) -> Dict[str, Any]:
        digest = ua_hash(user_agent)
        cached = self.db.cache_get("deviceatlas_cache", digest)
        if cached:
            return json.loads(cached["properties_json"])
        properties = self.api.get_properties(user_agent) or {}
        properties = {key: _json_safe(value) for key, value in properties.items()}
        self.db.cache_deviceatlas(digest, user_agent, properties)
        return properties


def enrich_with_deviceatlas(cfg: Dict[str, Any], db: Database, cleaned_path: str | Path) -> Path:
    mapper = DeviceAtlasMapper(cfg, db)
    iot_types = load_yaml("config/iot_device_types.yaml")
    keep_types = {item.lower() for item in iot_types.get("keep", [])}
    reject_types = {item.lower() for item in iot_types.get("reject", [])}
    rows = _read_dicts(cleaned_path)
    workers = int(cfg.get("deviceatlas", {}).get("workers", 4))

    enriched: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(mapper.map_user_agent, row["user_agent"]): row for row in rows}
        for future in as_completed(future_map):
            row = future_map[future]
            props = future.result()
            hardware = str(props.get("primaryHardwareType", "") or "")
            is_rejected = hardware.lower() in reject_types
            is_iot = hardware.lower() in keep_types and not is_rejected
            row.update(
                {
                    "device_vendor": props.get("vendor", ""),
                    "device_model": props.get("model", ""),
                    "marketing_name": props.get("marketingName", ""),
                    "hardware_type": hardware,
                    "browser_name": props.get("browserName", ""),
                    "os_name": props.get("osName", ""),
                    "os_version": props.get("osVersion", ""),
                    "is_mobile_phone": props.get("isMobilePhone", ""),
                    "is_tablet": props.get("isTablet", ""),
                    "is_robot": props.get("isRobot", ""),
                    "is_iot_candidate": "yes" if is_iot else "no",
                    "deviceatlas_json": json.dumps(props, ensure_ascii=False),
                }
            )
            enriched.append(row)

    enriched.sort(key=lambda item: int(item.get("total_group_hits") or 0), reverse=True)
    out_dir = Path(cfg["paths"]["enriched_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"{Path(cleaned_path).stem}.deviceatlas.csv"
    fieldnames = [
        "total_group_hits", "hit_count", "group_size", "group_key", "device_vendor", "device_model",
        "marketing_name", "hardware_type", "browser_name", "os_name", "os_version", "is_mobile_phone",
        "is_tablet", "is_robot", "is_iot_candidate", "user_agent", "deviceatlas_json",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(enriched)
    return output_path


def _read_dicts(path: str | Path) -> List[Dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
