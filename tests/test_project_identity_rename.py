"""Profile renames migrate every identity-bearing Projects column."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import core.database as cdb
import routes.auth_routes as auth_routes
from routes.auth_routes import RenameUserRequest, setup_auth_routes


def _route(router, name):
    return next(
        route.endpoint
        for route in router.routes
        if getattr(route.endpoint, "__name__", "") == name
    )


class _AuthManager:
    def __init__(self):
        self.users = {"admin": {}, "alice": {}}
        self.rename_calls = []
        self.rollback_calls = []

    def get_username_for_token(self, token):
        return "admin" if token == "admin-token" else None

    def is_admin(self, username):
        return username == "admin"

    def rename_user(self, old_username, new_username, requesting_user):
        self.rename_calls.append((old_username, new_username, requesting_user))
        self.users[new_username] = self.users.pop(old_username)
        return True

    def rollback_user_rename(self, old_username, new_username, requesting_user):
        self.rollback_calls.append((old_username, new_username, requesting_user))
        if old_username not in self.users or new_username in self.users:
            return False
        self.users[new_username] = self.users.pop(old_username)
        return True


def test_profile_rename_migrates_every_project_identity_column(monkeypatch, tmp_path):
    database_path = tmp_path / "rename-project-identity.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")
    cdb.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)

    db = factory()
    project = cdb.Project(
        id="project-1",
        owner="Alice",
        key="REST",
        name="Restia",
    )
    stage = cdb.ProjectStage(
        id="stage-1",
        project_id=project.id,
        name="Review",
        category="review",
        position=0,
    )
    item = cdb.ProjectWorkItem(
        id="item-1",
        project_id=project.id,
        stage_id=stage.id,
        item_number=1,
        title="Review controls report",
        reporter="ALICE",
        assignee="aLiCe",
    )
    db.add(project)
    db.flush()
    db.add(stage)
    db.flush()
    db.add(item)
    db.flush()
    guest = cdb.LinkGuest(
        handle="remote-one",
        token_hash="a" * 64,
        status="approved",
    )
    db.add(guest)
    db.flush()
    db.add_all(
        [
            cdb.ProjectQuotaLock(
                key=cdb.project_owner_quota_lock_key("alice"),
            ),
            cdb.ProjectMember(
                project_id=project.id,
                username="Alice",
                role="editor",
                added_by="ALICE",
            ),
            cdb.ProjectMember(
                project_id=project.id,
                username="bob",
                role="viewer",
                added_by="bob",
            ),
            cdb.ProjectRemoteGrant(
                id="grant-1",
                project_id=project.id,
                guest_id=guest.id,
                handle_snapshot=guest.handle,
                role="editor",
                status="active",
                invited_by="ALICE",
            ),
            cdb.ProjectChecklistItem(
                id="check-1",
                work_item_id=item.id,
                text="Verify equations",
                created_by="aLiCe",
            ),
            cdb.ProjectComment(
                id="comment-1",
                work_item_id=item.id,
                author="Alice",
                body="Please review",
            ),
            cdb.ProjectAttachment(
                id="attachment-1",
                work_item_id=item.id,
                uploader="ALICE",
                kind="draft",
                original_name="controls.docx",
                storage_key="project-1/item-1/attachment-1.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                size=12,
                sha256="b" * 64,
            ),
            cdb.ProjectActivity(
                id="activity-1",
                project_id=project.id,
                work_item_id=item.id,
                actor="aLiCe",
                event_type="comment_added",
                summary="Added a review note",
            ),
        ]
    )
    db.commit()
    db.close()

    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(auth_routes, "DEEP_RESEARCH_DIR", str(tmp_path / "research"))
    monkeypatch.setattr(auth_routes, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(auth_routes, "SKILLS_DIR", str(tmp_path / "skills"))

    import routes.prefs_routes as prefs_routes

    monkeypatch.setattr(prefs_routes, "_load", lambda: {})
    monkeypatch.setattr(prefs_routes, "_save", lambda _data: None)

    invalidations = []
    request = SimpleNamespace(
        cookies={"odysseus_session": "admin-token"},
        app=SimpleNamespace(
            state=SimpleNamespace(
                research_handler=None,
                upload_handler=None,
                personal_docs_manager=None,
                session_manager=None,
                invalidate_token_cache=lambda: invalidations.append(True),
            )
        ),
    )
    manager = _AuthManager()
    endpoint = _route(setup_auth_routes(manager), "rename_user")

    result = asyncio.run(
        endpoint("ALICE", RenameUserRequest(username="Alice2"), request)
    )

    db = factory()
    assert db.query(cdb.Project).filter(cdb.Project.id == "project-1").one().owner == "alice2"

    alice_member = (
        db.query(cdb.ProjectMember)
        .filter(cdb.ProjectMember.project_id == "project-1", cdb.ProjectMember.username == "alice2")
        .one()
    )
    assert alice_member.added_by == "alice2"
    bob_member = (
        db.query(cdb.ProjectMember)
        .filter(cdb.ProjectMember.project_id == "project-1", cdb.ProjectMember.username == "bob")
        .one()
    )
    assert bob_member.added_by == "bob"

    renamed_item = db.query(cdb.ProjectWorkItem).filter(cdb.ProjectWorkItem.id == "item-1").one()
    assert renamed_item.reporter == "alice2"
    assert renamed_item.assignee == "alice2"
    assert db.query(cdb.ProjectChecklistItem).one().created_by == "alice2"
    assert db.query(cdb.ProjectComment).one().author == "alice2"
    assert db.query(cdb.ProjectAttachment).one().uploader == "alice2"
    assert db.query(cdb.ProjectActivity).one().actor == "alice2"
    assert db.query(cdb.ProjectRemoteGrant).one().invited_by == "alice2"
    assert db.query(cdb.ProjectQuotaLock).filter(
        cdb.ProjectQuotaLock.key == cdb.project_owner_quota_lock_key("alice")
    ).count() == 0
    db.close()
    engine.dispose()

    assert manager.rename_calls == [("alice", "alice2", "admin")]
    assert manager.rollback_calls == []
    assert manager._identity_migrations == set()
    assert invalidations == [True]
    assert result == {"ok": True, "username": "alice2", "renamed_self": False}


def test_profile_rename_rolls_back_remote_grant_attribution_on_sql_failure(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / "rename-project-identity-rollback.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")
    cdb.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)

    db = factory()
    project = cdb.Project(
        id="project-rollback",
        owner="Alice",
        key="ROLL",
        name="Rollback",
    )
    guest = cdb.LinkGuest(
        handle="remote-rollback",
        token_hash="c" * 64,
        status="approved",
    )
    db.add_all([project, guest])
    db.flush()
    db.add_all(
        [
            cdb.ProjectRemoteGrant(
                id="grant-rollback",
                project_id=project.id,
                guest_id=guest.id,
                handle_snapshot=guest.handle,
                role="viewer",
                status="pending",
                invited_by="ALICE",
            ),
            cdb.ProjectActivity(
                id="activity-rollback",
                project_id=project.id,
                actor="Alice",
                event_type="remote_instance_invited",
                summary="Invited a linked Restia",
            ),
        ]
    )
    db.commit()
    db.close()

    # Fail after the grant-attribution UPDATE. The surrounding SQL transaction
    # must restore every earlier identity rewrite before auth is rolled back.
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TRIGGER fail_project_activity_identity_rename
            BEFORE UPDATE OF actor ON project_activity
            WHEN lower(OLD.actor) = 'alice'
            BEGIN
                SELECT RAISE(ABORT, 'forced project identity rename failure');
            END
        """))

    monkeypatch.setattr(cdb, "SessionLocal", factory)
    manager = _AuthManager()
    request = SimpleNamespace(
        cookies={"odysseus_session": "admin-token"},
        app=SimpleNamespace(state=SimpleNamespace()),
    )
    endpoint = _route(setup_auth_routes(manager), "rename_user")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            endpoint("alice", RenameUserRequest(username="alice2"), request)
        )

    assert exc_info.value.status_code == 500
    db = factory()
    assert db.query(cdb.Project).one().owner == "Alice"
    assert db.query(cdb.ProjectRemoteGrant).one().invited_by == "ALICE"
    assert db.query(cdb.ProjectActivity).one().actor == "Alice"
    db.close()
    engine.dispose()

    assert manager.users == {"admin": {}, "alice": {}}
    assert manager.rename_calls == [("alice", "alice2", "admin")]
    assert manager.rollback_calls == [("alice2", "alice", "admin")]
    assert manager._identity_migrations == set()
