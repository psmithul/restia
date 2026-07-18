"""Settings and features are merged from canonical SQL rows, not JSON files."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as database
from core.database import Account, Base
from src import settings
from src.profile_configuration_models import ProfileConfiguration  # noqa: F401


@pytest.fixture()
def settings_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'settings-shape.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    db = factory()
    db.add(Account(id=str(uuid.uuid4()), username="alice", status="active"))
    db.commit()
    db.close()
    monkeypatch.setattr(database, "SessionLocal", factory)
    settings._invalidate_caches()
    yield
    settings._invalidate_caches()
    engine.dispose()


def test_unknown_profile_loads_complete_defaults(settings_db):
    assert settings.load_settings(owner="missing") == settings.DEFAULT_SETTINGS
    assert settings.load_features(owner="missing") == settings.DEFAULT_FEATURES


def test_sql_overrides_merge_without_materializing_defaults(settings_db):
    settings.save_settings({"tts_voice": "nova"}, owner="alice")
    settings.save_features({"gallery": False}, owner="alice")

    loaded = settings.load_settings(owner="alice")
    assert loaded["tts_voice"] == "nova"
    assert all(key in loaded for key in settings.DEFAULT_SETTINGS)
    assert settings.load_features(owner="alice")["gallery"] is False
    assert settings.is_setting_overridden("tts_voice", owner="alice") is True
    assert settings.is_setting_overridden("tts_speed", owner="alice") is False
