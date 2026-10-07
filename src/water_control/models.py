"""稳油控水协同调度的领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
WELL_KINDS = {"producer", "injector"}
WELL_STATES = {"producing", "injecting", "shut", "observation"}
EVENT_TYPES = {"shutdown", "restore", "observation"}
OVERRIDE_ACTIONS = {"increase", "limit", "observe", "maintain", "shut"}


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


def percent_value(value: object, field: str) -> Decimal:
    return decimal_value(value, field, minimum=Decimal("0"), maximum=Decimal("100"))


def positive_integer(value: object, field: str, *, maximum: int = 366) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValidationFailed(f"{field} 必须是 1 到 {maximum} 的整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def utc_text_value(value: object, field: str) -> str:
    result = required_text(value, field, 40)
    try:
        parse_utc(result, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return result


@dataclass(frozen=True, slots=True)
class WellGroup:
    group_id: str
    field_id: str
    name: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WellGroup":
        return cls(
            group_id=identifier(raw.get("group_id"), "group_id"),
            field_id=identifier(raw.get("field_id"), "field_id"),
            name=required_text(raw.get("name"), "name"),
        )


@dataclass(frozen=True, slots=True)
class LayerSeries:
    layer_id: str
    group_id: str
    name: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LayerSeries":
        return cls(
            layer_id=identifier(raw.get("layer_id"), "layer_id"),
            group_id=identifier(raw.get("group_id"), "group_id"),
            name=required_text(raw.get("name"), "name"),
        )


@dataclass(frozen=True, slots=True)
class Well:
    well_id: str
    group_id: str
    layer_id: str
    name: str
    kind: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Well":
        kind = required_text(raw.get("kind"), "kind", 16)
        if kind not in WELL_KINDS:
            raise ValidationFailed("kind 必须是 producer 或 injector")
        return cls(
            well_id=identifier(raw.get("well_id"), "well_id"),
            group_id=identifier(raw.get("group_id"), "group_id"),
            layer_id=identifier(raw.get("layer_id"), "layer_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
        )


@dataclass(frozen=True, slots=True)
class ConnectivityLink:
    link_id: str
    injector_id: str
    producer_id: str
    coefficient: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConnectivityLink":
        injector = identifier(raw.get("injector_id"), "injector_id")
        producer = identifier(raw.get("producer_id"), "producer_id")
        if injector == producer:
            raise ValidationFailed("连通关系的注水井和生产井不能相同")
        return cls(
            link_id=identifier(raw.get("link_id"), "link_id"),
            injector_id=injector,
            producer_id=producer,
            coefficient=percent_value(raw.get("coefficient_percent"), "coefficient_percent"),
        )


@dataclass(frozen=True, slots=True)
class WellTest:
    test_id: str
    well_id: str
    tested_at: str
    liquid_rate: Decimal
    water_cut_percent: Decimal
    source_revision: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WellTest":
        return cls(
            test_id=identifier(raw.get("test_id"), "test_id"),
            well_id=identifier(raw.get("well_id"), "well_id"),
            tested_at=utc_text_value(raw.get("tested_at"), "tested_at"),
            liquid_rate=decimal_value(raw.get("liquid_rate"), "liquid_rate", minimum=Decimal("0")),
            water_cut_percent=percent_value(raw.get("water_cut_percent"), "water_cut_percent"),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
        )


@dataclass(frozen=True, slots=True)
class RateConstraint:
    constraint_id: str
    well_id: str
    min_rate: Decimal
    max_rate: Decimal
    effective_from: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RateConstraint":
        minimum = decimal_value(raw.get("min_rate"), "min_rate", minimum=Decimal("0"))
        maximum = decimal_value(raw.get("max_rate"), "max_rate", minimum=Decimal("0"))
        if maximum < minimum:
            raise ValidationFailed("max_rate 不能小于 min_rate")
        return cls(
            constraint_id=identifier(raw.get("constraint_id"), "constraint_id"),
            well_id=identifier(raw.get("well_id"), "well_id"),
            min_rate=minimum,
            max_rate=maximum,
            effective_from=date_text(raw.get("effective_from"), "effective_from"),
        )


@dataclass(frozen=True, slots=True)
class CapacityProfile:
    profile_id: str
    field_id: str
    liquid_limit: Decimal
    water_limit: Decimal
    effective_from: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapacityProfile":
        return cls(
            profile_id=identifier(raw.get("profile_id"), "profile_id"),
            field_id=identifier(raw.get("field_id"), "field_id"),
            liquid_limit=decimal_value(raw.get("liquid_limit"), "liquid_limit", minimum=Decimal("0.001")),
            water_limit=decimal_value(raw.get("water_limit"), "water_limit", minimum=Decimal("0")),
            effective_from=date_text(raw.get("effective_from"), "effective_from"),
        )


@dataclass(frozen=True, slots=True)
class PlanRequest:
    plan_id: str
    field_id: str
    horizon_start: str
    stage_count: int
    observe_threshold_percent: Decimal
    limit_threshold_percent: Decimal
    increase_threshold_percent: Decimal
    increase_cap_percent: Decimal
    limit_cut_percent: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanRequest":
        observe = percent_value(raw.get("observe_threshold_percent"), "observe_threshold_percent")
        limit = percent_value(raw.get("limit_threshold_percent"), "limit_threshold_percent")
        increase = percent_value(raw.get("increase_threshold_percent"), "increase_threshold_percent")
        if not increase < observe <= limit:
            raise ValidationFailed("阈值必须满足 increase < observe <= limit")
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            field_id=identifier(raw.get("field_id"), "field_id"),
            horizon_start=date_text(raw.get("horizon_start"), "horizon_start"),
            stage_count=positive_integer(raw.get("stage_count"), "stage_count", maximum=90),
            observe_threshold_percent=observe,
            limit_threshold_percent=limit,
            increase_threshold_percent=increase,
            increase_cap_percent=decimal_value(
                raw.get("increase_cap_percent"), "increase_cap_percent",
                minimum=Decimal("0"), maximum=Decimal("100"),
            ),
            limit_cut_percent=decimal_value(
                raw.get("limit_cut_percent"), "limit_cut_percent",
                minimum=Decimal("0"), maximum=Decimal("100"),
            ),
        )


@dataclass(frozen=True, slots=True)
class OverrideRequest:
    override_id: str
    plan_id: str
    stage_index: int
    well_id: str
    action: str
    target_liquid_rate: Decimal | None
    target_injection_rate: Decimal | None
    reason: str
    expires_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OverrideRequest":
        action = required_text(raw.get("action"), "action", 16)
        if action not in OVERRIDE_ACTIONS:
            raise ValidationFailed("action 不是受支持的覆盖动作")
        stage_index = raw.get("stage_index")
        if isinstance(stage_index, bool) or not isinstance(stage_index, int) or stage_index < 0:
            raise ValidationFailed("stage_index 必须是非负整数")
        liquid = raw.get("target_liquid_rate")
        injection = raw.get("target_injection_rate")
        return cls(
            override_id=identifier(raw.get("override_id"), "override_id"),
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            stage_index=stage_index,
            well_id=identifier(raw.get("well_id"), "well_id"),
            action=action,
            target_liquid_rate=None if liquid is None else decimal_value(liquid, "target_liquid_rate", minimum=Decimal("0")),
            target_injection_rate=None if injection is None else decimal_value(injection, "target_injection_rate", minimum=Decimal("0")),
            reason=required_text(raw.get("reason"), "reason", 512),
            expires_at=utc_text_value(raw.get("expires_at"), "expires_at"),
        )
