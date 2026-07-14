"""Study Mode data participates in its own and the full Danger Zone wipe."""

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, StudyState
from routes.admin_wipe_routes import setup_admin_wipe_routes


def test_wipe_study_clears_goal_and_timer_state(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(
        StudyState(
            id="user:alice",
            owner="alice",
            goal_text="Dynamics",
            target_minutes=600,
            total_seconds=900,
        )
    )
    db.commit()
    db.close()

    import routes.admin_wipe_routes as wipe_routes

    monkeypatch.setattr(wipe_routes, "SessionLocal", factory)
    monkeypatch.setattr(wipe_routes, "require_admin", lambda _request: None)
    router = setup_admin_wipe_routes(session_manager=None)
    handler = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/admin/wipe/{kind}"
    )

    result = handler(kind="study", request=Request(scope={"type": "http"}))
    db = factory()
    assert db.query(StudyState).count() == 0
    db.close()
    engine.dispose()
    assert result == {"status": "deleted", "kind": "study", "count": 1}
