"""Preference replacement is transactional and non-destructive across owners."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routes.prefs_routes as prefs_routes
from core.database import Account, Base


@pytest.fixture()
def prefs_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'prefs-atomic.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    db = factory()
    db.add(Account(id=str(uuid.uuid4()), username="alice", status="active"))
    db.commit()
    db.close()
    monkeypatch.setattr(prefs_routes, "SessionLocal", factory)
    yield
    engine.dispose()


def test_replace_removes_only_stale_keys_for_same_profile(prefs_db):
    prefs_routes._save_for_user("alice", {"theme": "light", "layout": "wide"})
    prefs_routes._save_for_user("alice", {"theme": "dark"})

    assert prefs_routes._load_for_user("alice") == {"theme": "dark"}


def test_invalid_replacement_rolls_back_partial_changes(prefs_db):
    prefs_routes._save_for_user("alice", {"theme": "light"})

    with pytest.raises(Exception):
        prefs_routes._save_for_user("alice", {
            "theme": "dark",
            "invalid key with spaces": True,
        })

    assert prefs_routes._load_for_user("alice") == {"theme": "light"}
