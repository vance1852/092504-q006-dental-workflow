"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    external_key TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('open', 'closed')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, external_key)
);
CREATE TABLE IF NOT EXISTS prescription_versions (
    version_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'superseded')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(case_id, version_no),
    UNIQUE(case_id, payload_hash)
);
CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    prescription_version_id TEXT NOT NULL REFERENCES prescription_versions(version_id),
    definition_hash TEXT NOT NULL,
    branch_of_product_id TEXT,
    rework_reason TEXT,
    status TEXT NOT NULL CHECK(status IN ('planned', 'in_progress', 'completed', 'reevaluated')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(case_id, definition_hash)
);
CREATE TABLE IF NOT EXISTS route_steps (
    step_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    station TEXT NOT NULL,
    responsible_id TEXT NOT NULL REFERENCES actors(actor_id),
    status TEXT NOT NULL CHECK(status IN ('pending', 'in_progress', 'completed')),
    started_at TEXT,
    completed_at TEXT,
    output_json TEXT,
    output_hash TEXT,
    UNIQUE(route_id, sequence)
);
CREATE TABLE IF NOT EXISTS handovers (
    handover_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    from_step_id TEXT NOT NULL REFERENCES route_steps(step_id),
    to_step_id TEXT NOT NULL REFERENCES route_steps(step_id),
    attempt_no INTEGER NOT NULL CHECK(attempt_no >= 1),
    from_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    to_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    version_ref_json TEXT NOT NULL,
    version_ref_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'disputed')),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    confirmed_by TEXT,
    UNIQUE(route_id, from_step_id, to_step_id, attempt_no)
);
CREATE TABLE IF NOT EXISTS handover_discrepancies (
    discrepancy_id TEXT PRIMARY KEY,
    handover_id TEXT NOT NULL REFERENCES handovers(handover_id),
    detail TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
    raised_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    resolution TEXT,
    resolved_by TEXT,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS material_batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    material_code TEXT NOT NULL,
    lot_number TEXT NOT NULL,
    unit TEXT NOT NULL,
    initial_quantity TEXT NOT NULL,
    remaining_quantity TEXT NOT NULL,
    parent_batch_id TEXT REFERENCES material_batches(batch_id),
    split_hash TEXT,
    status TEXT NOT NULL CHECK(status IN ('available', 'depleted')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, material_code, lot_number)
);
CREATE TABLE IF NOT EXISTS material_consumptions (
    consumption_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES material_batches(batch_id),
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    route_id TEXT REFERENCES routes(route_id),
    step_id TEXT REFERENCES route_steps(step_id),
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    route_id TEXT NOT NULL UNIQUE REFERENCES routes(route_id),
    prescription_version_id TEXT NOT NULL REFERENCES prescription_versions(version_id),
    completed_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交；写事务通过锁串行化。"""

        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """在与写事务互斥的临界区内执行一致读取。"""

        with self._lock:
            yield self.connection

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
