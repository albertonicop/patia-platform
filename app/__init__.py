import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from pathlib import Path


db = SQLAlchemy()


def create_app():
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        instance_relative_config=True
    )

    Path(app.instance_path).mkdir(exist_ok=True)
    app.config["SECRET_KEY"] = "change-this-secret-key-before-production"
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///tiendaia.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["STRIPE_SECRET_KEY"] = os.environ.get("STRIPE_SECRET_KEY")
    app.config["STRIPE_PRICE_ID"] = os.environ.get("STRIPE_PRICE_ID")
    app.config["STRIPE_WEBHOOK_SECRET"] = os.environ.get("STRIPE_WEBHOOK_SECRET")
    app.config["RESEND_API_KEY"] = os.environ.get("RESEND_API_KEY", "re_Xgm7h1DR_B7e1zkdPh2snGFD5AZz4gT3V")
    app.config["RESEND_FROM"] = os.environ.get("RESEND_FROM")

    db.init_app(app)

    from .routes import main
    app.register_blueprint(main)

    with app.app_context():
        db.create_all()

    return app
