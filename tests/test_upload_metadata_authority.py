from __future__ import annotations

import asyncio
import hashlib
import io
import json
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import Account, Base
from src.blob_store import FileSystemBlobStore
from src.upload_handler import UploadHandler
from src.upload_metadata_authority import SQLUploadMetadataAuthority
from src.upload_metadata_models import (
    ChatUploadMetadata,
    ChatUploadMetadataImportRun,
)


class _AuthManager:
    is_configured = True

    def is_admin(self, _user):
        return False


class _Request:
    def __init__(self, user: str):
        self.state = SimpleNamespace(current_user=user)
        self.app = SimpleNamespace(
            state=SimpleNamespace(auth_manager=_AuthManager())
        )
        self.client = SimpleNamespace(host="127.0.0.1")


@pytest.fixture()
def upload_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'uploads.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    store = FileSystemBlobStore(tmp_path / "blobs")
    authority = SQLUploadMetadataAuthority(store, session_factory=factory)
    handler = UploadHandler(
        str(tmp_path),
        str(store.root),
        metadata_authority=authority,
        blob_store=store,
        legacy_compat=False,
    )
    yield SimpleNamespace(
        engine=engine, Session=factory, store=store,
        authority=authority, handler=handler, root=tmp_path,
    )
    engine.dispose()


def _upload(name: str, data: bytes):
    return SimpleNamespace(filename=name, file=io.BytesIO(data))


def test_owner_isolation_dedupe_and_private_metadata_encryption(upload_env):
    env = upload_env
    first = env.handler.save_upload(
        _upload("private-alice.txt", b"same bytes"),
        "198.51.100.7",
        owner="alice",
    )
    duplicate = env.handler.save_upload(
        _upload("renamed.txt", b"same bytes"),
        "198.51.100.8",
        owner="alice",
    )
    bob = env.handler.save_upload(
        _upload("private-bob.txt", b"same bytes"),
        "203.0.113.9",
        owner="bob",
    )

    assert duplicate["id"] == first["id"]
    assert duplicate["is_duplicate"] is True
    assert bob["id"] != first["id"]
    assert env.handler.resolve_upload(first["id"], owner="alice") is not None
    assert env.handler.resolve_upload(first["id"], owner="bob") is None

    with env.engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT content_digest, payload FROM chat_upload_metadata "
            "ORDER BY id"
        )).all()
    assert len(rows) == 2
    raw = " ".join(str(value) for row in rows for value in row)
    assert "private-alice" not in raw
    assert "198.51.100.7" not in raw
    assert hashlib.sha256(b"same bytes").hexdigest() not in raw
    assert "enc:c1:" in raw
    assert len({row[0] for row in rows}) == 2


def test_missing_blob_is_repaired_with_cas_and_retired_row_reactivates(upload_env):
    env = upload_env
    original = env.handler.save_upload(
        _upload("repair.txt", b"repair me"), "127.0.0.1", owner="alice"
    )
    Path(original["path"]).unlink()
    repaired = env.handler.save_upload(
        _upload("repair-again.txt", b"repair me"),
        "127.0.0.2",
        owner="alice",
    )
    assert repaired["id"] == original["id"]
    assert Path(repaired["path"]).read_bytes() == b"repair me"

    db = env.Session()
    try:
        row = db.query(ChatUploadMetadata).filter_by(id=original["id"]).one()
        row.retention_until = datetime(2000, 1, 1)
        db.commit()
    finally:
        db.close()
    assert env.authority.cleanup_expired() == 1
    assert env.handler.resolve_upload(original["id"], owner="alice") is None
    restored = env.handler.save_upload(
        _upload("restored.txt", b"repair me"), "127.0.0.3", owner="alice"
    )
    assert restored["id"] == original["id"]
    assert Path(restored["path"]).is_file()


