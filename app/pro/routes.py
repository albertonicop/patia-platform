from __future__ import annotations

from datetime import date, timedelta
from io import BytesIO
import uuid

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_babel import format_date, get_locale, gettext
from app import db
from app.ai_narratives import controlled_narrative
from app.models import MonthlyOwnerReport, PurchaseOrder
from app.money import money_decimal
from app.monthly_reports import (
    MonthlyReportUnavailable,
    build_report_snapshot,
    generate_monthly_report,
    monthly_report_pdf,
    report_payload,
    report_snapshot,
)
from app.plans import has_entitlement
from app.routes import current_organization_owner, current_user
from app.team.services import (
    active_membership,
    has_permission,
    require_permission,
)
from app.timezones import local_today, safe_timezone_name, utc_to_local

from .purchases import (
    cancel_purchase_order,
    confirm_purchase_order,
    create_purchase_draft,
    purchase_order_query,
    purchase_suggestions,
    receive_purchase_order,
    update_purchase_draft,
)
from .services import build_smart_alerts


pro = Blueprint("pro", __name__, url_prefix="/pro")


def _pro_preview(module):
    previews = {
        "hub": {
            "icon": "fa-gem",
            "eyebrow": gettext("PATIA Pro"),
            "title": gettext("Pulso PATIA convierte tus datos en decisiones"),
            "description": gettext(
                "Te muestra qué pasó, por qué importa y qué hacer ahora, "
                "con evidencia de tu propio negocio."
            ),
            "benefits": (
                gettext("Hasta tres decisiones prioritarias"),
                gettext("Evidencia verificable detrás de cada decisión"),
                gettext("Una acción directa para resolver cada situación"),
            ),
        },
        "monthly": {
            "icon": "fa-file-lines",
            "eyebrow": gettext("Reporte mensual"),
            "title": gettext("Recibe el resumen que le darías a un gerente"),
            "description": gettext(
                "Conserva una fotografía de cada mes, descárgala en PDF "
                "y compártela sin preparar reportes manuales."
            ),
            "benefits": (
                gettext("Historial mensual inmutable"),
                gettext("PDF listo para compartir"),
                gettext("Envío y reenvío desde PATIA"),
            ),
        },
        "purchases": {
            "icon": "fa-truck-ramp-box",
            "eyebrow": gettext("Compras inteligentes"),
            "title": gettext("Compra lo necesario antes de quedarte sin stock"),
            "description": gettext(
                "PATIA usa existencias y ventas recientes para sugerir "
                "qué pedir, cuánto y a qué proveedor."
            ),
            "benefits": (
                gettext("Reposición agrupada por proveedor"),
                gettext("Pedidos y recepciones conectados al inventario"),
                gettext("Evidencia detrás de cada sugerencia"),
            ),
        },
        "alerts": {
            "icon": "fa-bell",
            "eyebrow": gettext("Alertas"),
            "title": gettext("Detecta lo importante antes de que sea urgente"),
            "description": gettext(
                "Revisa cambios en ventas, margen, stock, crédito y caja "
                "con evidencia y una acción concreta."
            ),
            "benefits": (
                gettext("Prioridad según impacto"),
                gettext("Evidencia fácil de comprobar"),
                gettext("Acceso directo para resolver cada situación"),
            ),
        },
    }
    return previews[module]


def _pro_access(preview=None):
    user = current_user()
    membership = active_membership(user) if user else None
    owner = current_organization_owner(user)
    if not owner or not has_entitlement(owner, "executive_dashboard"):
        if preview and user and membership and request.method == "GET":
            return user, membership, render_template(
                "pro_preview.html",
                user=user,
                organization=membership.organization,
                preview=_pro_preview(preview),
            )
        flash(
            gettext(
                "El Centro de decisiones está incluido en PATIA Pro. "
                "Compara los planes para activarlo."
            ),
            "info",
        )
        return None, None, redirect(url_for("main.subscribe"))
    return user, membership, None


@pro.route("", methods=["GET"], strict_slashes=False)
@require_permission("view_reports")
def dashboard():
    values = {
        key: request.args.get(key)
        for key in ("period", "start", "end")
        if request.args.get(key)
    }
    values["_anchor"] = "resumen"
    return redirect(url_for("main.reports", **values), code=302)


