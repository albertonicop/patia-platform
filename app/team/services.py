from __future__ import annotations

from functools import wraps

from flask import (
    abort,
    flash,
    g,
    has_request_context,
    jsonify,
    redirect,
    request,
    session,
    url_for,
)
from flask_babel import gettext
from app import db
from app.models import Organization, OrganizationMember, User


ROLE_PERMISSIONS = {
    "OWNER": frozenset(
        {
            "view_dashboard",
            "use_pos",
            "view_costs",
            "edit_prices",
            "apply_discounts",
            "cancel_sales",
            "process_returns",
            "manage_inventory",
            "make_inventory_adjustments",
            "view_reports",
            "manage_customers",
            "lookup_customers",
            "create_customers",
            "manage_customer_credit",
            "use_customer_credit",
            "receive_credit_payments",
            "authorize_credit_override",
            "grant_credit",
            "manage_employees",
            "manage_subscription",
            "operate_cash_register",
            "manage_cash_movements",
            "view_cash_history",
            "view_inventory_history",
            "manage_recipes",
        }
    ),
    "MANAGER": frozenset(
        {
            "view_dashboard",
            "use_pos",
            "view_costs",
            "edit_prices",
            "apply_discounts",
            "cancel_sales",
            "process_returns",
            "manage_inventory",
            "make_inventory_adjustments",
            "view_reports",
            "manage_customers",
            "lookup_customers",
            "create_customers",
            "manage_customer_credit",
            "use_customer_credit",
            "receive_credit_payments",
            "authorize_credit_override",
            "grant_credit",
            "operate_cash_register",
            "manage_cash_movements",
            "view_cash_history",
            "view_inventory_history",
            "manage_recipes",
        }
    ),
    "CASHIER": frozenset(
        {
            "use_pos",
            "lookup_customers",
            "create_customers",
            "use_customer_credit",
            "receive_credit_payments",
            "operate_cash_register",
        }
    ),
}

ACCESS_EXEMPT_PERMISSIONS = frozenset({"manage_subscription"})


def ensure_owner_organization(user: User) -> OrganizationMember:
    """Create the compatibility organization for a user when migrations are absent.

    Production data is backfilled by Alembic. This runtime path keeps tests,
    development databases created with ``db.create_all`` and newly registered
    owners consistent without deriving tenant access from request parameters.
    """
    membership = (
        OrganizationMember.query.filter_by(user_id=user.id, role="OWNER")
        .order_by(OrganizationMember.id)
        .first()
    )
    if membership:
        return membership

    organization = Organization(
        name=user.company_name,
        slug=f"org-{user.id}",
        owner_user_id=user.id,
        timezone=user.timezone or "America/Mexico_City",
        currency="MXN",
        country_code="MX",
        currency_code="MXN",
        locale_code="es_MX",
        business_type=(
            user.business_type if user.business_type in {"general", "restaurant"}
            else "general"
        ),
    )
    membership = OrganizationMember(
        organization=organization,
        user_id=user.id,
        role="OWNER",
        is_active=True,
    )
    db.session.add_all((organization, membership))
    db.session.flush()
    return membership


def active_membership(user: User) -> OrganizationMember | None:
    requested_id = session.get("organization_id") if has_request_context() else None
    cache_key = (user.id, requested_id)
    if has_request_context():
        cache = getattr(g, "_active_membership_cache", {})
        if cache_key in cache:
            return cache[cache_key]

    query = OrganizationMember.query.filter_by(user_id=user.id, is_active=True)
    if requested_id is not None:
        membership = query.filter_by(organization_id=requested_id).first()
        if membership and membership.organization.is_active:
            cache[cache_key] = membership
            g._active_membership_cache = cache
            return membership

    membership = query.order_by(OrganizationMember.id).first()
    if membership and membership.organization.is_active:
        if has_request_context():
            session["organization_id"] = membership.organization_id
            cache[cache_key] = membership
            cache[(user.id, membership.organization_id)] = membership
            g._active_membership_cache = cache
        return membership
    if has_request_context():
        cache[cache_key] = None
        g._active_membership_cache = cache
    return None


def membership_for_login(user: User) -> OrganizationMember | None:
    """Return an active membership without provisioning deactivated employees.

    Only genuinely legacy users with no membership rows receive a compatibility
    owner organization. A former employee whose memberships were disabled must
    never gain access by silently becoming the owner of a new tenant.
    """
    membership = active_membership(user)
    if membership:
        return membership
    if OrganizationMember.query.filter_by(user_id=user.id).first() is not None:
        return None
    return ensure_owner_organization(user)


def has_permission(membership: OrganizationMember | None, permission: str) -> bool:
    if not membership or not membership.is_active:
        return False
    return permission in ROLE_PERMISSIONS.get(membership.role, frozenset())


def organization_owner(user: User) -> User | None:
    membership = active_membership(user)
    if not membership:
        return None
    return db.session.get(User, membership.organization.owner_user_id)


def authentication_required_response():
    """Recover cleanly when a login or organization session is no longer valid."""
    disabled = session.pop("membership_disabled", False)
    revoked = session.pop("kicked_out", False)
    expired = session.pop("session_expired", False)
    if disabled:
        message = gettext(
            "Tu acceso a esta empresa fue desactivado. Contacta al propietario."
        )
        reason = "membership_disabled"
    elif revoked:
        message = gettext(
            "Tu sesión terminó porque se inició otra sesión con tu cuenta."
        )
        reason = "session_revoked"
    elif expired:
        message = gettext("Tu sesión expiró. Inicia sesión para continuar.")
        reason = "session_expired"
    else:
        message = gettext("Inicia sesión para continuar.")
        reason = "authentication_required"
    if request.is_json:
        return jsonify({"ok": False, "error": message, "error_code": reason}), 401
    flash(message, "warning")
    next_url = request.full_path.rstrip("?")
    return redirect(url_for("main.login", next=next_url))


def _operational_access_response(user, permission):
    """Apply the organization's single Trial/Pro policy to protected features."""
    if permission in ACCESS_EXEMPT_PERMISSIONS:
        return None
    from app.routes import _trial_access_response

    return _trial_access_response(user, json_response=request.is_json)


def require_permission(permission: str, *, allow_expired: bool = False):
    """Authorize against the active tenant, never against a submitted ID."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            from app.routes import current_user

            user = current_user()
            membership = active_membership(user) if user else None
            if not user:
                return authentication_required_response()
            if not has_permission(membership, permission):
                if request.is_json:
                    return jsonify({"ok": False, "error": gettext("Acceso no permitido.")}), 403
                abort(403)
            if not allow_expired:
                access_block = _operational_access_response(user, permission)
                if access_block:
                    return access_block
            return view(*args, **kwargs)

        return wrapped

    return decorator


def require_roles(*roles: str):
    allowed = frozenset(roles)

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            from app.routes import current_user

            user = current_user()
            membership = active_membership(user) if user else None
            if not user:
                return authentication_required_response()
            if not membership or membership.role not in allowed:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator
