from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from src.backup_encryption import (
    BackupAuthenticationError,
    BackupDestinationExists,
    BackupEncryptionError,
    InvalidBackupFormat,
    decrypt_backup,
    encrypt_backup,
    inspect_encrypted_backup,
    is_encrypted_backup,
)


PASSPHRASE = "correct horse battery staple"


def test_streaming_round_trip_and_private_destination(tmp_path: Path) -> None:
    source = tmp_path / "restia-backup.tar.gz"
    payload = os.urandom((2 * 1024 * 1024) + 137)
    source.write_bytes(payload)
    encrypted = tmp_path / "restia-backup.tar.gz.restia"
    restored = tmp_path / "restored.tar.gz"

    info = encrypt_backup(source, encrypted, PASSPHRASE, chunk_size=64 * 1024)

    assert info.algorithm == "AES-256-GCM"
    assert info.plaintext_size == len(payload)
    assert info.scrypt_n == 2**15
    assert is_encrypted_backup(encrypted) is True
    assert stat.S_IMODE(encrypted.stat().st_mode) == 0o600
    assert payload[:128] not in encrypted.read_bytes()

    assert decrypt_backup(
        encrypted, restored, PASSPHRASE, chunk_size=31 * 1024
    ) == restored
    assert restored.read_bytes() == payload
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600


def test_wrong_passphrase_never_publishes_plaintext(tmp_path: Path) -> None:
    source = tmp_path / "backup.tar.gz"
    source.write_bytes(b"private restia data" * 1000)
    encrypted = tmp_path / "backup.restia"
    destination = tmp_path / "should-not-exist.tar.gz"
    encrypt_backup(source, encrypted, PASSPHRASE)

    with pytest.raises(BackupAuthenticationError, match="authentication failed"):
        decrypt_backup(encrypted, destination, "different password value")

    assert not destination.exists()
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


def test_tampering_is_detected_and_destination_is_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "backup.tar.gz"
    source.write_bytes(os.urandom(4096))
    encrypted = tmp_path / "backup.restia"
    destination = tmp_path / "existing.tar.gz"
    destination.write_bytes(b"keep this")
    encrypt_backup(source, encrypted, PASSPHRASE)
    tampered = bytearray(encrypted.read_bytes())
    tampered[-17] ^= 0x01
    encrypted.write_bytes(tampered)

    with pytest.raises(BackupAuthenticationError):
        decrypt_backup(
            encrypted,
            destination,
            PASSPHRASE,
            overwrite=True,
            chunk_size=97,
        )

    assert destination.read_bytes() == b"keep this"


def test_truncated_and_plain_files_are_rejected(tmp_path: Path) -> None:
    plain = tmp_path / "plain.tar.gz"
    plain.write_bytes(b"not encrypted")
    truncated = tmp_path / "truncated.restia"
    truncated.write_bytes(b"RESTIABK\x01")

    assert is_encrypted_backup(plain) is False
    assert is_encrypted_backup(truncated) is False
    with pytest.raises(InvalidBackupFormat):
        inspect_encrypted_backup(truncated)


def test_destination_requires_explicit_overwrite_and_distinct_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "backup.tar.gz"
    source.write_bytes(b"payload")
    destination = tmp_path / "backup.restia"
    destination.write_bytes(b"existing")

    with pytest.raises(BackupDestinationExists):
        encrypt_backup(source, destination, PASSPHRASE)
    assert destination.read_bytes() == b"existing"

    with pytest.raises(BackupEncryptionError, match="different files"):
        encrypt_backup(source, source, PASSPHRASE, overwrite=True)


def test_no_clobber_is_atomic_when_destination_appears_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "backup.tar.gz"
    source.write_bytes(b"payload" * 100)
    destination = tmp_path / "backup.restia"
    real_link = os.link

    def race_link(temporary, target):
        Path(target).write_bytes(b"concurrent backup")
        return real_link(temporary, target)

    monkeypatch.setattr(os, "link", race_link)
    with pytest.raises(BackupDestinationExists):
        encrypt_backup(source, destination, PASSPHRASE)

    assert destination.read_bytes() == b"concurrent backup"
    assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))


def test_passphrase_and_chunk_contract_is_strict(tmp_path: Path) -> None:
    source = tmp_path / "backup.tar.gz"
    source.write_bytes(b"payload")

    with pytest.raises(ValueError, match="at least 12"):
        encrypt_backup(source, tmp_path / "short.restia", "too short")
    with pytest.raises(ValueError, match="positive"):
        encrypt_backup(
            source, tmp_path / "bad-chunk.restia", PASSPHRASE, chunk_size=0
        )


def test_backup_code_does_not_depend_on_path_whole_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "backup.tar.gz"
    source.write_bytes(os.urandom(128 * 1024))
    encrypted = tmp_path / "backup.restia"
    restored = tmp_path / "restored.tar.gz"

    def fail_read_bytes(_path: Path) -> bytes:
        raise AssertionError("whole-file Path.read_bytes() must not be used")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    encrypt_backup(source, encrypted, PASSPHRASE, chunk_size=4096)
    decrypt_backup(encrypted, restored, PASSPHRASE, chunk_size=4096)
    assert restored.stat().st_size == source.stat().st_size
