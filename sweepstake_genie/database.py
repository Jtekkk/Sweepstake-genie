"""
SQLite database layer for tracking sweepstakes discovery and entry status.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS sweepstakes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT UNIQUE NOT NULL,
    title         TEXT,
    source        TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status        TEXT NOT NULL DEFAULT 'pending',
    entered_at    TIMESTAMP,
    error_message TEXT,
    entry_count   INTEGER NOT NULL DEFAULT 0,
    allows_daily  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sweepstakes_status ON sweepstakes (status);
CREATE INDEX IF NOT EXISTS idx_sweepstakes_url    ON sweepstakes (url);
"""

# Valid status values
STATUS_PENDING  = "pending"
STATUS_ENTERED  = "entered"
STATUS_SKIPPED  = "skipped"
STATUS_CAPTCHA  = "captcha"
STATUS_ERROR    = "error"


# ── Database class ────────────────────────────────────────────────────────────

class Database:
    """Thin wrapper around a SQLite connection for sweepstakes tracking."""

    def __init__(self, db_path: str | Path = "sweepstakes.db") -> None:
        self.db_path = Path(db_path)
        self._ensure_schema()

    # ── Internal helpers ──────────────────────────────────────────────────

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield a thread-safe connection with row_factory set."""
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")  # safe with WAL, much faster writes
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """Create tables if they do not exist yet."""
        with self._conn() as conn:
            conn.executescript(_DDL)
            # Migration: add new columns to existing databases
            for col_def in [
                "ALTER TABLE sweepstakes ADD COLUMN entry_count INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE sweepstakes ADD COLUMN allows_daily INTEGER NOT NULL DEFAULT 0",
            ]:
                try:
                    conn.execute(col_def)
                except sqlite3.OperationalError:
                    pass  # column already exists

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ── Public API ────────────────────────────────────────────────────────

    def add_sweepstake(self, url: str, title: str, source: str) -> bool:
        """
        Insert a new sweepstake record.  If *url* already exists the row is
        left unchanged (INSERT OR IGNORE).

        Returns True if a new row was inserted, False if it already existed.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO sweepstakes (url, title, source, discovered_at, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (url, title, source, self._now(), STATUS_PENDING),
            )
            return cursor.rowcount == 1

    def add_sweepstakes_bulk(
        self, entries: list[dict[str, Any]]
    ) -> int:
        """
        Insert many sweepstakes in a single transaction.

        Each entry must have ``url``; ``title`` and ``source`` are optional.
        Existing URLs are left unchanged (INSERT OR IGNORE).

        Returns the number of newly inserted rows. Far faster than calling
        :meth:`add_sweepstake` in a loop because it uses one connection and one
        transaction instead of one per row.
        """
        if not entries:
            return 0
        now = self._now()
        rows = [
            (
                e["url"],
                e.get("title", e["url"]),
                e.get("source", ""),
                now,
                STATUS_PENDING,
            )
            for e in entries
            if e.get("url")
        ]
        if not rows:
            return 0
        with self._conn() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO sweepstakes (url, title, source, discovered_at, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
            return conn.total_changes - before

    def get_pending(self) -> list[dict[str, Any]]:
        """Return all sweepstakes whose status is 'pending'."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sweepstakes WHERE status = ? ORDER BY id",
                (STATUS_PENDING,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_entered(self, url: str) -> None:
        """Mark a sweepstake as successfully entered."""
        with self._conn() as conn:
            conn.execute(
                """UPDATE sweepstakes
                   SET status='entered', entered_at=?,
                       entry_count=entry_count+1
                 WHERE url=?""",
                (self._now(), url),
            )

    def mark_skipped(self, url: str, reason: str = "") -> None:
        """Mark a sweepstake as intentionally skipped."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE sweepstakes SET status = ?, error_message = ? WHERE url = ?",
                (STATUS_SKIPPED, reason, url),
            )

    def mark_captcha(self, url: str) -> None:
        """Mark a sweepstake as blocked by a CAPTCHA."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE sweepstakes SET status = ? WHERE url = ?",
                (STATUS_CAPTCHA, url),
            )

    def mark_error(self, url: str, message: str) -> None:
        """Mark a sweepstake as failed with an error message."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE sweepstakes SET status = ?, error_message = ? WHERE url = ?",
                (STATUS_ERROR, message, url),
            )

    def mark_allows_daily(self, url: str) -> None:
        """Mark a sweepstake as allowing daily re-entry."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE sweepstakes SET allows_daily=1 WHERE url=?", (url,)
            )

    def get_due_for_reentry(self) -> list[dict]:
        """Return sweepstakes that allow daily entry and haven't been entered today."""
        with self._conn() as conn:
            cur = conn.execute(
                """SELECT id, url, title, source
                     FROM sweepstakes
                    WHERE allows_daily=1
                      AND status='entered'
                      AND (entered_at IS NULL
                           OR DATE(entered_at) < DATE('now'))
                    ORDER BY id""",
            )
            return [dict(r) for r in cur.fetchall()]

    def get_stats(self) -> dict[str, int]:
        """
        Return a dict with counts per status and a 'total' key.

        Example::

            {"total": 120, "pending": 10, "entered": 80, "skipped": 5,
             "captcha": 20, "error": 5}
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM sweepstakes GROUP BY status"
            ).fetchall()
            total_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM sweepstakes"
            ).fetchone()
            daily_due = conn.execute(
                """SELECT COUNT(*) FROM sweepstakes
                    WHERE allows_daily=1 AND status='entered'
                      AND (entered_at IS NULL OR DATE(entered_at) < DATE('now','localtime'))"""
            ).fetchone()[0]

        stats: dict[str, int] = {
            STATUS_PENDING: 0,
            STATUS_ENTERED: 0,
            STATUS_SKIPPED: 0,
            STATUS_CAPTCHA: 0,
            STATUS_ERROR:   0,
            "total": total_row["cnt"] if total_row else 0,
            "daily_due": daily_due,
        }
        for row in rows:
            stats[row["status"]] = row["cnt"]
        return stats

    def get_retryable(self, include_captcha: bool = False) -> list[dict[str, Any]]:
        """
        Return sweepstakes that previously failed and are worth retrying.

        Always includes ``error`` entries (likely transient timeouts/network
        blips).  Optionally includes ``captcha`` entries when a solver is now
        available.
        """
        statuses = [STATUS_ERROR]
        if include_captcha:
            statuses.append(STATUS_CAPTCHA)
        placeholders = ",".join("?" * len(statuses))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM sweepstakes WHERE status IN ({placeholders}) ORDER BY id",
                statuses,
            ).fetchall()
        return [dict(row) for row in rows]

    def url_exists(self, url: str) -> bool:
        """Return True if *url* is already tracked in the database."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM sweepstakes WHERE url = ?", (url,)
            ).fetchone()
        return row is not None
