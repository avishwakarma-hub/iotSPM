from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from pipeline.artifacts import path_exists, should_reuse_artifact
from pipeline.stage1_rundeck import RundeckClient, day_window
from pipeline.stage2_download import download_drive_file
from pipeline.stage3_convert import convert_to_csv_if_needed
from pipeline.stage4_filter import filter_and_dedupe
from pipeline.stage5_deviceatlas import enrich_with_deviceatlas
from pipeline.stage6_spm import run_spm_check
from pipeline.stage7_report import build_review_report
from pipeline.stage8_upload import upload_report_if_enabled
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
            self.notifier.send("iotSPM pipeline completed", self._completion_body(run_id, report_path))
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
        self.progress.stage("report", "reuse existing artifact" if should_reuse_artifact(run["report_path"], force=force) else "build Excel review report")
        if should_reuse_artifact(run["report_path"], force=force):
            report_path = Path(run["report_path"])
            self.logger.info("Run %s reusing review report: %s", run_id, report_path)
        else:
            report_path = build_review_report(self.cfg, spm_path)
            self.logger.info("Run %s review report: %s", run_id, report_path)
        self.db.update_run(
            run_id,
            report_path=str(report_path),
            report_dir=str(report_path.parent),
            state=STAGE_STATE["report"],
            last_stage="report",
        )
        self.progress.done("report", str(report_path))
        return report_path

    def _stage_upload(self, run_id: int, report_path: Path, run, *, force: bool) -> None:
        self.progress.stage("upload", "reuse existing link" if run["uploaded_report_link"] and not force else "upload if enabled")
        if run["uploaded_report_link"] and not force:
            self.logger.info("Run %s reusing uploaded report link: %s", run_id, run["uploaded_report_link"])
            self.db.update_run(run_id, state=STAGE_STATE["upload"], last_stage="upload")
            self.progress.done("upload", run["uploaded_report_link"])
            return
        upload = upload_report_if_enabled(self.cfg, report_path)
        updates: Dict[str, Any] = {"state": STAGE_STATE["upload"], "last_stage": "upload"}
        if upload:
            updates.update(uploaded_report_file_id=upload.get("file_id"), uploaded_report_link=upload.get("web_view_link"))
            self.logger.info("Run %s uploaded review report: %s", run_id, upload.get("web_view_link"))
        self.db.update_run(run_id, **updates)
        self.progress.done("upload", upload.get("web_view_link") if upload else "upload disabled/not configured")

    @staticmethod
    def _validate_stage(stage: str) -> None:
        if stage not in STAGE_ORDER:
            raise ValueError(f"Unsupported stage '{stage}'. Use one of: {', '.join(STAGE_ORDER)}")

    @staticmethod
    def _should_stop(stop_after: Optional[str], current_stage: str) -> bool:
        return stop_after == current_stage

    def _completion_body(self, run_id: int, report_path: Path) -> str:
        run = self._run(run_id)
        lines = [
            f"Run {run_id} completed successfully.",
            f"Review report: {report_path}",
        ]
        if run["uploaded_report_link"]:
            lines.append(f"Google Drive link: {run['uploaded_report_link']}")
        lines.extend(
            [
                "",
                "Restart examples:",
                f"python run.py process {run_id} --from-stage spm",
                f"python run.py process {run_id} --force-stage report",
            ]
        )
        return "\n".join(lines)

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
