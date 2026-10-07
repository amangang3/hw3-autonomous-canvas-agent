from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
          outcome TEXT, posts INTEGER NOT NULL DEFAULT 0, skip_reason TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS seen_entries (
          entry_id INTEGER PRIMARY KEY, parent_id INTEGER, user_id INTEGER,
          created_at TEXT, first_seen_run TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS actions (
          idem_key TEXT PRIMARY KEY, kind TEXT NOT NULL, target INTEGER,
          content_hash TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL,
          canvas_entry_id INTEGER, run_id TEXT NOT NULL, intended_at TEXT NOT NULL,
          posted_at TEXT, verified_at TEXT, abandoned_at TEXT
        );
        """)
        if self.meta_get("consecutive_failed_runs") is None:
            self.meta_set("consecutive_failed_runs", "0")

    def meta_get(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: str | int) -> None:
        self.connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def create_run(self, run_id: str, now: datetime) -> None:
        self.connection.execute("INSERT INTO runs(run_id,started_at) VALUES(?,?)", (run_id, iso(now)))

    def claim_run(self, run_id: str, now: datetime, stale_after_seconds: float = 3600) -> tuple[bool, int, int]:
        """Atomically serialize runs; stale active rows are closed, never overlapped."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            breaker_row = self.connection.execute(
                "SELECT value FROM meta WHERE key='consecutive_failed_runs'"
            ).fetchone()
            failures = int(breaker_row[0]) if breaker_row else 0
            rows = self.connection.execute("SELECT run_id,started_at FROM runs WHERE ended_at IS NULL").fetchall()
            stale_ids: list[str] = []
            for row in rows:
                try:
                    started = datetime.fromisoformat(row["started_at"]).astimezone(timezone.utc)
                except (TypeError, ValueError):
                    self.connection.execute("ROLLBACK")
                    return False, 0, failures
                if (now.astimezone(timezone.utc) - started).total_seconds() < stale_after_seconds:
                    self.connection.execute("ROLLBACK")
                    return False, 0, failures
                stale_ids.append(row["run_id"])
            for stale_id in stale_ids:
                self.connection.execute(
                    "UPDATE runs SET ended_at=?,outcome='error',error='stale run recovered' WHERE run_id=? AND ended_at IS NULL",
                    (iso(now), stale_id),
                )
            failures += len(stale_ids)
            self.connection.execute(
                "INSERT INTO meta(key,value) VALUES('consecutive_failed_runs',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(failures),),
            )
            self.connection.execute("INSERT INTO runs(run_id,started_at) VALUES(?,?)", (run_id, iso(now)))
            self.connection.execute("COMMIT")
            return True, len(stale_ids), failures
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def active_run(self, run_id: str) -> bool:
        row = self.connection.execute("SELECT 1 FROM runs WHERE run_id=? AND ended_at IS NULL", (run_id,)).fetchone()
        return bool(row)

    def run_started(self, run_id: str) -> datetime | None:
        row = self.connection.execute("SELECT started_at FROM runs WHERE run_id=? AND ended_at IS NULL", (run_id,)).fetchone()
        if not row:
            return None
        try:
            return datetime.fromisoformat(row[0]).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    def end_run(self, run_id: str, outcome: str, now: datetime, error: str | None = None) -> bool:
        cur = self.connection.execute(
            "UPDATE runs SET ended_at=?, outcome=?, error=? WHERE run_id=? AND ended_at IS NULL",
            (iso(now), outcome, error, run_id),
        )
        return cur.rowcount == 1

    def finish_run(self, run_id: str, outcome: str, now: datetime, error: str | None = None) -> tuple[bool, int]:
        """Atomically close a run and update the consecutive-failure breaker."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cur = self.connection.execute(
                "UPDATE runs SET ended_at=?, outcome=?, error=? WHERE run_id=? AND ended_at IS NULL",
                (iso(now), outcome, error, run_id),
            )
            if cur.rowcount != 1:
                self.connection.execute("ROLLBACK")
                return False, int(self.meta_get("consecutive_failed_runs") or 0)
            current = int(self.meta_get("consecutive_failed_runs") or 0)
            failures = 0 if outcome == "ok" else current + 1
            self.connection.execute(
                "INSERT INTO meta(key,value) VALUES('consecutive_failed_runs',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(failures),),
            )
            self.connection.execute("COMMIT")
            return True, failures
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def record_skip(self, run_id: str, reason: str) -> None:
        self.connection.execute("UPDATE runs SET skip_reason=? WHERE run_id=? AND ended_at IS NULL", (reason, run_id))

    def mark_seen(self, entries: list[dict[str, Any]], run_id: str) -> None:
        with self.connection:
            self.connection.executemany(
                "INSERT OR IGNORE INTO seen_entries(entry_id,parent_id,user_id,created_at,first_seen_run) VALUES(?,?,?,?,?)",
                [(e["id"], e.get("parent_id"), e.get("user_id"), e.get("created_at"), run_id) for e in entries],
            )

    def seen_ids(self) -> set[int]:
        return {r[0] for r in self.connection.execute("SELECT entry_id FROM seen_entries")}

    def reply_targets(self) -> set[int]:
        rows = self.connection.execute(
            "SELECT DISTINCT target FROM actions "
            "WHERE kind='reply' AND target IS NOT NULL AND status!='abandoned'"
        ).fetchall()
        return {int(row[0]) for row in rows}

    def insert_action(self, key: str, kind: str, target: int | None, content_hash: str,
                      message: str, run_id: str, now: datetime) -> bool:
        try:
            self.connection.execute(
                "INSERT INTO actions(idem_key,kind,target,content_hash,message,status,run_id,intended_at) VALUES(?,?,?,?,?,'intended',?,?)",
                (key, kind, target, content_hash, message, run_id, iso(now)),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def action_exists(self, key: str) -> bool:
        return bool(self.connection.execute("SELECT 1 FROM actions WHERE idem_key=?", (key,)).fetchone())

    def actions(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute("SELECT * FROM actions WHERE status=? ORDER BY intended_at", (status,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM actions ORDER BY intended_at").fetchall()
        return [dict(r) for r in rows]

    def unresolved_actions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM actions WHERE status IN ('intended','posted') ORDER BY intended_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def count_hour(self, now: datetime) -> int:
        cutoff = iso(now - timedelta(hours=1))
        return self.connection.execute(
            "SELECT COUNT(*) FROM actions WHERE status IN ('intended','posted','verified') AND intended_at>?",
            (cutoff,),
        ).fetchone()[0]

    def count_run(self, run_id: str) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM actions WHERE run_id=? AND status IN ('intended','posted','verified')", (run_id,)
        ).fetchone()[0]

    def mark_posted(self, key: str, entry_id: int, now: datetime) -> None:
        self.connection.execute(
            "UPDATE actions SET status='posted',canvas_entry_id=?,posted_at=? WHERE idem_key=? AND status='intended'",
            (entry_id, iso(now), key),
        )

    def mark_verified(self, key: str, entry_id: int, now: datetime) -> None:
        self.connection.execute(
            "UPDATE actions SET status='verified',canvas_entry_id=?,verified_at=? WHERE idem_key=?",
            (entry_id, iso(now), key),
        )

    def mark_abandoned(self, key: str, now: datetime) -> None:
        self.connection.execute(
            "UPDATE actions SET status='abandoned',abandoned_at=? WHERE idem_key=? AND status='intended'",
            (iso(now), key),
        )

    def own_actions(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT kind,target,message,canvas_entry_id,verified_at FROM actions WHERE status='verified' ORDER BY verified_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
