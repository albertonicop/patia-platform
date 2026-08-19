"""Single source of truth for PATIA commercial plans and entitlements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from flask_babel import gettext


TRIAL = "TRIAL"
STARTER = "STARTER"
PRO = "PRO"
RESTAURANT = "RESTAURANT"
GRANDFATHERED = "GRANDFATHERED"
MANUAL = "MANUAL"

PAID_PLAN_CODES = frozenset({STARTER, PRO, RESTAURANT})
KNOWN_PLAN_CODES = frozenset(
    {TRIAL, STARTER, PRO, RESTAURANT, GRANDFATHERED, MANUAL}
)


@dataclass(frozen=True)
class PlanEntitlements:
    max_members: int
    advanced_roles: bool
    advanced_inventory_history: bool
    advanced_reports: bool
    advanced_exports: bool
    monthly_owner_report: bool
    priority_support: bool
    executive_dashboard: bool
    recipes: bool


STARTER_ENTITLEMENTS = PlanEntitlements(
    max_members=2,
    advanced_roles=False,
    advanced_inventory_history=False,
    advanced_reports=False,
    advanced_exports=False,
    monthly_owner_report=False,
    priority_support=False,
    executive_dashboard=False,
    recipes=False,
)
PRO_ENTITLEMENTS = PlanEntitlements(
    max_members=5,
    advanced_roles=True,
    advanced_inventory_history=True,
    advanced_reports=True,
    advanced_exports=True,
    monthly_owner_report=True,
    priority_support=True,
    executive_dashboard=True,
    recipes=False,
)
RESTAURANT_ENTITLEMENTS = PlanEntitlements(
    max_members=5,
    advanced_roles=True,
    advanced_inventory_history=True,
    advanced_reports=False,
    advanced_exports=False,
    monthly_owner_report=False,
    priority_support=False,
    executive_dashboard=False,
    recipes=True,
)
# Existing customers keep every capability they already had, but monthly email
# is not silently enabled because it is a new outbound communication.
GRANDFATHERED_ENTITLEMENTS = PlanEntitlements(
    max_members=5,
    advanced_roles=True,
    advanced_inventory_history=True,
    advanced_reports=True,
    advanced_exports=True,
    monthly_owner_report=False,
    priority_support=True,
    executive_dashboard=False,
    recipes=False,
)

DAILY_CAPABILITIES = frozenset(
    {
        "sell",
        "inventory",
        "tickets",
        "customers",
        "cash_register",
        "receivables",
        "alerts",
        "quick_load",
        "essential_reports",
        "basic_export",
        "basic_inventory_history",
        "inventory_adjustments",
    }
)


def normalize_plan_code(value: Any, default: str = STARTER) -> str:
    code = str(value or "").strip().upper()
    return code if code in KNOWN_PLAN_CODES else default


def entitlements_for(plan_code: str) -> PlanEntitlements:
    code = normalize_plan_code(plan_code)
    if code in {PRO, MANUAL}:
        return PRO_ENTITLEMENTS
    if code == GRANDFATHERED:
        return GRANDFATHERED_ENTITLEMENTS
    if code == RESTAURANT:
        return RESTAURANT_ENTITLEMENTS
    return STARTER_ENTITLEMENTS


def capabilities_for(plan_code: str) -> frozenset[str]:
    entitlements = entitlements_for(plan_code)
    capabilities = set(DAILY_CAPABILITIES)
    if entitlements.advanced_roles:
        capabilities.add("team_managers")
    if entitlements.advanced_inventory_history:
        capabilities.add("inventory_history")
    if entitlements.advanced_reports:
        capabilities.add("advanced_reports")
    if entitlements.advanced_exports:
        capabilities.add("advanced_exports")
    if entitlements.monthly_owner_report:
        capabilities.add("monthly_owner_report")
    if entitlements.priority_support:
        capabilities.add("priority_support")
    if entitlements.executive_dashboard:
        capabilities.add("executive_dashboard")
    if entitlements.recipes:
        capabilities.update({"recipes", "recipe_costing", "ingredient_depletion"})
    return frozenset(capabilities)


def stripe_subscription_access_is_active(user, *, now=None, grace_days=3) -> bool:
    if not user:
        return False
    if not getattr(user, "stripe_subscription_id", None):
        return False
    now = now or datetime.utcnow()
    status = str(getattr(user, "subscription_status", "") or "").lower()
    period_end = getattr(user, "current_period_end", None)
    if status in {"active", "trialing"}:
        return bool(period_end and period_end >= now)
    if status == "past_due" and period_end:
        return period_end + timedelta(days=grace_days) >= now
    return False


def subscription_access_is_active(user, *, now=None, grace_days=3) -> bool:
    if not user:
        return False
    if stripe_subscription_access_is_active(
        user, now=now, grace_days=grace_days
    ):
        return True
    return bool(getattr(user, "manual_pro_access", False))


def current_plan_code(
    user,
    *,
    has_paid_access: bool | None = None,
    now=None,
    grace_days=3,
) -> str:
    """Resolve the effective commercial plan without silently downgrading."""
    if not user:
        return TRIAL
    stripe_access = (
        str(getattr(user, "subscription_status", "") or "").lower()
        in {"active", "trialing"}
        and stripe_subscription_access_is_active(
            user, now=now, grace_days=grace_days
        )
    )
    if stripe_access:
        stored = normalize_plan_code(
            getattr(user, "subscription_plan_code", None),
            default=GRANDFATHERED,
        )
        if stored in {STARTER, PRO, RESTAURANT}:
            return stored
        if bool(getattr(user, "manual_pro_access", False)):
            return MANUAL
        if stored == GRANDFATHERED:
            return GRANDFATHERED
        return GRANDFATHERED
    if bool(getattr(user, "manual_pro_access", False)):
        return MANUAL
    paid_access = (
        subscription_access_is_active(
            user, now=now, grace_days=grace_days
        )
        if has_paid_access is None
        else bool(has_paid_access)
    )
    if paid_access:
        stored = normalize_plan_code(
            getattr(user, "subscription_plan_code", None),
            default=GRANDFATHERED,
        )
        if stored in {STARTER, PRO, RESTAURANT, GRANDFATHERED}:
            return stored
        return GRANDFATHERED
    stored = normalize_plan_code(
        getattr(user, "subscription_plan_code", None),
        default="",
    )
    if (
        stored in PAID_PLAN_CODES
        and getattr(user, "stripe_subscription_id", None)
        and not str(getattr(user, "subscription_status", "") or "").strip()
    ):
        return stored
    return TRIAL


def trial_plan_code(user) -> str:
    requested = normalize_plan_code(
        getattr(user, "trial_plan_code", None), default=STARTER
    )
    return requested if requested in PAID_PLAN_CODES else STARTER


def entitlement_plan_code(
    user,
    *,
    has_paid_access: bool | None = None,
    now=None,
    grace_days=3,
) -> str:
    effective = current_plan_code(
        user,
        has_paid_access=has_paid_access,
        now=now,
        grace_days=grace_days,
    )
    return trial_plan_code(user) if effective == TRIAL else effective


def has_entitlement(
    user,
    entitlement: str,
    *,
    has_paid_access: bool | None = None,
    now=None,
    grace_days=3,
) -> bool:
    values = entitlements_for(
        entitlement_plan_code(
            user,
            has_paid_access=has_paid_access,
            now=now,
            grace_days=grace_days,
        )
    )
    if not hasattr(values, entitlement):
        raise KeyError(f"Unknown PATIA entitlement: {entitlement}")
    return bool(getattr(values, entitlement))


def current_plan_label(plan_code: str) -> str:
    labels = {
        TRIAL: gettext("Prueba gratuita"),
        STARTER: gettext("Starter"),
        PRO: gettext("Pro"),
        RESTAURANT: gettext("Restaurant"),
        GRANDFATHERED: gettext("Plan actual protegido"),
        MANUAL: gettext("Acceso manual"),
    }
    return labels.get(plan_code, gettext("Plan actual"))


PLAN_PRICES_MXN = {STARTER: 199, PRO: 349, RESTAURANT: 360}


def plan_price(plan_code: str) -> int | None:
    return PLAN_PRICES_MXN.get(normalize_plan_code(plan_code))


def price_id_for(config, plan_code: str) -> str | None:
    code = normalize_plan_code(plan_code)
    if code == STARTER:
        return config.get("STRIPE_STARTER_PRICE_ID") or config.get(
            "STRIPE_PRICE_ID"
        )
    if code == PRO:
        return config.get("STRIPE_PRO_PRICE_ID")
    if code == RESTAURANT:
        return config.get("STRIPE_RESTAURANT_PRICE_ID")
    return None


def configured_price_plan(config, price_id: str | None) -> str | None:
    if not price_id:
        return None
    if price_id_for(config, PRO) == price_id:
        return PRO
    if price_id_for(config, RESTAURANT) == price_id:
        return RESTAURANT
    if price_id_for(config, STARTER) == price_id:
        return STARTER
    return None


def commercial_plans(config) -> list[dict]:
    """Return translated presentation from the same central definitions."""
    definitions = (
        {
            "code": STARTER,
            "name": gettext("Starter"),
            "price": 199,
            "audience": gettext(
                "Negocios pequeños manejados por el propietario y un cajero."
            ),
            "description": gettext(
                "Todo lo necesario para vender y controlar un negocio pequeño."
            ),
            "features": (
                gettext("Propietario y un cajero"),
                gettext("Ventas, inventario y tickets"),
                gettext("Clientes, caja y saldos pendientes"),
                gettext("Alertas y reportes esenciales"),
                gettext("Productos y ventas sin límites artificiales"),
            ),
        },
        {
            "code": PRO,
            "name": gettext("Pro"),
            "price": 349,
            "audience": gettext(
                "Negocios con varias personas que necesitan más control."
            ),
            "description": gettext(
                "Más control para negocios que trabajan con varias personas."
            ),
            "features": (
                gettext("Todo lo incluido en Starter"),
                gettext("Hasta cinco personas"),
                gettext("Encargados y permisos avanzados"),
                gettext("Historial y reportes avanzados"),
                gettext("Reporte mensual enviado al propietario"),
                gettext("Soporte prioritario"),
            ),
        },
        {
            "code": RESTAURANT,
            "name": gettext("Restaurant"),
            "price": PLAN_PRICES_MXN[RESTAURANT],
            "audience": gettext("Control especializado para restaurantes."),
            "description": gettext(
                "Para restaurantes que quieren controlar recetas, ingredientes y el costo real de cada platillo."
            ),
            "features": (
                gettext("Todo lo necesario para operar"),
                gettext("Recetas e ingredientes"),
                gettext("Costeo real por platillo"),
                gettext("Kg, g, L, ml y piezas"),
                gettext("Descuento automático de inventario"),
                gettext("Disponibilidad de platillos"),
            ),
        },
    )
    plans = []
    for definition in definitions:
        price_id = price_id_for(config, definition["code"])
        plans.append(
            {
                **definition,
                "max_members": entitlements_for(
                    definition["code"]
                ).max_members,
                "configured": bool(price_id),
                "checkout_enabled": bool(price_id),
            }
        )
    return plans
