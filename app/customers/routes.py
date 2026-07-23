from __future__ import annotations

import csv
from io import StringIO

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_babel import gettext
from sqlalchemy.orm import selectinload

from app import db
from app.models import Customer, SalesTicket
from app.money import money_sum
from app.team.services import active_membership, require_permission
from app.timezones import utc_to_local
from .services import (
    CustomerValidationError,
    create_customer,
    customer_summaries,
    update_customer,
    whatsapp_number,
)


customers = Blueprint("customers", __name__, url_prefix="/customers")


def _csv_safe(value):
    text = str(value or "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _current_context():
    from app.routes import current_user

    user = current_user()
    membership = active_membership(user) if user else None
    if not membership:
        abort(401)
    return user, membership


def _customer_or_404(customer_id: int, organization_id: int) -> Customer:
    return Customer.query.filter_by(
        id=customer_id,
        organization_id=organization_id,
    ).first_or_404()


@customers.get("")
@require_permission("manage_customers")
def index():
    _, membership = _current_context()
    query = request.args.get("q", "")
    include_inactive = request.args.get("status") == "all"
    summaries = customer_summaries(
        membership.organization_id,
        query=query,
        include_inactive=include_inactive,
    )
    return render_template(
        "customers.html",
        summaries=summaries,
        query=query,
        include_inactive=include_inactive,
        local_purchase_dates={
            row.customer.id: utc_to_local(
                row.last_purchase_at,
                membership.organization.timezone,
            )
            for row in summaries
            if row.last_purchase_at
        },
        whatsapp_numbers={
            row.customer.id: whatsapp_number(row.customer)
            for row in summaries
        },
    )


@customers.route("/new", methods=["GET", "POST"])
@require_permission("manage_customers")
def new():
    _, membership = _current_context()
    if request.method == "POST":
        try:
            customer = create_customer(
                membership.organization_id,
                membership,
                request.form,
            )
            db.session.commit()
            flash(gettext("Cliente guardado correctamente."), "success")
            return redirect(url_for("customers.detail", customer_id=customer.id))
        except CustomerValidationError as exc:
            db.session.rollback()
            flash(gettext(str(exc)), "danger")
    return render_template("customer_form.html", customer=None)


@customers.route("/<int:customer_id>/edit", methods=["GET", "POST"])
@require_permission("manage_customers")
def edit(customer_id):
    _, membership = _current_context()
    customer = _customer_or_404(customer_id, membership.organization_id)
    if request.method == "POST":
        try:
            update_customer(customer, membership.organization_id, request.form)
            db.session.commit()
            flash(gettext("Cliente actualizado correctamente."), "success")
            return redirect(url_for("customers.detail", customer_id=customer.id))
        except CustomerValidationError as exc:
            db.session.rollback()
            flash(gettext(str(exc)), "danger")
    return render_template("customer_form.html", customer=customer)


@customers.post("/<int:customer_id>/toggle")
@require_permission("manage_customers")
def toggle(customer_id):
    _, membership = _current_context()
    customer = _customer_or_404(customer_id, membership.organization_id)
    customer.is_active = not customer.is_active
    db.session.commit()
    flash(
        gettext("Cliente reactivado.")
        if customer.is_active
        else gettext("Cliente desactivado."),
        "success",
    )
    return redirect(url_for("customers.detail", customer_id=customer.id))


@customers.get("/<int:customer_id>")
@require_permission("manage_customers")
def detail(customer_id):
    _, membership = _current_context()
    customer = _customer_or_404(customer_id, membership.organization_id)
    tickets = (
        SalesTicket.query.options(
            selectinload(SalesTicket.sales),
        )
        .filter_by(
            organization_id=membership.organization_id,
            customer_id=customer.id,
        )
        .order_by(SalesTicket.created_at.desc(), SalesTicket.id.desc())
        .all()
    )
    ticket_rows = [
        {
            "ticket": ticket,
            "total": money_sum(sale.total for sale in ticket.sales),
            "items": sum(sale.quantity for sale in ticket.sales),
            "created_at_local": utc_to_local(
                ticket.created_at,
                membership.organization.timezone,
            ),
        }
        for ticket in tickets
    ]
    from app.credit.services import customer_balance

    credit_balance = customer_balance(
        customer.id, membership.organization_id
    )
    return render_template(
        "customer_detail.html",
        customer=customer,
        ticket_rows=ticket_rows,
        total_purchased=money_sum(row["total"] for row in ticket_rows),
        credit_balance=credit_balance,
        whatsapp_number=whatsapp_number(customer),
        customer_created_local=utc_to_local(
            customer.created_at,
            membership.organization.timezone,
        ),
        payment_method_labels={
            "cash": gettext("Efectivo"),
            "card": gettext("Tarjeta"),
            "transfer": gettext("Transferencia"),
            "other": gettext("Otro"),
            "credit": gettext("Crédito"),
        },
    )


@customers.get("/export.csv")
@require_permission("manage_customers")
def export_csv():
    _, membership = _current_context()
    rows = customer_summaries(
        membership.organization_id,
        query=request.args.get("q", ""),
        include_inactive=request.args.get("status") == "all",
    )
    output = StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(
        [
            gettext("Nombre"),
            gettext("Teléfono"),
            gettext("Email"),
            gettext("Estado"),
            gettext("Fecha de creación"),
            gettext("Total comprado"),
            gettext("Última compra"),
            gettext("Notas"),
        ]
    )
    for row in rows:
        writer.writerow(
            [
                _csv_safe(row.customer.name),
                _csv_safe(row.customer.phone),
                _csv_safe(row.customer.email),
                gettext("Activo")
                if row.customer.is_active
                else gettext("Inactivo"),
                utc_to_local(
                    row.customer.created_at,
                    membership.organization.timezone,
                ).isoformat(),
                f"{row.purchase_total:.2f}",
                (
                    utc_to_local(
                        row.last_purchase_at,
                        membership.organization.timezone,
                    ).isoformat()
                    if row.last_purchase_at
                    else ""
                ),
                _csv_safe(row.customer.notes),
            ]
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=clientes-patia.csv"
        },
    )


@customers.get("/api/search")
@require_permission("lookup_customers")
def api_search():
    _, membership = _current_context()
    query = str(request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify({"ok": True, "customers": []})
    summaries = customer_summaries(
        membership.organization_id,
        query=query,
    )[:8]
    return jsonify(
        {
            "ok": True,
            "customers": [
                {
                    "id": row.customer.id,
                    "name": row.customer.name,
                    "phone": row.customer.phone,
                }
                for row in summaries
            ],
        }
    )


@customers.post("/api/quick")
@require_permission("create_customers")
def api_quick_create():
    _, membership = _current_context()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(
            {"ok": False, "error": gettext("Solicitud inválida")}
        ), 400
    try:
        customer = create_customer(
            membership.organization_id,
            membership,
            data,
        )
        db.session.commit()
        return jsonify(
            {
                "ok": True,
                "customer": {
                    "id": customer.id,
                    "name": customer.name,
                    "phone": customer.phone,
                },
            }
        ), 201
    except CustomerValidationError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": gettext(str(exc))}), 400
