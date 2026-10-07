"""稳油控水协同调度的领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
WELL_KINDS = {"producer", "injector"}
EVENT_TYPES = {"shut_in", "restore"}
ZERO = Decimal("0")
MAX_WATER_CUT = Decimal("0.9999")


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str, maximum: int = 10000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ValidationFailed(f"{field} 必须是 1 到 {maximum} 的整数")
    return value


def moment_text(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        from .clock import utc_text

        return utc_text(parse_utc(text, field))
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class WellInput:
    well_id: str
    name: str
    kind: str
    layer_system_id: str
    group_id: str
    min_liquid: Decimal
    max_liquid: Decimal
    max_water_cut: Decimal
    min_injection: Decimal
    max_injection: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WellInput":
        kind = required_text(raw.get("kind"), "kind", 16)
        if kind not in WELL_KINDS:
            raise ValidationFailed("kind 必须是 producer 或 injector")
        if kind == "producer":
            min_liquid = decimal_value(raw.get("min_liquid"), "min_liquid", minimum=ZERO)
            max_liquid = decimal_value(raw.get("max_liquid"), "max_liquid", minimum=ZERO)
            if max_liquid < min_liquid:
                raise ValidationFailed("max_liquid 不能小于 min_liquid")
            max_water_cut = decimal_value(
                raw.get("max_water_cut"), "max_water_cut", minimum=Decimal("0.0001"), maximum=MAX_WATER_CUT
            )
            min_injection = max_injection = ZERO
        else:
            min_injection = decimal_value(raw.get("min_injection"), "min_injection", minimum=ZERO)
            max_injection = decimal_value(raw.get("max_injection"), "max_injection", minimum=ZERO)
            if max_injection < min_injection:
                raise ValidationFailed("max_injection 不能小于 min_injection")
            min_liquid = max_liquid = ZERO
            max_water_cut = MAX_WATER_CUT
        return cls(
            well_id=identifier(raw.get("well_id"), "well_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            layer_system_id=identifier(raw.get("layer_system_id"), "layer_system_id"),
            group_id=identifier(raw.get("group_id"), "group_id"),
            min_liquid=min_liquid,
            max_liquid=max_liquid,
            max_water_cut=max_water_cut,
            min_injection=min_injection,
            max_injection=max_injection,
        )


@dataclass(frozen=True, slots=True)
class EdgeInput:
    from_group_id: str
    to_group_id: str
    coefficient: Decimal
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EdgeInput":
        from_group = identifier(raw.get("from_group_id"), "from_group_id")
        to_group = identifier(raw.get("to_group_id"), "to_group_id")
        if from_group == to_group:
            raise ValidationFailed("井组连通起点和终点不能相同")
        return cls(
            from_group_id=from_group,
            to_group_id=to_group,
            coefficient=decimal_value(
                raw.get("coefficient"), "coefficient", minimum=ZERO, maximum=Decimal("1")
            ),
            note=required_text(raw.get("note", "井组连通"), "note"),
        )


@dataclass(frozen=True, slots=True)
class TestInput:
    well_id: str
    version: int
    tested_at: str
    liquid_rate: Decimal
    water_cut: Decimal
    injection_rate: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TestInput":
        return cls(
            well_id=identifier(raw.get("well_id"), "well_id"),
            version=positive_integer(raw.get("version"), "version"),
            tested_at=moment_text(raw.get("tested_at"), "tested_at"),
            liquid_rate=decimal_value(raw.get("liquid_rate", 0), "liquid_rate", minimum=ZERO),
            water_cut=decimal_value(
                raw.get("water_cut", 0), "water_cut", minimum=ZERO, maximum=MAX_WATER_CUT
            ),
            injection_rate=decimal_value(raw.get("injection_rate", 0), "injection_rate", minimum=ZERO),
        )


@dataclass(frozen=True, slots=True)
class CapInput:
    effective_from: str
    liquid_cap: Decimal
    water_cap: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapInput":
        return cls(
            effective_from=moment_text(raw.get("effective_from"), "effective_from"),
            liquid_cap=decimal_value(raw.get("liquid_cap"), "liquid_cap", minimum=Decimal("0.001")),
            water_cap=decimal_value(raw.get("water_cap"), "water_cap", minimum=ZERO),
        )


@dataclass(frozen=True, slots=True)
class ConstraintSetInput:
    constraint_set_id: str
    name: str
    test_validity_hours: int
    max_liquid_change_per_phase: Decimal
    increase_water_cut_limit: Decimal
    target_voidage_ratio: Decimal
    phase_hours: int
    phase_count: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConstraintSetInput":
        return cls(
            constraint_set_id=identifier(raw.get("constraint_set_id"), "constraint_set_id"),
            name=required_text(raw.get("name"), "name"),
            test_validity_hours=positive_integer(raw.get("test_validity_hours"), "test_validity_hours"),
            max_liquid_change_per_phase=decimal_value(
                raw.get("max_liquid_change_per_phase"), "max_liquid_change_per_phase", minimum=Decimal("0.001")
            ),
            increase_water_cut_limit=decimal_value(
                raw.get("increase_water_cut_limit"),
                "increase_water_cut_limit",
                minimum=Decimal("0.0001"),
                maximum=MAX_WATER_CUT,
            ),
            target_voidage_ratio=decimal_value(
                raw.get("target_voidage_ratio"), "target_voidage_ratio", minimum=ZERO, maximum=Decimal("3")
            ),
            phase_hours=positive_integer(raw.get("phase_hours"), "phase_hours", maximum=24 * 14),
            phase_count=positive_integer(raw.get("phase_count"), "phase_count", maximum=14),
        )

    def definition(self) -> dict[str, Any]:
        return {
            "test_validity_hours": self.test_validity_hours,
            "max_liquid_change_per_phase": format(self.max_liquid_change_per_phase, "f"),
            "increase_water_cut_limit": format(self.increase_water_cut_limit, "f"),
            "target_voidage_ratio": format(self.target_voidage_ratio, "f"),
            "phase_hours": self.phase_hours,
            "phase_count": self.phase_count,
        }


@dataclass(frozen=True, slots=True)
class EventInput:
    well_id: str
    event_type: str
    effective_at: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EventInput":
        event_type = required_text(raw.get("event_type"), "event_type", 16)
        if event_type not in EVENT_TYPES:
            raise ValidationFailed("event_type 必须是 shut_in 或 restore")
        return cls(
            well_id=identifier(raw.get("well_id"), "well_id"),
            event_type=event_type,
            effective_at=moment_text(raw.get("effective_at"), "effective_at"),
            note=required_text(raw.get("note", "井况事件"), "note"),
        )


@dataclass(frozen=True, slots=True)
class PlanInput:
    plan_id: str
    field_id: str
    horizon_starts_at: str
    constraint_set_id: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanInput":
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            field_id=identifier(raw.get("field_id"), "field_id"),
            horizon_starts_at=moment_text(raw.get("horizon_starts_at"), "horizon_starts_at"),
            constraint_set_id=identifier(raw.get("constraint_set_id"), "constraint_set_id"),
        )


@dataclass(frozen=True, slots=True)
class RecomputeInput:
    source_plan_id: str
    plan_id: str
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RecomputeInput":
        return cls(
            source_plan_id=identifier(raw.get("source_plan_id"), "source_plan_id"),
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            reason=required_text(raw.get("reason"), "reason"),
        )


@dataclass(frozen=True, slots=True)
class OverrideInput:
    plan_id: str
    phase_index: int
    well_id: str
    target_liquid: Decimal | None
    target_injection: Decimal | None
    reason: str
    expires_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OverrideInput":
        phase_index = raw.get("phase_index")
        if isinstance(phase_index, bool) or not isinstance(phase_index, int) or phase_index < 0:
            raise ValidationFailed("phase_index 必须是非负整数")
        target_liquid = raw.get("target_liquid")
        target_injection = raw.get("target_injection")
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            phase_index=phase_index,
            well_id=identifier(raw.get("well_id"), "well_id"),
            target_liquid=None
            if target_liquid is None
            else decimal_value(target_liquid, "target_liquid", minimum=ZERO),
            target_injection=None
            if target_injection is None
            else decimal_value(target_injection, "target_injection", minimum=ZERO),
            reason=required_text(raw.get("reason"), "reason"),
            expires_at=moment_text(raw.get("expires_at"), "expires_at"),
        )
