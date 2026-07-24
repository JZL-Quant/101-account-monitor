from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS snapshot_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at TEXT NOT NULL,
    status TEXT NOT NULL,
    error_message TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS spot_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES snapshot_runs(id) ON DELETE CASCADE,
    currency TEXT NOT NULL,
    account_type TEXT NOT NULL,
    balance TEXT NOT NULL,
    available TEXT NOT NULL,
    holds TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS futures_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES snapshot_runs(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    current_qty TEXT NOT NULL,
    base_qty TEXT NOT NULL,
    mark_price TEXT NOT NULL,
    mark_value TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hedge_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES snapshot_runs(id) ON DELETE CASCADE,
    asset TEXT NOT NULL,
    spot_qty TEXT NOT NULL,
    futures_qty TEXT NOT NULL,
    net_qty TEXT NOT NULL,
    net_value TEXT NOT NULL,
    mismatch_percent TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    username TEXT NOT NULL,
    asset TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_snapshot_runs_status_id
    ON snapshot_runs(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_trade_actions_id
    ON trade_actions(id DESC);
"""


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ExposureRepository:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    async def initialize(self):
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self):
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    async def save_success(
        self,
        payload: dict[str, Any],
        spot_balances: list[dict[str, Any]],
        futures_positions: list[dict[str, Any]],
        hedge_rows: list[dict[str, Any]],
    ) -> int:
        return await asyncio.to_thread(
            self._save_success_sync,
            payload,
            spot_balances,
            futures_positions,
            hedge_rows,
        )

    def _save_success_sync(
        self,
        payload: dict[str, Any],
        spot_balances: list[dict[str, Any]],
        futures_positions: list[dict[str, Any]],
        hedge_rows: list[dict[str, Any]],
    ) -> int:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO snapshot_runs(sampled_at, status, payload_json)
                VALUES (?, 'success', ?)
                """,
                (payload["sampled_at"], json.dumps(payload, ensure_ascii=False)),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO spot_balances(
                    run_id, currency, account_type, balance, available, holds
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        row["currency"],
                        row["account_type"],
                        str(row["balance"]),
                        str(row["available"]),
                        str(row["holds"]),
                    )
                    for row in spot_balances
                ],
            )
            connection.executemany(
                """
                INSERT INTO futures_positions(
                    run_id, symbol, side, current_qty, base_qty,
                    mark_price, mark_value, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        row["symbol"],
                        row["side"],
                        str(row["current_qty"]),
                        str(row["base_qty"]),
                        str(row["mark_price"]),
                        str(row["mark_value"]),
                        json.dumps(row, ensure_ascii=False),
                    )
                    for row in futures_positions
                ],
            )
            connection.executemany(
                """
                INSERT INTO hedge_summaries(
                    run_id, asset, spot_qty, futures_qty, net_qty,
                    net_value, mismatch_percent, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        row["asset"],
                        str(row["spot_qty"]),
                        str(row["futures_qty"]),
                        str(row["net_qty"]),
                        str(row["net_value"]),
                        str(row["mismatch_percent"]),
                        row["status"],
                    )
                    for row in hedge_rows
                ],
            )
            connection.commit()
            return run_id

    async def save_failure(self, error_message: str):
        await asyncio.to_thread(self._save_failure_sync, error_message)

    def _save_failure_sync(self, error_message: str):
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO snapshot_runs(sampled_at, status, error_message)
                VALUES (?, 'failed', ?)
                """,
                (utc_now_text(), error_message[:2000]),
            )
            connection.commit()

    async def latest_success(self) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._latest_success_sync)

    def _latest_success_sync(self) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM snapshot_runs
                WHERE status = 'success'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    async def last_attempt(self) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._last_attempt_sync)

    def _last_attempt_sync(self) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT sampled_at, status, error_message
                FROM snapshot_runs ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    async def save_trade_action(
        self,
        *,
        username: str,
        asset: str,
        status: str,
        request: dict[str, Any],
        result: dict[str, Any],
        error_message: str = "",
    ) -> int:
        return await asyncio.to_thread(
            self._save_trade_action_sync,
            username,
            asset,
            status,
            request,
            result,
            error_message,
        )

    def _save_trade_action_sync(
        self,
        username: str,
        asset: str,
        status: str,
        request: dict[str, Any],
        result: dict[str, Any],
        error_message: str,
    ) -> int:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO trade_actions(
                    created_at, username, asset, status,
                    request_json, result_json, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now_text(),
                    username,
                    asset,
                    status,
                    json.dumps(request, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    error_message[:2000],
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    async def recent_trade_actions(self, limit: int = 20) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._recent_trade_actions_sync, limit)

    def _recent_trade_actions_sync(self, limit: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, username, asset, status,
                       result_json, error_message
                FROM trade_actions ORDER BY id DESC LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json"))
            results.append(item)
        return results
