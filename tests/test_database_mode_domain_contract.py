"""One canonical Life contract exercised on SQLite and PostgreSQL."""

from __future__ import annotations

import concurrent.futures
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import ActionAudit, InboxItem, LifeEntity
from src.action_policy import create_action_proposal
from src.database_migrations import upgrade_schema
from src.distributed_leadership import RuntimeLeadershipAuthority
from src.identity import ensure_account
from src.life_core import create_inbox_item
from src.life_graph import (
    LifeGraphNotFound,
    create_entity_link,
    create_life_entity,
    create_life_source,
    get_life_entity,
)
from src.profile_configuration_service import put_configuration


def _postgres_test_url() -> str:
    return str(os.getenv("RESTIA_TEST_POSTGRES_URL") or "").strip()


@pytest.fixture(params=("sqlite", "postgresql"))
def canonical_database(request, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY",
        "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    )
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    mode = request.param
    if mode == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'domain-contract.db'}")
        cleanup = lambda: None
    else:
        database_url = _postgres_test_url()
        if not database_url:
            pytest.skip("RESTIA_TEST_POSTGRES_URL is not configured")
        schema = f"restia_contract_{uuid.uuid4().hex}"
        base_engine = create_engine(database_url)
        with base_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(
            database_url,
            connect_args={"options": f"-csearch_path={schema}"},
        )

        def cleanup() -> None:
            engine.dispose()
            with base_engine.begin() as connection:
                connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
            base_engine.dispose()

    try:
        upgrade_schema(engine)
        yield mode, sessionmaker(bind=engine, expire_on_commit=False), engine
    finally:
        if mode == "sqlite":
            engine.dispose()
        cleanup()


def test_canonical_identity_life_policy_profile_and_leadership_contract(
    canonical_database,
):
    mode, Session, engine = canonical_database
    db = Session()
    try:
        alice = ensure_account(db, f"alice-{mode}")
        bob = ensure_account(db, f"bob-{mode}")
        inbox, created = create_inbox_item(
            db,
            account=alice,
            title="Taxi receipt",
            content="Paid 18.50 for airport transfer",
            kind="expense",
            source_type="receipt",
            source_ref="receipt:test:1",
            metadata={"currency": "USD"},
            idempotency_key="receipt-test-1",
        )
        source, _ = create_life_source(
            db,
            account=alice,
            source_type="receipt",
            title="Taxi receipt",
            safe_excerpt="Paid 18.50",
            idempotency_key="source-test-1",
        )
        goal, _ = create_life_entity(
            db,
            account=alice,
            entity_type="goal",
            title="Submit application",
            provenance={"source_ids": [source.id]},
            idempotency_key="goal-test-1",
        )
        task, _ = create_life_entity(
            db,
            account=alice,
            entity_type="task",
            title="Review application essay",
            properties={"next_action": "Open the latest draft"},
            provenance={"source_ids": [source.id]},
            idempotency_key="task-test-1",
        )
        link, linked = create_entity_link(
            db,
            account=alice,
            source_id=task.id,
            relation="advances",
            target_id=goal.id,
            provenance={"source_ids": [source.id]},
        )
        proposal = create_action_proposal(
            db,
            owner_id=alice.id,
            domain="finance",
            action="transfer_money",
            autonomy_level=1,
            target_type="transaction",
            payload={"amount": "10.00", "currency": "USD"},
            reason="Contract test only",
            external=False,
            idempotency_key="finance-contract-1",
        )
        configuration = put_configuration(
            db,
            account=alice,
            namespace="preference",
            key="ui.density",
            value="compact",
            source="domain_service",
            idempotency_key="profile-contract-1",
        )
        db.commit()

        assert created is True
        assert linked is True
        assert proposal.proposal.autonomy_level == 6
        assert proposal.proposal.requires_confirmation is True
        assert proposal.confirmation_token
        assert configuration.record.owner_id == alice.id
        assert db.query(InboxItem).filter_by(owner_id=alice.id).count() == 1
        assert db.query(LifeEntity).filter_by(owner_id=alice.id).count() == 2
        assert db.query(ActionAudit).filter_by(owner_id=alice.id).count() >= 5
        assert get_life_entity(db, owner_id=alice.id, entity_id=task.id).id == task.id
        with pytest.raises(LifeGraphNotFound):
            get_life_entity(db, owner_id=bob.id, entity_id=task.id)
        assert link.owner_id == alice.id

        stored_private = db.execute(text(
            "SELECT private_value FROM profile_configurations "
            "WHERE id = :record_id"
        ), {"record_id": configuration.record.id}).scalar_one()
        # JSON columns return either the decoded envelope or its JSON string,
        # depending on dialect/driver. Both must be ciphertext, never payload.
        assert "compact" not in str(stored_private)
        assert "enc:c1:" in str(stored_private)
    finally:
        db.close()

    authority = RuntimeLeadershipAuthority(Session)
    token = authority.acquire(
        lease_name=f"contract-{mode}",
        holder_id="contract-replica",
        lease_seconds=30,
    )
    assert token is not None
    assert authority.release(token) is True

    def contend(holder_id: str):
        return RuntimeLeadershipAuthority(Session).acquire(
            lease_name=f"contract-contended-{mode}",
            holder_id=holder_id,
            lease_seconds=30,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        attempts = list(pool.map(
            contend, [f"contract-replica-{index}" for index in range(4)]
        ))
    winners = [candidate for candidate in attempts if candidate is not None]
    assert len(winners) == 1
    assert RuntimeLeadershipAuthority(Session).release(winners[0]) is True
