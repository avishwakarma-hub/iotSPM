from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL,
                    query_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state TEXT NOT NULL,
                    query TEXT,
                    start_time TEXT,
                    end_time TEXT,
                    execution_id TEXT,
                    drive_file_id TEXT,
                    drive_file_name TEXT,
                    raw_path TEXT,
                    csv_path TEXT,
                    cleaned_path TEXT,
                    enriched_path TEXT,
                    spm_path TEXT,
                    report_path TEXT,
                    report_dir TEXT,
                    uploaded_report_file_id TEXT,
                    uploaded_report_link TEXT,
                    last_stage TEXT,
                    stats_json TEXT DEFAULT '{}',
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ua_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    group_key TEXT NOT NULL,
                    user_agent TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    group_size INTEGER NOT NULL DEFAULT 1,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, group_key)
                );

                CREATE TABLE IF NOT EXISTS deviceatlas_cache (
                    ua_hash TEXT PRIMARY KEY,
                    user_agent TEXT NOT NULL,
                    properties_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS spm_cache (
                    ua_hash TEXT PRIMARY KEY,
                    user_agent TEXT NOT NULL,
                    detection_status TEXT NOT NULL,
                    matches_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signature_hits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sig_id TEXT,
                    ua_pattern TEXT,
                    hit_count INTEGER NOT NULL,
                    run_date TEXT NOT NULL,
                    week_number TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate_runs_table(conn)

    @staticmethod
    def _migrate_runs_table(conn: sqlite3.Connection) -> None:
        """Add columns introduced after the initial schema.

        SQLite supports very small, safe ALTER TABLE migrations. Keeping this
        here lets long-lived server installs upgrade in place without deleting
        the existing run history or DeviceAtlas/SPM caches.
        """

        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        required_columns = {
            "spm_path": "TEXT",
            "report_path": "TEXT",
            "uploaded_report_file_id": "TEXT",
            "uploaded_report_link": "TEXT",
            "last_stage": "TEXT",
        }
        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {column_name} {column_type}")

    def create_run(self, run_date: str, query_name: str, query: str, start: str, end: str) -> int:
        now = utcnow()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO runs(run_date, query_name, status, state, query, start_time, end_time, created_at, updated_at)
                VALUES (?, ?, 'created', 'CREATED', ?, ?, ?, ?, ?)
                """,
                (run_date, query_name, query, start, end, now, now),
            )
            return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utcnow()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [json.dumps(value) if key == "stats_json" and not isinstance(value, str) else value for key, value in fields.items()]
        values.append(run_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", values)

    def get_run(self, run_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def list_runs(self, limit: int = 20) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))

    def list_active_runs(self) -> List[sqlite3.Row]:
        terminal_states = ("COMPLETED", "FAILED", "RUNDECK_FAILED", "RUNDECK_ABORTED", "RUNDECK_TIMEDOUT")
        placeholders = ",".join("?" for _ in terminal_states)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT * FROM runs
                    WHERE execution_id IS NOT NULL
                      AND state NOT IN ({placeholders})
                    ORDER BY id ASC
                    """,
                    terminal_states,
                )
            )

    def cache_get(self, table: str, ua_hash: str) -> Optional[sqlite3.Row]:
        if table not in {"deviceatlas_cache", "spm_cache"}:
            raise ValueError(f"Unsupported cache table: {table}")
        with self.connect() as conn:
            return conn.execute(f"SELECT * FROM {table} WHERE ua_hash = ?", (ua_hash,)).fetchone()

    def cache_deviceatlas(self, ua_hash: str, user_agent: str, properties: Dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO deviceatlas_cache(ua_hash, user_agent, properties_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (ua_hash, user_agent, json.dumps(properties, ensure_ascii=False), utcnow()),
            )

    def cache_spm(self, ua_hash: str, user_agent: str, status: str, matches: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO spm_cache(ua_hash, user_agent, detection_status, matches_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ua_hash, user_agent, status, json.dumps(matches, ensure_ascii=False), utcnow()),
            )
