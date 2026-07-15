"""Project Danger Zone wipes remove only the Projects domain."""

from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Base,
    Note,
    Project,
    ProjectActivity,
    ProjectAttachment,
    ProjectChecklistItem,
    ProjectComment,
    ProjectMember,
    ProjectQuotaLock,
    ProjectStage,
    ProjectWorkItem,
)
from routes.admin_wipe_routes import setup_admin_wipe_routes


def test_wipe_projects_removes_all_project_rows_and_files_only(monkeypatch, tmp_path):
    database_path = tmp_path / "wipe-projects.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)

    db = factory()
    project = Project(
        id="project-1",
        owner="alice",
        key="REST",
        name="Restia",
    )
    stage = ProjectStage(
        id="stage-1",
        project_id=project.id,
        name="In Progress",
        category="in_progress",
        position=0,
    )
    item = ProjectWorkItem(
        id="item-1",
        project_id=project.id,
        stage_id=stage.id,
        item_number=1,
        title="Ship the Projects workspace",
        reporter="alice",
    )
    # Flush the FK spine explicitly. ProjectActivity intentionally has no ORM
    # relationship to its work item, so SQLAlchemy cannot infer that ordering
    # when the whole graph is staged at once.
    db.add(project)
    db.flush()
    db.add(stage)
    db.flush()
    db.add(item)
    db.flush()
    db.add_all(
        [
            ProjectMember(
                project_id=project.id,
                username="bob",
                role="editor",
                added_by="alice",
            ),
            ProjectChecklistItem(
                id="check-1",
                work_item_id=item.id,
                text="Run regressions",
                created_by="alice",
            ),
            ProjectComment(
                id="comment-1",
                work_item_id=item.id,
                author="alice",
                body="Ready for review",
            ),
            ProjectAttachment(
                id="attachment-1",
                work_item_id=item.id,
                uploader="alice",
                kind="deliverable",
                original_name="report.pdf",
                storage_key="project-1/item-1/attachment-1.pdf",
                mime="application/pdf",
                size=8,
                sha256="a" * 64,
            ),
            ProjectActivity(
                id="activity-1",
                project_id=project.id,
                work_item_id=item.id,
                actor="alice",
                event_type="work_submitted",
                summary="Submitted report.pdf",
            ),
            ProjectQuotaLock(
                key="project-activity:project-1",
                project_id=project.id,
            ),
            Note(id="unrelated-note", owner="alice", title="Keep me"),
        ]
    )
    db.commit()
    db.close()

    project_files = tmp_path / "project_files"
    stored_file = project_files / "project-1" / "item-1" / "attachment-1.pdf"
    stored_file.parent.mkdir(parents=True)
    stored_file.write_bytes(b"%PDF-1.7")
    unrelated_files = tmp_path / "uploads"
    unrelated_files.mkdir()
    unrelated_file = unrelated_files / "keep.txt"
    unrelated_file.write_text("unrelated", encoding="utf-8")

    import routes.admin_wipe_routes as wipe_routes

    monkeypatch.setattr(wipe_routes, "SessionLocal", factory)
    monkeypatch.setattr(wipe_routes, "PROJECT_FILES_DIR", str(project_files))
    monkeypatch.setattr(wipe_routes, "require_admin", lambda _request: None)

    router = setup_admin_wipe_routes(session_manager=None)
    handler = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/admin/wipe/{kind}"
    )
    result = handler(kind="projects", request=Request(scope={"type": "http"}))

    db = factory()
    project_models = (
        ProjectActivity,
        ProjectQuotaLock,
        ProjectAttachment,
        ProjectChecklistItem,
        ProjectComment,
        ProjectWorkItem,
        ProjectStage,
        ProjectMember,
        Project,
    )
    assert all(db.query(model).count() == 0 for model in project_models)
    assert db.query(Note).filter(Note.id == "unrelated-note").count() == 1
    db.close()
    engine.dispose()

    assert result == {"status": "deleted", "kind": "projects", "count": 1}
    assert not Path(project_files).exists()
    assert unrelated_file.read_text(encoding="utf-8") == "unrelated"
