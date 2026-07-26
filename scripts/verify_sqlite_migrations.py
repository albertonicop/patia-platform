"""Run Alembic only against a guarded, disposable SQLite database.

This command exists so local release validation never depends on a manually
assembled DATABASE_URL and can never fall back to instance/tiendaia.db.
Production keeps using Render's normal pre-deploy command.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.migration_safety import safe_temporary_database_url


def validation_environment(database_path: Path) -> dict[str, str]:
    """Build an isolated environment after enforcing the migration guard."""
    database_url = safe_temporary_database_url(database_path)
    env = os.environ.copy()
    env.update(
        DATABASE_URL=database_url,
        SECRET_KEY=secrets.token_hex(32),
        STRIPE_DISABLED="1",
        PUBLIC_BASE_URL="http://127.0.0.1:5000",
        WTF_CSRF_ENABLED="0",
    )
    return env


def inspect_database(database_path: Path) -> tuple[str, str]:
    connection = sqlite3.connect(
        f"file:{database_path.resolve().as_posix()}?mode=ro",
        uri=True,
    )
    try:
        integrity = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
    finally:
        connection.close()
    return integrity, revision


def run_validation(database_path: Path) -> tuple[str, str]:
    env = validation_environment(database_path)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "flask",
            "--app",
            "run.py",
            "db",
            "upgrade",
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )
    return inspect_database(database_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate all migrations using disposable SQLite."
    )
    parser.add_argument(
        "--database",
        type=Path,
        help=(
            "Optional guarded SQLite path. It must be inside a patia-* "
            "directory under the system temp directory."
        ),
    )
    args = parser.parse_args(argv)

    if args.database:
        database_path = args.database.resolve()
        integrity, revision = run_validation(database_path)
        print(f"TEMP_DATABASE={database_path}")
        print(f"INTEGRITY={integrity}")
        print(f"ALEMBIC={revision}")
        return 0

    with tempfile.TemporaryDirectory(
        prefix="patia-safe-migration-"
    ) as temporary_root:
        database_path = Path(temporary_root, "fresh.db")
        integrity, revision = run_validation(database_path)
        print(f"TEMP_DATABASE={database_path}")
        print(f"INTEGRITY={integrity}")
        print(f"ALEMBIC={revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
