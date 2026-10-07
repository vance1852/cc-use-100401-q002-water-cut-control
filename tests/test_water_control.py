from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from water_control.acceptance import run as acceptance_run
from water_control.api import JsonApplication
from water_control.clock import FrozenClock
from water_control.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from water_control.planning import (
    conservation_report,
    digest,
    plan_phases,
)
from water_control.service import WaterControlService


ROOT = Path(__file__).resolve().parents[1]


def snapshot_fixture(**overrides):
    """两口生产井 + 一口注水井的最小快照，便于精确断言。"""

    snapshot = {
        "field_id": "liuhua",
        "frozen_at": "2026-10-06T00:00:00Z",
        "horizon": {"starts_at": "2026-10-06T00:00:00Z", "phase_hours": 24, "phase_count": 2},
        "constraints": {
            "constraint_set_id": "cs-test",
            "content_sha256": "0" * 64,
            "test_validity_hours": 72,
            "max_liquid_change_per_phase": "100",
            "increase_water_cut_limit": "0.85",
            "target_voidage_ratio": "1.0",
        },
        "caps": {"cap_id": 1, "effective_from": "2026-10-01T00:00:00Z", "liquid_cap": "1000", "water_cap": "800"},
        "wells": [
            {
                "well_id": "P1", "kind": "producer", "group_id": "wg-a", "layer_system_id": "ls-1",
                "min_liquid": "50", "max_liquid": "500", "max_water_cut": "0.90",
                "min_injection": "0", "max_injection": "0",
                "test_version": 1, "tested_at": "2026-10-05T00:00:00Z",
                "test_liquid": "200", "test_water_cut": "0.60", "test_injection": "0",
                "trend_per_day": "0", "stale_test": False,
            },
            {
                "well_id": "P2", "kind": "producer", "group_id": "wg-b", "layer_system_id": "ls-1",
                "min_liquid": "50", "max_liquid": "500", "max_water_cut": "0.90",
                "min_injection": "0", "max_injection": "0",
                "test_version": 1, "tested_at": "2026-10-05T00:00:00Z",
                "test_liquid": "200", "test_water_cut": "0.80", "test_injection": "0",
                "trend_per_day": "0", "stale_test": False,
            },
            {
                "well_id": "I1", "kind": "injector", "group_id": "wg-a", "layer_system_id": "ls-1",
                "min_liquid": "0", "max_liquid": "0", "max_water_cut": "0.9999",
                "min_injection": "100", "max_injection": "2000",
                "test_version": 1, "tested_at": "2026-10-05T00:00:00Z",
                "test_liquid": "0", "test_water_cut": "0", "test_injection": "400",
                "trend_per_day": "0", "stale_test": False,
            },
        ],
        "edges": [],
        "events": [],
        "initial_targets": {},
    }
    for key, value in overrides.items():
        if key == "wells":
            snapshot["wells"] = value
        else:
            snapshot[key] = value
    return snapshot


def well_map(snapshot):
    return {well["well_id"]: well for well in snapshot["wells"]}


