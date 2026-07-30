"""Idempotent monthly owner report generation for eligible organizations."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
from types import SimpleNamespace

from flask import current_app, render_template
from flask_babel import (
    force_locale,
    format_currency,
    format_date,
    get_locale,
    gettext,
)
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app import db
from app.ai_narratives import controlled_narrative
from app.models import (
    CashRegisterSession,
    Customer,
    CustomerCreditMovement,
    MonthlyOwnerReport,
    Organization,
    Product,
    Sale,
    SalesTicket,
)
from app.money import MONEY_ZERO, money_decimal
from app.plans import has_entitlement, subscription_access_is_active
from app.timezones import local_date_bounds_utc, safe_timezone_name


class MonthlyReportUnavailable(ValueError):
    pass


RETRY_DELAYS = (
    timedelta(minutes=15),
    timedelta(hours=2),
    timedelta(hours=12),
    timedelta(days=1),
)

SNAPSHOT_VERSION = 2


def _decimal_text(value):
    return format(money_decimal(value, nonnegative=False), ".2f")


def _plain_number(value):
    return float(value) if isinstance(value, Decimal) else value


def build_report_snapshot(payload, subject):
    """Freeze every user-visible business value used by a monthly report."""
    analytics = payload["analytics"]
    kpis = analytics["report_kpis"]
    snapshot = {
        "version": SNAPSHOT_VERSION,
        "language": str(get_locale() or "es").split("_")[0],
        "subject": subject,
        "business_name": payload["organization"].name,
        "period": {
            "year": payload["period"]["start_date"].year,
            "month": payload["period"]["start_date"].month,
            "start": payload["period"]["start_date"].isoformat(),
            "end": payload["period"]["end_date"].isoformat(),
            "label": payload["period_label"],
        },
        "headline": payload["headline"],
        "wins": list(payload["wins"]),
        "attention": list(payload["attention"]),
        "recommendations": list(payload["recommendations"]),
        "comparison": _plain_number(payload["comparison"]),
        "profit_comparison": _plain_number(
            payload["profit_comparison"]
        ),
        "kpis": {
            "sales": _decimal_text(kpis["sales"]),
            "profit": _decimal_text(kpis["profit"]),
            "margin": _plain_number(kpis["margin"]),
            "average_ticket": _decimal_text(kpis["average_ticket"]),
            "ticket_count": int(kpis["ticket_count"]),
            "profit_coverage": _plain_number(
                kpis["profit_coverage"]
            ),
            "unknown_cost_lines": int(analytics["unknown_cost_lines"]),
        },
        "daily": [
            {
                "date": item["date"],
                "sales": _decimal_text(item["sales"]),
                "profit": _decimal_text(item["profit"]),
            }
            for item in analytics["daily_report"]
        ],
        "payments": [
            {
                "key": item["key"],
                "label": item["label"],
                "amount": _decimal_text(item["amount"]),
                "tickets": int(item["tickets"]),
                "percentage": _plain_number(item["percentage"]),
            }
            for item in analytics["payments_report"]
        ],
        "top_selling": [
            {
                "name": item.name,
                "units": int(item.units or 0),
                "revenue": _decimal_text(item.revenue or 0),
            }
            for item in analytics["top_selling_report"][:10]
        ],
        "profitable_products": [
            {
                "name": item["name"],
                "units": int(item["units"]),
                "revenue": _decimal_text(item["revenue"]),
                "cost": (
                    _decimal_text(item["cost"])
                    if item["cost"] is not None
                    else None
                ),
                "profit": (
                    _decimal_text(item["profit"])
                    if item["profit"] is not None
                    else None
                ),
                "margin": _plain_number(item["margin"]),
            }
            for item in analytics["profitable_products_report"][:10]
        ],
        "inventory": {
            "value": _decimal_text(payload["inventory"]["value"]),
            "low_stock": list(payload["inventory"]["low_stock"]),
        },
        "credit": {
            "total": _decimal_text(payload["credit"]["total"]),
            "customers": [
                {
                    "name": item["name"],
                    "balance": _decimal_text(item["balance"]),
                }
                for item in payload["credit"]["customers"]
            ],
        },
        "customers": list(payload.get("customers") or []),
        "cash": {
            "count": int(payload["cash"]["count"]),
            "net_difference": _decimal_text(
                payload["cash"]["net_difference"]
            ),
        },
    }
    return snapshot


def enrich_snapshot_narrative(snapshot, organization_id):
    """Attach one immutable, verified executive narrative to a snapshot."""
    fallback = {
        "summary": snapshot["headline"],
        "what_happened": (
            snapshot["wins"][0]
            if snapshot["wins"]
            else snapshot["headline"]
        ),
        "why_it_matters": (
            snapshot["attention"][0]
            if snapshot["attention"]
            else gettext(
                "El registro constante permite comparar el negocio con "
                "periodos futuros."
            )
        ),
        "recommended_actions": list(snapshot["recommendations"][:3]),
        "limitations": (
            [
                gettext(
                    "La utilidad es parcial porque existen ventas sin costo conocido."
                )
            ]
            if snapshot["kpis"]["unknown_cost_lines"]
            else []
        ),
        "data_period": (
            f"{snapshot['period']['start']}..{snapshot['period']['end']}"
        ),
    }
    metrics = {
        "sales": snapshot["kpis"]["sales"],
        "profit": snapshot["kpis"]["profit"],
        "margin": snapshot["kpis"]["margin"],
        "ticket_count": snapshot["kpis"]["ticket_count"],
        "average_ticket": snapshot["kpis"]["average_ticket"],
        "sales_change": snapshot["comparison"],
        "profit_change": snapshot["profit_comparison"],
        "unknown_cost_lines": snapshot["kpis"]["unknown_cost_lines"],
        "low_stock_count": len(snapshot["inventory"]["low_stock"]),
        "credit_total": snapshot["credit"]["total"],
        "credit_customer_count": len(snapshot["credit"]["customers"]),
        "cash_closures": snapshot["cash"]["count"],
        "cash_net_difference": snapshot["cash"]["net_difference"],
    }
    narrative, source = controlled_narrative(
        organization_id=organization_id,
        feature="monthly_report",
        language=snapshot["language"],
        period=fallback["data_period"],
        metrics=metrics,
        fallback=fallback,
        ttl_hours=24 * 40,
    )
    snapshot["narrative"] = narrative
    snapshot["narrative_source"] = source
    return snapshot


def serialize_snapshot(snapshot):
    return json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def report_snapshot(record):
    if not record or not record.snapshot_json:
        return None
    return json.loads(record.snapshot_json)


def payload_from_snapshot(record):
    snapshot = report_snapshot(record)
    if not snapshot:
        return None
    kpis = snapshot["kpis"]
    return {
        "subject": snapshot["subject"],
        "snapshot": snapshot,
        "organization": SimpleNamespace(
            name=snapshot["business_name"]
        ),
        "period_label": snapshot["period"]["label"],
        "headline": (snapshot.get("narrative") or {}).get(
            "summary", snapshot["headline"]
        ),
        "wins": snapshot["wins"],
        "attention": snapshot["attention"],
        "recommendations": (snapshot.get("narrative") or {}).get(
            "recommended_actions", snapshot["recommendations"]
        ),
        "analytics": {
            "report_kpis": {
                "sales": Decimal(kpis["sales"]),
                "profit": Decimal(kpis["profit"]),
                "margin": kpis["margin"],
                "average_ticket": Decimal(kpis["average_ticket"]),
                "ticket_count": kpis["ticket_count"],
                "profit_coverage": kpis["profit_coverage"],
            },
            "unknown_cost_lines": kpis["unknown_cost_lines"],
            "daily_report": snapshot["daily"],
            "payments_report": snapshot["payments"],
            "top_selling_report": [
                SimpleNamespace(
                    name=item["name"],
                    units=item["units"],
                    revenue=Decimal(item["revenue"]),
                )
                for item in snapshot["top_selling"]
            ],
            "profitable_products_report": snapshot[
                "profitable_products"
            ],
        },
        "inventory": {
            "value": Decimal(snapshot["inventory"]["value"]),
            "low_stock": snapshot["inventory"]["low_stock"],
        },
        "credit": {
            "total": Decimal(snapshot["credit"]["total"]),
            "customers": snapshot["credit"]["customers"],
        },
        "customers": snapshot.get("customers", []),
        "cash": {
            "count": snapshot["cash"]["count"],
            "net_difference": Decimal(
                snapshot["cash"]["net_difference"]
            ),
        },
        "comparison": snapshot["comparison"],
        "profit_comparison": snapshot["profit_comparison"],
    }


def monthly_report_pdf(record):
    """Render a professional executive document from the immutable snapshot."""
    snapshot = report_snapshot(record)
    if not snapshot:
        raise MonthlyReportUnavailable("Report snapshot is unavailable.")
    with force_locale(snapshot.get("language") or "es"):
        labels = {
            "report": gettext("Reporte mensual ejecutivo"),
            "summary": gettext("Resumen ejecutivo"),
            "sales": gettext("Ventas"),
            "profit": gettext("Utilidad estimada"),
            "recorded_sales": gettext("Ventas registradas"),
            "average_ticket": gettext("Ticket promedio"),
            "wins": gettext("Lo que salió bien"),
            "attention": gettext("Lo que conviene revisar"),
            "top_products": gettext("Productos destacados"),
            "customers": gettext("Clientes importantes"),
            "opportunities": gettext("Oportunidades y alertas"),
            "actions": gettext("Acciones para el siguiente mes"),
            "inventory": gettext("Valor del inventario"),
            "credit": gettext("Pendiente por cobrar"),
            "cash": gettext("Cierres con diferencia"),
            "no_data": gettext("Sin datos para este periodo."),
            "generated": gettext(
                "Documento generado por PATIA a partir de la operación registrada."
            ),
            "snapshot": gettext(
                "Snapshot %(hash)s",
                hash=(record.snapshot_hash or "")[:12],
            ),
            "monthly_results": gettext("Resultados del mes"),
            "monthly_results_help": gettext(
                "Ventas, rentabilidad y productos que explican el periodo."
            ),
            "product": gettext("Producto"),
            "units": gettext("Unidades"),
            "result": gettext("Resultado"),
            "customer": gettext("Cliente"),
            "purchases": gettext("Compras"),
            "total": gettext("Total"),
            "no_customer_sales": gettext(
                "No hubo ventas vinculadas a clientes durante este mes."
            ),
            "control_next": gettext("Control y siguiente paso"),
            "control_next_help": gettext(
                "Inventario, saldos y caja para preparar el próximo mes."
            ),
            "no_recommendations": gettext(
                "Registra actividad durante el mes para recibir acciones específicas."
            ),
        }
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=letter,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
        title=f"{labels['report']} · {snapshot['business_name']}",
        author="PATIA",
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("PatiaTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=24, leading=28, textColor=HexColor("#171A2B"), spaceAfter=6)
    h1 = ParagraphStyle("PatiaH1", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=HexColor("#171A2B"), spaceBefore=8, spaceAfter=9)
    h2 = ParagraphStyle("PatiaH2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=HexColor("#5B45E8"), spaceBefore=7, spaceAfter=6)
    body = ParagraphStyle("PatiaBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=9, leading=13, textColor=HexColor("#424A5D"), spaceAfter=5)
    small = ParagraphStyle("PatiaSmall", parent=body, fontSize=7.5, leading=10, textColor=HexColor("#7A8192"))
    number = ParagraphStyle("PatiaNumber", parent=body, alignment=TA_RIGHT, fontName="Helvetica-Bold", textColor=HexColor("#171A2B"))

    def money_value(value):
        return f"${Decimal(value):,.2f} MXN"

    def section(heading, items):
        values = list(items or [])
        if not values:
            values = [labels["no_data"]]
        return KeepTogether([
            Paragraph(heading, h1),
            *[Paragraph(f"• {value}", body) for value in values],
        ])

    def on_page(pdf, doc):
        pdf.saveState()
        pdf.setFillColor(HexColor("#5B45E8"))
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(18 * mm, letter[1] - 12 * mm, "PATIA PRO")
        pdf.setFillColor(HexColor("#7A8192"))
        pdf.setFont("Helvetica", 7)
        pdf.drawRightString(letter[0] - 18 * mm, letter[1] - 12 * mm, snapshot["period"]["label"])
        pdf.setStrokeColor(HexColor("#E3E6EF"))
        pdf.line(18 * mm, 13 * mm, letter[0] - 18 * mm, 13 * mm)
        pdf.drawString(18 * mm, 8 * mm, labels["generated"])
        pdf.drawRightString(letter[0] - 18 * mm, 8 * mm, str(doc.page))
        pdf.restoreState()

    kpis = snapshot["kpis"]
    kpi_data = [
        [Paragraph(labels["sales"], small), Paragraph(labels["profit"], small), Paragraph(labels["average_ticket"], small), Paragraph(labels["recorded_sales"], small)],
        [Paragraph(money_value(kpis["sales"]), number), Paragraph(money_value(kpis["profit"]), number), Paragraph(money_value(kpis["average_ticket"]), number), Paragraph(str(kpis["ticket_count"]), number)],
    ]
    kpi_table = Table(kpi_data, colWidths=[document.width / 4] * 4, rowHeights=[8 * mm, 12 * mm])
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#F5F4FC")),
        ("BOX", (0, 0), (-1, -1), .5, HexColor("#DED9F8")),
        ("INNERGRID", (0, 0), (-1, -1), .5, HexColor("#E7E4F6")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))

    story = [
        Spacer(1, 5 * mm),
        Paragraph(labels["report"].upper(), h2),
        Paragraph(snapshot["business_name"], title),
        Paragraph(snapshot["period"]["label"], body),
        Spacer(1, 7 * mm),
        Paragraph(labels["summary"], h1),
        Paragraph(
            (snapshot.get("narrative") or {}).get(
                "summary", snapshot["headline"]
            ),
            ParagraphStyle(
                "Headline",
                parent=h1,
                fontSize=13,
                leading=18,
                textColor=HexColor("#34304F"),
            ),
        ),
        Paragraph(
            (snapshot.get("narrative") or {}).get(
                "why_it_matters", snapshot["headline"]
            ),
            body,
        ),
        Spacer(1, 4 * mm),
        kpi_table,
        Spacer(1, 7 * mm),
        section(labels["wins"], snapshot["wins"]),
        Spacer(1, 4 * mm),
        section(labels["attention"], snapshot["attention"]),
        PageBreak(),
        Paragraph(labels["monthly_results"], title),
        Paragraph(labels["monthly_results_help"], body),
        Spacer(1, 4 * mm),
        Paragraph(labels["top_products"], h1),
    ]
    products = snapshot.get("profitable_products") or snapshot.get("top_selling", [])
    product_rows = [[Paragraph(labels["product"], small), Paragraph(labels["units"], small), Paragraph(labels["result"], small)]]
    for item in products[:7]:
        amount = item.get("profit") or item.get("revenue") or "0.00"
        product_rows.append([Paragraph(item["name"], body), Paragraph(str(item.get("units", 0)), number), Paragraph(money_value(amount), number)])
    if len(product_rows) == 1:
        product_rows.append([Paragraph(labels["no_data"], body), "", ""])
    product_table = Table(product_rows, colWidths=[document.width * .55, document.width * .17, document.width * .28], repeatRows=1)
    product_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#25213F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#FFFFFF")),
        ("LINEBELOW", (0, 1), (-1, -1), .4, HexColor("#E3E6EF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([
        product_table,
        Spacer(1, 7 * mm),
        Paragraph(labels["customers"], h1),
    ])
    customer_rows = [[Paragraph(labels["customer"], small), Paragraph(labels["purchases"], small), Paragraph(labels["total"], small)]]
    for item in snapshot.get("customers", [])[:6]:
        customer_rows.append([Paragraph(item["name"], body), Paragraph(str(item["tickets"]), number), Paragraph(money_value(item["sales"]), number)])
    if len(customer_rows) == 1:
        customer_rows.append([Paragraph(labels["no_customer_sales"], body), "", ""])
    customer_table = Table(customer_rows, colWidths=[document.width * .55, document.width * .17, document.width * .28], repeatRows=1)
    customer_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#F0EEFC")),
        ("LINEBELOW", (0, 0), (-1, -1), .4, HexColor("#E3E6EF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([
        customer_table,
        PageBreak(),
        Paragraph(labels["control_next"], title),
        Paragraph(labels["control_next_help"], body),
        Spacer(1, 5 * mm),
    ])
    control_data = [
        [Paragraph(labels["inventory"], small), Paragraph(labels["credit"], small), Paragraph(labels["cash"], small)],
        [Paragraph(money_value(snapshot["inventory"]["value"]), number), Paragraph(money_value(snapshot["credit"]["total"]), number), Paragraph(str(snapshot["cash"]["count"]), number)],
    ]
    control_table = Table(control_data, colWidths=[document.width / 3] * 3, rowHeights=[8 * mm, 12 * mm])
    control_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#F4FAF8")),
        ("BOX", (0, 0), (-1, -1), .5, HexColor("#CDE9E1")),
        ("INNERGRID", (0, 0), (-1, -1), .5, HexColor("#DDEFEA")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.extend([
        control_table,
        Spacer(1, 7 * mm),
        section(labels["opportunities"], snapshot.get("attention", [])),
        Spacer(1, 5 * mm),
        Paragraph(labels["actions"], h1),
    ])
    recommendations = (
        (snapshot.get("narrative") or {}).get("recommended_actions")
        or snapshot.get("recommendations")
        or [labels["no_recommendations"]]
    )
    for index, item in enumerate(recommendations[:5], 1):
        action_table = Table(
            [[Paragraph(f"{index:02d}", ParagraphStyle("Index", parent=number, fontSize=12, textColor=HexColor("#5B45E8"))), Paragraph(item, body)]],
            colWidths=[14 * mm, document.width - 14 * mm],
        )
        action_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        story.append(action_table)
    story.extend([Spacer(1, 8 * mm), Paragraph(labels["snapshot"], small)])
    document.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return output.getvalue()


def _delivery_is_claimed(record, now=None):
    if not record or record.status != "sending":
        return False
    now = now or datetime.utcnow()
    return bool(
        record.generated_at
        and record.generated_at >= now - timedelta(hours=1)
    )


def _next_retry_at(attempt_count, now=None):
    now = now or datetime.utcnow()
    index = min(max(int(attempt_count or 1) - 1, 0), len(RETRY_DELAYS) - 1)
    return now + RETRY_DELAYS[index]


def _period(year: int, month: int, timezone_name: str):
    if year < 2000 or month not in range(1, 13):
        raise ValueError("Invalid monthly report period.")
    start_date = date(year, month, 1)
    end_date = date(year, month, monthrange(year, month)[1])
    start_at, end_before = local_date_bounds_utc(
        start_date,
        end_date + timedelta(days=1),
        timezone_name,
    )
    return {
        "period": "custom",
        "start_date": start_date,
        "end_date": end_date,
        "start_at": start_at,
        "end_before": end_before,
        "custom_start": start_date.isoformat(),
        "custom_end": end_date.isoformat(),
        "error": None,
    }


def _credit_snapshot(organization_id: int):
    latest_ids = (
        db.session.query(
            CustomerCreditMovement.customer_id,
            func.max(CustomerCreditMovement.id).label("movement_id"),
        )
        .filter(
            CustomerCreditMovement.organization_id == organization_id
        )
        .group_by(CustomerCreditMovement.customer_id)
        .subquery()
    )
    rows = (
        db.session.query(
            Customer.name,
            CustomerCreditMovement.balance_after,
        )
        .join(latest_ids, latest_ids.c.customer_id == Customer.id)
        .join(
            CustomerCreditMovement,
            CustomerCreditMovement.id == latest_ids.c.movement_id,
        )
        .filter(
            Customer.organization_id == organization_id,
            CustomerCreditMovement.balance_after > 0,
        )
        .order_by(
            CustomerCreditMovement.balance_after.desc(), Customer.name
        )
        .all()
    )
    return {
        "total": money_decimal(
            sum((row.balance_after for row in rows), MONEY_ZERO)
        ),
        "customers": [
            {
                "name": row.name,
                "balance": money_decimal(row.balance_after),
            }
            for row in rows[:5]
        ],
    }


def _inventory_snapshot(organization_id: int):
    value = (
        db.session.query(
            func.coalesce(
                func.sum(Product.cost_price * Product.stock), 0
            )
        )
        .filter(
            Product.organization_id == organization_id,
            Product.is_active.is_(True),
        )
        .scalar()
    )
    low_stock = (
        Product.query.filter(
            Product.organization_id == organization_id,
            Product.is_active.is_(True),
            Product.stock <= Product.min_stock,
        )
        .order_by(Product.stock.asc(), Product.name.asc())
        .limit(10)
        .all()
    )
    return {
        "value": money_decimal(value or 0),
        "low_stock": [
            {
                "name": product.name,
                "stock": product.stock,
                "min_stock": product.min_stock,
            }
            for product in low_stock
        ],
    }


def _cash_snapshot(organization_id: int, start_at, end_before):
    row = (
        db.session.query(
            func.count(CashRegisterSession.id).label("count"),
            func.coalesce(
                func.sum(CashRegisterSession.difference), 0
            ).label("net_difference"),
        )
        .filter(
            CashRegisterSession.organization_id == organization_id,
            CashRegisterSession.status == "CLOSED",
            CashRegisterSession.closed_at >= start_at,
            CashRegisterSession.closed_at < end_before,
            CashRegisterSession.difference.is_not(None),
            CashRegisterSession.difference != 0,
        )
        .one()
    )
    return {
        "count": int(row.count or 0),
        "net_difference": money_decimal(
            row.net_difference or 0,
            nonnegative=False,
        ),
    }


def _customer_sales_snapshot(organization_id: int, start_at, end_before):
    rows = (
        db.session.query(
            Customer.name,
            func.count(func.distinct(SalesTicket.id)).label("tickets"),
            func.coalesce(func.sum(Sale.total), 0).label("sales"),
        )
        .join(SalesTicket, SalesTicket.customer_id == Customer.id)
        .join(Sale, Sale.sales_ticket_id == SalesTicket.id)
        .filter(
            Customer.organization_id == organization_id,
            SalesTicket.organization_id == organization_id,
            Sale.organization_id == organization_id,
            SalesTicket.created_at >= start_at,
            SalesTicket.created_at < end_before,
        )
        .group_by(Customer.id, Customer.name)
        .order_by(func.sum(Sale.total).desc(), Customer.name.asc())
        .limit(5)
        .all()
    )
    return [
        {
            "name": row.name,
            "tickets": int(row.tickets or 0),
            "sales": _decimal_text(row.sales or 0),
        }
        for row in rows
    ]


def report_payload(organization: Organization, year: int, month: int):
    from app.routes import _report_analytics

    timezone_name = safe_timezone_name(organization.timezone)
    period = _period(year, month, timezone_name)
    analytics = _report_analytics(
        organization.id, period, timezone_name=timezone_name
    )
    previous_date = period["start_date"] - timedelta(days=1)
    previous_period = _period(
        previous_date.year, previous_date.month, timezone_name
    )
    previous = _report_analytics(
        organization.id,
        previous_period,
        timezone_name=timezone_name,
    )
    sales = analytics["report_kpis"]["sales"]
    previous_sales = previous["report_kpis"]["sales"]
    comparison = None
    if previous_sales:
        comparison = round(
            (sales - previous_sales) / previous_sales * 100, 1
        )
    profit = analytics["report_kpis"]["profit"]
    previous_profit = previous["report_kpis"]["profit"]
    profit_comparison = None
    if (
        previous_profit
        and analytics["unknown_cost_lines"] == 0
        and previous["unknown_cost_lines"] == 0
    ):
        profit_comparison = round(
            (profit - previous_profit) / abs(previous_profit) * 100, 1
        )
    inventory = _inventory_snapshot(organization.id)
    credit = _credit_snapshot(organization.id)
    cash = _cash_snapshot(
        organization.id, period["start_at"], period["end_before"]
    )
    customers = _customer_sales_snapshot(
        organization.id, period["start_at"], period["end_before"]
    )
    recommendations = []
    wins = []
    attention = []
    if comparison is not None and comparison > 0:
        wins.append(
            gettext(
                "Tus ventas crecieron %(percentage)s%% frente al mes anterior.",
                percentage=comparison,
            )
        )
    elif comparison is not None and comparison < 0:
        attention.append(
            gettext(
                "Tus ventas bajaron %(percentage)s%% frente al mes anterior.",
                percentage=abs(comparison),
            )
        )
    if profit_comparison is not None and profit_comparison > 0:
        wins.append(
            gettext(
                "Tu utilidad creció %(percentage)s%% frente al mes anterior.",
                percentage=profit_comparison,
            )
        )
    elif profit_comparison is not None and profit_comparison < 0:
        attention.append(
            gettext(
                "Tu utilidad bajó %(percentage)s%% frente al mes anterior.",
                percentage=abs(profit_comparison),
            )
        )
    if inventory["low_stock"]:
        first_low_stock = inventory["low_stock"][0]
        recommendations.append(
            gettext(
                "Repón %(product)s y revisa los demás productos con pocas existencias.",
                product=first_low_stock["name"],
            )
        )
        attention.append(
            gettext(
                "%(count)s productos necesitan reabastecimiento.",
                count=len(inventory["low_stock"]),
            )
        )
    if credit["total"] > MONEY_ZERO:
        credit_total_label = format_currency(credit["total"], "MXN")
        recommendations.append(
            gettext(
                "Da seguimiento a %(amount)s pendientes por cobrar.",
                amount=credit_total_label,
            )
        )
        attention.append(
            gettext(
                "Tienes %(amount)s pendientes por cobrar.",
                amount=credit_total_label,
            )
        )
    if cash["count"]:
        recommendations.append(
            gettext(
                "Revisa los %(count)s cierres con diferencias de caja.",
                count=cash["count"],
            )
        )
        attention.append(
            gettext(
                "%(count)s cierres de caja tuvieron diferencias.",
                count=cash["count"],
            )
        )
    if analytics["unknown_cost_lines"]:
        recommendations.append(
            gettext(
                "Completa los costos faltantes para obtener una utilidad exacta."
            )
        )
        attention.append(
            gettext(
                "%(count)s ventas no tienen un costo histórico completo.",
                count=analytics["unknown_cost_lines"],
            )
        )
    if not recommendations:
        recommendations.append(
            gettext(
                "Mantén el seguimiento actual; no detectamos alertas importantes."
            )
        )
    if not wins and sales:
        wins.append(
            gettext(
                "Registraste %(count)s ventas durante el mes.",
                count=analytics["report_kpis"]["ticket_count"],
            )
        )
    if not attention:
        attention.append(
            gettext("No detectamos situaciones urgentes en este periodo.")
        )
    if not sales:
        headline = gettext(
            "Este mes todavía no tiene ventas registradas en PATIA."
        )
    elif comparison is None:
        headline = gettext(
            "Ya tienes un mes de información listo para revisar."
        )
    elif comparison > 0:
        headline = gettext(
            "Vendiste %(percentage)s%% más que el mes anterior.",
            percentage=comparison,
        )
    elif comparison < 0:
        headline = gettext(
            "Vendiste %(percentage)s%% menos que el mes anterior.",
            percentage=abs(comparison),
        )
    else:
        headline = gettext("Tus ventas se mantuvieron estables este mes.")
    return {
        "organization": organization,
        "period": period,
        "period_label": format_date(period["start_date"], format="LLLL y"),
        "analytics": analytics,
        "comparison": comparison,
        "profit_comparison": profit_comparison,
        "inventory": inventory,
        "credit": credit,
        "cash": cash,
        "customers": customers,
        "headline": headline,
        "wins": wins[:3],
        "attention": attention[:3],
        "recommendations": recommendations,
    }


def generate_monthly_report(
    organization_id: int,
    year: int,
    month: int,
    *,
    send=False,
    preview=False,
    force_retry=False,
    resend=False,
    generated_by_member_id=None,
    manual_generation=False,
):
    """Generate one report period once; optionally deliver it by email."""
    organization = (
        Organization.query.options(selectinload(Organization.owner))
        .filter_by(id=organization_id, is_active=True)
        .first()
    )
    if not organization:
        raise MonthlyReportUnavailable("Organization not found.")
    owner = organization.owner
    eligible = subscription_access_is_active(
        owner,
        grace_days=current_app.config.get(
            "STRIPE_PAST_DUE_GRACE_DAYS", 3
        ),
    ) and has_entitlement(owner, "monthly_owner_report")
    if not eligible:
        raise MonthlyReportUnavailable(
            "Organization does not have the monthly report entitlement."
        )
    if send and not preview and not organization.monthly_report_enabled:
        raise MonthlyReportUnavailable(
            "Monthly report delivery is disabled."
        )
    recipient = (
        (organization.monthly_report_recipient or "").strip().lower()
        or owner.email
    )

    record = MonthlyOwnerReport.query.filter_by(
        organization_id=organization.id,
        report_year=year,
        report_month=month,
    ).first()
    if record and send and not resend and (
        record.status == "sent" or _delivery_is_claimed(record)
    ):
        return record, None
    if (
        record
        and send
        and record.status == "failed"
        and record.next_retry_at
        and record.next_retry_at > datetime.utcnow()
        and not force_retry
    ):
        return record, None
    if not record:
        record = MonthlyOwnerReport(
            organization_id=organization.id,
            report_year=year,
            report_month=month,
            recipient=recipient,
            status="pending",
        )
        db.session.add(record)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            record = MonthlyOwnerReport.query.filter_by(
                organization_id=organization.id,
                report_year=year,
                report_month=month,
            ).with_for_update().one()
    else:
        record = MonthlyOwnerReport.query.filter_by(
            id=record.id
        ).with_for_update().one()
    if send and not resend and (
        record.status == "sent" or _delivery_is_claimed(record)
    ):
        return record, None

    try:
        language = owner.preferred_language or "es"
        with current_app.test_request_context("/"):
            with force_locale(language):
                payload = payload_from_snapshot(record)
                if payload is None:
                    live_payload = report_payload(
                        organization, year, month
                    )
                    subject = gettext(
                        "Tu resumen mensual de %(business)s — %(period)s",
                        business=organization.name,
                        period=live_payload["period_label"],
                    )
                    snapshot = build_report_snapshot(
                        live_payload, subject
                    )
                    snapshot = enrich_snapshot_narrative(
                        snapshot, organization.id
                    )
                    serialized = serialize_snapshot(snapshot)
                    record.snapshot_json = serialized
                    record.snapshot_hash = sha256(
                        serialized.encode("utf-8")
                    ).hexdigest()
                    record.snapshot_version = SNAPSHOT_VERSION
                    record.generated_by_member_id = (
                        generated_by_member_id
                    )
                    record.manual_generation = bool(manual_generation)
                    record.generated_at = datetime.utcnow()
                    live_payload["headline"] = snapshot["narrative"][
                        "summary"
                    ]
                    live_payload["recommendations"] = snapshot[
                        "narrative"
                    ]["recommended_actions"]
                    payload = {
                        "subject": subject,
                        "snapshot": snapshot,
                        **live_payload,
                    }
                subject = payload["subject"]
                reports_url = (
                    current_app.config["PUBLIC_BASE_URL"].rstrip("/")
                    + "/pro/monthly-reports"
                )
                html = render_template(
                    "emails/monthly_owner_report.html",
                    reports_url=reports_url,
                    **payload,
                )
        record.recipient = recipient
        if record.status not in {"sent", "failed"}:
            record.failure_code = None
            record.error_message = None
            record.status = "generated"

        db.session.commit()
        response_payload = {"subject": subject, "html": html, **payload}
        if not send or preview:
            return record, response_payload

        # Persist the delivery claim before contacting Resend. Concurrent
        # workers will observe ``sending`` and will not duplicate the email.
        record = MonthlyOwnerReport.query.filter_by(
            id=record.id
        ).with_for_update().one()
        if (
            not resend
            and (
                record.status == "sent"
                or _delivery_is_claimed(record)
            )
        ):
            db.session.rollback()
            return record, None
        record.status = "sending"
        record.attempt_count = int(record.attempt_count or 0) + 1
        record.last_attempt_at = datetime.utcnow()
        record.next_retry_at = None
        record.failure_code = None
        record.error_message = None
        db.session.commit()

        from app.routes import send_email

        delivered = send_email(
            to=recipient,
            subject=subject,
            html=html,
            language=language,
            idempotency_key=(
                f"patia-monthly-report-{organization.id}-"
                f"{year:04d}-{month:02d}"
                + (
                    f"-resend-{record.attempt_count}"
                    if resend
                    else ""
                )
            ),
        )
        record = db.session.get(MonthlyOwnerReport, record.id)
        if delivered:
            record.status = "sent"
            record.sent_at = datetime.utcnow()
            record.next_retry_at = None
            record.failure_code = None
            record.error_message = None
        else:
            record.status = "failed"
            record.sent_at = None
            record.failure_code = "DELIVERY_REJECTED"
            record.error_message = (
                "El proveedor de correo no confirmó el envío."
            )
            record.next_retry_at = _next_retry_at(record.attempt_count)
        db.session.commit()
        return record, response_payload
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Monthly report generation failed for organization_id=%s period=%04d-%02d",
            organization_id,
            year,
            month,
        )
        record = MonthlyOwnerReport.query.filter_by(
            organization_id=organization.id,
            report_year=year,
            report_month=month,
        ).first()
        if record:
            attempt_was_started = record.status == "sending"
            record.status = "failed"
            record.sent_at = None
            if not attempt_was_started:
                record.attempt_count = int(record.attempt_count or 0) + 1
            record.last_attempt_at = datetime.utcnow()
            record.next_retry_at = _next_retry_at(record.attempt_count)
            record.failure_code = "GENERATION_ERROR"
            record.error_message = (
                "No pudimos generar o entregar el reporte mensual."
            )
            db.session.commit()
        raise


def run_monthly_reports(year: int, month: int):
    summary = {"sent": 0, "skipped": 0, "failed": 0}
    last_id = 0
    while True:
        organizations = (
            Organization.query.options(selectinload(Organization.owner))
            .filter(
                Organization.id > last_id,
                Organization.is_active.is_(True),
                Organization.monthly_report_enabled.is_(True),
            )
            .order_by(Organization.id)
            .limit(100)
            .all()
        )
        if not organizations:
            break
        for organization in organizations:
            try:
                record, delivery_payload = generate_monthly_report(
                    organization.id, year, month, send=True
                )
                if delivery_payload is None:
                    summary["skipped"] += 1
                elif record.status == "sent":
                    summary["sent"] += 1
                elif record.status == "failed":
                    summary["failed"] += 1
                else:
                    summary["skipped"] += 1
            except MonthlyReportUnavailable:
                db.session.rollback()
                summary["skipped"] += 1
            except Exception:
                db.session.rollback()
                current_app.logger.exception(
                    "Monthly report failed for organization_id=%s",
                    organization.id,
                )
                summary["failed"] += 1
        last_id = organizations[-1].id
    return summary
