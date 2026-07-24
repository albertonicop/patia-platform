from __future__ import annotations

import uuid

from flask_babel import gettext

from app import db
from app.cash.services import open_cash_session, record_cash_movement
from app.models import Customer, CustomerCreditMovement, OrganizationMember
from app.money import MONEY_ZERO, money_decimal


class CreditError(ValueError):
    pass


class CreditNotEnabled(CreditError):
    pass


class CreditLimitExceeded(CreditError):
    def __init__(self, balance, limit):
        self.balance = balance
        self.limit = limit
        super().__init__(gettext("La venta supera el límite de crédito."))


def customer_balance(customer_id: int, organization_id: int):
    last = (
        CustomerCreditMovement.query.filter_by(
            customer_id=customer_id,
            organization_id=organization_id,
        )
        .order_by(
            CustomerCreditMovement.created_at.desc(),
            CustomerCreditMovement.id.desc(),
        )
        .first()
    )
    return money_decimal(last.balance_after if last else MONEY_ZERO)


def authorize_override(membership, organization_id, pin=None):
    from app.team.services import has_permission

    if has_permission(membership, "authorize_credit_override"):
        return membership
    pin = str(pin or "").strip()
    if not pin:
        return None
    approvers = OrganizationMember.query.filter(
        OrganizationMember.organization_id == organization_id,
        OrganizationMember.is_active.is_(True),
        OrganizationMember.role.in_(("OWNER", "MANAGER")),
        OrganizationMember.pin_hash.isnot(None),
    ).all()
    return next((member for member in approvers if member.check_pin(pin)), None)


def record_credit_charge(
    customer,
    membership,
    amount,
    sales_ticket,
    *,
    allow_override=False,
    override_pin=None,
):
    locked = Customer.query.filter_by(
        id=customer.id,
        organization_id=membership.organization_id,
        is_active=True,
    ).with_for_update().first()
    if not locked or not locked.credit_enabled:
        raise CreditNotEnabled(
            gettext("Este cliente todavía no tiene crédito habilitado.")
        )
    before = customer_balance(locked.id, membership.organization_id)
    amount = money_decimal(amount)
    after = money_decimal(before + amount)
    authorized_by = None
    if after > locked.credit_limit:
        if not allow_override:
            raise CreditLimitExceeded(after, locked.credit_limit)
        authorized_by = authorize_override(
            membership,
            membership.organization_id,
            override_pin,
        )
        if not authorized_by:
            raise CreditError(
                gettext("Se requiere autorización para exceder el límite.")
            )
    movement = CustomerCreditMovement(
        organization_id=membership.organization_id,
        customer_id=locked.id,
        performed_by_member_id=membership.id,
        authorized_by_member_id=authorized_by.id if authorized_by else None,
        sales_ticket_id=sales_ticket.id,
        movement_type="CHARGE",
        amount=amount,
        balance_before=before,
        balance_after=after,
        note=sales_ticket.folio,
    )
    db.session.add(movement)
    return movement


def record_credit_payment(
    customer,
    membership,
    amount,
    payment_method,
    *,
    note=None,
    request_id=None,
):
    locked = Customer.query.filter_by(
        id=customer.id,
        organization_id=membership.organization_id,
    ).with_for_update().first()
    if not locked:
        raise CreditError(gettext("Cliente no encontrado."))
    amount = money_decimal(amount)
    if amount <= MONEY_ZERO:
        raise CreditError(gettext("El abono debe ser mayor a cero."))
    if payment_method not in {"cash", "card", "transfer", "other"}:
        raise CreditError(gettext("Selecciona un método de pago válido."))
    request_id = (request_id or "").strip() or None
    if request_id:
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, AttributeError):
            raise CreditError(
                gettext(
                    "No se pudo verificar este abono. Actualiza la página e inténtalo de nuevo."
                )
            )
        existing = CustomerCreditMovement.query.filter_by(
            organization_id=membership.organization_id,
            request_id=request_id,
        ).first()
        if existing:
            same_payment = (
                existing.movement_type == "PAYMENT"
                and existing.customer_id == locked.id
                and money_decimal(existing.amount) == amount
                and existing.payment_method == payment_method
            )
            if not same_payment:
                raise CreditError(
                    gettext(
                        "No se pudo verificar este abono. Actualiza la página e inténtalo de nuevo."
                    )
                )
            return existing, False
    before = customer_balance(locked.id, membership.organization_id)
    if amount > before:
        raise CreditError(
            gettext("El abono no puede ser mayor al saldo pendiente.")
        )
    cash_session = None
    if payment_method == "cash":
        cash_session = open_cash_session(
            membership.organization_id,
            lock=True,
        )
        if not cash_session:
            raise CreditError(
                gettext("Abre la caja antes de recibir un abono en efectivo.")
            )
    after = money_decimal(before - amount)
    movement = CustomerCreditMovement(
        organization_id=membership.organization_id,
        customer_id=locked.id,
        performed_by_member_id=membership.id,
        cash_register_session_id=cash_session.id if cash_session else None,
        movement_type="PAYMENT",
        amount=amount,
        balance_before=before,
        balance_after=after,
        payment_method=payment_method,
        request_id=request_id,
        note=(note or "").strip()[:255] or None,
    )
    db.session.add(movement)
    db.session.flush()
    if cash_session:
        record_cash_movement(
            cash_session,
            membership,
            "CREDIT_PAYMENT",
            amount,
            note=gettext("Abono de crédito: %(customer)s", customer=locked.name),
        )
    return movement, True


def record_credit_reversal(customer, membership, amount, sales_ticket):
    """Reduce receivables when a credit-sale line is canceled or returned."""
    locked = Customer.query.filter_by(
        id=customer.id,
        organization_id=membership.organization_id,
    ).with_for_update().first()
    if not locked:
        raise CreditError(gettext("Cliente no encontrado."))
    before = customer_balance(locked.id, membership.organization_id)
    amount = money_decimal(amount)
    if amount > before:
        raise CreditError(
            gettext(
                "No se puede cancelar esta venta porque parte de su saldo ya fue pagado."
            )
        )
    movement = CustomerCreditMovement(
        organization_id=membership.organization_id,
        customer_id=locked.id,
        performed_by_member_id=membership.id,
        sales_ticket_id=sales_ticket.id,
        movement_type="REVERSAL",
        amount=amount,
        balance_before=before,
        balance_after=money_decimal(before - amount),
        note=gettext("Cancelación de %(folio)s", folio=sales_ticket.folio),
    )
    db.session.add(movement)
    return movement
