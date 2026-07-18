"""Single-profile writes cannot clobber another Account.id namespace."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routes.prefs_routes as prefs_routes
from core.database import Account, Base


@pytest.fixture()
def prefs_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'prefs-isolation.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    db = factory()
    db.add_all([
        Account(id=str(uuid.uuid4()), username="alice", status="active"),
        Account(id=str(uuid.uuid4()), username="bob", status="active"),
    ])
    db.commit()
    db.close()
    monkeypatch.setattr(prefs_routes, "SessionLocal", factory)
    yield
    engine.dispose()


def test_named_profile_save_preserves_other_profile(prefs_db):
    prefs_routes._save_for_user("alice", {"theme": "light"})
    prefs_routes._save_for_user("bob", {"theme": "paper"})
    prefs_routes._save_for_user("alice", {"theme": "dark"})

    assert prefs_routes._load_for_user("alice") == {"theme": "dark"}
    assert prefs_routes._load_for_user("bob") == {"theme": "paper"}


def test_bulk_snapshot_updates_profiles_without_cross_clobber(prefs_db):
    prefs_routes._save({
        "_users": {
            "alice": {"theme": "light"},
            "bob": {"theme": "paper"},
        },
    })
    prefs_routes._save_for_user("alice", {"theme": "dark"})

    assert prefs_routes._load_for_user("bob") == {"theme": "paper"}
