from __future__ import annotations

import argparse
from pathlib import Path

from orchestrator import PipelineOrchestrator
from pipeline.stage2_download import download_drive_file
from utils.config import ensure_directories, load_config
from utils.db import Database
from utils.google_auth import get_drive_service
from utils.logger import setup_logging


def build_app(config_path: str):
    cfg = load_config(config_path)
    ensure_directories(cfg)
    logger = setup_logging(cfg)
    db = Database(cfg["paths"]["db_path"])
    return cfg, logger, PipelineOrchestrator(cfg, db, logger)


def main() -> None:
    parser = argparse.ArgumentParser(description="iotSPM Zscaler UA → DeviceAtlas → SPM review pipeline")
    parser.add_argument("--config", default="config/settings.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("auth-drive", help="Perform one-time Google Drive OAuth auth")

    p = sub.add_parser("submit", help="Submit a Rundeck query for one day")
    p.add_argument("--day", required=True, help="YYYY-MM-DD")
    p.add_argument("--query", default="build_only", help="Query name from config/settings.yaml")

    p = sub.add_parser("poll", help="Poll Rundeck status for a run")
    p.add_argument("run_id", type=int)

    p = sub.add_parser("poll-active", help="Poll all active Rundeck runs; useful from cron")
    p.add_argument("--auto-process", action="store_true", help="Process succeeded runs automatically")

    p = sub.add_parser("process", help="Download/process a completed run")
    p.add_argument("run_id", type=int)

    p = sub.add_parser("run-local", help="Process an existing .current or .csv file")
    p.add_argument("path")
    p.add_argument("--day", default="manual")

    p = sub.add_parser("download-drive", help="Download a Google Drive file by id")
    p.add_argument("file_id")

    p = sub.add_parser("status", help="Show latest runs")
    p.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    cfg, logger, app = build_app(args.config)

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
    elif args.cmd == "process":
        print(app.process_ready_run(args.run_id))
    elif args.cmd == "run-local":
        print(app.run_local_file(Path(args.path), day=args.day))
    elif args.cmd == "download-drive":
        print(download_drive_file(cfg, args.file_id))
    elif args.cmd == "status":
        app.status(args.limit)


if __name__ == "__main__":
    main()
