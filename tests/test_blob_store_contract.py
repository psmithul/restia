from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from src.blob_store import (
    BlobKeyError,
    BlobStoreConfigurationError,
    FileSystemBlobStore,
    resolve_blob_store_config,
)
from src.project_storage import ProjectFileStore


def test_shared_mode_requires_explicit_shared_filesystem_and_absolute_root(tmp_path):
    base = {"RESTIA_DATABASE_MODE": "shared"}
    with pytest.raises(BlobStoreConfigurationError, match="shared-filesystem"):
        resolve_blob_store_config(environ=base)
    with pytest.raises(BlobStoreConfigurationError, match="absolute"):
        resolve_blob_store_config(environ={
            **base,
            "RESTIA_BLOB_STORE": "shared-filesystem",
            "RESTIA_BLOB_ROOT": "relative/blobs",
        })
    with pytest.raises(BlobStoreConfigurationError, match="filesystem root"):
        resolve_blob_store_config(environ={
            **base,
            "RESTIA_BLOB_STORE": "shared-filesystem",
            "RESTIA_BLOB_ROOT": "/",
        })

    config = resolve_blob_store_config(
        environ={
            **base,
            "RESTIA_BLOB_STORE": "shared-filesystem",
            "RESTIA_BLOB_ROOT": str(tmp_path / "shared"),
        },
        create=True,
    )
    assert config.shared is True
    assert config.chat_root == (tmp_path / "shared" / "uploads").resolve()
    assert config.project_root == (tmp_path / "shared" / "project_files").resolve()
    assert config.chat_root.is_dir()
    assert config.project_root.is_dir()


def test_local_mode_preserves_existing_namespaces_and_allows_explicit_root(tmp_path):
    config = resolve_blob_store_config(
        environ={
            "RESTIA_DATABASE_MODE": "local-single",
            "RESTIA_BLOB_STORE": "local-filesystem",
            "RESTIA_BLOB_ROOT": str(tmp_path / "local"),
        },
        create=True,
    )
    assert config.shared is False
    assert config.chat_root.name == "uploads"
    assert config.project_root.name == "project_files"


def test_filesystem_store_confines_keys_and_rejects_symlink_escape(tmp_path):
    store = FileSystemBlobStore(tmp_path / "blobs")
    path = store.write_stream("2026/07/item.bin", io.BytesIO(b"safe"))
    assert path.read_bytes() == b"safe"
    assert store.resolve("2026/07/item.bin") == path

    for unsafe in ("../outside", "/etc/passwd", "a/../../b", "a\\b"):
        with pytest.raises(BlobKeyError):
            store.resolve(unsafe, must_exist=False)

    outside = tmp_path / "outside"
    outside.mkdir()
    link = store.root / "escape"
    try:
        os.symlink(outside, link)
    except (AttributeError, NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(BlobKeyError):
        store.resolve("escape/private.bin", must_exist=False)


def test_project_file_store_uses_same_validated_shared_root(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "shared")
    monkeypatch.setenv("RESTIA_BLOB_STORE", "shared-filesystem")
    monkeypatch.setenv("RESTIA_BLOB_ROOT", str(tmp_path / "shared"))
    store = ProjectFileStore()
    assert store.root == (tmp_path / "shared" / "project_files").resolve()
