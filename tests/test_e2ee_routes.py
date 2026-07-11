"""E2EE key store endpoints (routes/e2ee_routes.py).

Pins the server's role as a validating byte-bag that can never read messages:
  - Publishing requires a signed-in account and a well-formed PUBLIC P-256 JWK
    (a JWK carrying a private 'd' component is rejected — the server must never
    be handed a private key).
  - A user can fetch their own wrapped bundle (to unlock on another device) and
    any other user's PUBLIC key (to encrypt to them), but never anyone else's
    wrapped private key.
  - KDF iterations are floored so a client can't publish weak parameters.
"""
import asyncio
import itertools
import json
import tempfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.e2ee_routes as e2

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)

_n = itertools.count(1)


def _req(username=None):
    state = SimpleNamespace(current_user=username, api_token=False)
    return SimpleNamespace(state=state, app=SimpleNamespace(state=SimpleNamespace()),
                           client=SimpleNamespace(host=f"10.5.0.{next(_n) % 250}"),
                           headers={}, cookies={})


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(e2, "SessionLocal", _TS)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    with _ENGINE.begin() as conn:
        conn.exec_driver_sql("DELETE FROM user_keys")
    yield


ROUTES = {}
for r in e2.setup_e2ee_routes().routes:
    for m in getattr(r, "methods", set()):
        ROUTES[(m, r.path)] = r.endpoint


def _run(c):
    return asyncio.run(c)


PUB = json.dumps({"kty": "EC", "crv": "P-256", "x": "abc", "y": "def"})
WRAPPED = json.dumps({"iv": "aXY=", "ct": "Y3Q="})


def _publish(user, public_jwk=PUB, wrapped=WRAPPED, salt="c2FsdA==", iters=210000):
    body = e2.PublishKeysRequest(public_jwk=public_jwk, wrapped_private=wrapped,
                                 kdf_salt=salt, kdf_iterations=iters)
    return _run(ROUTES[("POST", "/api/e2ee/me")](body, _req(user)))


def test_publish_then_fetch_own_bundle():
    assert _publish("alice")["ok"] is True
    me = _run(ROUTES[("GET", "/api/e2ee/me")](_req("alice")))
    assert me["exists"] is True
    assert me["public_jwk"] == PUB and me["wrapped_private"] == WRAPPED
    assert me["kdf_iterations"] == 210000


def test_missing_bundle_reports_not_exists():
    assert _run(ROUTES[("GET", "/api/e2ee/me")](_req("bob")))["exists"] is False


def test_fetch_others_public_key_but_never_their_wrapped_private():
    _publish("alice")
    got = _run(ROUTES[("GET", "/api/e2ee/key/{username}")]("alice", _req("bob")))
    assert got["exists"] is True and got["public_jwk"] == PUB
    # The public-key endpoint exposes only the public JWK.
    assert "wrapped_private" not in got and "kdf_salt" not in got


def test_publish_requires_signed_in_account():
    # Anonymous mode (require_user returns "") → our explicit 403.
    orig = e2.require_user
    e2.require_user = lambda request: ""
    try:
        with pytest.raises(HTTPException) as e:
            _publish("")
        assert e.value.status_code == 403
    finally:
        e2.require_user = orig


def test_rejects_private_component_in_public_jwk():
    bad = json.dumps({"kty": "EC", "crv": "P-256", "x": "a", "y": "b", "d": "SECRET"})
    with pytest.raises(HTTPException) as e:
        _publish("alice", public_jwk=bad)
    assert e.value.status_code == 400


def test_rejects_wrong_curve_and_malformed_jwk():
    for bad in (json.dumps({"kty": "EC", "crv": "P-384", "x": "a", "y": "b"}),
                json.dumps({"kty": "RSA", "n": "x", "e": "AQAB"}),
                "not json at all"):
        with pytest.raises(HTTPException) as e:
            _publish("alice", public_jwk=bad)
        assert e.value.status_code == 400


def test_rejects_weak_kdf_iterations():
    with pytest.raises(HTTPException) as e:
        _publish("alice", iters=1000)
    assert e.value.status_code == 400


def test_rejects_malformed_wrapped_blob():
    with pytest.raises(HTTPException) as e:
        _publish("alice", wrapped=json.dumps({"iv": "only-iv"}))
    assert e.value.status_code == 400


def test_republish_replaces_existing_identity():
    _publish("alice", salt="c2FsdDE=")
    new_pub = json.dumps({"kty": "EC", "crv": "P-256", "x": "new", "y": "key"})
    _publish("alice", public_jwk=new_pub, salt="c2FsdDI=")
    me = _run(ROUTES[("GET", "/api/e2ee/me")](_req("alice")))
    assert me["public_jwk"] == new_pub and me["kdf_salt"] == "c2FsdDI="
    # Still exactly one row for alice.
    db = _TS()
    try:
        assert db.query(cdb.UserKey).filter(cdb.UserKey.username == "alice").count() == 1
    finally:
        db.close()
