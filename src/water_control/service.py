"""稳油控水协同方案的事务用例。

方案把井、层系、井组连通、测试版本、含水趋势、注采约束和平台处理上限
冻结成带内容摘要的快照；并发确认只保留一个有效版本；重算只覆盖未来
时段，已执行指令以继承方式进入新版本且原记录不被回写。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    CapInput,
    ConstraintSetInput,
    EdgeInput,
    EventInput,
    OverrideInput,
    PlanInput,
    RecomputeInput,
    TestInput,
    WellInput,
    identifier,
    required_text,
)
from .planning import (
    ACTION_LABELS,
    InfeasiblePlan,
    canonical_json,
    conservation_report,
    decimal_text,
    digest,
    plan_phases,
    phase_totals,
    quantize_rate,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "engineer": {
        "catalog.write", "test.write", "event.write",
        "plan.create", "plan.recompute", "override.request",
    },
    "supervisor": {"plan.confirm", "override.approve"},
    "operator": {"phase.execute"},
    "auditor": {"report.read", "audit.read"},
}

ACTIVE_PLAN_STATES = ("confirmed", "executing")
SECONDS_PER_DAY = Decimal("86400")


class WaterControlService:
    """在单个 SQLite 连接上提供稳油控水协同的全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM wc_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM wc_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO wc_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    # ------------------------------------------------------------------
    # 用户与基础资料
    # ------------------------------------------------------------------

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO wc_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def create_layer_system(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        layer_system_id = identifier(raw.get("layer_system_id"), "layer_system_id")
        name = required_text(raw.get("name"), "name")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO layer_systems(layer_system_id,name,created_at) VALUES(?,?,?)",
                    (layer_system_id, name, self._now()),
                )
                self._audit("layer_system", layer_system_id, "catalog.layer_system_created", actor_id, {"name": name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("层系编号已经存在") from exc
        return {"layer_system_id": layer_system_id, "name": name}

    def create_well_group(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        group_id = identifier(raw.get("group_id"), "group_id")
        name = required_text(raw.get("name"), "name")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO well_groups(group_id,name,created_at) VALUES(?,?,?)",
                    (group_id, name, self._now()),
                )
                self._audit("well_group", group_id, "catalog.well_group_created", actor_id, {"name": name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("井组编号已经存在") from exc
        return {"group_id": group_id, "name": name}

    def create_well(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        well = WellInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO wells(well_id,name,kind,layer_system_id,group_id,min_liquid,max_liquid,"
                    "max_water_cut,min_injection,max_injection,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        well.well_id,
                        well.name,
                        well.kind,
                        well.layer_system_id,
                        well.group_id,
                        decimal_text(well.min_liquid),
                        decimal_text(well.max_liquid),
                        decimal_text(well.max_water_cut),
                        decimal_text(well.min_injection),
                        decimal_text(well.max_injection),
                        self._now(),
                    ),
                )
                self._audit("well", well.well_id, "catalog.well_created", actor_id, {"kind": well.kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict("井编号冲突或层系、井组不存在") from exc
        return {"well_id": well.well_id, "kind": well.kind}

    def add_connectivity(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        edge = EdgeInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO connectivity_edges(from_group_id,to_group_id,coefficient,note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        edge.from_group_id,
                        edge.to_group_id,
                        decimal_text(edge.coefficient),
                        edge.note,
                        actor_id,
                        self._now(),
                    ),
                )
                edge_id = int(cursor.lastrowid)
                self._audit(
                    "connectivity",
                    str(edge_id),
                    "catalog.connectivity_created",
                    actor_id,
                    {"from_group_id": edge.from_group_id, "to_group_id": edge.to_group_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("井组连通关系冲突或井组不存在") from exc
        return {"edge_id": edge_id, "from_group_id": edge.from_group_id, "to_group_id": edge.to_group_id}

    def register_cap(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        cap = CapInput.from_dict(raw)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO platform_caps(effective_from,liquid_cap,water_cap,created_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    cap.effective_from,
                    decimal_text(cap.liquid_cap),
                    decimal_text(cap.water_cap),
                    actor_id,
                    self._now(),
                ),
            )
            cap_id = int(cursor.lastrowid)
            self._audit(
                "platform_cap",
                str(cap_id),
                "catalog.cap_registered",
                actor_id,
                {"effective_from": cap.effective_from},
            )
        return {"cap_id": cap_id, "effective_from": cap.effective_from}

    def create_constraint_set(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        constraint_set = ConstraintSetInput.from_dict(raw)
        definition = constraint_set.definition()
        content_sha256 = digest(definition)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO constraint_sets(constraint_set_id,name,definition_json,content_sha256,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        constraint_set.constraint_set_id,
                        constraint_set.name,
                        canonical_json(definition),
                        content_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "constraint_set",
                    constraint_set.constraint_set_id,
                    "catalog.constraint_set_created",
                    actor_id,
                    {"sha256": content_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("注采约束集合编号或内容已经存在") from exc
        return {"constraint_set_id": constraint_set.constraint_set_id, "sha256": content_sha256}

    # ------------------------------------------------------------------
    # 测试版本与井况事件
    # ------------------------------------------------------------------

    def record_test(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "test.write")
        test = TestInput.from_dict(raw)
        well = self.connection.execute(
            "SELECT kind FROM wells WHERE well_id=?", (test.well_id,)
        ).fetchone()
        if well is None:
            raise NotFound("井不存在")
        content_sha256 = digest({
            "well_id": test.well_id,
            "version": test.version,
            "tested_at": test.tested_at,
            "liquid_rate": decimal_text(test.liquid_rate),
            "water_cut": decimal_text(test.water_cut),
            "injection_rate": decimal_text(test.injection_rate),
        })
        existing = self.connection.execute(
            "SELECT test_id,content_sha256 FROM well_tests WHERE well_id=? AND version=?",
            (test.well_id, test.version),
        ).fetchone()
        if existing is not None:
            if existing["content_sha256"] != content_sha256:
                raise Conflict("同一测试版本号对应了不同测试内容")
            return {"test_id": existing["test_id"], "well_id": test.well_id, "version": test.version, "replayed": True}
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO well_tests(well_id,version,tested_at,liquid_rate,water_cut,injection_rate,"
                    "content_sha256,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        test.well_id,
                        test.version,
                        test.tested_at,
                        decimal_text(test.liquid_rate),
                        decimal_text(test.water_cut),
                        decimal_text(test.injection_rate),
                        content_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                test_id = int(cursor.lastrowid)
                self._audit(
                    "well",
                    test.well_id,
                    "test.recorded",
                    actor_id,
                    {"test_id": test_id, "version": test.version, "sha256": content_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("测试版本并发冲突") from exc
        return {"test_id": test_id, "well_id": test.well_id, "version": test.version, "replayed": False}

    def report_event(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        event = EventInput.from_dict(raw)
        well = self.connection.execute(
            "SELECT well_id FROM wells WHERE well_id=?", (event.well_id,)
        ).fetchone()
        if well is None:
            raise NotFound("井不存在")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO well_events(well_id,event_type,effective_at,note,reported_by,reported_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    event.well_id,
                    event.event_type,
                    event.effective_at,
                    event.note,
                    actor_id,
                    self._now(),
                ),
            )
            event_id = int(cursor.lastrowid)
            self._audit(
                "well",
                event.well_id,
                f"event.{event.event_type}",
                actor_id,
                {"event_id": event_id, "effective_at": event.effective_at},
            )
        return {"event_id": event_id, "well_id": event.well_id, "event_type": event.event_type}

    # ------------------------------------------------------------------
    # 方案冻结
    # ------------------------------------------------------------------

    def _constraint_set(self, constraint_set_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM constraint_sets WHERE constraint_set_id=?", (constraint_set_id,)
        ).fetchone()
        if row is None:
            raise NotFound("注采约束集合不存在")
        return row

    def _snapshot(
        self,
        field_id: str,
        constraint_set: sqlite3.Row,
        horizon_starts_at: str,
        initial_targets: Mapping[str, str],
    ) -> dict[str, Any]:
        """把井、层系、井组连通、测试版本、含水趋势、注采约束和处理上限冻结成快照。"""

        definition = json.loads(constraint_set["definition_json"])
        validity_hours = int(definition["test_validity_hours"])
        now = self.clock.now()
        wells = self.connection.execute(
            "SELECT * FROM wells WHERE state='active' ORDER BY well_id"
        ).fetchall()
        if not wells:
            raise ValidationFailed("没有可用井，无法冻结方案")
        snapshot_wells: list[dict[str, Any]] = []
        for well in wells:
            tests = self.connection.execute(
                "SELECT * FROM well_tests WHERE well_id=? ORDER BY version DESC LIMIT 2",
                (well["well_id"],),
            ).fetchall()
            if not tests:
                raise ValidationFailed(f"井 {well['well_id']} 缺少测试版本，无法冻结方案")
            latest = tests[0]
            trend = Decimal("0")
            if len(tests) == 2:
                elapsed = Decimal(str((parse_utc(latest["tested_at"]) - parse_utc(tests[1]["tested_at"])).total_seconds()))
                if elapsed > 0:
                    days = elapsed / SECONDS_PER_DAY
                    trend = ((Decimal(latest["water_cut"]) - Decimal(tests[1]["water_cut"])) / days).quantize(
                        Decimal("0.000001")
                    )
            stale = parse_utc(latest["tested_at"]) + timedelta(hours=validity_hours) < now
            snapshot_wells.append({
                "well_id": well["well_id"],
                "kind": well["kind"],
                "group_id": well["group_id"],
                "layer_system_id": well["layer_system_id"],
                "min_liquid": well["min_liquid"],
                "max_liquid": well["max_liquid"],
                "max_water_cut": well["max_water_cut"],
                "min_injection": well["min_injection"],
                "max_injection": well["max_injection"],
                "test_version": latest["version"],
                "tested_at": latest["tested_at"],
                "test_liquid": latest["liquid_rate"],
                "test_water_cut": latest["water_cut"],
                "test_injection": latest["injection_rate"],
                "trend_per_day": decimal_text(trend),
                "stale_test": stale,
            })
        edges = [
            {
                "from_group_id": row["from_group_id"],
                "to_group_id": row["to_group_id"],
                "coefficient": row["coefficient"],
            }
            for row in self.connection.execute(
                "SELECT * FROM connectivity_edges ORDER BY from_group_id,to_group_id"
            ).fetchall()
        ]
        events = [
            {
                "event_id": row["event_id"],
                "well_id": row["well_id"],
                "event_type": row["event_type"],
                "effective_at": row["effective_at"],
            }
            for row in self.connection.execute(
                "SELECT * FROM well_events ORDER BY effective_at,event_id"
            ).fetchall()
        ]
        cap = self.connection.execute(
            "SELECT * FROM platform_caps WHERE effective_from<=? ORDER BY effective_from DESC,cap_id DESC LIMIT 1",
            (horizon_starts_at,),
        ).fetchone()
        if cap is None:
            raise ValidationFailed("没有覆盖方案起点的平台处理上限")
        return {
            "field_id": field_id,
            "frozen_at": utc_text(now),
            "horizon": {
                "starts_at": horizon_starts_at,
                "phase_hours": int(definition["phase_hours"]),
                "phase_count": int(definition["phase_count"]),
            },
            "constraints": {
                "constraint_set_id": constraint_set["constraint_set_id"],
                "content_sha256": constraint_set["content_sha256"],
                "test_validity_hours": validity_hours,
                "max_liquid_change_per_phase": definition["max_liquid_change_per_phase"],
                "increase_water_cut_limit": definition["increase_water_cut_limit"],
                "target_voidage_ratio": definition["target_voidage_ratio"],
            },
            "caps": {
                "cap_id": cap["cap_id"],
                "effective_from": cap["effective_from"],
                "liquid_cap": cap["liquid_cap"],
                "water_cap": cap["water_cap"],
            },
            "wells": snapshot_wells,
            "edges": edges,
            "events": events,
            "initial_targets": dict(initial_targets),
        }

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM wc_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def _next_version_no(self, field_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 AS next_no FROM plans WHERE field_id=?", (field_id,)
        ).fetchone()
        return int(row["next_no"])

    def _insert_plan(
        self,
        *,
        plan_id: str,
        field_id: str,
        version_no: int,
        constraint_set_id: str,
        snapshot: Mapping[str, Any],
        snapshot_sha256: str,
        phases: list[dict[str, Any]],
        supersedes_plan_id: str | None,
        recompute_reason: str | None,
        actor_id: str,
    ) -> None:
        horizon = snapshot["horizon"]
        self.connection.execute(
            "INSERT INTO plans(plan_id,field_id,version_no,state,horizon_starts_at,phase_hours,phase_count,"
            "constraint_set_id,snapshot_json,snapshot_sha256,supersedes_plan_id,recompute_reason,created_by,created_at) "
            "VALUES(?,?,?,'draft',?,?,?,?,?,?,?,?,?,?)",
            (
                plan_id,
                field_id,
                version_no,
                horizon["starts_at"],
                horizon["phase_hours"],
                horizon["phase_count"],
                constraint_set_id,
                canonical_json(snapshot),
                snapshot_sha256,
                supersedes_plan_id,
                recompute_reason,
                actor_id,
                self._now(),
            ),
        )
        for phase in phases:
            self.connection.execute(
                "INSERT INTO plan_phases(plan_id,phase_index,starts_at,ends_at,state,totals_json,"
                "executed_by,executed_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    phase["phase_index"],
                    phase["starts_at"],
                    phase["ends_at"],
                    phase.get("state", "pending"),
                    canonical_json(phase["totals"]),
                    phase.get("executed_by"),
                    phase.get("executed_at"),
                ),
            )
            for command in phase["commands"]:
                self.connection.execute(
                    "INSERT INTO well_commands(plan_id,phase_index,well_id,action,origin,target_liquid,"
                    "target_injection,projected_water_cut,oil_rate,water_rate,reasons_json,override_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id,
                        phase["phase_index"],
                        command["well_id"],
                        command["action"],
                        command["origin"],
                        command["target_liquid"],
                        command.get("target_injection"),
                        command.get("projected_water_cut"),
                        command["oil_rate"],
                        command["water_rate"],
                        canonical_json(command["reasons"]),
                        command.get("override_id"),
                    ),
                )

    def create_plan(self, actor_id: str, raw: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        self._require(actor_id, "plan.create")
        plan_input = PlanInput.from_dict(raw)
        if not idempotency_key.strip():
            raise ValidationFailed("缺少幂等键")
        request_digest = digest({"action": "create", **raw})
        scope = f"plan:{plan_input.field_id}"
        existing = self._idempotent_response(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        constraint_set = self._constraint_set(plan_input.constraint_set_id)
        snapshot = self._snapshot(plan_input.field_id, constraint_set, plan_input.horizon_starts_at, {})
        snapshot_sha256 = digest(snapshot)
        try:
            phases = plan_phases(snapshot)
        except InfeasiblePlan as exc:
            raise ValidationFailed(str(exc)) from exc
        response = {
            "plan_id": plan_input.plan_id,
            "field_id": plan_input.field_id,
            "version_no": self._next_version_no(plan_input.field_id),
            "state": "draft",
            "snapshot_sha256": snapshot_sha256,
            "phases": [
                {
                    "phase_index": phase["phase_index"],
                    "starts_at": phase["starts_at"],
                    "ends_at": phase["ends_at"],
                    "totals": phase["totals"],
                }
                for phase in phases
            ],
        }
        try:
            with transaction(self.connection, immediate=True):
                self._insert_plan(
                    plan_id=plan_input.plan_id,
                    field_id=plan_input.field_id,
                    version_no=response["version_no"],
                    constraint_set_id=constraint_set["constraint_set_id"],
                    snapshot=snapshot,
                    snapshot_sha256=snapshot_sha256,
                    phases=phases,
                    supersedes_plan_id=None,
                    recompute_reason=None,
                    actor_id=actor_id,
                )
                self.connection.execute(
                    "INSERT INTO wc_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (scope, idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit(
                    "plan",
                    plan_input.plan_id,
                    "plan.created",
                    actor_id,
                    {"version_no": response["version_no"], "snapshot_sha256": snapshot_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("方案编号或幂等键冲突") from exc
        return response

    # ------------------------------------------------------------------
    # 确认：并发确认只能有一个有效版本
    # ------------------------------------------------------------------

    def _plan_row(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("方案不存在")
        return row

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        with transaction(self.connection, immediate=True):
            plan = self._plan_row(plan_id)
            if plan["state"] != "draft" or plan["revision"] != expected_revision:
                raise InvalidState("方案不是当前草稿版本")
            active = self.connection.execute(
                "SELECT plan_id FROM plans WHERE field_id=? AND state IN ('confirmed','executing')",
                (plan["field_id"],),
            ).fetchone()
            if active is not None:
                if plan["supersedes_plan_id"] != active["plan_id"]:
                    raise Conflict("已存在其他有效版本，并发确认被拒绝")
                self.connection.execute(
                    "UPDATE plans SET state='superseded',revision=revision+1 WHERE plan_id=?",
                    (active["plan_id"],),
                )
                self.connection.execute(
                    "UPDATE override_requests SET status='void' "
                    "WHERE plan_id=? AND status IN ('pending','approved')",
                    (active["plan_id"],),
                )
                self._audit(
                    "plan",
                    active["plan_id"],
                    "plan.superseded",
                    actor_id,
                    {"superseded_by": plan_id},
                )
            elif plan["supersedes_plan_id"] is not None:
                raise Conflict("前序方案已被其他版本取代，并发确认被拒绝")
            cursor = self.connection.execute(
                "UPDATE plans SET state='confirmed',confirmed_by=?,confirmed_at=?,revision=revision+1 "
                "WHERE plan_id=? AND state='draft' AND revision=?",
                (actor_id, self._now(), plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise Conflict("并发确认冲突：有效版本已变化")
            self._audit(
                "plan",
                plan_id,
                "plan.confirmed",
                actor_id,
                {"version_no": plan["version_no"], "snapshot_sha256": plan["snapshot_sha256"]},
            )
        return {"plan_id": plan_id, "state": "confirmed", "revision": expected_revision + 1}

    # ------------------------------------------------------------------
    # 分阶段执行
    # ------------------------------------------------------------------

    def execute_phase(self, actor_id: str, plan_id: str, phase_index: int, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "phase.execute")
        with transaction(self.connection, immediate=True):
            plan = self._plan_row(plan_id)
            if plan["state"] not in ACTIVE_PLAN_STATES:
                raise InvalidState("方案不在有效执行状态")
            if plan["revision"] != expected_revision:
                raise InvalidState("方案版本已变化")
            phase = self.connection.execute(
                "SELECT * FROM plan_phases WHERE plan_id=? AND phase_index=?",
                (plan_id, phase_index),
            ).fetchone()
            if phase is None:
                raise NotFound("方案阶段不存在")
            previous_pending = self.connection.execute(
                "SELECT COUNT(*) AS pending FROM plan_phases WHERE plan_id=? AND phase_index<? AND state<>'executed'",
                (plan_id, phase_index),
            ).fetchone()["pending"]
            if previous_pending:
                raise InvalidState("必须按阶段顺序执行")
            cursor = self.connection.execute(
                "UPDATE plan_phases SET state='executed',executed_by=?,executed_at=? "
                "WHERE plan_id=? AND phase_index=? AND state='pending'",
                (actor_id, self._now(), plan_id, phase_index),
            )
            if cursor.rowcount != 1:
                raise InvalidState("阶段已执行，已执行指令不被回写")
            self.connection.execute(
                "UPDATE plans SET state='executing',revision=revision+1 WHERE plan_id=? AND state='confirmed'",
                (plan_id,),
            )
            remaining = self.connection.execute(
                "SELECT COUNT(*) AS pending FROM plan_phases WHERE plan_id=? AND state='pending'",
                (plan_id,),
            ).fetchone()["pending"]
            if not remaining:
                self.connection.execute(
                    "UPDATE plans SET state='completed',revision=revision+1 WHERE plan_id=?",
                    (plan_id,),
                )
            self._audit(
                "plan",
                plan_id,
                "phase.executed",
                actor_id,
                {"phase_index": phase_index},
            )
        return self.get_plan(actor_id, plan_id)

    # ------------------------------------------------------------------
    # 重算：只重算未来时段，已执行指令不被回写
    # ------------------------------------------------------------------

    def recompute_plan(self, actor_id: str, raw: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        self._require(actor_id, "plan.recompute")
        recompute = RecomputeInput.from_dict(raw)
        if not idempotency_key.strip():
            raise ValidationFailed("缺少幂等键")
        source = self._plan_row(recompute.source_plan_id)
        if source["state"] not in ACTIVE_PLAN_STATES:
            raise InvalidState("只有有效版本可以触发重算")
        request_digest = digest({"action": "recompute", **raw})
        scope = f"replan:{source['plan_id']}"
        existing = self._idempotent_response(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        executed = self.connection.execute(
            "SELECT * FROM plan_phases WHERE plan_id=? AND state='executed' ORDER BY phase_index",
            (source["plan_id"],),
        ).fetchall()
        if len(executed) == source["phase_count"]:
            raise InvalidState("方案已全部执行，无需重算")
        skip_before = int(executed[-1]["phase_index"]) + 1 if executed else 0
        initial_targets: dict[str, str] = {}
        if executed:
            last_commands = self.connection.execute(
                "SELECT well_id,target_liquid FROM well_commands WHERE plan_id=? AND phase_index=?",
                (source["plan_id"], executed[-1]["phase_index"]),
            ).fetchall()
            initial_targets = {row["well_id"]: row["target_liquid"] for row in last_commands}
        constraint_set = self._constraint_set(source["constraint_set_id"])
        snapshot = self._snapshot(
            source["field_id"], constraint_set, source["horizon_starts_at"], initial_targets
        )
        snapshot_sha256 = digest(snapshot)
        try:
            future_phases = plan_phases(snapshot, skip_before=skip_before)
        except InfeasiblePlan as exc:
            raise ValidationFailed(str(exc)) from exc
        inherited_phases: list[dict[str, Any]] = []
        for phase in executed:
            commands = self.connection.execute(
                "SELECT * FROM well_commands WHERE plan_id=? AND phase_index=? ORDER BY well_id",
                (source["plan_id"], phase["phase_index"]),
            ).fetchall()
            inherited_phases.append({
                "phase_index": phase["phase_index"],
                "starts_at": phase["starts_at"],
                "ends_at": phase["ends_at"],
                "state": "executed",
                "executed_by": phase["executed_by"],
                "executed_at": phase["executed_at"],
                "totals": json.loads(phase["totals_json"]),
                "commands": [
                    {
                        "well_id": row["well_id"],
                        "action": row["action"],
                        "origin": "inherited",
                        "target_liquid": row["target_liquid"],
                        "target_injection": row["target_injection"],
                        "projected_water_cut": row["projected_water_cut"],
                        "oil_rate": row["oil_rate"],
                        "water_rate": row["water_rate"],
                        "override_id": row["override_id"],
                        "reasons": json.loads(row["reasons_json"]) + [{
                            "code": "inherited_executed",
                            "message": "已执行指令原样继承，不重算不回写",
                            "detail": {"source_plan_id": source["plan_id"]},
                        }],
                    }
                    for row in commands
                ],
            })
        phases = sorted(inherited_phases + future_phases, key=lambda item: item["phase_index"])
        version_no = self._next_version_no(source["field_id"])
        response = {
            "plan_id": recompute.plan_id,
            "field_id": source["field_id"],
            "version_no": version_no,
            "state": "draft",
            "snapshot_sha256": snapshot_sha256,
            "supersedes_plan_id": source["plan_id"],
            "inherited_phases": [phase["phase_index"] for phase in inherited_phases],
            "recomputed_phases": [phase["phase_index"] for phase in future_phases],
        }
        try:
            with transaction(self.connection, immediate=True):
                self._insert_plan(
                    plan_id=recompute.plan_id,
                    field_id=source["field_id"],
                    version_no=version_no,
                    constraint_set_id=source["constraint_set_id"],
                    snapshot=snapshot,
                    snapshot_sha256=snapshot_sha256,
                    phases=phases,
                    supersedes_plan_id=source["plan_id"],
                    recompute_reason=recompute.reason,
                    actor_id=actor_id,
                )
                self.connection.execute(
                    "INSERT INTO wc_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (scope, idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit(
                    "plan",
                    recompute.plan_id,
                    "plan.recomputed",
                    actor_id,
                    {
                        "source_plan_id": source["plan_id"],
                        "reason": recompute.reason,
                        "inherited_phases": response["inherited_phases"],
                        "recomputed_phases": response["recomputed_phases"],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("方案编号或幂等键冲突") from exc
        return response

    # ------------------------------------------------------------------
    # 人工覆盖：需要期限和双人批准
    # ------------------------------------------------------------------

    def _snapshot_wells(self, plan: sqlite3.Row) -> dict[str, dict[str, Any]]:
        snapshot = json.loads(plan["snapshot_json"])
        return {well["well_id"]: well for well in snapshot["wells"]}

    def request_override(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "override.request")
        override = OverrideInput.from_dict(raw)
        plan = self._plan_row(override.plan_id)
        if plan["state"] not in ACTIVE_PLAN_STATES:
            raise InvalidState("只有有效版本可以人工覆盖")
        phase = self.connection.execute(
            "SELECT state FROM plan_phases WHERE plan_id=? AND phase_index=?",
            (override.plan_id, override.phase_index),
        ).fetchone()
        if phase is None:
            raise NotFound("方案阶段不存在")
        if phase["state"] != "pending":
            raise InvalidState("已执行阶段不能人工覆盖，已执行指令不被回写")
        wells = self._snapshot_wells(plan)
        well = wells.get(override.well_id)
        if well is None:
            raise NotFound("井不在方案快照中")
        if parse_utc(override.expires_at) <= self.clock.now():
            raise ValidationFailed("人工覆盖期限必须晚于当前时间")
        if well["kind"] == "producer":
            if override.target_liquid is None or override.target_injection is not None:
                raise ValidationFailed("生产井覆盖必须且只能提供 target_liquid")
            if not Decimal(well["min_liquid"]) <= override.target_liquid <= Decimal(well["max_liquid"]):
                raise ValidationFailed("覆盖产液超出单井上下限")
        else:
            if override.target_injection is None or override.target_liquid is not None:
                raise ValidationFailed("注水井覆盖必须且只能提供 target_injection")
            if not Decimal(well["min_injection"]) <= override.target_injection <= Decimal(well["max_injection"]):
                raise ValidationFailed("覆盖注入量超出单井上下限")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO override_requests(plan_id,phase_index,well_id,target_liquid,target_injection,"
                "reason,expires_at,requested_by,requested_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    override.plan_id,
                    override.phase_index,
                    override.well_id,
                    None if override.target_liquid is None else decimal_text(override.target_liquid),
                    None if override.target_injection is None else decimal_text(override.target_injection),
                    override.reason,
                    override.expires_at,
                    actor_id,
                    self._now(),
                ),
            )
            override_id = int(cursor.lastrowid)
            self._audit(
                "override",
                str(override_id),
                "override.requested",
                actor_id,
                {
                    "plan_id": override.plan_id,
                    "phase_index": override.phase_index,
                    "well_id": override.well_id,
                    "expires_at": override.expires_at,
                },
            )
        return {"override_id": override_id, "status": "pending"}

    def _override_row(self, override_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM override_requests WHERE override_id=?", (override_id,)
        ).fetchone()
        if row is None:
            raise NotFound("人工覆盖不存在")
        return row

    def _override_approvers(self, override_id: int) -> list[str]:
        rows = self.connection.execute(
            "SELECT approver_id FROM override_approvals WHERE override_id=? ORDER BY approver_id",
            (override_id,),
        ).fetchall()
        return [row["approver_id"] for row in rows]

    def approve_override(self, actor_id: str, override_id: int) -> dict[str, Any]:
        self._require(actor_id, "override.approve")
        override = self._override_row(override_id)
        if override["status"] != "pending":
            raise InvalidState("人工覆盖不在待批准状态")
        if override["requested_by"] == actor_id:
            raise Forbidden("申请人不能批准自己的人工覆盖")
        if parse_utc(override["expires_at"]) <= self.clock.now():
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE override_requests SET status='expired' WHERE override_id=? AND status='pending'",
                    (override_id,),
                )
            raise InvalidState("人工覆盖期限已过")
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO override_approvals(override_id,approver_id,approved_at) VALUES(?,?,?)",
                    (override_id, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("同一批准人不能重复批准") from exc
            approvers = self._override_approvers(override_id)
            status = "pending"
            if len(approvers) >= 2:
                status = "approved"
                self.connection.execute(
                    "UPDATE override_requests SET status='approved' WHERE override_id=? AND status='pending'",
                    (override_id,),
                )
                self._audit(
                    "override",
                    str(override_id),
                    "override.approved",
                    actor_id,
                    {"approvers": approvers, "expires_at": override["expires_at"]},
                )
        return {"override_id": override_id, "status": status, "approvers": approvers}

    def apply_override(self, actor_id: str, override_id: int) -> dict[str, Any]:
        self._require(actor_id, "override.request")
        override = self._override_row(override_id)
        if override["status"] != "approved":
            raise InvalidState("人工覆盖未获双人批准")
        if parse_utc(override["expires_at"]) <= self.clock.now():
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE override_requests SET status='expired' WHERE override_id=? AND status='approved'",
                    (override_id,),
                )
            raise InvalidState("人工覆盖期限已过")
        plan = self._plan_row(override["plan_id"])
        phase = self.connection.execute(
            "SELECT * FROM plan_phases WHERE plan_id=? AND phase_index=?",
            (override["plan_id"], override["phase_index"]),
        ).fetchone()
        if plan["state"] not in ACTIVE_PLAN_STATES or phase["state"] != "pending":
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE override_requests SET status='void' WHERE override_id=? AND status='approved'",
                    (override_id,),
                )
            if plan["state"] not in ACTIVE_PLAN_STATES:
                raise InvalidState("方案已失效，人工覆盖作废")
            raise InvalidState("阶段已执行，已执行指令不被回写")
        with transaction(self.connection, immediate=True):
            current = self.connection.execute(
                "SELECT status FROM override_requests WHERE override_id=?", (override_id,)
            ).fetchone()
            if current["status"] != "approved":
                raise InvalidState("人工覆盖状态已变化")
            phase_state = self.connection.execute(
                "SELECT state FROM plan_phases WHERE plan_id=? AND phase_index=?",
                (override["plan_id"], override["phase_index"]),
            ).fetchone()
            if phase_state["state"] != "pending":
                raise InvalidState("阶段已执行，已执行指令不被回写")
            commands = self.connection.execute(
                "SELECT * FROM well_commands WHERE plan_id=? AND phase_index=? ORDER BY well_id",
                (override["plan_id"], override["phase_index"]),
            ).fetchall()
            wells = self._snapshot_wells(plan)
            well = wells[override["well_id"]]
            snapshot = json.loads(plan["snapshot_json"])
            caps = snapshot["caps"]
            liquid_cap = Decimal(caps["liquid_cap"])
            water_cap = Decimal(caps["water_cap"])
            voidage_ratio = Decimal(snapshot["constraints"]["target_voidage_ratio"])
            approvers = self._override_approvers(override_id)
            updated: list[dict[str, Any]] = []
            new_action = ""
            for row in commands:
                command = dict(row)
                command["reasons"] = json.loads(row["reasons_json"])
                if row["well_id"] == override["well_id"]:
                    previous = dict(command)
                    if well["kind"] == "producer":
                        target = quantize_rate(Decimal(override["target_liquid"]))
                        projected = Decimal(row["projected_water_cut"])
                        oil = quantize_rate(target * (Decimal(1) - projected))
                        command["target_liquid"] = decimal_text(target)
                        command["oil_rate"] = decimal_text(oil)
                        command["water_rate"] = decimal_text(quantize_rate(target - oil))
                        old = Decimal(row["target_liquid"])
                        new_action = "increase" if target > old else "restrict" if target < old else row["action"]
                    else:
                        command["target_injection"] = decimal_text(quantize_rate(Decimal(override["target_injection"])))
                        new_action = "inject"
                    command["action"] = new_action
                    command["origin"] = "manual_override"
                    command["override_id"] = override_id
                    command["reasons"] = command["reasons"] + [{
                        "code": "manual_override",
                        "message": "人工覆盖（双人批准，含期限）",
                        "detail": {
                            "override_id": override_id,
                            "expires_at": override["expires_at"],
                            "requested_by": override["requested_by"],
                            "approvers": approvers,
                            "reason": override["reason"],
                        },
                    }]
                    superseded = canonical_json({
                        key: previous[key]
                        for key in ("action", "origin", "target_liquid", "target_injection", "oil_rate", "water_rate")
                    })
                updated.append(command)
            totals = phase_totals(
                updated,
                liquid_cap=liquid_cap,
                water_cap=water_cap,
                target_voidage_ratio=voidage_ratio,
            )
            if Decimal(totals["liquid_total"]) > liquid_cap or Decimal(totals["water_total"]) > water_cap:
                raise Conflict("人工覆盖将突破平台处理上限")
            for command in updated:
                if command["well_id"] != override["well_id"]:
                    continue
                self.connection.execute(
                    "UPDATE well_commands SET action=?,origin='manual_override',target_liquid=?,"
                    "target_injection=?,oil_rate=?,water_rate=?,reasons_json=?,override_id=? "
                    "WHERE plan_id=? AND phase_index=? AND well_id=?",
                    (
                        command["action"],
                        command["target_liquid"],
                        command.get("target_injection"),
                        command["oil_rate"],
                        command["water_rate"],
                        canonical_json(command["reasons"]),
                        override_id,
                        override["plan_id"],
                        override["phase_index"],
                        override["well_id"],
                    ),
                )
            self.connection.execute(
                "UPDATE plan_phases SET totals_json=? WHERE plan_id=? AND phase_index=?",
                (canonical_json(totals), override["plan_id"], override["phase_index"]),
            )
            self.connection.execute(
                "UPDATE plans SET revision=revision+1 WHERE plan_id=?",
                (override["plan_id"],),
            )
            self.connection.execute(
                "UPDATE override_requests SET status='applied',applied_by=?,applied_at=?,"
                "superseded_command_json=? WHERE override_id=? AND status='approved'",
                (actor_id, self._now(), superseded, override_id),
            )
            self._audit(
                "override",
                str(override_id),
                "override.applied",
                actor_id,
                {
                    "plan_id": override["plan_id"],
                    "phase_index": override["phase_index"],
                    "well_id": override["well_id"],
                    "approvers": approvers,
                },
            )
        return {"override_id": override_id, "status": "applied", "action": new_action, "totals": totals}

    # ------------------------------------------------------------------
    # 解释与守恒核对
    # ------------------------------------------------------------------

    def get_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._user(actor_id)
        plan = self._plan_row(plan_id)
        phases = self.connection.execute(
            "SELECT * FROM plan_phases WHERE plan_id=? ORDER BY phase_index", (plan_id,)
        ).fetchall()
        overrides = self.connection.execute(
            "SELECT override_id,phase_index,well_id,status,expires_at FROM override_requests "
            "WHERE plan_id=? ORDER BY override_id",
            (plan_id,),
        ).fetchall()
        return {
            "plan_id": plan["plan_id"],
            "field_id": plan["field_id"],
            "version_no": plan["version_no"],
            "state": plan["state"],
            "revision": plan["revision"],
            "horizon_starts_at": plan["horizon_starts_at"],
            "phase_hours": plan["phase_hours"],
            "phase_count": plan["phase_count"],
            "constraint_set_id": plan["constraint_set_id"],
            "snapshot_sha256": plan["snapshot_sha256"],
            "supersedes_plan_id": plan["supersedes_plan_id"],
            "recompute_reason": plan["recompute_reason"],
            "phases": [
                {
                    "phase_index": row["phase_index"],
                    "starts_at": row["starts_at"],
                    "ends_at": row["ends_at"],
                    "state": row["state"],
                    "executed_by": row["executed_by"],
                    "executed_at": row["executed_at"],
                    "totals": json.loads(row["totals_json"]),
                }
                for row in phases
            ],
            "overrides": [dict(row) for row in overrides],
        }

    def explain_phase(self, actor_id: str, plan_id: str, phase_index: int) -> dict[str, Any]:
        """解释每口井在该阶段为何增产、限产或转入观察。"""

        self._user(actor_id)
        plan = self._plan_row(plan_id)
        phase = self.connection.execute(
            "SELECT * FROM plan_phases WHERE plan_id=? AND phase_index=?",
            (plan_id, phase_index),
        ).fetchone()
        if phase is None:
            raise NotFound("方案阶段不存在")
        wells = self._snapshot_wells(plan)
        snapshot = json.loads(plan["snapshot_json"])
        commands = self.connection.execute(
            "SELECT * FROM well_commands WHERE plan_id=? AND phase_index=? ORDER BY well_id",
            (plan_id, phase_index),
        ).fetchall()
        return {
            "plan_id": plan["plan_id"],
            "field_id": plan["field_id"],
            "version_no": plan["version_no"],
            "plan_state": plan["state"],
            "snapshot_sha256": plan["snapshot_sha256"],
            "phase": {
                "phase_index": phase["phase_index"],
                "starts_at": phase["starts_at"],
                "ends_at": phase["ends_at"],
                "state": phase["state"],
                "totals": json.loads(phase["totals_json"]),
            },
            "caps": snapshot["caps"],
            "wells": [
                {
                    "well_id": row["well_id"],
                    "kind": wells[row["well_id"]]["kind"],
                    "group_id": wells[row["well_id"]]["group_id"],
                    "layer_system_id": wells[row["well_id"]]["layer_system_id"],
                    "action": row["action"],
                    "action_label": ACTION_LABELS[row["action"]],
                    "origin": row["origin"],
                    "target_liquid": row["target_liquid"],
                    "target_injection": row["target_injection"],
                    "projected_water_cut": row["projected_water_cut"],
                    "oil_rate": row["oil_rate"],
                    "water_rate": row["water_rate"],
                    "override_id": row["override_id"],
                    "reasons": json.loads(row["reasons_json"]),
                }
                for row in commands
            ],
        }

    def check_conservation(self, actor_id: str, plan_id: str, phase_index: int) -> dict[str, Any]:
        """核对该阶段每口井与平台整体的油、水与处理能力守恒。"""

        self._user(actor_id)
        plan = self._plan_row(plan_id)
        phase = self.connection.execute(
            "SELECT state FROM plan_phases WHERE plan_id=? AND phase_index=?",
            (plan_id, phase_index),
        ).fetchone()
        if phase is None:
            raise NotFound("方案阶段不存在")
        snapshot = json.loads(plan["snapshot_json"])
        wells = self._snapshot_wells(plan)
        caps = snapshot["caps"]
        commands = self.connection.execute(
            "SELECT * FROM well_commands WHERE plan_id=? AND phase_index=? ORDER BY well_id",
            (plan_id, phase_index),
        ).fetchall()
        report = conservation_report(
            commands=[dict(row) for row in commands],
            wells=wells,
            liquid_cap=Decimal(caps["liquid_cap"]),
            water_cap=Decimal(caps["water_cap"]),
            target_voidage_ratio=Decimal(snapshot["constraints"]["target_voidage_ratio"]),
        )
        return {
            "plan_id": plan["plan_id"],
            "version_no": plan["version_no"],
            "phase_index": phase_index,
            "phase_state": phase["state"],
            **report,
        }

    # ------------------------------------------------------------------
    # 审计链
    # ------------------------------------------------------------------

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM wc_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
