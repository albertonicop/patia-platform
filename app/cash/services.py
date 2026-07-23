from __future__ import annotations

from decimal import Decimal

from sqlalchemy import case, func

from app import db
from app.models import CashMovement, CashRegisterSession
from app.money import MONEY_ZERO, money_decimal


POSITIVE_MOVEMENTS = ("OPENING", "SALE_CASH", "CREDIT_PAYMENT", "CASH_IN")
NEGATIVE_MOVEMENTS = ("WITHDRAWAL", "EXPENSE", "REFUND")


def open_cash_session(organization_id, register_key="MAIN", *, lock=False):
    query = CashRegisterSession.query.filter_by(
        organization_id=organization_id,
        open_key=register_key,
        status="OPEN",
    )
    if lock:
        query = query.with_for_update()
    return query.first()


def expected_cash(session_id) -> Decimal:
    signed_amount = case(
        (
            CashMovement.movement_type.in_(POSITIVE_MOVEMENTS),
            CashMovement.amount,
        ),
        else_=-CashMovement.amount,
    )
    value = (
        db.session.query(func.coalesce(func.sum(signed_amount), 0))
        .filter(CashMovement.cash_register_session_id == session_id)
        .scalar()
    )
    return money_decimal(value or MONEY_ZERO, nonnegative=False)


def record_cash_movement(
    cash_session,
    membership,
    movement_type,
    amount,
    *,
    note=None,
    sales_ticket=None,
):
    amount = money_decimal(amount)
    movement = CashMovement(
        organization_id=cash_session.organization_id,
        cash_register_session_id=cash_session.id,
        performed_by_member_id=membership.id if membership else None,
        sales_ticket_id=sales_ticket.id if sales_ticket else None,
        movement_type=movement_type,
        amount=amount,
        note=(note or "").strip()[:255] or None,
    )
    db.session.add(movement)
    return movement
