"""
secret_storage.py

Fernet-based symmetric encryption for secrets stored in the SQLite DB
(IMAP / SMTP passwords today; safe to extend). The key lives at
`data/.app_key`, mode 0o600, generated on first call. `data/` is
gitignored so the key never ships with the repo.

Threat model: protects against SQLite-file exfiltration (stolen
backup, leaked container layer, sibling-tenant read). Does **not**
protect against a process compromise — anyone who can read this
module's memory or the key file has plaintext.

Encrypted values carry an `enc:` prefix so the migration is
idempotent: passing an already-encrypted value to `encrypt()` is a
no-op; passing a plaintext value to `decrypt()` returns it
unchanged. That lets legacy rows coexist with new ones until a
single migration pass rewrites them.
"""

import os
import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from src.constants import APP_KEY_FILE

logger = logging.getLogger(__name__)

_KEY_PATH = Path(APP_KEY_FILE)
_PREFIX = "enc:"
_fernet: Fernet | None = None


def _harden_key_permissions(path: Path) -> None:
    """Apply owner-only POSIX permissions without importing ``core``.

    Importing ``core.platform_compat`` first executes ``core.__init__``, which
    imports the database and can re-enter this module while it is only partly
    initialized. Secret storage is deliberately a low-level dependency, so the
    tiny chmod operation stays stdlib-only here.
    """
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Matches the existing cross-platform helper: permission hardening is
        # best-effort because some mounted filesystems reject chmod.
        pass


def _configured_key_path() -> tuple[Path, bool]:
    configured = (
        os.getenv("RESTIA_ENCRYPTION_KEY_FILE")
        or os.getenv("ODYSSEUS_ENCRYPTION_KEY_FILE")
        or ""
    ).strip()
    return (Path(configured).expanduser(), True) if configured else (_KEY_PATH, False)


def _load_or_create_key() -> bytes:
    inline = (
        os.getenv("RESTIA_ENCRYPTION_KEY")
        or os.getenv("ODYSSEUS_ENCRYPTION_KEY")
        or ""
    ).strip()
    if inline:
        # Fernet validates the material in _get_fernet(). Shared deployments
        # use this explicit key (or the file override below) so every process
        # can decrypt the same rows; the secret is never written to disk here.
        return inline.encode("ascii")

    key_path, explicitly_configured = _configured_key_path()
    if key_path.exists():
        return key_path.read_bytes()
    if explicitly_configured:
        raise FileNotFoundError(
            "RESTIA_ENCRYPTION_KEY_FILE must name a readable existing file"
        )
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    key_path.write_bytes(key)
    # POSIX: lock the key to 0o600. Windows: no-op (the user-profile data dir is
    # already ACL-restricted); safe_chmod swallows both cases.
    _harden_key_permissions(key_path)
    logger.info("Generated new app key at %s", key_path)
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Empty input passes through. Already-encrypted
    values pass through unchanged so re-encrypting is a no-op."""
    if not plaintext:
        return plaintext or ""
    if plaintext.startswith(_PREFIX):
        return plaintext
    token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(value: str) -> str:
    """Decrypt an `enc:`-prefixed value. Plaintext (legacy) passes
    through unchanged. Returns "" on decryption failure so a corrupt
    or rotated-key row degrades to "unconfigured" rather than 500."""
    if not value:
        return value or ""
    if not value.startswith(_PREFIX):
        return value
    try:
        return _get_fernet().decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("Failed to decrypt stored secret — wrong key or corrupt token")
        return ""
    except Exception as e:
        logger.error(f"Decrypt failure: {e}")
        return ""


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(_PREFIX)
