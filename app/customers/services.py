from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from email_validator import EmailNotValidError, validate_email
from flask_babel import gettext
from sqlalchemy import func, or_

from app import db
from app.models import Customer, CustomerCreditMovement, Sale, SalesTicket
from app.money import MONEY_ZERO, money_decimal


class CustomerValidationError(ValueError):
    """A safe validation error whose message may be shown to the user."""


@dataclass(frozen=True)
class CustomerSummary:
    customer: Customer
    purchase_total: Decimal
    ticket_count: int
    last_purchase_at: object | None
    credit_balance: Decimal


def normalize_phone(value: str | None) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _like_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def whatsapp_number(customer: Customer) -> str:
    digits = customer.phone_normalized or ""
    return f"52{digits}" if len(digits) == 10 else digits


def validate_customer_data(data) -> dict:
    name = str(data.get("name") or "").strip()
    phone = str(data.get("phone") or "").strip()
    phone_normalized = normalize_phone(phone)
    email = str(data.get("email") or "").strip().lower()
    notes = str(data.get("notes") or "").strip()

    if not name:
        raise CustomerValidationError(gettext("Escribe el nombre del cliente."))
    if len(name) > 160:
        raise CustomerValidationError(gettext("El nombre es demasiado largo."))
    if phone_normalized and not 7 <= len(phone_normalized) <= 15:
        raise CustomerValidationError(
            gettext("Escribe un teléfono válido de 7 a 15 dígitos.")
        )
    if len(phone) > 30:
        raise CustomerValidationError(
            gettext("El teléfono es demasiado largo.")
        )
    if email:
        try:
            email = validate_email(email, check_deliverability=False).normalized
        except EmailNotValidError as exc:
            raise CustomerValidationError(
                gettext("Escribe un correo electrónico válido.")
            ) from exc
    if len(notes) > 2000:
        raise CustomerValidationError(
            gettext("Las notas no pueden superar 2000 caracteres.")
        )
    return {
        "name": name,
        "phone": phone or None,
        "phone_normalized": phone_normalized or None,
        "email": email or None,
        "notes": notes or None,
    }


def create_customer(organization_id: int, membership, data) -> Customer:
    if membership.organization_id != organization_id:
        raise CustomerValidationError(gettext("Acceso no permitido."))
    values = validate_customer_data(data)
    customer = Customer(
        organization_id=organization_id,
        created_by_member_id=membership.id,
        **values,
    )
    db.session.add(customer)
    return customer


def update_customer(customer: Customer, organization_id: int, data) -> Customer:
    if customer.organization_id != organization_id:
        raise CustomerValidationError(gettext("Acceso no permitido."))
    for field, value in validate_customer_data(data).items():
        setattr(customer, field, value)
    return customer


def customer_purchase_aggregates(organization_id: int):
    return (
        db.session.query(
            SalesTicket.customer_id.label("customer_id"),
            func.coalesce(func.sum(Sale.total), MONEY_ZERO).label(
                "purchase_total"
            ),
            func.count(func.distinct(SalesTicket.id)).label("ticket_count"),
            func.max(SalesTicket.created_at).label("last_purchase_at"),
        )
        .join(Sale, Sale.sales_ticket_id == SalesTicket.id)
        .filter(
            SalesTicket.organization_id == organization_id,
            SalesTicket.customer_id.isnot(None),
        )
        .group_by(SalesTicket.customer_id)
        .subquery()
    )


def customer_summaries(
    organization_id: int,
    *,
    query: str = "",
    include_inactive: bool = False,
) -> list[CustomerSummary]:
    aggregates = customer_purchase_aggregates(organization_id)
    latest_credit_ids = (
        db.session.query(
            CustomerCreditMovement.customer_id,
            func.max(CustomerCreditMovement.id).label("last_id"),
        )
        .filter(CustomerCreditMovement.organization_id == organization_id)
        .group_by(CustomerCreditMovement.customer_id)
        .subquery()
    )
    customer_query = (
        db.session.query(
            Customer,
            func.coalesce(aggregates.c.purchase_total, MONEY_ZERO),
            func.coalesce(aggregates.c.ticket_count, 0),
            aggregates.c.last_purchase_at,
            func.coalesce(CustomerCreditMovement.balance_after, MONEY_ZERO),
        )
        .outerjoin(aggregates, aggregates.c.customer_id == Customer.id)
        .outerjoin(latest_credit_ids, latest_credit_ids.c.customer_id == Customer.id)
        .outerjoin(
            CustomerCreditMovement,
            CustomerCreditMovement.id == latest_credit_ids.c.last_id,
        )
        .filter(Customer.organization_id == organization_id)
    )
    if not include_inactive:
        customer_query = customer_query.filter(Customer.is_active.is_(True))
    cleaned_query = str(query or "").strip()
    if cleaned_query:
        normalized = normalize_phone(cleaned_query)
        filters = [
            func.lower(Customer.name).like(
                f"%{_like_value(cleaned_query.lower())}%",
                escape="\\",
            )
        ]
        if normalized:
            filters.append(
                Customer.phone_normalized.like(
                    f"%{_like_value(normalized)}%",
                    escape="\\",
                )
            )
        customer_query = customer_query.filter(or_(*filters))

    return [
        CustomerSummary(
            customer=row[0],
            purchase_total=money_decimal(row[1]),
            ticket_count=int(row[2] or 0),
            last_purchase_at=row[3],
            credit_balance=money_decimal(row[4]),
        )
        for row in customer_query.order_by(
            Customer.is_active.desc(),
            Customer.name,
            Customer.id,
        ).all()
    ]
