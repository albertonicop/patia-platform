"""Hard safety boundary for automated Alembic tests.

Production migrations keep using the normal Render command. Test helpers must
use this module before spawning Alembic so a typo can never target the local
development database.
"""

from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]
LOCAL_DATABASE = (ROOT / "instance" / "tiendaia.db").resolve()
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


def safe_temporary_database_url(database_path) -> str:
    candidate = Path(database_path).resolve()
    if candidate == LOCAL_DATABASE:
        raise RuntimeError(
            "Migration safety refused instance/tiendaia.db."
        )
    try:
        relative = candidate.relative_to(TEMP_ROOT)
    except ValueError as exc:
        raise RuntimeError(
            "Migration tests require a database inside the system temp directory."
        ) from exc
    if candidate.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise RuntimeError(
            "Migration tests require an identifiable temporary SQLite file."
        )
    if not any(part.lower().startswith("patia-") for part in relative.parts):
        raise RuntimeError(
            "Migration tests require a patia-* temporary directory."
        )
    return f"sqlite:///{candidate.as_posix()}"
