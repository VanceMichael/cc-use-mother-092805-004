"""SQLite 持久化：事件、投影、回执、任务与告警落在同一库中，进程重启不丢。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    max_biosafety INTEGER NOT NULL,
    capacity_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'operational'
);

CREATE TABLE IF NOT EXISTS licenses (
    id TEXT PRIMARY KEY,
    issuer TEXT NOT NULL,
    purposes_json TEXT NOT NULL,
    orgs_json TEXT NOT NULL,
    equity_json TEXT NOT NULL,
    derivative_owner TEXT NOT NULL,
    transferable INTEGER NOT NULL DEFAULT 0,
    valid_from REAL NOT NULL,
    valid_to REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS materials (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    quality_batch TEXT NOT NULL,
    biosafety_level INTEGER NOT NULL,
    storage_class TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    site_id TEXT NOT NULL,
    custodian_org TEXT NOT NULL,
    license_id TEXT,
    composition_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    created_event TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,
    occurred_at REAL NOT NULL,
    recorded_at REAL NOT NULL,
    actor TEXT NOT NULL,
    org TEXT NOT NULL,
    site_id TEXT,
    basis_json TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    outputs_json TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    content_hash TEXT
);

CREATE TABLE IF NOT EXISTS access_requests (
    id TEXT PRIMARY KEY,
    submitter TEXT NOT NULL,
    org TEXT NOT NULL,
    qualifications_json TEXT NOT NULL,
    purpose TEXT NOT NULL,
    material_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    site_id TEXT NOT NULL,
    status TEXT NOT NULL,
    risk_flags_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    max_quantity REAL NOT NULL DEFAULT 0,
    approver TEXT,
    created_at REAL NOT NULL,
    decided_at REAL
);

CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    org TEXT NOT NULL,
    material_id TEXT,
    quantity REAL,
    unit TEXT,
    content_hash TEXT,
    occurred_at REAL NOT NULL,
    recorded_at REAL NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    reporter TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS freezes (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    released_at REAL,
    released_by TEXT,
    release_rationale TEXT
);

CREATE TABLE IF NOT EXISTS disposal_tasks (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    material_id TEXT,
    assignee_org TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    completed_at REAL,
    completed_by TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    due_at REAL NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS condition_readings (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    temperature REAL NOT NULL,
    humidity REAL
);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    ref_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trials (
    id TEXT PRIMARY KEY,
    org TEXT NOT NULL,
    plan_ref TEXT NOT NULL,
    site_id TEXT NOT NULL,
    access_request_id TEXT NOT NULL,
    follow_up_interval REAL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Store:
    """对 SQLite 的薄封装：所有写方法都走同一连接，便于事务组合。"""

    def __init__(self, path: str | Path):
        self._path = str(path)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        if self._path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    @classmethod
    def in_memory(cls) -> "Store":
        return cls(":memory:")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """多步写入的事务边界：全部成功才提交，任一失败整体回滚。"""
        try:
            yield self._conn
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.transaction() as conn:
            return conn.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(sql, params)]

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None