def test_cleanup_delete_cannot_remove_a_concurrent_reactivation(
    upload_env, monkeypatch,
):
    env = upload_env
    original = env.handler.save_upload(
        _upload("race.txt", b"race-safe bytes"),
        "127.0.0.1",
        owner="alice",
    )
    old_path = Path(original["path"])
    db = env.Session()
    try:
        row = db.query(ChatUploadMetadata).filter_by(id=original["id"]).one()
        row.retention_until = datetime(2000, 1, 1)
        db.commit()
    finally:
        db.close()

    delete = env.store.delete
    raced: dict[str, dict] = {}

    def reactivate_then_delete_old(key):
        raced["upload"] = env.handler.save_upload(
            _upload("race-again.txt", b"race-safe bytes"),
            "127.0.0.2",
            owner="alice",
        )
        delete(key)

    monkeypatch.setattr(env.store, "delete", reactivate_then_delete_old)
    assert env.authority.cleanup_expired() == 1
    restored = raced["upload"]
    assert restored["id"] == original["id"]
    assert Path(restored["path"]).is_file()
    assert Path(restored["path"]) != old_path
    assert not old_path.exists()


def _legacy_row(path: Path, upload_id: str, owner: str, data: bytes) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "id": upload_id,
        "path": str(path),
        "hash": hashlib.sha256(data).hexdigest(),
        "mime": "text/plain",
        "size": len(data),
        "name": f"{owner}.txt",
        "original_name": f"private-{owner}.txt",
        "uploaded_at": "2026-07-17T10:00:00",
        "last_accessed": "2026-07-17T10:00:00",
        "client_ip": "192.0.2.1",
        "owner": owner,
    }


def test_legacy_import_is_bounded_idempotent_owner_scoped_and_source_preserved(upload_env):
    env = upload_env
    alice_id = "a" * 32 + ".txt"
    bob_id = "b" * 32 + ".txt"
    index_path = env.store.root / "uploads.json"
    index = {
        "alice": _legacy_row(
            env.store.root / "2026/07/17" / alice_id,
            alice_id, "alice", b"alice legacy",
        ),
        "bob": _legacy_row(
            env.store.root / "2026/07/17" / bob_id,
            bob_id, "bob", b"bob legacy",
        ),
        "unsafe": {
            **_legacy_row(
                env.root / "outside" / ("c" * 32 + ".txt"),
                "c" * 32 + ".txt", "alice", b"outside",
            ),
        },
    }
    original = json.dumps(index)
    index_path.write_text(original, encoding="utf-8")

    first = env.authority.import_legacy_index(
        index_path, max_blob_bytes=1024 * 1024
    )
    second = env.authority.import_legacy_index(
        index_path, max_blob_bytes=1024 * 1024
    )
    assert first.imported == 2
    assert first.skipped == 1
    assert second.imported == 0
    assert index_path.read_text(encoding="utf-8") == original
    assert env.authority.get(alice_id, owner="alice") is not None
    assert env.authority.get(alice_id, owner="bob") is None
    db = env.Session()
    try:
        assert db.query(ChatUploadMetadataImportRun).filter_by(
            state="completed"
        ).count() == 2
    finally:
        db.close()


def test_legacy_import_recomputes_blob_digest_instead_of_trusting_index(upload_env):
    env = upload_env
    upload_id = "e" * 32 + ".txt"
    data = b"actual legacy content"
    row = _legacy_row(
        env.store.root / "2026/07/17" / upload_id,
        upload_id,
        "alice",
        data,
    )
    row["hash"] = "f" * 64
    index_path = env.store.root / "uploads.json"
    index_path.write_text(json.dumps({"alice": row}), encoding="utf-8")

    result = env.authority.import_legacy_index(
        index_path, max_blob_bytes=1024 * 1024,
    )
    assert result.imported == 1
    imported = env.authority.get(upload_id, owner="alice")
    assert imported is not None
    assert imported["hash"] == hashlib.sha256(data).hexdigest()


