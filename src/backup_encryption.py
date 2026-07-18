"""Streaming, authenticated encryption for Restia backup archives.

This module deliberately has no dependency on the backup CLI.  Callers create a
backup archive first and then pass its path to :func:`encrypt_backup`.  The
encrypted destination is published atomically only after encryption succeeds.

Format v1 uses AES-256-GCM and a key derived from the user supplied passphrase
with scrypt.  Header bytes (including the random salt and nonce) are
authenticated as additional data.  Neither encryption nor decryption loads the
archive into memory in full.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


MAGIC: Final[bytes] = b"RESTIABK"
FORMAT_VERSION: Final[int] = 1
ALGORITHM_AES_256_GCM: Final[int] = 1
SALT_BYTES: Final[int] = 16
NONCE_BYTES: Final[int] = 12
TAG_BYTES: Final[int] = 16
SCRYPT_LOG_N: Final[int] = 15
SCRYPT_R: Final[int] = 8
SCRYPT_P: Final[int] = 1
DEFAULT_CHUNK_SIZE: Final[int] = 1024 * 1024
MIN_PASSPHRASE_BYTES: Final[int] = 12

# magic, version, algorithm, salt length, nonce length, log2(N), r, p,
# plaintext byte length
_HEADER = struct.Struct(">8sBBBBBIIQ")


class BackupEncryptionError(Exception):
    """Base exception for encrypted-backup failures."""


class InvalidBackupFormat(BackupEncryptionError):
    """Raised when a file is not a supported Restia encrypted backup."""


class BackupAuthenticationError(BackupEncryptionError):
    """Raised when the passphrase is wrong or encrypted bytes were modified."""


class BackupDestinationExists(BackupEncryptionError):
    """Raised when atomic publication would replace a file without approval."""


@dataclass(frozen=True, slots=True)
class EncryptedBackupInfo:
    """Non-secret metadata available without decrypting an archive."""

    version: int
    algorithm: str
    plaintext_size: int
    encrypted_size: int
    scrypt_n: int
    scrypt_r: int
    scrypt_p: int


@dataclass(frozen=True, slots=True)
class _ParsedHeader:
    info: EncryptedBackupInfo
    aad: bytes
    nonce: bytes
    salt: bytes
    ciphertext_offset: int
    ciphertext_size: int


def _passphrase_bytes(passphrase: str | bytes) -> bytes:
    if isinstance(passphrase, str):
        encoded = passphrase.encode("utf-8")
    elif isinstance(passphrase, bytes):
        encoded = passphrase
    else:
        raise TypeError("passphrase must be a string or bytes")
    if len(encoded) < MIN_PASSPHRASE_BYTES:
        raise ValueError(
            f"passphrase must contain at least {MIN_PASSPHRASE_BYTES} UTF-8 bytes"
        )
    return encoded


def _derive_key(passphrase: bytes, salt: bytes) -> bytes:
    try:
        return hashlib.scrypt(
            passphrase,
            salt=salt,
            n=1 << SCRYPT_LOG_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=32,
            # OpenSSL's default can be exactly at the estimated boundary and
            # reject an otherwise valid N=2^15, r=8 derivation.
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, MemoryError) as exc:
        raise BackupEncryptionError("unable to derive the backup encryption key") from exc


def _validate_source(source: Path) -> Path:
    if source.is_symlink():
        raise BackupEncryptionError("backup source must not be a symbolic link")
    try:
        resolved = source.resolve(strict=True)
        source_stat = resolved.stat()
    except OSError as exc:
        raise BackupEncryptionError(f"backup source is not readable: {source}") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise BackupEncryptionError("backup source must be a regular file")
    return resolved


def _validate_destination(source: Path, destination: Path, *, overwrite: bool) -> Path:
    if destination.is_symlink():
        raise BackupEncryptionError("backup destination must not be a symbolic link")
    resolved = destination.expanduser().resolve(strict=False)
    if resolved == source:
        raise BackupEncryptionError("backup source and destination must be different files")
    if resolved.exists() and not overwrite:
        raise BackupDestinationExists(f"backup destination already exists: {resolved}")
    if not resolved.parent.exists() or not resolved.parent.is_dir():
        raise BackupEncryptionError(
            f"backup destination directory does not exist: {resolved.parent}"
        )
    return resolved


def _temporary_output(destination: Path) -> tuple[BinaryIO, Path]:
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.chmod(name, 0o600)
        return os.fdopen(descriptor, "w+b"), Path(name)
    except OSError as exc:
        raise BackupEncryptionError(
            f"cannot create a private temporary file in {destination.parent}"
        ) from exc


def _publish_temporary(
    handle: BinaryIO,
    temporary: Path,
    destination: Path,
    *,
    overwrite: bool,
) -> None:
    try:
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        if overwrite:
            os.replace(temporary, destination)
        else:
            # The earlier existence check improves the error message but is
            # not a publication lock.  Hard-linking a complete temporary file
            # into its final name gives us atomic no-clobber semantics even if
            # another backup process creates the destination in between.
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise BackupDestinationExists(
                    f"backup destination already exists: {destination}"
                ) from exc
            temporary.unlink()
        os.chmod(destination, 0o600)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BackupEncryptionError:
        raise
    except OSError as exc:
        raise BackupEncryptionError(f"cannot publish encrypted backup: {destination}") from exc


def _cleanup_temporary(handle: BinaryIO, temporary: Path) -> None:
    try:
        if not handle.closed:
            handle.close()
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_exact(handle: BinaryIO, size: int, description: str) -> bytes:
    value = handle.read(size)
    if len(value) != size:
        raise InvalidBackupFormat(f"encrypted backup has a truncated {description}")
    return value


def _parse_header(handle: BinaryIO, encrypted_size: int) -> _ParsedHeader:
    fixed = _read_exact(handle, _HEADER.size, "header")
    (
        magic,
        version,
        algorithm,
        salt_size,
        nonce_size,
        log_n,
        scrypt_r,
        scrypt_p,
        plaintext_size,
    ) = _HEADER.unpack(fixed)

    if magic != MAGIC:
        raise InvalidBackupFormat("file is not a Restia encrypted backup")
    if version != FORMAT_VERSION:
        raise InvalidBackupFormat(f"unsupported encrypted-backup version: {version}")
    if algorithm != ALGORITHM_AES_256_GCM:
        raise InvalidBackupFormat(f"unsupported encrypted-backup algorithm: {algorithm}")
    if (
        salt_size != SALT_BYTES
        or nonce_size != NONCE_BYTES
        or log_n != SCRYPT_LOG_N
        or scrypt_r != SCRYPT_R
        or scrypt_p != SCRYPT_P
    ):
        raise InvalidBackupFormat("encrypted backup uses unsupported key parameters")

    salt = _read_exact(handle, salt_size, "salt")
    nonce = _read_exact(handle, nonce_size, "nonce")
    aad = fixed + salt + nonce
    ciphertext_offset = len(aad)
    ciphertext_size = encrypted_size - ciphertext_offset - TAG_BYTES
    if ciphertext_size < 0:
        raise InvalidBackupFormat("encrypted backup is truncated before its authentication tag")
    if ciphertext_size != plaintext_size:
        raise InvalidBackupFormat("encrypted backup length does not match its authenticated header")

    return _ParsedHeader(
        info=EncryptedBackupInfo(
            version=version,
            algorithm="AES-256-GCM",
            plaintext_size=plaintext_size,
            encrypted_size=encrypted_size,
            scrypt_n=1 << log_n,
            scrypt_r=scrypt_r,
            scrypt_p=scrypt_p,
        ),
        aad=aad,
        nonce=nonce,
        salt=salt,
        ciphertext_offset=ciphertext_offset,
        ciphertext_size=ciphertext_size,
    )


def inspect_encrypted_backup(path: str | os.PathLike[str]) -> EncryptedBackupInfo:
    """Validate the container structure and return its non-secret metadata.

    This does not authenticate ciphertext; authentication occurs during
    :func:`decrypt_backup` because it requires the passphrase.
    """

    source = _validate_source(Path(path).expanduser())
    encrypted_size = source.stat().st_size
    try:
        with source.open("rb") as handle:
            return _parse_header(handle, encrypted_size).info
    except OSError as exc:
        raise BackupEncryptionError(f"cannot inspect encrypted backup: {source}") from exc


def is_encrypted_backup(path: str | os.PathLike[str]) -> bool:
    """Return whether *path* has a structurally valid Restia encrypted header."""

    try:
        inspect_encrypted_backup(path)
    except (BackupEncryptionError, OSError):
        return False
    return True


def encrypt_backup(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    passphrase: str | bytes,
    *,
    overwrite: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> EncryptedBackupInfo:
    """Encrypt an existing archive to an owner-only, atomically published file."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    source_path = _validate_source(Path(source).expanduser())
    destination_path = _validate_destination(
        source_path, Path(destination), overwrite=overwrite
    )
    secret = _passphrase_bytes(passphrase)
    plaintext_size = source_path.stat().st_size
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    fixed = _HEADER.pack(
        MAGIC,
        FORMAT_VERSION,
        ALGORITHM_AES_256_GCM,
        SALT_BYTES,
        NONCE_BYTES,
        SCRYPT_LOG_N,
        SCRYPT_R,
        SCRYPT_P,
        plaintext_size,
    )
    aad = fixed + salt + nonce
    key = _derive_key(secret, salt)
    output, temporary = _temporary_output(destination_path)
    try:
        encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(aad)
        output.write(aad)
        with source_path.open("rb") as input_handle:
            while chunk := input_handle.read(chunk_size):
                output.write(encryptor.update(chunk))
        output.write(encryptor.finalize())
        output.write(encryptor.tag)
        _publish_temporary(
            output, temporary, destination_path, overwrite=overwrite,
        )
    except BackupEncryptionError:
        _cleanup_temporary(output, temporary)
        raise
    except (OSError, ValueError) as exc:
        _cleanup_temporary(output, temporary)
        raise BackupEncryptionError("backup encryption failed") from exc

    return inspect_encrypted_backup(destination_path)


