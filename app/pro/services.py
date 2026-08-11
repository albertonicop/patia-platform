from __future__ import annotations

import calendar
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from flask import has_request_context, url_for
from flask_babel import gettext, ngettext
from sqlalchemy import case, exists, func

from app import db
from app.models import (
    CashMovement,
    CashRegisterSession,
    CustomerCreditMovement,
    InventoryMovement,
    OrganizationMember,
    Product,
    Sale,
)
from app.money import MONEY_ZERO, money_decimal
from app.currencies import format_money
from app.timezones import (
    local_date_bounds_utc,
    local_today,
    safe_timezone_name,
    utc_to_local,
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


def _internal_url(endpoint, **values):
    """Build app links in requests and deterministic paths in service tests."""
    if has_request_context():
        return url_for(endpoint, **values)
    paths = {
        "main.products": "/products",
        "main.reports": "/reports",
        "inventory.index": "/inventory/kardex",
        "credit.index": "/credit",
        "cash.index": "/cash-register",
    }
    path = paths[endpoint]
    clean_values = {
        key: value
        for key, value in values.items()
        if value not in (None, "")
    }
    return f"{path}?{urlencode(clean_values)}" if clean_values else path


def _margin_change(current, previous):
    if current is None or previous is None:
        return None
    return round(Decimal(str(current)) - Decimal(str(previous)), 1)


def _executive_summary(
    current, previous, sales_change, margin_change, organization
):
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
                sales=format_money(current["sales"], organization),
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


def _best_hour(organization_id, period, timezone_name):
    """Return the strongest local sales hour without loading individual sales."""
    if db.session.get_bind().dialect.name == "postgresql":
        hour_bucket = func.date_trunc("hour", Sale.created_at)
    else:
        hour_bucket = func.strftime("%Y-%m-%d %H:00:00", Sale.created_at)
    rows = (
        db.session.query(
            hour_bucket.label("hour"),
            func.sum(Sale.total).label("sales"),
        )
        .filter(
            Sale.organization_id == organization_id,
            Sale.created_at >= period["start_at"],
            Sale.created_at < period["end_before"],
        )
        .group_by(hour_bucket)
        .all()
    )
    local_hours = {}
    for row in rows:
        hour = row.hour
        if isinstance(hour, str):
            hour = datetime.fromisoformat(hour)
        local_hour = utc_to_local(hour, timezone_name).hour
        local_hours[local_hour] = (
            local_hours.get(local_hour, MONEY_ZERO)
            + money_decimal(row.sales or 0)
        )
    if not local_hours:
        return None
    hour, sales = max(local_hours.items(), key=lambda item: (item[1], -item[0]))
    return {
        "hour": hour,
        "range": f"{hour:02d}:00–{(hour + 1) % 24:02d}:00",
        "sales": sales,
    }


def _credit_balance(organization_id):
    latest_ids = (
        db.session.query(
            CustomerCreditMovement.customer_id,
            func.max(CustomerCreditMovement.id).label("last_id"),
        )
        .filter(CustomerCreditMovement.organization_id == organization_id)
        .group_by(CustomerCreditMovement.customer_id)
        .subquery()
    )
    row = (
        db.session.query(
            func.coalesce(
                func.sum(CustomerCreditMovement.balance_after), 0
            ).label("balance"),
            func.coalesce(
                func.sum(
                    case(
                        (CustomerCreditMovement.balance_after > 0, 1),
                        else_=0,
                    )
                ),
                0,
            ).label("customers"),
        )
        .join(latest_ids, CustomerCreditMovement.id == latest_ids.c.last_id)
        .one()
    )
    return {
        "balance": money_decimal(row.balance or 0),
        "customers": int(row.customers or 0),
    }


def _inventory_control(organization_id):
    row = (
        db.session.query(
            func.count(Product.id).label("products"),
            func.coalesce(
                func.sum(Product.stock * Product.cost_price), 0
            ).label("value"),
            func.coalesce(
                func.sum(
                    case((Product.stock <= Product.min_stock, 1), else_=0)
                ),
                0,
            ).label("low_stock"),
            func.coalesce(
                func.sum(case((Product.stock <= 0, 1), else_=0)),
                0,
            ).label("out_of_stock"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            Product.stock <= Product.min_stock,
                            Product.min_stock - Product.stock,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("suggested_units"),
        )
        .filter(
            Product.organization_id == organization_id,
            Product.is_active.is_(True),
        )
        .one()
    )
    return {
        "products": int(row.products or 0),
        "value": money_decimal(row.value or 0),
        "low_stock": int(row.low_stock or 0),
        "out_of_stock": int(row.out_of_stock or 0),
        "suggested_units": int(row.suggested_units or 0),
    }


def _cash_control(organization_id):
    from app.cash.services import expected_cash

    current = (
        CashRegisterSession.query.filter_by(
            organization_id=organization_id,
            status="OPEN",
            open_key="MAIN",
        )
        .order_by(CashRegisterSession.opened_at.desc())
        .first()
    )
    if current:
        return {
            "status": "open",
            "amount": expected_cash(current.id),
            "difference": None,
        }
    latest = (
        CashRegisterSession.query.filter_by(
            organization_id=organization_id,
            status="CLOSED",
        )
        .order_by(
            CashRegisterSession.closed_at.desc(),
            CashRegisterSession.id.desc(),
        )
        .first()
    )
    return {
        "status": "closed",
        "amount": None,
        "difference": (
            money_decimal(latest.difference, nonnegative=False)
            if latest and latest.difference is not None
            else None
        ),
    }


def _products_without_movement(organization_id, period):
    sold_in_period = exists().where(
        Sale.organization_id == organization_id,
        Sale.product_id == Product.id,
        Sale.created_at >= period["start_at"],
        Sale.created_at < period["end_before"],
    )
    query = Product.query.filter(
        Product.organization_id == organization_id,
        Product.is_active.is_(True),
        ~sold_in_period,
    )
    return {
        "count": query.count(),
        "examples": [
            {"id": row.id, "name": row.name, "sku": row.sku}
            for row in query.order_by(Product.name).limit(2).all()
        ],
    }


def _cash_differences(organization_id, period):
    row = (
        db.session.query(
            func.coalesce(
                func.sum(
                    case(
                        (
                            CashRegisterSession.difference != 0,
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("count"),
            func.coalesce(
                func.sum(func.abs(CashRegisterSession.difference)), 0
            ).label("amount"),
        )
        .filter(
            CashRegisterSession.organization_id == organization_id,
            CashRegisterSession.status == "CLOSED",
            CashRegisterSession.closed_at >= period["start_at"],
            CashRegisterSession.closed_at < period["end_before"],
        )
        .one()
    )
    return {
        "count": int(row.count or 0),
        "amount": money_decimal(row.amount or 0),
    }


def _team_activity(organization_id, period, cash_difference):
    active_members = OrganizationMember.query.filter_by(
        organization_id=organization_id,
        is_active=True,
    ).count()
    if active_members <= 1:
        return {"visible": False, "member_count": active_members, "items": []}

    movement_rows = dict(
        db.session.query(
            InventoryMovement.movement_type,
            func.count(InventoryMovement.id),
        )
        .filter(
            InventoryMovement.organization_id == organization_id,
            InventoryMovement.created_at >= period["start_at"],
            InventoryMovement.created_at < period["end_before"],
            InventoryMovement.movement_type.in_(
                (
                    "SALE_CANCELLATION",
                    "ADJUSTMENT_IN",
                    "ADJUSTMENT_OUT",
                    "PHYSICAL_COUNT",
                )
            ),
        )
        .group_by(InventoryMovement.movement_type)
        .all()
    )
    corrections = sum(
        movement_rows.get(key, 0)
        for key in (
            "ADJUSTMENT_IN",
            "ADJUSTMENT_OUT",
            "PHYSICAL_COUNT",
        )
    )
    withdrawal = (
        db.session.query(
            func.count(CashMovement.id).label("count"),
            func.coalesce(func.sum(CashMovement.amount), 0).label("amount"),
        )
        .filter(
            CashMovement.organization_id == organization_id,
            CashMovement.movement_type == "WITHDRAWAL",
            CashMovement.created_at >= period["start_at"],
            CashMovement.created_at < period["end_before"],
        )
        .one()
    )
    return {
        "visible": True,
        "member_count": active_members,
        "items": [
            {
                "key": "cancellations",
                "label": gettext("Ventas canceladas"),
                "count": int(movement_rows.get("SALE_CANCELLATION", 0)),
                "amount": None,
                "url": _internal_url(
                    "inventory.index",
                    type="SALE_CANCELLATION",
                    date_from=period["start_date"].isoformat(),
                    date_to=period["end_date"].isoformat(),
                    source="decisions",
                ),
                "icon": "fa-rotate-left",
            },
            {
                "key": "corrections",
                "label": gettext("Correcciones de inventario"),
                "count": int(corrections),
                "amount": None,
                "url": _internal_url(
                    "inventory.index",
                    group="corrections",
                    date_from=period["start_date"].isoformat(),
                    date_to=period["end_date"].isoformat(),
                    source="decisions",
                ),
                "icon": "fa-boxes-stacked",
            },
            {
                "key": "withdrawals",
                "label": gettext("Retiros de caja"),
                "count": int(withdrawal.count or 0),
                "amount": money_decimal(withdrawal.amount or 0),
                "url": _internal_url(
                    "cash.index",
                    movements="withdrawals",
                    start=period["start_date"].isoformat(),
                    end=period["end_date"].isoformat(),
                    source="decisions",
                ),
                "icon": "fa-money-bill-transfer",
            },
            {
                "key": "differences",
                "label": gettext("Cierres con diferencia"),
                "count": cash_difference["count"],
                "amount": cash_difference["amount"],
                "url": _internal_url(
                    "cash.index",
                    differences=1,
                    start=period["start_date"].isoformat(),
                    end=period["end_date"].isoformat(),
                    source="decisions",
                ),
                "icon": "fa-scale-balanced",
            },
        ],
    }


def _actionable_snapshot(
    organization,
    period,
    current,
    previous,
    margin_change,
    sales_change,
):
    organization_id = organization.id
    inventory = _inventory_control(organization_id)
    credit = _credit_balance(organization_id)
    cash = _cash_control(organization_id)
    no_movement = _products_without_movement(organization_id, period)

    profitable_products = [
        product
        for product in current["profitable_products_report"]
        if product["profit"] is not None
    ][:3]
    drivers = [
        {
            "kind": "product",
            "label": gettext("Producto rentable"),
            "title": product["name"],
            "value": product["profit"],
            "evidence": gettext(
                "%(units)s unidades · %(margin)s%% de margen",
                units=product["units"],
                margin=f"{product['margin']:.1f}",
            ),
            "icon": "fa-box",
        }
        for product in profitable_products
    ]
    selling_days = [
        day for day in current["daily_report"] if day["sales"] > MONEY_ZERO
    ]
    if selling_days:
        best_day = max(selling_days, key=lambda item: item["sales"])
        drivers.append(
            {
                "kind": "day",
                "label": gettext("Mejor día"),
                "title": datetime.fromisoformat(
                    best_day["date"]
                ).strftime("%d/%m/%Y"),
                "value": best_day["sales"],
                "evidence": gettext("Ventas registradas ese día"),
                "icon": "fa-calendar-day",
            }
        )
    best_hour = _best_hour(
        organization_id,
        period,
        safe_timezone_name(organization.timezone),
    )
    if best_hour:
        drivers.append(
            {
                "kind": "hour",
                "label": gettext("Mejor horario"),
                "title": best_hour["range"],
                "value": best_hour["sales"],
                "evidence": gettext("Ventas acumuladas en esa franja"),
                "icon": "fa-clock",
            }
        )

    attention = []
    if sales_change is not None and sales_change <= Decimal("-10"):
        attention.append(
            {
                "key": "sales",
                "priority": 110 + abs(float(sales_change)),
                "title": gettext("Las ventas disminuyeron"),
                "evidence": gettext(
                    "Vendiste %(percentage)s%% menos que en el periodo anterior.",
                    percentage=f"{abs(sales_change):.1f}",
                ),
                "action": gettext(
                    "Revisa los productos y días que explican la caída."
                ),
                "url": _internal_url(
                    "main.reports",
                    period=period["period"],
                    start=period["custom_start"],
                    end=period["custom_end"],
                ),
                "action_label": gettext("Analizar ventas"),
                "icon": "fa-arrow-trend-down",
                "tone": "danger",
            }
        )
    current_margin = current["report_kpis"]["margin"]
    previous_margin = previous["report_kpis"]["margin"]
    if (
        margin_change is not None
        and margin_change <= Decimal("-2")
        and current_margin is not None
        and previous_margin is not None
        and current["unknown_cost_lines"] == 0
        and previous["unknown_cost_lines"] == 0
    ):
        attention.append(
            {
                "key": "margin",
                "priority": 100 + abs(float(margin_change)),
                "title": gettext("El margen se redujo"),
                "evidence": gettext(
                    "Pasó de %(previous)s%% a %(current)s%% en el periodo.",
                    previous=f"{previous_margin:.1f}",
                    current=f"{current_margin:.1f}",
                ),
                "action": gettext(
                    "Revisa precios y costos de los productos vendidos."
                ),
                "url": _internal_url("main.products", low_margin=1, source="decisions"),
                "action_label": gettext("Revisar productos"),
                "icon": "fa-percent",
                "tone": "warning",
            }
        )
    if current["unknown_cost_lines"]:
        attention.append(
            {
                "key": "cost",
                "priority": 72 + current["unknown_cost_lines"],
                "title": ngettext(
                    "%(count)s venta no tiene costo conocido",
                    "%(count)s ventas no tienen costo conocido",
                    current["unknown_cost_lines"],
                    count=current["unknown_cost_lines"],
                ),
                "evidence": gettext(
                    "La utilidad no puede calcularse completamente sin el costo del producto."
                ),
                "action": gettext(
                    "Agrega los costos faltantes para recuperar una lectura confiable del margen."
                ),
                "url": _internal_url("main.products", missing_cost=1, source="decisions"),
                "action_label": gettext("Completar costos"),
                "icon": "fa-circle-dollar-to-slot",
                "tone": "warning",
            }
        )
    if inventory["out_of_stock"]:
        attention.append(
            {
                "key": "out_of_stock",
                "priority": 125 + inventory["out_of_stock"],
                "title": ngettext(
                    "%(count)s producto está agotado",
                    "%(count)s productos están agotados",
                    inventory["out_of_stock"],
                    count=inventory["out_of_stock"],
                ),
                "evidence": gettext(
                    "Estos productos ya no tienen unidades disponibles."
                ),
                "action": gettext(
                    "Define cuáles reabastecer primero para recuperar ventas."
                ),
                "url": _internal_url(
                    "main.products", out_of_stock=1, source="decisions"
                ),
                "action_label": gettext("Reabastecer productos agotados"),
                "icon": "fa-box-open",
                "tone": "danger",
            }
        )
    low_stock_available = (
        inventory["low_stock"] - inventory["out_of_stock"]
    )
    if low_stock_available:
        attention.append(
            {
                "key": "stock",
                "priority": 90 + low_stock_available,
                "title": ngettext(
                    "%(count)s producto puede agotarse",
                    "%(count)s productos pueden agotarse",
                    low_stock_available,
                    count=low_stock_available,
                ),
                "evidence": ngettext(
                    "La reposición sugerida suma %(units)s unidad.",
                    "La reposición sugerida suma %(units)s unidades.",
                    inventory["suggested_units"],
                    units=inventory["suggested_units"],
                ),
                "action": gettext(
                    "Prioriza la mercancía que ya alcanzó su mínimo."
                ),
                "url": _internal_url(
                    "main.products",
                    low_stock=1,
                    in_stock=1,
                    source="decisions",
                ),
                "action_label": gettext("Preparar reabastecimiento"),
                "icon": "fa-box-open",
                "tone": "danger",
            }
        )
    if no_movement["count"]:
        attention.append(
            {
                "key": "movement",
                "priority": 60 + no_movement["count"],
                "title": ngettext(
                    "%(count)s producto no tuvo ventas",
                    "%(count)s productos no tuvieron ventas",
                    no_movement["count"],
                    count=no_movement["count"],
                ),
                "evidence": gettext(
                    "No registraron movimiento entre %(start)s y %(end)s.",
                    start=period["start_date"].strftime("%d/%m/%Y"),
                    end=period["end_date"].strftime("%d/%m/%Y"),
                ),
                "action": gettext(
                    "Evalúa precio, exhibición o próxima compra."
                ),
                "url": (
                    _internal_url(
                        "main.products",
                        no_sales=1,
                        start=period["start_date"].isoformat(),
                        end=period["end_date"].isoformat(),
                        source="decisions",
                    )
                ),
                "action_label": gettext(
                    "Decidir qué promover o dejar de comprar"
                ),
                "icon": "fa-box-archive",
                "tone": "neutral",
            }
        )
    if credit["balance"] > MONEY_ZERO:
        attention.append(
            {
                "key": "credit",
                "priority": 85 + credit["customers"],
                "title": gettext("Hay saldos pendientes por cobrar"),
                "evidence": ngettext(
                    "%(count)s cliente debe %(amount)s.",
                    "%(count)s clientes deben %(amount)s.",
                    credit["customers"],
                    count=credit["customers"],
                    amount=format_money(credit["balance"], organization),
                ),
                "action": gettext(
                    "Da seguimiento a los saldos con mayor antigüedad."
                ),
                "url": _internal_url("credit.index", source="decisions"),
                "action_label": gettext("Ver saldos pendientes"),
                "icon": "fa-hand-holding-dollar",
                "tone": "warning",
            }
        )

    cash_differences = _cash_differences(organization_id, period)
    activity = _team_activity(
        organization_id, period, cash_differences
    )
    if cash_differences["count"]:
        attention.append(
            {
                "key": "cash",
                "priority": 80 + cash_differences["count"],
                "title": ngettext(
                    "%(count)s cierre tuvo diferencia",
                    "%(count)s cierres tuvieron diferencia",
                    cash_differences["count"],
                    count=cash_differences["count"],
                ),
                "evidence": gettext(
                    "La diferencia absoluta acumulada fue %(amount)s.",
                    amount=format_money(
                        cash_differences["amount"], organization
                    ),
                ),
                "action": gettext(
                    "Compara el efectivo esperado con los cierres registrados."
                ),
                "url": _internal_url(
                    "cash.index",
                    differences=1,
                    start=period["start_date"].isoformat(),
                    end=period["end_date"].isoformat(),
                    source="decisions",
                ),
                "action_label": gettext("Abrir Caja del día"),
                "icon": "fa-cash-register",
                "tone": "warning",
            }
        )
    attention = sorted(
        attention, key=lambda item: item["priority"], reverse=True
    )[:4]

    control = {
        "inventory": {
            **inventory,
            "url": _internal_url("main.products"),
        },
        "credit": {
            **credit,
            "url": _internal_url("credit.index"),
        },
        "cash": {
            **cash,
            "url": _internal_url("cash.index"),
        },
    }
    action_candidates = []
    for finding in attention:
        action_candidates.append(
            {
                "priority": finding["priority"],
                "title": finding["title"],
                "evidence": finding["evidence"],
                "label": finding["action_label"],
                "url": finding["url"],
                "icon": finding["icon"],
            }
        )
    if credit["balance"] > MONEY_ZERO:
        action_candidates.append(
            {
                "priority": 85 + credit["customers"],
                "title": gettext(
                    "Da seguimiento a %(amount)s por cobrar",
                    amount=format_money(credit["balance"], organization),
                ),
                "evidence": ngettext(
                    "%(count)s cliente mantiene saldo pendiente.",
                    "%(count)s clientes mantienen saldo pendiente.",
                    credit["customers"],
                    count=credit["customers"],
                ),
                "label": gettext("Ver saldos pendientes"),
                "url": _internal_url("credit.index"),
                "icon": "fa-hand-holding-dollar",
            }
        )
    if cash["status"] == "closed":
        action_candidates.append(
            {
                "priority": 50,
                "title": gettext("La caja principal está cerrada"),
                "evidence": gettext(
                    "Las ventas en efectivo necesitan una caja abierta."
                ),
                "label": gettext("Abrir Caja del día"),
                "url": _internal_url("cash.index"),
                "icon": "fa-cash-register",
            }
        )
    actions = sorted(
        action_candidates,
        key=lambda item: item["priority"],
        reverse=True,
    )[:3]
    return {
        "executive_drivers": drivers[:5],
        "executive_attention": attention,
        "executive_control": control,
        "team_activity": activity,
        "priority_actions": actions,
        "products_without_movement": no_movement,
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
        currency_code=organization.currency_code,
    )
    previous = _report_analytics(
        organization.id,
        previous_period,
        timezone_name=timezone_name,
        currency_code=organization.currency_code,
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
            currency_code=organization.currency_code,
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

    result = {
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
            organization,
        ),
        "monthly_goal": goal,
        "monthly_sales": month_sales,
        "monthly_goal_progress": goal_progress,
        "monthly_projection": projection,
        "executive_chart": {
            "current": current["daily_report"],
            "previous": previous["daily_report"],
        },
        "payments_report": current["payments_report"],
        "unknown_cost_lines": current["unknown_cost_lines"],
    }
    result.update(
        _actionable_snapshot(
            organization,
            period,
            current,
            previous,
            margin_change,
            sales_change,
        )
    )
    return result


def build_smart_alerts(organization, args, *, now_utc=None):
    """Reuse the executive evidence engine for the dedicated alert center."""
    data = build_executive_dashboard(
        organization,
        args,
        now_utc=now_utc,
    )
    alerts = []
    for item in data["executive_attention"]:
        alerts.append(
            {
                **item,
                "priority_label": (
                    gettext("Alta")
                    if item["priority"] >= 90
                    else (
                        gettext("Media")
                        if item["priority"] >= 70
                        else gettext("Informativa")
                    )
                ),
            }
        )
    return {
        "alert_period": data["executive_period"],
        "smart_alerts": alerts,
        "executive_control": data["executive_control"],
        "team_activity": data["team_activity"],
        "alert_summary": {
            "total": len(alerts),
            "high": sum(
                1 for item in alerts if item["priority"] >= 90
            ),
            "medium": sum(
                1
                for item in alerts
                if 70 <= item["priority"] < 90
            ),
        },
    }
