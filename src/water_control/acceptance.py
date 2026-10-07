"""贯通目录冻结、方案确认、分阶段执行、异常重算与人工覆盖的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import WaterControlService


PRODUCERS = (
    ("p1", "30"), ("p2", "40"), ("p3", "70"),
    ("p4", "86"), ("p5", "92"), ("p6", "96"),
)


def _seed(service: WaterControlService) -> None:
    service.create_group("eng", {"group_id": "wg-east", "field_id": "liuhua", "name": "东井组"})
    service.create_group("eng", {"group_id": "wg-west", "field_id": "liuhua", "name": "西井组"})
    service.create_layer("eng", {"layer_id": "ls-zhuhai", "group_id": "wg-east", "name": "珠海组层系"})
    service.create_layer("eng", {"layer_id": "ls-hanjiang", "group_id": "wg-west", "name": "韩江组层系"})
    for index, (well_id, _water_cut) in enumerate(PRODUCERS):
        group = "wg-east" if index < 3 else "wg-west"
        layer = "ls-zhuhai" if index < 3 else "ls-hanjiang"
        service.create_well("eng", {"well_id": well_id, "group_id": group, "layer_id": layer, "name": f"生产井{well_id}", "kind": "producer"})
    service.create_well("eng", {"well_id": "i1", "group_id": "wg-east", "layer_id": "ls-zhuhai", "name": "注水井i1", "kind": "injector"})
    service.create_well("eng", {"well_id": "i2", "group_id": "wg-west", "layer_id": "ls-hanjiang", "name": "注水井i2", "kind": "injector"})
    for link_id, injector, producer in (
        ("lk-i1-p1", "i1", "p1"), ("lk-i1-p2", "i1", "p2"), ("lk-i1-p3", "i1", "p3"),
        ("lk-i2-p4", "i2", "p4"), ("lk-i2-p5", "i2", "p5"), ("lk-i2-p6", "i2", "p6"),
    ):
        service.create_link("eng", {"link_id": link_id, "injector_id": injector, "producer_id": producer, "coefficient_percent": "50"})
    for well_id in (*[item[0] for item in PRODUCERS], "i1", "i2"):
        service.upsert_constraint("eng", {"constraint_id": f"rc-{well_id}", "well_id": well_id, "min_rate": "0", "max_rate": "200" if well_id.startswith("p") else "400", "effective_from": "2026-01-01"})
    service.add_capacity_profile("eng", {"profile_id": "cap-2026q4", "field_id": "liuhua", "liquid_limit": "1000", "water_limit": "420", "effective_from": "2026-10-01"})
    for well_id, water_cut in PRODUCERS:
        service.record_test("eng", {"test_id": f"t-{well_id}-1", "well_id": well_id, "tested_at": "2026-10-05T08:00:00Z", "liquid_rate": "100", "water_cut_percent": water_cut, "source_revision": "rev-1"})
    for well_id in ("i1", "i2"):
        service.record_test("eng", {"test_id": f"t-{well_id}-1", "well_id": well_id, "tested_at": "2026-10-05T08:00:00Z", "liquid_rate": "150", "water_cut_percent": "0", "source_revision": "rev-1"})


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = WaterControlService(connection, FrozenClock(datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("eng", "engineer"), ("appr-a", "approver"), ("appr-b", "approver"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    _seed(service)
    plan = service.create_plan("eng", {
        "plan_id": "wc-plan-001", "field_id": "liuhua", "horizon_start": "2026-10-07", "stage_count": 3,
        "observe_threshold_percent": "85", "limit_threshold_percent": "90",
        "increase_threshold_percent": "60", "increase_cap_percent": "20", "limit_cut_percent": "10",
    })
    service.confirm_plan("eng", "wc-plan-001", 1)
    executed = service.execute_stage("eng", "wc-plan-001", 0, 2)
    service.report_event("eng", "p5", "shutdown", "井底压力异常，紧急停产")
    late_test = service.record_test("eng", {"test_id": "t-p3-2", "well_id": "p3", "tested_at": "2026-10-07T06:00:00Z", "liquid_rate": "100", "water_cut_percent": "71", "source_revision": "rev-2"})
    replanned = service.replan("eng", "liuhua", "wc-plan-002", "well_shutdown")
    service.confirm_plan("eng", "wc-plan-002", 1)
    service.request_override("eng", {
        "override_id": "ovr-001", "plan_id": "wc-plan-002", "stage_index": 1, "well_id": "p3",
        "action": "limit", "target_liquid_rate": "80", "reason": "邻井停产后临时压产观察",
        "expires_at": "2026-10-09T00:00:00Z",
    })
    service.approve_override("appr-a", "ovr-001")
    approved = service.approve_override("appr-b", "ovr-001")
    applied = service.apply_override("eng", "ovr-001")
    explanation = service.explain_well("eng", "wc-plan-002", "p5")
    reconciliation = service.reconcile_plan("audit", "wc-plan-002")
    result = {
        "status": "ok",
        "plan_sha256": plan["input_sha256"],
        "executed_instructions": executed["instructions"],
        "late_test": late_test["late"],
        "carried_stages": replanned["carried_stages"],
        "override": {"state": applied["state"], "approvals": approved["approvals"]},
        "explanation_action": explanation["stages"][1]["action"],
        "conserved": reconciliation["conserved"],
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行稳油控水协同服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
