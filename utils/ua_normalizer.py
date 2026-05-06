from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass
class UaRecord:
    user_agent: str
    hit_count: int
    extra: Dict[str, Any]


def ua_hash(user_agent: str) -> str:
    return hashlib.sha256(user_agent.encode("utf-8", errors="ignore")).hexdigest()


def read_ua_csv(path: str | Path) -> List[UaRecord]:
    records: List[UaRecord] = []
    with Path(path).open("r", encoding="utf-8", errors="ignore", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t") if sample else csv.excel
        reader = csv.reader(handle, dialect)
        rows = list(reader)

    if not rows:
        return records

    header = [item.strip().lower() for item in rows[0]]
    has_header = any("useragent" in item or item == "count" or "count" in item for item in header)
    data_rows = rows[1:] if has_header else rows

    ua_idx, count_idx = _detect_columns(header if has_header else [], data_rows[:20])
    for row in data_rows:
        if not row or ua_idx >= len(row):
            continue
        ua = row[ua_idx].strip()
        if not ua:
            continue
        hit_count = 1
        if count_idx is not None and count_idx < len(row):
            hit_count = _to_int(row[count_idx], default=1)
        records.append(UaRecord(user_agent=ua, hit_count=hit_count, extra={"raw_row": row}))
    return records


def _detect_columns(header: List[str], rows: List[List[str]]) -> Tuple[int, Optional[int]]:
    if header:
        ua_idx = next((i for i, name in enumerate(header) if "useragent" in name or name == "user_agent"), len(header) - 1)
        count_idx = next((i for i, name in enumerate(header) if "count" in name or name in {"hits", "hit_count"}), None)
        return ua_idx, count_idx

    # No header: usually count,useragent OR useragent,count. Prefer longest text as UA and numeric as count.
    max_cols = max((len(row) for row in rows), default=1)
    ua_idx = 0
    count_idx = None
    if rows:
        scores = [0] * max_cols
        numeric_scores = [0] * max_cols
        for row in rows:
            for idx, value in enumerate(row):
                scores[idx] += len(value)
                if value.strip().isdigit():
                    numeric_scores[idx] += 1
        ua_idx = max(range(max_cols), key=lambda idx: scores[idx])
        count_candidates = [idx for idx in range(max_cols) if idx != ua_idx and numeric_scores[idx] > 0]
        count_idx = count_candidates[0] if count_candidates else None
    return ua_idx, count_idx


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip().replace(",", ""))
    except Exception:
        return default


class UaFilter:
    def __init__(self, config: Dict[str, Any], blocklist: Dict[str, Any]):
        filtering = config.get("filtering", {})
        self.min_len = int(filtering.get("min_ua_length", 5))
        self.max_len = int(filtering.get("max_ua_length", 1000))
        self.min_hits = int(filtering.get("min_hits", 1))
        self.hard_reject = [re.compile(p) for p in blocklist.get("hard_reject_regex", [])]
        self.desktop_reject = [re.compile(p) for p in blocklist.get("desktop_reject_regex", [])]
        self.iot_positive = [re.compile(p) for p in blocklist.get("iot_positive_regex", [])]

    def keep_pre_deviceatlas(self, record: UaRecord) -> Tuple[bool, str]:
        ua = record.user_agent.strip()
        if record.hit_count < self.min_hits:
            return False, "below_min_hits"
        if len(ua) < self.min_len:
            return False, "too_short"
        if len(ua) > self.max_len:
            return False, "too_long"
        if any(pattern.search(ua) for pattern in self.hard_reject):
            return False, "hard_reject"
        if any(pattern.search(ua) for pattern in self.desktop_reject):
            return False, "desktop_mobile_browser_reject"
        if any(pattern.search(ua) for pattern in self.iot_positive):
            return True, "iot_positive_signal"
        if _looks_like_short_app_ua(ua):
            return True, "short_app_ua"
        return True, "unknown_keep_for_deviceatlas"


def _looks_like_short_app_ua(ua: str) -> bool:
    if len(ua) > 180:
        return False
    if "Mozilla/" in ua or "Windows NT" in ua or "Macintosh" in ua:
        return False
    return bool(re.search(r"^[A-Za-z0-9_.+-]+/[A-Za-z0-9_.+-]+", ua))


def grouping_key(user_agent: str) -> str:
    ua = user_agent.strip()
    app_family = _app_family(ua)
    android_version = _match_or_unknown(r"Android\s+([0-9.]+)", ua)
    model = _extract_model(ua)
    build = _normalize_build(_match_or_unknown(r"Build/([A-Za-z0-9._-]+)", ua))
    return "|".join([app_family, android_version, model, build]).lower()


def _app_family(ua: str) -> str:
    first = ua.split(" ", 1)[0].strip()
    first = first.split("(", 1)[0].strip()
    first = re.sub(r"/[0-9][A-Za-z0-9._+-]*$", "", first)
    first = re.sub(r"/[0-9.]+$", "", first)
    return first or "unknown_app"


def _match_or_unknown(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.I)
    return match.group(1).strip() if match else "unknown"


def _extract_model(ua: str) -> str:
    patterns = [
        r"Android\s+[0-9.]+;\s*([^;)]+?)\s+Build/",
        r"Android\s+[0-9.]+;\s*([^;)]+?);\s*Build/",
        r";\s*([^;()]+?)\s+Build/",
        r"\((?:Linux;\s*U;\s*)?Android\s+[0-9.]+;\s*([^;)]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, ua, re.I)
        if match:
            model = match.group(1).strip()
            model = re.sub(r"^(U;|en[-_][A-Z]+;|[a-z]{2};)\s*", "", model, flags=re.I)
            return re.sub(r"\s+", " ", model)
    return "unknown_model"


def _normalize_build(build: str) -> str:
    if build == "unknown":
        return build
    parts = re.split(r"[._-]", build)
    return ".".join(parts[:2]) if len(parts) > 1 else build


def dedupe_records(records: Iterable[UaRecord]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for record in records:
        key = grouping_key(record.user_agent)
        current = groups.get(key)
        if current is None:
            groups[key] = {
                "group_key": key,
                "user_agent": record.user_agent,
                "hit_count": record.hit_count,
                "group_size": 1,
                "total_group_hits": record.hit_count,
            }
            continue
        current["group_size"] += 1
        current["total_group_hits"] += record.hit_count
        if record.hit_count > current["hit_count"]:
            current["user_agent"] = record.user_agent
            current["hit_count"] = record.hit_count
    return sorted(groups.values(), key=lambda item: item["total_group_hits"], reverse=True)