@pro.route("/monthly-goal", methods=["POST"])
@require_permission("manage_subscription")
def monthly_goal():
    user, membership, blocked = _pro_access()
    if blocked:
        return blocked
    raw_goal = (request.form.get("monthly_sales_goal") or "").strip()
    try:
        goal = money_decimal(raw_goal, allow_none=True)
        if goal is not None and goal <= 0:
            raise ValueError("goal_must_be_positive")
    except ValueError:
        flash(
            gettext("Escribe una meta mensual mayor a cero."),
            "error",
        )
        return redirect(_monthly_goal_return_url())
    membership.organization.monthly_sales_goal = goal
    db.session.commit()
    if goal is None:
        flash(gettext("Meta mensual eliminada."), "success")
    else:
        flash(gettext("Meta mensual guardada."), "success")
    return redirect(_monthly_goal_return_url())


def _monthly_goal_return_url():
    period = (request.form.get("return_period") or "").strip()
    if period not in {"7d", "30d", "this_month", "custom"}:
        return url_for("main.reports", _anchor="monthly-goal")
    values = {"period": period, "_anchor": "monthly-goal"}
    if period == "custom":
        values["start"] = (request.form.get("return_start") or "").strip()
        values["end"] = (request.form.get("return_end") or "").strip()
    return url_for("main.reports", **values)


def _completed_months(organization, count=12):
    today = local_today(safe_timezone_name(organization.timezone))
    cursor = today.replace(day=1) - timedelta(days=1)
    values = []
    for _ in range(count):
        values.append(
            {
                "year": cursor.year,
                "month": cursor.month,
                "value": f"{cursor.year:04d}-{cursor.month:02d}",
            }
        )
        cursor = cursor.replace(day=1) - timedelta(days=1)
    return values


def _selected_completed_period(organization):
    periods = _completed_months(organization)
    selected = (request.form.get("period") or "").strip()
    allowed = {item["value"]: item for item in periods}
    if selected not in allowed:
        raise ValueError("invalid_period")
    return allowed[selected]


@pro.route("/hub")
@require_permission("view_reports")
def hub():
    user, membership, blocked = _pro_access("hub")
    if blocked:
        return blocked
    alerts = build_smart_alerts(
        membership.organization,
        {"period": "this_month"},
    )
    purchases = purchase_suggestions(membership.organization_id)
    latest_report = (
        MonthlyOwnerReport.query.filter_by(
            organization_id=membership.organization_id
        )
        .order_by(
            MonthlyOwnerReport.report_year.desc(),
            MonthlyOwnerReport.report_month.desc(),
        )
        .first()
    )
    priorities = [
        {
            "title": item["title"],
            "evidence": item["evidence"],
            "impact": item["action"],
            "label": item["action_label"],
            "url": item["url"],
            "icon": item["icon"],
            "tone": item["tone"],
        }
        for item in alerts["smart_alerts"][:3]
    ]
    if (
        len(priorities) < 3
        and purchases["summary"]["products"]
        and not any(item["key"] == "stock" for item in alerts["smart_alerts"])
    ):
        priorities.append(
            {
                "title": gettext(
                    "%(count)s productos necesitan reposición",
                    count=purchases["summary"]["products"],
                ),
                "evidence": gettext(
                    "PATIA sugiere pedir %(units)s unidades con base en existencias y ventas recientes.",
                    units=purchases["summary"]["units"],
                ),
                "impact": gettext(
                    "Revisa las cantidades sugeridas y prepara el pedido antes de perder una venta."
                ),
                "label": gettext("Preparar compra"),
                "url": url_for("pro.purchases"),
                "icon": "fa-truck-ramp-box",
                "tone": "warning",
            }
        )
    if len(priorities) < 3 and not latest_report:
        priorities.append(
            {
                "title": gettext("Tu primer reporte mensual está listo para generarse"),
                "evidence": gettext(
                    "Guarda una fotografía del mes y descárgala en PDF."
                ),
                "impact": gettext(
                    "Crear el resumen ahora te dará una referencia fija para comparar el siguiente mes."
                ),
                "label": gettext("Generar reporte"),
                "url": url_for("pro.monthly_reports"),
                "icon": "fa-file-lines",
                "tone": "neutral",
            }
        )
    language = str(get_locale() or "es").split("_")[0]
    if priorities:
        primary = priorities[0]
        pulse_fallback = {
            "summary": gettext(
                "PATIA ordenó la operación y encontró una acción principal."
            ),
            "what_happened": primary["title"],
            "why_it_matters": primary["evidence"],
            "recommended_actions": [primary["impact"]],
            "limitations": [],
            "data_period": local_today(
                safe_timezone_name(membership.organization.timezone)
            ).strftime("%Y-%m"),
        }
    else:
        pulse_fallback = {
            "summary": gettext(
                "La operación se mantiene estable con los datos disponibles."
            ),
            "what_happened": gettext(
                "No se detectó una situación urgente en ventas, margen, inventario, crédito o caja."
            ),
            "why_it_matters": gettext(
                "Puedes concentrarte en vender y mantener actualizados costos y existencias."
            ),
            "recommended_actions": [
                gettext("Continúa registrando la operación diaria.")
            ],
            "limitations": [],
            "data_period": local_today(
                safe_timezone_name(membership.organization.timezone)
            ).strftime("%Y-%m"),
        }
    pulse_metrics = {
        "priority_count": len(priorities),
        "priorities": [
            {
                "title": item["title"],
                "evidence": item["evidence"],
                "action": item["impact"],
            }
            for item in priorities[:3]
        ],
        "inventory": {
            "low_stock": alerts["executive_control"]["inventory"][
                "low_stock"
            ],
        },
        "credit": {
            "customers": alerts["executive_control"]["credit"]["customers"],
            "balance": str(
                alerts["executive_control"]["credit"]["balance"]
            ),
        },
        "cash": {
            "status": alerts["executive_control"]["cash"]["status"],
        },
    }
    pulse_narrative, pulse_source = controlled_narrative(
        organization_id=membership.organization_id,
        feature="pulse",
        language=language,
        period=pulse_fallback["data_period"],
        metrics=pulse_metrics,
        fallback=pulse_fallback,
    )
    if db.session.new or db.session.dirty:
        db.session.commit()
    return render_template(
        "pro_hub.html",
        user=user,
        organization=membership.organization,
        business_type=(current_organization_owner(user).business_type or "").strip(),
        latest_report=latest_report,
        priorities=priorities[:3],
        executive_control=alerts["executive_control"],
        team_activity=alerts["team_activity"],
        pulse_narrative=pulse_narrative,
        pulse_source=pulse_source,
    )


