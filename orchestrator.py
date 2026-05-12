from __future__ import annotations

import csv
from collections import Counter, defaultdict
from html import escape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pipeline.artifacts import path_exists, should_reuse_artifact
from pipeline.stage1_rundeck import RundeckClient, day_window
from pipeline.stage2_download import download_drive_file
from pipeline.stage3_convert import convert_to_csv_if_needed
from pipeline.stage4_filter import filter_and_dedupe
from pipeline.stage5_deviceatlas import enrich_with_deviceatlas
from pipeline.stage6_spm import run_spm_check
from pipeline.stage7_report import build_focus_report, build_review_report, focus_report_path
from pipeline.stage8_upload import ReportUploadPermissionError, upload_report_if_enabled
from tools.spm_export_fetcher import ensure_spm_knowledge_base
from utils.db import Database
from utils.notifier import Notifier
from utils.progress import ProgressReporter, progress_from_config


STAGE_ORDER = ["download", "convert", "filter", "deviceatlas", "spm", "report", "upload"]
STAGE_STATE = {
    "download": "DOWNLOADED",
    "convert": "CONVERTED",
    "filter": "FILTERED",
    "deviceatlas": "DEVICEATLAS_ENRICHED",
    "spm": "SPM_CHECKED",
    "report": "REPORTED",
    "upload": "UPLOADED",
}


