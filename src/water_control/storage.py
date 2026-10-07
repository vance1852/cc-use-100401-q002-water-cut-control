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
    role TEXT NOT NULL CHECK(role IN ('engineer','approver','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS well_groups (
    group_id TEXT PRIMARY KEY,
    field_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS layer_series (
    layer_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES well_groups(group_id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wells (
    well_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES well_groups(group_id),
    layer_id TEXT NOT NULL REFERENCES layer_series(layer_id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('producer','injector')),
    state TEXT NOT NULL DEFAULT 'producing'
        CHECK(state IN ('producing','injecting','shut','observation')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wells_group ON wells(group_id, kind, well_id);

CREATE TABLE IF NOT EXISTS connectivity_links (
    link_id TEXT PRIMARY KEY,
    injector_id TEXT NOT NULL REFERENCES wells(well_id),
    producer_id TEXT NOT NULL REFERENCES wells(well_id),
    coefficient_percent TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(injector_id, producer_id),
    CHECK(injector_id <> producer_id)
);

CREATE TABLE IF NOT EXISTS well_tests (
    test_id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    tested_at TEXT NOT NULL,
    liquid_rate TEXT NOT NULL,
    water_cut_percent TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES wc_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(well_id, tested_at, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_tests_well_time ON well_tests(well_id, tested_at, test_id);

CREATE TABLE IF NOT EXISTS rate_constraints (
    constraint_id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    min_rate TEXT NOT NULL,
    max_rate TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_constraints_well ON rate_constraints(well_id, effective_from, constraint_id);

CREATE TABLE IF NOT EXISTS capacity_profiles (
    profile_id TEXT PRIMARY KEY,
    field_id TEXT NOT NULL,
    liquid_limit TEXT NOT NULL,
    water_limit TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_capacity_field ON capacity_profiles(field_id, effective_from, profile_id);

CREATE TABLE IF NOT EXISTS well_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    event_type TEXT NOT NULL CHECK(event_type IN ('shutdown','restore','observation')),
    note TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES wc_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    field_id TEXT NOT NULL,
    horizon_start TEXT NOT NULL,
    stage_count INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','confirmed','superseded','completed')),
    revision INTEGER NOT NULL DEFAULT 1,
    trigger TEXT NOT NULL DEFAULT 'manual' CHECK(trigger IN ('manual','well_shutdown','late_test')),
    base_plan_id TEXT,
    input_sha256 TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES wc_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_plan_per_field
ON plans(field_id) WHERE state='confirmed';

CREATE TABLE IF NOT EXISTS plan_stages (
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    stage_index INTEGER NOT NULL,
    stage_date TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','executed')),
    executed_by TEXT,
    executed_at TEXT,
    PRIMARY KEY(plan_id, stage_index)
);

CREATE TABLE IF NOT EXISTS plan_instructions (
    instruction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    stage_index INTEGER NOT NULL,
    stage_date TEXT NOT NULL,
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    action TEXT NOT NULL CHECK(action IN ('increase','limit','observe','maintain','shut')),
    target_liquid_rate TEXT NOT NULL,
    target_oil_rate TEXT NOT NULL,
    target_water_rate TEXT NOT NULL,
    target_injection_rate TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','executed')),
    carried_from_plan_id TEXT,
    executed_by TEXT,
    executed_at TEXT,
    UNIQUE(plan_id, stage_index, well_id)
);

CREATE INDEX IF NOT EXISTS idx_instructions_stage
ON plan_instructions(plan_id, stage_index, well_id);

CREATE TABLE IF NOT EXISTS override_requests (
    override_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    stage_index INTEGER NOT NULL,
    well_id TEXT NOT NULL REFERENCES wells(well_id),
    action TEXT NOT NULL CHECK(action IN ('increase','limit','observe','maintain','shut')),
    target_liquid_rate TEXT,
    target_injection_rate TEXT,
    reason TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','approved','applied','rejected','expired','cancelled')),
    requested_by TEXT NOT NULL REFERENCES wc_users(user_id),
    created_at TEXT NOT NULL,
    applied_by TEXT,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS override_approvals (
    override_id TEXT NOT NULL REFERENCES override_requests(override_id),
    approver_id TEXT NOT NULL REFERENCES wc_users(user_id),
    approved_at TEXT NOT NULL,
    PRIMARY KEY(override_id, approver_id)
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
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
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
