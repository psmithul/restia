from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import (
    Base,
    ContactDelivery,
    ContactRecord,
    ContactSource,
    utcnow_naive,
)
from src.identity import ensure_account


@pytest.fixture()
def delivery_env(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'contact-delivery.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.1},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    owner = ensure_account(db, "contact-delivery-owner")
    source = ContactSource(
        id="contact-delivery-source",
        owner_id=owner.id,
        kind="carddav",
        label="CardDAV",
        base_url="https://dav.example.test/addressbook",
        username="private-user",
        password="private-password",
        enabled=True,
        sync_state="idle",
        config_version=1,
        version=1,
    )
    record = ContactRecord(
        id="contact-delivery-record",
        owner_id=owner.id,
        source_id=source.id,
        remote_uid="private-contact-uid",
        payload={
            "name": "Private Person",
            "emails": ["private@example.test"],
            "phones": [],
            "address": "",
        },
        raw_vcard=(
            "BEGIN:VCARD\r\nVERSION:4.0\r\nUID:private-contact-uid\r\n"
            "FN:Private Person\r\nEND:VCARD\r\n"
        ),
        version=1,
    )
    db.add(source)
    db.commit()
    db.add(record)
    db.commit()
    db.close()
    try:
        yield SimpleNamespace(
            engine=engine,
            Session=factory,
            owner_id=owner.id,
            source_id=source.id,
            record_id=record.id,
        )
    finally:
        engine.dispose()


def _queue_delivery(delivery_env, *, delivery_id="delivery-1"):
    db = delivery_env.Session()
    try:
        db.add(ContactDelivery(
            id=delivery_id,
            owner_id=delivery_env.owner_id,
            source_id=delivery_env.source_id,
            record_id=delivery_env.record_id,
            operation="create",
            idempotency_key=f"key-{delivery_id}",
            payload={
                "uid": "private-contact-uid",
                "raw_vcard": (
                    "BEGIN:VCARD\r\nVERSION:4.0\r\n"
                    "UID:private-contact-uid\r\nFN:Private Person\r\n"
                    "END:VCARD\r\n"
                ),
                "record_version": 1,
                "source_config_version": 1,
            },
            state="pending",
            attempts=0,
            version=1,
        ))
        db.commit()
    finally:
        db.close()


def test_source_label_and_delivery_payload_are_private_at_rest(delivery_env):
    import src.contact_delivery as delivery

    raw_vcard = (
        "BEGIN:VCARD\r\nVERSION:4.0\r\nUID:private-contact-uid\r\n"
        "FN:Private Person\r\nEND:VCARD\r\n"
    )
    db = delivery_env.Session()
    try:
        source = db.query(ContactSource).filter_by(
            id=delivery_env.source_id
        ).one()
        record = db.query(ContactRecord).filter_by(
            id=delivery_env.record_id
        ).one()
        queued = delivery.enqueue_contact_delivery(
            db,
            owner_id=delivery_env.owner_id,
            source=source,
            record=record,
            operation="create",
            raw_vcard=raw_vcard,
        )
        queued_id = queued.id
        db.commit()
    finally:
        db.close()

    with delivery_env.engine.connect() as connection:
        raw_label = connection.execute(text(
            "SELECT label FROM contact_sources WHERE id=:id"
        ), {"id": delivery_env.source_id}).scalar_one()
        raw_key, raw_payload = connection.execute(text(
            "SELECT idempotency_key, payload FROM contact_deliveries WHERE id=:id"
        ), {"id": queued_id}).one()
    assert str(raw_label).startswith("enc:c1:")
    assert "CardDAV" not in str(raw_label)
    serialized_payload = str(raw_payload)
    assert serialized_payload.startswith('"enc:c1:')
    assert "Private Person" not in serialized_payload
    assert "private-contact-uid" not in serialized_payload

    content_hash = hashlib.sha256(raw_vcard.encode("utf-8")).hexdigest()
    predictable = hashlib.sha256(
        (
            f"{delivery_env.record_id}:1:create:{content_hash}"
        ).encode("utf-8")
    ).hexdigest()
    assert raw_key != predictable
    assert len(raw_key) == 64


def test_idempotency_is_scoped_to_carddav_config_generation(delivery_env):
    import src.contact_delivery as delivery

    raw_vcard = (
        "BEGIN:VCARD\r\nVERSION:4.0\r\nUID:private-contact-uid\r\n"
        "FN:Private Person\r\nEND:VCARD\r\n"
    )
    db = delivery_env.Session()
    try:
        source = db.query(ContactSource).filter_by(
            id=delivery_env.source_id
        ).one()
        record = db.query(ContactRecord).filter_by(
            id=delivery_env.record_id
        ).one()
        first = delivery.enqueue_contact_delivery(
            db,
            owner_id=delivery_env.owner_id,
            source=source,
            record=record,
            operation="create",
            raw_vcard=raw_vcard,
        )
        first_id = first.id
        first_key = first.idempotency_key
        db.commit()

        source.config_version = 2
        db.commit()
        second = delivery.enqueue_contact_delivery(
            db,
            owner_id=delivery_env.owner_id,
            source=source,
            record=record,
            operation="create",
            raw_vcard=raw_vcard,
        )
        db.commit()
        assert second.id != first_id
        assert second.idempotency_key != first_key
        assert second.payload["source_config_version"] == 2
    finally:
        db.close()


def test_claim_returns_the_committed_attempt_and_token(delivery_env):
    import src.contact_delivery as delivery

    _queue_delivery(delivery_env)
    db = delivery_env.Session()
    try:
        claimed = delivery._claim_one(db, owner_id=delivery_env.owner_id)
        assert claimed is not None
        assert claimed.state == "processing"
        assert claimed.attempts == 1
        assert claimed.version == 2
        assert claimed.claim_token
    finally:
        db.rollback()
        db.close()


def test_blocked_records_cannot_starve_later_independent_work(delivery_env):
    import src.contact_delivery as delivery

    db = delivery_env.Session()
    try:
        base = datetime(2026, 1, 1)
        blocked_deliveries = []
        for index in range(101):
            record_id = f"blocked-record-{index:03d}"
            db.add(ContactRecord(
                id=record_id,
                owner_id=delivery_env.owner_id,
                source_id=delivery_env.source_id,
                remote_uid=f"blocked-uid-{index:03d}",
                payload={"name": "", "emails": [], "phones": [], "address": ""},
                version=1,
            ))
            blocked_deliveries.extend([
                ContactDelivery(
                    id=f"blocked-conflict-{index:03d}",
                    owner_id=delivery_env.owner_id,
                    source_id=delivery_env.source_id,
                    record_id=record_id,
                    operation="update",
                    idempotency_key=f"blocked-conflict-key-{index:03d}",
                    payload={},
                    state="conflict",
                    attempts=1,
                    version=1,
                    created_at=base + timedelta(seconds=index * 2),
                ),
                ContactDelivery(
                    id=f"blocked-pending-{index:03d}",
                    owner_id=delivery_env.owner_id,
                    source_id=delivery_env.source_id,
                    record_id=record_id,
                    operation="update",
                    idempotency_key=f"blocked-pending-key-{index:03d}",
                    payload={},
                    state="pending",
                    attempts=0,
                    version=1,
                    created_at=base + timedelta(seconds=index * 2 + 1),
                ),
            ])
        independent_record = ContactRecord(
            id="independent-record",
            owner_id=delivery_env.owner_id,
            source_id=delivery_env.source_id,
            remote_uid="independent-uid",
            payload={"name": "", "emails": [], "phones": [], "address": ""},
            version=1,
        )
        independent = ContactDelivery(
            id="independent-delivery",
            owner_id=delivery_env.owner_id,
            source_id=delivery_env.source_id,
            record_id=independent_record.id,
            operation="create",
            idempotency_key="independent-key",
            payload={"source_config_version": 1},
            state="pending",
            attempts=0,
            version=1,
            created_at=base + timedelta(seconds=1000),
        )
        db.add(independent_record)
        db.commit()
        db.add_all(blocked_deliveries)
        db.add(independent)
        db.commit()

        claimed = delivery._claim_one(db, owner_id=delivery_env.owner_id)
        assert claimed is not None
        assert claimed.id == independent.id
    finally:
        db.rollback()
        db.close()


def test_network_phase_holds_no_database_transaction(delivery_env, monkeypatch):
    import src.contact_delivery as delivery

    _queue_delivery(delivery_env)

    def put_contact(_config, **_kwargs):
        # A separate writer must be able to commit while CardDAV is in flight.
        # The fixture's 100ms SQLite busy timeout makes a lingering claim/read
        # transaction fail this assertion promptly.
        other = delivery_env.Session()
        try:
            other.query(ContactSource).filter(
                ContactSource.id == delivery_env.source_id
            ).update({ContactSource.label: "Concurrent label"})
            other.commit()
        finally:
            other.close()
        return "https://dav.example.test/addressbook/private-contact-uid.vcf", '"e1"'

    monkeypatch.setattr(delivery.carddav, "put_contact", put_contact)
    assert delivery.drain_contact_deliveries(
        delivery_env.Session, owner_id=delivery_env.owner_id, limit=1
    ) == {"completed": 1, "retried": 0, "conflicts": 0}

    db = delivery_env.Session()
    try:
        queued = db.query(ContactDelivery).filter_by(id="delivery-1").one()
        record = db.query(ContactRecord).filter_by(
            id=delivery_env.record_id
        ).one()
        source = db.query(ContactSource).filter_by(
            id=delivery_env.source_id
        ).one()
        assert queued.state == "completed"
        assert queued.payload == {}
        assert record.remote_etag == '"e1"'
        assert source.label == "Concurrent label"
    finally:
        db.close()


def test_unrelated_success_does_not_hide_an_existing_conflict(
    delivery_env, monkeypatch
):
    import src.contact_delivery as delivery

    db = delivery_env.Session()
    try:
        blocked_record = ContactRecord(
            id="conflicted-record",
            owner_id=delivery_env.owner_id,
            source_id=delivery_env.source_id,
            remote_uid="conflicted-uid",
            payload={"name": "", "emails": [], "phones": [], "address": ""},
            version=1,
        )
        db.add(blocked_record)
        db.commit()
        db.add(ContactDelivery(
            id="existing-conflict",
            owner_id=delivery_env.owner_id,
            source_id=delivery_env.source_id,
            record_id=blocked_record.id,
            operation="update",
            idempotency_key="existing-conflict-key",
            payload={"source_config_version": 1},
            state="conflict",
            attempts=1,
            version=1,
        ))
        db.commit()
    finally:
        db.close()
    _queue_delivery(delivery_env)
    monkeypatch.setattr(
        delivery.carddav,
        "put_contact",
        lambda _config, **_kwargs: (
            "https://dav.example.test/addressbook/private-contact-uid.vcf",
            '"e-conflict-preserved"',
        ),
    )
    result = delivery.drain_contact_deliveries(
        delivery_env.Session, owner_id=delivery_env.owner_id, limit=1
    )
    assert result["completed"] == 1
    db = delivery_env.Session()
    try:
        source = db.query(ContactSource).filter_by(
            id=delivery_env.source_id
        ).one()
        assert source.sync_state == "error"
        assert source.last_error == "CardDAV contact delivery requires attention"
    finally:
        db.close()


def test_unexpected_worker_crash_leaves_a_recoverable_processing_lease(
    delivery_env, monkeypatch
):
    import src.contact_delivery as delivery

    _queue_delivery(delivery_env)

    def crash(_config, **_kwargs):
        raise ValueError("simulated process failure")

    monkeypatch.setattr(delivery.carddav, "put_contact", crash)
    with pytest.raises(ValueError, match="simulated process failure"):
        delivery.drain_contact_deliveries(
            delivery_env.Session, owner_id=delivery_env.owner_id, limit=1
        )

    db = delivery_env.Session()
    try:
        queued = db.query(ContactDelivery).filter_by(id="delivery-1").one()
        assert queued.state == "processing"
        assert queued.claim_token
        assert queued.attempts == 1
        queued.claimed_at = utcnow_naive() - timedelta(
            seconds=delivery.CLAIM_LEASE_SECONDS + 1
        )
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        delivery.carddav,
        "put_contact",
        lambda _config, **_kwargs: (
            "https://dav.example.test/addressbook/private-contact-uid.vcf",
            '"e2"',
        ),
    )
    result = delivery.drain_contact_deliveries(
        delivery_env.Session, owner_id=delivery_env.owner_id, limit=1
    )
    assert result["completed"] == 1
    db = delivery_env.Session()
    try:
        queued = db.query(ContactDelivery).filter_by(id="delivery-1").one()
        assert queued.state == "completed"
        assert queued.attempts == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_background_pass_is_bounded_and_independent_from_tasks(
    delivery_env, monkeypatch
):
    import src.contact_delivery as delivery

    _queue_delivery(delivery_env)
    monkeypatch.setenv("RESTIA_INPROCESS_TASKS", "0")
    monkeypatch.delenv("RESTIA_INPROCESS_CONTACT_DELIVERY", raising=False)
    assert delivery.inprocess_contact_delivery_enabled() is True
    assert delivery.contact_delivery_worker_enabled(
        cutover_error="legacy_import_locked"
    ) is False
    assert delivery.contact_delivery_worker_enabled(cutover_error=None) is True
    monkeypatch.setattr(
        delivery.carddav,
        "put_contact",
        lambda _config, **_kwargs: (
            "https://dav.example.test/addressbook/private-contact-uid.vcf",
            '"e3"',
        ),
    )
    result = await delivery.drain_contact_deliveries_once(
        delivery_env.Session, owner_limit=1, batch_size=1
    )
    assert result == {
        "owners": 1,
        "completed": 1,
        "retried": 0,
        "conflicts": 0,
    }
    monkeypatch.setenv("RESTIA_INPROCESS_CONTACT_DELIVERY", "0")
    assert delivery.inprocess_contact_delivery_enabled() is False


