"""稳油控水协同的事务用例：目录冻结、方案确认、分阶段执行、重算与人工覆盖。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .coordination import (
    ALGORITHM_VERSION,
    canonical_json,
    compute_stage,
    decimal_text,
    digest,
    projected_water_cut,
    quantize_rate,
    reconcile_stage,
    water_cut_trend,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    EVENT_TYPES,
    CapacityProfile,
    ConnectivityLink,
    LayerSeries,
    OverrideRequest,
    PlanRequest,
    RateConstraint,
    Well,
    WellGroup,
    WellTest,
    identifier,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "engineer": {
        "catalog.write", "test.import", "event.write", "plan.write", "plan.confirm",
        "stage.execute", "override.request", "override.apply", "replan.run", "report.read",
    },
    "approver": {"override.approve", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

REPLAN_TRIGGERS = {"manual", "well_shutdown", "late_test"}


class WaterControlService:
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

    # ------------------------------------------------------------------
    # 目录：井组、层系、井、连通、注采约束、平台处理上限
    # ------------------------------------------------------------------

    def create_group(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        group = WellGroup.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO well_groups(group_id,field_id,name,created_at) VALUES(?,?,?,?)",
                    (group.group_id, group.field_id, group.name, self._now()),
                )
                self._audit("well_group", group.group_id, "group.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("井组编号已经存在") from exc
        return dict(raw)

    def create_layer(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        layer = LayerSeries.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO layer_series(layer_id,group_id,name,created_at) VALUES(?,?,?,?)",
                    (layer.layer_id, layer.group_id, layer.name, self._now()),
                )
                self._audit("layer_series", layer.layer_id, "layer.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("层系编号冲突或井组不存在") from exc
        return dict(raw)

    def create_well(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        well = Well.from_dict(raw)
        state = "injecting" if well.kind == "injector" else "producing"
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO wells(well_id,group_id,layer_id,name,kind,state,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (well.well_id, well.group_id, well.layer_id, well.name, well.kind, state, self._now()),
                )
                self._audit("well", well.well_id, "well.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("井编号冲突或层系不存在") from exc
        return self.well(well.well_id)

    def well(self, well_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM wells WHERE well_id=?", (well_id,)).fetchone()
        if row is None:
            raise NotFound("井不存在")
        return dict(row)

    def create_link(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        link = ConnectivityLink.from_dict(raw)
        injector = self.well(link.injector_id)
        producer = self.well(link.producer_id)
        if injector["kind"] != "injector" or producer["kind"] != "producer":
            raise ValidationFailed("连通关系必须由注水井指向生产井")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO connectivity_links(link_id,injector_id,producer_id,coefficient_percent,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        link.link_id,
                        link.injector_id,
                        link.producer_id,
                        decimal_text(link.coefficient),
                        self._now(),
                    ),
                )
                self._audit("connectivity_link", link.link_id, "link.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("连通关系编号冲突或井间连通已登记") from exc
        return dict(raw)

    def upsert_constraint(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        constraint = RateConstraint.from_dict(raw)
        self.well(constraint.well_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO rate_constraints(constraint_id,well_id,min_rate,max_rate,effective_from,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        constraint.constraint_id,
                        constraint.well_id,
                        decimal_text(constraint.min_rate),
                        decimal_text(constraint.max_rate),
                        constraint.effective_from,
                        self._now(),
                    ),
                )
                self._audit("rate_constraint", constraint.constraint_id, "constraint.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("注采约束编号已经存在") from exc
        return dict(raw)

    def add_capacity_profile(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        profile = CapacityProfile.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO capacity_profiles(profile_id,field_id,liquid_limit,water_limit,effective_from,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        profile.profile_id,
                        profile.field_id,
                        decimal_text(profile.liquid_limit),
                        decimal_text(profile.water_limit),
                        profile.effective_from,
                        self._now(),
                    ),
                )
                self._audit("capacity_profile", profile.profile_id, "capacity.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("平台处理上限版本已经存在") from exc
        return dict(raw)

    # ------------------------------------------------------------------
    # 测试版本与井事件
    # ------------------------------------------------------------------

    def _field_of_well(self, well_id: str) -> str:
        row = self.connection.execute(
            "SELECT g.field_id FROM wells w JOIN well_groups g ON g.group_id=w.group_id WHERE w.well_id=?",
            (well_id,),
        ).fetchone()
        if row is None:
            raise NotFound("井不存在")
        return row["field_id"]

    def record_test(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "test.import")
        test = WellTest.from_dict(raw)
        field_id = self._field_of_well(test.well_id)
        test_date = test.tested_at[:10]
        late_row = self.connection.execute(
            "SELECT count(*) AS late_count FROM plan_instructions i JOIN plans p ON p.plan_id=i.plan_id "
            "WHERE p.field_id=? AND p.state IN ('confirmed','superseded','completed') "
            "AND i.well_id=? AND i.state='executed' AND i.stage_date>=?",
            (field_id, test.well_id, test_date),
        ).fetchone()
        late = bool(late_row["late_count"])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO well_tests(test_id,well_id,tested_at,liquid_rate,water_cut_percent,"
                    "source_revision,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        test.test_id,
                        test.well_id,
                        test.tested_at,
                        decimal_text(test.liquid_rate),
                        decimal_text(test.water_cut_percent),
                        test.source_revision,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "well_test", test.test_id, "test.recorded", actor_id,
                    {"well_id": test.well_id, "tested_at": test.tested_at, "late": late},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一井同一测试时刻的来源修订已登记") from exc
        return {"test_id": test.test_id, "well_id": test.well_id, "late": late}

    def report_event(self, actor_id: str, well_id: str, event_type: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        well = self.well(well_id)
        if event_type not in EVENT_TYPES:
            raise ValidationFailed("event_type 必须是 shutdown、restore 或 observation")
        if event_type == "shutdown":
            state = "shut"
        elif event_type == "restore":
            state = "injecting" if well["kind"] == "injector" else "producing"
        else:
            state = "observation"
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO well_events(well_id,event_type,note,created_by,created_at) VALUES(?,?,?,?,?)",
                (well_id, event_type, note or "", actor_id, self._now()),
            )
            self.connection.execute(
                "UPDATE wells SET state=?,revision=revision+1 WHERE well_id=?",
                (state, well_id),
            )
            event_id = int(cursor.lastrowid)
            self._audit("well", well_id, f"well.{event_type}", actor_id, {"event_id": event_id, "state": state})
        return {"event_id": event_id, "well_id": well_id, "state": state}

    # ------------------------------------------------------------------
    # 方案：冻结快照、确认、分阶段执行、重算
    # ------------------------------------------------------------------

    def _snapshot(self, field_id: str, horizon_start: str) -> dict[str, Any]:
        groups = self.connection.execute(
            "SELECT * FROM well_groups WHERE field_id=? ORDER BY group_id", (field_id,)
        ).fetchall()
        if not groups:
            raise InvalidState("油田没有登记井组")
        group_ids = [row["group_id"] for row in groups]
        marks = ",".join("?" for _ in group_ids)
        wells = self.connection.execute(
            f"SELECT * FROM wells WHERE group_id IN ({marks}) ORDER BY well_id", group_ids
        ).fetchall()
        if not wells:
            raise InvalidState("油田没有登记井")
        well_ids = [row["well_id"] for row in wells]
        well_marks = ",".join("?" for _ in well_ids)
        test_rows = self.connection.execute(
            f"SELECT * FROM well_tests WHERE well_id IN ({well_marks}) ORDER BY well_id,tested_at,test_id",
            well_ids,
        ).fetchall()
        tests_by_well: dict[str, list[sqlite3.Row]] = {}
        for row in test_rows:
            tests_by_well.setdefault(row["well_id"], []).append(row)
        tests: dict[str, Any] = {}
        trends: dict[str, Any] = {}
        for well_id, rows in tests_by_well.items():
            latest = rows[-1]
            tests[well_id] = {
                "test_id": latest["test_id"],
                "tested_at": latest["tested_at"],
                "liquid_rate": latest["liquid_rate"],
                "water_cut_percent": latest["water_cut_percent"],
                "source_revision": latest["source_revision"],
            }
            trends[well_id] = water_cut_trend(rows)
        constraint_rows = self.connection.execute(
            f"SELECT * FROM rate_constraints WHERE well_id IN ({well_marks}) AND effective_from<=? "
            "ORDER BY well_id,effective_from DESC,constraint_id DESC",
            (*well_ids, horizon_start),
        ).fetchall()
        constraints: dict[str, Any] = {}
        for row in constraint_rows:
            constraints.setdefault(row["well_id"], {
                "constraint_id": row["constraint_id"],
                "min_rate": row["min_rate"],
                "max_rate": row["max_rate"],
            })
        link_rows = self.connection.execute(
            f"SELECT * FROM connectivity_links WHERE injector_id IN ({well_marks}) "
            f"AND producer_id IN ({well_marks}) ORDER BY link_id",
            (*well_ids, *well_ids),
        ).fetchall()
        capacity = self.connection.execute(
            "SELECT * FROM capacity_profiles WHERE field_id=? AND effective_from<=? "
            "ORDER BY effective_from DESC,profile_id DESC LIMIT 1",
            (field_id, horizon_start),
        ).fetchone()
        if capacity is None:
            raise InvalidState("油田没有可用平台处理上限")
        return {
            "field_id": field_id,
            "horizon_start": horizon_start,
            "wells": [
                {
                    "well_id": row["well_id"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "state": row["state"],
                    "group_id": row["group_id"],
                    "layer_id": row["layer_id"],
                }
                for row in wells
            ],
            "tests": tests,
            "trends": trends,
            "constraints": constraints,
            "links": [
                {
                    "link_id": row["link_id"],
                    "injector_id": row["injector_id"],
                    "producer_id": row["producer_id"],
                    "coefficient_percent": row["coefficient_percent"],
                }
                for row in link_rows
            ],
            "capacity": {
                "profile_id": capacity["profile_id"],
                "liquid_limit": capacity["liquid_limit"],
                "water_limit": capacity["water_limit"],
                "effective_from": capacity["effective_from"],
            },
        }

    def _insert_plan(
        self,
        *,
        plan_id: str,
        field_id: str,
        horizon_start: str,
        stage_count: int,
        trigger: str,
        base_plan_id: str | None,
        snapshot: Mapping[str, Any],
        parameters: Mapping[str, Any],
        actor_id: str,
    ) -> str:
        input_sha256 = digest(snapshot)
        self.connection.execute(
            "INSERT INTO plans(plan_id,field_id,horizon_start,stage_count,state,trigger,base_plan_id,"
            "input_sha256,snapshot_json,parameters_json,created_by,created_at) "
            "VALUES(?,?,?,?,'draft',?,?,?,?,?,?,?)",
            (
                plan_id,
                field_id,
                horizon_start,
                stage_count,
                trigger,
                base_plan_id,
                input_sha256,
                canonical_json(snapshot),
                canonical_json(parameters),
                actor_id,
                self._now(),
            ),
        )
        carried = self._executed_instructions(base_plan_id) if base_plan_id else {}
        start = parse_utc(f"{horizon_start}T00:00:00Z", "horizon_start")
        for stage_index in range(stage_count):
            stage_date = (start + timedelta(days=stage_index)).date().isoformat()
            if stage_index in carried:
                stage_row, rows = carried[stage_index]
                self.connection.execute(
                    "INSERT INTO plan_stages(plan_id,stage_index,stage_date,state,executed_by,executed_at) "
                    "VALUES(?,?,?,'executed',?,?)",
                    (plan_id, stage_index, stage_date, stage_row["executed_by"], stage_row["executed_at"]),
                )
                for row in rows:
                    self.connection.execute(
                        "INSERT INTO plan_instructions(plan_id,stage_index,stage_date,well_id,action,"
                        "target_liquid_rate,target_oil_rate,target_water_rate,target_injection_rate,"
                        "reasons_json,state,carried_from_plan_id,executed_by,executed_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,'executed',?,?,?)",
                        (
                            plan_id,
                            stage_index,
                            stage_date,
                            row["well_id"],
                            row["action"],
                            row["target_liquid_rate"],
                            row["target_oil_rate"],
                            row["target_water_rate"],
                            row["target_injection_rate"],
                            row["reasons_json"],
                            base_plan_id,
                            row["executed_by"],
                            row["executed_at"],
                        ),
                    )
            else:
                self.connection.execute(
                    "INSERT INTO plan_stages(plan_id,stage_index,stage_date,state) VALUES(?,?,?,'pending')",
                    (plan_id, stage_index, stage_date),
                )
                for instruction in compute_stage(snapshot, stage_date):
                    self.connection.execute(
                        "INSERT INTO plan_instructions(plan_id,stage_index,stage_date,well_id,action,"
                        "target_liquid_rate,target_oil_rate,target_water_rate,target_injection_rate,"
                        "reasons_json,state) VALUES(?,?,?,?,?,?,?,?,?,?,'pending')",
                        (
                            plan_id,
                            stage_index,
                            stage_date,
                            instruction["well_id"],
                            instruction["action"],
                            instruction["target_liquid_rate"],
                            instruction["target_oil_rate"],
                            instruction["target_water_rate"],
                            instruction["target_injection_rate"],
                            canonical_json(instruction["reasons"]),
                        ),
                    )
        return input_sha256

    def _executed_instructions(self, plan_id: str | None) -> dict[int, tuple[sqlite3.Row, list[sqlite3.Row]]]:
        if plan_id is None:
            return {}
        stages = self.connection.execute(
            "SELECT * FROM plan_stages WHERE plan_id=? AND state='executed' ORDER BY stage_index",
            (plan_id,),
        ).fetchall()
        result: dict[int, tuple[sqlite3.Row, list[sqlite3.Row]]] = {}
        for stage in stages:
            rows = self.connection.execute(
                "SELECT * FROM plan_instructions WHERE plan_id=? AND stage_index=? AND state='executed' "
                "ORDER BY well_id",
                (plan_id, stage["stage_index"]),
            ).fetchall()
            result[stage["stage_index"]] = (stage, list(rows))
        return result

    def create_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        request = PlanRequest.from_dict(raw)
        parameters = {
            "observe_threshold_percent": decimal_text(request.observe_threshold_percent),
            "limit_threshold_percent": decimal_text(request.limit_threshold_percent),
            "increase_threshold_percent": decimal_text(request.increase_threshold_percent),
            "increase_cap_percent": decimal_text(request.increase_cap_percent),
            "limit_cut_percent": decimal_text(request.limit_cut_percent),
        }
        snapshot = self._snapshot(request.field_id, request.horizon_start)
        snapshot["parameters"] = parameters
        try:
            with transaction(self.connection, immediate=True):
                input_sha256 = self._insert_plan(
                    plan_id=request.plan_id,
                    field_id=request.field_id,
                    horizon_start=request.horizon_start,
                    stage_count=request.stage_count,
                    trigger="manual",
                    base_plan_id=None,
                    snapshot=snapshot,
                    parameters=parameters,
                    actor_id=actor_id,
                )
                self._audit(
                    "plan", request.plan_id, "plan.created", actor_id,
                    {"field_id": request.field_id, "input_sha256": input_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("方案编号已经存在") from exc
        return {
            "plan_id": request.plan_id,
            "field_id": request.field_id,
            "state": "draft",
            "revision": 1,
            "input_sha256": input_sha256,
            "algorithm_version": ALGORITHM_VERSION,
        }

    def plan(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("方案不存在")
        return row

    def confirm_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        plan = self.plan(plan_id)
        if plan["state"] != "draft" or plan["revision"] != expected_revision:
            raise InvalidState("方案不是当前草稿版本")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE plans SET state='superseded',revision=revision+1 "
                    "WHERE field_id=? AND state='confirmed'",
                    (plan["field_id"],),
                )
                cursor = self.connection.execute(
                    "UPDATE plans SET state='confirmed',confirmed_by=?,confirmed_at=?,revision=revision+1 "
                    "WHERE plan_id=? AND state='draft' AND revision=?",
                    (actor_id, self._now(), plan_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise Conflict("并发确认冲突，方案版本已变化")
                self._audit("plan", plan_id, "plan.confirmed", actor_id, {"field_id": plan["field_id"]})
        except sqlite3.IntegrityError as exc:
            raise Conflict("并发确认冲突：油田已存在有效方案") from exc
        return {"plan_id": plan_id, "state": "confirmed", "revision": expected_revision + 1}

    def execute_stage(self, actor_id: str, plan_id: str, stage_index: int, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "stage.execute")
        with transaction(self.connection, immediate=True):
            plan = self.plan(plan_id)
            if plan["state"] != "confirmed":
                raise InvalidState("只有已确认方案可以执行")
            if plan["revision"] != expected_revision:
                raise InvalidState("方案版本已变化，请重新读取")
            stage = self.connection.execute(
                "SELECT * FROM plan_stages WHERE plan_id=? AND stage_index=?",
                (plan_id, stage_index),
            ).fetchone()
            if stage is None:
                raise NotFound("方案阶段不存在")
            if stage["state"] != "pending":
                raise InvalidState("阶段已执行，不能重复执行")
            blocked = self.connection.execute(
                "SELECT count(*) AS pending FROM plan_stages WHERE plan_id=? AND stage_index<? AND state='pending'",
                (plan_id, stage_index),
            ).fetchone()
            if blocked["pending"]:
                raise InvalidState("必须按顺序执行阶段")
            today = self.clock.now().date().isoformat()
            if stage["stage_date"] > today:
                raise InvalidState("阶段未到执行日期")
            now = self._now()
            self.connection.execute(
                "UPDATE plan_stages SET state='executed',executed_by=?,executed_at=? "
                "WHERE plan_id=? AND stage_index=?",
                (actor_id, now, plan_id, stage_index),
            )
            cursor = self.connection.execute(
                "UPDATE plan_instructions SET state='executed',executed_by=?,executed_at=? "
                "WHERE plan_id=? AND stage_index=? AND state='pending'",
                (actor_id, now, plan_id, stage_index),
            )
            self.connection.execute(
                "UPDATE plans SET revision=revision+1 WHERE plan_id=?", (plan_id,)
            )
            self._audit(
                "plan", plan_id, "stage.executed", actor_id,
                {"stage_index": stage_index, "instructions": cursor.rowcount},
            )
        return {
            "plan_id": plan_id,
            "stage_index": stage_index,
            "state": "executed",
            "instructions": cursor.rowcount,
            "revision": expected_revision + 1,
        }

    def replan(self, actor_id: str, field_id: str, new_plan_id: str, trigger: str) -> dict[str, Any]:
        self._require(actor_id, "replan.run")
        if trigger not in REPLAN_TRIGGERS:
            raise ValidationFailed("trigger 必须是 manual、well_shutdown 或 late_test")
        new_plan_id = identifier(new_plan_id, "plan_id")
        base = self.connection.execute(
            "SELECT * FROM plans WHERE field_id=? AND state='confirmed'", (field_id,)
        ).fetchone()
        if base is None:
            raise InvalidState("油田没有已确认方案可以重算")
        parameters = json.loads(base["parameters_json"])
        snapshot = self._snapshot(field_id, base["horizon_start"])
        snapshot["parameters"] = parameters
        try:
            with transaction(self.connection, immediate=True):
                input_sha256 = self._insert_plan(
                    plan_id=new_plan_id,
                    field_id=field_id,
                    horizon_start=base["horizon_start"],
                    stage_count=int(base["stage_count"]),
                    trigger=trigger,
                    base_plan_id=base["plan_id"],
                    snapshot=snapshot,
                    parameters=parameters,
                    actor_id=actor_id,
                )
                carried = sorted(self._executed_instructions(base["plan_id"]))
                self._audit(
                    "plan", new_plan_id, "plan.replanned", actor_id,
                    {
                        "base_plan_id": base["plan_id"],
                        "trigger": trigger,
                        "carried_stages": carried,
                        "input_sha256": input_sha256,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("方案编号已经存在") from exc
        return {
            "plan_id": new_plan_id,
            "field_id": field_id,
            "state": "draft",
            "revision": 1,
            "base_plan_id": base["plan_id"],
            "trigger": trigger,
            "carried_stages": carried,
            "input_sha256": input_sha256,
        }

    # ------------------------------------------------------------------
    # 人工覆盖：期限 + 双人批准
    # ------------------------------------------------------------------

    def request_override(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "override.request")
        request = OverrideRequest.from_dict(raw)
        plan = self.plan(request.plan_id)
        if plan["state"] != "confirmed":
            raise InvalidState("只能覆盖当前有效方案")
        stage = self.connection.execute(
            "SELECT * FROM plan_stages WHERE plan_id=? AND stage_index=?",
            (request.plan_id, request.stage_index),
        ).fetchone()
        if stage is None:
            raise NotFound("方案阶段不存在")
        if stage["state"] != "pending":
            raise InvalidState("已执行指令不能被覆盖回写")
        instruction = self.connection.execute(
            "SELECT instruction_id FROM plan_instructions WHERE plan_id=? AND stage_index=? AND well_id=?",
            (request.plan_id, request.stage_index, request.well_id),
        ).fetchone()
        if instruction is None:
            raise NotFound("方案中没有该井的指令")
        if parse_utc(request.expires_at, "expires_at") <= self.clock.now():
            raise ValidationFailed("expires_at 必须晚于当前时间")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO override_requests(override_id,plan_id,stage_index,well_id,action,"
                    "target_liquid_rate,target_injection_rate,reason,expires_at,requested_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request.override_id,
                        request.plan_id,
                        request.stage_index,
                        request.well_id,
                        request.action,
                        None if request.target_liquid_rate is None else decimal_text(request.target_liquid_rate),
                        None if request.target_injection_rate is None else decimal_text(request.target_injection_rate),
                        request.reason,
                        request.expires_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "override", request.override_id, "override.requested", actor_id,
                    {"plan_id": request.plan_id, "well_id": request.well_id, "expires_at": request.expires_at},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("覆盖请求编号已经存在") from exc
        return {"override_id": request.override_id, "state": "pending", "approvals": 0}

    def _override(self, override_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM override_requests WHERE override_id=?", (override_id,)
        ).fetchone()
        if row is None:
            raise NotFound("覆盖请求不存在")
        return row

    def _expire_if_due(self, actor_id: str, request: sqlite3.Row) -> None:
        if parse_utc(request["expires_at"], "expires_at") <= self.clock.now():
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE override_requests SET state='expired' "
                    "WHERE override_id=? AND state IN ('pending','approved')",
                    (request["override_id"],),
                )
                self._audit("override", request["override_id"], "override.expired", actor_id, {})
            raise InvalidState("覆盖请求已过期限")

    def approve_override(self, actor_id: str, override_id: str) -> dict[str, Any]:
        self._require(actor_id, "override.approve")
        request = self._override(override_id)
        if request["requested_by"] == actor_id:
            raise Forbidden("申请人不能批准自己的覆盖请求")
        if request["state"] not in ("pending", "approved"):
            raise InvalidState("覆盖请求当前不能批准")
        self._expire_if_due(actor_id, request)
        with transaction(self.connection, immediate=True):
            request = self._override(override_id)
            if request["state"] not in ("pending", "approved"):
                raise InvalidState("覆盖请求当前不能批准")
            try:
                self.connection.execute(
                    "INSERT INTO override_approvals(override_id,approver_id,approved_at) VALUES(?,?,?)",
                    (override_id, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("同一批准人不能重复批准") from exc
            approvals = self.connection.execute(
                "SELECT count(*) AS total FROM override_approvals WHERE override_id=?",
                (override_id,),
            ).fetchone()["total"]
            state = request["state"]
            if approvals >= 2 and state == "pending":
                self.connection.execute(
                    "UPDATE override_requests SET state='approved' WHERE override_id=?",
                    (override_id,),
                )
                state = "approved"
                self._audit("override", override_id, "override.approved", actor_id, {"approvals": approvals})
            else:
                self._audit("override", override_id, "override.approval_added", actor_id, {"approvals": approvals})
        return {"override_id": override_id, "state": state, "approvals": approvals}

    def apply_override(self, actor_id: str, override_id: str) -> dict[str, Any]:
        self._require(actor_id, "override.apply")
        request = self._override(override_id)
        if request["state"] != "approved":
            raise InvalidState("覆盖请求未获双人批准")
        self._expire_if_due(actor_id, request)
        with transaction(self.connection, immediate=True):
            request = self._override(override_id)
            if request["state"] != "approved":
                raise InvalidState("覆盖请求未获双人批准")
            approvals = self.connection.execute(
                "SELECT count(DISTINCT approver_id) AS total FROM override_approvals WHERE override_id=?",
                (override_id,),
            ).fetchone()["total"]
            if approvals < 2:
                raise InvalidState("覆盖请求未获双人批准")
            plan = self.plan(request["plan_id"])
            if plan["state"] != "confirmed":
                raise InvalidState("方案已不是当前有效版本")
            stage = self.connection.execute(
                "SELECT * FROM plan_stages WHERE plan_id=? AND stage_index=?",
                (request["plan_id"], request["stage_index"]),
            ).fetchone()
            if stage is None or stage["state"] != "pending":
                raise InvalidState("已执行指令不能被覆盖回写")
            instruction = self.connection.execute(
                "SELECT * FROM plan_instructions WHERE plan_id=? AND stage_index=? AND well_id=?",
                (request["plan_id"], request["stage_index"], request["well_id"]),
            ).fetchone()
            if instruction is None:
                raise NotFound("方案中没有该井的指令")
            snapshot = json.loads(plan["snapshot_json"])
            updates: dict[str, Any] = {"action": request["action"]}
            if request["target_liquid_rate"] is not None:
                liquid = quantize_rate(Decimal(request["target_liquid_rate"]))
                water_cut = projected_water_cut(snapshot, request["well_id"], stage["stage_date"])
                water = quantize_rate(liquid * water_cut / Decimal(100))
                updates["target_liquid_rate"] = decimal_text(liquid)
                updates["target_water_rate"] = decimal_text(water)
                updates["target_oil_rate"] = decimal_text(quantize_rate(liquid - water))
            if request["target_injection_rate"] is not None:
                updates["target_injection_rate"] = decimal_text(
                    quantize_rate(Decimal(request["target_injection_rate"]))
                )
            reasons = json.loads(instruction["reasons_json"])
            reasons.append({"code": "manual_override", "override_id": override_id})
            assignments = ",".join(f"{key}=?" for key in (*updates, "reasons_json"))
            self.connection.execute(
                f"UPDATE plan_instructions SET {assignments} WHERE instruction_id=? AND state='pending'",
                (
                    *updates.values(),
                    canonical_json(reasons),
                    instruction["instruction_id"],
                ),
            )
            now = self._now()
            self.connection.execute(
                "UPDATE override_requests SET state='applied',applied_by=?,applied_at=? WHERE override_id=?",
                (actor_id, now, override_id),
            )
            self._audit(
                "override", override_id, "override.applied", actor_id,
                {"plan_id": request["plan_id"], "well_id": request["well_id"], "updates": updates},
            )
        return {"override_id": override_id, "state": "applied", "updates": updates}

    # ------------------------------------------------------------------
    # 解释与守恒核对
    # ------------------------------------------------------------------

    def explain_well(self, actor_id: str, plan_id: str, well_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        plan = self.plan(plan_id)
        snapshot = json.loads(plan["snapshot_json"])
        wells = {item["well_id"]: item for item in snapshot["wells"]}
        if well_id not in wells:
            raise NotFound("井不在方案快照中")
        rows = self.connection.execute(
            "SELECT * FROM plan_instructions WHERE plan_id=? AND well_id=? ORDER BY stage_index",
            (plan_id, well_id),
        ).fetchall()
        overrides = self.connection.execute(
            "SELECT override_id,stage_index,action,state,expires_at,requested_by,applied_by "
            "FROM override_requests WHERE plan_id=? AND well_id=? ORDER BY created_at,override_id",
            (plan_id, well_id),
        ).fetchall()
        links = [
            link for link in snapshot["links"]
            if link["injector_id"] == well_id or link["producer_id"] == well_id
        ]
        return {
            "plan_id": plan_id,
            "plan_state": plan["state"],
            "well_id": well_id,
            "algorithm_version": ALGORITHM_VERSION,
            "well": wells[well_id],
            "test": snapshot["tests"].get(well_id),
            "trend": snapshot["trends"].get(well_id),
            "constraint": snapshot["constraints"].get(well_id),
            "links": links,
            "capacity": snapshot["capacity"],
            "parameters": snapshot["parameters"],
            "stages": [
                {
                    "stage_index": row["stage_index"],
                    "stage_date": row["stage_date"],
                    "action": row["action"],
                    "target_liquid_rate": row["target_liquid_rate"],
                    "target_oil_rate": row["target_oil_rate"],
                    "target_water_rate": row["target_water_rate"],
                    "target_injection_rate": row["target_injection_rate"],
                    "reasons": json.loads(row["reasons_json"]),
                    "state": row["state"],
                    "carried_from_plan_id": row["carried_from_plan_id"],
                    "executed_by": row["executed_by"],
                    "executed_at": row["executed_at"],
                }
                for row in rows
            ],
            "overrides": [dict(row) for row in overrides],
        }

    def reconcile_plan(self, actor_id: str, plan_id: str, stage_index: int | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        plan = self.plan(plan_id)
        snapshot = json.loads(plan["snapshot_json"])
        capacity = snapshot["capacity"]
        if stage_index is None:
            stages = self.connection.execute(
                "SELECT * FROM plan_stages WHERE plan_id=? ORDER BY stage_index", (plan_id,)
            ).fetchall()
        else:
            stage = self.connection.execute(
                "SELECT * FROM plan_stages WHERE plan_id=? AND stage_index=?",
                (plan_id, stage_index),
            ).fetchone()
            if stage is None:
                raise NotFound("方案阶段不存在")
            stages = [stage]
        results = []
        for stage in stages:
            rows = self.connection.execute(
                "SELECT * FROM plan_instructions WHERE plan_id=? AND stage_index=? ORDER BY well_id",
                (plan_id, stage["stage_index"]),
            ).fetchall()
            balance = reconcile_stage(rows, capacity)
            results.append({
                "stage_index": stage["stage_index"],
                "stage_date": stage["stage_date"],
                "state": stage["state"],
                **balance,
            })
        return {
            "plan_id": plan_id,
            "plan_state": plan["state"],
            "conserved": all(item["conserved"] for item in results),
            "stages": results,
        }

    def plan_detail(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        plan = self.plan(plan_id)
        stages = self.connection.execute(
            "SELECT * FROM plan_stages WHERE plan_id=? ORDER BY stage_index", (plan_id,)
        ).fetchall()
        return {
            "plan_id": plan["plan_id"],
            "field_id": plan["field_id"],
            "state": plan["state"],
            "revision": plan["revision"],
            "trigger": plan["trigger"],
            "base_plan_id": plan["base_plan_id"],
            "horizon_start": plan["horizon_start"],
            "stage_count": plan["stage_count"],
            "input_sha256": plan["input_sha256"],
            "algorithm_version": ALGORITHM_VERSION,
            "created_by": plan["created_by"],
            "created_at": plan["created_at"],
            "confirmed_by": plan["confirmed_by"],
            "confirmed_at": plan["confirmed_at"],
            "stages": [dict(row) for row in stages],
        }

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
