from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from orchestrator import PipelineOrchestrator, STAGE_ORDER
from utils.db import Database
from utils.notifier import Notifier


DATE_FORMAT_OUT = "%Y-%m-%d %H:%M"


@dataclass
class SchedulerDecision:
    action: str
    message: str
    run_id: Optional[int] = None
    run_date: Optional[str] = None


class SchedulerLockError(RuntimeError):
    pass


class ProgressiveScheduler:
    def __init__(self, cfg: Dict[str, Any], db: Database, app: PipelineOrchestrator, logger):
        self.cfg = cfg
        self.scheduler_cfg = cfg.get("scheduler", {})
        self.db = db
        self.app = app
        self.logger = logger
        self.notifier = Notifier(cfg, logger)

    @property
    def name(self) -> str:
        return str(self.scheduler_cfg.get("name") or "daily_build_only")

    @property
    def query_name(self) -> str:
        return str(self.scheduler_cfg.get("query_name") or "build_only")

    def set_base(self, base_date: str, query_name: Optional[str] = None, enabled: bool = True) -> None:
        self._parse_date(base_date)
        self.db.upsert_scheduler_state(self.name, query_name or self.query_name, base_date, enabled=enabled)

    def status(self) -> None:
        state = self._ensure_state(create_if_configured=False)
        if not state:
            print("Scheduler state is not initialized. Run scheduler-set-base first or set scheduler.base_date.")
            return
        next_date = self._find_next_pending_date(state["base_date"], state["query_name"])
        latest = self._latest_eligible_date()
        print(
            f"scheduler={state['name']} enabled={bool(state['enabled'])} query={state['query_name']} "
            f"base_date={state['base_date']} latest_eligible={latest} "
            f"last_submitted={state['last_submitted_date']} last_completed={state['last_completed_date']} "
            f"next_pending={next_date} active_runs={self.db.count_active_runs()}"
        )
        queued = self.db.list_retries(status="queued")
        if queued:
            print("queued retries:")
            for item in queued:
                print(f"  run_id={item['run_id']} requested_stage={item['requested_stage']} attempts={item['attempts']} note={item['note']}")

    def tick(self, dry_run: bool = False) -> SchedulerDecision:
        with self._lock(dry_run=dry_run):
            return self._tick_unlocked(dry_run=dry_run)

    def _tick_unlocked(self, dry_run: bool = False) -> SchedulerDecision:
        state = self._ensure_state(create_if_configured=True)
        if not state:
            return self._decision("not_configured", "Scheduler base_date is not set. Run scheduler-set-base --date YYYY-MM-DD.")
        if not bool(state["enabled"]) or not bool(self.scheduler_cfg.get("enabled", False)):
            return self._decision("disabled", "Scheduler is disabled in DB or config.")

        if bool(self.scheduler_cfg.get("process_retries_first", True)):
            retry_decision = self._process_retry_queue(dry_run=dry_run)
            if retry_decision:
                return retry_decision

        active_count = self.db.count_active_runs()
        if dry_run:
            if active_count:
                return self._decision("would_poll", f"Would poll/process {active_count} active Rundeck run(s).")
        else:
            self.app.poll_active(auto_process=bool(self.scheduler_cfg.get("auto_process_succeeded", True)))
            self._refresh_completed_cursor(state["name"], state["base_date"], state["query_name"])
            active_count = self.db.count_active_runs()

        max_active = int(self.scheduler_cfg.get("max_active_rundeck_runs", 1))
        if active_count >= max_active:
            return self._decision("waiting_active", f"Active Rundeck run limit reached: {active_count}/{max_active}.")

        next_date = self._find_next_pending_date(state["base_date"], state["query_name"])
        if not next_date:
            return self._decision("caught_up", "No pending eligible dates to submit.")

        if dry_run:
            return self._decision("would_submit", f"Would submit {state['query_name']} for {next_date}.", run_date=next_date)

        run_id = self.app.submit(next_date, state["query_name"])
        self.db.update_scheduler_state(state["name"], last_submitted_date=next_date)
        return self._decision("submitted", f"Submitted {state['query_name']} for {next_date} as run {run_id}.", run_id=run_id, run_date=next_date)

    def _process_retry_queue(self, dry_run: bool = False) -> Optional[SchedulerDecision]:
        queued = self.db.list_retries(status="queued")[: int(self.scheduler_cfg.get("max_retries_per_tick", 1))]
        if not queued:
            return None

        item = queued[0]
        run_id = int(item["run_id"])
        requested_stage = item["requested_stage"]
        from_stage = requested_stage or self._infer_restart_stage(run_id)
        if dry_run:
            return self._decision("would_retry", f"Would retry run {run_id} from stage {from_stage}.", run_id=run_id)

        try:
            self.db.update_retry(run_id, status="processing", attempts=int(item["attempts"] or 0) + 1, last_error=None)
            report_path = self.app.process_ready_run(run_id, from_stage=from_stage)
            self.db.remove_retry(run_id)
            return self._decision("retry_completed", f"Retried run {run_id} from {from_stage}; report={report_path}", run_id=run_id)
        except Exception as exc:
            self.db.update_retry(run_id, status="failed", last_error=str(exc))
            self.notifier.send("iotSPM retry failed", f"Run {run_id} retry failed from stage {from_stage}:\n{exc}")
            raise

    def _infer_restart_stage(self, run_id: int) -> str:
        run = self.db.get_run(run_id)
        if not run:
            raise RuntimeError(f"Run not found: {run_id}")
        last_stage = run["last_stage"] or ""
        if last_stage in STAGE_ORDER:
            idx = STAGE_ORDER.index(last_stage) + 1
            return STAGE_ORDER[idx] if idx < len(STAGE_ORDER) else "report"
        if run["drive_file_id"] or run["raw_path"]:
            return "download"
        raise RuntimeError(f"Run {run_id} has no Drive/raw artifact and cannot be retried by the processor")

    def _ensure_state(self, create_if_configured: bool):
        state = self.db.get_scheduler_state(self.name)
        if state:
            return state
        base_date = str(self.scheduler_cfg.get("base_date") or "").strip()
        if not create_if_configured or not base_date:
            return None
        self.set_base(base_date, self.query_name, enabled=bool(self.scheduler_cfg.get("enabled", False)))
        return self.db.get_scheduler_state(self.name)

    def _find_next_pending_date(self, base_date: str, query_name: str) -> Optional[str]:
        current = self._parse_date(base_date)
        latest = self._parse_date(self._latest_eligible_date())
        interval_days = float(self.scheduler_cfg.get("interval_days", 1.0))
        if current > latest:
            return None
        while current <= latest:
            day = current.strftime(DATE_FORMAT_OUT) if current.hour or current.minute else current.strftime("%Y-%m-%d")
            if self._date_needs_run(day, query_name):
                return day
            current += timedelta(days=interval_days)
        return None

    def _date_needs_run(self, run_date: str, query_name: str) -> bool:
        runs = self.db.find_runs_for_date(run_date, query_name)
        if not runs:
            return True
        for run in runs:
            state = str(run["state"] or "")
            if state == "COMPLETED":
                return False
            if state not in {"FAILED", "RUNDECK_FAILED", "RUNDECK_ABORTED", "RUNDECK_TIMEDOUT"}:
                return False
        return False

    def _refresh_completed_cursor(self, scheduler_name: str, base_date: str, query_name: str) -> None:
        current = self._parse_date(base_date)
        latest = self._parse_date(self._latest_eligible_date())
        interval_days = float(self.scheduler_cfg.get("interval_days", 1.0))
        last_completed = None
        while current <= latest:
            day = current.strftime(DATE_FORMAT_OUT) if current.hour or current.minute else current.strftime("%Y-%m-%d")
            runs = self.db.find_runs_for_date(day, query_name)
            if any(str(run["state"] or "") == "COMPLETED" for run in runs):
                last_completed = day
                current += timedelta(days=interval_days)
                continue
            break
        if last_completed:
            self.db.update_scheduler_state(scheduler_name, last_completed_date=last_completed)

    def _latest_eligible_date(self) -> str:
        lag = float(self.scheduler_cfg.get("date_lag_days", 1.0))
        dt = datetime.now() - timedelta(days=lag)
        return dt.strftime(DATE_FORMAT_OUT) if dt.hour or dt.minute else dt.strftime("%Y-%m-%d")

    @staticmethod
    def _parse_date(value: str) -> datetime:
        value = value.strip()
        if len(value) <= 10:
            return datetime.strptime(value, "%Y-%m-%d")
        return datetime.strptime(value, DATE_FORMAT_OUT)

    @contextmanager
    def _lock(self, dry_run: bool = False) -> Iterator[None]:
        lock_path = Path(self.cfg.get("paths", {}).get("logs_dir", ".")) / f"scheduler-{self.name}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.exists():
            timeout = timedelta(minutes=int(self.scheduler_cfg.get("lock_timeout_minutes", 180)))
            age = datetime.now() - datetime.fromtimestamp(lock_path.stat().st_mtime)
            if age < timeout:
                raise SchedulerLockError(f"Scheduler lock exists: {lock_path} age={age}")
            self.logger.warning("Removing stale scheduler lock %s age=%s", lock_path, age)
            if not dry_run:
                lock_path.unlink(missing_ok=True)
        if not dry_run:
            lock_path.write_text(f"pid={os.getpid()} created={datetime.now().isoformat(timespec='seconds')}\n", encoding="utf-8")
        try:
            yield
        finally:
            if not dry_run:
                lock_path.unlink(missing_ok=True)

    @staticmethod
    def _decision(action: str, message: str, run_id: Optional[int] = None, run_date: Optional[str] = None) -> SchedulerDecision:
        print(message)
        return SchedulerDecision(action=action, message=message, run_id=run_id, run_date=run_date)
