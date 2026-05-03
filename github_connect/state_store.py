from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Literal

DeliveryStatus = Literal["received", "processed", "failed", "ignored"]
ClaimAction = Literal["process", "skip_processed"]


@dataclass(frozen=True)
class ClaimResult:
    action: ClaimAction
    prior_status: DeliveryStatus | None


class StateStore:
    def __init__(self, db_path: Path, stale_after: timedelta = timedelta(minutes=15)) -> None:
        self._db_path = db_path
        self._stale_after = stale_after
        self._lock = Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event TEXT NOT NULL,
                    installation_id INTEGER,
                    repo TEXT,
                    status TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    processed_at TEXT,
                    error TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_webhook_received_at ON webhook_deliveries(received_at)"
            )

    def claim_or_skip(
        self,
        *,
        delivery_id: str,
        event: str,
        installation_id: int | None,
        repo: str | None,
    ) -> ClaimResult:
        now = datetime.now(UTC)
        with self._lock:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT status, received_at FROM webhook_deliveries WHERE delivery_id = ?",
                    (delivery_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO webhook_deliveries
                        (delivery_id, event, installation_id, repo, status, received_at, processed_at, error)
                        VALUES (?, ?, ?, ?, 'received', ?, NULL, NULL)
                        """,
                        (delivery_id, event, installation_id, repo, now.isoformat()),
                    )
                    return ClaimResult(action="process", prior_status=None)

                status = row["status"]
                if status == "processed":
                    return ClaimResult(action="skip_processed", prior_status="processed")
                if status == "failed":
                    conn.execute(
                        """
                        UPDATE webhook_deliveries
                        SET status='received', event=?, installation_id=?, repo=?, received_at=?, processed_at=NULL, error=NULL
                        WHERE delivery_id=?
                        """,
                        (event, installation_id, repo, now.isoformat(), delivery_id),
                    )
                    return ClaimResult(action="process", prior_status="failed")
                if status == "ignored":
                    conn.execute(
                        """
                        UPDATE webhook_deliveries
                        SET status='received', event=?, installation_id=?, repo=?, received_at=?, processed_at=NULL, error=NULL
                        WHERE delivery_id=?
                        """,
                        (event, installation_id, repo, now.isoformat(), delivery_id),
                    )
                    return ClaimResult(action="process", prior_status="ignored")
                # received
                received_at = datetime.fromisoformat(row["received_at"])
                if now - received_at > self._stale_after:
                    conn.execute(
                        """
                        UPDATE webhook_deliveries
                        SET status='received', event=?, installation_id=?, repo=?, received_at=?, processed_at=NULL, error=NULL
                        WHERE delivery_id=?
                        """,
                        (event, installation_id, repo, now.isoformat(), delivery_id),
                    )
                    return ClaimResult(action="process", prior_status="received")
                return ClaimResult(action="skip_processed", prior_status="received")

    def mark_processed(self, delivery_id: str) -> None:
        self._mark(delivery_id, "processed")

    def mark_ignored(self, delivery_id: str) -> None:
        self._mark(delivery_id, "ignored")

    def mark_failed(self, delivery_id: str, error: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status='failed', processed_at=?, error=?
                    WHERE delivery_id=?
                    """,
                    (now, error[:500], delivery_id),
                )

    def _mark(self, delivery_id: str, status: DeliveryStatus) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE webhook_deliveries SET status=?, processed_at=?, error=NULL WHERE delivery_id=?",
                    (status, now, delivery_id),
                )

    def ping(self) -> bool:
        with self._conn() as conn:
            conn.execute("SELECT 1").fetchone()
        return True

