"""Ejecuta la verificación RC solo contra una base PostgreSQL desechable."""

import os
import secrets
import subprocess
import sys
from urllib.parse import urlparse


def main():
    database_url = os.environ.get("PATIA_TEST_DATABASE_URL", "")
    parsed = urlparse(database_url)
    database_name = parsed.path.lstrip("/")
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise SystemExit("PATIA_TEST_DATABASE_URL debe ser PostgreSQL.")
    if not database_name.endswith("_test"):
        raise SystemExit("La base desechable debe terminar en _test.")

    env = os.environ.copy()
    env.update(
        DATABASE_URL=database_url,
        SECRET_KEY=secrets.token_hex(32),
        STRIPE_DISABLED="1",
        PUBLIC_BASE_URL="http://127.0.0.1:5000",
        WTF_CSRF_ENABLED="0",
    )
    commands = (
        [sys.executable, "-m", "flask", "--app", "run.py", "db", "upgrade"],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
    )
    for command in commands:
        subprocess.run(command, env=env, check=True)


if __name__ == "__main__":
    main()
