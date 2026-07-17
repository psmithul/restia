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

Encrypted values carry an `enc:` prefix. Credential/secret paths reserve that
prefix and :func:`encrypt` preserves it byte-for-byte, including malformed or
wrong-key envelopes, so recovery with the original key remains possible and
corruption continues to fail closed. Arbitrary content uses
:func:`encrypt_plaintext` instead; it wraps the exact text even when it begins
with ``enc:`` or is itself a complete Fernet-looking token. Legacy plaintext
still passes through :func:`decrypt` unchanged until a migration rewrites it.
"""

import base64
import binascii
import os
import logging
import stat
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from src.constants import APP_KEY_FILE

logger = logging.getLogger(__name__)

_KEY_PATH = Path(APP_KEY_FILE)
_PREFIX = "enc:"
_CONTENT_PREFIX = "enc:c1:"
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
        if not explicitly_configured:
            info = key_path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise RuntimeError("Local Restia encryption key is not a regular file")
        return key_path.read_bytes()
    if explicitly_configured:
        raise FileNotFoundError(
            "RESTIA_ENCRYPTION_KEY_FILE must name a readable existing file"
        )
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd, temporary_name = tempfile.mkstemp(
        prefix=key_path.name + ".tmp.",
        dir=key_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        written = 0
        while written < len(key):
            count = os.write(fd, key[written:])
            if count <= 0:
                raise OSError("short write while creating Restia encryption key")
            written += count
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            # Linking a fully-fsynced same-directory temporary file publishes
            # it atomically and fails if another process already published its
            # own complete key. No process can ever observe an empty/partial
            # final key path.
            os.link(temporary_path, key_path, follow_symlinks=False)
            won_creation = True
        except FileExistsError:
            won_creation = False
        info = key_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Local Restia encryption key is not a regular file")
        winner = key_path.read_bytes()
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    _harden_key_permissions(key_path)
    if os.name != "nt":
        try:
            directory_fd = os.open(key_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # The file itself is durable and owner-only. Some network mounts
            # reject directory fsync; subsequent reads still validate Fernet.
            pass
    if won_creation:
        logger.info("Generated new app key at %s", key_path)
    return winner


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def _envelope_token(value: str) -> str | None:
    if value.startswith(_CONTENT_PREFIX):
        return value[len(_CONTENT_PREFIX):]
    if value.startswith(_PREFIX):
        return value[len(_PREFIX):]
    return None


def _has_fernet_envelope_shape(value: str) -> bool:
    """Recognize a Fernet envelope without consulting the active key.

    Key-based recognition is destructive during rotation: a perfectly valid
    token from the previous key would look like plaintext and get wrapped a
    second time.  Fernet has a stable binary frame (version, timestamp, IV,
    block-aligned ciphertext, HMAC), which is enough to distinguish stored
    envelopes from ordinary text such as ``enc:private note`` while preserving
    wrong-key ciphertext byte-for-byte.
    """
    if not value:
        return False
    token = _envelope_token(value)
    if token is None:
        return False
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return False
    # 1 version + 8 timestamp + 16 IV + >=16 ciphertext + 32 HMAC.
    return (
        len(raw) >= 73
        and raw[0] == 0x80
        and (len(raw) - 57) % 16 == 0
    )


def encrypt_plaintext(plaintext: str) -> str:
    """Encrypt an application plaintext value even when it resembles a token.

    Arbitrary content columns use this function because a user may paste a
    literal Fernet-looking string. Secret/credential migrations use
    :func:`encrypt` instead so existing envelopes remain idempotent across key
    rotation.
    """
    if not plaintext:
        return plaintext or ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _CONTENT_PREFIX + token


def encrypt(plaintext: str) -> str:
    """Encrypt a credential/secret string with fail-closed idempotency.

    Any reserved ``enc:`` value passes through unchanged. This conservative
    rule is essential for a wrong-key or frame-damaged token: wrapping the bad
    envelope as plaintext would turn a safe empty read into token text and can
    permanently destroy recovery. User-authored content must use
    :func:`encrypt_plaintext` (normally through ``EncryptedContentText``).
    """
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
    token = _envelope_token(value)
    if token is None:
        return value
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("Failed to decrypt stored secret — wrong key or corrupt token")
        return ""
    except Exception as e:
        logger.error(f"Decrypt failure: {e}")
        return ""


def is_encrypted(value: str) -> bool:
    """Return whether ``value`` has a structurally valid Fernet envelope.

    Prefix-only checks are unsafe for user-authored encrypted text columns:
    ``enc:hello`` is plaintext. Key-based checks are also unsafe because they
    would rewrite ciphertext from a previous key. Callers that need to prove
    the active key can decrypt a token must use :func:`is_decryptable`.
    """
    return _has_fernet_envelope_shape(value)


def is_content_encrypted(value: str) -> bool:
    """Return whether ``value`` is a V3 content envelope."""
    return bool(value) and value.startswith(_CONTENT_PREFIX) and is_encrypted(value)


def is_decryptable(value: str) -> bool:
    """Return whether a structured envelope authenticates with the active key."""
    if not is_encrypted(value):
        return False
    token = _envelope_token(value)
    if token is None:
        return False
    try:
        _get_fernet().decrypt(token.encode("ascii"))
        return True
    except (InvalidToken, ValueError, UnicodeError):
        return False
