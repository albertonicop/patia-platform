"""Safety checks for scripts that create disposable database schemas."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url


class UnsafeDatabaseTarget(RuntimeError):
    """Raised before a script can create tables in a persistent database."""


def _resolved_sqlite_path(app, database: str) -> Path:
    path = Path(database)
    if not path.is_absolute():
        path = Path(app.instance_path) / path
    return path.resolve()


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def assert_safe_ephemeral_database(app) -> Path | None:
    """Require an explicit in-memory or operating-system temporary SQLite DB.

    This guard is intentionally called only by ``db.create_all()`` and manual
    data-loading scripts. Normal application startup and Alembic migrations do
    not use it.
    """

    raw_database_url = os.environ.get("DATABASE_URL")
    if raw_database_url is None or not raw_database_url.strip():
        raise UnsafeDatabaseTarget(
            "DATABASE_URL debe definirse explícitamente antes de crear tablas "
            "de prueba."
        )

    try:
        environment_url = make_url(raw_database_url.strip())
        configured_url = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UnsafeDatabaseTarget(
            "DATABASE_URL no contiene una URL de base temporal válida."
        ) from exc

    if not environment_url.drivername.startswith("sqlite"):
        raise UnsafeDatabaseTarget(
            "create_all() solo está permitido para una SQLite temporal "
            "explícita."
        )
    if not configured_url.drivername.startswith("sqlite"):
        raise UnsafeDatabaseTarget(
            "La aplicación no está configurada con una SQLite temporal."
        )

    environment_database = environment_url.database
    configured_database = configured_url.database
    if environment_database == ":memory:" and configured_database == ":memory:":
        return None
    if not environment_database or not configured_database:
        raise UnsafeDatabaseTarget(
            "DATABASE_URL debe identificar una SQLite temporal concreta."
        )

    if not Path(environment_database).is_absolute():
        raise UnsafeDatabaseTarget(
            "DATABASE_URL debe usar una ruta temporal absoluta; las rutas "
            "relativas son ambiguas."
        )
    if not Path(configured_database).is_absolute():
        raise UnsafeDatabaseTarget(
            "La ruta SQLite configurada debe ser temporal y absoluta."
        )

    environment_path = _resolved_sqlite_path(app, environment_database)
    configured_path = _resolved_sqlite_path(app, configured_database)
    persistent_path = (Path(app.instance_path) / "tiendaia.db").resolve()
    if environment_path == persistent_path or configured_path == persistent_path:
        raise UnsafeDatabaseTarget(
            "Operación cancelada: create_all() no puede ejecutarse contra "
            "instance/tiendaia.db."
        )
    if environment_path != configured_path:
        raise UnsafeDatabaseTarget(
            "DATABASE_URL no coincide con la base configurada por la "
            "aplicación."
        )

    temporary_root = Path(tempfile.gettempdir()).resolve()
    if not _is_within(configured_path, temporary_root):
        raise UnsafeDatabaseTarget(
            "La base para create_all() debe estar dentro del directorio "
            "temporal del sistema."
        )
    return configured_path