class PipelineOrchestrator:
    def __init__(self, cfg: Dict[str, Any], db: Database, logger):
        self.cfg = cfg
        self.db = db
        self.logger = logger
        self.notifier = Notifier(cfg, logger)
        self.progress = progress_from_config(cfg)

    def set_progress(self, progress: ProgressReporter) -> None:
        self.progress = progress

    def submit(self, day: str, query_name: str = "build_only") -> int:
        query = self.cfg["rundeck"]["queries"][query_name]
        start_time, end_time = day_window(day)
        run_id = self.db.create_run(day, query_name, query, start_time, end_time)
        self.logger.info("Created run %s for %s query=%s", run_id, day, query_name)
        client = RundeckClient(self.cfg)
        try:
            execution_id = client.submit_query(query, start_time, end_time, query_name=query_name)
            self.db.update_run(run_id, status="submitted", state="RUNDECK_SUBMITTED", execution_id=execution_id)
            self.notifier.send("Rundeck query submitted", f"Run {run_id} submitted. Execution ID: {execution_id}")
            return run_id
        except Exception as exc:
            self.db.update_run(run_id, status="failed", state="FAILED", error_message=str(exc))
            self.notifier.send("Rundeck query submission failed", f"Run {run_id} failed during submit:\n{exc}")
            raise

    def poll(self, run_id: int) -> Dict[str, Any]:
        run = self._run(run_id)
        if not run["execution_id"]:
            raise RuntimeError(f"Run {run_id} has no execution_id")
        client = RundeckClient(self.cfg)
        result = client.poll_execution(run["execution_id"])
        updates = {"status": result["status"], "state": f"RUNDECK_{result['status'].upper()}"}
        if result.get("drive_file_id"):
            updates["drive_file_id"] = result["drive_file_id"]
            updates["drive_file_name"] = result.get("drive_file_name")
        self.db.update_run(run_id, **updates)
        if result["status"] in {"failed", "aborted", "timedout"}:
            self.notifier.send("Rundeck query failed", f"Run {run_id} ended with status {result['status']}\n{result.get('url')}")
        if result["status"] == "succeeded":
            self.notifier.send("Rundeck query completed", f"Run {run_id} succeeded. Drive file: {result.get('drive_file_id')}")
        return result

    def poll_active(self, auto_process: bool = False) -> None:
        active_runs = self.db.list_active_runs()
        if not active_runs:
            print("No active Rundeck runs found.")
            return
        for run in active_runs:
            run_id = int(run["id"])
            try:
                result = self.poll(run_id)
                print(f"#{run_id} status={result['status']} drive_file_id={result.get('drive_file_id')}")
                if auto_process and result["status"] == "succeeded" and result.get("drive_file_id"):
                    report_path = self.process_ready_run(run_id)
                    print(f"#{run_id} processed: {report_path}")
            except Exception as exc:
                self.db.update_run(run_id, state="FAILED", status="failed", error_message=str(exc))
                self.logger.exception("Active poll failed for run %s", run_id)
                self.notifier.send("iotSPM active poll failed", f"Run {run_id} failed during active polling:\n{exc}")

    def process_ready_run(
        self,
        run_id: int,
        *,
        from_stage: Optional[str] = None,
        force_stages: Optional[Iterable[str]] = None,
        stop_after: Optional[str] = None,
    ) -> Path:
        force_set = {stage.strip().lower() for stage in (force_stages or []) if stage.strip()}
        if "all" in force_set:
            force_set = set(STAGE_ORDER)
        if from_stage:
            from_stage = from_stage.lower()
            self._validate_stage(from_stage)
            force_set.update(STAGE_ORDER[STAGE_ORDER.index(from_stage) :])
        if stop_after:
            stop_after = stop_after.lower()
            self._validate_stage(stop_after)

        run = self._run(run_id)
        if not run["drive_file_id"] and not run["raw_path"]:
            raise RuntimeError("Run does not have a Drive file or raw path yet. Poll until Rundeck succeeds.")

        try:
            self.progress.stage("pipeline", f"run_id={run_id}")
            raw_path = self._stage_download(run_id, run, force="download" in force_set)
            if self._should_stop(stop_after, "download"):
                return raw_path

            run = self._run(run_id)
            csv_path = self._stage_convert(run_id, raw_path, run, force="convert" in force_set)
            if self._should_stop(stop_after, "convert"):
                return csv_path

            run = self._run(run_id)
            cleaned_path = self._stage_filter(run_id, csv_path, run, force="filter" in force_set)
            if self._should_stop(stop_after, "filter"):
                return cleaned_path

            run = self._run(run_id)
            enriched_path = self._stage_deviceatlas(run_id, cleaned_path, run, force="deviceatlas" in force_set)
            if self._should_stop(stop_after, "deviceatlas"):
                return enriched_path

            self._sync_spm_kb(force="spm" in force_set)

            run = self._run(run_id)
            spm_path = self._stage_spm(run_id, enriched_path, run, force="spm" in force_set)
            if self._should_stop(stop_after, "spm"):
                return spm_path

            run = self._run(run_id)
            report_path = self._stage_report(run_id, spm_path, run, force="report" in force_set)
            if self._should_stop(stop_after, "report"):
                return report_path

            run = self._run(run_id)
            self._stage_upload(run_id, report_path, run, force="upload" in force_set)
            self.db.update_run(run_id, status="completed", state="COMPLETED", last_stage="completed", report_dir=str(report_path.parent))
            self.progress.done("pipeline", f"report={report_path}")
            text_body, html_body = self._completion_email(run_id, report_path)
            focus_path = focus_report_path(self.cfg, spm_path)
            attachments = [focus_path] if focus_path.is_file() else []
            self.notifier.send("iotSPM pipeline completed", text_body, html_body=html_body, attachments=attachments)
            return report_path
        except Exception as exc:
            self.db.update_run(run_id, status="failed", state="FAILED", error_message=str(exc))
            self.logger.exception("Run %s failed during processing", run_id)
            self.notifier.send("iotSPM pipeline failed", self._failure_body(run_id, exc))
            raise

    def run_local_file(self, path: str | Path, day: str = "manual", query_name: str = "local_file") -> Path:
        run_id = self.db.create_run(day, query_name, "local-file", "", "")
        self.db.update_run(run_id, raw_path=str(path), status="local", state="LOCAL_FILE")
        return self.process_ready_run(run_id)

    def status(self, limit: int = 20) -> None:
        for run in self.db.list_runs(limit):
            print(
                f"#{run['id']} {run['run_date']} {run['query_name']} state={run['state']} "
                f"status={run['status']} last_stage={run['last_stage']} exec={run['execution_id']} report={run['report_path'] or run['report_dir']}"
            )

    def _run(self, run_id: int):
        run = self.db.get_run(run_id)
        if not run:
            raise RuntimeError(f"Run not found: {run_id}")
        return run

    def _stage_download(self, run_id: int, run, *, force: bool) -> Path:
        self.progress.stage("download", "reuse existing artifact" if should_reuse_artifact(run["raw_path"], force=force) else "download from Drive/local input")
        if should_reuse_artifact(run["raw_path"], force=force):
            raw_path = Path(run["raw_path"])
            self.logger.info("Run %s reusing raw file: %s", run_id, raw_path)
        else:
            if run["raw_path"] and not path_exists(run["raw_path"]):
                raise FileNotFoundError(f"Raw/local input file not found: {run['raw_path']}")
            if not run["drive_file_id"]:
                raise RuntimeError("No Drive file id available for download stage")
            raw_path = download_drive_file(self.cfg, run["drive_file_id"])
            self.logger.info("Run %s downloaded/raw file: %s", run_id, raw_path)
        self.db.update_run(run_id, raw_path=str(raw_path), state=STAGE_STATE["download"], last_stage="download")
        self.progress.done("download", str(raw_path))
        return raw_path

    def _stage_convert(self, run_id: int, raw_path: Path, run, *, force: bool) -> Path:
        self.progress.stage("convert", "reuse existing artifact" if should_reuse_artifact(run["csv_path"], force=force) else "convert .current to CSV if needed")
        if should_reuse_artifact(run["csv_path"], force=force):
            csv_path = Path(run["csv_path"])
            self.logger.info("Run %s reusing CSV file: %s", run_id, csv_path)
        else:
            csv_path = convert_to_csv_if_needed(self.cfg, raw_path)
            self.logger.info("Run %s CSV file: %s", run_id, csv_path)
        self.db.update_run(run_id, csv_path=str(csv_path), state=STAGE_STATE["convert"], last_stage="convert")
        self.progress.done("convert", str(csv_path))
        return csv_path

    def _stage_filter(self, run_id: int, csv_path: Path, run, *, force: bool) -> Path:
        self.progress.stage("filter", "reuse existing artifact" if should_reuse_artifact(run["cleaned_path"], force=force) else "clean, reject obvious non-IoT, and dedupe UA variants")
        if should_reuse_artifact(run["cleaned_path"], force=force):
            cleaned_path = Path(run["cleaned_path"])
            self.logger.info("Run %s reusing cleaned file: %s", run_id, cleaned_path)
            self.db.update_run(run_id, cleaned_path=str(cleaned_path), state=STAGE_STATE["filter"], last_stage="filter")
            self.progress.done("filter", str(cleaned_path))
            return cleaned_path
        cleaned_path, filter_stats = filter_and_dedupe(self.cfg, csv_path)
        self.db.update_run(run_id, cleaned_path=str(cleaned_path), state=STAGE_STATE["filter"], last_stage="filter", stats_json=filter_stats)
        self.logger.info("Run %s cleaned file: %s stats=%s", run_id, cleaned_path, filter_stats)
        self.progress.done("filter", f"{cleaned_path} stats={filter_stats}")
        return cleaned_path

    def _stage_deviceatlas(self, run_id: int, cleaned_path: Path, run, *, force: bool) -> Path:
        self.progress.stage("deviceatlas", "reuse existing artifact" if should_reuse_artifact(run["enriched_path"], force=force) else "enrich UAs with DeviceAtlas")
        if should_reuse_artifact(run["enriched_path"], force=force):
            enriched_path = Path(run["enriched_path"])
            self.logger.info("Run %s reusing DeviceAtlas file: %s", run_id, enriched_path)
        else:
            enriched_path = enrich_with_deviceatlas(self.cfg, self.db, cleaned_path, progress=self.progress)
            self.logger.info("Run %s enriched file: %s", run_id, enriched_path)
        self.db.update_run(run_id, enriched_path=str(enriched_path), state=STAGE_STATE["deviceatlas"], last_stage="deviceatlas")
        self.progress.done("deviceatlas", str(enriched_path))
        return enriched_path

    def _sync_spm_kb(self, *, force: bool = False) -> None:
        export_cfg = self.cfg.get("spm_export", {})
        if not export_cfg.get("enabled", True) or not export_cfg.get("auto_sync", True):
            return
        self.progress.stage("spm-kb", "check latest approved SPM export and update local KB only if export_id changed")
        try:
            kb_path = ensure_spm_knowledge_base(self.cfg, force=force, logger=self.logger)
            self.progress.done("spm-kb", str(kb_path) if kb_path else "not available")
        except Exception as exc:
            if export_cfg.get("required", False):
                raise
            self.logger.warning("SPM KB sync failed; continuing without local KB pre-check: %s", exc)
            self.progress.done("spm-kb", f"sync failed; continuing without KB: {exc}")

    def _stage_spm(self, run_id: int, enriched_path: Path, run, *, force: bool) -> Path:
        self.progress.stage("spm", "reuse existing artifact" if should_reuse_artifact(run["spm_path"], force=force) else "check Z-Intel/SPM coverage")
        if should_reuse_artifact(run["spm_path"], force=force):
            spm_path = Path(run["spm_path"])
            self.logger.info("Run %s reusing SPM file: %s", run_id, spm_path)
        elif self.cfg.get("spm", {}).get("enabled", True):
            spm_path = run_spm_check(self.cfg, self.db, enriched_path, progress=self.progress)
            self.logger.info("Run %s SPM file: %s", run_id, spm_path)
        else:
            spm_path = enriched_path
            self.logger.info("Run %s SPM disabled; report will use enriched file", run_id)
        self.db.update_run(run_id, spm_path=str(spm_path), state=STAGE_STATE["spm"], last_stage="spm")
        self.progress.done("spm", str(spm_path))
        return spm_path

    def _stage_report(self, run_id: int, spm_path: Path, run, *, force: bool) -> Path:
        existing_focus_path = focus_report_path(self.cfg, spm_path)
        reuse_review = should_reuse_artifact(run["report_path"], force=force)
        reuse_focus = should_reuse_artifact(existing_focus_path, force=force)
        self.progress.stage(
            "report",
            "reuse existing artifacts" if reuse_review and reuse_focus else "build Excel review and focus cluster reports",
        )
        if reuse_review:
            report_path = Path(run["report_path"])
            self.logger.info("Run %s reusing review report: %s", run_id, report_path)
        else:
            report_path = build_review_report(self.cfg, spm_path)
            self.logger.info("Run %s review report: %s", run_id, report_path)
        if reuse_focus:
            focus_path = existing_focus_path
            self.logger.info("Run %s reusing focus report: %s", run_id, focus_path)
        else:
            focus_path = build_focus_report(self.cfg, spm_path)
            self.logger.info("Run %s focus report: %s", run_id, focus_path)
        self.db.update_run(
            run_id,
            report_path=str(report_path),
            report_dir=str(report_path.parent),
            state=STAGE_STATE["report"],
            last_stage="report",
        )
        self.progress.done("report", f"review={report_path}; focus={focus_path}")
        return report_path

    def _stage_upload(self, run_id: int, report_path: Path, run, *, force: bool) -> None:
        self.progress.stage("upload", "reuse existing link" if run["uploaded_report_link"] and not force else "upload if enabled")
        if run["uploaded_report_link"] and not force:
            self.logger.info("Run %s reusing uploaded report link: %s", run_id, run["uploaded_report_link"])
            self.db.update_run(run_id, state=STAGE_STATE["upload"], last_stage="upload")
            self.progress.done("upload", run["uploaded_report_link"])
            return
        upload_cfg = self.cfg.get("report_upload", {})
        focus_path = focus_report_path(self.cfg, run["spm_path"] or report_path)
        focus_upload = None
        try:
            upload = upload_report_if_enabled(self.cfg, report_path)
            if upload and focus_path.is_file():
                focus_upload = upload_report_if_enabled(self.cfg, focus_path, filename=focus_path.name)
        except ReportUploadPermissionError as exc:
            if upload_cfg.get("required", False):
                raise
            self.logger.warning("Run %s final report upload skipped: %s", run_id, exc)
            upload = None
        updates: Dict[str, Any] = {"state": STAGE_STATE["upload"], "last_stage": "upload"}
        if upload:
            updates.update(uploaded_report_file_id=upload.get("file_id"), uploaded_report_link=upload.get("web_view_link"))
            self.logger.info("Run %s uploaded review report: %s", run_id, upload.get("web_view_link"))
        if focus_upload:
            self.logger.info("Run %s uploaded focus report: %s", run_id, focus_upload.get("web_view_link"))
        self.db.update_run(run_id, **updates)
        if upload and focus_upload:
            upload_message = f"review={upload.get('web_view_link')}; focus={focus_upload.get('web_view_link')}"
        elif upload:
            upload_message = upload.get("web_view_link") or "uploaded"
        else:
            upload_message = "upload disabled/not configured"
        self.progress.done("upload", upload_message)

    @staticmethod
    def _validate_stage(stage: str) -> None:
        if stage not in STAGE_ORDER:
            raise ValueError(f"Unsupported stage '{stage}'. Use one of: {', '.join(STAGE_ORDER)}")

    @staticmethod
    def _should_stop(stop_after: Optional[str], current_stage: str) -> bool:
        return stop_after == current_stage

    def _completion_email(self, run_id: int, report_path: Path) -> Tuple[str, str]:
        run = self._run(run_id)
        summary = self._spm_summary(run["spm_path"])
        focus_path = focus_report_path(self.cfg, run["spm_path"] or report_path)

        text_lines = [
            f"Run {run_id} completed successfully.",
            f"Date/query: {run['run_date']} / {run['query_name']}",
            f"IoT candidate devices: {summary['total_devices']} UA groups ({summary['total_hits']} hits)",
            f"Already detected: {summary['detected_devices']} UA groups ({summary['detected_hits']} hits)",
            f"Not present: {summary['not_present_devices']} UA groups ({summary['not_present_hits']} hits)",
            f"Disabled: {summary['disabled_devices']} UA groups ({summary['disabled_hits']} hits)",
            f"SPM errors: {summary['error_devices']} UA groups ({summary['error_hits']} hits)",
            f"Review report: {report_path}",
            f"Focus cluster report: {focus_path}",
        ]
        if run["uploaded_report_link"]:
            text_lines.append(f"Google Drive link: {run['uploaded_report_link']}")
        text_lines.extend(["", "Top not-present devices by hits:"])
        if summary["top_not_present"]:
            for item in summary["top_not_present"]:
                text_lines.append(
                    f"- {item['device']}: {item['hits']} hits, {item['groups']} UA groups, "
                    f"hardware={item['hardware_type'] or 'unknown'}"
                )
        else:
            text_lines.append("- None")
        text_lines.extend(
            [
                "",
                "Restart examples:",
                f"python run.py process {run_id} --from-stage spm",
                f"python run.py process {run_id} --force-stage report",
            ]
        )

        html_body = self._completion_html(run_id, run, report_path, summary, focus_path)
        return "\n".join(text_lines), html_body

    def _spm_summary(self, spm_path: str | Path | None) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        if spm_path and Path(spm_path).is_file():
            with Path(spm_path).open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

        status_counts: Counter[str] = Counter()
        status_hits: defaultdict[str, int] = defaultdict(int)
        not_present: Dict[str, Dict[str, Any]] = {}
        total_hits = 0

        for row in rows:
            status = row.get("spm_detection_status") or "unknown"
            hits = self._to_int(row.get("total_group_hits") or row.get("hit_count"))
            total_hits += hits
            status_counts[status] += 1
            status_hits[status] += hits
            if status == "not-present":
                device = self._device_label(row)
                entry = not_present.setdefault(
                    device,
                    {
                        "device": device,
                        "hits": 0,
                        "groups": 0,
                        "hardware_type": row.get("hardware_type", ""),
                        "vendor": row.get("device_vendor", ""),
                        "model": row.get("device_model", ""),
                        "marketing_name": row.get("marketing_name", ""),
                        "sample_ua": row.get("user_agent", ""),
                    },
                )
                entry["hits"] += hits
                entry["groups"] += 1
                if hits > self._to_int(entry.get("sample_hits")):
                    entry["sample_hits"] = hits
                    entry["sample_ua"] = row.get("user_agent", "")

        detected_statuses = {"detected-released", "detected-reviewed"}
        disabled_statuses = {"detected-disabled"}
        error_statuses = {"spm-error"}
        not_present_items = sorted(not_present.values(), key=lambda item: int(item.get("hits") or 0), reverse=True)
        return {
            "total_devices": len(rows),
            "total_hits": total_hits,
            "detected_devices": sum(status_counts[status] for status in detected_statuses),
            "detected_hits": sum(status_hits[status] for status in detected_statuses),
            "not_present_devices": status_counts["not-present"],
            "not_present_hits": status_hits["not-present"],
            "disabled_devices": sum(status_counts[status] for status in disabled_statuses),
            "disabled_hits": sum(status_hits[status] for status in disabled_statuses),
            "error_devices": sum(status_counts[status] for status in error_statuses),
            "error_hits": sum(status_hits[status] for status in error_statuses),
            "status_counts": dict(status_counts),
            "status_hits": dict(status_hits),
            "top_not_present": not_present_items[:25],
            "all_not_present_count": len(not_present_items),
        }

    def _completion_html(self, run_id: int, run: Any, report_path: Path, summary: Dict[str, Any], focus_path: Path) -> str:
        total_devices = int(summary["total_devices"] or 0)
        total_hits = int(summary["total_hits"] or 0)
        detected_pct = self._pct(summary["detected_devices"], total_devices)
        not_present_pct = self._pct(summary["not_present_devices"], total_devices)
        top_rows = "".join(self._not_present_html_row(item) for item in summary["top_not_present"])
        if not top_rows:
            top_rows = '<tr><td colspan="5" style="padding:14px;color:#667085;text-align:center;">No not-present IoT devices found 🎉</td></tr>'

        drive_link = ""
        if run["uploaded_report_link"]:
            drive_link = (
                f'<a href="{escape(str(run["uploaded_report_link"]))}" '
                'style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;'
                'padding:10px 14px;border-radius:8px;font-weight:600;margin-right:8px;">Open Google Drive Report</a>'
            )
        report_path_text = escape(str(report_path))
        focus_path_text = escape(str(focus_path))
        spm_path_text = escape(str(run["spm_path"] or ""))

        return f"""
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f4f7fb;font-family:Arial,Helvetica,sans-serif;color:#1f2937;">
    <div style="max-width:980px;margin:0 auto;padding:24px;">
      <div style="background:linear-gradient(135deg,#0f766e,#2563eb);border-radius:16px 16px 0 0;padding:28px;color:#ffffff;">
        <div style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;opacity:.85;">iotSPM Pipeline Completed</div>
        <h1 style="margin:8px 0 0;font-size:28px;line-height:1.25;">Run #{run_id} finished successfully</h1>
        <p style="margin:8px 0 0;opacity:.92;">{escape(str(run['run_date']))} · {escape(str(run['query_name']))}</p>
      </div>

      <div style="background:#ffffff;border:1px solid #e5e7eb;border-top:0;border-radius:0 0 16px 16px;padding:24px;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin-bottom:22px;">
          <tr>
            {self._metric_card('IoT devices found', summary['total_devices'], f'{total_hits} hits', '#0f766e')}
            {self._metric_card('Already detected', summary['detected_devices'], f'{summary["detected_hits"]} hits · {detected_pct}', '#16a34a')}
            {self._metric_card('Not present', summary['not_present_devices'], f'{summary["not_present_hits"]} hits · {not_present_pct}', '#dc2626')}
            {self._metric_card('SPM errors', summary['error_devices'], f'{summary["error_hits"]} hits', '#ea580c')}
          </tr>
        </table>

        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:16px;margin-bottom:22px;">
          <h2 style="font-size:18px;margin:0 0 10px;color:#111827;">Coverage summary</h2>
          <div style="height:14px;background:#e5e7eb;border-radius:999px;overflow:hidden;margin-bottom:10px;">
            <div style="height:14px;width:{detected_pct};background:#22c55e;float:left;"></div>
            <div style="height:14px;width:{not_present_pct};background:#ef4444;float:left;"></div>
          </div>
          <p style="margin:0;color:#475569;font-size:14px;">Green = already detected/reviewed/released. Red = not-present candidates that likely need signature review.</p>
          <p style="margin:10px 0 0;color:#475569;font-size:14px;">The attached focus cluster report groups related UA variants by DeviceAtlas model/family and ranks them by hits so you can cover the largest traffic families first.</p>
        </div>

        <h2 style="font-size:18px;margin:0 0 12px;color:#111827;">Top not-present IoT devices by Zscaler hits</h2>
        <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;margin-bottom:22px;">
          <thead>
            <tr style="background:#f1f5f9;color:#334155;font-size:13px;text-align:left;">
              <th style="padding:10px;border-bottom:1px solid #e5e7eb;">Device</th>
              <th style="padding:10px;border-bottom:1px solid #e5e7eb;">Hardware</th>
              <th style="padding:10px;border-bottom:1px solid #e5e7eb;text-align:right;">Hits</th>
              <th style="padding:10px;border-bottom:1px solid #e5e7eb;text-align:right;">UA groups</th>
              <th style="padding:10px;border-bottom:1px solid #e5e7eb;">Sample UA</th>
            </tr>
          </thead>
          <tbody>{top_rows}</tbody>
        </table>

        <div style="margin-bottom:22px;">
          {drive_link}
          <span style="display:inline-block;background:#eef2ff;color:#3730a3;padding:10px 14px;border-radius:8px;font-weight:600;">Local report generated</span>
        </div>

        <div style="font-size:13px;color:#475569;line-height:1.55;background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:14px;">
          <div><strong>Review report:</strong> <code>{report_path_text}</code></div>
          <div><strong>Attached focus cluster report:</strong> <code>{focus_path_text}</code></div>
          <div><strong>SPM CSV:</strong> <code>{spm_path_text}</code></div>
          <div style="margin-top:10px;"><strong>Restart examples:</strong></div>
          <code>python run.py process {run_id} --from-stage spm</code><br>
          <code>python run.py process {run_id} --force-stage report</code>
        </div>
      </div>
    </div>
  </body>
</html>
"""

    @staticmethod
    def _metric_card(title: str, value: Any, subtitle: str, color: str) -> str:
        return (
            '<td style="width:25%;padding:6px;vertical-align:top;">'
            '<div style="border:1px solid #e5e7eb;border-radius:12px;padding:14px;background:#ffffff;">'
            f'<div style="font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:.04em;">{escape(title)}</div>'
            f'<div style="font-size:28px;font-weight:700;color:{color};margin-top:6px;">{escape(str(value))}</div>'
            f'<div style="font-size:13px;color:#475569;margin-top:4px;">{escape(subtitle)}</div>'
            '</div>'
            '</td>'
        )

    @staticmethod
    def _not_present_html_row(item: Dict[str, Any]) -> str:
        sample_ua = str(item.get("sample_ua") or "")
        if len(sample_ua) > 180:
            sample_ua = sample_ua[:177] + "..."
        return (
            '<tr style="font-size:13px;color:#1f2937;">'
            f'<td style="padding:10px;border-bottom:1px solid #f1f5f9;font-weight:600;">{escape(str(item.get("device") or "Unknown"))}</td>'
            f'<td style="padding:10px;border-bottom:1px solid #f1f5f9;color:#475569;">{escape(str(item.get("hardware_type") or "unknown"))}</td>'
            f'<td style="padding:10px;border-bottom:1px solid #f1f5f9;text-align:right;font-weight:700;color:#dc2626;">{escape(str(item.get("hits") or 0))}</td>'
            f'<td style="padding:10px;border-bottom:1px solid #f1f5f9;text-align:right;">{escape(str(item.get("groups") or 0))}</td>'
            f'<td style="padding:10px;border-bottom:1px solid #f1f5f9;color:#64748b;font-family:Consolas,Monaco,monospace;">{escape(sample_ua)}</td>'
            '</tr>'
        )

    @staticmethod
    def _device_label(row: Dict[str, Any]) -> str:
        vendor = str(row.get("device_vendor") or "").strip()
        model = str(row.get("device_model") or "").strip()
        marketing = str(row.get("marketing_name") or "").strip()
        hardware = str(row.get("hardware_type") or "").strip()
        parts = [part for part in [vendor, model] if part]
        label = " ".join(parts) or marketing or hardware or "Unknown device"
        if marketing and marketing.lower() not in label.lower():
            label = f"{label} ({marketing})"
        return label

    @staticmethod
    def _pct(value: Any, total: Any) -> str:
        total_int = PipelineOrchestrator._to_int(total)
        if total_int <= 0:
            return "0%"
        return f"{(PipelineOrchestrator._to_int(value) / total_int) * 100:.1f}%"

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except Exception:
            return 0

    def _failure_body(self, run_id: int, exc: Exception) -> str:
        run = self._run(run_id)
        last_stage = run["last_stage"] or "unknown"
        next_stage = self._next_stage(last_stage)
        lines = [
            f"Run {run_id} failed.",
            f"Last completed stage: {last_stage}",
            f"Current state: {run['state']}",
            f"Error: {exc}",
        ]
        if next_stage:
            lines.extend(["", "Restart command:", f"python run.py process {run_id} --from-stage {next_stage}"])
        else:
            lines.extend(["", "Restart command:", f"python run.py process {run_id} --force-stage all"])
        return "\n".join(lines)

    @staticmethod
    def _next_stage(last_stage: str) -> Optional[str]:
        if last_stage not in STAGE_ORDER:
            return STAGE_ORDER[0]
        idx = STAGE_ORDER.index(last_stage) + 1
        return STAGE_ORDER[idx] if idx < len(STAGE_ORDER) else None
