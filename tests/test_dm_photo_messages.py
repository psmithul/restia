"""Pair-scoped, encrypted-at-rest still-photo messages."""

import asyncio
import base64
import tempfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.link_routes as lr
import routes.messaging_routes as mr


PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCS"
    "AQANHQEDgslx/wAAAABJRU5ErkJggg=="
)

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)

USERS = {
    "admin": {"is_admin": True},
    "alice": {"is_admin": False},
    "bob": {"is_admin": False},
}


class _FakeAuth:
    @property
    def users(self):
        return USERS

    def is_admin(self, username):
        return bool(USERS.get(username, {}).get("is_admin"))


def _req(username):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=username, api_token=False),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=_FakeAuth())),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
        cookies={},
    )


def _routes():
    out = {}
    for route in mr.setup_messaging_routes().routes:
        for method in getattr(route, "methods", set()):
            out[(method, route.path)] = route.endpoint
    return out


R = _routes()
SEND = R[("POST", "/api/messages/conversations/{other}")]
GETC = R[("GET", "/api/messages/conversations/{other}")]
LIST = R[("GET", "/api/messages/conversations")]
MEDIA = R[("GET", "/api/messages/media/{attachment_id}")]
DELETE = R[("DELETE", "/api/messages/msg/{msg_id}")]


def _run(coro):
    return asyncio.run(coro)


def _photo(name="photo.png", encoded=PNG_B64):
    return mr.PhotoAttachmentRequest(
        name=name,
        data=f"data:image/png;base64,{encoded}",
    )


def _send_photo(sender="alice", recipient="bob", body=""):
    return _run(SEND(
        recipient,
        mr.SendMessageRequest(body=body, attachments=[_photo("../../portrait.svg")]),
        _req(sender),
    ))["message"]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(mr, "SessionLocal", _TS)
    monkeypatch.setattr(lr, "SessionLocal", _TS)
    monkeypatch.setenv("RESTIA_HOME_SERVER", "")
    with _ENGINE.begin() as conn:
        conn.exec_driver_sql("DELETE FROM direct_message_attachments")
        conn.exec_driver_sql("DELETE FROM direct_messages")
    mr.bus._subscribers.clear()
    yield


def test_photo_only_roundtrip_preview_and_encrypted_storage():
    sent = _send_photo()
    assert sent["body"] == ""
    assert len(sent["attachments"]) == 1
    attachment = sent["attachments"][0]
    assert attachment["mime"] == "image/png"
    assert attachment["name"] == "portrait.png"
    assert attachment["width"] == attachment["height"] == 2

    received = _run(GETC("alice", _req("bob"), after_id=0))["messages"][0]
    assert received["attachments"] == [attachment]
    preview = _run(LIST(_req("bob")))["conversations"][0]
    assert preview["last_body"] == "Photo"

    # Raw SQLite contains Fernet ciphertext, not recoverable image/name data.
    with _ENGINE.connect() as conn:
        raw = conn.exec_driver_sql(
            "SELECT filename, data_b64 FROM direct_message_attachments"
        ).one()
    assert "portrait" not in raw[0]
    assert PNG_B64 not in raw[1]


def test_photo_bytes_are_pair_scoped_with_no_admin_bypass():
    sent = _send_photo()
    attachment_id = sent["attachments"][0]["id"]

    for profile in ("alice", "bob"):
        response = _run(MEDIA(attachment_id, _req(profile), peer=""))
        assert response.body.startswith(b"\x89PNG\r\n\x1a\n")
        assert response.media_type == "image/png"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in response.headers["content-security-policy"]

    # Even a local administrator cannot read a pair they do not participate in.
    with pytest.raises(HTTPException) as exc:
        _run(MEDIA(attachment_id, _req("admin"), peer=""))
    assert exc.value.status_code == 404


def test_delete_removes_photo_bytes_and_returns_tombstone():
    sent = _send_photo()
    attachment_id = sent["attachments"][0]["id"]
    tombstone = _run(DELETE(sent["id"], _req("alice")))["message"]
    assert tombstone["deleted"] is True
    assert tombstone["attachments"] == []

    db = _TS()
    try:
        assert db.query(cdb.DirectMessageAttachment).count() == 0
    finally:
        db.close()
    with pytest.raises(HTTPException) as exc:
        _run(MEDIA(attachment_id, _req("bob"), peer=""))
    assert exc.value.status_code == 404


def test_photo_reply_uses_nonleaking_photo_preview():
    original = _send_photo()
    reply = _run(SEND(
        "alice",
        mr.SendMessageRequest(body="Looks good", reply_to_id=original["id"]),
        _req("bob"),
    ))["message"]
    assert reply["reply_to"]["body"] == "Photo"


def test_non_image_payload_and_multiple_photos_are_rejected():
    svg = base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>").decode()
    with pytest.raises(HTTPException) as exc:
        _run(SEND(
            "bob",
            mr.SendMessageRequest(body="", attachments=[_photo("evil.png", svg)]),
            _req("alice"),
        ))
    assert exc.value.status_code == 400

    with pytest.raises(ValidationError):
        mr.SendMessageRequest(body="", attachments=[_photo(), _photo()])


def test_photo_decode_has_bounded_pixels_and_immediate_backpressure():
    assert mr.MAX_PHOTO_PIXELS <= 12_000_000
    assert mr._photo_decode_slot.acquire(blocking=False) is True
    try:
        with pytest.raises(HTTPException) as exc:
            mr.prepare_photo_attachments([_photo()])
        assert exc.value.status_code == 429
    finally:
        mr._photo_decode_slot.release()
