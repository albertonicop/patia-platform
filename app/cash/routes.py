from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app import db
from app.models import CashMovement, CashRegisterSession, OrganizationMember
from app.money import money_decimal
from app.timezones import safe_timezone_name, utc_to_local
from app.team.services import (
    active_membership,
    has_permission,
    require_permission,
)
from .services import expected_cash, open_cash_session, record_cash_movement


cash = Blueprint("cash", __name__, url_prefix="/cash-register")


def _movement_labels():
    return {
        "OPENING": gettext("Fondo inicial"),
        "SALE_CASH": gettext("Venta en efectivo"),
        "CREDIT_PAYMENT": gettext("Abono de crédito en efectivo"),
        "CASH_IN": gettext("Entrada de efectivo"),
        "WITHDRAWAL": gettext("Retiro"),
        "EXPENSE": gettext("Gasto"),
        "REFUND": gettext("Devolución"),
    }


def _current_context():
    from app.routes import current_user

    user = current_user()
    membership = active_membership(user) if user else None
    if not user or not membership:
        abort(401)
    return user, membership


@cash.get("")
@require_permission("operate_cash_register")
def index():
    user, membership = _current_context()
    current = (
        CashRegisterSession.query.options(
            selectinload(CashRegisterSession.movements),
            selectinload(CashRegisterSession.opened_by_member)
            .selectinload(OrganizationMember.user),
        )
        .filter_by(
            organization_id=membership.organization_id,
            open_key="MAIN",
            status="OPEN",
        )
        .first()
    )
    can_view_history = has_permission(membership, "view_cash_history")
    history = []
    if can_view_history:
        history = (
            CashRegisterSession.query.filter_by(
                organization_id=membership.organization_id,
                status="CLOSED",
            )
            .order_by(
                CashRegisterSession.closed_at.desc(),
                CashRegisterSession.id.desc(),
            )
            .limit(30)
            .all()
        )
    timezone_name = safe_timezone_name(membership.organization.timezone)
    if current:
        current.opened_at_local = utc_to_local(
            current.opened_at, timezone_name
        )
    for item in history:
        item.closed_at_local = utc_to_local(item.closed_at, timezone_name)
    return render_template(
        "cash_register.html",
        user=user,
        cash_session=current,
        expected_cash=expected_cash(current.id) if current else None,
        history=history,
        can_manage_movements=has_permission(
            membership, "manage_cash_movements"
        ),
        can_view_history=can_view_history,
    )


@cash.post("/open")
@require_permission("operate_cash_register")
def open_register():
    _, membership = _current_context()
    try:
        opening_cash = money_decimal(request.form.get("opening_cash") or 0)
    except ValueError:
        flash(gettext("Ingresa un fondo inicial válido."), "danger")
        return redirect(url_for("cash.index"))
    if open_cash_session(membership.organization_id):
        flash(gettext("La caja principal ya tiene un turno abierto."), "danger")
        return redirect(url_for("cash.index"))

    cash_session = CashRegisterSession(
        organization_id=membership.organization_id,
        register_key="MAIN",
        open_key="MAIN",
        status="OPEN",
        opened_by_member_id=membership.id,
        opening_cash=opening_cash,
    )
    db.session.add(cash_session)
    db.session.flush()
    if opening_cash > 0:
        record_cash_movement(
            cash_session,
            membership,
            "OPENING",
            opening_cash,
            note=gettext("Fondo inicial"),
        )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(gettext("La caja principal ya tiene un turno abierto."), "danger")
        return redirect(url_for("cash.index"))
    flash(gettext("Caja abierta correctamente."), "success")
    return redirect(url_for("cash.index"))


@cash.post("/movement")
@require_permission("manage_cash_movements")
def add_movement():
    _, membership = _current_context()
    cash_session = open_cash_session(
        membership.organization_id, lock=True
    )
    if not cash_session:
        flash(gettext("Abre la caja antes de registrar movimientos."), "danger")
        return redirect(url_for("cash.index"))
    movement_type = request.form.get("movement_type")
    if movement_type not in {"CASH_IN", "WITHDRAWAL", "EXPENSE"}:
        abort(400)
    try:
        amount = money_decimal(request.form.get("amount"))
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash(gettext("Ingresa un importe mayor a cero."), "danger")
        return redirect(url_for("cash.index"))
    note = request.form.get("note", "")
    if not note.strip():
        flash(gettext("Describe brevemente el movimiento."), "danger")
        return redirect(url_for("cash.index"))
    record_cash_movement(
        cash_session, membership, movement_type, amount, note=note
    )
    db.session.commit()
    flash(gettext("Movimiento de caja registrado."), "success")
    return redirect(url_for("cash.index"))


@cash.post("/close")
@require_permission("operate_cash_register")
def close_register():
    _, membership = _current_context()
    cash_session = open_cash_session(
        membership.organization_id, lock=True
    )
    if not cash_session:
        flash(gettext("No hay una caja abierta para cerrar."), "danger")
        return redirect(url_for("cash.index"))
    try:
        counted = money_decimal(request.form.get("counted_cash"))
    except ValueError:
        flash(gettext("Ingresa el efectivo contado."), "danger")
        return redirect(url_for("cash.index"))
    expected = expected_cash(cash_session.id)
    cash_session.expected_cash_at_close = expected
    cash_session.counted_cash = counted
    cash_session.difference = counted - expected
    cash_session.closing_notes = (
        request.form.get("closing_notes", "").strip()[:1000] or None
    )
    cash_session.closed_by_member_id = membership.id
    cash_session.closed_at = datetime.utcnow()
    cash_session.status = "CLOSED"
    cash_session.open_key = None
    db.session.commit()
    flash(gettext("Corte de caja guardado correctamente."), "success")
    return redirect(url_for("cash.detail", session_id=cash_session.id))


@cash.get("/<int:session_id>")
@require_permission("view_cash_history")
def detail(session_id):
    user, membership = _current_context()
    cash_session = (
        CashRegisterSession.query.options(
            selectinload(CashRegisterSession.movements)
            .selectinload(CashMovement.performed_by_member),
            selectinload(CashRegisterSession.opened_by_member)
            .selectinload(OrganizationMember.user),
            selectinload(CashRegisterSession.closed_by_member)
            .selectinload(OrganizationMember.user),
        )
        .filter_by(
            id=session_id,
            organization_id=membership.organization_id,
        )
        .first_or_404()
    )
    timezone_name = safe_timezone_name(membership.organization.timezone)
    cash_session.opened_at_local = utc_to_local(
        cash_session.opened_at, timezone_name
    )
    cash_session.closed_at_local = (
        utc_to_local(cash_session.closed_at, timezone_name)
        if cash_session.closed_at else None
    )
    for movement in cash_session.movements:
        movement.created_at_local = utc_to_local(
            movement.created_at, timezone_name
        )
    return render_template(
        "cash_register_detail.html",
        user=user,
        cash_session=cash_session,
        expected_cash=(
            cash_session.expected_cash_at_close
            if cash_session.status == "CLOSED"
            else expected_cash(cash_session.id)
        ),
        movement_labels=_movement_labels(),
    )
