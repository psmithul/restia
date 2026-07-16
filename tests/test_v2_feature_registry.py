from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI

from src.v2.bootstrap import build_v2_feature_registry
from src.v2.feature_registry import (
    FeatureConfigurationError,
    FeatureContext,
    FeatureLifecycleError,
    FeatureRegistrationError,
    FeatureRegistry,
    FeatureSpec,
    _route_signatures,
)
from src.v2.llm_provider import LLMProvider
from src.constants import APP_VERSION


class StubLLMProvider(LLMProvider):
    async def complete(self, url, model, messages, **options):
        return "complete"

    async def complete_with_fallback(self, candidates, messages, **options):
        return "fallback"

    async def stream(self, url, model, messages, **options):
        yield "stream"

    async def stream_with_fallback(self, candidates, messages, **options):
        yield "fallback-stream"


def _context(**services) -> FeatureContext:
    return FeatureContext(services=services, llm_provider=StubLLMProvider())


def _router(path: str, label: str) -> APIRouter:
    router = APIRouter()

    @router.get(path)
    async def endpoint():
        return {"feature": label}

    return router


def test_registration_order_is_explicit_and_deterministic():
    app = FastAPI()
    registry = FeatureRegistry(_context())
    registry.register(FeatureSpec("zulu", 20, lambda _ctx: _router("/v2/z", "z")))
    registry.register(FeatureSpec("beta", 10, lambda _ctx: _router("/v2/b", "b")))
    registry.register(FeatureSpec("alpha", 10, lambda _ctx: _router("/v2/a", "a")))

    registry.install(app)

    assert registry.feature_names == ("alpha", "beta", "zulu")
    assert [path for path in app.openapi()["paths"] if path.startswith("/v2/")] == [
        "/v2/a",
        "/v2/b",
        "/v2/z",
    ]
    assert app.state.v2_feature_registry is registry


def test_duplicate_feature_or_route_fails_before_app_mutation():
    registry = FeatureRegistry(_context())
    registry.register(FeatureSpec("one", 1, lambda _ctx: _router("/v2/one", "one")))
    with pytest.raises(FeatureRegistrationError, match="already registered"):
        registry.register(FeatureSpec("one", 2, lambda _ctx: _router("/v2/two", "two")))

    app = FastAPI()

    @app.get("/v2/items/{item_id}")
    async def existing(item_id: str):
        return {"id": item_id}

    duplicate = FeatureRegistry(_context())
    duplicate.register(
        FeatureSpec("duplicate", 1, lambda _ctx: _router("/v2/items/{id}", "duplicate"))
    )
    before = tuple((route.path, getattr(route, "methods", None)) for route in app.routes)

    with pytest.raises(FeatureRegistrationError, match=r"duplicates GET /v2/items/\{\*\}"):
        duplicate.install(app)

    assert tuple((route.path, getattr(route, "methods", None)) for route in app.routes) == before


def test_final_route_validation_rejects_late_v2_collision_only():
    app = FastAPI()
    registry = FeatureRegistry(_context())
    registry.register(FeatureSpec("owned", 1, lambda _ctx: _router("/v2/owned", "v2")))
    registry.install(app)

    app.include_router(_router("/unrelated", "legacy"))
    registry.validate_final_routes(app)

    app.include_router(_router("/v2/owned", "late-legacy"))
    with pytest.raises(
        FeatureRegistrationError,
        match=r"owned.*GET /v2/owned.*found 2 routes",
    ):
        registry.validate_final_routes(app)


def test_router_factory_errors_are_named_and_loud():
    registry = FeatureRegistry(_context())

    def broken(_ctx):
        raise ValueError("missing dependency")

    registry.register(FeatureSpec("broken-feature", 1, router_factory=broken))
    with pytest.raises(
        FeatureRegistrationError,
        match="broken-feature.*ValueError.*missing dependency",
    ):
        registry.install(FastAPI())


def test_lifecycle_runs_once_in_order_and_shutdown_reverses_order():
    events: list[str] = []

    def spec(name: str, order: int) -> FeatureSpec:
        async def startup(_ctx, _app):
            events.append(f"start:{name}")

        async def shutdown(_ctx, _app):
            events.append(f"stop:{name}")

        return FeatureSpec(name, order, startup=startup, shutdown=shutdown)

    app = FastAPI()
    registry = FeatureRegistry(_context())
    registry.register(spec("second", 20))
    registry.register(spec("first", 10))
    registry.install(app)

    asyncio.run(registry.startup(app))
    with pytest.raises(FeatureLifecycleError, match="state 'started'"):
        asyncio.run(registry.startup(app))

    asyncio.run(registry.shutdown(app))
    with pytest.raises(FeatureLifecycleError, match="state 'stopped'"):
        asyncio.run(registry.shutdown(app))

    assert events == ["start:first", "start:second", "stop:second", "stop:first"]


