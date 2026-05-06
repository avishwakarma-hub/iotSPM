from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from pipeline.stage1_rundeck import RundeckClient, day_window
from pipeline.stage2_download import download_drive_file
from pipeline.stage3_convert import convert_to_csv_if_needed
from pipeline.stage4_filter import filter_and_dedupe
from pipeline.stage5_deviceatlas import enrich_with_deviceatlas
from pipeline.stage6_spm import run_spm_check
from pipeline.stage7_report import build_review_report
from utils.db import Database
from utils.notifier import Notifier


class PipelineOrchestrator:
    def __init__(self, cfg: Dict[str, Any], db: Database, logger):
        self.cfg = cfg
        self.db = db
        self.logger = logger
        self.notifier = Notifier(cfg, logger)

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

    def process_ready_run(self, run_id: int) -> Path:
        run = self._run(run_id)
        if not run["drive_file_id"] and not run["raw_path"]:
            raise RuntimeError("Run does not have a Drive file or raw path yet. Poll until Rundeck succeeds.")

        raw_path = Path(run["raw_path"]) if run["raw_path"] else download_drive_file(self.cfg, run["drive_file_id"])
        self.db.update_run(run_id, raw_path=str(raw_path), state="DOWNLOADED")
        self.logger.info("Run %s downloaded/raw file: %s", run_id, raw_path)

        csv_path = convert_to_csv_if_needed(self.cfg, raw_path)
        self.db.update_run(run_id, csv_path=str(csv_path), state="CONVERTED")
        self.logger.info("Run %s CSV file: %s", run_id, csv_path)

        cleaned_path, filter_stats = filter_and_dedupe(self.cfg, csv_path)
        self.db.update_run(run_id, cleaned_path=str(cleaned_path), state="FILTERED", stats_json=filter_stats)
        self.logger.info("Run %s cleaned file: %s stats=%s", run_id, cleaned_path, filter_stats)

        enriched_path = enrich_with_deviceatlas(self.cfg, self.db, cleaned_path)
        self.db.update_run(run_id, enriched_path=str(enriched_path), state="DEVICEATLAS_ENRICHED")
        self.logger.info("Run %s enriched file: %s", run_id, enriched_path)

        if self.cfg.get("spm", {}).get("enabled", True):
            spm_path = run_spm_check(self.cfg, self.db, enriched_path)
        else:
            spm_path = enriched_path
        report_path = build_review_report(self.cfg, spm_path)
        self.db.update_run(run_id, report_dir=str(report_path.parent), status="completed", state="COMPLETED")
        self.notifier.send("iotSPM pipeline completed", f"Run {run_id} completed. Review report:\n{report_path}")
        return report_path

    def run_local_file(self, path: str | Path, day: str = "manual", query_name: str = "local_file") -> Path:
        run_id = self.db.create_run(day, query_name, "local-file", "", "")
        self.db.update_run(run_id, raw_path=str(path), status="local", state="LOCAL_FILE")
        return self.process_ready_run(run_id)

    def status(self, limit: int = 20) -> None:
        for run in self.db.list_runs(limit):
            print(
                f"#{run['id']} {run['run_date']} {run['query_name']} state={run['state']} "
                f"status={run['status']} exec={run['execution_id']} report={run['report_dir']}"
            )

    def _run(self, run_id: int):
        run = self.db.get_run(run_id)
        if not run:
            raise RuntimeError(f"Run not found: {run_id}")
        return run