class PlanningTests(unittest.TestCase):
    def test_same_snapshot_gives_same_result(self) -> None:
        snapshot = snapshot_fixture()
        first = plan_phases(snapshot)
        second = plan_phases(snapshot)
        self.assertEqual(digest(first), digest(second))

    def test_low_water_cut_well_increases_first(self) -> None:
        phases = plan_phases(snapshot_fixture())
        commands = {cmd["well_id"]: cmd for cmd in phases[0]["commands"]}
        self.assertEqual(commands["P1"]["action"], "increase")
        self.assertEqual(commands["P1"]["target_liquid"], "300.000")
        self.assertEqual(commands["P2"]["action"], "increase")
        self.assertEqual(commands["P2"]["target_liquid"], "300.000")
        codes = [reason["code"] for reason in commands["P1"]["reasons"]]
        self.assertIn("low_water_cut_priority", codes)
        self.assertIn("ramp_limit", codes)

    def test_conservation_holds_by_construction(self) -> None:
        snapshot = snapshot_fixture()
        phases = plan_phases(snapshot)
        for phase in phases:
            report = conservation_report(
                commands=phase["commands"],
                wells=well_map(snapshot),
                liquid_cap=Decimal("1000"),
                water_cap=Decimal("800"),
                target_voidage_ratio=Decimal("1.0"),
            )
            self.assertTrue(report["balanced"], report["violations"])
            totals = report["totals"]
            liquid = Decimal(totals["liquid_total"])
            self.assertEqual(Decimal(totals["oil_total"]) + Decimal(totals["water_total"]), liquid)
            self.assertEqual(liquid + Decimal(totals["liquid_headroom"]), Decimal("1000"))
            self.assertEqual(Decimal(totals["injection_total"]), liquid)

    def test_high_water_cut_well_is_restricted(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["wells"][1]["test_water_cut"] = "0.95"
        phases = plan_phases(snapshot)
        commands = {cmd["well_id"]: cmd for cmd in phases[0]["commands"]}
        self.assertEqual(commands["P2"]["action"], "restrict")
        self.assertEqual(commands["P2"]["target_liquid"], "100.000")
        self.assertEqual(commands["P2"]["reasons"][0]["code"], "water_cut_limit")

    def test_platform_water_cap_forces_restriction(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["caps"]["water_cap"] = "250"
        phases = plan_phases(snapshot)
        commands = {cmd["well_id"]: cmd for cmd in phases[0]["commands"]}
        self.assertEqual(commands["P2"]["action"], "restrict")
        self.assertEqual(commands["P2"]["target_liquid"], "100.000")
        self.assertEqual(commands["P2"]["reasons"][0]["code"], "platform_water_cap")
        water_total = Decimal(phases[0]["totals"]["water_total"])
        self.assertLessEqual(water_total, Decimal("250"))

    def test_infeasible_water_cap_raises(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["caps"]["water_cap"] = "10"
        with self.assertRaises(ValueError):
            plan_phases(snapshot)

    def test_neighbor_connectivity_limits_increase(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["edges"] = [{"from_group_id": "wg-a", "to_group_id": "wg-b", "coefficient": "0.5"}]
        phases = plan_phases(snapshot)
        commands = {cmd["well_id"]: cmd for cmd in phases[0]["commands"]}
        # P1 增产 100 使 P2 含水上升 0.05；P2 因此触及 0.90 上限前只能微调。
        self.assertEqual(commands["P2"]["projected_water_cut"], "0.8500")
        codes = [reason["code"] for reason in commands["P1"]["reasons"]]
        self.assertIn("low_water_cut_priority", codes)

    def test_neighbor_effect_blocks_increase_when_tight(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["edges"] = [{"from_group_id": "wg-a", "to_group_id": "wg-b", "coefficient": "0.5"}]
        snapshot["wells"][1]["max_water_cut"] = "0.82"
        phases = plan_phases(snapshot)
        commands = {cmd["well_id"]: cmd for cmd in phases[0]["commands"]}
        # P2 上限 0.82，只允许 P1 增产 (0.82-0.80)*1000/0.5 = 40。
        self.assertEqual(commands["P1"]["target_liquid"], "240.000")
        codes = [reason["code"] for reason in commands["P1"]["reasons"]]
        self.assertIn("neighbor_water_cut", codes)
        self.assertEqual(commands["P2"]["projected_water_cut"], "0.8200")

    def test_shut_in_zeroes_future_phases_only(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["events"] = [
            {"event_id": 1, "well_id": "P1", "event_type": "shut_in", "effective_at": "2026-10-07T00:00:00Z"}
        ]
        phases = plan_phases(snapshot)
        phase0 = {cmd["well_id"]: cmd for cmd in phases[0]["commands"]}
        phase1 = {cmd["well_id"]: cmd for cmd in phases[1]["commands"]}
        self.assertEqual(phase0["P1"]["action"], "increase")
        self.assertEqual(phase1["P1"]["action"], "shut")
        self.assertEqual(phase1["P1"]["target_liquid"], "0.000")
        self.assertEqual(phase1["P1"]["reasons"][0]["code"], "shut_in_event")

    def test_stale_test_goes_to_observe(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["wells"][0]["stale_test"] = True
        phases = plan_phases(snapshot)
        commands = {cmd["well_id"]: cmd for cmd in phases[0]["commands"]}
        self.assertEqual(commands["P1"]["action"], "observe")
        self.assertEqual(commands["P1"]["target_liquid"], "200.000")
        self.assertEqual(commands["P1"]["reasons"][0]["code"], "test_stale")

    def test_water_cut_trend_raises_projection(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["wells"][0]["trend_per_day"] = "0.01"
        phases = plan_phases(snapshot)
        commands = {cmd["well_id"]: cmd for cmd in phases[1]["commands"]}
        # 第二阶段距测试 2 天：0.60 + 0.01*2 = 0.62
        self.assertEqual(commands["P1"]["projected_water_cut"], "0.6200")

    def test_injection_follows_voidage_ratio(self) -> None:
        snapshot = snapshot_fixture()
        phases = plan_phases(snapshot)
        totals = phases[0]["totals"]
        self.assertEqual(totals["injection_total"], totals["liquid_total"])
        commands = {cmd["well_id"]: cmd for cmd in phases[0]["commands"]}
        self.assertEqual(commands["I1"]["action"], "inject")
        self.assertEqual(commands["I1"]["reasons"][0]["code"], "voidage_balance")

    def test_skip_before_uses_initial_targets(self) -> None:
        snapshot = snapshot_fixture()
        snapshot["initial_targets"] = {"P1": "350", "P2": "150"}
        phases = plan_phases(snapshot, skip_before=1)
        self.assertEqual(len(phases), 1)
        self.assertEqual(phases[0]["phase_index"], 1)
        commands = {cmd["well_id"]: cmd for cmd in phases[0]["commands"]}
        # P2 从 150 起按调幅上限增产 100。
        self.assertEqual(commands["P2"]["target_liquid"], "250.000")


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc))
        self.service = WaterControlService(self.connection, self.clock)
        for user_id, role in (
            ("eng-1", "engineer"),
            ("eng-2", "engineer"),
            ("sup-1", "supervisor"),
            ("sup-2", "supervisor"),
            ("ops-1", "operator"),
            ("audit-1", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_layer_system("eng-1", {"layer_system_id": "ls-1", "name": "上油组"})
        for group_id in ("wg-a", "wg-b"):
            self.service.create_well_group("eng-1", {"group_id": group_id, "name": f"井组{group_id}"})
        for well_id, group_id, water_cut in (("P1", "wg-a", "0.60"), ("P2", "wg-b", "0.80")):
            self.service.create_well("eng-1", {
                "well_id": well_id, "name": well_id, "kind": "producer",
                "layer_system_id": "ls-1", "group_id": group_id,
                "min_liquid": "50", "max_liquid": "500", "max_water_cut": "0.90",
            })
            self.service.record_test("eng-1", {
                "well_id": well_id, "version": 1, "tested_at": "2026-10-05T00:00:00Z",
                "liquid_rate": "200", "water_cut": water_cut, "injection_rate": "0",
            })
        self.service.create_well("eng-1", {
            "well_id": "I1", "name": "I1", "kind": "injector",
            "layer_system_id": "ls-1", "group_id": "wg-a",
            "min_injection": "100", "max_injection": "2000",
        })
        self.service.record_test("eng-1", {
            "well_id": "I1", "version": 1, "tested_at": "2026-10-05T00:00:00Z",
            "liquid_rate": "0", "water_cut": "0", "injection_rate": "400",
        })
        self.service.register_cap("eng-1", {
            "effective_from": "2026-10-01T00:00:00Z", "liquid_cap": "1000", "water_cap": "800",
        })
        self.service.create_constraint_set("eng-1", {
            "constraint_set_id": "cs-test", "name": "测试约束",
            "test_validity_hours": 72, "max_liquid_change_per_phase": "100",
            "increase_water_cut_limit": "0.85", "target_voidage_ratio": "1.0",
            "phase_hours": 24, "phase_count": 2,
        })

    def tearDown(self) -> None:
        self.connection.close()

    def create_plan(self, plan_id: str = "plan-1", key: str = "key-1") -> dict:
        return self.service.create_plan("eng-1", {
            "plan_id": plan_id, "field_id": "liuhua",
            "horizon_starts_at": "2026-10-06T00:00:00Z", "constraint_set_id": "cs-test",
        }, key)

    def confirmed_plan(self, plan_id: str = "plan-1", key: str = "key-1") -> dict:
        plan = self.create_plan(plan_id, key)
        self.service.confirm_plan("sup-1", plan_id, 1)
        return plan


class PlanLifecycleTests(ServiceTestBase):
    def test_plan_freezes_traceable_snapshot(self) -> None:
        plan = self.create_plan()
        self.assertEqual(plan["state"], "draft")
        self.assertEqual(len(plan["snapshot_sha256"]), 64)
        row = self.connection.execute("SELECT snapshot_json FROM plans WHERE plan_id='plan-1'").fetchone()
        snapshot = json.loads(row["snapshot_json"])
        self.assertEqual([well["well_id"] for well in snapshot["wells"]], ["I1", "P1", "P2"])
        self.assertEqual(snapshot["wells"][1]["layer_system_id"], "ls-1")
        self.assertEqual(snapshot["constraints"]["constraint_set_id"], "cs-test")
        self.assertEqual(snapshot["caps"]["liquid_cap"], "1000")
        self.assertEqual(snapshot["wells"][1]["test_version"], 1)

    def test_plan_creation_is_idempotent(self) -> None:
        first = self.create_plan()
        second = self.create_plan()
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.create_plan("eng-1", {
                "plan_id": "plan-2", "field_id": "liuhua",
                "horizon_starts_at": "2026-10-06T00:00:00Z", "constraint_set_id": "cs-test",
            }, "key-1")

    def test_only_one_active_version_survives_concurrent_confirm(self) -> None:
        self.create_plan("plan-1", "key-1")
        self.create_plan("plan-2", "key-2")
        self.service.confirm_plan("sup-1", "plan-1", 1)
        with self.assertRaises(Conflict):
            self.service.confirm_plan("sup-1", "plan-2", 1)
        # 从有效版本派生的两个重算草稿，也只能确认一个。
        self.service.recompute_plan("eng-1", {
            "source_plan_id": "plan-1", "plan_id": "plan-3", "reason": "测试迟到",
        }, "key-3")
        self.service.recompute_plan("eng-1", {
            "source_plan_id": "plan-1", "plan_id": "plan-4", "reason": "异常停产",
        }, "key-4")
        self.service.confirm_plan("sup-1", "plan-3", 1)
        with self.assertRaises(Conflict):
            self.service.confirm_plan("sup-1", "plan-4", 1)
        state = self.connection.execute("SELECT state FROM plans WHERE plan_id='plan-1'").fetchone()["state"]
        self.assertEqual(state, "superseded")

    def test_confirm_requires_draft_and_revision(self) -> None:
        self.create_plan()
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("sup-1", "plan-1", 7)
        self.service.confirm_plan("sup-1", "plan-1", 1)
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("sup-1", "plan-1", 2)

    def test_phases_execute_in_order(self) -> None:
        self.confirmed_plan()
        with self.assertRaises(InvalidState):
            self.service.execute_phase("ops-1", "plan-1", 1, 2)
        self.service.execute_phase("ops-1", "plan-1", 0, 2)
        with self.assertRaises(InvalidState):
            self.service.execute_phase("ops-1", "plan-1", 0, 3)
        plan = self.service.execute_phase("ops-1", "plan-1", 1, 3)
        self.assertEqual(plan["state"], "completed")

    def test_recompute_keeps_executed_commands_untouched(self) -> None:
        self.confirmed_plan()
        self.service.execute_phase("ops-1", "plan-1", 0, 2)
        before = self.service.explain_phase("eng-1", "plan-1", 0)["wells"]
        self.clock.advance(hours=30)
        self.service.report_event("eng-1", {
            "well_id": "P1", "event_type": "shut_in",
            "effective_at": "2026-10-07T00:00:00Z", "note": "异常停产",
        })
        recomputed = self.service.recompute_plan("eng-1", {
            "source_plan_id": "plan-1", "plan_id": "plan-2", "reason": "P1 异常停产",
        }, "key-2")
        self.assertEqual(recomputed["inherited_phases"], [0])
        self.assertEqual(recomputed["recomputed_phases"], [1])
        self.service.confirm_plan("sup-1", "plan-2", 1)
        inherited = self.service.explain_phase("eng-1", "plan-2", 0)["wells"]
        for old, new in zip(before, inherited):
            self.assertEqual(old["target_liquid"], new["target_liquid"])
            self.assertEqual(old["oil_rate"], new["oil_rate"])
            self.assertEqual(new["origin"], "inherited")
        # 原方案的已执行指令不被回写。
        after = self.service.explain_phase("eng-1", "plan-1", 0)["wells"]
        self.assertEqual(before, after)
        # 未来时段按新事件重算：P1 停产清零。
        future = {well["well_id"]: well for well in self.service.explain_phase("eng-1", "plan-2", 1)["wells"]}
        self.assertEqual(future["P1"]["action"], "shut")
        self.assertEqual(future["P1"]["target_liquid"], "0.000")

    def test_late_test_turns_well_to_observe_in_future_phases(self) -> None:
        self.confirmed_plan()
        self.service.execute_phase("ops-1", "plan-1", 0, 2)
        self.clock.advance(hours=80)  # 超过 72 小时测试有效期
        self.service.recompute_plan("eng-1", {
            "source_plan_id": "plan-1", "plan_id": "plan-2", "reason": "测试迟到",
        }, "key-2")
        self.service.confirm_plan("sup-1", "plan-2", 1)
        future = {well["well_id"]: well for well in self.service.explain_phase("eng-1", "plan-2", 1)["wells"]}
        self.assertEqual(future["P1"]["action"], "observe")
        self.assertEqual(future["P2"]["action"], "observe")
        self.assertEqual(future["P1"]["reasons"][0]["code"], "test_stale")

    def test_recompute_rejected_for_draft_or_completed(self) -> None:
        self.create_plan()
        with self.assertRaises(InvalidState):
            self.service.recompute_plan("eng-1", {
                "source_plan_id": "plan-1", "plan_id": "plan-2", "reason": "过早",
            }, "key-2")

    def test_recompute_is_idempotent(self) -> None:
        self.confirmed_plan()
        payload = {"source_plan_id": "plan-1", "plan_id": "plan-2", "reason": "测试迟到"}
        first = self.service.recompute_plan("eng-1", payload, "key-2")
        second = self.service.recompute_plan("eng-1", payload, "key-2")
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.recompute_plan("eng-1", {**payload, "plan_id": "plan-3"}, "key-2")


class OverrideTests(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.confirmed_plan()

    def request(self, **overrides):
        payload = {
            "plan_id": "plan-1", "phase_index": 1, "well_id": "P1",
            "target_liquid": "250", "reason": "人工压产观察",
            "expires_at": "2026-10-08T00:00:00Z",
        }
        payload.update(overrides)
        return self.service.request_override("eng-1", payload)

    def test_override_requires_future_deadline(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.request(expires_at="2026-10-05T00:00:00Z")
        payload = {
            "plan_id": "plan-1", "phase_index": 1, "well_id": "P1",
            "target_liquid": "250", "reason": "缺期限",
        }
        with self.assertRaises(ValidationFailed):
            self.service.request_override("eng-1", payload)

    def test_override_needs_two_distinct_approvers(self) -> None:
        override = self.request()
        with self.assertRaises(Forbidden):
            self.service.approve_override("eng-1", override["override_id"])
        first = self.service.approve_override("sup-1", override["override_id"])
        self.assertEqual(first["status"], "pending")
        with self.assertRaises(Conflict):
            self.service.approve_override("sup-1", override["override_id"])
        second = self.service.approve_override("sup-2", override["override_id"])
        self.assertEqual(second["status"], "approved")
        self.assertEqual(second["approvers"], ["sup-1", "sup-2"])

    def test_override_applies_and_stays_conserved(self) -> None:
        override = self.request()
        self.service.approve_override("sup-1", override["override_id"])
        self.service.approve_override("sup-2", override["override_id"])
        applied = self.service.apply_override("eng-2", override["override_id"])
        self.assertEqual(applied["status"], "applied")
        explanation = self.service.explain_phase("eng-1", "plan-1", 1)
        command = next(well for well in explanation["wells"] if well["well_id"] == "P1")
        self.assertEqual(command["origin"], "manual_override")
        self.assertEqual(command["target_liquid"], "250.000")
        codes = [reason["code"] for reason in command["reasons"]]
        self.assertIn("manual_override", codes)
        detail = command["reasons"][-1]["detail"]
        self.assertEqual(detail["approvers"], ["sup-1", "sup-2"])
        self.assertEqual(detail["expires_at"], "2026-10-08T00:00:00Z")
        conservation = self.service.check_conservation("eng-1", "plan-1", 1)
        self.assertTrue(conservation["balanced"], conservation["violations"])

    def test_expired_override_cannot_be_approved_or_applied(self) -> None:
        override = self.request()
        self.clock.advance(hours=49)  # 超过 2026-10-08 期限
        with self.assertRaises(InvalidState):
            self.service.approve_override("sup-1", override["override_id"])
        status = self.connection.execute(
            "SELECT status FROM override_requests WHERE override_id=?", (override["override_id"],)
        ).fetchone()["status"]
        self.assertEqual(status, "expired")

    def test_override_beyond_platform_cap_is_rejected(self) -> None:
        # 更紧的处理上限方案：液量上限 700，P1 覆盖到 500 将使总量越限。
        self.service.register_cap("eng-1", {
            "effective_from": "2026-10-02T00:00:00Z", "liquid_cap": "700", "water_cap": "800",
        })
        self.service.create_plan("eng-1", {
            "plan_id": "plan-tight", "field_id": "liuhua-tight",
            "horizon_starts_at": "2026-10-06T00:00:00Z", "constraint_set_id": "cs-test",
        }, "key-tight")
        self.service.confirm_plan("sup-1", "plan-tight", 1)
        override = self.service.request_override("eng-1", {
            "plan_id": "plan-tight", "phase_index": 1, "well_id": "P1",
            "target_liquid": "500", "reason": "尝试超限", "expires_at": "2026-10-08T00:00:00Z",
        })
        self.service.approve_override("sup-1", override["override_id"])
        self.service.approve_override("sup-2", override["override_id"])
        with self.assertRaises(Conflict):
            self.service.apply_override("eng-2", override["override_id"])

    def test_override_on_executed_phase_is_rejected(self) -> None:
        self.service.execute_phase("ops-1", "plan-1", 0, 2)
        with self.assertRaises(InvalidState):
            self.request(phase_index=0)

    def test_override_is_voided_when_plan_superseded(self) -> None:
        override = self.request()
        self.service.approve_override("sup-1", override["override_id"])
        self.service.approve_override("sup-2", override["override_id"])
        self.service.recompute_plan("eng-1", {
            "source_plan_id": "plan-1", "plan_id": "plan-2", "reason": "测试迟到",
        }, "key-2")
        self.service.confirm_plan("sup-1", "plan-2", 1)
        with self.assertRaises(InvalidState):
            self.service.apply_override("eng-2", override["override_id"])
        status = self.connection.execute(
            "SELECT status FROM override_requests WHERE override_id=?", (override["override_id"],)
        ).fetchone()["status"]
        self.assertEqual(status, "void")


class ExplanationAndConservationTests(ServiceTestBase):
    def test_explanation_covers_every_well(self) -> None:
        self.confirmed_plan()
        explanation = self.service.explain_phase("eng-1", "plan-1", 0)
        self.assertEqual(len(explanation["wells"]), 3)
        by_well = {well["well_id"]: well for well in explanation["wells"]}
        self.assertEqual(by_well["P1"]["action_label"], "增产")
        self.assertTrue(by_well["P1"]["reasons"])
        self.assertEqual(by_well["I1"]["action_label"], "注采调整")
        self.assertEqual(explanation["caps"]["liquid_cap"], "1000")

    def test_conservation_report_matches_totals(self) -> None:
        self.confirmed_plan()
        report = self.service.check_conservation("eng-1", "plan-1", 0)
        self.assertTrue(report["balanced"], report["violations"])
        totals = report["totals"]
        self.assertEqual(
            Decimal(totals["liquid_total"]) + Decimal(totals["liquid_headroom"]),
            Decimal(totals["liquid_cap"]),
        )
        self.assertEqual(
            Decimal(totals["oil_total"]) + Decimal(totals["water_total"]),
            Decimal(totals["liquid_total"]),
        )

    def test_conservation_detects_tampering(self) -> None:
        self.confirmed_plan()
        self.connection.execute(
            "UPDATE well_commands SET oil_rate='1.000' WHERE plan_id='plan-1' AND phase_index=0 AND well_id='P1'"
        )
        report = self.service.check_conservation("eng-1", "plan-1", 0)
        self.assertFalse(report["balanced"])
        codes = {violation["code"] for violation in report["violations"]}
        self.assertIn("split_imbalance", codes)


class PermissionAndAuditTests(ServiceTestBase):
    def test_role_permissions_are_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.confirm_plan("eng-1", "plan-1", 1)
        with self.assertRaises(Forbidden):
            self.service.create_plan("sup-1", {
                "plan_id": "plan-1", "field_id": "liuhua",
                "horizon_starts_at": "2026-10-06T00:00:00Z", "constraint_set_id": "cs-test",
            }, "key-1")
        self.confirmed_plan()
        with self.assertRaises(Forbidden):
            self.service.execute_phase("eng-1", "plan-1", 0, 2)
        with self.assertRaises(Forbidden):
            self.service.report_event("audit-1", {
                "well_id": "P1", "event_type": "shut_in",
                "effective_at": "2026-10-07T00:00:00Z", "note": "越权",
            })

    def test_unknown_user_and_well(self) -> None:
        with self.assertRaises(NotFound):
            self.service.get_plan("ghost", "plan-1")
        with self.assertRaises(NotFound):
            self.service.record_test("eng-1", {
                "well_id": "PX", "version": 1, "tested_at": "2026-10-05T00:00:00Z",
                "liquid_rate": "100", "water_cut": "0.5", "injection_rate": "0",
            })

    def test_test_recording_is_idempotent_by_natural_key(self) -> None:
        payload = {
            "well_id": "P1", "version": 2, "tested_at": "2026-10-06T00:00:00Z",
            "liquid_rate": "210", "water_cut": "0.61", "injection_rate": "0",
        }
        first = self.service.record_test("eng-1", payload)
        second = self.service.record_test("eng-1", payload)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        with self.assertRaises(Conflict):
            self.service.record_test("eng-1", {**payload, "water_cut": "0.62"})

    def test_audit_chain_detects_tampering(self) -> None:
        self.confirmed_plan()
        self.assertTrue(self.service.audit_chain("audit-1")["valid"])
        self.connection.execute("UPDATE wc_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit-1")["valid"])


class ApiTests(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.app = JsonApplication(self.service)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")
        response = self.app.handle("GET", "/plans/plan-1", {"X-Actor-Id": "eng-1"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")

    def test_plan_flow_over_http(self) -> None:
        headers = {"X-Actor-Id": "eng-1", "Idempotency-Key": "http-key-1"}
        payload = json.dumps({
            "plan_id": "plan-http", "field_id": "liuhua",
            "horizon_starts_at": "2026-10-06T00:00:00Z", "constraint_set_id": "cs-test",
        }).encode()
        created = self.app.handle("POST", "/plans", headers, payload)
        self.assertEqual(created.status, 201)
        replayed = self.app.handle("POST", "/plans", headers, payload)
        self.assertEqual(replayed.body, created.body)
        confirmed = self.app.handle(
            "POST", "/plans/plan-http/confirm", {"X-Actor-Id": "sup-1"},
            json.dumps({"expected_revision": 1}).encode(),
        )
        self.assertEqual(confirmed.status, 200)
        explanation = self.app.handle(
            "GET", "/plans/plan-http/phases/0/explanation", {"X-Actor-Id": "eng-1"}
        )
        self.assertEqual(explanation.status, 200)
        self.assertEqual(len(explanation.body["wells"]), 3)
        conservation = self.app.handle(
            "GET", "/plans/plan-http/phases/0/conservation", {"X-Actor-Id": "eng-1"}
        )
        self.assertTrue(conservation.body["balanced"])
        missing_key = self.app.handle("POST", "/plans", {"X-Actor-Id": "eng-1"}, payload)
        self.assertEqual(missing_key.status, 422)


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = acceptance_run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["producers"], 32)
        self.assertEqual(result["plan_v2"]["inherited_phases"], [0])
        self.assertEqual(result["plan_v2"]["recomputed_phases"], [1, 2])
        self.assertTrue(result["balanced"])
        self.assertEqual(len(result["override_approvers"]), 2)
        self.assertGreater(result["audit_events"], 0)


if __name__ == "__main__":
    unittest.main()
