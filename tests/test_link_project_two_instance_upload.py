"""Real-process Home Link project upload regression.

The local Restia and authoritative hub intentionally run with different
SQLite databases and project-file roots.  This catches integration failures
that an in-process router test can mask through shared module globals.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import queue
import socket
import sqlite3
import traceback
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import core.database as cdb
from core.project_upload_limit import ProjectAttachmentBodyLimitMiddleware
from routes import link_routes


PROJECT_ID = "11111111-1111-1111-1111-111111111111"
ITEM_ID = "22222222-2222-2222-2222-222222222222"
STAGE_ID = "33333333-3333-3333-3333-333333333333"
GRANT_ID = "44444444-4444-4444-4444-444444444444"
TOKEN = "two-instance-project-token-1234567890"


def _sqlite_factory(path: Path, models):
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    cdb.Base.metadata.create_all(
        engine,
        tables=[model.__table__ for model in models],
    )
    return engine, sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _serve_hub(
    port: int,
    database_path: str,
    files_path: str,
    ready_queue,
    stop_event,
) -> None:
    """Run the authoritative Restia in a clean interpreter and data root."""

    engine = None
    try:
        import uvicorn
        from fastapi import Depends
        from routes import project_routes
        from src.project_storage import ProjectFileStore

        os.environ["LINK_HUB_ENABLED"] = "true"
        engine, factory = _sqlite_factory(Path(database_path), (
            cdb.LinkGuest,
            cdb.RemoteBlock,
            cdb.Project,
            cdb.ProjectStage,
            cdb.ProjectWorkItem,
            cdb.ProjectRemoteGrant,
            cdb.ProjectAttachment,
            cdb.ProjectActivity,
        ))
        link_routes.SessionLocal = factory
        project_routes.SessionLocal = factory

        db = factory()
        try:
            guest = cdb.LinkGuest(
                handle="remote-editor",
                token_hash=link_routes._hash_token(TOKEN),
                status="approved",
            )
            project = cdb.Project(
                id=PROJECT_ID,
                owner="owner",
                key="LINK",
                name="Linked project",
            )
            stage = cdb.ProjectStage(
                id=STAGE_ID,
                project_id=PROJECT_ID,
                name="Doing",
                category="in_progress",
                position=0,
            )
            item = cdb.ProjectWorkItem(
                id=ITEM_ID,
                project_id=PROJECT_ID,
                stage_id=STAGE_ID,
                item_number=1,
                title="Remote deliverable",
                reporter="owner",
            )
            db.add_all([guest, project, stage, item])
            db.flush()
            db.add(cdb.ProjectRemoteGrant(
                id=GRANT_ID,
                project_id=PROJECT_ID,
                guest_id=int(guest.id),
                handle_snapshot=guest.handle,
                role="editor",
                status="active",
                invited_by="owner",
            ))
            db.commit()
        finally:
            db.close()

        app = FastAPI()
        app.add_middleware(ProjectAttachmentBodyLimitMiddleware)
        app.include_router(project_routes.setup_project_routes(
            ProjectFileStore(files_path),
            prefix="/api/link/projects",
            remote_only=True,
            dependencies=[Depends(link_routes.require_link_project_remote)],
        ))
        server = uvicorn.Server(uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="error",
            lifespan="off",
        ))
        server.install_signal_handlers = lambda: None

        async def run() -> None:
            task = asyncio.create_task(server.serve())
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Hub stopped before becoming ready")
                await asyncio.sleep(0.02)
            ready_queue.put(("ready", ""))
            while not stop_event.is_set() and not task.done():
                await asyncio.sleep(0.05)
            server.should_exit = True
            await task

        asyncio.run(run())
    except BaseException:
        ready_queue.put(("error", traceback.format_exc()))
    finally:
        if engine is not None:
            engine.dispose()


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _LocalAuth:
    is_configured = True

    @staticmethod
    def is_admin(username: str) -> bool:
        return str(username or "").lower() == "admin"


def _attachment_rows(database_path: Path):
    with sqlite3.connect(database_path) as db:
        return db.execute(
            "SELECT uploader, storage_key, original_name, size FROM project_attachments "
            "ORDER BY created_at, id"
        ).fetchall()


def test_editor_upload_crosses_two_real_instances_and_viewer_stays_read_only(
    monkeypatch,
    tmp_path,
):
    pytest.importorskip("uvicorn")
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    stop_event = context.Event()
    hub_db = tmp_path / "hub.db"
    hub_files = tmp_path / "hub-project-files"
    port = _available_port()
    process = context.Process(
        target=_serve_hub,
        args=(port, str(hub_db), str(hub_files), ready_queue, stop_event),
    )
    process.start()

    local_engine = None
    try:
        try:
            state, detail = ready_queue.get(timeout=60)
        except queue.Empty:
            pytest.fail("Authoritative Restia did not start")
        assert state == "ready", detail

        local_engine, local_factory = _sqlite_factory(
            tmp_path / "local.db",
            (cdb.HomeLink,),
        )
        monkeypatch.setattr(link_routes, "SessionLocal", local_factory)
        db = local_factory()
        try:
            db.add(cdb.HomeLink(
                local_user=link_routes.INSTANCE_LINK_USER,
                home_url=f"http://127.0.0.1:{port}",
                handle="authoritative-restia",
                owner="owner",
                token=TOKEN,
                created_at=cdb.utcnow_naive(),
            ))
            db.commit()
        finally:
            db.close()

        local = FastAPI()
        local.state.auth_manager = _LocalAuth()

        @local.middleware("http")
        async def signed_in(request: Request, call_next):
            request.state.current_user = request.headers.get("x-test-user", "owner")
            request.state.api_token = False
            return await call_next(request)

        local.add_middleware(ProjectAttachmentBodyLimitMiddleware)
        local.include_router(link_routes.setup_home_link_routes())

        payload = b"separate-instance editor evidence\n"
        path = f"/api/homelink/projects/{PROJECT_ID}/items/{ITEM_ID}/attachments"
        with TestClient(local) as client:
            uploaded = client.post(
                path,
                headers={"X-Test-User": "owner"},
                files={"file": ("evidence.txt", payload, "text/plain")},
                data={"kind": "deliverable", "description": "Remote result"},
            )
            assert uploaded.status_code == 201, uploaded.text
            attachment = uploaded.json()["attachment"]
            assert attachment["name"] == "evidence.txt"
            assert TOKEN not in uploaded.text

            preview_path = (
                f"/api/homelink/projects/attachments/{attachment['id']}/view"
            )
            preview = client.get(
                preview_path,
                headers={"X-Test-User": "owner", "Range": "bytes=0-7"},
            )
            assert preview.status_code == 206, preview.text
            assert preview.content == payload[:8]
            assert preview.headers["content-range"] == f"bytes 0-7/{len(payload)}"
            assert preview.headers["content-disposition"].startswith("inline;")
            assert preview.headers["x-content-type-options"] == "nosniff"
            assert TOKEN not in preview.text

            rows = _attachment_rows(hub_db)
            assert len(rows) == 1
            uploader, storage_key, original_name, size = rows[0]
            assert uploader == f"remote:{GRANT_ID}"
            assert original_name == "evidence.txt"
            assert size == len(payload)
            assert (hub_files / storage_key).read_bytes() == payload

            wrong_profile = client.post(
                path,
                headers={"X-Test-User": "another-profile"},
                files={"file": ("wrong-profile.txt", b"denied\n", "text/plain")},
            )
            assert wrong_profile.status_code == 403
            assert wrong_profile.json()["detail"] == (
                "Only an admin or the Home Link owner can access linked projects"
            )
            wrong_profile_preview = client.get(
                preview_path,
                headers={"X-Test-User": "another-profile", "Range": "bytes=0-7"},
            )
            assert wrong_profile_preview.status_code == 403
            assert len(_attachment_rows(hub_db)) == 1

            with sqlite3.connect(hub_db) as db:
                db.execute(
                    "UPDATE project_remote_grants SET role='viewer', version=version + 1 "
                    "WHERE id=?",
                    (GRANT_ID,),
                )
                db.commit()

            viewer = client.post(
                path,
                headers={"X-Test-User": "owner"},
                files={"file": ("viewer.txt", b"denied\n", "text/plain")},
            )
            assert viewer.status_code == 403
            assert viewer.json()["detail"] == "Project role does not allow this action"
            viewer_preview = client.get(
                preview_path,
                headers={"X-Test-User": "owner", "Range": "bytes=0-7"},
            )
            assert viewer_preview.status_code == 206, viewer_preview.text
            assert viewer_preview.content == payload[:8]
            assert len(_attachment_rows(hub_db)) == 1
    finally:
        stop_event.set()
        process.join(timeout=15)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        ready_queue.close()
        if local_engine is not None:
            local_engine.dispose()
    assert process.exitcode == 0
