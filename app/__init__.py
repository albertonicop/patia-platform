import os
import secrets
from datetime import datetime
from pathlib import Path

import click
from flask import Flask, jsonify, render_template, request, session
from flask_babel import Babel, gettext
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix


db = SQLAlchemy()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=[])
migrate = Migrate()
babel = Babel()

SUPPORTED_LANGUAGES = ("es", "en")


def select_locale():
    selected = session.get("language", "es")
    return selected if selected in SUPPORTED_LANGUAGES else "es"


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_app():
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        instance_relative_config=True,
    )

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    # En Render esta variable debe existir.
    # Localmente se genera una clave temporal si todavía no configuraste .env.
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        if os.environ.get("RENDER"):
            raise RuntimeError(
                "Falta configurar SECRET_KEY en las variables de entorno de Render."
            )

        secret_key = secrets.token_hex(32)

    database_url = os.environ.get("DATABASE_URL", "sqlite:///tiendaia.db")
    stripe_disabled = _env_flag("STRIPE_DISABLED", default=False)
    public_base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    stripe_config = {
        "STRIPE_SECRET_KEY": os.environ.get("STRIPE_SECRET_KEY"),
        "STRIPE_PRICE_ID": os.environ.get("STRIPE_PRICE_ID"),
        "STRIPE_STARTER_PRICE_ID": os.environ.get("STRIPE_STARTER_PRICE_ID"),
        "STRIPE_PRO_PRICE_ID": os.environ.get("STRIPE_PRO_PRICE_ID"),
        "STRIPE_WEBHOOK_SECRET": os.environ.get("STRIPE_WEBHOOK_SECRET"),
    }
    if not stripe_disabled:
        missing = [
            name
            for name in (
                "STRIPE_SECRET_KEY",
                "STRIPE_PRICE_ID",
                "STRIPE_WEBHOOK_SECRET",
            )
            if not stripe_config[name]
        ]
        if missing:
            raise RuntimeError(
                "Falta configurar Stripe: " + ", ".join(sorted(missing))
            )
        if not public_base_url:
            raise RuntimeError("Falta configurar PUBLIC_BASE_URL para Stripe.")

    # Compatibilidad con URLs antiguas de PostgreSQL.
    if database_url.startswith("postgres://"):
        database_url = database_url.replace(
            "postgres://",
            "postgresql://",
            1,
        )

    app.config.update(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=database_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        **stripe_config,
        STRIPE_DISABLED=stripe_disabled,
        PUBLIC_BASE_URL=public_base_url,
        STRIPE_PAST_DUE_GRACE_DAYS=int(
            os.environ.get("STRIPE_PAST_DUE_GRACE_DAYS", "3")
        ),
        RESEND_API_KEY=os.environ.get("RESEND_API_KEY"),
        RESEND_FROM=os.environ.get("RESEND_FROM"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=os.environ.get("SESSION_COOKIE_SAMESITE", "Lax"),
        SESSION_COOKIE_SECURE=_env_flag(
            "SESSION_COOKIE_SECURE",
            default=os.environ.get("RENDER") is not None,
        ),
        WTF_CSRF_TIME_LIMIT=3600,
        RATELIMIT_STORAGE_URI=os.environ.get(
            "RATELIMIT_STORAGE_URI",
            "memory://",
        ),
        BABEL_DEFAULT_LOCALE="es",
        BABEL_SUPPORTED_LOCALES=SUPPORTED_LANGUAGES,
        BABEL_TRANSLATION_DIRECTORIES="translations",
    )

    trusted_proxy_hops = int(
        os.environ.get(
            "TRUSTED_PROXY_HOPS",
            "1" if os.environ.get("RENDER") else "0",
        )
    )
    if trusted_proxy_hops:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=trusted_proxy_hops,
            x_proto=trusted_proxy_hops,
        )
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)
    babel.init_app(app, locale_selector=select_locale)

    from .routes import current_user, has_pro_access, main

    app.register_blueprint(main)
    from .team.routes import team

    app.register_blueprint(team)
    from .cash.routes import cash

    app.register_blueprint(cash)
    from .inventory.routes import inventory

    app.register_blueprint(inventory)
    from .customers.routes import customers

    app.register_blueprint(customers)
    from .credit.routes import credit

    app.register_blueprint(credit)

    @app.context_processor
    def inject_pro_access():
        user = current_user()
        from .team.services import active_membership, has_permission

        current_membership = active_membership(user) if user else None
        access_user = (
            current_membership.organization.owner
            if current_membership
            else user
        )
        trial_days_left = None
        if access_user and access_user.created_at:
            trial_days_left = max(0, 14 - (datetime.utcnow() - access_user.created_at).days)
        return {
            "has_pro_access": has_pro_access(access_user),
            "trial_days_left": trial_days_left,
            "supported_languages": SUPPORTED_LANGUAGES,
            "current_language": select_locale(),
            "current_membership": current_membership,
            "can_manage_team": has_permission(current_membership, "manage_employees"),
            "can_manage_inventory": has_permission(current_membership, "manage_inventory"),
            "can_view_reports": has_permission(current_membership, "view_reports"),
            "can_manage_subscription": has_permission(current_membership, "manage_subscription"),
            "can_view_dashboard": has_permission(current_membership, "view_dashboard"),
            "can_use_pos": has_permission(current_membership, "use_pos"),
            "can_operate_cash_register": has_permission(
                current_membership, "operate_cash_register"
            ),
            "can_view_inventory_history": has_permission(
                current_membership, "view_inventory_history"
            ),
            "can_manage_customers": has_permission(
                current_membership, "manage_customers"
            ),
            "can_lookup_customers": has_permission(
                current_membership, "lookup_customers"
            ),
            "can_manage_credit": has_permission(
                current_membership, "manage_customer_credit"
            ),
            "can_authorize_credit_override": has_permission(
                current_membership, "authorize_credit_override"
            ),
        }

    @app.cli.command("audit-manual-pro-candidates")
    def audit_manual_pro_candidates():
        """Lista usuarios Pro históricos que requieren revisión manual."""
        from .models import User

        candidates = User.query.filter(
            User.plan == "pro",
            User.manual_pro_access.is_(False),
            User.stripe_subscription_id.is_(None),
        ).order_by(User.id).all()
        click.echo("user_id,email,company_name")
        for candidate in candidates:
            click.echo(
                f"{candidate.id},{candidate.email},{candidate.company_name}"
            )
        click.echo(f"total={len(candidates)}")

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "; ".join(
            (
                "default-src 'self'",
                "base-uri 'self'",
                "frame-ancestors 'none'",
                "form-action 'self' https://checkout.stripe.com https://billing.stripe.com",
                "object-src 'none'",
                "img-src 'self' data: https://patiaapp.com",
                "font-src 'self' https://cdnjs.cloudflare.com data:",
                "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com",
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
                "connect-src 'self'",
            )
        )
        if request_is_secure():
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    error_messages = {
        400: ("No pudimos procesar la solicitud", "Revisa la información e inténtalo nuevamente."),
        403: ("Acceso no permitido", "Tu cuenta no tiene permiso para abrir esta página."),
        404: ("Página no encontrada", "La dirección puede haber cambiado o ya no estar disponible."),
        429: ("Demasiados intentos", "Espera unos minutos antes de volver a intentarlo."),
        500: ("PATIA no pudo completar la operación", "Tus datos siguen protegidos. Inténtalo nuevamente en unos minutos."),
    }

    def render_error(status_code):
        title, message = error_messages[status_code]
        return render_template(
            "error.html",
            status_code=status_code,
            error_title=gettext(title),
            error_message=gettext(message),
        ), status_code

    def render_client_error(error, status_code):
        if status_code == 429 and request.is_json:
            limiter_response = error.get_response()
            if limiter_response and limiter_response.is_json:
                return limiter_response
            return jsonify(
                {
                    "ok": False,
                    "error": gettext("Demasiados intentos"),
                    "error_code": "rate_limited",
                }
            ), 429
        return render_error(status_code)

    for status_code in (400, 403, 404, 429):
        app.register_error_handler(
            status_code,
            lambda error, code=status_code: render_client_error(error, code),
        )

    @app.errorhandler(500)
    def internal_server_error(error):
        db.session.rollback()
        return render_error(500)

    return app


def request_is_secure() -> bool:
    from flask import request

    return request.is_secure
