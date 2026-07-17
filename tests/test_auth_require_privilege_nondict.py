import types

import pytest

from src import auth_helpers
from src.auth_helpers import require_privilege


class _Mgr:
    def __init__(self, privs):
        self._privs = privs

    def get_privileges(self, user):
        return self._privs


def _request(mgr):
    state = types.SimpleNamespace(auth_manager=mgr)
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def test_require_privilege_fails_closed_on_non_dict_policy(monkeypatch):
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = _request(_Mgr(["do_x"]))
    with pytest.raises(Exception) as exc:
        require_privilege(req, "do_x")
    assert getattr(exc.value, "status_code", None) == 503


def test_require_privilege_still_blocks_disallowed(monkeypatch):
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = _request(_Mgr({"do_x": False}))
    with pytest.raises(Exception):
        require_privilege(req, "do_x")


def test_require_privilege_blocks_missing_capability(monkeypatch):
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = _request(_Mgr({}))
    with pytest.raises(Exception) as exc:
        require_privilege(req, "do_x")
    assert getattr(exc.value, "status_code", None) == 403


def test_require_privilege_surfaces_database_outage(monkeypatch):
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")

    class _Unavailable:
        def get_privileges(self, user):
            raise RuntimeError("database unavailable")

    with pytest.raises(Exception) as exc:
        require_privilege(_request(_Unavailable()), "do_x")
    assert getattr(exc.value, "status_code", None) == 503
