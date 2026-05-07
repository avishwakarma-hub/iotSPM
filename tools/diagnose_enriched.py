from __future__ import annotations

import argparse
import collections
import csv
import json
import statistics
import time
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.stage6_spm import SpmAnalyzer, classify_matches
from pipeline.stage5_deviceatlas import IotCandidateClassifier
from utils.config import load_config
from utils.config import load_yaml
from utils.db import Database


SECRET_KEYS = {"password", "api_key", "token", "secret", "username"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile enriched IoT UA CSV quality and optionally smoke-test SPM.")
    parser.add_argument("csv_path", help="Path to enriched .csv file")
    parser.add_argument("--top", type=int, default=25, help="Rows to print from highest-hit enriched data")
    parser.add_argument("--spm-sample", type=int, default=0, help="Run SPM for top N IoT candidate rows")
    parser.add_argument("--spm-workers", type=int, default=1, help="Temporary worker count for this diagnostic")
    parser.add_argument("--config", default="config/settings.yaml", help="Config path")
    args = parser.parse_args()

    rows = read_rows(args.csv_path)
    print_profile(args.csv_path, rows, args.top)

    cfg = load_config(args.config)
    print_config_summary(cfg)

    if args.spm_sample:
        run_spm_sample(cfg, rows, args.spm_sample, args.spm_workers)


def read_rows(path: str | Path) -> List[Dict[str, str]]:
    csv_path = Path(path)
    print(f"file: {csv_path}")
    print(f"exists: {csv_path.exists()}")
    if csv_path.exists():
        print(f"size_mb: {csv_path.stat().st_size / (1024 * 1024):.2f}")
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def print_profile(path: str | Path, rows: List[Dict[str, str]], top_n: int) -> None:
    print("\n== Enriched CSV profile ==")
    print(f"rows: {len(rows)}")
    print(f"columns: {list(rows[0].keys()) if rows else []}")
    hits = [to_int(row.get("total_group_hits") or row.get("hit_count")) for row in rows]
    print(f"total_group_hits_sum: {sum(hits)}")
    print(f"max_group_hits: {max(hits) if hits else 0}")
    print(f"median_group_hits: {statistics.median(hits) if hits else 0}")
    print(f"single_hit_rows: {sum(1 for hit in hits if hit <= 1)}")
    print(f"rows_with_100_plus_hits: {sum(1 for hit in hits if hit >= 100)}")
    print(f"rows_with_1000_plus_hits: {sum(1 for hit in hits if hit >= 1000)}")

    print_counter("iot_candidate_counts", (row.get("is_iot_candidate", "") for row in rows))
    print_counter("hardware_type_top", (row.get("hardware_type") or "<blank>" for row in rows), 30)
    print_counter("vendor_top", (row.get("device_vendor") or "<blank>" for row in rows), 30)
    print_counter("browser_top", (row.get("browser_name") or "<blank>" for row in rows), 20)
    print_counter("os_top", (row.get("os_name") or "<blank>" for row in rows), 20)

    missing = {
        col: sum(1 for row in rows if not (row.get(col) or "").strip())
        for col in [
            "hardware_type",
            "device_vendor",
            "device_model",
            "marketing_name",
            "browser_name",
            "os_name",
            "deviceatlas_json",
        ]
    }
    print(f"missing_fields: {missing}")

    print_rescue_impact(rows)

    suspicious = find_suspicious_rows(rows)
    print(f"suspicious_iot_candidate_rows: {len(suspicious)}")
    for row in suspicious[:20]:
        print_row(row)

    print(f"\n== Top {top_n} rows by total_group_hits ==")
    for row in sorted(rows, key=hit_sort_key, reverse=True)[:top_n]:
        print_row(row)

    write_quality_samples(path, rows)


def print_counter(name: str, values: Iterable[str], limit: int = 20) -> None:
    print(f"{name}: {collections.Counter(values).most_common(limit)}")


def print_rescue_impact(rows: List[Dict[str, str]]) -> None:
    classifier = IotCandidateClassifier(load_yaml("config/iot_device_types.yaml"))
    rescued: List[Dict[str, str]] = []
    reasons: collections.Counter[str] = collections.Counter()
    for row in rows:
        try:
            props = json.loads(row.get("deviceatlas_json") or "{}")
        except Exception:
            props = {}
        is_iot, reason = classifier.classify(row.get("user_agent", ""), props)
        reasons[reason] += 1
        if row.get("is_iot_candidate") != "yes" and is_iot:
            rescued_row = dict(row)
            rescued_row["new_iot_candidate_reason"] = reason
            rescued.append(rescued_row)
    print(f"candidate_reason_counts_with_current_rules: {reasons.most_common(20)}")
    print(f"rescued_candidate_rows_if_reprocessed: {len(rescued)}")
    print(f"rescued_candidate_hits_if_reprocessed: {sum(hit_sort_key(row) for row in rescued)}")
    for row in sorted(rescued, key=hit_sort_key, reverse=True)[:20]:
        print_row(row)


def find_suspicious_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    suspicious_tokens = [
        "windows nt",
        "macintosh",
        "iphone",
        "ipad",
        "android",
        "chrome/",
        "safari/",
        "firefox/",
        "edg/",
        "opr/",
    ]
    suspicious_hardware = {"desktop", "mobile phone", "tablet"}
    out: List[Dict[str, str]] = []
    for row in rows:
        ua = (row.get("user_agent") or "").lower()
        hardware = (row.get("hardware_type") or "").lower()
        if row.get("is_iot_candidate") == "yes" and (
            hardware in suspicious_hardware or any(token in ua for token in suspicious_tokens)
        ):
            out.append(row)
    return sorted(out, key=hit_sort_key, reverse=True)


def write_quality_samples(path: str | Path, rows: List[Dict[str, str]]) -> None:
    base = Path(path)
    out_path = base.with_suffix(".quality_samples.csv")
    fields = [
        "bucket",
        "total_group_hits",
        "hit_count",
        "group_size",
        "hardware_type",
        "device_vendor",
        "device_model",
        "marketing_name",
        "is_iot_candidate",
        "browser_name",
        "os_name",
        "user_agent",
    ]
    selected: List[Dict[str, str]] = []
    buckets = {
        "top_all": sorted(rows, key=hit_sort_key, reverse=True)[:100],
        "top_iot_yes": sorted([r for r in rows if r.get("is_iot_candidate") == "yes"], key=hit_sort_key, reverse=True)[:100],
        "top_iot_no": sorted([r for r in rows if r.get("is_iot_candidate") != "yes"], key=hit_sort_key, reverse=True)[:100],
        "suspicious_iot_yes": find_suspicious_rows(rows)[:100],
    }
    for bucket, bucket_rows in buckets.items():
        for row in bucket_rows:
            item = {field: row.get(field, "") for field in fields}
            item["bucket"] = bucket
            selected.append(item)
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(selected)
    print(f"quality_sample_csv: {out_path}")


def print_config_summary(cfg: Dict[str, Any]) -> None:
    print("\n== Config summary (secrets redacted) ==")
    summary = {
        "db_path": cfg.get("paths", {}).get("db_path"),
        "reports_dir": cfg.get("paths", {}).get("reports_dir"),
        "spm": redact(cfg.get("spm", {})),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def run_spm_sample(cfg: Dict[str, Any], rows: List[Dict[str, str]], sample_size: int, workers: int) -> None:
    print("\n== SPM sample ==")
    if not cfg.get("spm", {}).get("enabled", True):
        print("SPM is disabled in config")
        return
    cfg = dict(cfg)
    cfg["spm"] = dict(cfg.get("spm", {}))
    cfg["spm"]["workers"] = workers
    db = Database(cfg["paths"]["db_path"])
    analyzer = SpmAnalyzer(cfg, db)
    sample = sorted([row for row in rows if row.get("is_iot_candidate") == "yes"], key=hit_sort_key, reverse=True)[:sample_size]
    print(f"sample_rows: {len(sample)}")
    start = time.perf_counter()
    statuses = collections.Counter()
    errors = 0
    for idx, row in enumerate(sample, start=1):
        row_start = time.perf_counter()
        try:
            status, matches = analyzer.analyze(row["user_agent"])
            statuses[status] += 1
            print(
                f"{idx:03d} status={status} matches={len(matches)} "
                f"elapsed={time.perf_counter() - row_start:.2f}s hits={row.get('total_group_hits')} "
                f"hw={row.get('hardware_type')} vendor={row.get('device_vendor')} model={row.get('device_model')}"
            )
            for match in matches[:3]:
                print("    match", json.dumps(match_summary(match), ensure_ascii=False)[:1000])
        except Exception as exc:
            errors += 1
            print(
                f"{idx:03d} ERROR elapsed={time.perf_counter() - row_start:.2f}s "
                f"hits={row.get('total_group_hits')} error={type(exc).__name__}: {exc}"
            )
    elapsed = time.perf_counter() - start
    print(f"spm_elapsed_seconds: {elapsed:.2f}")
    print(f"spm_avg_seconds_per_row: {(elapsed / len(sample)) if sample else 0:.2f}")
    print(f"spm_status_counts: {statuses}")
    print(f"spm_errors: {errors}")


def match_summary(match: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": match.get("id") or match.get("signature_id") or match.get("sig_id"),
        "public_title": match.get("public_title"),
        "info": match.get("info"),
        "status_classified": classify_matches([match]),
    }


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("***REDACTED***" if any(token in key.lower() for token in SECRET_KEYS) else redact(val)) for key, val in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def print_row(row: Dict[str, str]) -> None:
    fields = [
        "total_group_hits",
        "hit_count",
        "group_size",
        "hardware_type",
        "device_vendor",
        "device_model",
        "marketing_name",
        "is_iot_candidate",
        "browser_name",
        "os_name",
        "user_agent",
    ]
    compact = {key: row.get(key, "") for key in fields}
    print(json.dumps(compact, ensure_ascii=False)[:1600])


def hit_sort_key(row: Dict[str, str]) -> int:
    return to_int(row.get("total_group_hits") or row.get("hit_count"))


def to_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return 0


if __name__ == "__main__":
    main()