def test_legacy_import_remaps_copied_absolute_paths_to_shared_root(upload_env):
    env = upload_env
    upload_id = "9" * 32 + ".txt"
    data = b"copied to shared mount"
    old_path = env.root / "old-uploads/2026/07/17" / upload_id
    row = _legacy_row(old_path, upload_id, "alice", data)
    copied = env.store.root / "2026/07/17" / upload_id
    copied.parent.mkdir(parents=True, exist_ok=True)
    copied.write_bytes(data)
    index_path = env.store.root / "uploads.json"
    original = json.dumps({"alice": row})
    index_path.write_text(original, encoding="utf-8")

    result = env.authority.import_legacy_index(
        index_path, max_blob_bytes=1024 * 1024,
    )
    assert result.imported == 1
    imported = env.authority.get(upload_id, owner="alice")
    assert imported is not None
    assert Path(imported["path"]) == copied
    assert index_path.read_text(encoding="utf-8") == original


def test_corrupt_legacy_index_recovers_from_backup_and_marks_total_failure(upload_env):
    env = upload_env
    upload_id = "d" * 32 + ".txt"
    path = env.store.root / "2026/07/17" / upload_id
    row = _legacy_row(path, upload_id, "alice", b"backup bytes")
    live = env.store.root / "uploads.json"
    backup = Path(str(live) + ".bak")
    live.write_text('{"broken":', encoding="utf-8")
    backup.write_text(json.dumps({"alice": row}), encoding="utf-8")

    result = env.authority.import_legacy_index(
        live, max_blob_bytes=1024 * 1024
    )
    assert result.recovered_from_backup is True
    assert result.imported == 1
    assert live.read_text(encoding="utf-8") == '{"broken":'

    other_root = env.root / "second"
    other_store = FileSystemBlobStore(other_root)
    other = SQLUploadMetadataAuthority(
        other_store, session_factory=env.Session
    )
    bad_live = other_root / "uploads.json"
    bad_live.write_text("not-json", encoding="utf-8")
    Path(str(bad_live) + ".bak").write_text("also-not-json", encoding="utf-8")
    failed = other.import_legacy_index(bad_live, max_blob_bytes=1024)
    assert failed.failed == 1
    db = env.Session()
    try:
        assert db.query(ChatUploadMetadataImportRun).filter_by(
            state="failed"
        ).count() == 1
    finally:
        db.close()


def test_legacy_import_rejects_oversized_source_without_mutating_it(
    upload_env, monkeypatch,
):
    env = upload_env
    import src.upload_metadata_authority as authority_module

    monkeypatch.setattr(authority_module, "LEGACY_UPLOAD_INDEX_MAX_BYTES", 16)
    path = env.store.root / "uploads.json"
    original = b"{" + (b"x" * 64)
    path.write_bytes(original)
    result = env.authority.import_legacy_index(path, max_blob_bytes=1024)
    assert result.failed == 1
    assert path.read_bytes() == original


def test_routes_and_email_consumer_resolve_only_sql_owned_metadata(upload_env, monkeypatch):
    env = upload_env
    saved = env.handler.save_upload(
        _upload("route.txt", b"route bytes"), "127.0.0.1", owner="alice"
    )

    import fastapi.dependencies.utils as dependency_utils
    from routes.upload_routes import router, setup_upload_routes

    monkeypatch.setattr(
        dependency_utils, "ensure_multipart_is_installed", lambda: None
    )
    before = len(router.routes)
    setup_upload_routes(env.handler)
    endpoints = {
        route.endpoint.__name__: route.endpoint
        for route in router.routes[before:]
    }
    response = asyncio.run(
        endpoints["download_file"](_Request("alice"), saved["id"])
    )
    assert Path(response.path).read_bytes() == b"route bytes"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoints["download_file"](_Request("bob"), saved["id"]))
    assert exc.value.status_code == 404

    monkeypatch.setattr(
        "src.upload_handler.create_production_upload_handler",
        lambda _base: env.handler,
    )
    from src.email_delivery_worker import _default_attachment_loader

    data, mime = _default_attachment_loader({"id": saved["id"]}, "alice")
    assert data == b"route bytes"
    assert mime == "text/plain"
    with pytest.raises(Exception, match="attachment_owner_mismatch"):
        _default_attachment_loader({"id": saved["id"]}, "bob")
