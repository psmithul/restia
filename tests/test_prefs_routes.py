"""Compatibility preference helpers use canonical Account.id-owned SQL."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routes.prefs_routes as prefs_routes
from core.database import Account, Base


@pytest.fixture()
def prefs_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'prefs.db'}")
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
    yield factory
    engine.dispose()


def test_missing_profile_preferences_are_empty(prefs_db):
    assert prefs_routes._load_for_user("alice") == {}
    assert prefs_routes._load_for_user("bob") == {}


def test_profile_preferences_round_trip_without_cross_owner_leak(prefs_db):
    prefs_routes._save_for_user("alice", {"theme": "dark"})

    assert prefs_routes._load_for_user("alice") == {"theme": "dark"}
    assert prefs_routes._load_for_user("bob") == {}
    assert prefs_routes._load() == {
        "_users": {"alice": {"theme": "dark"}, "bob": {}},
    }
