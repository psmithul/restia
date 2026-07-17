from pathlib import Path

from fastapi.routing import APIRoute

from routes.action_policy_routes import setup_action_policy_routes
from routes.focus_routes import setup_focus_routes
from routes.life_routes import setup_life_routes


ROOT = Path(__file__).resolve().parents[1]


def _signatures(router):
    return {
        (method, route.path)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }


def test_real_app_registers_each_planning_spine_router_once():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    for setup_name in (
        "setup_life_routes",
        "setup_action_policy_routes",
        "setup_focus_routes",
    ):
        assert source.count(f"app.include_router({setup_name}())") == 1


def test_planning_spine_route_contracts_are_distinct_and_complete():
    signatures = set()
    for router in (
        setup_life_routes(),
        setup_action_policy_routes(),
        setup_focus_routes(),
    ):
        current = _signatures(router)
        assert not signatures.intersection(current)
        signatures.update(current)

    required = {
        ("POST", "/api/life/sources"),
        ("POST", "/api/life/entities"),
        ("GET", "/api/life/search"),
        ("GET", "/api/life/tasks/quality"),
        ("POST", "/api/life/links"),
        ("PUT", "/api/life/policies/{domain}"),
        ("POST", "/api/life/actions"),
        ("POST", "/api/life/actions/{proposal_id}/approve"),
        ("POST", "/api/life/actions/{proposal_id}/execute"),
        ("POST", "/api/life/focus/start"),
        ("GET", "/api/life/focus/current"),
        ("POST", "/api/life/focus/{session_id}/complete"),
    }
    assert required <= signatures
