"""Owner presentation preferences; never a source of roles or entitlements."""
from datetime import datetime

from flask import Blueprint, flash, redirect, request, url_for
from flask_babel import gettext
from sqlalchemy.exc import IntegrityError

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


def eligible_for_update(user, membership):
    from .plans import subscription_access_is_active

    return bool(user and membership and membership.role == "OWNER"
                and owner_preference(user, membership) is None
                and (user.manual_pro_access or user.subscription_status != "trialing")
                and subscription_access_is_active(user))


def claim_update_notice(user, membership):
    if not eligible_for_update(user, membership):
        return False
    if db.session.get(OwnerWorkUpdateNotice, user.id):
        return False
    # The primary key also prevents two devices displaying the notice concurrently.
    try:
        with db.session.begin_nested():
            db.session.add(OwnerWorkUpdateNotice(user_id=user.id))
            db.session.flush()
        db.session.commit()
    except IntegrityError:
        return False
    return True


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
    return dict(work_mode=mode, work_mode_label=labels.get(mode), show_work_setup=mode == "pending",
                owner_navigation=prioritized_navigation(membership, mode), platform_admin=is_platform_admin(user),
                show_work_update=(request.method == "GET" and request.endpoint == "main.dashboard"
                                  and claim_update_notice(user, membership)))


@work_style.post("/settings/work-style/update")
@require_roles("OWNER")
def acknowledge_update():
    from .routes import current_user

    user = current_user()
    action = request.form.get("action")
    if action not in {"personalize", "later"}:
        return redirect(url_for("main.dashboard")), 303
    membership = active_membership(user)
    if eligible_for_update(user, membership):
        claim_update_notice(user, membership)
        notice = db.session.get(OwnerWorkUpdateNotice, user.id)
        notice.action = action
        db.session.commit()
    destination = "work_style.setup" if action == "personalize" else "main.dashboard"
    return redirect(url_for(destination)), 303


@work_style.get("/settings/work-style/setup")
@require_roles("OWNER")
def setup():
    from flask import render_template

    return render_template("work_style_setup.html")


@work_style.post("/settings/work-style")
@require_roles("OWNER")
def save():
    from .routes import current_user

    user = current_user()
    membership = active_membership(user)
    destination = "main.dashboard" if request.form.get("source") == "onboarding" else "main.settings"
    style = request.form.get("work_style")
    if request.form.get("action") == "defer" or style == "deferred":
        mode = "deferred"
    elif style == "solo":
        mode = "solo"
    elif style == "team" and request.form.get("owner_activity") in {"operations", "supervision"}:
        mode = "team_" + request.form["owner_activity"]
    else:
        flash(gettext("Selecciona cómo trabajas y, si tienes equipo, cómo participas."), "warning")
        return redirect(url_for(destination)), 303
    preference = owner_preference(user, membership)
    if preference is None:
        preference = OwnerWorkPreference(user_id=user.id)
        db.session.add(preference)
    preference.mode = mode
    db.session.commit()
    flash(gettext("Forma de trabajo guardada. Tus permisos y tu plan siguen iguales."), "success")
    if request.form.get("action") == "team" and mode in {"team_operations", "team_supervision"}:
        return redirect(url_for("team.index")), 303
    return redirect(url_for(destination, _anchor="work-style" if destination == "main.settings" else None)), 303
