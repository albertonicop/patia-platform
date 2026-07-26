"""Idempotent monthly owner report generation for eligible organizations."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta

from flask import current_app, render_template
from flask_babel import force_locale, format_currency, format_date, gettext
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app import db
from app.models import (
    CashRegisterSession,
    Customer,
    CustomerCreditMovement,
    MonthlyOwnerReport,
    Organization,
    Product,
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
    products = Product.query.filter_by(
        organization_id=organization_id, is_active=True
    ).all()
    return {
        "value": money_decimal(
            sum(
                (
                    product.cost_price * product.stock
                    for product in products
                ),
                MONEY_ZERO,
            )
        ),
        "low_stock": [
            {
                "name": product.name,
                "stock": product.stock,
                "min_stock": product.min_stock,
            }
            for product in products
            if product.stock <= product.min_stock
        ][:10],
    }


def _cash_snapshot(organization_id: int, start_at, end_before):
    rows = CashRegisterSession.query.filter(
        CashRegisterSession.organization_id == organization_id,
        CashRegisterSession.status == "CLOSED",
        CashRegisterSession.closed_at >= start_at,
        CashRegisterSession.closed_at < end_before,
        CashRegisterSession.difference.is_not(None),
        CashRegisterSession.difference != 0,
    ).all()
    return {
        "count": len(rows),
        "net_difference": money_decimal(
            sum((row.difference for row in rows), MONEY_ZERO),
            nonnegative=False,
        ),
    }


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
    if record and send and (
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
    if send and (
        record.status == "sent" or _delivery_is_claimed(record)
    ):
        return record, None

    try:
        language = owner.preferred_language or "es"
        # Scheduled jobs do not have a browser request. This minimal context
        # lets Flask-Babel and the existing Jinja processors render from CLI.
        with current_app.test_request_context("/"):
            with force_locale(language):
                payload = report_payload(organization, year, month)
                reports_url = (
                    current_app.config["PUBLIC_BASE_URL"].rstrip("/")
                    + "/reports"
                )
                html = render_template(
                    "emails/monthly_owner_report.html",
                    reports_url=reports_url,
                    **payload,
                )
                subject = gettext(
                "Tu resumen mensual de %(business)s — %(period)s",
                    business=organization.name,
                    period=payload["period_label"],
                )
        record.recipient = recipient
        record.generated_at = datetime.utcnow()
        record.sent_at = None
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
        if record.status == "sent" or _delivery_is_claimed(record):
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
                f"patia-monthly-report-{organization.id}-{year:04d}-{month:02d}"
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
                record, _ = generate_monthly_report(
                    organization.id, year, month, send=True
                )
                if record.status == "sent":
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
