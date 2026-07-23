from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


MONEY_SCALE = Decimal("0.01")
MONEY_ZERO = Decimal("0.00")
MONEY_MAX = Decimal("999999999999.99")


def money_decimal(value, *, allow_none=False, nonnegative=True) -> Decimal | None:
    """Return a finite, cent-rounded monetary value from an external value."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_none:
            return None
        value = MONEY_ZERO
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("invalid monetary value") from error
    if not amount.is_finite():
        raise ValueError("monetary value must be finite")
    amount = amount.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
    if nonnegative and amount < MONEY_ZERO:
        raise ValueError("monetary value must be nonnegative")
    if abs(amount) > MONEY_MAX:
        raise ValueError("monetary value exceeds NUMERIC(14,2)")
    return amount


def money_json(value) -> str:
    """Serialize money exactly at JSON/template boundaries."""
    return format(money_decimal(value, nonnegative=False), ".2f")


def money_sum(values) -> Decimal:
    return sum(
        (money_decimal(value, nonnegative=False) for value in values),
        MONEY_ZERO,
    )
