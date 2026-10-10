"""Owner presentation preferences; never a source of roles or entitlements."""
from datetime import datetime

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_babel import gettext
from sqlalchemy.exc import SQLAlchemyError

from . import db
from .team.services import active_membership, has_permission, require_roles


work_style = Blueprint("work_style", __name__)
MODES = ("pending", "deferred", "solo", "team_operations", "team_supervision")


class OwnerWorkPreference(db.Model):
    __tablename__ = "owner_work_preference"
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    mode = db.Column(db.String(32), nullable=False, default="deferred")
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (db.CheckConstraint(
        "mode IN ('pending','deferred','solo','team_operations','team_supervision')",
        name="ck_owner_work_preference_mode",
    ),)


class OwnerWorkUpdateNotice(db.Model):
    """Independent of preferences: seeing the update never changes the menu."""
    __tablename__ = "owner_work_update_notice"
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    seen_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    action = db.Column(db.String(16), nullable=False, default="seen")
    __table_args__ = (db.CheckConstraint(
        "action IN ('seen','personalize','later')", name="ck_owner_work_update_notice_action"),)


COMPLETED_MODES = frozenset(MODES[2:])
PUBLIC_ENDPOINTS = frozenset({
    "static", "main.login", "main.register", "main.logout", "main.logout_get",
    "main.verify_email", "main.resend_verification", "main.change_verification_email",
    "main.forgot_password", "main.reset_password", "main.set_language", "main.set_display_currency",
    "main.terms", "main.privacy", "main.stripe_webhook", "main.stripe_success", "team.accept",
})


def setup_required(user, membership):
    if not user or not membership or membership.role != "OWNER":
        return False
    preference = owner_preference(user, membership)
    return not preference or preference.mode not in COMPLETED_MODES


@work_style.before_app_request
def require_owner_setup():
    # Only authenticated business requests; CLI jobs and public callbacks are untouched.
    if (not request.endpoint or request.endpoint in PUBLIC_ENDPOINTS
            or request.endpoint.startswith("work_style.") or not session.get("user_id")):
        return None
    from .routes import current_user

    user = current_user()
    membership = active_membership(user) if user else None
    # Checkout's own email-verification check precedes personalization, including
    # legacy unverified accounts without a pending registration code.
    if user and not user.email_verified and request.endpoint in {"main.subscribe", "main.create_checkout_session"}:
        return None
    if setup_required(user, membership):
        destination = url_for("work_style.setup")
        if request.is_json or request.accept_mimetypes.best == "application/json":
            return jsonify(ok=False, error_code="work_style_required",
                           error=gettext("Completa tu forma de trabajo para continuar."), setup_url=destination), 409
        return redirect(destination), 303
    return None


def initialize_new_owner(user):
    """Called only by registration, never by legacy-owner compatibility login."""
    db.session.add(OwnerWorkPreference(user_id=user.id, mode="pending"))


def is_platform_admin(user):
    # Preserve the platform's existing allowlist, independently of business roles.
    return bool(user and user.email_verified and user.email == "albertonicopat@gmail.com")


def owner_preference(user, membership):
    if not user or not membership or membership.role != "OWNER":
        return None
    return db.session.get(OwnerWorkPreference, user.id)


