"""Durable task persistence (Phase 4E). Not wired into create_app in 4E.1."""

from app.persistence.migrate import MigrationError, apply_migrations
from app.persistence.sqlite import SqliteTaskRepository

__all__ = [
    "MigrationError",
    "SqliteTaskRepository",
    "apply_migrations",
]
