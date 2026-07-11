"""User display profiles (routes/profile_routes.py)."""
import asyncio
import itertools
import tempfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.profile_routes as pr

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(f"sqlite:///{_TMPDB.name}",
                        connect_args={"check_same_thread": False}, poolclass=NullPool)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)
_n = itertools.count(1)


def _req(username=None):
    return SimpleNamespace(state=SimpleNamespace(current_user=username, api_token=False),
                           app=SimpleNamespace(state=SimpleNamespace()),
                           client=SimpleNamespace(host=f"10.2.0.{next(_n)%250}"),
                           headers={}, cookies={})


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(pr, "SessionLocal", _TS)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    with _ENGINE.begin() as c:
        c.exec_driver_sql("DELETE FROM user_profiles")
    yield


R = {}
for r in pr.setup_profile_routes().routes:
    for m in getattr(r, "methods", set()):
        R[(m, r.path)] = r.endpoint


def _run(c):
    return asyncio.run(c)


def test_set_and_get_display_name():
    _run(R[("POST", "/api/profile")](pr.ProfileRequest(display_name="Mika ⚡"), _req("mika")))
    got = _run(R[("GET", "/api/profile")](_req("mika")))
    assert got["display_name"] == "Mika ⚡" and got["username"] == "mika"


def test_display_names_for_batch_resolution():
    _run(R[("POST", "/api/profile")](pr.ProfileRequest(display_name="Al"), _req("alice")))
    names = pr.display_names_for(["alice", "bob"])
    assert names == {"alice": "Al"}       # bob has none → omitted


def test_name_length_capped():
    with pytest.raises(HTTPException) as e:
        _run(R[("POST", "/api/profile")](pr.ProfileRequest(display_name="x" * 100), _req("mika")))
    assert e.value.status_code == 400


def test_requires_signed_in():
    orig = pr.require_user
    pr.require_user = lambda request: ""
    try:
        with pytest.raises(HTTPException) as e:
            _run(R[("GET", "/api/profile")](_req(None)))
        assert e.value.status_code == 403
    finally:
        pr.require_user = orig


def test_clearing_name_falls_back():
    _run(R[("POST", "/api/profile")](pr.ProfileRequest(display_name="Temp"), _req("mika")))
    _run(R[("POST", "/api/profile")](pr.ProfileRequest(display_name=""), _req("mika")))
    got = _run(R[("GET", "/api/profile")](_req("mika")))
    assert got["display_name"] is None
