"""流花油田高含水期稳油控水协同流程的离线验收。

32 口生产井、4 口注水井、3 个层系、4 个井组；覆盖方案冻结、并发确认、
分阶段执行、异常停产与测试迟到后的未来时段重算、人工覆盖（期限+双人
批准）、逐井解释和油水与处理能力守恒核对。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .clock import FrozenClock
from .service import WaterControlService


def _build_field(service: WaterControlService) -> None:
    for layer_system_id, name in (("ls-upper", "上油组"), ("ls-middle", "中油组"), ("ls-lower", "下油组")):
        service.create_layer_system("eng-1", {"layer_system_id": layer_system_id, "name": name})
    for group_id, name in (("wg-a", "井组A"), ("wg-b", "井组B"), ("wg-c", "井组C"), ("wg-d", "井组D")):
        service.create_well_group("eng-1", {"group_id": group_id, "name": name})
    layers = ["ls-upper", "ls-middle", "ls-lower"]
    groups = ["wg-a", "wg-b", "wg-c", "wg-d"]
    for index in range(1, 33):
        service.create_well("eng-1", {
            "well_id": f"LH-{index:02d}",
            "name": f"流花生产井{index:02d}",
            "kind": "producer",
            "layer_system_id": layers[index % 3],
            "group_id": groups[index % 4],
            "min_liquid": "50",
            "max_liquid": "600",
            "max_water_cut": "0.95",
        })
    for index in range(1, 5):
        service.create_well("eng-1", {
            "well_id": f"INJ-{index:02d}",
            "name": f"流花注水井{index:02d}",
            "kind": "injector",
            "layer_system_id": layers[index % 3],
            "group_id": groups[index % 4],
            "min_injection": "200",
            "max_injection": "4000",
        })
    for from_group, to_group, coefficient in (
        ("wg-a", "wg-b", "0.03"),
        ("wg-b", "wg-c", "0.03"),
        ("wg-c", "wg-d", "0.02"),
        ("wg-a", "wg-c", "0.01"),
    ):
        service.add_connectivity("eng-1", {
            "from_group_id": from_group,
            "to_group_id": to_group,
            "coefficient": coefficient,
            "note": "构造-砂体连通",
        })


def _record_tests(service: WaterControlService, version: int, tested_at: str, well_range) -> None:
    for index in well_range:
        service.record_test("eng-1", {
            "well_id": f"LH-{index:02d}",
            "version": version,
            "tested_at": tested_at,
            "liquid_rate": str(300 + (index % 7) * 20),
            "water_cut": str(Decimal("0.55") + Decimal(index % 9) * Decimal("0.03") + Decimal(version - 1) * Decimal("0.01")),
            "injection_rate": "0",
        })
    for index in range(1, 5):
        service.record_test("eng-1", {
            "well_id": f"INJ-{index:02d}",
            "version": version,
            "tested_at": tested_at,
            "liquid_rate": "0",
            "water_cut": "0",
            "injection_rate": "1200",
        })


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc))
    service = WaterControlService(connection, clock)
    for user_id, role in (
        ("eng-1", "engineer"),
        ("eng-2", "engineer"),
        ("sup-1", "supervisor"),
        ("sup-2", "supervisor"),
        ("ops-1", "operator"),
        ("audit-1", "auditor"),
    ):
        service.create_user(user_id, user_id, role)
    _build_field(service)
    _record_tests(service, 1, "2026-10-04T00:00:00Z", range(1, 33))
    service.register_cap("eng-1", {
        "effective_from": "2026-10-01T00:00:00Z",
        "liquid_cap": "13000",
        "water_cap": "12000",
    })
    service.create_constraint_set("eng-1", {
        "constraint_set_id": "cs-liuhua-2026q4",
        "name": "2026Q4 稳油控水注采约束",
        "test_validity_hours": 72,
        "max_liquid_change_per_phase": "120",
        "increase_water_cut_limit": "0.85",
        "target_voidage_ratio": "1.0",
        "phase_hours": 24,
        "phase_count": 3,
    })
    plan_v1 = service.create_plan("eng-1", {
        "plan_id": "plan-liuhua-v1",
        "field_id": "liuhua",
        "horizon_starts_at": "2026-10-06T00:00:00Z",
        "constraint_set_id": "cs-liuhua-2026q4",
    }, "plan-liuhua-v1-key")
    service.confirm_plan("sup-1", "plan-liuhua-v1", 1)
    service.execute_phase("ops-1", "plan-liuhua-v1", 0, 2)

    # 异常井停产 + 部分井测试迟到：只重算未来时段。
    clock.advance(hours=30)
    service.report_event("eng-1", {
        "well_id": "LH-03",
        "event_type": "shut_in",
        "effective_at": "2026-10-07T00:00:00Z",
        "note": "井底流压异常，关井排查",
    })
    _record_tests(service, 2, "2026-10-07T06:00:00Z", range(1, 31))
    plan_v2 = service.recompute_plan("eng-1", {
        "source_plan_id": "plan-liuhua-v1",
        "plan_id": "plan-liuhua-v2",
        "reason": "LH-03 异常停产，LH-31/LH-32 测试迟到",
    }, "plan-liuhua-v2-key")
    service.confirm_plan("sup-2", "plan-liuhua-v2", 1)

    # 已执行指令必须原样继承，不被回写。
    phase0_v1 = service.explain_phase("eng-1", "plan-liuhua-v1", 0)["wells"]
    phase0_v2 = service.explain_phase("eng-1", "plan-liuhua-v2", 0)["wells"]
    inherited_ok = all(
        before["target_liquid"] == after["target_liquid"]
        and before["oil_rate"] == after["oil_rate"]
        and after["origin"] == "inherited"
        for before, after in zip(phase0_v1, phase0_v2)
    )
    if not inherited_ok or plan_v2["inherited_phases"] != [0]:
        raise RuntimeError("已执行指令继承校验失败")

    service.execute_phase("ops-1", "plan-liuhua-v2", 1, 2)
    explanation_v2 = service.explain_phase("eng-1", "plan-liuhua-v2", 2)
    actions = {well["well_id"]: well["action"] for well in explanation_v2["wells"]}
    if actions["LH-03"] != "shut" or actions["LH-31"] != "observe" or actions["LH-32"] != "observe":
        raise RuntimeError("异常停产或测试迟到井的未来时段处置不正确")

    # 人工覆盖：期限 + 双人批准后生效。
    current = next(well for well in explanation_v2["wells"] if well["well_id"] == "LH-10")
    override = service.request_override("eng-1", {
        "plan_id": "plan-liuhua-v2",
        "phase_index": 2,
        "well_id": "LH-10",
        "target_liquid": str(Decimal(current["target_liquid"]) - Decimal("50")),
        "reason": "邻井含水上升，人工压产观察",
        "expires_at": "2026-10-09T00:00:00Z",
    })
    service.approve_override("sup-1", override["override_id"])
    approval = service.approve_override("sup-2", override["override_id"])
    if approval["status"] != "approved":
        raise RuntimeError("人工覆盖双人批准未生效")
    applied = service.apply_override("eng-2", override["override_id"])
    if applied["status"] != "applied":
        raise RuntimeError("人工覆盖未应用")

    final_explanation = service.explain_phase("eng-1", "plan-liuhua-v2", 2)
    overridden = next(well for well in final_explanation["wells"] if well["well_id"] == "LH-10")
    if overridden["origin"] != "manual_override":
        raise RuntimeError("人工覆盖未写入指令来源")
    conservation = service.check_conservation("eng-1", "plan-liuhua-v2", 2)
    if not conservation["balanced"]:
        raise RuntimeError("油水与处理能力守恒核对失败")
    audit = service.audit_chain("audit-1")
    if not audit["valid"]:
        raise RuntimeError("审计链校验失败")
    result = {
        "status": "ok",
        "producers": 32,
        "injectors": 4,
        "plan_v1": {"version_no": plan_v1["version_no"], "snapshot_sha256": plan_v1["snapshot_sha256"]},
        "plan_v2": {
            "version_no": plan_v2["version_no"],
            "inherited_phases": plan_v2["inherited_phases"],
            "recomputed_phases": plan_v2["recomputed_phases"],
        },
        "override_id": override["override_id"],
        "override_approvers": approval["approvers"],
        "balanced": conservation["balanced"],
        "liquid_headroom": conservation["totals"]["liquid_headroom"],
        "audit_events": audit["events"],
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行稳油控水协同调度离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
