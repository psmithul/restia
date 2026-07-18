"""Owner-scoped, honest security-posture reporting for Restia V3.

The posture report exposes controls and gaps without returning credential
material.  It intentionally distinguishes an implemented control from an
operator action (for example, the encrypted-backup command exists, but a
backup is not reported as current until a valid recent archive is present).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.database import (
    Account,
    ActionAudit,
    ActionProposal,
    AuthSession,
    MfaFactor,
)
from src.ambient_capabilities import list_ambient_capabilities
from src.backup_encryption import BackupEncryptionError, inspect_encrypted_backup
from src.integration_permissions import normalize_integration_permissions
from src.profile_configuration_models import ProfileConfiguration
from src.profile_configuration_service import serialize_configuration


DEFAULT_BACKUP_MAX_AGE = timedelta(days=7)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _backup_posture(
    backup_dir: Path | None,
    *,
    now: datetime,
    max_age: timedelta,
) -> dict[str, Any]:
    directory = backup_dir or Path(__file__).resolve().parents[1] / "backups"
    candidates: list[Path] = []
    if directory.is_dir():
        try:
            candidates = sorted(
                (
                    item
                    for item in directory.iterdir()
                    if item.name.endswith(".restia")
                    and item.is_file()
                    and not item.is_symlink()
                ),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[:100]
        except OSError:
            candidates = []

    latest: dict[str, Any] | None = None
    for candidate in candidates:
        try:
            info = inspect_encrypted_backup(candidate)
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
        except (BackupEncryptionError, OSError, ValueError):
            continue
        age_seconds = max(0.0, (now - modified).total_seconds())
        latest = {
            "created_at": _iso(modified),
            "age_days": round(age_seconds / 86_400, 2),
            "algorithm": info.algorithm,
            "format_version": info.version,
            "encrypted_size": info.encrypted_size,
        }
        break

    current = bool(
        latest is not None
        and float(latest["age_days"]) <= max_age.total_seconds() / 86_400
    )
    return {
        "supported": True,
        "current": current,
        "latest": latest,
        "recommended_max_age_days": int(max_age.total_seconds() / 86_400),
        "command": (
            "./scripts/odysseus-backup snapshot "
            "--encrypt-with-passphrase-file /private/path/restia-backup-passphrase"
        ),
    }


def build_security_posture(
    db,
    *,
    account: Account,
    backup_dir: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return security controls for one account without exposing secrets."""

    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    db_now = observed_at.astimezone(timezone.utc).replace(tzinfo=None)

    active_sessions = db.query(AuthSession).filter(
        AuthSession.account_id == account.id,
        AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > db_now,
        AuthSession.auth_epoch == account.auth_epoch,
    ).count()
    mfa_enabled = db.query(MfaFactor).filter(
        MfaFactor.account_id == account.id,
        MfaFactor.kind == "totp",
        MfaFactor.state == "active",
    ).count() > 0

    integration_rows = db.query(ProfileConfiguration).filter(
        ProfileConfiguration.owner_id == account.id,
        ProfileConfiguration.namespace == "integration",
        ProfileConfiguration.state == "active",
    ).all()
    write_connectors = 0
    invalid_connectors = 0
    for row in integration_rows:
        try:
            value = serialize_configuration(row)["value"]
            permissions = normalize_integration_permissions(value.get("permissions"))
        except (AttributeError, TypeError, ValueError):
            invalid_connectors += 1
            continue
        if set(permissions["allowed_methods"]) - {"GET"}:
            write_connectors += 1

    ambient = list_ambient_capabilities(db, owner_id=account.id)
    enabled_ambient = sum(1 for item in ambient if item["enabled"])
    device_unlock_controls = sum(
        1 for item in ambient if item["enabled"] and item["require_device_unlock"]
    )
    audits = db.query(ActionAudit).filter(ActionAudit.owner_id == account.id).count()
    visible_actions = db.query(ActionProposal).filter(
        ActionProposal.owner_id == account.id,
        ActionProposal.state.in_(("prepared", "approved", "executing", "failed")),
    ).count()

    backups = _backup_posture(
        Path(backup_dir) if backup_dir is not None else None,
        now=observed_at,
        max_age=DEFAULT_BACKUP_MAX_AGE,
    )
    checks = [
        {
            "id": "encryption_and_key_separation",
            "label": "Encrypted private data",
            "status": "protected",
            "detail": (
                "Sensitive SQL fields use authenticated encryption; password, "
                "session, idempotency, and confirmation credentials use separate "
                "one-way derivation contexts."
            ),
        },
        {
            "id": "secure_auth",
            "label": "Secure authentication",
            "status": "protected" if mfa_enabled else "attention",
            "detail": (
                "TOTP is enabled for this account."
                if mfa_enabled
                else "Password authentication is active; enable TOTP for a second factor."
            ),
        },
        {
            "id": "device_management",
            "label": "Devices and sessions",
            "status": "protected",
            "detail": (
                f"{active_sessions} active database-backed session(s); each can be "
                "reviewed and revoked by this account."
            ),
        },
        {
            "id": "biometric_device_controls",
            "label": "Device-unlock controls",
            "status": "available",
            "detail": (
                f"{device_unlock_controls} of {enabled_ambient} enabled ambient "
                "capability grant(s) require device unlock; biometric verification "
                "is performed by the trusted client platform."
            ),
        },
        {
            "id": "connector_permissions",
            "label": "Connector permissions",
            "status": "protected" if invalid_connectors == 0 else "attention",
            "detail": (
                f"{len(integration_rows)} connector(s), {write_connectors} with "
                "approved-write grants, and "
                f"{invalid_connectors} invalid permission contract(s)."
            ),
        },
        {
            "id": "audit_and_agent_visibility",
            "label": "Audit and agent visibility",
            "status": "protected",
            "detail": (
                f"{audits} append-only audit record(s); {visible_actions} action "
                "proposal(s) currently need review or resolution."
            ),
        },
        {
            "id": "export",
            "label": "Portable private-data export",
            "status": "available",
            "detail": "Owner-scoped export excludes credentials and reports truncation explicitly.",
            "endpoint": "/api/life/privacy-export",
        },
        {
            "id": "encrypted_backups",
            "label": "Encrypted backups",
            "status": "protected" if backups["current"] else "attention",
            "detail": (
                "A valid encrypted backup is recent."
                if backups["current"]
                else "Create and verify an encrypted backup; none is current within 7 days."
            ),
        },
        {
            "id": "private_data_no_training",
            "label": "Private data and external actions",
            "status": "protected",
            "detail": (
                "Ambient evidence is private/no-training, WhatsApp is read-only, "
                "and external or high-risk actions require policy approval."
            ),
        },
    ]
    attention = sum(1 for item in checks if item["status"] == "attention")
    return {
        "schema": "restia.v3.security-posture",
        "schema_version": 1,
        "generated_at": _iso(observed_at),
        "overall": "attention" if attention else "protected",
        "attention_count": attention,
        "checks": checks,
        "backups": backups,
        "links": {
            "sessions": "/api/auth/sessions",
            "audit": "/api/life/audit",
            "privacy_export": "/api/life/privacy-export",
            "ambient_capabilities": "/api/life/ambient/capabilities",
        },
    }


__all__ = ["DEFAULT_BACKUP_MAX_AGE", "build_security_posture"]
