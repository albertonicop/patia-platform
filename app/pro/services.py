from __future__ import annotations

import calendar
from datetime import timedelta
from decimal import Decimal

from flask_babel import gettext

from app.money import MONEY_ZERO, money_decimal
from app.timezones import (
    local_date_bounds_utc,
    local_today,
    safe_timezone_name,
)


def _period_from_dates(start_date, end_date, timezone_name):
    start_at, end_before = local_date_bounds_utc(
        start_date,
        end_date + timedelta(days=1),
        timezone_name,
    )
    return {
        "period": "comparison",
        "start_date": start_date,
        "end_date": end_date,
        "start_at": start_at,
        "end_before": end_before,
        "custom_start": "",
        "custom_end": "",
        "error": None,
    }


def _comparison_period(period, timezone_name):
    length = (period["end_date"] - period["start_date"]).days + 1
    previous_end = period["start_date"] - timedelta(days=1)
    previous_start = previous_end - timedelta(days=length - 1)
    return _period_from_dates(previous_start, previous_end, timezone_name)


def _percentage_change(current, previous):
    current = Decimal(str(current or 0))
    previous = Decimal(str(previous or 0))
    if previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


def _margin_change(current, previous):
    if current is None or previous is None:
        return None
    return round(Decimal(str(current)) - Decimal(str(previous)), 1)


def _executive_summary(current, previous, sales_change, margin_change):
    if not current["ticket_count"]:
        return {
            "state": "insufficient",
            "label": gettext("Datos insuficientes"),
            "title": gettext("Tu resumen comenzará con la primera venta"),
            "body": gettext(
                "Registra ventas para comparar el desempeño, calcular la utilidad "
                "y proyectar el cierre del mes."
            ),
        }
    if not previous["ticket_count"] or sales_change is None:
        return {
            "state": "stable",
            "label": gettext("Primer periodo comparable"),
            "title": gettext("Tu negocio ya está generando información útil"),
            "body": gettext(
                "Registraste %(tickets)s ventas por %(sales)s. Cuando exista un "
                "periodo anterior comparable, PATIA mostrará la tendencia.",
                tickets=current["ticket_count"],
                sales=f"${current['sales']:,.2f}",
            ),
        }
    if sales_change >= Decimal("5") and (
        margin_change is None or margin_change >= Decimal("-1")
    ):
        margin_text = ""
        if margin_change is not None and margin_change > 0:
            margin_text = gettext(
                " y el margen mejoró %(points)s puntos",
                points=margin_change,
            )
        return {
            "state": "strong",
            "label": gettext("Resultado favorable"),
            "title": gettext("Las ventas avanzan con una tendencia saludable"),
            "body": gettext(
                "Vendiste %(change)s%% más que en el periodo anterior"
                "%(margin_text)s.",
                change=abs(sales_change),
                margin_text=margin_text,
            ),
        }
    if sales_change <= Decimal("-5"):
        return {
            "state": "attention",
            "label": gettext("Requiere atención"),
            "title": gettext("Las ventas bajaron frente al periodo anterior"),
            "body": gettext(
                "La variación fue de %(change)s%%. Revisa el periodo completo "
                "antes de tomar una decisión.",
                change=abs(sales_change),
            ),
        }
    return {
        "state": "stable",
        "label": gettext("Negocio estable"),
        "title": gettext("El desempeño se mantiene cerca del periodo anterior"),
        "body": gettext(
            "Las ventas variaron %(change)s%% y no muestran un cambio brusco.",
            change=abs(sales_change),
        ),
    }


def build_executive_dashboard(
    organization,
    args,
    *,
    now_utc=None,
):
    """Build one tenant-scoped executive snapshot from existing sales data."""
    from app.routes import _parse_report_period, _report_analytics

    timezone_name = safe_timezone_name(organization.timezone)
    requested_args = {
        "period": args.get("period") or "this_month",
        "start": args.get("start") or "",
        "end": args.get("end") or "",
    }
    period = _parse_report_period(
        requested_args,
        timezone_name=timezone_name,
        now_utc=now_utc,
    )
    previous_period = _comparison_period(period, timezone_name)
    current = _report_analytics(
        organization.id,
        period,
        timezone_name=timezone_name,
    )
    previous = _report_analytics(
        organization.id,
        previous_period,
        timezone_name=timezone_name,
    )
    current_kpis = current["report_kpis"]
    previous_kpis = previous["report_kpis"]
    sales_change = _percentage_change(
        current_kpis["sales"], previous_kpis["sales"]
    )
    profit_change = _percentage_change(
        current_kpis["profit"], previous_kpis["profit"]
    )
    average_change = _percentage_change(
        current_kpis["average_ticket"],
        previous_kpis["average_ticket"],
    )
    margin_change = _margin_change(
        current_kpis["margin"], previous_kpis["margin"]
    )

    today = local_today(timezone_name, now_utc=now_utc)
    month_period = _period_from_dates(today.replace(day=1), today, timezone_name)
    month = (
        current
        if period["period"] == "this_month"
        else _report_analytics(
            organization.id,
            month_period,
            timezone_name=timezone_name,
        )
    )
    month_sales = money_decimal(month["report_kpis"]["sales"])
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    projection = None
    if today.day >= 3 and month["report_kpis"]["ticket_count"] >= 3:
        projection = money_decimal(
            month_sales / today.day * days_in_month
        )
    goal = money_decimal(
        organization.monthly_sales_goal,
        allow_none=True,
    )
    goal_progress = None
    if goal and goal > MONEY_ZERO:
        goal_progress = round(month_sales / goal * 100, 1)

    return {
        "executive_period": period,
        "previous_period": previous_period,
        "executive_kpis": {
            "sales": current_kpis["sales"],
            "sales_change": sales_change,
            "profit": current_kpis["profit"],
            "profit_change": profit_change,
            "profit_coverage": current_kpis["profit_coverage"],
            "margin": current_kpis["margin"],
            "margin_change": margin_change,
            "average_ticket": current_kpis["average_ticket"],
            "average_change": average_change,
            "ticket_count": current_kpis["ticket_count"],
        },
        "executive_summary": _executive_summary(
            current_kpis,
            previous_kpis,
            sales_change,
            margin_change,
        ),
        "monthly_goal": goal,
        "monthly_sales": month_sales,
        "monthly_goal_progress": goal_progress,
        "monthly_projection": projection,
        "executive_chart": {
            "current": current["daily_report"],
            "previous": previous["daily_report"],
        },
        "unknown_cost_lines": current["unknown_cost_lines"],
    }
