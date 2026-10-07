"""稳油控水协同的确定性计算引擎。

所有计算只使用 Decimal 与排序后的输入，同一冻结快照必然得到同一组指令，
从而保证方案可重放、可审计、可解释。
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .clock import parse_utc


ZERO = Decimal("0")
HUNDRED = Decimal("100")
FLAT_SLOPE = Decimal("0.05")
ALGORITHM_VERSION = "water-control-coordination/1"


def quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def quantize_percent(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _days_between(starts_at: str, ends_at: str) -> Decimal:
    delta = parse_utc(ends_at) - parse_utc(starts_at)
    return Decimal(str(delta.total_seconds())) / Decimal(86400)


def water_cut_trend(tests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """由最近至多 5 次测试按最小二乘法拟合含水趋势（百分点/天）。"""
    ordered = sorted(tests, key=lambda item: (str(item["tested_at"]), str(item["test_id"])))[-5:]
    if len(ordered) < 2:
        return {"slope_per_day": "0.0000", "direction": "flat", "samples": len(ordered)}
    origin = str(ordered[0]["tested_at"])
    points = [
        (_days_between(origin, str(item["tested_at"])), Decimal(str(item["water_cut_percent"])))
        for item in ordered
    ]
    count = Decimal(len(points))
    mean_x = sum((point[0] for point in points), ZERO) / count
    mean_y = sum((point[1] for point in points), ZERO) / count
    variance = sum(((point[0] - mean_x) ** 2 for point in points), ZERO)
    if variance == ZERO:
        slope = ZERO
    else:
        covariance = sum(((point[0] - mean_x) * (point[1] - mean_y) for point in points), ZERO)
        slope = covariance / variance
    slope = quantize_percent(slope)
    if slope > FLAT_SLOPE:
        direction = "rising"
    elif slope < -FLAT_SLOPE:
        direction = "falling"
    else:
        direction = "flat"
    return {"slope_per_day": decimal_text(slope), "direction": direction, "samples": len(points)}


def projected_water_cut(snapshot: Mapping[str, Any], well_id: str, stage_date: str) -> Decimal:
    """按冻结测试版本与含水趋势外推某井在指定日期的含水率。"""
    test = snapshot["tests"].get(well_id)
    if test is None:
        return ZERO
    slope = Decimal(str(snapshot["trends"].get(well_id, {}).get("slope_per_day", "0")))
    days = max(ZERO, _days_between(str(test["tested_at"]), f"{stage_date}T00:00:00Z"))
    return min(HUNDRED, max(ZERO, Decimal(str(test["water_cut_percent"])) + slope * days))


def _clamp_rate(value: Decimal, minimum: Decimal, maximum: Decimal | None) -> Decimal:
    result = max(value, minimum)
    if maximum is not None:
        result = min(result, maximum)
    return quantize_rate(result)


def compute_stage(snapshot: Mapping[str, Any], stage_date: str) -> list[dict[str, Any]]:
    """按冻结快照计算某一天各井的协同指令与机器可读原因。"""
    parameters = snapshot["parameters"]
    observe_threshold = Decimal(str(parameters["observe_threshold_percent"]))
    limit_threshold = Decimal(str(parameters["limit_threshold_percent"]))
    increase_threshold = Decimal(str(parameters["increase_threshold_percent"]))
    increase_cap = Decimal(str(parameters["increase_cap_percent"])) / HUNDRED
    limit_cut = Decimal(str(parameters["limit_cut_percent"])) / HUNDRED
    capacity = snapshot["capacity"]
    liquid_limit = Decimal(str(capacity["liquid_limit"]))
    water_limit = Decimal(str(capacity["water_limit"]))
    tests: Mapping[str, Any] = snapshot["tests"]
    trends: Mapping[str, Any] = snapshot["trends"]
    constraints: Mapping[str, Any] = snapshot["constraints"]
    links = sorted(snapshot["links"], key=lambda item: str(item["link_id"]))
    wells = sorted(snapshot["wells"], key=lambda item: str(item["well_id"]))

    def bounds(well_id: str) -> tuple[Decimal, Decimal | None]:
        row = constraints.get(well_id)
        if row is None:
            return ZERO, None
        maximum = row.get("max_rate")
        return Decimal(str(row["min_rate"])), None if maximum is None else Decimal(str(maximum))

    projected: dict[str, Decimal] = {}
    for well in wells:
        if well["well_id"] in tests:
            projected[well["well_id"]] = projected_water_cut(snapshot, well["well_id"], stage_date)

    instructions: dict[str, dict[str, Any]] = {}
    base_liquid: dict[str, Decimal] = {}
    candidates: list[str] = []

    for well in wells:
        well_id = well["well_id"]
        if well["kind"] != "producer":
            continue
        minimum, maximum = bounds(well_id)
        test = tests.get(well_id)
        if well["state"] == "shut":
            instructions[well_id] = {
                "well_id": well_id, "action": "shut", "target_liquid_rate": ZERO,
                "reasons": [{"code": "well_shut"}],
            }
            base_liquid[well_id] = ZERO
            continue
        if test is None:
            target = _clamp_rate(ZERO, minimum, maximum)
            instructions[well_id] = {
                "well_id": well_id, "action": "observe", "target_liquid_rate": target,
                "reasons": [{"code": "test_missing"}],
            }
            base_liquid[well_id] = target
            continue
        base = _clamp_rate(Decimal(str(test["liquid_rate"])), minimum, maximum)
        base_liquid[well_id] = base
        water_cut = projected[well_id]
        slope = Decimal(str(trends.get(well_id, {}).get("slope_per_day", "0")))
        if water_cut >= limit_threshold:
            target = _clamp_rate(quantize_rate(base * (Decimal(1) - limit_cut)), minimum, maximum)
            instructions[well_id] = {
                "well_id": well_id, "action": "limit", "target_liquid_rate": target,
                "reasons": [{
                    "code": "water_cut_above_limit",
                    "projected_water_cut": decimal_text(quantize_percent(water_cut)),
                    "threshold_percent": decimal_text(limit_threshold),
                }],
            }
        elif water_cut >= observe_threshold:
            code = "water_cut_rising_watch" if slope > ZERO else "water_cut_above_observe"
            instructions[well_id] = {
                "well_id": well_id, "action": "observe", "target_liquid_rate": base,
                "reasons": [{
                    "code": code,
                    "projected_water_cut": decimal_text(quantize_percent(water_cut)),
                    "threshold_percent": decimal_text(observe_threshold),
                }],
            }
        elif water_cut <= increase_threshold and slope <= FLAT_SLOPE:
            candidates.append(well_id)
            instructions[well_id] = {
                "well_id": well_id, "action": "maintain", "target_liquid_rate": base,
                "reasons": [],
            }
        else:
            instructions[well_id] = {
                "well_id": well_id, "action": "maintain", "target_liquid_rate": base,
                "reasons": [{"code": "within_band", "projected_water_cut": decimal_text(quantize_percent(water_cut))}],
            }

    # 平台处理余量按含水从低到高分配给候选增产井。
    committed = sum(
        (instructions[well_id]["target_liquid_rate"] for well_id in instructions if well_id not in candidates),
        ZERO,
    )
    remaining = max(ZERO, quantize_rate(liquid_limit - committed - sum((base_liquid[item] for item in candidates), ZERO)))
    for well_id in sorted(candidates, key=lambda item: (projected[item], item)):
        minimum, maximum = bounds(well_id)
        base = base_liquid[well_id]
        room = quantize_rate(base * increase_cap)
        if maximum is not None:
            room = min(room, max(ZERO, quantize_rate(maximum - base)))
        increase = max(ZERO, min(room, remaining))
        target = quantize_rate(base + increase)
        remaining = quantize_rate(remaining - increase)
        water_cut = projected[well_id]
        if increase > ZERO:
            instructions[well_id] = {
                "well_id": well_id, "action": "increase", "target_liquid_rate": target,
                "reasons": [
                    {
                        "code": "stable_low_water_cut",
                        "projected_water_cut": decimal_text(quantize_percent(water_cut)),
                        "threshold_percent": decimal_text(increase_threshold),
                    },
                    {
                        "code": "capacity_headroom_allocated",
                        "increase_liquid_rate": decimal_text(increase),
                        "liquid_limit": decimal_text(liquid_limit),
                    },
                ],
            }
        else:
            instructions[well_id]["reasons"] = [
                {"code": "no_capacity_headroom", "liquid_limit": decimal_text(liquid_limit)}
            ]

    # 平台水处理上限：按含水从高到低压减，直到总水量回到限值内。
    def water_of(well_id: str) -> Decimal:
        return quantize_rate(instructions[well_id]["target_liquid_rate"] * projected[well_id] / HUNDRED)

    excess = quantize_rate(sum((water_of(item) for item in instructions), ZERO) - water_limit)
    if excess > ZERO:
        order = sorted(instructions, key=lambda item: (-projected[item], item))
        for _ in range(10):
            if excess <= ZERO:
                break
            changed = False
            for well_id in order:
                if excess <= ZERO:
                    break
                fraction = projected[well_id] / HUNDRED
                if fraction <= ZERO:
                    continue
                minimum, _ = bounds(well_id)
                target = instructions[well_id]["target_liquid_rate"]
                removable_water = quantize_rate((target - minimum) * fraction)
                if removable_water <= ZERO:
                    continue
                cut_water = min(excess, removable_water)
                new_target = max(minimum, quantize_rate(target - cut_water / fraction))
                if new_target >= target:
                    continue
                instructions[well_id]["target_liquid_rate"] = new_target
                if instructions[well_id]["action"] in {"increase", "maintain"}:
                    instructions[well_id]["action"] = "limit"
                instructions[well_id]["reasons"].append({
                    "code": "water_capacity_limited",
                    "water_limit": decimal_text(water_limit),
                })
                excess = quantize_rate(excess - quantize_rate((target - new_target) * fraction))
                changed = True
            if not changed:
                break

    # 注水井目标跟随连通生产井，受注采约束封顶时等比回压生产井的增产量。
    producer_scale: dict[str, tuple[Decimal, str]] = {}
    injector_targets: dict[str, dict[str, Any]] = {}
    for well in wells:
        well_id = well["well_id"]
        if well["kind"] != "injector":
            continue
        minimum, maximum = bounds(well_id)
        if well["state"] == "shut":
            injector_targets[well_id] = {
                "well_id": well_id, "action": "shut", "target_injection_rate": ZERO,
                "reasons": [{"code": "well_shut"}],
            }
            for link in links:
                if link["injector_id"] == well_id and link["producer_id"] in instructions:
                    producer_scale[link["producer_id"]] = (ZERO, well_id)
            continue
        required = ZERO
        for link in links:
            if link["injector_id"] != well_id:
                continue
            producer = instructions.get(link["producer_id"])
            if producer is None:
                continue
            required += producer["target_liquid_rate"] * Decimal(str(link["coefficient_percent"])) / HUNDRED
        required = quantize_rate(required)
        if maximum is not None and required > maximum:
            injector_targets[well_id] = {
                "well_id": well_id, "action": "limit", "target_injection_rate": maximum,
                "reasons": [{
                    "code": "injection_capped",
                    "required_injection_rate": decimal_text(required),
                    "max_rate": decimal_text(maximum),
                }],
            }
            scale = ZERO if required == ZERO else maximum / required
            for link in links:
                if link["injector_id"] == well_id and link["producer_id"] in instructions:
                    current = producer_scale.get(link["producer_id"])
                    if current is None or scale < current[0]:
                        producer_scale[link["producer_id"]] = (scale, well_id)
        elif required < minimum:
            injector_targets[well_id] = {
                "well_id": well_id, "action": "maintain", "target_injection_rate": minimum,
                "reasons": [{"code": "injection_minimum", "min_rate": decimal_text(minimum)}],
            }
        else:
            injector_targets[well_id] = {
                "well_id": well_id, "action": "maintain", "target_injection_rate": required,
                "reasons": [{"code": "injection_follows_producers"}],
            }

    for producer_id, (scale, injector_id) in sorted(producer_scale.items()):
        instruction = instructions[producer_id]
        increase = instruction["target_liquid_rate"] - base_liquid[producer_id]
        if increase <= ZERO:
            continue
        new_target = quantize_rate(base_liquid[producer_id] + increase * scale)
        instruction["target_liquid_rate"] = new_target
        if new_target <= base_liquid[producer_id]:
            instruction["action"] = "maintain"
        instruction["reasons"].append({
            "code": "injector_support_limited",
            "injector_id": injector_id,
            "support_scale": decimal_text(quantize_percent(scale)),
        })

    # 注水量上调可能推高邻井含水，受波及的稳产井转入观察。
    for well in wells:
        well_id = well["well_id"]
        if well["kind"] != "injector" or well_id not in injector_targets:
            continue
        target = injector_targets[well_id]["target_injection_rate"]
        test = tests.get(well_id)
        current = Decimal(str(test["liquid_rate"])) if test is not None else ZERO
        delta = target - current
        if delta <= ZERO:
            continue
        for link in links:
            if link["injector_id"] != well_id:
                continue
            producer = instructions.get(link["producer_id"])
            if producer is None or producer["action"] != "maintain":
                continue
            liquid = producer["target_liquid_rate"]
            if liquid <= ZERO:
                continue
            coefficient = Decimal(str(link["coefficient_percent"])) / HUNDRED
            risk_cut = projected[link["producer_id"]] + coefficient * delta / liquid * HUNDRED
            if quantize_percent(risk_cut) >= observe_threshold:
                producer["action"] = "observe"
                producer["reasons"].append({
                    "code": "connectivity_water_risk",
                    "injector_id": well_id,
                    "risk_water_cut": decimal_text(quantize_percent(risk_cut)),
                    "threshold_percent": decimal_text(observe_threshold),
                })

    # 油水拆分：水量取整后油量取差，保证单井油+水=液严格守恒。
    result: list[dict[str, Any]] = []
    for well in wells:
        well_id = well["well_id"]
        if well["kind"] == "injector":
            row = injector_targets[well_id]
            result.append({
                "well_id": well_id,
                "action": row["action"],
                "target_liquid_rate": "0.000",
                "target_oil_rate": "0.000",
                "target_water_rate": "0.000",
                "target_injection_rate": decimal_text(quantize_rate(row["target_injection_rate"])),
                "reasons": row["reasons"],
            })
            continue
        row = instructions[well_id]
        liquid = quantize_rate(row["target_liquid_rate"])
        water = quantize_rate(liquid * projected.get(well_id, ZERO) / HUNDRED)
        oil = quantize_rate(liquid - water)
        result.append({
            "well_id": well_id,
            "action": row["action"],
            "target_liquid_rate": decimal_text(liquid),
            "target_oil_rate": decimal_text(oil),
            "target_water_rate": decimal_text(water),
            "target_injection_rate": "0.000",
            "reasons": row["reasons"],
        })
    return result


def reconcile_stage(
    instructions: Sequence[Mapping[str, Any]],
    capacity: Mapping[str, Any],
) -> dict[str, Any]:
    """核对单阶段油、水与处理能力守恒，返回机器可读的差异清单。"""
    liquid_limit = Decimal(str(capacity["liquid_limit"]))
    water_limit = Decimal(str(capacity["water_limit"]))
    oil = water = liquid = injection = ZERO
    well_imbalances: list[dict[str, str]] = []
    for row in instructions:
        row_oil = Decimal(str(row["target_oil_rate"]))
        row_water = Decimal(str(row["target_water_rate"]))
        row_liquid = Decimal(str(row["target_liquid_rate"]))
        oil += row_oil
        water += row_water
        liquid += row_liquid
        injection += Decimal(str(row["target_injection_rate"]))
        if row_liquid - row_oil - row_water != ZERO:
            well_imbalances.append({"well_id": str(row["well_id"])})
    violations: list[dict[str, str]] = []
    balance = quantize_rate(liquid - oil - water)
    if balance != ZERO:
        violations.append({"code": "oil_water_imbalance", "balance": decimal_text(balance)})
    if liquid > liquid_limit:
        violations.append({
            "code": "liquid_capacity_exceeded",
            "liquid_total": decimal_text(quantize_rate(liquid)),
            "liquid_limit": decimal_text(liquid_limit),
        })
    if water > water_limit:
        violations.append({
            "code": "water_capacity_exceeded",
            "water_total": decimal_text(quantize_rate(water)),
            "water_limit": decimal_text(water_limit),
        })
    for item in well_imbalances:
        violations.append({"code": "well_oil_water_imbalance", **item})
    return {
        "oil_total": decimal_text(quantize_rate(oil)),
        "water_total": decimal_text(quantize_rate(water)),
        "liquid_total": decimal_text(quantize_rate(liquid)),
        "injection_total": decimal_text(quantize_rate(injection)),
        "oil_water_balance": decimal_text(balance),
        "liquid_limit": decimal_text(liquid_limit),
        "water_limit": decimal_text(water_limit),
        "conserved": not violations,
        "violations": violations,
    }
