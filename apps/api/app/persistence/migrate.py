"""Versioned SQLite migrations for task persistence (Phase 4E.1)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d+)_([a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    """Raised when migrations cannot be applied cleanly."""


def _migration_files() -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    if not MIGRATIONS_DIR.is_dir():
        return found
    for path in sorted(MIGRATIONS_DIR.iterdir()):
        if not path.is_file():
            continue
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            continue
        found.append((int(match.group(1)), path))
    versions = [version for version, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(
            f"Migration versions must be contiguous from 1; found {versions}"
        )
    return found


def _split_sql_statements(script: str) -> list[str]:
    """Split a simple SQL script into statements (no procedure bodies)."""
    statements: list[str] = []
    for chunk in script.split(";"):
        # Drop full-line SQL comments.
        lines = [
            line
            for line in chunk.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        text = "\n".join(lines).strip()
        if text:
            statements.append(text)
    return statements


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return set()
    return {
        int(r[0])
        for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order. Returns newly applied versions.

    Each migration's DDL/DML and its ``schema_migrations`` row run in one
    transaction. Failure rolls back that migration; the version is not recorded.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    already = applied_versions(conn)
    applied_now: list[int] = []
    for version, path in _migration_files():
        if version in already:
            continue
        statements = _split_sql_statements(path.read_text(encoding="utf-8"))
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (?, datetime('now'))",
                (version,),
            )
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise MigrationError(
                f"Migration {version} ({path.name}) failed: {exc}"
            ) from exc
        if version not in applied_versions(conn):
            raise MigrationError(
                f"Migration {version} did not record a schema_migrations row"
            )
        applied_now.append(version)
    return applied_now
