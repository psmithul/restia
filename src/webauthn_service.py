"""Server-verified WebAuthn registration and recent-user-verification grants."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    options_to_json,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from core.database import Account, AuthSession, utcnow_naive
from src.life_core import append_action_audit
from src.public_origin import canonical_shared_origin
from src.webauthn_models import WebAuthnChallenge, WebAuthnCredential


CHALLENGE_TTL_SECONDS = 300
DEFAULT_VERIFICATION_TTL_SECONDS = 15 * 60
MAX_CREDENTIAL_JSON_BYTES = 128 * 1024
MAX_ACTIVE_CREDENTIALS = 20


class PasskeyError(ValueError):
    """A passkey request is invalid, unavailable, stale, or denied."""


class PasskeyNotFound(PasskeyError):
    pass


class PasskeyConflict(PasskeyError):
    pass


class PasskeyVerificationError(PasskeyError):
    pass


@dataclass(frozen=True, slots=True)
class RelyingPartyContext:
    rp_id: str
    rp_name: str
    origin: str


def _now(value: datetime | None = None) -> datetime:
    current = value or utcnow_naive()
    if current.tzinfo is not None:
        current = current.astimezone(timezone.utc).replace(tzinfo=None)
    return current


def _verification_ttl(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = str(
        env.get("RESTIA_WEBAUTHN_VERIFICATION_TTL_SECONDS")
        or env.get("ODYSSEUS_WEBAUTHN_VERIFICATION_TTL_SECONDS")
        or DEFAULT_VERIFICATION_TTL_SECONDS
    ).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise PasskeyError(
            "RESTIA_WEBAUTHN_VERIFICATION_TTL_SECONDS must be an integer"
        ) from exc
    if value < 60 or value > 3600:
        raise PasskeyError(
            "RESTIA_WEBAUTHN_VERIFICATION_TTL_SECONDS must be from 60 to 3600"
        )
    return value


def relying_party_context(
    request_base_url: object,
    *,
    environ: Mapping[str, str] | None = None,
) -> RelyingPartyContext:
    """Resolve one exact RP origin; non-loopback HTTP is always rejected."""

    env = os.environ if environ is None else environ
    configured_origin = str(
        env.get("RESTIA_WEBAUTHN_ORIGIN")
        or env.get("ODYSSEUS_WEBAUTHN_ORIGIN")
        or ""
    ).strip()
    origin = canonical_shared_origin(
        configured_origin or str(request_base_url or "").rstrip("/"),
        allow_loopback_http=True,
    )
    if not origin:
        raise PasskeyError(
            "WebAuthn requires HTTPS, or HTTP on localhost; configure "
            "RESTIA_WEBAUTHN_ORIGIN behind a reverse proxy"
        )
    origin_host = (urlsplit(origin).hostname or "").lower()
    configured_rp = str(
        env.get("RESTIA_WEBAUTHN_RP_ID")
        or env.get("ODYSSEUS_WEBAUTHN_RP_ID")
        or ""
    ).strip().lower().rstrip(".")
    rp_id = configured_rp or origin_host
    if (
        not rp_id
        or len(rp_id) > 255
        or not (origin_host == rp_id or origin_host.endswith("." + rp_id))
    ):
        raise PasskeyError(
            "RESTIA_WEBAUTHN_RP_ID must equal the WebAuthn origin host or its parent domain"
        )
    rp_name = str(env.get("RESTIA_WEBAUTHN_RP_NAME") or "Restia").strip()
    if not rp_name or len(rp_name) > 160:
        raise PasskeyError("RESTIA_WEBAUTHN_RP_NAME must contain 1 to 160 characters")
    return RelyingPartyContext(rp_id=rp_id, rp_name=rp_name, origin=origin)


def _bounded_label(value: object) -> str:
    label = " ".join(str(value or "Passkey").split())
    if not label or len(label) > 160:
        raise PasskeyError("Passkey label must contain 1 to 160 characters")
    return label


def _bounded_credential(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PasskeyVerificationError("WebAuthn credential must be an object")
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise PasskeyVerificationError("WebAuthn credential must be bounded JSON") from exc
    if len(encoded) > MAX_CREDENTIAL_JSON_BYTES:
        raise PasskeyVerificationError("WebAuthn credential exceeds the size limit")
    return json.loads(encoded)


def _active_credentials(db, account_id: str) -> list[WebAuthnCredential]:
    return db.query(WebAuthnCredential).filter(
        WebAuthnCredential.account_id == account_id,
        WebAuthnCredential.state == "active",
        WebAuthnCredential.revoked_at.is_(None),
    ).order_by(
        WebAuthnCredential.created_at.asc(),
        WebAuthnCredential.id.asc(),
    ).limit(MAX_ACTIVE_CREDENTIALS + 1).all()


def _session(
    db,
    *,
    account_id: str,
    auth_session_id: str,
    now: datetime,
    lock: bool,
) -> AuthSession:
    query = db.query(AuthSession).filter(
        AuthSession.id == auth_session_id,
        AuthSession.account_id == account_id,
        AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > now,
    )
    if lock:
        query = query.with_for_update()
    row = query.one_or_none()
    if row is None:
        raise PasskeyVerificationError("Authenticated session is no longer active")
    return row


def _new_challenge(
    db,
    *,
    account_id: str,
    auth_session_id: str,
    purpose: str,
    challenge: bytes,
    context: RelyingPartyContext,
    now: datetime,
) -> WebAuthnChallenge:
    # Only the newest ceremony of each kind stays usable for a session.
    db.query(WebAuthnChallenge).filter(
        WebAuthnChallenge.account_id == account_id,
        WebAuthnChallenge.auth_session_id == auth_session_id,
        WebAuthnChallenge.purpose == purpose,
        WebAuthnChallenge.consumed_at.is_(None),
    ).update({WebAuthnChallenge.consumed_at: now}, synchronize_session=False)
    row = WebAuthnChallenge(
        id=str(uuid.uuid4()),
        account_id=account_id,
        auth_session_id=auth_session_id,
        purpose=purpose,
        challenge=bytes_to_base64url(challenge),
        rp_id=context.rp_id,
        expected_origin=context.origin,
        expires_at=now + timedelta(seconds=CHALLENGE_TTL_SECONDS),
        consumed_at=None,
        created_at=now,
    )
    db.add(row)
    db.flush()
    return row


def _challenge(
    db,
    *,
    challenge_id: object,
    account_id: str,
    auth_session_id: str,
    purpose: str,
    now: datetime,
) -> WebAuthnChallenge:
    identifier = str(challenge_id or "").strip()
    if not identifier or len(identifier) > 64:
        raise PasskeyVerificationError("WebAuthn ceremony is invalid or expired")
    row = db.query(WebAuthnChallenge).filter(
        WebAuthnChallenge.id == identifier,
        WebAuthnChallenge.account_id == account_id,
        WebAuthnChallenge.auth_session_id == auth_session_id,
        WebAuthnChallenge.purpose == purpose,
        WebAuthnChallenge.consumed_at.is_(None),
        WebAuthnChallenge.expires_at > now,
    ).with_for_update().one_or_none()
    if row is None:
        raise PasskeyVerificationError("WebAuthn ceremony is invalid or expired")
    _session(
        db,
        account_id=account_id,
        auth_session_id=auth_session_id,
        now=now,
        lock=True,
    )
    return row


def begin_registration(
    db,
    *,
    account: Account,
    auth_session_id: str,
    context: RelyingPartyContext,
    label: object,
    now: datetime | None = None,
) -> dict[str, Any]:
    clock = _now(now)
    _session(
        db, account_id=account.id, auth_session_id=auth_session_id,
        now=clock, lock=True,
    )
    credentials = _active_credentials(db, account.id)
    if len(credentials) >= MAX_ACTIVE_CREDENTIALS:
        raise PasskeyConflict(f"An account may have at most {MAX_ACTIVE_CREDENTIALS} passkeys")
    options = generate_registration_options(
        rp_id=context.rp_id,
        rp_name=context.rp_name,
        user_id=uuid.UUID(account.id).bytes,
        user_name=account.username,
        user_display_name=account.display_name or account.username,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(item.credential_id))
            for item in credentials
        ],
        timeout=CHALLENGE_TTL_SECONDS * 1000,
    )
    ceremony = _new_challenge(
        db,
        account_id=account.id,
        auth_session_id=auth_session_id,
        purpose="registration",
        challenge=options.challenge,
        context=context,
        now=clock,
    )
    return {
        "ceremony_id": ceremony.id,
        "label": _bounded_label(label),
        "options": json.loads(options_to_json(options)),
        "expires_at": ceremony.expires_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def complete_registration(
    db,
    *,
    account: Account,
    auth_session_id: str,
    ceremony_id: object,
    label: object,
    credential: object,
    now: datetime | None = None,
) -> dict[str, Any]:
    clock = _now(now)
    ceremony = _challenge(
        db,
        challenge_id=ceremony_id,
        account_id=account.id,
        auth_session_id=auth_session_id,
        purpose="registration",
        now=clock,
    )
    payload = _bounded_credential(credential)
    try:
        verification = verify_registration_response(
            credential=payload,
            expected_challenge=base64url_to_bytes(ceremony.challenge),
            expected_rp_id=ceremony.rp_id,
            expected_origin=ceremony.expected_origin,
            require_user_presence=True,
            require_user_verification=True,
        )
    except WebAuthnException as exc:
        raise PasskeyVerificationError("WebAuthn registration verification failed") from exc
    if not verification.user_verified:
        raise PasskeyVerificationError("The authenticator did not verify the user")
    credential_id = bytes_to_base64url(verification.credential_id)
    supplied_id = str(payload.get("id") or "")
    if supplied_id != credential_id:
        raise PasskeyVerificationError("WebAuthn credential identifier mismatch")
    row = db.query(WebAuthnCredential).filter(
        WebAuthnCredential.credential_id == credential_id
    ).with_for_update().one_or_none()
    if row is not None and row.account_id != account.id:
        raise PasskeyConflict("This passkey is already registered to another account")
    raw_transports = payload.get("response", {}).get("transports", [])
    transports = sorted({
        str(item).strip().lower() for item in raw_transports
        if isinstance(item, str) and 0 < len(item.strip()) <= 32
    })[:16]
    created = row is None
    if row is None:
        row = WebAuthnCredential(
            id=str(uuid.uuid4()),
            account_id=account.id,
            credential_id=credential_id,
            public_key=verification.credential_public_key,
            sign_count=int(verification.sign_count),
            transports=transports,
            device_type=str(verification.credential_device_type.value),
            backed_up=bool(verification.credential_backed_up),
            label=_bounded_label(label),
            state="active",
            last_used_at=None,
            revoked_at=None,
        )
        db.add(row)
    else:
        row.public_key = verification.credential_public_key
        row.sign_count = int(verification.sign_count)
        row.transports = transports
        row.device_type = str(verification.credential_device_type.value)
        row.backed_up = bool(verification.credential_backed_up)
        row.label = _bounded_label(label)
        row.state = "active"
        row.revoked_at = None
    ceremony.consumed_at = clock
    db.flush()
    append_action_audit(
        db,
        owner_id=account.id,
        action="auth.passkey.registered",
        entity_type="webauthn_credential",
        entity_id=row.id,
        reason="Owner completed a server-verified WebAuthn registration",
        before_state=None if created else {"state": "revoked"},
        after_state={
            "state": "active",
            "device_type": row.device_type,
            "backed_up": row.backed_up,
        },
        details={"user_verification_required": True, "attestation": "none"},
    )
    return serialize_credential(row)


def begin_unlock(
    db,
    *,
    account: Account,
    auth_session_id: str,
    context: RelyingPartyContext,
    now: datetime | None = None,
) -> dict[str, Any]:
    clock = _now(now)
    _session(
        db, account_id=account.id, auth_session_id=auth_session_id,
        now=clock, lock=True,
    )
    credentials = _active_credentials(db, account.id)
    if not credentials:
        raise PasskeyNotFound("Register a passkey before verifying this device")
    options = generate_authentication_options(
        rp_id=context.rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(item.credential_id))
            for item in credentials
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
        timeout=CHALLENGE_TTL_SECONDS * 1000,
    )
    ceremony = _new_challenge(
        db,
        account_id=account.id,
        auth_session_id=auth_session_id,
        purpose="unlock",
        challenge=options.challenge,
        context=context,
        now=clock,
    )
    return {
        "ceremony_id": ceremony.id,
        "options": json.loads(options_to_json(options)),
        "expires_at": ceremony.expires_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def complete_unlock(
    db,
    *,
    account: Account,
    auth_session_id: str,
    ceremony_id: object,
    credential: object,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    clock = _now(now)
    ceremony = _challenge(
        db,
        challenge_id=ceremony_id,
        account_id=account.id,
        auth_session_id=auth_session_id,
        purpose="unlock",
        now=clock,
    )
    payload = _bounded_credential(credential)
    credential_id = str(payload.get("id") or "")
    if not credential_id or len(credential_id) > 2048:
        raise PasskeyVerificationError("WebAuthn credential identifier is invalid")
    row = db.query(WebAuthnCredential).filter(
        WebAuthnCredential.account_id == account.id,
        WebAuthnCredential.credential_id == credential_id,
        WebAuthnCredential.state == "active",
        WebAuthnCredential.revoked_at.is_(None),
    ).with_for_update().one_or_none()
    if row is None:
        raise PasskeyVerificationError("WebAuthn credential is not registered")
    try:
        verification = verify_authentication_response(
            credential=payload,
            expected_challenge=base64url_to_bytes(ceremony.challenge),
            expected_rp_id=ceremony.rp_id,
            expected_origin=ceremony.expected_origin,
            credential_public_key=bytes(row.public_key),
            credential_current_sign_count=int(row.sign_count or 0),
            require_user_verification=True,
        )
    except WebAuthnException as exc:
        raise PasskeyVerificationError("WebAuthn device verification failed") from exc
    if not verification.user_verified:
        raise PasskeyVerificationError("The authenticator did not verify the user")
    session = _session(
        db,
        account_id=account.id,
        auth_session_id=auth_session_id,
        now=clock,
        lock=True,
    )
    row.sign_count = int(verification.new_sign_count)
    row.device_type = str(verification.credential_device_type.value)
    row.backed_up = bool(verification.credential_backed_up)
    row.last_used_at = clock
    verified_until = clock + timedelta(seconds=_verification_ttl(environ))
    session.user_verified_at = clock
    session.user_verification_expires_at = verified_until
    session.user_verification_method = "webauthn"
    session.user_verification_credential_id = row.id
    ceremony.consumed_at = clock
    db.flush()
    append_action_audit(
        db,
        owner_id=account.id,
        action="auth.passkey.user_verified",
        entity_type="auth_session",
        entity_id=session.id,
        reason="Owner completed a server-verified WebAuthn assertion",
        before_state={"user_verified": False},
        after_state={
            "user_verified": True,
            "expires_at": verified_until.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        details={"credential_id": row.id, "method": "webauthn"},
    )
    return session_verification_state(
        db,
        account_id=account.id,
        auth_session_id=session.id,
        now=clock,
    )


def session_verification_state(
    db,
    *,
    account_id: str,
    auth_session_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    clock = _now(now)
    row = db.query(AuthSession).filter(
        AuthSession.id == auth_session_id,
        AuthSession.account_id == account_id,
        AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > clock,
    ).one_or_none()
    valid = bool(
        row is not None
        and row.user_verified_at is not None
        and row.user_verification_expires_at is not None
        and row.user_verification_expires_at > clock
        and row.user_verification_method == "webauthn"
        and row.user_verification_credential_id
    )
    return {
        "verified": valid,
        "method": row.user_verification_method if valid else None,
        "verified_at": (
            row.user_verified_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
            if valid else None
        ),
        "expires_at": (
            row.user_verification_expires_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
            if valid else None
        ),
        "credential_id": row.user_verification_credential_id if valid else None,
    }


def list_credentials(db, *, account_id: str) -> list[dict[str, Any]]:
    rows = db.query(WebAuthnCredential).filter(
        WebAuthnCredential.account_id == account_id,
        WebAuthnCredential.state == "active",
        WebAuthnCredential.revoked_at.is_(None),
    ).order_by(
        WebAuthnCredential.last_used_at.desc(),
        WebAuthnCredential.created_at.desc(),
        WebAuthnCredential.id.desc(),
    ).limit(MAX_ACTIVE_CREDENTIALS).all()
    return [serialize_credential(row) for row in rows]


def serialize_credential(row: WebAuthnCredential) -> dict[str, Any]:
    def iso(value: datetime | None) -> str | None:
        return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if value else None

    return {
        "id": row.id,
        "label": row.label,
        "device_type": row.device_type,
        "backed_up": bool(row.backed_up),
        "transports": list(row.transports or []),
        "created_at": iso(row.created_at),
        "last_used_at": iso(row.last_used_at),
    }


def revoke_credential(
    db,
    *,
    account: Account,
    credential_id: object,
    now: datetime | None = None,
) -> dict[str, Any]:
    clock = _now(now)
    identifier = str(credential_id or "").strip()
    row = db.query(WebAuthnCredential).filter(
        WebAuthnCredential.id == identifier,
        WebAuthnCredential.account_id == account.id,
        WebAuthnCredential.state == "active",
        WebAuthnCredential.revoked_at.is_(None),
    ).with_for_update().one_or_none()
    if row is None:
        raise PasskeyNotFound("Passkey not found")
    row.state = "revoked"
    row.revoked_at = clock
    db.query(AuthSession).filter(
        AuthSession.account_id == account.id,
        AuthSession.user_verification_credential_id == row.id,
    ).update({
        AuthSession.user_verified_at: None,
        AuthSession.user_verification_expires_at: None,
        AuthSession.user_verification_method: None,
        AuthSession.user_verification_credential_id: None,
    }, synchronize_session="fetch")
    append_action_audit(
        db,
        owner_id=account.id,
        action="auth.passkey.revoked",
        entity_type="webauthn_credential",
        entity_id=row.id,
        reason="Owner revoked a passkey after password and MFA step-up",
        before_state={"state": "active"},
        after_state={"state": "revoked"},
        details={"verification_grants_cleared": True},
    )
    return {"id": row.id, "revoked": True}


__all__ = [
    "PasskeyConflict",
    "PasskeyError",
    "PasskeyNotFound",
    "PasskeyVerificationError",
    "RelyingPartyContext",
    "begin_registration",
    "begin_unlock",
    "complete_registration",
    "complete_unlock",
    "list_credentials",
    "relying_party_context",
    "revoke_credential",
    "serialize_credential",
    "session_verification_state",
]
