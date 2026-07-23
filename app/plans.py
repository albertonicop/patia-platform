"""Commercial plan presentation and centralized product capabilities.

Existing paid and manual accounts remain grandfathered. This module does not
change Stripe state or revoke capabilities; it centralizes the vocabulary so
future enforcement cannot be scattered through templates.
"""

from __future__ import annotations

from flask_babel import gettext


STARTER = "STARTER"
PRO = "PRO"
TRIAL = "TRIAL"
GRANDFATHERED = "GRANDFATHERED"


_STARTER_CAPABILITIES = frozenset(
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
    }
)
_PRO_CAPABILITIES = _STARTER_CAPABILITIES | {
    "team_managers",
    "inventory_history",
    "advanced_adjustments",
    "advanced_reports",
    "priority_support",
}


def capabilities_for(plan_code: str) -> frozenset[str]:
    """Return the centralized capability set without mutating access."""
    if plan_code in {PRO, GRANDFATHERED}:
        return _PRO_CAPABILITIES
    return _STARTER_CAPABILITIES


def current_plan_code(user, *, has_paid_access: bool) -> str:
    """Preserve every existing paid/manual customer without silent downgrade."""
    if user and (
        user.manual_pro_access
        or user.stripe_subscription_id
        or has_paid_access
    ):
        return GRANDFATHERED
    return TRIAL


def current_plan_label(plan_code: str) -> str:
    labels = {
        TRIAL: gettext("Prueba gratuita"),
        STARTER: gettext("Starter"),
        PRO: gettext("Pro"),
        GRANDFATHERED: gettext("Plan actual protegido"),
    }
    return labels.get(plan_code, gettext("Plan actual"))


def commercial_plans(config) -> list[dict]:
    """Plans shown in UI; existing STRIPE_PRICE_ID remains Starter-compatible."""
    starter_configured = bool(
        config.get("STRIPE_STARTER_PRICE_ID")
        or config.get("STRIPE_PRICE_ID")
    )
    pro_configured = bool(config.get("STRIPE_PRO_PRICE_ID"))
    return [
        {
            "code": STARTER,
            "name": gettext("Starter"),
            "price": 199,
            "description": gettext(
                "Todo lo necesario para vender y controlar un negocio pequeño."
            ),
            "features": [
                gettext("Propietario y un cajero"),
                gettext("Ventas, inventario y tickets"),
                gettext("Clientes, caja y saldos pendientes"),
                gettext("Alertas, carga rápida y reportes esenciales"),
                gettext("Productos y ventas sin límites artificiales"),
            ],
            "configured": starter_configured,
            "checkout_enabled": starter_configured,
        },
        {
            "code": PRO,
            "name": gettext("Pro"),
            "price": 349,
            "description": gettext(
                "Más control para negocios que trabajan con un equipo."
            ),
            "features": [
                gettext("Todo lo incluido en Starter"),
                gettext("Hasta cinco personas"),
                gettext("Encargados y permisos"),
                gettext("Historial completo de inventario"),
                gettext("Reportes avanzados y soporte prioritario"),
            ],
            "configured": pro_configured,
            # Pro checkout requires a deliberate Stripe plan-change integration.
            "checkout_enabled": False,
        },
    ]
