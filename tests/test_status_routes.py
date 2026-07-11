"""Status photos (routes/status_routes.py).

Pins the visibility rules:
  - A status is visible only to accounts the author has a DM thread with (plus
    the author) — a non-contact gets a 404 for the feed slot, the image, and
    the seen/viewer endpoints, so photos never leak to strangers.
  - Images must be still-image data URLs (no SVG), size-capped.
  - Expired posts vanish from every read; authors can delete their own.
  - Seen-state drives the unseen flag and the author's viewer list.
"""
import asyncio
import base64
import itertools
import tempfile
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.status_routes as st

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)

_n = itertools.count(1)
_PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 64).decode()


def _req(username=None):
    return SimpleNamespace(state=SimpleNamespace(current_user=username, api_token=False),
                           app=SimpleNamespace(state=SimpleNamespace()),
                           client=SimpleNamespace(host=f"10.4.0.{next(_n) % 250}"),
                           headers={}, cookies={})


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(st, "SessionLocal", _TS)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    with _ENGINE.begin() as conn:
        for t in ("direct_messages", "status_posts", "status_views"):
            conn.exec_driver_sql(f"DELETE FROM {t}")
    yield


ROUTES = {}
for r in st.setup_status_routes().routes:
    for m in getattr(r, "methods", set()):
        ROUTES[(m, r.path)] = r.endpoint


def _run(c):
    return asyncio.run(c)


def _dm(a, b):
    """Give a and b a DM thread so they become contacts."""
    db = _TS()
    try:
        db.add(cdb.DirectMessage(sender=a, recipient=b, body="hi",
                                 created_at=cdb.utcnow_naive()))
        db.commit()
    finally:
        db.close()


def _post(user, caption=None, ttl=None):
    body = st.StatusPostRequest(image=_PNG, caption=caption, ttl_hours=ttl)
    return _run(ROUTES[("POST", "/api/status/post")](body, _req(user)))


def _feed(user):
    return _run(ROUTES[("GET", "/api/status/feed")](_req(user)))["statuses"]


def test_contact_sees_status_but_stranger_does_not():
    _dm("alice", "bob")               # alice & bob are contacts; carol is not
    pid = _post("alice", caption="lunch")["id"]
    # bob (contact) sees it in the feed and can load the image.
    feed = _feed("bob")
    assert [a["author"] for a in feed] == ["alice"]
    assert feed[0]["has_unseen"] is True and feed[0]["posts"][0]["caption"] == "lunch"
    img = _run(ROUTES[("GET", "/api/status/{status_id}/image")](pid, _req("bob")))
    assert img["image"] == _PNG
    # carol (non-contact) sees nothing and is refused the image.
    assert _feed("carol") == []
    with pytest.raises(HTTPException) as e:
        _run(ROUTES[("GET", "/api/status/{status_id}/image")](pid, _req("carol")))
    assert e.value.status_code == 404


def test_author_sees_own_status_as_seen():
    _post("alice")
    feed = _feed("alice")
    assert feed[0]["mine"] is True and feed[0]["has_unseen"] is False


def test_marking_seen_clears_unseen_and_lists_viewer():
    _dm("alice", "bob")
    pid = _post("alice")["id"]
    _run(ROUTES[("POST", "/api/status/{status_id}/seen")](pid, _req("bob")))
    assert _feed("bob")[0]["has_unseen"] is False
    v = _run(ROUTES[("GET", "/api/status/{status_id}/viewers")](pid, _req("alice")))
    assert v["viewers"] == ["bob"] and v["count"] == 1
    # Only the author sees the viewer list.
    with pytest.raises(HTTPException) as e:
        _run(ROUTES[("GET", "/api/status/{status_id}/viewers")](pid, _req("bob")))
    assert e.value.status_code == 404


def test_expired_status_is_invisible_and_purged():
    _dm("alice", "bob")
    pid = _post("alice")["id"]
    db = _TS()
    try:
        p = db.query(cdb.StatusPost).filter(cdb.StatusPost.id == pid).first()
        p.expires_at = cdb.utcnow_naive() - timedelta(hours=1)
        db.commit()
    finally:
        db.close()
    assert _feed("bob") == []
    with pytest.raises(HTTPException) as e:
        _run(ROUTES[("GET", "/api/status/{status_id}/image")](pid, _req("bob")))
    assert e.value.status_code == 404
    # The feed read purged it.
    db = _TS()
    try:
        assert db.query(cdb.StatusPost).filter(cdb.StatusPost.id == pid).count() == 0
    finally:
        db.close()


def test_rejects_non_image_and_svg_data_urls():
    for bad in ("data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
                "data:text/html;base64,PGh0bWw+",
                "https://example.com/x.png",
                "not a data url"):
        with pytest.raises(HTTPException) as e:
            _run(ROUTES[("POST", "/api/status/post")](
                st.StatusPostRequest(image=bad), _req("alice")))
        assert e.value.status_code == 400


def test_rejects_oversize_image(monkeypatch):
    monkeypatch.setattr(st, "STATUS_UPLOAD_MAX_BYTES", 16)
    with pytest.raises(HTTPException) as e:
        _post("alice")
    assert e.value.status_code == 400


def test_ttl_bounds_enforced():
    for bad in (0, 999):
        with pytest.raises(HTTPException) as e:
            _post("alice", ttl=bad)
        assert e.value.status_code == 400


def test_author_can_delete_but_others_cannot():
    _dm("alice", "bob")
    pid = _post("alice")["id"]
    with pytest.raises(HTTPException) as e:
        _run(ROUTES[("DELETE", "/api/status/{status_id}")](pid, _req("bob")))
    assert e.value.status_code == 404
    assert _run(ROUTES[("DELETE", "/api/status/{status_id}")](pid, _req("alice")))["ok"] is True
    assert _feed("bob") == []
