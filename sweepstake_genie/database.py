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
    error_message TEXT
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
                "UPDATE sweepstakes SET status = ?, entered_at = ? WHERE url = ?",
                (STATUS_ENTERED, self._now(), url),
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

        stats: dict[str, int] = {
            STATUS_PENDING: 0,
            STATUS_ENTERED: 0,
            STATUS_SKIPPED: 0,
            STATUS_CAPTCHA: 0,
            STATUS_ERROR:   0,
            "total": total_row["cnt"] if total_row else 0,
        }
        for row in rows:
            stats[row["status"]] = row["cnt"]
        return stats

    def url_exists(self, url: str) -> bool:
        """Return True if *url* is already tracked in the database."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM sweepstakes WHERE url = ?", (url,)
            ).fetchone()
        return row is not None
