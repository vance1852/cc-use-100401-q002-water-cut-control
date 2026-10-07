"""稳油控水协同服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS wc_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('engineer','supervisor','operator','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS layer_systems (
    layer_system_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS well_groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wells (
    well_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('producer','injector')),
    layer_system_id TEXT NOT NULL REFERENCES layer_systems(layer_system_id),
    group_id TEXT NOT NULL REFERENCES well_groups(group_id),
    min_liquid TEXT NOT NULL,
    max_liquid TEXT NOT NULL,
    max_water_cut TEXT NOT NULL,
    min_injection TEXT NOT NULL,
    max_injection TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','disabled')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connectivity_edges (
    edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_group_id TEXT NOT NULL REFERENCES well_groups(group_id),
    to_group_id TEXT NOT NULL REFERENCES well_groups(group_id),
    coefficient TEXT NOT NULL,
    note TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES wc_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(from_group_id, to_group_id),
    CHECK(from_group_id <> to_group_id)
);

CREATE TABLE IF NOT EXISTS well_tests (
    test_id INTEGER PRIMARY KEY AUTOINCREMENT,
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    version INTEGER NOT NULL CHECK(version > 0),
    tested_at TEXT NOT NULL,
    liquid_rate TEXT NOT NULL,
    water_cut TEXT NOT NULL,
    injection_rate TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    recorded_by TEXT NOT NULL REFERENCES wc_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(well_id, version)
);

CREATE TABLE IF NOT EXISTS platform_caps (
    cap_id INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_from TEXT NOT NULL,
    liquid_cap TEXT NOT NULL,
    water_cap TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES wc_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS constraint_sets (
    constraint_set_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES wc_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS well_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    event_type TEXT NOT NULL CHECK(event_type IN ('shut_in','restore')),
    effective_at TEXT NOT NULL,
    note TEXT NOT NULL,
    reported_by TEXT NOT NULL REFERENCES wc_users(user_id),
    reported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_well_events_well_time
ON well_events(well_id, effective_at, event_id);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    field_id TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK(version_no > 0),
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','confirmed','executing','completed','superseded')),
    horizon_starts_at TEXT NOT NULL,
    phase_hours INTEGER NOT NULL,
    phase_count INTEGER NOT NULL,
    constraint_set_id TEXT NOT NULL REFERENCES constraint_sets(constraint_set_id),
    snapshot_json TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256) = 64),
    supersedes_plan_id TEXT REFERENCES plans(plan_id),
    recompute_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    created_by TEXT NOT NULL REFERENCES wc_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_by TEXT REFERENCES wc_users(user_id),
    confirmed_at TEXT,
    UNIQUE(field_id, version_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_plan_per_field
ON plans(field_id)
WHERE state IN ('confirmed','executing');

CREATE TABLE IF NOT EXISTS plan_phases (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    phase_index INTEGER NOT NULL CHECK(phase_index >= 0),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','executed')),
    totals_json TEXT NOT NULL,
    executed_by TEXT REFERENCES wc_users(user_id),
    executed_at TEXT,
    PRIMARY KEY(plan_id, phase_index)
);

CREATE TABLE IF NOT EXISTS well_commands (
    plan_id TEXT NOT NULL,
    phase_index INTEGER NOT NULL,
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    action TEXT NOT NULL
        CHECK(action IN ('increase','restrict','maintain','observe','shut','inject')),
    origin TEXT NOT NULL CHECK(origin IN ('engine','inherited','manual_override')),
    target_liquid TEXT NOT NULL,
    target_injection TEXT,
    projected_water_cut TEXT,
    oil_rate TEXT NOT NULL,
    water_rate TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    override_id INTEGER,
    PRIMARY KEY(plan_id, phase_index, well_id),
    FOREIGN KEY(plan_id, phase_index) REFERENCES plan_phases(plan_id, phase_index)
);

CREATE TABLE IF NOT EXISTS override_requests (
    override_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    phase_index INTEGER NOT NULL,
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    target_liquid TEXT,
    target_injection TEXT,
    reason TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','approved','applied','expired','void')),
    superseded_command_json TEXT,
    requested_by TEXT NOT NULL REFERENCES wc_users(user_id),
    requested_at TEXT NOT NULL,
    applied_by TEXT REFERENCES wc_users(user_id),
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS override_approvals (
    override_id INTEGER NOT NULL REFERENCES override_requests(override_id),
    approver_id TEXT NOT NULL REFERENCES wc_users(user_id),
    approved_at TEXT NOT NULL,
    PRIMARY KEY(override_id, approver_id)
);

CREATE TABLE IF NOT EXISTS wc_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS wc_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wc_audit_entity
ON wc_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
