from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class FacilitatorStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_hash TEXT NOT NULL,
                pay_to TEXT NOT NULL,
                amount TEXT NOT NULL,
                network TEXT NOT NULL,
                resource TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'settled',
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS observability_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                resource TEXT NOT NULL DEFAULT '',
                network TEXT NOT NULL DEFAULT '',
                pay_to TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_settlements_pay_to ON settlements(pay_to)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_settlements_resource ON settlements(resource)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON observability_events(event_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_resource ON observability_events(resource)")
        self._ensure_column(conn, "settlements", "resource", "TEXT NOT NULL DEFAULT ''")
        conn.commit()
        conn.close()

    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def record_event(
        self,
        event_type: str,
        *,
        resource: str = "",
        network: str = "",
        pay_to: str = "",
        status: str = "",
        detail: str = "",
    ) -> None:
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO observability_events (event_type, resource, network, pay_to, status, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_type, resource, network, pay_to, status, detail, time.time()),
        )
        conn.commit()
        conn.close()

    def record_settlement(
        self,
        tx_hash: str,
        pay_to: str,
        amount: str,
        network: str,
        *,
        resource: str = "",
        status: str = "settled",
    ) -> None:
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO settlements (tx_hash, pay_to, amount, network, resource, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (tx_hash, pay_to, amount, network, resource, status, time.time()),
        )
        conn.commit()
        conn.close()

    def get_settlements(self, pay_to: str, limit: int, offset: int) -> dict:
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT tx_hash, pay_to, amount, network, resource, status, created_at
            FROM settlements
            WHERE pay_to = ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (pay_to, limit, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM settlements WHERE pay_to = ?", (pay_to,)).fetchone()[0]
        conn.close()
        return {
            "settlements": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_settlement_stats(self, pay_to: str) -> dict:
        conn = self._connect()
        row = conn.execute(
            """
            SELECT COUNT(*) AS total_settlements,
                   COALESCE(SUM(CAST(amount AS REAL)), 0) AS total_amount
            FROM settlements
            WHERE pay_to = ? AND status = 'settled'
            """,
            (pay_to,),
        ).fetchone()
        conn.close()
        return {
            "pay_to": pay_to,
            "total_settlements": row["total_settlements"],
            "total_amount": row["total_amount"],
        }

    def get_observability_summary(self, limit: int = 20) -> dict:
        conn = self._connect()
        event_rows = conn.execute(
            "SELECT event_type, COUNT(*) AS total FROM observability_events GROUP BY event_type"
        ).fetchall()
        resource_rows = conn.execute(
            """
            SELECT resource,
                   SUM(CASE WHEN event_type = 'verify_attempt' THEN 1 ELSE 0 END) AS verify_attempts,
                   SUM(CASE WHEN event_type = 'verify_success' THEN 1 ELSE 0 END) AS verify_successes,
                   SUM(CASE WHEN event_type = 'settle_attempt' THEN 1 ELSE 0 END) AS settle_attempts,
                   SUM(CASE WHEN event_type = 'settle_success' THEN 1 ELSE 0 END) AS settle_successes
            FROM observability_events
            WHERE resource != ''
            GROUP BY resource
            """
        ).fetchall()
        settlement_rows = conn.execute(
            """
            SELECT resource, COUNT(*) AS settlements
            FROM settlements
            WHERE resource != ''
            GROUP BY resource
            """
        ).fetchall()
        recent_rows = conn.execute(
            """
            SELECT event_type, resource, network, pay_to, status, detail, created_at
            FROM observability_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()

        settlement_totals = {row["resource"]: row["settlements"] for row in settlement_rows}
        resources = []
        for row in resource_rows:
            resources.append(
                {
                    "resource": row["resource"],
                    "verify_attempts": row["verify_attempts"],
                    "verify_successes": row["verify_successes"],
                    "settle_attempts": row["settle_attempts"],
                    "settle_successes": row["settle_successes"],
                    "settlements": settlement_totals.get(row["resource"], 0),
                }
            )

        resources.sort(key=lambda item: (-item["settlements"], item["resource"]))
        return {
            "events": {row["event_type"]: row["total"] for row in event_rows},
            "resources": resources,
            "recent": [dict(row) for row in recent_rows],
        }
