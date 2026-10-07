from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from water_control.api import JsonApplication
from water_control.clock import FrozenClock
from water_control.coordination import compute_stage, reconcile_stage, water_cut_trend
from water_control.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from water_control.service import WaterControlService


PARAMETERS = {
    "observe_threshold_percent": "85",
    "limit_threshold_percent": "90",
    "increase_threshold_percent": "60",
    "increase_cap_percent": "20",
    "limit_cut_percent": "10",
}


def snapshot_fixture(**overrides) -> dict:
    snapshot = {
        "field_id": "liuhua",
        "horizon_start": "2026-10-07",
        "wells": [
            {"well_id": "i1", "name": "注水井i1", "kind": "injector", "state": "injecting", "group_id": "wg-east", "layer_id": "ls-1"},
            {"well_id": "p1", "name": "生产井p1", "kind": "producer", "state": "producing", "group_id": "wg-east", "layer_id": "ls-1"},
            {"well_id": "p2", "name": "生产井p2", "kind": "producer", "state": "producing", "group_id": "wg-east", "layer_id": "ls-1"},
        ],
        "tests": {
            "i1": {"test_id": "t-i1", "tested_at": "2026-10-05T08:00:00Z", "liquid_rate": "100", "water_cut_percent": "0", "source_revision": "r1"},
            "p1": {"test_id": "t-p1", "tested_at": "2026-10-05T08:00:00Z", "liquid_rate": "100", "water_cut_percent": "30", "source_revision": "r1"},
            "p2": {"test_id": "t-p2", "tested_at": "2026-10-05T08:00:00Z", "liquid_rate": "100", "water_cut_percent": "70", "source_revision": "r1"},
        },
        "trends": {
            "i1": {"slope_per_day": "0.0000", "direction": "flat", "samples": 1},
            "p1": {"slope_per_day": "0.0000", "direction": "flat", "samples": 1},
            "p2": {"slope_per_day": "0.0000", "direction": "flat", "samples": 1},
        },
        "constraints": {
            "i1": {"constraint_id": "rc-i1", "min_rate": "0", "max_rate": "150"},
            "p1": {"constraint_id": "rc-p1", "min_rate": "0", "max_rate": "200"},
            "p2": {"constraint_id": "rc-p2", "min_rate": "0", "max_rate": "200"},
        },
        "links": [
            {"link_id": "lk-1", "injector_id": "i1", "producer_id": "p1", "coefficient_percent": "50"},
            {"link_id": "lk-2", "injector_id": "i1", "producer_id": "p2", "coefficient_percent": "50"},
        ],
        "capacity": {"profile_id": "cap-1", "liquid_limit": "500", "water_limit": "300", "effective_from": "2026-10-01"},
        "parameters": dict(PARAMETERS),
    }
    for key, value in overrides.items():
        snapshot[key] = value
    return snapshot


class TrendTests(unittest.TestCase):
    def test_rising_trend_from_recent_tests(self) -> None:
        tests = [
            {"test_id": "t1", "tested_at": "2026-10-01T00:00:00Z", "water_cut_percent": "80"},
            {"test_id": "t2", "tested_at": "2026-10-03T00:00:00Z", "water_cut_percent": "82"},
            {"test_id": "t3", "tested_at": "2026-10-05T00:00:00Z", "water_cut_percent": "84"},
        ]
        trend = water_cut_trend(tests)
        self.assertEqual(trend["direction"], "rising")
        self.assertEqual(trend["slope_per_day"], "1.0000")
        self.assertEqual(trend["samples"], 3)

    def test_single_test_is_flat(self) -> None:
        trend = water_cut_trend([{"test_id": "t1", "tested_at": "2026-10-01T00:00:00Z", "water_cut_percent": "80"}])
        self.assertEqual(trend["direction"], "flat")
        self.assertEqual(trend["samples"], 1)

    def test_trend_uses_only_five_most_recent_tests(self) -> None:
        tests = [
            {"test_id": f"t{index}", "tested_at": f"2026-09-0{index}T00:00:00Z", "water_cut_percent": "90"}
            for index in range(1, 6)
        ]
        tests.append({"test_id": "t6", "tested_at": "2026-09-06T00:00:00Z", "water_cut_percent": "50"})
        trend = water_cut_trend(tests)
        self.assertEqual(trend["direction"], "falling")
        self.assertEqual(trend["samples"], 5)


