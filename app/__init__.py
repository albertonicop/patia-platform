import os
import secrets
from pathlib import Path

import sqlalchemy as sa
from flask import Flask
from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


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
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("RENDER") is not None,
    )

    db.init_app(app)

    from .routes import main

    app.register_blueprint(main)

    with app.app_context():
        # Crea únicamente las tablas que todavía no existen.
        # NUNCA borra información.
        db.create_all()
        _add_missing_columns()

    return app