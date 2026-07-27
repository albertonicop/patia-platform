from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_babel import gettext

from app import db
from app.money import money_decimal
from app.plans import has_entitlement
from app.routes import current_organization_owner, current_user
from app.team.services import (
    active_membership,
    has_permission,
    require_permission,
)

from .services import build_executive_dashboard


pro = Blueprint("pro", __name__, url_prefix="/pro")


def _pro_access():
    user = current_user()
    membership = active_membership(user) if user else None
    owner = current_organization_owner(user)
    if not owner or not has_entitlement(owner, "executive_dashboard"):
        flash(
            gettext(
                "El Dashboard Ejecutivo está incluido en PATIA Pro. "
                "Compara los planes para activarlo."
            ),
            "info",
        )
        return None, None, redirect(url_for("main.subscribe"))
    return user, membership, None


@pro.route("", methods=["GET"], strict_slashes=False)
@require_permission("view_reports")
def dashboard():
    user, membership, blocked = _pro_access()
    if blocked:
        return blocked
    data = build_executive_dashboard(
        membership.organization,
        request.args,
    )
    if data["executive_period"]["error"]:
        flash(data["executive_period"]["error"], "warning")
    return render_template(
        "pro_dashboard.html",
        user=user,
        organization=membership.organization,
        can_edit_goal=has_permission(membership, "manage_subscription"),
        **data,
    )


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
        return redirect(url_for("pro.dashboard"))
    membership.organization.monthly_sales_goal = goal
    db.session.commit()
    if goal is None:
        flash(gettext("Meta mensual eliminada."), "success")
    else:
        flash(gettext("Meta mensual guardada."), "success")
    return redirect(url_for("pro.dashboard"))
