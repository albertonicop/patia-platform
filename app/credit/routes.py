from urllib.parse import quote
import uuid

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app import db
from app.models import Customer, CustomerCreditMovement, OrganizationMember
from app.money import money_decimal
from app.team.services import active_membership, require_permission
from app.timezones import utc_to_local
from .services import CreditError, customer_balance, record_credit_payment


credit = Blueprint("credit", __name__, url_prefix="/credit")


def _context():
    from app.routes import current_user

    user = current_user()
    membership = active_membership(user) if user else None
    if not membership:
        abort(401)
    return user, membership


def _customer(customer_id, organization_id, *, lock=False):
    query = Customer.query.filter_by(
        id=customer_id,
        organization_id=organization_id,
    )
    if lock:
        query = query.with_for_update()
    return query.first_or_404()


@credit.get("")
@require_permission("manage_customer_credit")
def index():
    _, membership = _context()
    last_ids = (
        db.session.query(
            CustomerCreditMovement.customer_id,
            func.max(CustomerCreditMovement.id).label("last_id"),
        )
        .filter(
            CustomerCreditMovement.organization_id
            == membership.organization_id
        )
        .group_by(CustomerCreditMovement.customer_id)
        .subquery()
    )
    last_credit_purchase = (
        db.session.query(
            CustomerCreditMovement.customer_id,
            func.max(CustomerCreditMovement.created_at).label(
                "last_credit_purchase_at"
            ),
        )
        .filter(
            CustomerCreditMovement.organization_id
            == membership.organization_id,
            CustomerCreditMovement.movement_type == "CHARGE",
        )
        .group_by(CustomerCreditMovement.customer_id)
        .subquery()
    )
    rows = (
        db.session.query(
            Customer,
            CustomerCreditMovement,
            last_credit_purchase.c.last_credit_purchase_at,
        )
        .join(last_ids, last_ids.c.customer_id == Customer.id)
        .join(
            CustomerCreditMovement,
            CustomerCreditMovement.id == last_ids.c.last_id,
        )
        .outerjoin(
            last_credit_purchase,
            last_credit_purchase.c.customer_id == Customer.id,
        )
        .filter(
            Customer.organization_id == membership.organization_id,
            Customer.credit_enabled.is_(True),
            CustomerCreditMovement.balance_after > 0,
        )
        .order_by(Customer.name)
        .all()
    )
    return render_template(
        "credit_accounts.html",
        accounts=[
            {
                "customer": customer,
                "balance": money_decimal(
                    movement.balance_after if movement else 0
                ),
                "last_credit_purchase": (
                    utc_to_local(
                        last_purchase_at,
                        membership.organization.timezone,
                    )
                    if last_purchase_at else None
                ),
            }
            for customer, movement, last_purchase_at in rows
        ],
        total_receivable=sum(
            (
                money_decimal(movement.balance_after if movement else 0)
                for _, movement, _ in rows
            ),
            money_decimal(0),
        ),
    )


@credit.get("/customers/<int:customer_id>")
@require_permission("receive_credit_payments")
def account(customer_id):
    _, membership = _context()
    customer = _customer(customer_id, membership.organization_id)
    movements = (
        CustomerCreditMovement.query.options(
            selectinload(CustomerCreditMovement.sales_ticket),
            selectinload(
                CustomerCreditMovement.performed_by_member
            ).selectinload(OrganizationMember.user),
            selectinload(
                CustomerCreditMovement.authorized_by_member
            ).selectinload(OrganizationMember.user),
        )
        .filter_by(
            organization_id=membership.organization_id,
            customer_id=customer.id,
        )
        .order_by(
            CustomerCreditMovement.created_at.desc(),
            CustomerCreditMovement.id.desc(),
        )
        .all()
    )
    for movement in movements:
        movement.local_created_at = utc_to_local(
            movement.created_at,
            membership.organization.timezone,
        )
    balance = customer_balance(customer.id, membership.organization_id)
    reminder = gettext(
        "Hola %(name)s, te recordamos que tienes un saldo pendiente de %(balance)s en %(business)s.",
        name=customer.name,
        balance=f"${balance:,.2f} {membership.organization.currency}",
        business=membership.organization.name,
    )
    from app.customers.services import whatsapp_number

    return render_template(
        "credit_account.html",
        customer=customer,
        movements=movements,
        balance=balance,
        reminder_url=(
            f"https://wa.me/{whatsapp_number(customer)}?text={quote(reminder)}"
        ),
        payment_request_id=str(uuid.uuid4()),
    )


@credit.post("/customers/<int:customer_id>/settings")
@require_permission("manage_customer_credit")
def settings(customer_id):
    _, membership = _context()
    customer = _customer(
        customer_id,
        membership.organization_id,
        lock=True,
    )
    enabled = request.form.get("credit_enabled") == "1"
    try:
        limit = money_decimal(request.form.get("credit_limit") or 0)
    except (TypeError, ValueError):
        flash(gettext("Escribe un límite de crédito válido."), "danger")
        return redirect(url_for("customers.detail", customer_id=customer.id))
    if not enabled and customer_balance(
        customer.id, membership.organization_id
    ) > 0:
        flash(
            gettext("Liquida el saldo antes de desactivar el crédito."),
            "danger",
        )
        return redirect(url_for("credit.account", customer_id=customer.id))
    customer.credit_enabled = enabled
    customer.credit_limit = limit
    db.session.commit()
    flash(gettext("Configuración de crédito actualizada."), "success")
    return redirect(url_for("credit.account", customer_id=customer.id))


@credit.post("/customers/<int:customer_id>/payments")
@require_permission("receive_credit_payments")
def payment(customer_id):
    _, membership = _context()
    customer = _customer(customer_id, membership.organization_id)
    try:
        movement, created = record_credit_payment(
            customer,
            membership,
            request.form.get("amount"),
            request.form.get("payment_method"),
            note=request.form.get("note"),
            request_id=request.form.get("request_id") or str(uuid.uuid4()),
        )
        db.session.commit()
        if created and movement.balance_after == 0:
            message = gettext(
                "Pago registrado. %(name)s ya no tiene saldo pendiente.",
                name=customer.name,
            )
        elif created:
            message = gettext(
                "Pago registrado. %(name)s ahora debe %(balance)s.",
                name=customer.name,
                balance=f"${movement.balance_after:,.2f}",
            )
        else:
            message = gettext("Este pago ya había sido registrado.")
        flash(message, "success")
    except IntegrityError:
        db.session.rollback()
        request_id = (request.form.get("request_id") or "").strip()
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, AttributeError):
            request_id = ""
        existing = CustomerCreditMovement.query.filter_by(
            organization_id=membership.organization_id,
            request_id=request_id,
            customer_id=customer.id,
            movement_type="PAYMENT",
        ).first()
        if existing:
            flash(gettext("Este pago ya había sido registrado."), "success")
        else:
            flash(
                gettext(
                    "No se pudo registrar el abono. Actualiza la página e inténtalo de nuevo."
                ),
                "danger",
            )
    except (CreditError, TypeError, ValueError) as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("credit.account", customer_id=customer.id))
