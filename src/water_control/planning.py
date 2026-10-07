"""稳油控水协同调度的确定性规划引擎与油水守恒核对。

同一冻结快照必然得到同一调度结果；所有流量使用十进制定点数，
产液按 0.001、含水按 0.0001 量化，油水切分保证每口井 油+水=产液 精确成立。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Iterable, Mapping


ZERO = Decimal("0")
ONE = Decimal("1")
RATE_QUANT = Decimal("0.001")
WC_QUANT = Decimal("0.0001")
SECONDS_PER_DAY = Decimal("86400")
NEIGHBOR_REFERENCE_RATE = Decimal("1000")
MAX_WATER_CUT = Decimal("0.9999")
MIN_STEP = RATE_QUANT

ACTION_LABELS = {
    "increase": "增产",
    "restrict": "限产",
    "maintain": "稳产",
    "observe": "观察",
    "shut": "停产",
    "inject": "注采调整",
}


class InfeasiblePlan(ValueError):
    """给定约束组合下不存在可行的调度方案。"""


def quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


def quantize_step(value: Decimal) -> Decimal:
    """增产量向下取整，保证任何约束都不会因舍入被突破。"""

    return value.quantize(RATE_QUANT, rounding=ROUND_DOWN)


def quantize_wc(value: Decimal) -> Decimal:
    return value.quantize(WC_QUANT, rounding=ROUND_HALF_UP)


def clamp_wc(value: Decimal) -> Decimal:
    return quantize_wc(min(MAX_WATER_CUT, max(ZERO, value)))


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed.astimezone(timezone.utc)


def moment_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class SnapshotWell:
    well_id: str
    kind: str
    group_id: str
    layer_system_id: str
    min_liquid: Decimal
    max_liquid: Decimal
    max_water_cut: Decimal
    min_injection: Decimal
    max_injection: Decimal
    test_version: int
    tested_at: str
    test_liquid: Decimal
    test_water_cut: Decimal
    test_injection: Decimal
    trend_per_day: Decimal
    stale_test: bool

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SnapshotWell":
        return cls(
            well_id=str(raw["well_id"]),
            kind=str(raw["kind"]),
            group_id=str(raw["group_id"]),
            layer_system_id=str(raw["layer_system_id"]),
            min_liquid=Decimal(str(raw["min_liquid"])),
            max_liquid=Decimal(str(raw["max_liquid"])),
            max_water_cut=Decimal(str(raw["max_water_cut"])),
            min_injection=Decimal(str(raw["min_injection"])),
            max_injection=Decimal(str(raw["max_injection"])),
            test_version=int(raw["test_version"]),
            tested_at=str(raw["tested_at"]),
            test_liquid=Decimal(str(raw["test_liquid"])),
            test_water_cut=Decimal(str(raw["test_water_cut"])),
            test_injection=Decimal(str(raw["test_injection"])),
            trend_per_day=Decimal(str(raw["trend_per_day"])),
            stale_test=bool(raw["stale_test"]),
        )


@dataclass(frozen=True, slots=True)
class NeighborPair:
    from_well_id: str
    to_well_id: str
    coefficient: Decimal
    from_group_id: str
    to_group_id: str


def expand_neighbor_pairs(
    edges: Iterable[Mapping[str, Any]], wells: Iterable[SnapshotWell]
) -> dict[str, tuple[NeighborPair, ...]]:
    """把井组级连通展开为井级影响对，展开顺序确定。"""

    producers_by_group: dict[str, list[SnapshotWell]] = {}
    for well in wells:
        if well.kind == "producer":
            producers_by_group.setdefault(well.group_id, []).append(well)
    pairs: dict[str, list[NeighborPair]] = {}
    ordered_edges = sorted(edges, key=lambda e: (str(e["from_group_id"]), str(e["to_group_id"])))
    for edge in ordered_edges:
        coefficient = Decimal(str(edge["coefficient"]))
        from_group = str(edge["from_group_id"])
        to_group = str(edge["to_group_id"])
        for source in producers_by_group.get(from_group, ()):
            for target in producers_by_group.get(to_group, ()):
                if source.well_id == target.well_id:
                    continue
                pairs.setdefault(source.well_id, []).append(
                    NeighborPair(source.well_id, target.well_id, coefficient, from_group, to_group)
                )
    return {key: tuple(sorted(value, key=lambda p: p.to_well_id)) for key, value in pairs.items()}


def natural_water_cut(well: SnapshotWell, at: datetime) -> Decimal:
    """按含水趋势外推指定时刻的自然含水（不含连通影响）。"""

    elapsed = Decimal(str((at - moment(well.tested_at)).total_seconds())) / SECONDS_PER_DAY
    return clamp_wc(well.test_water_cut + well.trend_per_day * elapsed)


def shut_event_at(events: Iterable[Mapping[str, Any]], at: datetime) -> Mapping[str, Any] | None:
    """返回指定时刻生效的停产事件；最近事件为恢复时返回 None。"""

    ordered = sorted(events, key=lambda e: (str(e["effective_at"]), int(e["event_id"])))
    active: Mapping[str, Any] | None = None
    for event in ordered:
        if moment(str(event["effective_at"])) > at:
            break
        active = event if event["event_type"] == "shut_in" else None
    return active


@dataclass
class _ProducerWork:
    well: SnapshotWell
    target: Decimal
    eff_wc: Decimal
    oil: Decimal = ZERO
    water: Decimal = ZERO
    shut: bool = False
    observe: bool = False
    action: str = ""
    reasons: list[dict[str, Any]] = field(default_factory=list)

    def split(self) -> None:
        """油水切分：先量化油，水=产液-油，保证每口井精确守恒。"""

        self.oil = quantize_rate(self.target * (ONE - self.eff_wc))
        self.water = self.target - self.oil


def _apply_delta(
    work: _ProducerWork,
    delta: Decimal,
    works: Mapping[str, _ProducerWork],
    pairs: Mapping[str, tuple[NeighborPair, ...]],
) -> None:
    work.target = quantize_rate(work.target + delta)
    work.split()
    for pair in pairs.get(work.well.well_id, ()):
        if pair.coefficient == ZERO:
            continue
        neighbor = works[pair.to_well_id]
        shift = pair.coefficient * delta / NEIGHBOR_REFERENCE_RATE
        neighbor.eff_wc = clamp_wc(neighbor.eff_wc + shift)
        neighbor.split()


def _totals(works: Iterable[_ProducerWork]) -> tuple[Decimal, Decimal]:
    liquid = ZERO
    water = ZERO
    for work in works:
        liquid += work.target
        water += work.water
    return liquid, water


def _restrict_pass(
    works: dict[str, _ProducerWork],
    pairs: Mapping[str, tuple[NeighborPair, ...]],
    max_change: Decimal,
    water_cap: Decimal,
    ramp_used: dict[str, Decimal],
) -> None:
    """控水限产：先压超单井含水上限的井，再压推高平台水量的高含水井。"""

    exhausted: set[str] = set()
    for _ in range(16 * (len(works) + 1)):
        over_wc = [
            work
            for work in works.values()
            if not work.shut and work.eff_wc > work.well.max_water_cut and work.well.well_id not in exhausted
        ]
        _, total_water = _totals(works.values())
        if over_wc:
            pick = max(over_wc, key=lambda w: (w.eff_wc, w.well.well_id))
            cause = "water_cut_limit"
        elif total_water > water_cap:
            pool = [
                work
                for work in works.values()
                if not work.shut and work.target > work.well.min_liquid and work.well.well_id not in exhausted
            ]
            if not pool:
                raise InfeasiblePlan("平台水处理上限不可满足：全部生产井已降至产液下限")
            pick = max(pool, key=lambda w: (w.eff_wc, w.well.well_id))
            cause = "platform_water_cap"
        else:
            return
        room = pick.target - pick.well.min_liquid
        ramp_left = max_change - ramp_used[pick.well.well_id]
        delta = quantize_rate(max(ZERO, min(room, ramp_left)))
        if delta < MIN_STEP:
            exhausted.add(pick.well.well_id)
            if pick.eff_wc > pick.well.max_water_cut:
                pick.reasons.append({
                    "code": "min_liquid_floor",
                    "message": "已降至产液下限仍超含水上限，需人工介入",
                    "detail": {
                        "water_cut": decimal_text(pick.eff_wc),
                        "max_water_cut": decimal_text(pick.well.max_water_cut),
                    },
                })
            continue
        if cause == "water_cut_limit":
            message = "井含水超过单井上限，限产控水"
            detail = {
                "water_cut": decimal_text(pick.eff_wc),
                "max_water_cut": decimal_text(pick.well.max_water_cut),
                "reduced_by": decimal_text(delta),
            }
        else:
            message = "平台水处理上限紧张，高含水井限产"
            detail = {
                "water_total": decimal_text(total_water),
                "water_cap": decimal_text(water_cap),
                "reduced_by": decimal_text(delta),
            }
        pick.reasons.append({"code": cause, "message": message, "detail": detail})
        pick.action = "restrict"
        _apply_delta(pick, -delta, works, pairs)
        ramp_used[pick.well.well_id] += delta
    raise InfeasiblePlan("限产迭代未收敛")


def _increase_pass(
    works: dict[str, _ProducerWork],
    pairs: Mapping[str, tuple[NeighborPair, ...]],
    max_change: Decimal,
    increase_limit: Decimal,
    liquid_cap: Decimal,
    water_cap: Decimal,
    ramp_used: dict[str, Decimal],
) -> None:
    """稳油增产：低含水井优先使用平台处理余量，且不推高邻井含水越限。"""

    candidates = sorted(
        (
            work
            for work in works.values()
            if not work.shut
            and not work.observe
            and work.action != "restrict"
            and work.eff_wc <= increase_limit
        ),
        key=lambda w: (w.eff_wc, w.well.well_id),
    )
    for work in candidates:
        well_id = work.well.well_id
        total_liquid, total_water = _totals(works.values())
        headroom = liquid_cap - total_liquid
        if headroom < MIN_STEP:
            work.reasons.append({
                "code": "headroom_exhausted",
                "message": "平台处理余量已耗尽，无法增产",
                "detail": {},
            })
            continue
        bounds: list[tuple[str, Decimal, dict[str, Any]]] = [
            ("ramp_limit", max_change - ramp_used[well_id], {}),
            ("well_max_liquid", work.well.max_liquid - work.target, {}),
            ("platform_liquid_headroom", headroom, {}),
        ]
        neighbor_load = ZERO
        well_pairs = pairs.get(well_id, ())
        for pair in well_pairs:
            neighbor_load += pair.coefficient * works[pair.to_well_id].target / NEIGHBOR_REFERENCE_RATE
        water_per_step = work.eff_wc + neighbor_load
        if water_per_step > ZERO:
            bounds.append(("platform_water_cap", (water_cap - total_water) / water_per_step, {}))
        for pair in well_pairs:
            if pair.coefficient <= ZERO:
                continue
            neighbor = works[pair.to_well_id]
            allowed = (neighbor.well.max_water_cut - neighbor.eff_wc) * NEIGHBOR_REFERENCE_RATE / pair.coefficient
            bounds.append((
                "neighbor_water_cut",
                allowed,
                {
                    "neighbor_well_id": pair.to_well_id,
                    "from_group_id": pair.from_group_id,
                    "to_group_id": pair.to_group_id,
                },
            ))
        limiting_name, limiting_value, limiting_detail = min(bounds, key=lambda item: (item[1], item[0]))
        delta = quantize_step(max(ZERO, limiting_value))
        if delta < MIN_STEP:
            messages = {
                "ramp_limit": "单阶段调幅已用完，无法继续增产",
                "well_max_liquid": "已达单井产液上限",
                "platform_liquid_headroom": "平台处理余量不足，无法增产",
                "platform_water_cap": "平台水处理余量不足，无法增产",
                "neighbor_water_cut": "连通邻井含水接近上限，增产被限制",
            }
            work.reasons.append({
                "code": limiting_name,
                "message": messages[limiting_name],
                "detail": limiting_detail,
            })
            continue
        work.reasons.append({
            "code": "low_water_cut_priority",
            "message": "低含水井优先使用平台处理余量增产",
            "detail": {"water_cut": decimal_text(work.eff_wc)},
        })
        work.reasons.append({
            "code": "platform_liquid_headroom",
            "message": "平台处理余量允许增产",
            "detail": {"headroom_before": decimal_text(headroom), "increased_by": decimal_text(delta)},
        })
        if limiting_name in {"ramp_limit", "neighbor_water_cut", "platform_water_cap"}:
            work.reasons.append({
                "code": limiting_name,
                "message": {
                    "ramp_limit": "增产量受单阶段调幅上限约束",
                    "neighbor_water_cut": "增产量受连通邻井含水上限约束",
                    "platform_water_cap": "增产量受平台水处理上限约束",
                }[limiting_name],
                "detail": limiting_detail,
            })
        work.action = "increase"
        _apply_delta(work, delta, works, pairs)
        ramp_used[well_id] += delta


def _allocate_injection(
    injectors: Iterable[SnapshotWell],
    total_liquid: Decimal,
    voidage_ratio: Decimal,
) -> dict[str, Decimal]:
    """按注采平衡目标在注水井间分配注入量，分配顺序确定。"""

    target_total = quantize_rate(voidage_ratio * total_liquid)
    ordered = sorted(injectors, key=lambda w: w.well_id)
    if not ordered:
        if target_total > ZERO:
            raise InfeasiblePlan("缺少注水井，注采约束不可满足")
        return {}
    min_sum = sum((well.min_injection for well in ordered), ZERO)
    max_sum = sum((well.max_injection for well in ordered), ZERO)
    if min_sum > target_total or max_sum < target_total:
        raise InfeasiblePlan("注采约束不可满足：注入能力区间不含目标注入量")
    assigned = {well.well_id: well.min_injection for well in ordered}
    remaining = target_total - min_sum
    pool = [well for well in ordered if well.max_injection > well.min_injection]
    while remaining >= MIN_STEP and pool:
        weight = sum((well.max_injection - assigned[well.well_id] for well in pool), ZERO)
        if weight <= ZERO:
            break
        progressed = False
        for well in list(pool):
            spare = well.max_injection - assigned[well.well_id]
            share = quantize_step(min(spare, remaining * spare / weight, remaining))
            if share >= MIN_STEP:
                assigned[well.well_id] += share
                remaining -= share
                progressed = True
            if well.max_injection - assigned[well.well_id] < MIN_STEP:
                pool.remove(well)
        if not progressed:
            well = pool.pop(0)
            top_up = min(remaining, well.max_injection - assigned[well.well_id])
            assigned[well.well_id] += top_up
            remaining -= top_up
    if remaining >= MIN_STEP:
        raise InfeasiblePlan("注采约束不可满足：注入分配余量无法消化")
    return assigned


def phase_totals(
    commands: Iterable[Mapping[str, Any]],
    *,
    liquid_cap: Decimal,
    water_cap: Decimal,
    target_voidage_ratio: Decimal,
) -> dict[str, Any]:
    """由井指令汇总阶段总量；处理余量恒等式 总量+余量=上限 在此成立。"""

    liquid = ZERO
    oil = ZERO
    water = ZERO
    injection = ZERO
    for command in commands:
        liquid += Decimal(str(command.get("target_liquid") or "0"))
        oil += Decimal(str(command.get("oil_rate") or "0"))
        water += Decimal(str(command.get("water_rate") or "0"))
        injection += Decimal(str(command.get("target_injection") or "0"))
    voidage_actual = None if liquid == ZERO else quantize_wc(injection / liquid)
    return {
        "liquid_total": decimal_text(liquid),
        "oil_total": decimal_text(oil),
        "water_total": decimal_text(water),
        "injection_total": decimal_text(injection),
        "liquid_cap": decimal_text(liquid_cap),
        "water_cap": decimal_text(water_cap),
        "liquid_headroom": decimal_text(liquid_cap - liquid),
        "water_headroom": decimal_text(water_cap - water),
        "voidage_ratio_target": decimal_text(target_voidage_ratio),
        "voidage_ratio_actual": None if voidage_actual is None else decimal_text(voidage_actual),
    }


def plan_phases(snapshot: Mapping[str, Any], *, skip_before: int = 0) -> list[dict[str, Any]]:
    """按冻结快照逐阶段生成井指令；skip_before 之前的阶段由已执行指令继承，不参与重算。"""

    horizon_cfg = snapshot["horizon"]
    horizon = moment(str(horizon_cfg["starts_at"]))
    phase_hours = int(horizon_cfg["phase_hours"])
    phase_count = int(horizon_cfg["phase_count"])
    constraints = snapshot["constraints"]
    caps = snapshot["caps"]
    max_change = Decimal(str(constraints["max_liquid_change_per_phase"]))
    increase_limit = Decimal(str(constraints["increase_water_cut_limit"]))
    voidage_ratio = Decimal(str(constraints["target_voidage_ratio"]))
    liquid_cap = Decimal(str(caps["liquid_cap"]))
    water_cap = Decimal(str(caps["water_cap"]))
    wells = [SnapshotWell.from_mapping(raw) for raw in snapshot["wells"]]
    producers = [well for well in wells if well.kind == "producer"]
    injectors = [well for well in wells if well.kind == "injector"]
    pairs = expand_neighbor_pairs(snapshot["edges"], wells)
    events_by_well: dict[str, list[Mapping[str, Any]]] = {}
    for event in snapshot["events"]:
        events_by_well.setdefault(str(event["well_id"]), []).append(event)
    initial = {str(key): Decimal(str(value)) for key, value in snapshot.get("initial_targets", {}).items()}
    carryover = {
        well.well_id: quantize_rate(initial.get(well.well_id, well.test_liquid)) for well in producers
    }
    phases: list[dict[str, Any]] = []
    for index in range(skip_before, phase_count):
        start = horizon + timedelta(hours=phase_hours * index)
        end = start + timedelta(hours=phase_hours)
        works: dict[str, _ProducerWork] = {}
        for well in producers:
            event = shut_event_at(events_by_well.get(well.well_id, ()), start)
            shut = event is not None
            observe = not shut and well.stale_test
            work = _ProducerWork(
                well=well,
                target=ZERO if shut else carryover[well.well_id],
                eff_wc=natural_water_cut(well, start),
                shut=shut,
                observe=observe,
            )
            if shut:
                work.action = "shut"
                work.reasons.append({
                    "code": "shut_in_event",
                    "message": "异常停产事件生效，未来时段指令清零",
                    "detail": {"event_id": event["event_id"], "effective_at": event["effective_at"]},
                })
            elif observe:
                work.action = "observe"
                work.reasons.append({
                    "code": "test_stale",
                    "message": "测试版本超出有效期（测试迟到），转入观察并维持现状",
                    "detail": {"test_version": well.test_version, "tested_at": well.tested_at},
                })
            work.split()
            works[well.well_id] = work
        ramp_used = {well.well_id: ZERO for well in producers}
        _restrict_pass(works, pairs, max_change, water_cap, ramp_used)
        _increase_pass(works, pairs, max_change, increase_limit, liquid_cap, water_cap, ramp_used)
        for work in works.values():
            if work.action:
                continue
            work.action = "maintain"
            if work.eff_wc > increase_limit:
                work.reasons.append({
                    "code": "water_cut_above_increase_threshold",
                    "message": "含水高于增产门槛，维持现状",
                    "detail": {
                        "water_cut": decimal_text(work.eff_wc),
                        "increase_water_cut_limit": decimal_text(increase_limit),
                    },
                })
            else:
                work.reasons.append({
                    "code": "hold_baseline",
                    "message": "维持当前产液，等待下一阶段评估",
                    "detail": {},
                })
        total_liquid, _ = _totals(works.values())
        injection = _allocate_injection(injectors, total_liquid, voidage_ratio)
        commands: list[dict[str, Any]] = []
        for well_id in sorted(works):
            work = works[well_id]
            commands.append({
                "well_id": well_id,
                "action": work.action,
                "origin": "engine",
                "target_liquid": decimal_text(quantize_rate(work.target)),
                "target_injection": None,
                "projected_water_cut": decimal_text(work.eff_wc),
                "oil_rate": decimal_text(work.oil),
                "water_rate": decimal_text(work.water),
                "reasons": work.reasons,
            })
        for well in injectors:
            assigned = injection.get(well.well_id, ZERO)
            commands.append({
                "well_id": well.well_id,
                "action": "inject",
                "origin": "engine",
                "target_liquid": decimal_text(quantize_rate(ZERO)),
                "target_injection": decimal_text(quantize_rate(assigned)),
                "projected_water_cut": None,
                "oil_rate": decimal_text(quantize_rate(ZERO)),
                "water_rate": decimal_text(quantize_rate(ZERO)),
                "reasons": [{
                    "code": "voidage_balance",
                    "message": "按注采平衡目标分配注入量",
                    "detail": {
                        "target_voidage_ratio": decimal_text(voidage_ratio),
                        "assigned_injection": decimal_text(assigned),
                    },
                }],
            })
        commands.sort(key=lambda item: item["well_id"])
        phases.append({
            "phase_index": index,
            "starts_at": moment_text(start),
            "ends_at": moment_text(end),
            "commands": commands,
            "totals": phase_totals(
                commands,
                liquid_cap=liquid_cap,
                water_cap=water_cap,
                target_voidage_ratio=voidage_ratio,
            ),
        })
        carryover = {well_id: works[well_id].target for well_id in works}
    return phases


def conservation_report(
    *,
    commands: Iterable[Mapping[str, Any]],
    wells: Mapping[str, Mapping[str, Any]],
    liquid_cap: Decimal,
    water_cap: Decimal,
    target_voidage_ratio: Decimal,
) -> dict[str, Any]:
    """核对一段时期内每口井与平台整体的油、水、处理能力守恒。"""

    violations: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    ordered = sorted(commands, key=lambda item: str(item["well_id"]))
    for command in ordered:
        well_id = str(command["well_id"])
        well = wells[well_id]
        liquid = Decimal(str(command.get("target_liquid") or "0"))
        oil = Decimal(str(command.get("oil_rate") or "0"))
        water = Decimal(str(command.get("water_rate") or "0"))
        injection = Decimal(str(command.get("target_injection") or "0"))
        if oil + water != liquid:
            violations.append({
                "code": "split_imbalance",
                "well_id": well_id,
                "message": "油水之和与产液不守恒",
                "detail": {
                    "target_liquid": decimal_text(liquid),
                    "oil_rate": decimal_text(oil),
                    "water_rate": decimal_text(water),
                },
            })
        action = str(command.get("action") or "")
        if well["kind"] == "producer" and action != "shut":
            min_liquid = Decimal(str(well["min_liquid"]))
            max_liquid = Decimal(str(well["max_liquid"]))
            if liquid < min_liquid or liquid > max_liquid:
                violations.append({
                    "code": "liquid_out_of_bounds",
                    "well_id": well_id,
                    "message": "产液超出单井上下限",
                    "detail": {
                        "target_liquid": decimal_text(liquid),
                        "min_liquid": decimal_text(min_liquid),
                        "max_liquid": decimal_text(max_liquid),
                    },
                })
            projected = command.get("projected_water_cut")
            if projected is not None and Decimal(str(projected)) > Decimal(str(well["max_water_cut"])):
                violations.append({
                    "code": "water_cut_above_max",
                    "well_id": well_id,
                    "message": "预测含水超过单井上限",
                    "detail": {
                        "projected_water_cut": str(projected),
                        "max_water_cut": str(well["max_water_cut"]),
                    },
                })
        if well["kind"] == "injector":
            min_injection = Decimal(str(well["min_injection"]))
            max_injection = Decimal(str(well["max_injection"]))
            if injection < min_injection or injection > max_injection:
                violations.append({
                    "code": "injection_out_of_bounds",
                    "well_id": well_id,
                    "message": "注入量超出单井上下限",
                    "detail": {
                        "target_injection": decimal_text(injection),
                        "min_injection": decimal_text(min_injection),
                        "max_injection": decimal_text(max_injection),
                    },
                })
        rows.append({
            "well_id": well_id,
            "action": action,
            "target_liquid": decimal_text(liquid),
            "oil_rate": decimal_text(oil),
            "water_rate": decimal_text(water),
            "target_injection": decimal_text(injection),
        })
    totals = phase_totals(
        ordered,
        liquid_cap=liquid_cap,
        water_cap=water_cap,
        target_voidage_ratio=target_voidage_ratio,
    )
    liquid_total = Decimal(totals["liquid_total"])
    water_total = Decimal(totals["water_total"])
    oil_total = Decimal(totals["oil_total"])
    if oil_total + water_total != liquid_total:
        violations.append({
            "code": "total_split_imbalance",
            "well_id": None,
            "message": "平台油水总量与产液总量不守恒",
            "detail": totals,
        })
    if liquid_total > liquid_cap:
        violations.append({
            "code": "platform_liquid_cap_exceeded",
            "well_id": None,
            "message": "平台产液总量超过处理上限",
            "detail": {"liquid_total": decimal_text(liquid_total), "liquid_cap": decimal_text(liquid_cap)},
        })
    if water_total > water_cap:
        violations.append({
            "code": "platform_water_cap_exceeded",
            "well_id": None,
            "message": "平台水量超过水处理上限",
            "detail": {"water_total": decimal_text(water_total), "water_cap": decimal_text(water_cap)},
        })
    voidage_deviation = None
    if totals["voidage_ratio_actual"] is not None:
        voidage_deviation = decimal_text(
            Decimal(totals["voidage_ratio_actual"]) - target_voidage_ratio
        )
    return {
        "balanced": not violations,
        "violations": violations,
        "totals": totals,
        "voidage": {
            "target": decimal_text(target_voidage_ratio),
            "actual": totals["voidage_ratio_actual"],
            "deviation": voidage_deviation,
        },
        "wells": rows,
    }
