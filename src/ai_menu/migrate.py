"""Apply ordered, immutable SQLite migrations."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import sqlite3

from .paths import PROJECT_ROOT


def migrate(database):
    database = Path(database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        migrations = sorted((PROJECT_ROOT / "migrations").glob("[0-9]*.sql"))
        if not migrations:
            raise RuntimeError("No migration files found")
        if conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            backup = sqlite3.connect(str(database) + "." + stamp + ".bak")
            try:
                conn.backup(backup)
            finally:
                backup.close()
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migration'").fetchone()
            legacy = not existing and conn.execute("PRAGMA user_version").fetchone()[0] == 1
            if legacy:
                reference = sqlite3.connect(":memory:")
                try:
                    reference.executescript(migrations[0].read_text())
                    for (table,) in reference.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                        expected = reference.execute(f'PRAGMA table_info("{table}")').fetchall()
                        actual = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                        if expected != actual:
                            raise RuntimeError("Legacy schema mismatch: " + table)
                finally:
                    reference.close()
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migration(name TEXT PRIMARY KEY, checksum TEXT NOT NULL)")
            if legacy:
                conn.execute("INSERT INTO schema_migration VALUES (?,?)", (
                    migrations[0].name, sha256(migrations[0].read_bytes()).hexdigest()))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        for path in migrations:
            checksum = sha256(path.read_bytes()).hexdigest()
            conn.execute("BEGIN IMMEDIATE")
            try:
                saved = conn.execute("SELECT checksum FROM schema_migration WHERE name=?", (path.name,)).fetchone()
                if saved:
                    if saved[0] != checksum:
                        raise RuntimeError("Applied migration was modified: " + path.name)
                else:
                    statement = ""
                    for line in path.read_text().splitlines(keepends=True):
                        statement += line
                        if sqlite3.complete_statement(statement):
                            conn.execute(statement)
                            statement = ""
                    if statement.strip():
                        raise RuntimeError("Incomplete migration SQL")
                    conn.execute("INSERT INTO schema_migration VALUES (?,?)", (path.name, checksum))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    finally:
        conn.close()


if __name__ == "__main__":
    import os
    from .paths import DEFAULT_DATABASE
    migrate(os.environ.get("MENU_DATABASE", DEFAULT_DATABASE))
