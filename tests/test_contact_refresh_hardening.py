from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import (
    ActionAudit,
    Base,
    ContactDelivery,
    ContactRecord,
    ContactSource,
)
from src import carddav_contacts as carddav
from src.audit_context import bind_service_audit_context
from src.contact_delivery import drain_contact_deliveries
from src.contact_service import (
    ContactConflict,
    ContactNotFound,
    create_contact,
    delete_contact,
    list_contacts,
    refresh_contact_source,
    refresh_contact_source_detached,
    resolve_contact_conflict_detached,
    search_contacts,
    update_contact,
    upsert_carddav_config,
)
from src.identity import ensure_account


@pytest.fixture()
def refresh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(
        carddav, "validate_carddav_url", lambda value: str(value).rstrip("/")
    )
    engine = create_engine(
        f"sqlite:///{tmp_path / 'refresh.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    account = ensure_account(db, "alice")
    db.commit()
    db.close()
    try:
        yield SimpleNamespace(engine=engine, Session=factory, account=account)
    finally:
        engine.dispose()


def _configured_source(env, *, url="https://dav.example/addressbook"):
    db = env.Session()
    try:
        source = upsert_carddav_config(
            db,
            owner_id=env.account.id,
            url=url,
            username="private-user",
            password="private-password",
        )
        db.commit()
        return source
    finally:
        db.close()


def test_refresh_has_no_open_transaction_or_sqlite_writer_lock_during_fetch(
    refresh_env, monkeypatch,
):
    source = _configured_source(refresh_env)
    caller = refresh_env.Session()
    observed = {}

    def fetch(_config):
        observed["caller_transaction"] = caller.in_transaction()
        writer = refresh_env.Session()
        try:
            writer.execute(text(
                "UPDATE accounts SET updated_at=updated_at WHERE id=:owner_id"
            ), {"owner_id": refresh_env.account.id})
            writer.commit()
            observed["writer_committed"] = True
        finally:
            writer.close()
        return [{
            "uid": "detached-refresh",
            "name": "Detached Refresh",
            "emails": [],
            "phones": [],
            "address": "",
        }]

    monkeypatch.setattr(carddav, "fetch_contacts", fetch)
    try:
        rows = refresh_contact_source(
            caller,
            owner_id=refresh_env.account.id,
            source_id=source.id,
            raise_errors=True,
        )
    finally:
        caller.close()

    assert observed == {
        "caller_transaction": False,
        "writer_committed": True,
    }
    assert [row["name"] for row in rows] == ["Detached Refresh"]


def test_refresh_config_fence_discards_an_inflight_old_generation(
    refresh_env, monkeypatch,
):
    source = _configured_source(refresh_env, url="https://old.example/book")

    def fetch(_config):
        writer = refresh_env.Session()
        try:
            current = writer.query(ContactSource).filter_by(id=source.id).one()
            upsert_carddav_config(
                writer,
                owner_id=refresh_env.account.id,
                url="https://new.example/book",
                username="new-user",
                password="new-password",
                expected_version=int(current.config_version),
            )
            writer.commit()
        finally:
            writer.close()
        return [{
            "uid": "must-not-apply",
            "name": "Old source response",
            "emails": [],
            "phones": [],
            "address": "",
        }]

    monkeypatch.setattr(carddav, "fetch_contacts", fetch)
    with pytest.raises(ContactConflict, match="changed while CardDAV"):
        refresh_contact_source_detached(
            refresh_env.Session,
            owner_id=refresh_env.account.id,
            source_id=source.id,
            raise_errors=True,
        )

    db = refresh_env.Session()
    try:
        assert db.query(ContactRecord).count() == 0
        current = db.query(ContactSource).filter_by(id=source.id).one()
        assert current.base_url == "https://new.example/book"
        assert current.sync_state == "idle"
    finally:
        db.close()


def test_refresh_source_version_fence_preserves_concurrent_source_state(
    refresh_env, monkeypatch,
):
    source = _configured_source(refresh_env)

    def fetch(_config):
        writer = refresh_env.Session()
        try:
            current = writer.query(ContactSource).filter_by(id=source.id).one()
            updated = writer.query(ContactSource).filter(
                ContactSource.id == source.id,
                ContactSource.owner_id == refresh_env.account.id,
                ContactSource.version == int(current.version),
            ).update({
                ContactSource.sync_state: "error",
                ContactSource.last_error: "Concurrent delivery state",
                ContactSource.version: int(current.version) + 1,
            }, synchronize_session=False)
            assert updated == 1
            writer.commit()
        finally:
            writer.close()
        return [{
            "uid": "stale-source-state",
            "name": "Must Not Apply",
            "emails": [],
            "phones": [],
            "address": "",
        }]

    monkeypatch.setattr(carddav, "fetch_contacts", fetch)
    with pytest.raises(ContactConflict, match="source changed"):
        refresh_contact_source_detached(
            refresh_env.Session,
            owner_id=refresh_env.account.id,
            source_id=source.id,
            raise_errors=True,
        )

    db = refresh_env.Session()
    try:
        current = db.query(ContactSource).filter_by(id=source.id).one()
        assert current.sync_state == "error"
        assert current.last_error == "Concurrent delivery state"
        assert db.query(ContactRecord).count() == 0
    finally:
        db.close()


def test_refresh_claim_suppresses_overlapping_network_fetch(
    refresh_env, monkeypatch,
):
    source = _configured_source(refresh_env)
    calls = 0

    def fetch(_config):
        nonlocal calls
        calls += 1
        nested = refresh_contact_source_detached(
            refresh_env.Session,
            owner_id=refresh_env.account.id,
            source_id=source.id,
            raise_errors=True,
        )
        assert nested == []
        return [{
            "uid": "single-fetch",
            "name": "Single Fetch",
            "emails": [],
            "phones": [],
            "address": "",
        }]

    monkeypatch.setattr(carddav, "fetch_contacts", fetch)
    result = refresh_contact_source_detached(
        refresh_env.Session,
        owner_id=refresh_env.account.id,
        source_id=source.id,
        raise_errors=True,
    )
    assert calls == 1
    assert result[0]["name"] == "Single Fetch"


def test_refresh_audit_keeps_caller_attribution_without_contact_pii(
    refresh_env, monkeypatch,
):
    source = _configured_source(refresh_env)
    monkeypatch.setattr(carddav, "fetch_contacts", lambda _config: [{
        "uid": "private-refresh-uid",
        "name": "Private Refresh Person",
        "emails": ["private-refresh@example.test"],
        "phones": ["+15550001111"],
        "address": "Private Refresh Address",
    }])
    caller = refresh_env.Session()
    try:
        bind_service_audit_context(
            caller,
            account_id=refresh_env.account.id,
            interface="internal_tool",
            actor_type="agent_tool",
            credential_type="internal",
        )
        refresh_contact_source(
            caller,
            owner_id=refresh_env.account.id,
            source_id=source.id,
            raise_errors=True,
        )
    finally:
        caller.close()

    db = refresh_env.Session()
    try:
        audit = db.query(ActionAudit).filter_by(action="contacts.refreshed").one()
        assert audit.owner_id == refresh_env.account.id
        assert audit.details["audit"]["interface"] == "internal_tool"
        assert audit.details["audit"]["actor_type"] == "agent_tool"
        rendered = json.dumps({
            "before": audit.before_state,
            "after": audit.after_state,
            "details": audit.details,
        })
        for private in (
            "private-refresh-uid",
            "Private Refresh Person",
            "private-refresh@example.test",
            "+15550001111",
            "Private Refresh Address",
            "private-user",
            "private-password",
        ):
            assert private not in rendered
    finally:
        db.close()


def test_owner_reads_keep_local_and_disabled_carddav_snapshots_visible(
    refresh_env, monkeypatch,
):
    db = refresh_env.Session()
    try:
        local = create_contact(
            db,
            owner_id=refresh_env.account.id,
            name="Retained Local",
            email="retained-local@example.test",
        )
        db.commit()
    finally:
        db.close()

    source = _configured_source(refresh_env)
    monkeypatch.setattr(carddav, "fetch_contacts", lambda _config: [{
        "uid": "retained-remote",
        "name": "Retained Remote",
        "emails": ["retained-remote@example.test"],
        "phones": [],
        "address": "",
    }])
    refresh_contact_source_detached(
        refresh_env.Session,
        owner_id=refresh_env.account.id,
        source_id=source.id,
        raise_errors=True,
    )

    db = refresh_env.Session()
    try:
        configured_rows = list_contacts(
            db, owner_id=refresh_env.account.id, refresh=False,
        )
        assert {row["name"] for row in configured_rows} == {
            "Retained Local", "Retained Remote",
        }
        assert local["source_id"] in {
            row["source_id"] for row in configured_rows
        }
        assert [row["name"] for row in search_contacts(
            db, owner_id=refresh_env.account.id, query="retained-local",
        )] == ["Retained Local"]

        current = db.query(ContactSource).filter_by(id=source.id).one()
        upsert_carddav_config(
            db,
            owner_id=refresh_env.account.id,
            url="",
            expected_version=int(current.config_version),
        )
        db.commit()
        disabled_rows = list_contacts(
            db, owner_id=refresh_env.account.id, refresh=False,
        )
        assert {row["name"] for row in disabled_rows} == {
            "Retained Local", "Retained Remote",
        }
        disabled_remote = next(
            row for row in disabled_rows if row["source_id"] == source.id
        )
        with pytest.raises(ContactConflict, match="disabled"):
            update_contact(
                db,
                owner_id=refresh_env.account.id,
                uid=disabled_remote["uid"],
                source_id=source.id,
                name="Must Re-enable First",
                emails=[],
                phones=[],
                expected_version=disabled_remote["version"],
            )
    finally:
        db.close()


def test_ambiguous_uid_requires_owner_validated_source_id(
    refresh_env, monkeypatch,
):
    db = refresh_env.Session()
    try:
        local = create_contact(
            db,
            owner_id=refresh_env.account.id,
            name="Local Duplicate UID",
            email="local-duplicate@example.test",
        )
        db.commit()
    finally:
        db.close()
    source = _configured_source(refresh_env)
    monkeypatch.setattr(carddav, "fetch_contacts", lambda _config: [{
        "uid": local["uid"],
        "name": "Remote Duplicate UID",
        "emails": ["remote-duplicate@example.test"],
        "phones": [],
        "address": "",
    }])
    refresh_contact_source_detached(
        refresh_env.Session,
        owner_id=refresh_env.account.id,
        source_id=source.id,
        raise_errors=True,
    )

    db = refresh_env.Session()
    try:
        rows = list_contacts(db, owner_id=refresh_env.account.id)
        assert len(rows) == 2
        local_row = next(row for row in rows if row["source_id"] == local["source_id"])
        remote_row = next(row for row in rows if row["source_id"] == source.id)
        with pytest.raises(ContactConflict, match="ambiguous"):
            update_contact(
                db,
                owner_id=refresh_env.account.id,
                uid=local["uid"],
                name="Unqualified Must Fail",
                emails=[],
                phones=[],
                expected_version=local_row["version"],
            )
        db.rollback()

        updated = update_contact(
            db,
            owner_id=refresh_env.account.id,
            uid=local["uid"],
            source_id=local_row["source_id"],
            name="Qualified Local Update",
            emails=["qualified-local@example.test"],
            phones=[],
            expected_version=local_row["version"],
        )
        db.commit()
        assert updated["source_id"] == local_row["source_id"]
        names = {
            row["source_id"]: row["name"]
            for row in list_contacts(db, owner_id=refresh_env.account.id)
        }
        assert names[local_row["source_id"]] == "Qualified Local Update"
        assert names[remote_row["source_id"]] == "Remote Duplicate UID"

        bob = ensure_account(db, "bob")
        bob_contact = create_contact(
            db,
            owner_id=bob.id,
            name="Bob Private",
            email="bob-private@example.test",
        )
        db.commit()
        with pytest.raises(ContactNotFound, match="source"):
            update_contact(
                db,
                owner_id=refresh_env.account.id,
                uid=local["uid"],
                source_id=bob_contact["source_id"],
                name="Cross Owner",
                emails=[],
                phones=[],
                expected_version=updated["version"],
            )
    finally:
        db.rollback()
        db.close()


def test_settings_round_trips_source_and_only_shows_resolution_for_tombstones():
    settings = Path("static/js/settings.js").read_text(encoding="utf-8")
    assert 'data-source-id="${esc(c.source_id || \'\')}"' in settings
    assert "source_id: row.dataset.sourceId || undefined" in settings
    assert "params.set('source_id', row.dataset.sourceId)" in settings
    assert "Deletion conflict" in settings
    assert "c.deleted ? 'display:none;'" in settings


def test_keep_local_rebases_stale_generation_and_unblocks_delivery(
    refresh_env, monkeypatch,
):
    source = _configured_source(refresh_env, url="https://old.example/book")
    db = refresh_env.Session()
    try:
        contact = create_contact(
            db,
            owner_id=refresh_env.account.id,
            name="Keep Local",
            email="keep-local@example.test",
        )
        delivery = db.query(ContactDelivery).one()
        delivery.state = "conflict"
        delivery.last_error_code = "remote_conflict"
        db.commit()

        current = db.query(ContactSource).filter_by(id=source.id).one()
        upsert_carddav_config(
            db,
            owner_id=refresh_env.account.id,
            url="https://new.example/book",
            username="new-user",
            password="new-password",
            expected_version=int(current.config_version),
        )
        db.commit()
    finally:
        db.close()

    remote = carddav.build_vcard(
        "Remote Before Rebase",
        uid=contact["uid"],
        emails=["remote@example.test"],
    )
    monkeypatch.setattr(
        carddav,
        "get_contact_resource",
        lambda _config, *, target: (remote, '"remote-v2"', True),
    )
    result = resolve_contact_conflict_detached(
        refresh_env.Session,
        owner_id=refresh_env.account.id,
        uid=contact["uid"],
        expected_version=contact["version"],
        resolution="keep_local",
    )
    assert result["name"] == "Keep Local"
    assert result["sync_state"] == "pending"

    db = refresh_env.Session()
    try:
        deliveries = db.query(ContactDelivery).order_by(
            ContactDelivery.created_at, ContactDelivery.id,
        ).all()
        assert len(deliveries) == 2
        assert deliveries[0].state == "completed"
        assert deliveries[0].payload == {}
        assert deliveries[1].state == "pending"
        assert deliveries[1].operation == "update"
        assert deliveries[1].payload["source_config_version"] == 2
        row = db.query(ContactRecord).one()
        assert row.remote_etag == '"remote-v2"'
        assert row.remote_href.startswith("https://new.example/")
    finally:
        db.close()

    monkeypatch.setattr(
        carddav,
        "put_contact",
        lambda _config, **_kwargs: (
            "https://new.example/book/contact.vcf", '"delivered-v3"'
        ),
    )
    assert drain_contact_deliveries(
        refresh_env.Session,
        owner_id=refresh_env.account.id,
        limit=5,
    ) == {"completed": 1, "retried": 0, "conflicts": 0}


def test_use_remote_supersedes_all_local_work_and_adopts_remote(
    refresh_env, monkeypatch,
):
    _configured_source(refresh_env)
    db = refresh_env.Session()
    try:
        contact = create_contact(
            db,
            owner_id=refresh_env.account.id,
            name="Local Before Conflict",
            email="local-before@example.test",
        )
        delivery = db.query(ContactDelivery).one()
        delivery.state = "conflict"
        delivery.last_error_code = "remote_conflict"
        db.commit()
    finally:
        db.close()

    remote = carddav.build_vcard(
        "Authoritative Remote",
        uid=contact["uid"],
        emails=["remote-authority@example.test"],
    )
    monkeypatch.setattr(
        carddav,
        "get_contact_resource",
        lambda _config, *, target: (remote, '"remote-v9"', True),
    )
    result = resolve_contact_conflict_detached(
        refresh_env.Session,
        owner_id=refresh_env.account.id,
        uid=contact["uid"],
        expected_version=contact["version"],
        resolution="use_remote",
    )
    assert result["name"] == "Authoritative Remote"
    assert result["emails"] == ["remote-authority@example.test"]
    assert result["sync_state"] == "synced"

    db = refresh_env.Session()
    try:
        assert db.query(ContactDelivery).filter(
            ContactDelivery.state != "completed"
        ).count() == 0
        delivery = db.query(ContactDelivery).one()
        assert delivery.payload == {}
        audit = db.query(ActionAudit).filter_by(
            action="contacts.conflict_resolved"
        ).one()
        assert audit.details["resolution"] == "use_remote"
        assert audit.details["superseded_delivery_count"] == 1
    finally:
        db.close()


def test_conflict_resolution_source_fence_preserves_concurrent_state(
    refresh_env, monkeypatch,
):
    source = _configured_source(refresh_env)
    db = refresh_env.Session()
    try:
        contact = create_contact(
            db,
            owner_id=refresh_env.account.id,
            name="Fenced Conflict",
            email="fenced-conflict@example.test",
        )
        delivery = db.query(ContactDelivery).one()
        delivery.state = "conflict"
        delivery.last_error_code = "remote_conflict"
        db.commit()
    finally:
        db.close()

    remote = carddav.build_vcard(
        "Remote Fenced", uid=contact["uid"], emails=["remote@example.test"],
    )

    def concurrent_remote_read(_config, *, target):
        writer = refresh_env.Session()
        try:
            current = writer.query(ContactSource).filter_by(id=source.id).one()
            updated = writer.query(ContactSource).filter(
                ContactSource.id == source.id,
                ContactSource.owner_id == refresh_env.account.id,
                ContactSource.version == int(current.version),
            ).update({
                ContactSource.sync_state: "error",
                ContactSource.last_error: "Concurrent source owner",
                ContactSource.version: int(current.version) + 1,
            }, synchronize_session=False)
            assert updated == 1
            writer.commit()
        finally:
            writer.close()
        return remote, '"remote-fenced"', True

    monkeypatch.setattr(carddav, "get_contact_resource", concurrent_remote_read)
    with pytest.raises(ContactConflict, match="source changed"):
        resolve_contact_conflict_detached(
            refresh_env.Session,
            owner_id=refresh_env.account.id,
            uid=contact["uid"],
            expected_version=contact["version"],
            resolution="use_remote",
        )

    db = refresh_env.Session()
    try:
        current = db.query(ContactSource).filter_by(id=source.id).one()
        assert current.last_error == "Concurrent source owner"
        delivery = db.query(ContactDelivery).one()
        assert delivery.state == "conflict"
        assert delivery.payload
        assert db.query(ContactRecord).one().payload["name"] == "Fenced Conflict"
    finally:
        db.close()


def _tombstoned_delete_conflict(env):
    db = env.Session()
    try:
        contact = create_contact(
            db,
            owner_id=env.account.id,
            name="Delete Conflict",
            email="delete-conflict@example.test",
        )
        initial = db.query(ContactDelivery).one()
        initial.state = "completed"
        initial.payload = {}
        deleted = delete_contact(
            db,
            owner_id=env.account.id,
            uid=contact["uid"],
            expected_version=contact["version"],
        )
        assert deleted is True
        conflict = db.query(ContactDelivery).filter(
            ContactDelivery.state != "completed"
        ).one()
        conflict.state = "conflict"
        conflict.last_error_code = "remote_conflict"
        row = db.query(ContactRecord).one()
        version = int(row.version)
        db.commit()
        return contact["uid"], version
    finally:
        db.close()


def test_tombstoned_delete_conflict_is_visible_and_keep_local_retries_delete(
    refresh_env, monkeypatch,
):
    _configured_source(refresh_env)
    uid, version = _tombstoned_delete_conflict(refresh_env)

    db = refresh_env.Session()
    try:
        visible = list_contacts(
            db, owner_id=refresh_env.account.id, refresh=False,
        )
        assert len(visible) == 1
        assert visible[0]["uid"] == uid
        assert visible[0]["deleted"] is True
        assert visible[0]["sync_state"] == "conflict"
    finally:
        db.close()

    remote = carddav.build_vcard(
        "Still Remote", uid=uid, emails=["still-remote@example.test"],
    )
    monkeypatch.setattr(
        carddav,
        "get_contact_resource",
        lambda _config, *, target: (remote, '"delete-v2"', True),
    )
    result = resolve_contact_conflict_detached(
        refresh_env.Session,
        owner_id=refresh_env.account.id,
        uid=uid,
        expected_version=version,
        resolution="keep_local",
    )
    assert result["deleted"] is True
    assert result["sync_state"] == "pending"

    db = refresh_env.Session()
    try:
        pending = db.query(ContactDelivery).filter(
            ContactDelivery.state == "pending"
        ).one()
        assert pending.operation == "delete"
        assert db.query(ContactRecord).one().deleted_at is not None
    finally:
        db.close()


def test_tombstoned_delete_conflict_can_restore_the_remote_copy(
    refresh_env, monkeypatch,
):
    _configured_source(refresh_env)
    uid, version = _tombstoned_delete_conflict(refresh_env)
    remote = carddav.build_vcard(
        "Restored Remote",
        uid=uid,
        emails=["restored-remote@example.test"],
    )
    monkeypatch.setattr(
        carddav,
        "get_contact_resource",
        lambda _config, *, target: (remote, '"restore-v3"', True),
    )
    result = resolve_contact_conflict_detached(
        refresh_env.Session,
        owner_id=refresh_env.account.id,
        uid=uid,
        expected_version=version,
        resolution="use_remote",
    )
    assert result["deleted"] is False
    assert result["name"] == "Restored Remote"
    assert result["emails"] == ["restored-remote@example.test"]

    db = refresh_env.Session()
    try:
        row = db.query(ContactRecord).one()
        assert row.deleted_at is None
        assert db.query(ContactDelivery).filter(
            ContactDelivery.state != "completed"
        ).count() == 0
    finally:
        db.close()
