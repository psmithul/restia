from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta

import cbor2
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from webauthn.helpers import bytes_to_base64url

from core.database import Account, AuthSession, Base
from src.audit_context import bind_service_audit_context
from src.identity import ensure_account
from src.webauthn_service import (
    PasskeyVerificationError,
    begin_registration,
    begin_unlock,
    complete_registration,
    complete_unlock,
    list_credentials,
    relying_party_context,
    revoke_credential,
    session_verification_state,
)


ORIGIN = "http://localhost:7902"
RP_ID = "localhost"


@pytest.fixture()
def passkey_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'passkeys.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime(2026, 7, 18, 12, 0, 0)
    db = factory()
    account = ensure_account(db, "alice")
    session = AuthSession(
        id=str(uuid.uuid4()),
        account_id=account.id,
        token_digest="d" * 64,
        auth_epoch=account.auth_epoch,
        expires_at=now + timedelta(days=1),
        interface="web",
        auth_method="local",
    )
    db.add(session)
    db.commit()
    yield db, account, session, now
    db.close()
    engine.dispose()
    secret_storage._fernet = None
    secret_storage._digest_key = None


def _client_data(kind: str, challenge: str, *, origin: str = ORIGIN) -> bytes:
    return json.dumps(
        {"type": kind, "challenge": challenge, "origin": origin},
        separators=(",", ":"),
    ).encode("utf-8")


def _registration_credential(options: dict, private_key, credential_id: bytes) -> dict:
    numbers = private_key.public_key().public_numbers()
    cose_key = cbor2.dumps({
        1: 2,
        3: -7,
        -1: 1,
        -2: numbers.x.to_bytes(32, "big"),
        -3: numbers.y.to_bytes(32, "big"),
    })
    authenticator_data = (
        hashlib.sha256(RP_ID.encode()).digest()
        + b"\x45"  # user present, user verified, attested credential data
        + (0).to_bytes(4, "big")
        + (b"\0" * 16)
        + len(credential_id).to_bytes(2, "big")
        + credential_id
        + cose_key
    )
    client_data = _client_data("webauthn.create", options["challenge"])
    identifier = bytes_to_base64url(credential_id)
    return {
        "id": identifier,
        "rawId": identifier,
        "type": "public-key",
        "authenticatorAttachment": "platform",
        "clientExtensionResults": {},
        "response": {
            "clientDataJSON": bytes_to_base64url(client_data),
            "attestationObject": bytes_to_base64url(cbor2.dumps({
                "fmt": "none",
                "attStmt": {},
                "authData": authenticator_data,
            })),
            "transports": ["internal"],
        },
    }


def _authentication_credential(
    options: dict,
    private_key,
    credential_id: bytes,
    *,
    sign_count: int = 1,
) -> dict:
    authenticator_data = (
        hashlib.sha256(RP_ID.encode()).digest()
        + b"\x05"  # user present and user verified
        + sign_count.to_bytes(4, "big")
    )
    client_data = _client_data("webauthn.get", options["challenge"])
    signed = authenticator_data + hashlib.sha256(client_data).digest()
    signature = private_key.sign(signed, ec.ECDSA(hashes.SHA256()))
    identifier = bytes_to_base64url(credential_id)
    return {
        "id": identifier,
        "rawId": identifier,
        "type": "public-key",
        "authenticatorAttachment": "platform",
        "clientExtensionResults": {},
        "response": {
            "clientDataJSON": bytes_to_base64url(client_data),
            "authenticatorData": bytes_to_base64url(authenticator_data),
            "signature": bytes_to_base64url(signature),
            "userHandle": None,
        },
    }


def test_real_webauthn_registration_unlock_replay_and_revocation(passkey_env):
    db, account, session, now = passkey_env
    bind_service_audit_context(
        db,
        account_id=account.id,
        interface="web",
        credential_type="session",
        credential_id=session.id,
    )
    context = relying_party_context(ORIGIN)
    private_key = ec.generate_private_key(ec.SECP256R1())
    credential_id = uuid.uuid4().bytes + uuid.uuid4().bytes

    registration = begin_registration(
        db,
        account=account,
        auth_session_id=session.id,
        context=context,
        label="MacBook Touch ID",
        now=now,
    )
    encrypted_challenge = db.execute(text(
        "SELECT challenge FROM webauthn_challenges WHERE id = :identifier"
    ), {"identifier": registration["ceremony_id"]}).scalar_one()
    assert str(encrypted_challenge).startswith("enc:")
    assert registration["options"]["challenge"] not in str(encrypted_challenge)
    credential = _registration_credential(registration["options"], private_key, credential_id)
    enrolled = complete_registration(
        db,
        account=account,
        auth_session_id=session.id,
        ceremony_id=registration["ceremony_id"],
        label="MacBook Touch ID",
        credential=credential,
        now=now,
    )
    db.commit()
    assert enrolled["label"] == "MacBook Touch ID"
    assert enrolled["backed_up"] is False
    assert len(list_credentials(db, account_id=account.id)) == 1

    with pytest.raises(PasskeyVerificationError, match="invalid or expired"):
        complete_registration(
            db,
            account=account,
            auth_session_id=session.id,
            ceremony_id=registration["ceremony_id"],
            label="Replay",
            credential=credential,
            now=now,
        )
    db.rollback()

    unlock = begin_unlock(
        db,
        account=account,
        auth_session_id=session.id,
        context=context,
        now=now + timedelta(seconds=1),
    )
    assertion = _authentication_credential(unlock["options"], private_key, credential_id)
    verified = complete_unlock(
        db,
        account=account,
        auth_session_id=session.id,
        ceremony_id=unlock["ceremony_id"],
        credential=assertion,
        now=now + timedelta(seconds=1),
    )
    db.commit()
    assert verified["verified"] is True
    assert verified["method"] == "webauthn"
    assert session_verification_state(
        db,
        account_id=account.id,
        auth_session_id=session.id,
        now=now + timedelta(minutes=14),
    )["verified"] is True
    assert session_verification_state(
        db,
        account_id=account.id,
        auth_session_id=session.id,
        now=now + timedelta(minutes=16),
    )["verified"] is False

    revoked = revoke_credential(db, account=account, credential_id=enrolled["id"], now=now)
    db.commit()
    assert revoked["revoked"] is True
    assert list_credentials(db, account_id=account.id) == []
    assert session_verification_state(
        db,
        account_id=account.id,
        auth_session_id=session.id,
        now=now + timedelta(seconds=2),
    )["verified"] is False


def test_relying_party_rejects_non_loopback_plain_http():
    with pytest.raises(ValueError, match="requires HTTPS"):
        relying_party_context("http://restia.example.test")
