"""Exact inventory units and conversions for PATIA."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


QUANTITY_STEP = Decimal("0.001")


@dataclass(frozen=True)
class UnitDefinition:
    code: str
    family: str
    factor_to_base: Decimal
    label_es: str
    label_en: str


UNITS = {
    "kg": UnitDefinition("kg", "mass", Decimal("1"), "kg", "kg"),
    "g": UnitDefinition("g", "mass", Decimal("0.001"), "g", "g"),
    "L": UnitDefinition("L", "volume", Decimal("1"), "L", "L"),
    "ml": UnitDefinition("ml", "volume", Decimal("0.001"), "ml", "ml"),
    "piece": UnitDefinition("piece", "count", Decimal("1"), "pieza", "piece"),
    "dozen": UnitDefinition("dozen", "count", Decimal("12"), "docena", "dozen"),
    "portion": UnitDefinition("portion", "portion", Decimal("1"), "porción", "portion"),
}

BASE_UNIT_BY_FAMILY = {
    "mass": "kg",
    "volume": "L",
    "count": "piece",
    "portion": "portion",
}


def quantity_decimal(
    value, *, positive: bool = False, allow_negative: bool = False
) -> Decimal:
    try:
        result = Decimal(str(value).strip()).quantize(
            QUANTITY_STEP, rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid_quantity") from exc
    if (result < 0 and not allow_negative) or (positive and result <= 0):
        raise ValueError("invalid_quantity")
    return result


def normalize_unit(value: str | None, default: str = "piece") -> str:
    aliases = {
        "pieza": "piece", "piezas": "piece", "unit": "piece", "units": "piece",
        "docena": "dozen", "docenas": "dozen",
        "porcion": "portion", "porción": "portion", "porciones": "portion",
        "l": "L", "litro": "L", "litros": "L",
        "mililitro": "ml", "mililitros": "ml",
        "kilogramo": "kg", "kilogramos": "kg",
        "gramo": "g", "gramos": "g",
    }
    raw = str(value or "").strip()
    code = aliases.get(raw.casefold(), raw)
    return code if code in UNITS else default


def compatible_units(unit_code: str) -> tuple[str, ...]:
    unit = UNITS[normalize_unit(unit_code)]
    return tuple(code for code, candidate in UNITS.items() if candidate.family == unit.family)


def convert_quantity(value, from_unit: str, to_unit: str) -> Decimal:
    source = UNITS[normalize_unit(from_unit)]
    target = UNITS[normalize_unit(to_unit)]
    if source.family != target.family:
        raise ValueError("incompatible_units")
    amount = quantity_decimal(value)
    return (amount * source.factor_to_base / target.factor_to_base).quantize(
        QUANTITY_STEP, rounding=ROUND_HALF_UP
    )


def format_quantity(value) -> str:
    amount = quantity_decimal(value, allow_negative=True)
    rendered = format(amount, "f").rstrip("0").rstrip(".")
    return rendered or "0"
