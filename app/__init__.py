import os
import secrets
from pathlib import Path

import sqlalchemy as sa
from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix


db = SQLAlchemy()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=[])


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _add_missing_columns() -> None:
    """
    Migración temporal para instalaciones existentes.

    No borra tablas ni datos. Solo agrega columnas antiguas que todavía
    pudieran faltar. Más adelante lo reemplazaremos por Flask-Migrate.
    """
    inspector = sa.inspect(db.engine)
    table_names = set(inspector.get_table_names())

    missing_columns = {
        "user": {
            "trial_warning_sent": "BOOLEAN DEFAULT FALSE",
            "rfc": "VARCHAR(20)",
            "tax_regime": "VARCHAR(120)",
        },
        "sale": {
            "ticket_id": "VARCHAR(36)",
        },
    }

    with db.engine.begin() as connection:
        for table_name, expected_columns in missing_columns.items():
            if table_name not in table_names:
                continue

            existing_columns = {
                column["name"]
                for column in inspector.get_columns(table_name)
            }

            for column_name, column_type in expected_columns.items():
                if column_name in existing_columns:
                    continue

                connection.execute(
                    sa.text(
                        f'ALTER TABLE "{table_name}" '
                        f'ADD COLUMN "{column_name}" {column_type}'
                    )
                )


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
        STRIPE_SECRET_KEY=os.environ.get("STRIPE_SECRET_KEY"),
        STRIPE_PRICE_ID=os.environ.get("STRIPE_PRICE_ID"),
        STRIPE_WEBHOOK_SECRET=os.environ.get("STRIPE_WEBHOOK_SECRET"),
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
    csrf.init_app(app)
    limiter.init_app(app)

    from .routes import main

    app.register_blueprint(main)

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

    with app.app_context():
        # Crea únicamente las tablas que todavía no existen.
        # NUNCA borra información.
        db.create_all()
        _add_missing_columns()

    return app


def request_is_secure() -> bool:
    from flask import request

    return request.is_secure
