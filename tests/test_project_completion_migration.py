"""Upgrade coverage for the reversible Projects completion state."""

import sqlite3

import pytest

import core.database as database


def test_existing_projects_table_gets_completion_column_and_index(monkeypatch, tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE projects (
                id VARCHAR(36) PRIMARY KEY,
                owner VARCHAR(255) NOT NULL,
                key VARCHAR(12) NOT NULL,
                name VARCHAR(160) NOT NULL,
                archived BOOLEAN NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            "INSERT INTO projects (id, owner, key, name, archived) VALUES (?, ?, ?, ?, ?)",
            ("project-1", "alice", "OLD", "Legacy project", 0),
        )

    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{path}")
    database._migrate_add_project_completion_column()
    database._migrate_add_project_completion_column()  # idempotent restart

    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(projects)")]
        indexes = [row[1] for row in connection.execute("PRAGMA index_list(projects)")]
        row = connection.execute(
            "SELECT id, completed_at FROM projects WHERE id = 'project-1'"
        ).fetchone()

    assert columns.count("completed_at") == 1
    assert "ix_projects_completed_at" in indexes
    assert row == ("project-1", None)


def test_project_completion_migration_failure_stops_startup(monkeypatch, tmp_path):
    path = tmp_path / "broken.db"
    path.touch()

    class BrokenConnection:
        rolled_back = False
        closed = False

        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    connection = BrokenConnection()
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: connection)

    with pytest.raises(RuntimeError, match="projects.completed_at migration failed") as exc:
        database._migrate_add_project_completion_column()

    assert isinstance(exc.value.__cause__, sqlite3.OperationalError)
    assert connection.rolled_back is True
    assert connection.closed is True