def test_startup_failure_rolls_back_and_names_the_feature():
    events: list[str] = []

    async def first_start(_ctx, _app):
        events.append("start:first")

    async def first_stop(_ctx, _app):
        events.append("stop:first")

    async def broken_start(_ctx, _app):
        events.append("start:broken")
        raise RuntimeError("boom")

    async def broken_stop(_ctx, _app):
        events.append("stop:broken")

    app = FastAPI()
    registry = FeatureRegistry(_context())
    registry.register(FeatureSpec("first", 10, startup=first_start, shutdown=first_stop))
    registry.register(
        FeatureSpec("broken", 20, startup=broken_start, shutdown=broken_stop)
    )
    registry.install(app)

    with pytest.raises(FeatureLifecycleError, match="broken.*RuntimeError.*boom"):
        asyncio.run(registry.startup(app))

    assert registry.state == registry.FAILED
    assert events == ["start:first", "start:broken", "stop:broken", "stop:first"]


def test_v2_context_rejects_raw_model_clients_and_missing_services():
    with pytest.raises(FeatureConfigurationError, match="LLMProvider"):
        FeatureContext(services={}, llm_provider=object())

    context = _context(optional_value=None)
    with pytest.raises(FeatureConfigurationError, match="required but unavailable"):
        context.require("optional_value")
    with pytest.raises(FeatureConfigurationError, match="required but unavailable"):
        context.require("missing")


def test_product_registry_declares_the_v2_feature_areas_in_one_order():
    async def remote_guard():
        return None

    registry = build_v2_feature_registry(
        rag_manager=None,
        memory_vector=None,
        require_link_project_remote=remote_guard,
        llm_provider=StubLLMProvider(),
    )

    assert registry.feature_names == (
        "mission-control",
        "progression",
        "planning",
        "projects",
        "linked-projects",
        "calendar",
    )


def test_product_registry_installs_each_actual_v2_route_once():
    async def remote_guard():
        return None

    app = FastAPI()
    registry = build_v2_feature_registry(
        rag_manager=None,
        memory_vector=None,
        require_link_project_remote=remote_guard,
        llm_provider=StubLLMProvider(),
    )

    registry.install(app)

    signatures = _route_signatures(app.router)
    assert len(signatures) == len(set(signatures))
    assert ("GET", "/api/mission-control/today") in signatures
    assert ("GET", "/api/mission-control/activity") in signatures
    assert ("GET", "/api/progression") in signatures
    assert ("GET", "/api/planning") in signatures
    assert ("GET", "/api/projects") in signatures
    assert ("GET", "/api/link/projects") in signatures
    assert ("GET", "/api/calendar/events") in signatures


def test_app_owns_v2_registration_and_lifecycle_at_one_call_site():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")

    assert source.count("v2_feature_registry.install(app)") == 1
    assert source.count("v2_feature_registry.validate_final_routes(app)") == 1
    assert source.count("await v2_feature_registry.startup(app)") == 1
    assert source.count("await v2_feature_registry.shutdown(app)") == 1
    assert "from routes.mission_control_routes import setup_mission_control_routes" not in source
    assert "from routes.project_routes import setup_project_routes" not in source
    assert "from routes.calendar_routes import setup_calendar_routes" not in source
    assert source.index("v2_feature_registry.validate_final_routes(app)") > source.index(
        '@app.get("/api/runtime")'
    )


def test_v2_release_identity_uses_the_shared_version_constant():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")

    assert APP_VERSION == "2.1.0"
    assert "version=APP_VERSION" in source
    assert 'title="Restia"' in source


def test_new_v2_bootstrap_modules_do_not_bypass_llm_provider():
    repo = Path(__file__).resolve().parents[1]
    guarded = [repo / "src/v2/bootstrap.py", repo / "src/v2/feature_registry.py"]

    for path in guarded:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert "src.llm_core" not in imports, f"{path} bypasses the V2 LLMProvider"
