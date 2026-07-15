"""Schema invariants for project-scoped Home Link grants."""

from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from core.database import Base, LinkGuest, Project, ProjectRemoteGrant


def _factory():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _seed(db):
    project = Project(id="project-1", owner="alice", key="REST", name="Restia")
    guest = LinkGuest(
        handle="remote-one",
        token_hash="a" * 64,
        status="approved",
    )
    db.add_all([project, guest])
    db.commit()
    return project, guest


def test_remote_grant_is_unique_per_project_guest_and_survives_guest_delete():
    engine, factory = _factory()
    db = factory()
    project, guest = _seed(db)
    grant = ProjectRemoteGrant(
        id="grant-1",
        project_id=project.id,
        guest_id=guest.id,
        handle_snapshot=guest.handle,
        role="editor",
        status="pending",
        invited_by="alice",
    )
    db.add(grant)
    db.commit()

    db.add(
        ProjectRemoteGrant(
            id="grant-2",
            project_id=project.id,
            guest_id=guest.id,
            handle_snapshot=guest.handle,
            role="viewer",
            status="pending",
            invited_by="alice",
        )
    )
    try:
        db.commit()
        raise AssertionError("duplicate project/guest grant unexpectedly committed")
    except IntegrityError:
        db.rollback()

    db.delete(guest)
    db.commit()
    retained = db.query(ProjectRemoteGrant).filter_by(id="grant-1").one()
    assert retained.guest_id is None
    assert retained.handle_snapshot == "remote-one"

    db.delete(project)
    db.commit()
    assert db.query(ProjectRemoteGrant).count() == 0
    db.close()
    engine.dispose()
