from __future__ import annotations

import sqlite3
from pathlib import Path


def run_migrations(db_path: str | Path, migrations_dir: str | Path) -> None:
    """Apply any unapplied SQL migrations in numeric order.

    Called at application startup before any other database access.
    Forward-only: mistakes are fixed by new migration files, never by editing old ones.
    """
    db_path = Path(db_path)
    migrations_dir = Path(migrations_dir)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version     INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        conn.commit()

        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] if row[0] is not None else 0

        for migration_file in sorted(migrations_dir.glob("*.sql")):
            version = int(migration_file.stem.split("_")[0])
            if version <= current:
                continue
            description = migration_file.stem[5:]  # strip leading "NNNN_"
            conn.executescript(migration_file.read_text())
            conn.execute(
                "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                (version, description),
            )
            conn.commit()
    finally:
        conn.close()
