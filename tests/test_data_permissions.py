"""The local Restia data directory and SQLite sidecars are private."""

import os

import pytest

import core.database as database


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_harden_database_permissions_repairs_directory_and_sqlite_files(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    db = data / "app.db"
    wal = data / "app.db-wal"
    shm = data / "app.db-shm"
    for path in (db, wal, shm):
        path.write_bytes(b"")
        path.chmod(0o644)

    monkeypatch.setattr(database, "DATA_DIR", str(data))
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{db}")
    database.harden_database_permissions()

    assert data.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (db, wal, shm))