def test_pinned_transport_rejects_oversized_responses():
    import src.carddav_contacts as carddav

    class CoreResponse:
        status = 200
        headers = []
        extensions = {}

        def __init__(self):
            self.closed = False

        def iter_stream(self):
            yield b"x" * carddav.MAX_CARDDAV_RESPONSE_BYTES
            yield b"y"

        def close(self):
            self.closed = True

    response = CoreResponse()

    class Pool:
        def handle_request(self, _request):
            return response

    transport = object.__new__(carddav._PinnedSyncTransport)
    transport._pool = Pool()
    with pytest.raises(carddav.CardDAVError, match="size limit"):
        transport.handle_request(httpx.Request("GET", "https://dav.example.test"))
    assert response.closed is True


def test_carddav_requires_https_for_public_destinations(monkeypatch):
    import src.carddav_contacts as carddav

    monkeypatch.delenv("CARDDAV_ALLOW_PUBLIC_HTTP", raising=False)
    monkeypatch.delenv("CARDDAV_BLOCK_PRIVATE_IPS", raising=False)
    monkeypatch.setattr(carddav, "_resolved_ips", lambda _host: ["93.184.216.34"])
    with pytest.raises(ValueError, match="public endpoints require HTTPS"):
        carddav.validate_carddav_url("http://dav.example.test/addressbook")

    monkeypatch.setattr(carddav, "_resolved_ips", lambda _host: ["192.168.1.20"])
    assert carddav.validate_carddav_url(
        "http://carddav.lan/addressbook"
    ) == "http://carddav.lan/addressbook"

    monkeypatch.setenv("CARDDAV_ALLOW_PUBLIC_HTTP", "true")
    monkeypatch.setattr(carddav, "_resolved_ips", lambda _host: ["93.184.216.34"])
    assert carddav.validate_carddav_url(
        "http://dav.example.test/addressbook"
    ) == "http://dav.example.test/addressbook"


def test_carddav_request_rechecks_public_http_after_dns_resolution(monkeypatch):
    import src.carddav_contacts as carddav

    monkeypatch.delenv("CARDDAV_ALLOW_PUBLIC_HTTP", raising=False)
    # Model a config-time-safe hostname that rebinds before the actual socket.
    monkeypatch.setattr(
        carddav, "validate_carddav_url", lambda value: str(value).rstrip("/")
    )
    monkeypatch.setattr(carddav, "_resolved_ips", lambda _host: ["93.184.216.34"])
    with pytest.raises(carddav.CardDAVError, match="require HTTPS"):
        carddav._request("GET", "http://rebound.example.test/addressbook")