@pro.route("/alerts")
@require_permission("view_reports")
def alerts():
    user, membership, blocked = _pro_access("alerts")
    if blocked:
        return blocked
    data = build_smart_alerts(membership.organization, request.args)
    if data["alert_period"]["error"]:
        flash(data["alert_period"]["error"], "warning")
    return render_template(
        "pro_alerts.html",
        user=user,
        organization=membership.organization,
        **data,
    )


@pro.route("/monthly-reports")
@require_permission("view_reports")
def monthly_reports():
    user, membership, blocked = _pro_access("monthly")
    if blocked:
        return blocked
    reports = (
        MonthlyOwnerReport.query.filter_by(
            organization_id=membership.organization_id
        )
        .order_by(
            MonthlyOwnerReport.report_year.desc(),
            MonthlyOwnerReport.report_month.desc(),
        )
        .all()
    )
    timezone_name = safe_timezone_name(membership.organization.timezone)
    for report in reports:
        report.generated_at_local = utc_to_local(
            report.generated_at, timezone_name
        )
        report.period_label = format_date(
            date(report.report_year, report.report_month, 1),
            format="LLLL yyyy",
        ).capitalize()
    return render_template(
        "pro_monthly_reports.html",
        user=user,
        organization=membership.organization,
        reports=reports,
        periods=_completed_months(membership.organization),
        can_resend=has_permission(
            membership, "manage_subscription"
        ),
    )


@pro.route("/monthly-reports/preview")
@require_permission("view_reports")
def monthly_report_preview():
    user = current_user()
    membership = active_membership(user)
    periods = _completed_months(membership.organization)
    selected = periods[0]
    payload = report_payload(
        membership.organization,
        selected["year"],
        selected["month"],
    )
    subject = gettext(
        "Tu resumen mensual de %(business)s — %(period)s",
        business=membership.organization.name,
        period=payload["period_label"],
    )
    snapshot = build_report_snapshot(payload, subject)
    return render_template(
        "pro_monthly_report.html",
        user=user,
        organization=membership.organization,
        snapshot=snapshot,
        record=None,
        preview=True,
        can_resend=False,
    )