class CoordinationTests(unittest.TestCase):
    def test_compute_stage_is_deterministic(self) -> None:
        snapshot = snapshot_fixture()
        first = compute_stage(snapshot, "2026-10-07")
        second = compute_stage(snapshot, "2026-10-07")
        self.assertEqual(first, second)

    def test_increase_respects_headroom_and_cap(self) -> None:
        snapshot = snapshot_fixture()
        rows = {row["well_id"]: row for row in compute_stage(snapshot, "2026-10-07")}
        self.assertEqual(rows["p1"]["action"], "increase")
        self.assertEqual(rows["p1"]["target_liquid_rate"], "120.000")
        self.assertEqual(rows["p2"]["action"], "maintain")
        self.assertEqual(rows["i1"]["target_injection_rate"], "110.000")
        reasons = [item["code"] for item in rows["p1"]["reasons"]]
        self.assertIn("capacity_headroom_allocated", reasons)

    def test_no_headroom_keeps_candidate_at_maintain(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["capacity"] = {"profile_id": "cap-1", "liquid_limit": "200", "water_limit": "300", "effective_from": "2026-10-01"}
        rows = {row["well_id"]: row for row in compute_stage(snapshot, "2026-10-07")}
        self.assertEqual(rows["p1"]["action"], "maintain")
        self.assertEqual(rows["p1"]["reasons"][0]["code"], "no_capacity_headroom")

    def test_injector_cap_scales_back_producer_increase(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["constraints"]["i1"] = {"constraint_id": "rc-i1", "min_rate": "0", "max_rate": "100"}
        rows = {row["well_id"]: row for row in compute_stage(snapshot, "2026-10-07")}
        self.assertEqual(rows["i1"]["action"], "limit")
        self.assertEqual(rows["i1"]["target_injection_rate"], "100.000")
        # 增产量 20 按注水支撑比例 100/110 回压为 18.182。
        self.assertEqual(rows["p1"]["action"], "increase")
        self.assertEqual(rows["p1"]["target_liquid_rate"], "118.182")
        codes = [item["code"] for item in rows["p1"]["reasons"]]
        self.assertIn("injector_support_limited", codes)

    def test_water_capacity_limits_highest_water_cut_first(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["tests"]["p2"] = {"test_id": "t-p2", "tested_at": "2026-10-05T08:00:00Z", "liquid_rate": "100", "water_cut_percent": "95", "source_revision": "r1"}
        snapshot["capacity"] = {"profile_id": "cap-1", "liquid_limit": "500", "water_limit": "80", "effective_from": "2026-10-01"}
        rows = {row["well_id"]: row for row in compute_stage(snapshot, "2026-10-07")}
        self.assertEqual(rows["p2"]["action"], "limit")
        balance = reconcile_stage(list(rows.values()), snapshot["capacity"])
        self.assertTrue(balance["conserved"])
        self.assertLessEqual(Decimal(balance["water_total"]), Decimal("80"))

    def test_neighbor_water_risk_moves_maintain_well_to_observe(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["tests"]["p2"] = {"test_id": "t-p2", "tested_at": "2026-10-05T08:00:00Z", "liquid_rate": "100", "water_cut_percent": "80", "source_revision": "r1"}
        snapshot["tests"]["i1"] = {"test_id": "t-i1", "tested_at": "2026-10-05T08:00:00Z", "liquid_rate": "50", "water_cut_percent": "0", "source_revision": "r1"}
        rows = {row["well_id"]: row for row in compute_stage(snapshot, "2026-10-07")}
        self.assertEqual(rows["p2"]["action"], "observe")
        codes = [item["code"] for item in rows["p2"]["reasons"]]
        self.assertIn("connectivity_water_risk", codes)

    def test_shut_well_gets_zero_target(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["wells"][1]["state"] = "shut"
        rows = {row["well_id"]: row for row in compute_stage(snapshot, "2026-10-07")}
        self.assertEqual(rows["p1"]["action"], "shut")
        self.assertEqual(rows["p1"]["target_liquid_rate"], "0.000")

    def test_oil_water_split_is_conserved_per_well(self) -> None:
        snapshot = snapshot_fixture()
        rows = compute_stage(snapshot, "2026-10-07")
        for row in rows:
            liquid = Decimal(row["target_liquid_rate"])
            oil = Decimal(row["target_oil_rate"])
            water = Decimal(row["target_water_rate"])
            self.assertEqual(liquid, oil + water)
        balance = reconcile_stage(rows, snapshot["capacity"])
        self.assertEqual(balance["oil_water_balance"], "0.000")
        self.assertTrue(balance["conserved"])

    def test_reconcile_reports_capacity_violation(self) -> None:
        rows = [
            {"well_id": "p1", "target_liquid_rate": "300", "target_oil_rate": "200", "target_water_rate": "100", "target_injection_rate": "0"},
        ]
        balance = reconcile_stage(rows, {"liquid_limit": "100", "water_limit": "50"})
        self.assertFalse(balance["conserved"])
        codes = {item["code"] for item in balance["violations"]}
        self.assertEqual(codes, {"liquid_capacity_exceeded", "water_capacity_exceeded"})


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc))
        self.service = WaterControlService(self.connection, self.clock)
        for user_id, role in (("eng", "engineer"), ("eng-2", "engineer"), ("appr-a", "approver"), ("appr-b", "approver"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self._seed_catalog()

    def tearDown(self) -> None:
        self.connection.close()

    def _seed_catalog(self) -> None:
        self.service.create_group("eng", {"group_id": "wg-east", "field_id": "liuhua", "name": "东井组"})
        self.service.create_layer("eng", {"layer_id": "ls-1", "group_id": "wg-east", "name": "珠海组层系"})
        for well_id, kind in (("p1", "producer"), ("p2", "producer"), ("i1", "injector")):
            self.service.create_well("eng", {"well_id": well_id, "group_id": "wg-east", "layer_id": "ls-1", "name": well_id, "kind": kind})
        self.service.create_link("eng", {"link_id": "lk-1", "injector_id": "i1", "producer_id": "p1", "coefficient_percent": "50"})
        self.service.create_link("eng", {"link_id": "lk-2", "injector_id": "i1", "producer_id": "p2", "coefficient_percent": "50"})
        for well_id, maximum in (("p1", "200"), ("p2", "200"), ("i1", "400")):
            self.service.upsert_constraint("eng", {"constraint_id": f"rc-{well_id}", "well_id": well_id, "min_rate": "0", "max_rate": maximum, "effective_from": "2026-01-01"})
        self.service.add_capacity_profile("eng", {"profile_id": "cap-1", "field_id": "liuhua", "liquid_limit": "1000", "water_limit": "500", "effective_from": "2026-10-01"})
        self.service.record_test("eng", {"test_id": "t-p1", "well_id": "p1", "tested_at": "2026-10-05T08:00:00Z", "liquid_rate": "100", "water_cut_percent": "30", "source_revision": "r1"})
        self.service.record_test("eng", {"test_id": "t-p2", "well_id": "p2", "tested_at": "2026-10-05T08:00:00Z", "liquid_rate": "100", "water_cut_percent": "70", "source_revision": "r1"})
        self.service.record_test("eng", {"test_id": "t-i1", "well_id": "i1", "tested_at": "2026-10-05T08:00:00Z", "liquid_rate": "150", "water_cut_percent": "0", "source_revision": "r1"})

    def _create_and_confirm(self, plan_id: str = "plan-1") -> dict:
        plan = self.service.create_plan("eng", {
            "plan_id": plan_id, "field_id": "liuhua", "horizon_start": "2026-10-07", "stage_count": 3,
            **PARAMETERS,
        })
        self.service.confirm_plan("eng", plan_id, 1)
        return plan

    def test_plan_freezes_snapshot_and_is_traceable(self) -> None:
        plan = self._create_and_confirm()
        detail = self.service.plan_detail("eng", "plan-1")
        self.assertEqual(detail["state"], "confirmed")
        self.assertEqual(detail["input_sha256"], plan["input_sha256"])
        self.assertEqual(len(detail["stages"]), 3)
        row = self.connection.execute("SELECT snapshot_json FROM plans WHERE plan_id='plan-1'").fetchone()
        snapshot = json.loads(row["snapshot_json"])
        self.assertEqual(snapshot["capacity"]["profile_id"], "cap-1")
        self.assertEqual(snapshot["tests"]["p1"]["test_id"], "t-p1")
        self.assertIn("trends", snapshot)

    def test_only_one_confirmed_plan_per_field(self) -> None:
        self._create_and_confirm("plan-1")
        self.service.create_plan("eng", {"plan_id": "plan-2", "field_id": "liuhua", "horizon_start": "2026-10-07", "stage_count": 3, **PARAMETERS})
        self.service.confirm_plan("eng", "plan-2", 1)
        first = self.service.plan_detail("eng", "plan-1")
        second = self.service.plan_detail("eng", "plan-2")
        self.assertEqual(first["state"], "superseded")
        self.assertEqual(second["state"], "confirmed")
        count = self.connection.execute("SELECT count(*) FROM plans WHERE field_id='liuhua' AND state='confirmed'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_confirm_with_stale_revision_is_rejected(self) -> None:
        self.service.create_plan("eng", {"plan_id": "plan-1", "field_id": "liuhua", "horizon_start": "2026-10-07", "stage_count": 3, **PARAMETERS})
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("eng", "plan-1", 7)
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("eng", "plan-1", 1)
            self.service.confirm_plan("eng", "plan-1", 1)

    def test_stages_execute_in_order_and_only_when_due(self) -> None:
        self._create_and_confirm()
        with self.assertRaises(InvalidState):
            self.service.execute_stage("eng", "plan-1", 1, 2)
        with self.assertRaises(InvalidState):
            self.service.execute_stage("eng", "plan-1", 2, 2)
        result = self.service.execute_stage("eng", "plan-1", 0, 2)
        self.assertEqual(result["state"], "executed")
        with self.assertRaises(InvalidState):
            self.service.execute_stage("eng", "plan-1", 0, 3)
        with self.assertRaises(InvalidState):
            self.service.execute_stage("eng", "plan-1", 1, 2)
        self.clock.advance(days=1)
        follow = self.service.execute_stage("eng", "plan-1", 1, 3)
        self.assertEqual(follow["revision"], 4)

    def test_replan_preserves_executed_and_recomputes_future(self) -> None:
        self._create_and_confirm("plan-1")
        self.service.execute_stage("eng", "plan-1", 0, 2)
        before = self.connection.execute(
            "SELECT well_id,action,target_liquid_rate,executed_by FROM plan_instructions WHERE plan_id='plan-1' AND stage_index=0 ORDER BY well_id"
        ).fetchall()
        self.service.report_event("eng", "p2", "shutdown", "异常停产")
        replanned = self.service.replan("eng", "liuhua", "plan-2", "well_shutdown")
        self.assertEqual(replanned["carried_stages"], [0])
        self.service.confirm_plan("eng", "plan-2", 1)
        carried = self.connection.execute(
            "SELECT well_id,action,target_liquid_rate,executed_by,carried_from_plan_id FROM plan_instructions WHERE plan_id='plan-2' AND stage_index=0 ORDER BY well_id"
        ).fetchall()
        self.assertEqual(
            [(row["well_id"], row["action"], row["target_liquid_rate"], row["executed_by"]) for row in before],
            [(row["well_id"], row["action"], row["target_liquid_rate"], row["executed_by"]) for row in carried],
        )
        self.assertTrue(all(row["carried_from_plan_id"] == "plan-1" for row in carried))
        future = self.connection.execute(
            "SELECT action,target_liquid_rate,state FROM plan_instructions WHERE plan_id='plan-2' AND stage_index=1 AND well_id='p2'"
        ).fetchone()
        self.assertEqual(future["action"], "shut")
        self.assertEqual(future["target_liquid_rate"], "0.000")
        self.assertEqual(future["state"], "pending")
        self.assertEqual(self.service.plan_detail("eng", "plan-1")["state"], "superseded")

    def test_late_test_is_flagged_and_triggers_replan(self) -> None:
        self._create_and_confirm("plan-1")
        early = self.service.record_test("eng", {"test_id": "t-p1-2", "well_id": "p1", "tested_at": "2026-10-08T06:00:00Z", "liquid_rate": "100", "water_cut_percent": "31", "source_revision": "r2"})
        self.assertFalse(early["late"])
        self.service.execute_stage("eng", "plan-1", 0, 2)
        late = self.service.record_test("eng", {"test_id": "t-p1-3", "well_id": "p1", "tested_at": "2026-10-07T06:00:00Z", "liquid_rate": "98", "water_cut_percent": "33", "source_revision": "r3"})
        self.assertTrue(late["late"])
        replanned = self.service.replan("eng", "liuhua", "plan-2", "late_test")
        self.assertEqual(replanned["trigger"], "late_test")
        row = self.connection.execute("SELECT snapshot_json FROM plans WHERE plan_id='plan-2'").fetchone()
        snapshot = json.loads(row["snapshot_json"])
        # 当前测试版本取测试时刻最新者，迟到的测试进入趋势样本。
        self.assertEqual(snapshot["tests"]["p1"]["test_id"], "t-p1-2")
        self.assertEqual(snapshot["trends"]["p1"]["samples"], 3)

    def test_duplicate_test_revision_conflicts(self) -> None:
        with self.assertRaises(Conflict):
            self.service.record_test("eng", {"test_id": "t-p1-dup", "well_id": "p1", "tested_at": "2026-10-05T08:00:00Z", "liquid_rate": "100", "water_cut_percent": "30", "source_revision": "r1"})

    def test_override_requires_dual_approval_and_deadline(self) -> None:
        self._create_and_confirm("plan-1")
        with self.assertRaises(ValidationFailed):
            self.service.request_override("eng", {"override_id": "ovr-x", "plan_id": "plan-1", "stage_index": 1, "well_id": "p1", "action": "limit", "target_liquid_rate": "50", "reason": "测试", "expires_at": "2026-10-06T00:00:00Z"})
        self.service.request_override("eng", {"override_id": "ovr-1", "plan_id": "plan-1", "stage_index": 1, "well_id": "p1", "action": "limit", "target_liquid_rate": "50", "reason": "临时压产", "expires_at": "2026-10-09T00:00:00Z"})
        with self.assertRaises(Forbidden):
            self.service.approve_override("eng", "ovr-1")
        with self.assertRaises(Forbidden):
            self.service.approve_override("eng-2", "ovr-1")
        first = self.service.approve_override("appr-a", "ovr-1")
        self.assertEqual(first["state"], "pending")
        with self.assertRaises(Conflict):
            self.service.approve_override("appr-a", "ovr-1")
        with self.assertRaises(InvalidState):
            self.service.apply_override("eng", "ovr-1")
        second = self.service.approve_override("appr-b", "ovr-1")
        self.assertEqual(second["state"], "approved")
        applied = self.service.apply_override("eng", "ovr-1")
        self.assertEqual(applied["state"], "applied")
        row = self.connection.execute(
            "SELECT action,target_liquid_rate,target_oil_rate,target_water_rate,reasons_json FROM plan_instructions WHERE plan_id='plan-1' AND stage_index=1 AND well_id='p1'"
        ).fetchone()
        self.assertEqual(row["action"], "limit")
        self.assertEqual(row["target_liquid_rate"], "50.000")
        self.assertEqual(Decimal(row["target_oil_rate"]) + Decimal(row["target_water_rate"]), Decimal("50.000"))
        self.assertIn("manual_override", {item["code"] for item in json.loads(row["reasons_json"])})
        with self.assertRaises(InvalidState):
            self.service.apply_override("eng", "ovr-1")

    def test_override_expires_and_never_touches_executed_stage(self) -> None:
        self._create_and_confirm("plan-1")
        self.service.execute_stage("eng", "plan-1", 0, 2)
        with self.assertRaises(InvalidState):
            self.service.request_override("eng", {"override_id": "ovr-2", "plan_id": "plan-1", "stage_index": 0, "well_id": "p1", "action": "maintain", "reason": "回写已执行", "expires_at": "2026-10-09T00:00:00Z"})
        self.service.request_override("eng", {"override_id": "ovr-3", "plan_id": "plan-1", "stage_index": 1, "well_id": "p1", "action": "maintain", "reason": "即将过期", "expires_at": "2026-10-08T00:00:00Z"})
        self.service.approve_override("appr-a", "ovr-3")
        self.service.approve_override("appr-b", "ovr-3")
        self.clock.advance(days=2)
        with self.assertRaises(InvalidState):
            self.service.apply_override("eng", "ovr-3")
        row = self.connection.execute("SELECT state FROM override_requests WHERE override_id='ovr-3'").fetchone()
        self.assertEqual(row["state"], "expired")

    def test_explain_well_gives_reasons_and_context(self) -> None:
        self._create_and_confirm("plan-1")
        explanation = self.service.explain_well("eng", "plan-1", "p1")
        self.assertEqual(explanation["stages"][0]["action"], "increase")
        codes = [item["code"] for item in explanation["stages"][0]["reasons"]]
        self.assertIn("capacity_headroom_allocated", codes)
        self.assertEqual(explanation["test"]["test_id"], "t-p1")
        self.assertEqual(explanation["links"][0]["injector_id"], "i1")
        with self.assertRaises(NotFound):
            self.service.explain_well("eng", "plan-1", "p-unknown")

    def test_reconcile_plan_conserves_oil_water_and_capacity(self) -> None:
        self._create_and_confirm("plan-1")
        report = self.service.reconcile_plan("audit", "plan-1")
        self.assertTrue(report["conserved"])
        self.assertEqual(len(report["stages"]), 3)
        for stage in report["stages"]:
            self.assertEqual(stage["oil_water_balance"], "0.000")
            self.assertLessEqual(Decimal(stage["liquid_total"]), Decimal(stage["liquid_limit"]))
            self.assertLessEqual(Decimal(stage["water_total"]), Decimal(stage["water_limit"]))

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_plan("appr-a", {"plan_id": "plan-x", "field_id": "liuhua", "horizon_start": "2026-10-07", "stage_count": 3, **PARAMETERS})
        self._create_and_confirm("plan-1")
        with self.assertRaises(Forbidden):
            self.service.execute_stage("appr-a", "plan-1", 0, 2)
        with self.assertRaises(Forbidden):
            self.service.audit_chain("eng")

    def test_audit_chain_detects_tampering(self) -> None:
        self._create_and_confirm("plan-1")
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE wc_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_plan_requires_capacity_profile(self) -> None:
        self.service.create_group("eng", {"group_id": "wg-empty", "field_id": "other-field", "name": "空井组"})
        self.service.create_layer("eng", {"layer_id": "ls-9", "group_id": "wg-empty", "name": "层系"})
        self.service.create_well("eng", {"well_id": "p9", "group_id": "wg-empty", "layer_id": "ls-9", "name": "p9", "kind": "producer"})
        with self.assertRaises(InvalidState):
            self.service.create_plan("eng", {"plan_id": "plan-9", "field_id": "other-field", "horizon_start": "2026-10-07", "stage_count": 3, **PARAMETERS})


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = WaterControlService(self.connection, FrozenClock(datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)))
        self.app = JsonApplication(self.service)
        self.service.create_user("eng", "工程师", "engineer")

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_missing_actor_is_rejected(self) -> None:
        response = self.app.handle("POST", "/catalog/groups", body=b"{}")
        self.assertEqual(response.status, 422)

    def test_catalog_and_plan_routes(self) -> None:
        headers = {"X-Actor-Id": "eng"}
        group = self.app.handle("POST", "/catalog/groups", headers, json.dumps({"group_id": "wg-1", "field_id": "liuhua", "name": "井组"}).encode())
        self.assertEqual(group.status, 201)
        layer = self.app.handle("POST", "/catalog/layers", headers, json.dumps({"layer_id": "ls-1", "group_id": "wg-1", "name": "层系"}).encode())
        self.assertEqual(layer.status, 201)
        well = self.app.handle("POST", "/catalog/wells", headers, json.dumps({"well_id": "p1", "group_id": "wg-1", "layer_id": "ls-1", "name": "p1", "kind": "producer"}).encode())
        self.assertEqual(well.status, 201)
        self.app.handle("POST", "/catalog/capacity", headers, json.dumps({"profile_id": "cap-1", "field_id": "liuhua", "liquid_limit": "100", "water_limit": "50", "effective_from": "2026-10-01"}).encode())
        self.app.handle("POST", "/tests", headers, json.dumps({"test_id": "t-1", "well_id": "p1", "tested_at": "2026-10-05T00:00:00Z", "liquid_rate": "10", "water_cut_percent": "30", "source_revision": "r1"}).encode())
        plan = self.app.handle("POST", "/plans", headers, json.dumps({"plan_id": "plan-1", "field_id": "liuhua", "horizon_start": "2026-10-07", "stage_count": 2, **PARAMETERS}).encode())
        self.assertEqual(plan.status, 201)
        confirm = self.app.handle("POST", "/plans/plan-1/confirm", headers, json.dumps({"expected_revision": 1}).encode())
        self.assertEqual(confirm.status, 200)
        explanation = self.app.handle("GET", "/plans/plan-1/wells/p1/explanation", headers)
        self.assertEqual(explanation.status, 200)
        self.assertEqual(explanation.body["well_id"], "p1")
        reconciliation = self.app.handle("GET", "/plans/plan-1/reconciliation?stage_index=0", headers)
        self.assertEqual(reconciliation.status, 200)
        self.assertTrue(reconciliation.body["stages"][0]["conserved"])
        missing = self.app.handle("GET", "/plans/plan-9", headers)
        self.assertEqual(missing.status, 404)


if __name__ == "__main__":
    unittest.main()
