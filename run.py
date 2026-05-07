from __future__ import annotations

import argparse
from pathlib import Path

from orchestrator import PipelineOrchestrator
from pipeline.stage2_download import download_drive_file
from scheduler import ProgressiveScheduler
from utils.config import ensure_directories, load_config
from utils.db import Database
from utils.google_auth import get_drive_service
from utils.logger import setup_logging
from utils.progress import progress_from_config


STATE_REFERENCE = """\
State reference / where to restart:
  FILTERED              -> next stage is deviceatlas
  DEVICEATLAS_ENRICHED  -> next stage is spm
  SPM_CHECKED           -> next stage is report
  REPORTED              -> next stage is upload/completed
  COMPLETED             -> done
  RUNDECK_FAILED        -> Rundeck failed, usually cannot process
  FAILED                -> failed somewhere, check error/logs
"""


def build_app(config_path: str):
    cfg = load_config(config_path)
    ensure_directories(cfg)
    logger = setup_logging(cfg)
    db = Database(cfg["paths"]["db_path"])
    return cfg, logger, db, PipelineOrchestrator(cfg, db, logger)


def build_scheduler(cfg, logger, db, app) -> ProgressiveScheduler:
    return ProgressiveScheduler(cfg, db, app, logger)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="iotSPM Zscaler UA -> DeviceAtlas -> SPM review pipeline",
        epilog=STATE_REFERENCE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config/settings.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_verbose_flag(subparser):
        subparser.add_argument(
            "--verbose",
            action="store_true",
            help="Show stage progress and throttled progress bars for long-running stages",
        )

    sub.add_parser("auth-drive", help="Perform one-time Google Drive OAuth auth")

    p = sub.add_parser("submit", help="Submit a Rundeck query for one day")
    p.add_argument("--day", required=True, help="YYYY-MM-DD")
    p.add_argument("--query", default="build_only", help="Query name from config/settings.yaml")

    p = sub.add_parser("poll", help="Poll one Rundeck run")
    p.add_argument("run_id", type=int)

    p = sub.add_parser("poll-active", help="Poll all active Rundeck runs; useful from cron")
    p.add_argument("--auto-process", action="store_true", help="Process succeeded runs automatically")
    add_verbose_flag(p)

    p = sub.add_parser("scheduler-set-base", help="Initialize/update the progressive daily scheduler base date")
    p.add_argument("--date", required=True, help="First day to process, YYYY-MM-DD")
    p.add_argument("--query", help="Query name from config/settings.yaml; defaults to scheduler.query_name")
    p.add_argument("--disabled", action="store_true", help="Create/update scheduler state as disabled")

    p = sub.add_parser("scheduler-tick", help="Run one cron-safe scheduler tick: retry, poll/process active, then submit next day")
    p.add_argument("--dry-run", action="store_true", help="Show the next scheduler action without polling, processing, or submitting")
    add_verbose_flag(p)

    sub.add_parser("scheduler-status", help="Show scheduler cursor, next pending date, active runs, and queued retries")

    p = sub.add_parser("retry-add", help="Queue a failed/interrupted run for scheduler-managed processing retry")
    p.add_argument("run_id", type=int)
    p.add_argument("--from-stage", choices=["download", "convert", "filter", "deviceatlas", "spm", "report", "upload"], help="Stage to rebuild from; inferred from last_stage when omitted")
    p.add_argument("--note", help="Optional operator note")

    sub.add_parser("retry-list", help="List scheduler retry queue")

    process = sub.add_parser(
        "process",
        help="Process or restart a completed run",
        epilog=STATE_REFERENCE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    process.add_argument("run_id", type=int)
    process.add_argument("--from-stage", choices=["download", "convert", "filter", "deviceatlas", "spm", "report", "upload"], help="Rebuild this stage and every later stage; reuse earlier artifacts")
    process.add_argument("--force-stage", action="append", default=[], choices=["download", "convert", "filter", "deviceatlas", "spm", "report", "upload", "all"], help="Rebuild a specific stage even when its artifact exists. Can be repeated.")
    process.add_argument("--stop-after", choices=["download", "convert", "filter", "deviceatlas", "spm", "report", "upload"], help="Stop after a stage for debugging/review")
    add_verbose_flag(process)

    local = sub.add_parser("run-local", help="Process an existing .current or .csv file")
    local.add_argument("path")
    local.add_argument("--day", default="manual")
    local.add_argument("--stop-after", choices=["download", "convert", "filter", "deviceatlas", "spm", "report", "upload"], help="Stop after a stage for debugging/review")
    add_verbose_flag(local)

    p = sub.add_parser("download-drive", help="Download a Google Drive file by id")
    p.add_argument("file_id")

    p = sub.add_parser(
        "status",
        help="Show latest runs",
        epilog=STATE_REFERENCE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    cfg, logger, db, app = build_app(args.config)
    if getattr(args, "verbose", False):
        cfg.setdefault("runtime", {}).setdefault("progress", {})["enabled"] = True
        app.set_progress(progress_from_config(cfg))

    if args.cmd == "auth-drive":
        service = get_drive_service(cfg)
        about = service.about().get(fields="user(emailAddress,displayName)").execute()
        user = about.get("user", {})
        identity = user.get("emailAddress") or user.get("displayName") or "unknown account"
        print(f"Google Drive authentication verified for: {identity}")
        print(f"Token saved at: {cfg['paths']['google_token_file']}")
    elif args.cmd == "submit":
        print(app.submit(args.day, args.query))
    elif args.cmd == "poll":
        print(app.poll(args.run_id))
    elif args.cmd == "poll-active":
        app.poll_active(auto_process=args.auto_process)
    elif args.cmd == "scheduler-set-base":
        scheduler = build_scheduler(cfg, logger, db, app)
        scheduler.set_base(args.date, query_name=args.query, enabled=not args.disabled)
        print(f"Scheduler base set to {args.date} query={args.query or scheduler.query_name} enabled={not args.disabled}")
    elif args.cmd == "scheduler-tick":
        scheduler = build_scheduler(cfg, logger, db, app)
        decision = scheduler.tick(dry_run=args.dry_run)
        logger.info("Scheduler decision: %s", decision)
    elif args.cmd == "scheduler-status":
        scheduler = build_scheduler(cfg, logger, db, app)
        scheduler.status()
    elif args.cmd == "retry-add":
        db.add_retry(args.run_id, requested_stage=args.from_stage, note=args.note)
        print(f"Queued run {args.run_id} for retry from {args.from_stage or 'auto'}")
    elif args.cmd == "retry-list":
        retries = db.list_retries()
        if not retries:
            print("Retry queue is empty.")
        for item in retries:
            print(
                f"#{item['id']} run_id={item['run_id']} status={item['status']} "
                f"stage={item['requested_stage']} attempts={item['attempts']} error={item['last_error']}"
            )
    elif args.cmd == "process":
        print(
            app.process_ready_run(
                args.run_id,
                from_stage=args.from_stage,
                force_stages=args.force_stage,
                stop_after=args.stop_after,
            )
        )
    elif args.cmd == "run-local":
        if args.stop_after:
            run_id = db.create_run(args.day, "local_file", "local-file", "", "")
            db.update_run(run_id, raw_path=str(Path(args.path)), status="local", state="LOCAL_FILE")
            print(app.process_ready_run(run_id, stop_after=args.stop_after))
        else:
            print(app.run_local_file(Path(args.path), day=args.day))
    elif args.cmd == "download-drive":
        print(download_drive_file(cfg, args.file_id))
    elif args.cmd == "status":
        app.status(args.limit)


if __name__ == "__main__":
    main()
