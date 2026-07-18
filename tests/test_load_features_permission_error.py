"""The pre-SQL cache reset convention remains safe during the cutover."""

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as database
from core.database import Account, Base
from src import settings
from src.profile_configuration_models import ProfileConfiguration  # noqa: F401


def test_load_features_recovers_from_legacy_none_cache(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'features-cache.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    db = factory()
    db.add(Account(id=str(uuid.uuid4()), username="alice", status="active"))
    db.commit()
    db.close()
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(settings, "_features_cache", None)

    assert settings.load_features(owner="alice") == dict(settings.DEFAULT_FEATURES)
    engine.dispose()
