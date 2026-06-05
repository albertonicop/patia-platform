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
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///tiendaia.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    from .routes import main
    app.register_blueprint(main)

    with app.app_context():
        db.create_all()

    return app