def decrypt_backup(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    passphrase: str | bytes,
    *,
    overwrite: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Path:
    """Authenticate and decrypt a Restia backup, publishing only on success."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    source_path = _validate_source(Path(source).expanduser())
    destination_path = _validate_destination(
        source_path, Path(destination), overwrite=overwrite
    )
    secret = _passphrase_bytes(passphrase)
    encrypted_size = source_path.stat().st_size
    output, temporary = _temporary_output(destination_path)
    try:
        with source_path.open("rb") as input_handle:
            parsed = _parse_header(input_handle, encrypted_size)
            input_handle.seek(-TAG_BYTES, os.SEEK_END)
            tag = _read_exact(input_handle, TAG_BYTES, "authentication tag")
            input_handle.seek(parsed.ciphertext_offset)
            remaining = parsed.ciphertext_size
            key = _derive_key(secret, parsed.salt)
            decryptor = Cipher(
                algorithms.AES(key), modes.GCM(parsed.nonce, tag)
            ).decryptor()
            decryptor.authenticate_additional_data(parsed.aad)
            while remaining:
                chunk = input_handle.read(min(chunk_size, remaining))
                if not chunk:
                    raise InvalidBackupFormat("encrypted backup ciphertext is truncated")
                remaining -= len(chunk)
                output.write(decryptor.update(chunk))
            output.write(decryptor.finalize())
        if output.tell() != parsed.info.plaintext_size:
            raise InvalidBackupFormat("decrypted backup length is invalid")
        _publish_temporary(
            output, temporary, destination_path, overwrite=overwrite,
        )
    except InvalidTag as exc:
        _cleanup_temporary(output, temporary)
        raise BackupAuthenticationError(
            "backup authentication failed; the passphrase is wrong or the file was modified"
        ) from exc
    except BackupEncryptionError:
        _cleanup_temporary(output, temporary)
        raise
    except (OSError, ValueError) as exc:
        _cleanup_temporary(output, temporary)
        raise BackupEncryptionError("backup decryption failed") from exc

    return destination_path


__all__ = [
    "BackupAuthenticationError",
    "BackupDestinationExists",
    "BackupEncryptionError",
    "EncryptedBackupInfo",
    "InvalidBackupFormat",
    "decrypt_backup",
    "encrypt_backup",
    "inspect_encrypted_backup",
    "is_encrypted_backup",
]