@pro.route("/monthly-reports/generate", methods=["POST"])
@require_permission("view_reports")
def monthly_report_generate():
    user, membership, blocked = _pro_access()
    if blocked:
        return blocked
    try:
        selected = _selected_completed_period(membership.organization)
        record, _ = generate_monthly_report(
            membership.organization_id,
            selected["year"],
            selected["month"],
            send=False,
            generated_by_member_id=membership.id,
            manual_generation=True,
        )
    except (ValueError, MonthlyReportUnavailable):
        flash(
            gettext("No pudimos generar ese reporte mensual."),
            "error",
        )
        return redirect(url_for("pro.monthly_reports"))
    flash(gettext("Reporte mensual generado."), "success")
    return redirect(
        url_for("pro.monthly_report_detail", report_id=record.id)
    )


def _monthly_report_for_membership(report_id, membership):
    return MonthlyOwnerReport.query.filter_by(
        id=report_id,
        organization_id=membership.organization_id,
    ).first_or_404()


@pro.route("/monthly-reports/<int:report_id>")
@require_permission("view_reports")
def monthly_report_detail(report_id):
    user, membership, blocked = _pro_access()
    if blocked:
        return blocked
    record = _monthly_report_for_membership(report_id, membership)
    snapshot = report_snapshot(record)
    if not snapshot:
        try:
            record, _ = generate_monthly_report(
                membership.organization_id,
                record.report_year,
                record.report_month,
                send=False,
                generated_by_member_id=membership.id,
                manual_generation=True,
            )
            snapshot = report_snapshot(record)
        except MonthlyReportUnavailable:
            snapshot = None
    return render_template(
        "pro_monthly_report.html",
        user=user,
        organization=membership.organization,
        record=record,
        snapshot=snapshot,
        preview=False,
        can_resend=has_permission(
            membership, "manage_subscription"
        ),
    )


@pro.route("/monthly-reports/<int:report_id>/pdf")
@require_permission("view_reports")
def monthly_report_download(report_id):
    user, membership, blocked = _pro_access()
    if blocked:
        return blocked
    record = _monthly_report_for_membership(report_id, membership)
    try:
        document = monthly_report_pdf(record)
    except MonthlyReportUnavailable:
        flash(gettext("Este reporte todavía no tiene snapshot."), "error")
        return redirect(
            url_for("pro.monthly_report_detail", report_id=record.id)
        )
    return send_file(
        BytesIO(document),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=(
            f"PATIA-{record.report_year:04d}-"
            f"{record.report_month:02d}.pdf"
        ),
    )


@pro.route("/monthly-reports/<int:report_id>/resend", methods=["POST"])
@require_permission("manage_subscription")
def monthly_report_resend(report_id):
    user, membership, blocked = _pro_access()
    if blocked:
        return blocked
    record = _monthly_report_for_membership(report_id, membership)
    try:
        record, _ = generate_monthly_report(
            membership.organization_id,
            record.report_year,
            record.report_month,
            send=True,
            force_retry=True,
            resend=True,
        )
    except MonthlyReportUnavailable:
        flash(
            gettext(
                "Activa el envío mensual y confirma el correo destinatario."
            ),
            "error",
        )
        return redirect(
            url_for("pro.monthly_report_detail", report_id=report_id)
        )
    if record.status == "sent":
        flash(gettext("Reporte reenviado correctamente."), "success")
    else:
        flash(
            gettext(
                "No pudimos reenviar el reporte. Podrás intentarlo de nuevo."
            ),
            "error",
        )
    return redirect(
        url_for("pro.monthly_report_detail", report_id=report_id)
    )


