from __future__ import annotations

from cryptography.fernet import Fernet
from concurrent.futures import ThreadPoolExecutor
import stat
import pytest

from src.auth_keyring import (
    AuthKeyConfigurationError,
    derive_auth_token_hmac_key,
    load_auth_token_hmac_key,
)


def test_auth_token_key_is_deterministic_domain_separated_and_256_bit():
    master = Fernet.generate_key()

    first = derive_auth_token_hmac_key(master)
    second = derive_auth_token_hmac_key(master + b"\n")

    assert first == second
    assert len(first) == 32
    assert first != master


def test_different_master_keys_derive_different_auth_keys():
    assert derive_auth_token_hmac_key(
        Fernet.generate_key()
    ) != derive_auth_token_hmac_key(Fernet.generate_key())


@pytest.mark.parametrize(
    "value",
    [None, "not-bytes", b"", b"not-a-fernet-key", b"YWJjZA=="],
)
def test_invalid_master_key_fails_loudly(value):
    with pytest.raises(AuthKeyConfigurationError):
        derive_auth_token_hmac_key(value)


def test_loader_uses_existing_restia_key_source_without_second_secret(
    monkeypatch,
    tmp_path,
):
    from src import secret_storage

    master = Fernet.generate_key()
    key_path = tmp_path / "shared-fernet.key"
    key_path.write_bytes(master)
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY_FILE", str(key_path))
    monkeypatch.delenv("RESTIA_ENCRYPTION_KEY", raising=False)

    assert load_auth_token_hmac_key() == derive_auth_token_hmac_key(master)


def test_first_start_key_creation_is_exclusive_across_workers(
    monkeypatch,
    tmp_path,
):
    from src import secret_storage

    key_path = tmp_path / ".app_key"
    monkeypatch.delenv("RESTIA_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("RESTIA_ENCRYPTION_KEY_FILE", raising=False)
    monkeypatch.setattr(secret_storage, "_KEY_PATH", key_path)
    monkeypatch.setattr(secret_storage, "_fernet", None)

    with ThreadPoolExecutor(max_workers=16) as pool:
        keys = list(pool.map(lambda _index: secret_storage._load_or_create_key(), range(64)))

    assert len(set(keys)) == 1
    assert key_path.read_bytes() == keys[0]
    Fernet(keys[0])
    if __import__("os").name != "nt":
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
