from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional


TERMINAL_STATES = ("COMPLETED", "FAILED", "RUNDECK_FAILED", "RUNDECK_ABORTED", "RUNDECK_TIMEDOUT")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
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

                CREATE TABLE IF NOT EXISTS scheduler_state (
                    name TEXT PRIMARY KEY,
                    query_name TEXT NOT NULL,
                    base_date TEXT NOT NULL,
                    last_submitted_date TEXT,
                    last_completed_date TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS retry_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL UNIQUE,
                    requested_stage TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
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
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT * FROM runs
                    WHERE execution_id IS NOT NULL
                      AND state NOT IN ({placeholders})
                    ORDER BY id ASC
                    """,
                    TERMINAL_STATES,
                )
            )

    def count_active_runs(self) -> int:
        terminal_states = TERMINAL_STATES
        placeholders = ",".join("?" for _ in terminal_states)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM runs
                WHERE execution_id IS NOT NULL
                  AND state NOT IN ({placeholders})
                """,
                terminal_states,
            ).fetchone()
            return int(row["count"] if row else 0)

    def find_runs_for_date(self, run_date: str, query_name: str) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM runs
                    WHERE run_date = ? AND query_name = ?
                    ORDER BY id ASC
                    """,
                    (run_date, query_name),
                )
            )

    def get_latest_completed_run(self, query_name: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM runs
                WHERE query_name = ? AND state = 'COMPLETED'
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (query_name,),
            ).fetchone()

    def get_scheduler_state(self, name: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM scheduler_state WHERE name = ?", (name,)).fetchone()

    def upsert_scheduler_state(self, name: str, query_name: str, base_date: str, enabled: bool = True) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduler_state(name, query_name, base_date, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    query_name = excluded.query_name,
                    base_date = excluded.base_date,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (name, query_name, base_date, 1 if enabled else 0, now, now),
            )

    def update_scheduler_state(self, name: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utcnow()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        values.append(name)
        with self.connect() as conn:
            conn.execute(f"UPDATE scheduler_state SET {assignments} WHERE name = ?", values)

    def list_scheduler_states(self) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM scheduler_state ORDER BY name ASC"))

    def add_retry(self, run_id: int, requested_stage: Optional[str] = None, note: Optional[str] = None) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO retry_queue(run_id, requested_stage, status, attempts, note, created_at, updated_at)
                VALUES (?, ?, 'queued', 0, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    requested_stage = excluded.requested_stage,
                    status = 'queued',
                    note = excluded.note,
                    updated_at = excluded.updated_at,
                    last_error = NULL
                """,
                (run_id, requested_stage, note, now, now),
            )

    def list_retries(self, status: Optional[str] = None) -> List[sqlite3.Row]:
        with self.connect() as conn:
            if status:
                return list(conn.execute("SELECT * FROM retry_queue WHERE status = ? ORDER BY id ASC", (status,)))
            return list(conn.execute("SELECT * FROM retry_queue ORDER BY id ASC"))

    def update_retry(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utcnow()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        values.append(run_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE retry_queue SET {assignments} WHERE run_id = ?", values)

    def remove_retry(self, run_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM retry_queue WHERE run_id = ?", (run_id,))

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
