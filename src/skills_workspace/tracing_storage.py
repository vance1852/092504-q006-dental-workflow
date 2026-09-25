"""病例工序与材料追溯账本的表结构。

所有写入都在 :class:`skills_workspace.storage.Database` 的同一 SQLite 库中完成，
复用既有 actors / audit_events / request_receipts 机制。数量一律以定点十进制
文本保存（scale=4），并由 CHECK 约束禁止负值。
"""

from __future__ import annotations

TRACING_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    subject_code TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','completed','cancelled')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, subject_code)
);

-- 脱敏处方：仅保留病例约束（牙位、关键尺寸、材料约束等），不含任何身份信息。
CREATE TABLE IF NOT EXISTS prescriptions (
    prescription_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(case_id, version)
);

-- 返工分支树。初始分支 parent_branch_id 为空；返修新建分支而不覆盖原分支。
CREATE TABLE IF NOT EXISTS case_branches (
    branch_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    parent_branch_id TEXT REFERENCES case_branches(branch_id),
    root_branch_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);

-- 工序路线：处方修订后尚未开始的工序重新评估（superseded 或换版），已开始记录不可改写。
CREATE TABLE IF NOT EXISTS route_operations (
    operation_id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES case_branches(branch_id),
    seq INTEGER NOT NULL CHECK(seq >= 1),
    code TEXT NOT NULL,
    station_id TEXT NOT NULL,
    prescription_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','active','done','skipped','superseded')),
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(branch_id, seq)
);

CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    branch_id TEXT NOT NULL REFERENCES case_branches(branch_id),
    parent_product_id TEXT REFERENCES products(product_id),
    sku TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('in_progress','finished','scrapped','delivered')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);

-- 工位责任快照：工序在哪个工位、由谁发起、由谁接收。
CREATE TABLE IF NOT EXISTS station_assignments (
    assignment_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES route_operations(operation_id),
    station_id TEXT NOT NULL,
    technician_id TEXT NOT NULL REFERENCES actors(actor_id),
    role TEXT NOT NULL CHECK(role IN ('owner','handled')),
    assigned_at TEXT NOT NULL
);

-- 交接主记录。终态唯一，杜绝重复交接；同操作下只允许一个未关闭交接。
CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    branch_id TEXT NOT NULL REFERENCES case_branches(branch_id),
    operation_id TEXT NOT NULL REFERENCES route_operations(operation_id),
    seq INTEGER NOT NULL,
    from_station_id TEXT NOT NULL,
    from_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    to_station_id TEXT NOT NULL,
    to_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    prescription_version INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('proposed','confirmed','disputed','closed','superseded')),
    completed_operation INTEGER NOT NULL DEFAULT 0 CHECK(completed_operation IN (0,1)),
    marked_started INTEGER NOT NULL DEFAULT 0 CHECK(marked_started IN (0,1)),
    message_id TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    closed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_handoff_one_open
    ON handoffs(operation_id) WHERE state IN ('proposed','disputed');
CREATE UNIQUE INDEX IF NOT EXISTS ux_handoff_terminal
    ON handoffs(operation_id, state) WHERE state IN ('confirmed','closed');
CREATE INDEX IF NOT EXISTS ix_handoff_case ON handoffs(case_id);

-- 差异（接收方提出）。resolved 后交接方可确认关闭。
CREATE TABLE IF NOT EXISTS discrepancies (
    discrepancy_id TEXT PRIMARY KEY,
    handoff_id TEXT NOT NULL REFERENCES handoffs(handoff_id),
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    reason TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    actual_json TEXT NOT NULL,
    raised_by TEXT NOT NULL REFERENCES actors(actor_id),
    raised_at TEXT NOT NULL,
    resolution TEXT NOT NULL DEFAULT '',
    resolved_by TEXT REFERENCES actors(actor_id),
    resolved_at TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved','rejected'))
);
CREATE INDEX IF NOT EXISTS ix_discrepancy_case ON discrepancies(case_id);

-- 材料批次：initial = remaining + 全部已拆出/消耗，由应用层在锁内守恒。
CREATE TABLE IF NOT EXISTS material_lots (
    lot_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    material_code TEXT NOT NULL,
    initial_qty TEXT NOT NULL,
    remaining_qty TEXT NOT NULL,
    scale INTEGER NOT NULL DEFAULT 4,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    CHECK(CAST(initial_qty AS REAL) >= 0),
    CHECK(CAST(remaining_qty AS REAL) >= 0)
);

-- 批次拆分：parent 扣减 qty，child 初始得到 qty，构成拆分树。
CREATE TABLE IF NOT EXISTS material_splits (
    split_id TEXT PRIMARY KEY,
    parent_lot_id TEXT NOT NULL REFERENCES material_lots(lot_id),
    child_lot_id TEXT NOT NULL UNIQUE REFERENCES material_lots(lot_id),
    qty TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    CHECK(CAST(qty AS REAL) > 0)
);

-- 领料/消耗：批次扣减并登记到工序；带状态以支持撤销（终态迟到消息）。
CREATE TABLE IF NOT EXISTS material_consumptions (
    consumption_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL REFERENCES material_lots(lot_id),
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    branch_id TEXT NOT NULL REFERENCES case_branches(branch_id),
    operation_id TEXT NOT NULL REFERENCES route_operations(operation_id),
    qty TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'consumed' CHECK(status IN ('consumed','reversed')),
    consumed_by TEXT NOT NULL REFERENCES actors(actor_id),
    consumed_at TEXT NOT NULL,
    reversed_at TEXT,
    CHECK(CAST(qty AS REAL) > 0)
);
CREATE INDEX IF NOT EXISTS ix_consumption_lot ON material_consumptions(lot_id);
CREATE INDEX IF NOT EXISTS ix_consumption_case ON material_consumptions(case_id);

-- 成品输入：记录每个成品由哪些已确认交接版本、哪些领料构成。
CREATE TABLE IF NOT EXISTS product_inputs (
    product_id TEXT NOT NULL REFERENCES products(product_id),
    kind TEXT NOT NULL CHECK(kind IN ('handoff','consumption')),
    ref_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(product_id, kind, ref_id)
);
"""
