"""Domain-separated key material for database-backed authentication.

Restia already has one stable 256-bit Fernet master key for encrypted data.
Database session and API-token lookup uses a separate HMAC subkey derived from
that master key, so the encryption key bytes are never reused directly as a
token digest key.  Shared deployments therefore need only the existing
``RESTIA_ENCRYPTION_KEY`` (or file) secret, while every API replica derives the
same authentication key deterministically.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac


_AUTH_TOKEN_KDF_CONTEXT = b"restia/auth-token-hmac/v1"
_MASTER_KEY_BYTES = 32


class AuthKeyConfigurationError(RuntimeError):
    """Raised when the configured Restia master key is not usable."""


def derive_auth_token_hmac_key(fernet_key: bytes) -> bytes:
    """Derive a stable, domain-separated 256-bit token HMAC key."""

    if not isinstance(fernet_key, bytes):
        raise AuthKeyConfigurationError("Restia encryption key must be bytes")
    encoded = fernet_key.strip()
    try:
        decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AuthKeyConfigurationError(
            "Restia encryption key is not valid Fernet key material"
        ) from exc
    if len(decoded) != _MASTER_KEY_BYTES:
        raise AuthKeyConfigurationError(
            "Restia encryption key is not valid Fernet key material"
        )
    return hmac.new(
        decoded,
        _AUTH_TOKEN_KDF_CONTEXT,
        hashlib.sha256,
    ).digest()


def load_auth_token_hmac_key() -> bytes:
    """Load/create Restia's master key and derive the auth-token subkey.

    The underlying loader already honors the explicit shared-deployment
    environment variables and creates an owner-only local key when appropriate.
    Import it lazily to keep this low-level module free of database side effects.
    """

    from src.secret_storage import _load_or_create_key

    return derive_auth_token_hmac_key(_load_or_create_key())


__all__ = [
    "AuthKeyConfigurationError",
    "derive_auth_token_hmac_key",
    "load_auth_token_hmac_key",
]
