import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import sqlalchemy as sa

from tests.migration_safety import safe_temporary_database_url


ROOT = Path(__file__).resolve().parents[1]


class ControlledAiMigrationTests(unittest.TestCase):
    def test_clean_database_upgrades_to_controlled_ai_revision(self):
        with tempfile.TemporaryDirectory(prefix="patia-ai-migration-") as temp:
            database_path = Path(temp, "explicit-temporary-ai.db")
            environment = os.environ.copy()
            environment.update(
                DATABASE_URL=safe_temporary_database_url(database_path),
                SECRET_KEY="controlled-ai-migration-test",
                STRIPE_DISABLED="1",
                PUBLIC_BASE_URL="http://127.0.0.1:5000",
                FLASK_DEBUG="0",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "flask",
                    "--app",
                    "run.py",
                    "db",
                    "upgrade",
                    "20260730_23",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(
                result.returncode,
                0,
                result.stdout + result.stderr,
            )
            engine = sa.create_engine(
                f"sqlite:///{database_path.as_posix()}"
            )
            try:
                inspector = sa.inspect(engine)
                self.assertIn("ai_narrative_run", inspector.get_table_names())
                columns = {
                    item["name"]
                    for item in inspector.get_columns("ai_narrative_run")
                }
                self.assertTrue(
                    {
                        "organization_id",
                        "feature_name",
                        "language",
                        "data_hash",
                        "model",
                        "status",
                        "input_tokens",
                        "output_tokens",
                        "estimated_cost_microusd",
                        "latency_ms",
                        "error_code",
                    }.issubset(columns)
                )
                with engine.connect() as connection:
                    revision = connection.execute(
                        sa.text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                    integrity = connection.execute(
                        sa.text("PRAGMA integrity_check")
                    ).scalar_one()
                self.assertEqual(revision, "20260730_23")
                self.assertEqual(integrity, "ok")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
