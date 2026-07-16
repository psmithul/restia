"""Home Link Projects stays a pinned, same-origin, closed proxy."""

import asyncio
import gzip
import io
import json
import threading
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as cdb
from core.project_upload_limit import ProjectAttachmentBodyLimitMiddleware
from routes import link_routes
from routes import project_routes
from src.project_storage import ProjectFileStore

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
ITEM_ID = "22222222-2222-2222-2222-222222222222"
ATTACHMENT_ID = "33333333-3333-3333-3333-333333333333"


def _docx_preview_bytes(text: str = "Linked Office proof") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>"
                "</w:document>"
            ),
        )
    return output.getvalue()


def _remote_upload_env(monkeypatch, tmp_path):
    """Mount the real remote Projects router behind the local Home Link proxy."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(link_routes, "SessionLocal", factory)
    monkeypatch.setattr(project_routes, "SessionLocal", factory)
    monkeypatch.setattr(
        link_routes,
        "_project_remote_limiter",
        link_routes.RateLimiter(max_requests=10_000, window_seconds=60),
    )
    monkeypatch.setattr(
        link_routes,
        "_project_remote_invalid_limiter",
        link_routes.RateLimiter(max_requests=10_000, window_seconds=60),
    )
    monkeypatch.setenv("LINK_HUB_ENABLED", "true")

    db = factory()
    try:
        guest = cdb.LinkGuest(
            handle="remote-editor",
            token_hash=link_routes._hash_token("server-only-bearer"),
            status="approved",
        )
        project = cdb.Project(
            id=PROJECT_ID,
            owner="owner",
            key="LINK",
            name="Linked project",
        )
        stage = cdb.ProjectStage(
            id="44444444-4444-4444-4444-444444444444",
            project_id=PROJECT_ID,
            name="Doing",
            category="active",
            position=0,
        )
        item = cdb.ProjectWorkItem(
            id=ITEM_ID,
            project_id=PROJECT_ID,
            stage_id=stage.id,
            item_number=1,
            title="Remote deliverable",
            reporter="owner",
        )
        db.add_all([guest, project, stage, item])
        db.flush()
        grant = cdb.ProjectRemoteGrant(
            id="55555555-5555-5555-5555-555555555555",
            project_id=PROJECT_ID,
            guest_id=guest.id,
            handle_snapshot=guest.handle,
            role="editor",
            status="active",
            invited_by="owner",
        )
        db.add(grant)
        db.commit()
        guest_id = int(guest.id)
        grant_id = str(grant.id)
    finally:
        db.close()

    store = ProjectFileStore(tmp_path / "project-files")
    hub = FastAPI()
    hub.add_middleware(ProjectAttachmentBodyLimitMiddleware)
    hub.include_router(
        project_routes.setup_project_routes(
            store,
            prefix="/api/link/projects",
            remote_only=True,
            dependencies=[Depends(link_routes.require_link_project_remote)],
        )
    )
    return hub, factory, store, engine, guest_id, grant_id


def _proxy_app(monkeypatch, *, assert_current=None):
    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request: Request, call_next):
        request.state.current_user = "owner"
        return await call_next(request)

    snapshot = {
        "link_identity": 7,
        "owner": "owner",
        "token": "server-only-bearer",
        "token_fingerprint": "fingerprint",
        "base_url": "https://pinned.example",
    }
    monkeypatch.setattr(
        link_routes,
        "_require_home_project_link_snapshot",
        lambda _request: dict(snapshot),
    )
    if assert_current is None:
        assert_current = lambda _snapshot, **_kwargs: None
    monkeypatch.setattr(
        link_routes,
        "_assert_home_project_link_current",
        assert_current,
    )
    app.include_router(link_routes.setup_home_link_routes())
    return app, snapshot


def test_project_proxy_uses_only_pinned_origin_and_server_side_bearer(monkeypatch):
    app, snapshot = _proxy_app(monkeypatch)
    seen = {}

    async def fake_hub_call(method, path, **kwargs):
        seen.update({"method": method, "path": path, **kwargs})
        return {"projects": []}

    monkeypatch.setattr(link_routes, "_hub_call", fake_hub_call)
    response = TestClient(app).get(
        "/api/homelink/projects?include_archived=true",
        headers={"Authorization": "Bearer browser-controlled-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"projects": []}
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/link/projects"
    assert seen["base_url"] == snapshot["base_url"]
    assert seen["token"] == snapshot["token"]
    assert seen["params"] == {"include_archived": "true"}
    assert (
        seen["max_response_bytes"]
        == link_routes.MAX_PROJECT_PROXY_JSON_RESPONSE_BYTES
    )
    assert snapshot["token"] not in response.text


def test_office_preview_proxy_uses_small_cap_and_server_side_bearer(monkeypatch):
    app, snapshot = _proxy_app(monkeypatch)
    seen = {}

    async def fake_hub_call(method, path, **kwargs):
        seen.update({"method": method, "path": path, **kwargs})
        return {
            "version": 1,
            "format": "docx",
            "sections": [{"kind": "text", "title": "Document", "text": "safe"}],
            "truncated": False,
        }

    monkeypatch.setattr(link_routes, "_hub_call", fake_hub_call)
    response = TestClient(app).get(
        f"/api/homelink/projects/attachments/{ATTACHMENT_ID}/preview",
        headers={"Authorization": "Bearer browser-controlled-token"},
    )
    assert response.status_code == 200, response.text
    assert seen["method"] == "GET"
    assert seen["path"] == f"/api/link/projects/attachments/{ATTACHMENT_ID}/preview"
    assert seen["base_url"] == snapshot["base_url"]
    assert seen["token"] == snapshot["token"]
    assert seen["max_response_bytes"] == link_routes.OFFICE_PREVIEW_MAX_RESPONSE_BYTES
    assert snapshot["token"] not in response.text
    assert response.headers["cache-control"] == "private, no-store"


def test_project_json_response_cap_covers_legal_endpoint_maxima():
    # JSON may expand each user-controlled character to a six-byte \uXXXX
    # escape.  Cover the largest bounded detail and archived-board shapes.
    detail_bytes = 6 * (
        100_000  # item description
        + (200 * 50_000)  # detail/comment page
        + (500 * 500)  # checklist
        + (200 * 500)  # attachment descriptions
    )
    board_bytes = 10_000 * (
        6 * (240 + (30 * 40))  # title and labels
        + 1024  # bounded card metadata and JSON structure
    )
    assert link_routes.MAX_PROJECT_PROXY_JSON_RESPONSE_BYTES >= max(
        detail_bytes,
        board_bytes,
    )


def test_hub_call_accepts_project_json_above_media_response_limit(monkeypatch):
    body = "x" * (link_routes.MAX_HUB_MEDIA_RESPONSE_BYTES + 1024)
    payload = ('{"body":"' + body + '"}').encode()

    class FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_bytes(self):
            yield payload

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(link_routes.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(link_routes._hub_call(
        "GET",
        "/api/link/projects",
        base_url="https://pinned.example",
        max_response_bytes=link_routes.MAX_PROJECT_PROXY_JSON_RESPONSE_BYTES,
    ))
    assert result == {"body": body}


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/homelink/projects/invitations", 405),
        ("GET", "/api/homelink/projects/https://evil.example/x", 404),
        ("GET", f"/api/homelink/projects/{PROJECT_ID}/members", 404),
        ("POST", f"/api/homelink/projects/{PROJECT_ID}/archive", 404),
    ],
)
def test_project_proxy_rejects_non_whitelisted_route_or_method(
    monkeypatch,
    method,
    path,
    expected,
):
    app, _ = _proxy_app(monkeypatch)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid proxy path reached the hub")

    monkeypatch.setattr(link_routes, "_hub_call", forbidden)
    response = TestClient(app).request(method, path)
    assert response.status_code == expected


def test_project_proxy_rejects_unknown_or_duplicate_query_parameters(monkeypatch):
    app, _ = _proxy_app(monkeypatch)
    client = TestClient(app)

    assert client.get("/api/homelink/projects?url=https://evil.example").status_code == 400
    assert client.get("/api/homelink/projects?limit=1&limit=2").status_code == 400


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/homelink/projects", None),
        ("PATCH", "/api/homelink/projects/11111111-1111-1111-1111-111111111111", {"name": "new"}),
    ],
)
def test_project_proxy_discards_response_if_link_changes_in_flight(
    monkeypatch,
    method,
    path,
    body,
):
    def stale(_snapshot, *, mutation=False):
        # Body-bearing mutations pin and validate their link before the body is
        # staged. Let that preflight pass, then simulate replacement while the
        # upstream request is in flight at the postflight check.
        if method != "GET" and not mutation:
            return
        raise HTTPException(409, "Home Link changed; retry")

    app, _ = _proxy_app(monkeypatch, assert_current=stale)

    async def fake_hub_call(*_args, **_kwargs):
        return {"ok": True}

    monkeypatch.setattr(link_routes, "_hub_call", fake_hub_call)
    response = TestClient(app).request(method, path, json=body)
    assert response.status_code == 409
    assert response.json()["detail"] == "Home Link changed; retry"


@pytest.mark.parametrize(
    "failure_detail",
    [
        "Home server unreachable (ReadTimeout)",
        "Home server response too large",
        "Home server returned malformed data",
    ],
)
@pytest.mark.parametrize("is_mutation", [False, True])
def test_project_proxy_marks_ambiguous_mutation_failures_indeterminate(
    monkeypatch,
    failure_detail,
    is_mutation,
):
    app, _ = _proxy_app(monkeypatch)

    async def failed_hub_call(*_args, **kwargs):
        detail = (
            link_routes.PROJECT_MUTATION_INDETERMINATE_DETAIL
            if kwargs.get("indeterminate_mutation")
            else failure_detail
        )
        raise HTTPException(502, detail)

    monkeypatch.setattr(link_routes, "_hub_call", failed_hub_call)
    if is_mutation:
        response = TestClient(app).post(
            f"/api/homelink/projects/{PROJECT_ID}/items",
            json={"title": "May already exist"},
        )
        assert response.json()["detail"] == (
            link_routes.PROJECT_MUTATION_INDETERMINATE_DETAIL
        )
    else:
        response = TestClient(app).get("/api/homelink/projects")
        assert response.json()["detail"] == failure_detail
    assert response.status_code == 502


@pytest.mark.parametrize(
    ("failure_mode", "ordinary_detail"),
    [
        ("transport", "Home server unreachable (ReadTimeout)"),
        ("oversize", "Home server response too large"),
        ("malformed", "Home server returned malformed data"),
    ],
)
@pytest.mark.parametrize("indeterminate_mutation", [False, True])
def test_hub_call_distinguishes_ambiguous_mutation_response_failures(
    monkeypatch,
    failure_mode,
    ordinary_detail,
    indeterminate_mutation,
):
    class FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_bytes(self):
            if failure_mode == "transport":
                raise httpx.ReadTimeout("response lost")
            if failure_mode == "oversize":
                yield b"x" * 1025
            else:
                yield b"not-json"

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(link_routes.httpx, "AsyncClient", FakeClient)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(link_routes._hub_call(
            "POST" if indeterminate_mutation else "GET",
            "/api/link/projects",
            base_url="https://pinned.example",
            max_response_bytes=1024,
            indeterminate_mutation=indeterminate_mutation,
        ))
    assert exc.value.status_code == 502
    assert exc.value.detail == (
        link_routes.PROJECT_MUTATION_INDETERMINATE_DETAIL
        if indeterminate_mutation
        else ordinary_detail
    )


def test_home_project_snapshot_requires_link_owner_or_admin(monkeypatch):
    link = SimpleNamespace(
        id=9,
        owner="owner",
        token="secret-token",
        home_url="https://pinned.example",
    )

    class FakeDb:
        def close(self):
            pass

    monkeypatch.setattr(link_routes, "SessionLocal", FakeDb)
    monkeypatch.setattr(link_routes, "_require_link", lambda _db, _me: link)
    monkeypatch.setattr(link_routes, "_require_local_profile", lambda _request: "other")

    def not_admin(_request):
        raise HTTPException(403, "Admin only")

    monkeypatch.setattr(link_routes, "require_admin", not_admin)
    request = SimpleNamespace(state=SimpleNamespace(api_token=False))
    with pytest.raises(HTTPException) as exc:
        link_routes._require_home_project_link_snapshot(request)
    assert exc.value.status_code == 403

    monkeypatch.setattr(link_routes, "require_admin", lambda _request: None)
    snapshot = link_routes._require_home_project_link_snapshot(request)
    assert snapshot["link_identity"] == 9
    assert snapshot["base_url"] == "https://pinned.example"
    assert snapshot["token"] == "secret-token"

    request.state.api_token = True
    with pytest.raises(HTTPException) as api_token:
        link_routes._require_home_project_link_snapshot(request)
    assert api_token.value.status_code == 403
    assert api_token.value.detail == "A signed-in browser session is required"


def test_project_proxy_transport_classification_is_exact():
    # GET and POST use separate allowlist rules for the same collection path.
    # A method mismatch on the GET rule must not mask the later POST rule.
    assert link_routes._project_proxy_kind(
        "POST", f"{PROJECT_ID}/items"
    ) == "json"
    assert link_routes._project_proxy_kind(
        "POST", f"{PROJECT_ID}/items/{ITEM_ID}/attachments"
    ) == "upload"
    assert link_routes._project_proxy_kind(
        "GET", f"attachments/{ATTACHMENT_ID}/download"
    ) == "download"
    assert link_routes._project_proxy_kind(
        "GET", f"attachments/{ATTACHMENT_ID}/view"
    ) == "preview"
    assert link_routes._project_proxy_kind(
        "GET", f"attachments/{ATTACHMENT_ID}/preview"
    ) == "office_preview"
    assert link_routes._project_proxy_kind("GET", f"{PROJECT_ID}/board") == "json"
    assert link_routes._project_proxy_kind("GET", f"{PROJECT_ID}/context") == "json"
    with pytest.raises(HTTPException) as exc:
        link_routes._project_proxy_kind(
            "POST", f"attachments/{ATTACHMENT_ID}/download"
        )
    assert exc.value.status_code == 405
    with pytest.raises(HTTPException) as preview_method:
        link_routes._project_proxy_kind(
            "POST", f"attachments/{ATTACHMENT_ID}/view"
        )
    assert preview_method.value.status_code == 405
    with pytest.raises(HTTPException) as office_preview_method:
        link_routes._project_proxy_kind(
            "POST", f"attachments/{ATTACHMENT_ID}/preview"
        )
    assert office_preview_method.value.status_code == 405


def test_stalled_project_json_body_does_not_hold_lifecycle_lock(monkeypatch):
    lock = asyncio.Lock()
    monkeypatch.setattr(link_routes, "_home_link_lifecycle_lock", lock)
    monkeypatch.setattr(
        link_routes,
        "_require_home_project_link_snapshot",
        lambda _request: {
            "link_identity": 7,
            "owner": "owner",
            "token": "server-only-bearer",
            "token_fingerprint": "fingerprint",
            "base_url": "https://pinned.example",
        },
    )
    monkeypatch.setattr(
        link_routes,
        "_assert_home_project_link_current",
        lambda _snapshot, **_kwargs: None,
    )
    upstream_started = None

    async def fake_hub_call(*_args, **_kwargs):
        upstream_started.set()
        return {"ok": True}

    monkeypatch.setattr(link_routes, "_hub_call", fake_hub_call)
    router = link_routes.setup_home_link_routes()
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "").endswith("/projects/{remote_path:path}")
    )

    async def scenario():
        nonlocal upstream_started
        body_started = asyncio.Event()
        release_body = asyncio.Event()
        upstream_started = asyncio.Event()
        payload = json.dumps({"name": "Staged first"}).encode()

        async def receive():
            body_started.set()
            await release_body.wait()
            return {"type": "http.request", "body": payload, "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "PATCH",
                "path": f"/api/homelink/projects/{PROJECT_ID}",
                "query_string": b"",
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
                "path_params": {},
                "state": {},
            },
            receive,
        )
        task = asyncio.create_task(
            endpoint(remote_path=PROJECT_ID, request=request)
        )
        await asyncio.wait_for(body_started.wait(), 1)
        assert lock.locked() is False
        await asyncio.wait_for(lock.acquire(), 0.1)
        release_body.set()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(upstream_started.wait(), 0.05)
        lock.release()
        assert await asyncio.wait_for(task, 1) == {"ok": True}

    asyncio.run(scenario())


def test_stalled_project_upload_is_spooled_before_lifecycle_lock(monkeypatch):
    lock = asyncio.Lock()
    monkeypatch.setattr(link_routes, "_home_link_lifecycle_lock", lock)
    monkeypatch.setattr(
        link_routes,
        "_require_home_project_link_snapshot",
        lambda _request: {
            "link_identity": 7,
            "owner": "owner",
            "token": "server-only-bearer",
            "token_fingerprint": "fingerprint",
            "base_url": "https://pinned.example",
        },
    )
    monkeypatch.setattr(
        link_routes,
        "_assert_home_project_link_current",
        lambda _snapshot, **_kwargs: None,
    )
    forwarded = None
    staged_handle = None

    async def fake_upload(staged_upload, remote_path, _snapshot):
        nonlocal staged_handle
        staged_handle, content_type, size = staged_upload
        assert remote_path == f"{PROJECT_ID}/items/{ITEM_ID}/attachments"
        assert content_type == "multipart/form-data; boundary=restia-test"
        forwarded.set()
        return {"size": size, "body": staged_handle.read().decode()}

    monkeypatch.setattr(link_routes, "_proxy_home_project_upload", fake_upload)
    router = link_routes.setup_home_link_routes()
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "").endswith("/projects/{remote_path:path}")
    )

    async def scenario():
        nonlocal forwarded
        body_started = asyncio.Event()
        release_body = asyncio.Event()
        forwarded = asyncio.Event()
        payload = b"--restia-test\r\nstaged multipart bytes\r\n--restia-test--\r\n"

        async def receive():
            body_started.set()
            await release_body.wait()
            return {"type": "http.request", "body": payload, "more_body": False}

        remote_path = f"{PROJECT_ID}/items/{ITEM_ID}/attachments"
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"/api/homelink/projects/{remote_path}",
                "query_string": b"",
                "headers": [
                    (
                        b"content-type",
                        b"multipart/form-data; boundary=restia-test",
                    ),
                    (b"content-length", str(len(payload)).encode()),
                ],
                "path_params": {},
                "state": {},
            },
            receive,
        )
        task = asyncio.create_task(
            endpoint(remote_path=remote_path, request=request)
        )
        await asyncio.wait_for(body_started.wait(), 1)
        assert lock.locked() is False
        await asyncio.wait_for(lock.acquire(), 0.1)
        release_body.set()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(forwarded.wait(), 0.05)
        lock.release()
        result = await asyncio.wait_for(task, 1)
        assert result == {"size": len(payload), "body": payload.decode()}

    asyncio.run(scenario())
    assert staged_handle.closed is True


def test_project_upload_proxy_replays_the_staged_multipart_bytes(monkeypatch):
    payload = b"--restia-test\r\nraw multipart body\r\n--restia-test--\r\n"
    staged = io.BytesIO(payload)
    seen = {}
    real_client = httpx.AsyncClient

    async def upstream(request):
        seen["body"] = await request.aread()
        seen["content_length"] = request.headers["content-length"]
        seen["content_type"] = request.headers["content-type"]
        return httpx.Response(200, json={"ok": True})

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(upstream), **kwargs)

    monkeypatch.setattr(link_routes.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        link_routes,
        "_assert_home_project_link_current",
        lambda _snapshot, **_kwargs: None,
    )
    result = asyncio.run(link_routes._proxy_home_project_upload(
        (
            staged,
            "multipart/form-data; boundary=restia-test",
            len(payload),
        ),
        f"{PROJECT_ID}/items/{ITEM_ID}/attachments",
        {"token": "server-only-bearer", "base_url": "https://pinned.example"},
    ))
    assert result.status_code == 200
    assert json.loads(result.body) == {"ok": True}
    assert result.headers["cache-control"] == "no-store"
    assert seen == {
        "body": payload,
        "content_length": str(len(payload)),
        "content_type": "multipart/form-data; boundary=restia-test",
    }


@pytest.mark.parametrize(
    ("status_code", "upstream_detail", "expected_detail"),
    [
        (408, "Project attachment upload body timed out", "Project attachment upload body timed out"),
        (413, "Attachment exceeds 50 MB limit", "Attachment exceeds 50 MB limit"),
        (415, "Project attachment upload must be multipart/form-data", "Project attachment upload must be multipart/form-data"),
        (422, [{"loc": ["body", "file"], "msg": "Field required"}], "Invalid project attachment upload request"),
        (507, "Unable to store attachment; check project storage space and permissions", "Unable to store attachment; check project storage space and permissions"),
    ],
)
def test_project_upload_proxy_preserves_bounded_actionable_upstream_errors(
    monkeypatch,
    status_code,
    upstream_detail,
    expected_detail,
):
    payload = b"--restia-test\r\nbody\r\n--restia-test--\r\n"
    real_client = httpx.AsyncClient

    async def upstream(_request):
        return httpx.Response(status_code, json={"detail": upstream_detail})

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(upstream), **kwargs)

    monkeypatch.setattr(link_routes.httpx, "AsyncClient", client_factory)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(link_routes._proxy_home_project_upload(
            (
                io.BytesIO(payload),
                "multipart/form-data; boundary=restia-test",
                len(payload),
            ),
            f"{PROJECT_ID}/items/{ITEM_ID}/attachments",
            {"token": "server-only-bearer", "base_url": "https://pinned.example"},
        ))
    assert exc.value.status_code == status_code
    assert exc.value.detail == expected_detail
    assert "server-only-bearer" not in str(exc.value.detail)


def test_remote_editor_upload_reaches_authoritative_project_router(
    monkeypatch,
    tmp_path,
):
    """Exercise browser multipart -> local proxy -> bearer-gated hub upload."""
    hub, factory, store, engine, _guest_id, grant_id = _remote_upload_env(
        monkeypatch,
        tmp_path,
    )
    local, snapshot = _proxy_app(monkeypatch)
    local.add_middleware(ProjectAttachmentBodyLimitMiddleware)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_async_client(
            transport=httpx.ASGITransport(
                app=hub,
                client=("203.0.113.43", 4321),
            ),
            **kwargs,
        )

    monkeypatch.setattr(link_routes.httpx, "AsyncClient", client_factory)
    payload = b"linked editor evidence\n"
    try:
        response = TestClient(local).post(
            f"/api/homelink/projects/{PROJECT_ID}/items/{ITEM_ID}/attachments",
            headers={"Authorization": "Bearer browser-controlled-token"},
            files={"file": ("evidence.txt", payload, "text/plain")},
            data={"kind": "deliverable", "description": "Remote result"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["attachment"]["name"] == "evidence.txt"
        assert body["attachment"]["size"] == len(payload)
        assert body["attachment"]["download_url"].startswith(
            "/api/link/projects/attachments/"
        )
        assert snapshot["token"] not in response.text

        db = factory()
        try:
            attachment = db.query(cdb.ProjectAttachment).one()
            assert attachment.uploader == project_routes.remote_grant_principal(grant_id)
            stored_path = store.resolve(attachment.storage_key)
        finally:
            db.close()
        assert stored_path.read_bytes() == payload

        db = factory()
        try:
            grant = db.query(cdb.ProjectRemoteGrant).filter_by(id=grant_id).one()
            grant.role = "viewer"
            db.commit()
        finally:
            db.close()
        denied = TestClient(local).post(
            f"/api/homelink/projects/{PROJECT_ID}/items/{ITEM_ID}/attachments",
            files={"file": ("forbidden.txt", b"not stored\n", "text/plain")},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "Project role does not allow this action"
        assert snapshot["token"] not in denied.text
        db = factory()
        try:
            assert db.query(cdb.ProjectAttachment).count() == 1
        finally:
            db.close()
    finally:
        engine.dispose()


def test_linked_office_preview_stays_same_origin_and_viewer_readable(
    monkeypatch,
    tmp_path,
):
    hub, factory, _store, engine, _guest_id, grant_id = _remote_upload_env(
        monkeypatch,
        tmp_path,
    )
    local, snapshot = _proxy_app(monkeypatch)
    local.add_middleware(ProjectAttachmentBodyLimitMiddleware)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_async_client(
            transport=httpx.ASGITransport(
                app=hub,
                client=("203.0.113.43", 4321),
            ),
            **kwargs,
        )

    monkeypatch.setattr(link_routes.httpx, "AsyncClient", client_factory)
    try:
        uploaded = TestClient(local).post(
            f"/api/homelink/projects/{PROJECT_ID}/items/{ITEM_ID}/attachments",
            files={
                "file": (
                    "linked-proof.docx",
                    _docx_preview_bytes(),
                    "application/octet-stream",
                )
            },
            data={"kind": "deliverable"},
        )
        assert uploaded.status_code == 201, uploaded.text
        attachment_id = uploaded.json()["attachment"]["id"]

        db = factory()
        try:
            grant = db.query(cdb.ProjectRemoteGrant).filter_by(id=grant_id).one()
            grant.role = "viewer"
            db.commit()
        finally:
            db.close()

        preview = TestClient(local).get(
            f"/api/homelink/projects/attachments/{attachment_id}/preview",
            headers={"Authorization": "Bearer browser-controlled-token"},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["format"] == "docx"
        assert preview.json()["sections"][0]["text"] == "Linked Office proof"
        assert snapshot["token"] not in preview.text
        assert preview.headers["content-type"].startswith("application/json")
        assert preview.headers["cache-control"] == "private, no-store"
    finally:
        engine.dispose()


def test_project_spool_io_finishes_worker_before_propagating_cancellation():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_file_io():
        started.set()
        release.wait(1)
        finished.set()

    async def scenario():
        task = asyncio.create_task(
            link_routes._run_project_spool_io(blocking_file_io)
        )
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert finished.is_set()

    asyncio.run(scenario())


def test_download_headers_are_bounded_and_unicode_safe():
    response = httpx.Response(
        200,
        headers={
            "content-type": "application/pdf",
            "content-length": "42",
            "content-disposition": (
                "attachment; filename*=utf-8''%E6%8E%A7%E5%88%B6-report.pdf"
            ),
        },
    )
    headers = link_routes._safe_project_download_headers(response)
    assert headers["Content-Length"] == "42"
    assert "filename*=UTF-8''" in headers["Content-Disposition"]
    headers["Content-Disposition"].encode("ascii")

    oversized = httpx.Response(
        200,
        headers={"content-length": str(link_routes.PROJECT_ATTACHMENT_MAX_BYTES + 1)},
    )
    with pytest.raises(HTTPException) as exc:
        link_routes._safe_project_download_headers(oversized)
    assert exc.value.status_code == 502

    missing_size = httpx.Response(200, headers={"content-type": "application/pdf"})
    with pytest.raises(HTTPException) as exc:
        link_routes._safe_project_download_headers(missing_size)
    assert exc.value.status_code == 502
    assert exc.value.detail == "Home server omitted attachment size"


def test_preview_headers_require_safe_mime_and_consistent_single_range():
    ranged = httpx.Response(
        206,
        headers={
            "content-type": "text/plain; charset=utf-8",
            "content-length": "10",
            "content-range": "bytes 0-9/42",
            "content-disposition": 'inline; filename="notes.txt"',
        },
    )
    headers = link_routes._safe_project_download_headers(
        ranged,
        inline=True,
        range_requested=True,
    )
    assert headers["Content-Disposition"].startswith("inline;")
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Range"] == "bytes 0-9/42"
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"

    unsafe = httpx.Response(
        200,
        headers={
            "content-type": "text/html",
            "content-length": "12",
            "content-disposition": 'inline; filename="payload.html"',
        },
    )
    with pytest.raises(HTTPException) as unsafe_type:
        link_routes._safe_project_download_headers(unsafe, inline=True)
    assert unsafe_type.value.status_code == 502

    inconsistent = httpx.Response(
        206,
        headers={
            "content-type": "application/pdf",
            "content-length": "9",
            "content-range": "bytes 0-9/42",
        },
    )
    with pytest.raises(HTTPException) as invalid_range:
        link_routes._safe_project_download_headers(
            inconsistent,
            inline=True,
            range_requested=True,
        )
    assert invalid_range.value.status_code == 502

    assert link_routes._validated_project_preview_range("bytes=0-9") == "bytes=0-9"
    with pytest.raises(HTTPException) as multipart:
        link_routes._validated_project_preview_range("bytes=0-1,4-5")
    assert multipart.value.status_code == 416


def test_preview_proxy_forwards_only_validated_range_and_preserves_206(monkeypatch):
    raw = b"preview-10"
    upstream = httpx.Response(
        206,
        headers={
            "content-type": "text/plain; charset=utf-8",
            "content-length": str(len(raw)),
            "content-range": f"bytes 0-{len(raw) - 1}/100",
            "content-disposition": 'inline; filename="notes.txt"',
        },
        content=raw,
    )
    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False
            self.request = None
            self.closed = False
            clients.append(self)

        def build_request(self, method, url, headers):
            self.request = httpx.Request(method, url, headers=headers)
            return self.request

        async def send(self, request, *, stream):
            assert request is self.request
            assert stream is True
            return upstream

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(link_routes.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        link_routes,
        "_assert_home_project_link_current",
        lambda _snapshot, **_kwargs: None,
    )
    snapshot = {
        "token": "server-only-bearer",
        "base_url": "https://pinned.example",
    }

    async def collect():
        response = await link_routes._proxy_home_project_download(
            f"attachments/{ATTACHMENT_ID}/view",
            snapshot,
            inline=True,
            range_header=f"bytes=0-{len(raw) - 1}",
        )
        content = b"".join([chunk async for chunk in response.body_iterator])
        return response, content

    response, content = asyncio.run(collect())
    upstream_headers = clients[0].request.headers
    assert upstream_headers["authorization"] == "Bearer server-only-bearer"
    assert upstream_headers["range"] == f"bytes=0-{len(raw) - 1}"
    assert "cookie" not in upstream_headers
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 0-{len(raw) - 1}/100"
    assert response.headers["content-disposition"].startswith("inline;")
    assert content == raw
    assert clients[0].closed is True


def test_download_proxy_forces_identity_encoding_and_preserves_length(monkeypatch):
    raw = b"%PDF-1.7\nnot-compressed"
    upstream = httpx.Response(
        200,
        headers={
            "content-type": "application/pdf",
            "content-length": str(len(raw)),
            "content-disposition": 'attachment; filename="report.pdf"',
        },
        content=raw,
    )
    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False
            self.request = None
            self.closed = False
            clients.append(self)

        def build_request(self, method, url, headers):
            self.request = httpx.Request(method, url, headers=headers)
            return self.request

        async def send(self, request, *, stream):
            assert request is self.request
            assert stream is True
            return upstream

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(link_routes.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        link_routes,
        "_assert_home_project_link_current",
        lambda _snapshot, **_kwargs: None,
    )
    snapshot = {
        "token": "server-only-bearer",
        "base_url": "https://pinned.example",
    }

    async def collect():
        response = await link_routes._proxy_home_project_download(
            f"attachments/{ATTACHMENT_ID}/download",
            snapshot,
        )
        content = b"".join([chunk async for chunk in response.body_iterator])
        return response, content

    response, content = asyncio.run(collect())
    assert clients[0].request.headers["accept-encoding"] == "identity"
    assert response.headers["content-length"] == str(len(raw))
    assert content == raw
    assert clients[0].closed is True


def test_download_proxy_rejects_unexpected_content_encoding(monkeypatch):
    compressed = gzip.compress(b"encoded-placeholder")
    upstream = httpx.Response(
        200,
        headers={
            "content-encoding": "gzip",
            "content-length": str(len(compressed)),
        },
        content=compressed,
    )
    clients = []

    class FakeClient:
        def __init__(self, **_kwargs):
            self.closed = False
            clients.append(self)

        def build_request(self, method, url, headers):
            self.request = httpx.Request(method, url, headers=headers)
            return self.request

        async def send(self, _request, *, stream):
            assert stream is True
            return upstream

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(link_routes.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        link_routes,
        "_assert_home_project_link_current",
        lambda _snapshot, **_kwargs: None,
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(link_routes._proxy_home_project_download(
            f"attachments/{ATTACHMENT_ID}/download",
            {"token": "server-only-bearer", "base_url": "https://pinned.example"},
        ))
    assert exc.value.status_code == 502
    assert exc.value.detail == "Home server returned an encoded attachment unexpectedly"
    assert clients[0].request.headers["accept-encoding"] == "identity"
    assert clients[0].closed is True