def prioritized_navigation(membership, mode):
    if not membership or membership.role != "OWNER" or mode not in MODES[2:]:
        return []
    from .plans import has_entitlement

    recipes = (membership.organization.business_type == "restaurant"
               and has_entitlement(membership.organization.owner, "recipes"))
    # Same tools and permission checks as the existing sidebar; only regrouped.
    definitions = [
        ("home", "Inicio", "/", "house", "view_dashboard", "Operación", ("/",)),
        ("pos", "Vender", "/sell", "cash-register", "use_pos", "Operación", ("/sell", "/sales/", "/ticket/", "/receipt/")),
        ("inventory", "Inventario", "/products", "boxes-stacked", "manage_inventory", "Operación", ("/products", "/inventory/kardex")),
        ("recipes", "Recetas", "/recipes", "utensils", "manage_recipes", "Operación", ("/recipes",)),
        ("cash", "Caja del día", "/cash-register", "vault", "operate_cash_register", "Operación", ("/cash-register",)),
        ("customers", "Clientes", "/customers", "address-book", "manage_customers", "Operación", ("/customers",)),
        ("credit", "Saldos pendientes", "/credit", "hand-holding-dollar", "manage_customer_credit", "Operación", ("/credit",)),
        ("suppliers", "Proveedores", "/suppliers", "truck", "manage_inventory", "Operación", ("/suppliers",)),
        ("reports", "Reportes", "/reports", "chart-line", "view_reports", "Análisis", ("/reports",)),
        ("decisions", "Centro de decisiones", "/pro/hub", "bolt", "view_reports", "Análisis", ("/pro/hub",)),
        ("purchases", "Compras inteligentes", "/pro/purchases", "truck-ramp-box", "manage_inventory", "Análisis", ("/pro/purchases",)),
        ("settings", "Negocio", "/settings", "store", "manage_subscription", "Configuración", ("/settings",)),
        ("team", "Personal", "/team", "users", "manage_employees", "Configuración", ("/team",)),
        ("subscription", "Suscripción", "/subscription", "credit-card", "manage_subscription", "Configuración", ("/subscription", "/subscribe")),
    ]
    entries = []
    for key, label, href, icon, permission, group, prefixes in definitions:
        if not has_permission(membership, permission) or (key == "recipes" and not recipes):
            continue
        active = request.path == "/" if key == "home" else any(request.path.startswith(p) for p in prefixes)
        entries.append(dict(key=key, label=gettext(label), href=href, icon=icon, group=group, active=active,
                            pro=key in {"decisions", "purchases"}))
    priorities = ("home", "reports", "decisions") if mode == "team_supervision" else ("home", "pos", "inventory", "cash")
    favorite = [entry for key in priorities for entry in entries if entry["key"] == key]
    groups = [dict(label=gettext("Tu día"), entries=favorite)]
    for label in ("Operación", "Análisis", "Configuración"):
        remaining = [e for e in entries if e["group"] == label and e["key"] not in priorities]
        if remaining:
            groups.append(dict(label=gettext(label), entries=remaining))
    return groups


@work_style.app_context_processor
def presentation_context():
    from .routes import current_user

    user = current_user()
    membership = active_membership(user) if user else None
    preference = owner_preference(user, membership)
    mode = preference.mode if preference else "deferred"
    labels = {"solo": gettext("Trabajo solo"), "team_operations": gettext("Equipo · también atiendo y cobro"),
              "team_supervision": gettext("Equipo · principalmente superviso")}
    return dict(work_mode=mode, work_mode_label=labels.get(mode),
                work_setup_required=setup_required(user, membership),
                owner_navigation=prioritized_navigation(membership, mode), platform_admin=is_platform_admin(user))


@work_style.post("/settings/work-style/update")
@require_roles("OWNER")
def acknowledge_update():
    # Retired notice links cannot postpone the now-required setup.
    return redirect(url_for("work_style.setup")), 303


@work_style.get("/settings/work-style/setup")
@require_roles("OWNER")
def setup():
    return render_template("work_style_setup.html")


@work_style.post("/settings/work-style")
@require_roles("OWNER")
def save():
    from .routes import current_user

    user = current_user()
    membership = active_membership(user)
    destination = "main.dashboard" if request.form.get("source") == "onboarding" else "main.settings"
    style = request.form.get("work_style")
    activity = request.form.get("owner_activity")
    if style == "solo" and request.form.get("action") not in {"defer", "later", "skip"}:
        mode = "solo"
    elif style == "team" and activity in {"operations", "supervision"} and request.form.get("action") not in {"defer", "later", "skip"}:
        mode = "team_" + activity
    else:
        return render_template("work_style_setup.html", selected_style=style, selected_activity=activity,
                               work_save_error=gettext("Selecciona cómo trabajas y, si tienes equipo, cómo participas.")), 422
    try:
        preference = owner_preference(user, membership)
        if preference is None:
            preference = OwnerWorkPreference(user_id=user.id)
            db.session.add(preference)
        preference.mode = mode
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception("Could not persist owner work preference")
        return render_template("work_style_setup.html", selected_style=style, selected_activity=activity,
                               work_save_error=gettext("No pudimos guardar tu forma de trabajo. Tu selección se conserva; vuelve a intentarlo.")), 503
    flash(gettext("Forma de trabajo guardada. Tus permisos y tu plan siguen iguales."), "success")
    return redirect(url_for(destination, _anchor="work-style" if destination == "main.settings" else None)), 303