@pro.route("/purchases")
@require_permission("manage_inventory")
def purchases():
    user, membership, blocked = _pro_access("purchases")
    if blocked:
        return blocked
    suggestions = purchase_suggestions(membership.organization_id)
    orders = (
        purchase_order_query(membership.organization_id)
        .order_by(PurchaseOrder.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template(
        "pro_purchases.html",
        user=user,
        organization=membership.organization,
        orders=orders,
        **suggestions,
    )


@pro.route("/purchases/drafts", methods=["POST"])
@require_permission("manage_inventory")
def purchase_draft_create():
    user, membership, blocked = _pro_access()
    if blocked:
        return blocked
    quantities = {}
    for key, value in request.form.items():
        if not key.startswith("quantity_"):
            continue
        try:
            product_id = int(key.removeprefix("quantity_"))
            quantity = int(value)
        except (TypeError, ValueError):
            continue
        if quantity > 0:
            quantities[product_id] = quantity
    try:
        order = create_purchase_draft(
            membership,
            quantities,
            supplier_name=request.form.get("supplier_name"),
            notes=request.form.get("notes"),
        )
    except ValueError:
        flash(
            gettext("Revisa los productos y cantidades del pedido."),
            "error",
        )
        return redirect(url_for("pro.purchases"))
    flash(gettext("Borrador de pedido creado."), "success")
    return redirect(
        url_for("pro.purchase_detail", order_id=order.id)
    )


def _purchase_for_membership(order_id, membership):
    return purchase_order_query(
        membership.organization_id
    ).filter(PurchaseOrder.id == order_id).first_or_404()


@pro.route("/purchases/<int:order_id>")
@require_permission("manage_inventory")
def purchase_detail(order_id):
    user, membership, blocked = _pro_access()
    if blocked:
        return blocked
    order = _purchase_for_membership(order_id, membership)
    timezone_name = safe_timezone_name(membership.organization.timezone)
    for receipt in order.receipts:
        receipt.created_at_local = utc_to_local(
            receipt.created_at, timezone_name
        )
    return render_template(
        "pro_purchase_detail.html",
        user=user,
        organization=membership.organization,
        order=order,
        receipt_request_id=uuid.uuid4().hex,
    )


@pro.route("/purchases/<int:order_id>/update", methods=["POST"])
@require_permission("manage_inventory")
def purchase_update(order_id):
    user, membership, blocked = _pro_access()
    if blocked:
        return blocked
    order = _purchase_for_membership(order_id, membership)
    quantities = {}
    for item in order.items:
        try:
            quantities[item.id] = int(
                request.form.get(f"quantity_{item.id}", "")
            )
        except (TypeError, ValueError):
            quantities[item.id] = 0
    try:
        update_purchase_draft(
            order,
            quantities,
            notes=request.form.get("notes"),
        )
    except ValueError:
        flash(gettext("Revisa las cantidades del pedido."), "error")
    else:
        flash(gettext("Borrador actualizado."), "success")
    return redirect(
        url_for("pro.purchase_detail", order_id=order.id)
    )


@pro.route("/purchases/<int:order_id>/confirm", methods=["POST"])
@require_permission("manage_inventory")
def purchase_confirm(order_id):
    user, membership, blocked = _pro_access()
    if blocked:
        return blocked
    order = _purchase_for_membership(order_id, membership)
    try:
        confirm_purchase_order(order)
    except ValueError:
        flash(gettext("Este pedido no puede confirmarse."), "error")
    else:
        flash(gettext("Pedido confirmado y listo para recibir."), "success")
    return redirect(
        url_for("pro.purchase_detail", order_id=order.id)
    )


@pro.route("/purchases/<int:order_id>/receive", methods=["POST"])
@require_permission("manage_inventory")
def purchase_receive(order_id):
    user, membership, blocked = _pro_access()
    if blocked:
        return blocked
    order = _purchase_for_membership(order_id, membership)
    quantities = {}
    for item in order.items:
        try:
            quantities[item.id] = int(
                request.form.get(f"received_{item.id}", "0")
            )
        except (TypeError, ValueError):
            quantities[item.id] = -1
    try:
        receipt, created = receive_purchase_order(
            order,
            membership,
            quantities,
            request_id=request.form.get("request_id"),
        )
    except ValueError:
        flash(
            gettext("Revisa las cantidades recibidas."),
            "error",
        )
    else:
        flash(
            (
                gettext("Mercancía recibida e inventario actualizado.")
                if created
                else gettext("Esta recepción ya había sido registrada.")
            ),
            "success",
        )
    return redirect(
        url_for("pro.purchase_detail", order_id=order.id)
    )


@pro.route("/purchases/<int:order_id>/cancel", methods=["POST"])
@require_permission("manage_inventory")
def purchase_cancel(order_id):
    user, membership, blocked = _pro_access()
    if blocked:
        return blocked
    order = _purchase_for_membership(order_id, membership)
    try:
        cancel_purchase_order(order)
    except ValueError:
        flash(gettext("Este pedido ya no puede cancelarse."), "error")
    else:
        flash(
            gettext(
                "Pedido cancelado. La mercancía recibida anteriormente no cambió."
            ),
            "success",
        )
    return redirect(url_for("pro.purchase_detail", order_id=order.id))
