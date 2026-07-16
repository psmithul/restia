"""Declarative registration for the first Restia V2 backend feature areas."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends

from src.v2.feature_registry import FeatureContext, FeatureRegistry, FeatureSpec
from src.v2.llm_provider import LLMProvider


def build_v2_feature_registry(
    *,
    rag_manager: Any,
    memory_vector: Any,
    require_link_project_remote: Callable[..., Any],
    llm_provider: LLMProvider,
) -> FeatureRegistry:
    """Build, but do not install, the deterministic V2 feature registry."""

    context = FeatureContext(
        services={
            "rag_manager": rag_manager,
            "memory_vector": memory_vector,
            "require_link_project_remote": require_link_project_remote,
        },
        llm_provider=llm_provider,
    )
    registry = FeatureRegistry(context)

    def mission_control(ctx: FeatureContext):
        from routes.mission_control_routes import setup_mission_control_routes

        return setup_mission_control_routes(
            ctx.optional("rag_manager"),
            ctx.optional("memory_vector"),
        )

    def projects(_ctx: FeatureContext):
        from routes.project_routes import setup_project_routes

        return setup_project_routes()

    def linked_projects(ctx: FeatureContext):
        from routes.project_routes import setup_project_routes

        return setup_project_routes(
            prefix="/api/link/projects",
            remote_only=True,
            dependencies=[Depends(ctx.require("require_link_project_remote"))],
        )

    def calendar(_ctx: FeatureContext):
        from routes.calendar_routes import setup_calendar_routes

        return setup_calendar_routes()

    registry.register(FeatureSpec("mission-control", 100, router_factory=mission_control))
    registry.register(FeatureSpec("projects", 110, router_factory=projects))
    registry.register(FeatureSpec("linked-projects", 120, router_factory=linked_projects))
    registry.register(FeatureSpec("calendar", 130, router_factory=calendar))
    return registry
