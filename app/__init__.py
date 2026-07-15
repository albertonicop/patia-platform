import os
import secrets
from pathlib import Path

import click
from flask import Flask
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
        "STRIPE_WEBHOOK_SECRET": os.environ.get("STRIPE_WEBHOOK_SECRET"),
    }
    if not stripe_disabled:
        missing = [name for name, value in stripe_config.items() if not value]
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

    from .routes import current_user, has_pro_access, main

    app.register_blueprint(main)

    @app.context_processor
    def inject_pro_access():
        user = current_user()
        return {"has_pro_access": has_pro_access(user)}

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
                "form-action 'self'",
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

    return app


def request_is_secure() -> bool:
    from flask import request

    return request.is_secure
