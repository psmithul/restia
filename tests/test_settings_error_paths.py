"""Transactional error paths for canonical SQL settings adapters."""

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
    engine = create_engine(f"sqlite:///{tmp_path / 'settings-errors.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    db = factory()
    db.add_all([
        Account(id=str(uuid.uuid4()), username="alice", status="active"),
        Account(id=str(uuid.uuid4()), username="bob", status="active"),
    ])
    db.commit()
    db.close()
    monkeypatch.setattr(database, "SessionLocal", factory)
    settings._invalidate_caches()
    yield
    settings._invalidate_caches()
    engine.dispose()


def test_non_object_settings_are_rejected(settings_db):
    with pytest.raises(ValueError, match="object"):
        settings.save_settings([])


def test_wrong_setting_type_rolls_back(settings_db):
    settings.save_settings({"tts_voice": "nova"}, owner="alice")

    with pytest.raises(ValueError, match="true or false"):
        settings.save_settings({
            "tts_voice": "echo",
            "vision_enabled": "yes",
        }, owner="alice")

    settings._invalidate_caches()
    assert settings.load_settings(owner="alice")["tts_voice"] == "nova"


def test_cached_results_are_defensive_copies(settings_db):
    first = settings.load_settings(owner="alice")
    first["tts_voice"] = "mutated-outside-adapter"

    assert settings.load_settings(owner="alice")["tts_voice"] == settings.DEFAULT_SETTINGS["tts_voice"]


def test_setting_overrides_are_owner_isolated(settings_db):
    settings.save_settings({"tts_voice": "nova"}, owner="alice")

    assert settings.load_settings(owner="alice")["tts_voice"] == "nova"
    assert settings.load_settings(owner="bob")["tts_voice"] == settings.DEFAULT_SETTINGS["tts_voice"]


def test_deployment_keys_are_not_accepted_by_canonical_service(settings_db):
    from src.identity import find_account
    from src.profile_configuration_service import ProfileConfigurationError, put_configuration

    db = database.SessionLocal()
    try:
        account = find_account(db, "alice")
        with pytest.raises(ProfileConfigurationError, match="deployment"):
            put_configuration(
                db,
                account=account,
                namespace="preference",
                key="database_url",
                value="postgresql://private",
            )
    finally:
        db.rollback()
        db.close()
