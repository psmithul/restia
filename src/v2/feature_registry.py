"""Deterministic router and lifecycle ownership for Restia V2 features."""

from __future__ import annotations

import inspect
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from fastapi import APIRouter, FastAPI

from src.v2.llm_provider import LLMProvider


class FeatureRegistryError(RuntimeError):
    """Base class for explicit V2 bootstrap failures."""


class FeatureConfigurationError(FeatureRegistryError):
    """A feature dependency or definition is invalid."""


class FeatureRegistrationError(FeatureRegistryError):
    """A feature router cannot be installed safely."""


class FeatureLifecycleError(FeatureRegistryError):
    """A feature startup or shutdown hook failed."""


@dataclass(frozen=True, slots=True)
class FeatureContext:
    """Typed capability container shared by V2 feature factories and hooks."""

    services: Mapping[str, Any]
    llm_provider: LLMProvider

    def __post_init__(self) -> None:
        if not isinstance(self.llm_provider, LLMProvider):
            raise FeatureConfigurationError(
                "V2 features require an LLMProvider instance; raw model clients are not allowed"
            )
        object.__setattr__(self, "services", MappingProxyType(dict(self.services)))

    @property
    def llm(self) -> LLMProvider:
        """The sole model capability available to V2 features."""

        return self.llm_provider

    def require(self, name: str) -> Any:
        value = self.services.get(name)
        if value is None:
            raise FeatureConfigurationError(
                f"V2 feature dependency '{name}' is required but unavailable"
            )
        return value

    def optional(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)


RouterFactory = Callable[[FeatureContext], APIRouter | Sequence[APIRouter]]
LifecycleHook = Callable[[FeatureContext, FastAPI], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """Declarative V2 feature definition.

    ``order`` is explicit so route and lifecycle order never depend on import or
    dictionary insertion order. Equal orders are resolved by feature name.
    """

    name: str
    order: int
    router_factory: RouterFactory | None = None
    startup: LifecycleHook | None = None
    shutdown: LifecycleHook | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedFeature:
    spec: FeatureSpec
    routers: tuple[APIRouter, ...]


_PATH_PARAMETER = re.compile(r"\{[^}:]+(?P<converter>:[^}]+)?\}")


def _normalized_path(path: str) -> str:
    """Normalize parameter names while preserving Starlette converters."""

    def replace(match: re.Match[str]) -> str:
        converter = match.group("converter") or ""
        return "{*" + converter + "}"

    return _PATH_PARAMETER.sub(replace, path)


def _route_signatures(router: Any, prefix: str = "") -> tuple[tuple[str, str], ...]:
    signatures: list[tuple[str, str]] = []
    for route in getattr(router, "routes", ()):
        # FastAPI 0.135+ retains included routers lazily instead of eagerly
        # flattening their APIRoutes. Traverse that wrapper so collision checks
        # remain correct on both old and new FastAPI versions.
        included = getattr(route, "original_router", None)
        if included is not None:
            include_context = getattr(route, "include_context", None)
            include_prefix = str(getattr(include_context, "prefix", "") or "")
            signatures.extend(_route_signatures(included, prefix + include_prefix))
            continue
        path = getattr(route, "path", None)
        if not path:
            continue
        effective_path = prefix + str(path)
        methods = getattr(route, "methods", None)
        if methods:
            signatures.extend(
                (str(method).upper(), _normalized_path(effective_path))
                for method in sorted(methods)
            )
        elif route.__class__.__name__.lower().endswith("websocketroute"):
            signatures.append(("WEBSOCKET", _normalized_path(effective_path)))
    return tuple(signatures)


class FeatureRegistry:
    """Install V2 routers atomically and run feature hooks exactly once."""

    COLLECTING = "collecting"
    INSTALLED = "installed"
    STARTED = "started"
    STOPPED = "stopped"
    FAILED = "failed"

    def __init__(self, context: FeatureContext):
        self.context = context
        self._specs: dict[str, FeatureSpec] = {}
        self._resolved: tuple[_ResolvedFeature, ...] = ()
        self._started: list[_ResolvedFeature] = []
        self._owned_routes: Mapping[tuple[str, str], str] = MappingProxyType({})
        self._state = self.COLLECTING
        self._app: FastAPI | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._ordered_specs())

    def register(self, spec: FeatureSpec) -> None:
        if self._state != self.COLLECTING:
            raise FeatureRegistrationError(
                f"Cannot register V2 feature '{spec.name}' after registry state '{self._state}'"
            )
        name = str(spec.name or "").strip()
        if not name:
            raise FeatureConfigurationError("V2 feature name must not be blank")
        if name != spec.name:
            raise FeatureConfigurationError(
                f"V2 feature name must be normalized; use '{name}' instead of {spec.name!r}"
            )
        if not isinstance(spec.order, int):
            raise FeatureConfigurationError(
                f"V2 feature '{name}' order must be an integer"
            )
        if name in self._specs:
            raise FeatureRegistrationError(f"V2 feature '{name}' is already registered")
        if spec.router_factory is None and spec.startup is None and spec.shutdown is None:
            raise FeatureConfigurationError(
                f"V2 feature '{name}' has no router or lifecycle capability"
            )
        self._specs[name] = spec

    def install(self, app: FastAPI) -> None:
        if self._state != self.COLLECTING:
            raise FeatureRegistrationError(
                f"V2 feature registry can only be installed once (state '{self._state}')"
            )

        owners: dict[tuple[str, str], str] = {}
        owned_routes: dict[tuple[str, str], str] = {}
        for signature in _route_signatures(app.router):
            owners.setdefault(signature, "the existing application")

        resolved: list[_ResolvedFeature] = []
        for spec in self._ordered_specs():
            try:
                produced = spec.router_factory(self.context) if spec.router_factory else ()
            except Exception as exc:
                raise FeatureRegistrationError(
                    f"V2 feature '{spec.name}' router factory failed: {type(exc).__name__}: {exc}"
                ) from exc

            routers = self._normalize_routers(spec, produced)
            for router in routers:
                for signature in _route_signatures(router):
                    previous = owners.get(signature)
                    if previous is not None:
                        method, path = signature
                        raise FeatureRegistrationError(
                            f"V2 feature '{spec.name}' duplicates {method} {path} already owned by {previous}"
                        )
                    owners[signature] = f"V2 feature '{spec.name}'"
                    owned_routes[signature] = spec.name
            resolved.append(_ResolvedFeature(spec=spec, routers=routers))

        # All factories and route signatures are validated before mutating the
        # application, so a registration failure cannot leave a half-installed
        # V2 surface.
        for feature in resolved:
            for router in feature.routers:
                app.include_router(router)

        self._resolved = tuple(resolved)
        self._owned_routes = MappingProxyType(dict(owned_routes))
        self._app = app
        self._state = self.INSTALLED
        app.state.v2_feature_registry = self

    def validate_final_routes(self, app: FastAPI) -> None:
        """Verify V2 route ownership after every application route is mounted."""

        if self._app is not app:
            raise FeatureRegistrationError(
                "Cannot validate V2 routes on an application other than the installed owner"
            )
        if self._state not in (self.INSTALLED, self.STARTED, self.STOPPED):
            raise FeatureRegistrationError(
                f"Cannot validate V2 routes from registry state '{self._state}'"
            )

        counts = Counter(_route_signatures(app.router))
        conflicts = [
            (feature_name, method, path, counts[(method, path)])
            for (method, path), feature_name in self._owned_routes.items()
            if counts[(method, path)] != 1
        ]
        if conflicts:
            detail = "; ".join(
                f"V2 feature '{feature}' owns {method} {path}, found {count} routes"
                for feature, method, path, count in conflicts
            )
            raise FeatureRegistrationError(
                "Final V2 route ownership validation failed: " + detail
            )

    def router_for(self, feature_name: str, index: int = 0) -> APIRouter:
        if self._state == self.COLLECTING:
            raise FeatureRegistrationError("V2 feature routers are unavailable before install()")
        for feature in self._resolved:
            if feature.spec.name == feature_name:
                try:
                    return feature.routers[index]
                except IndexError as exc:
                    raise FeatureRegistrationError(
                        f"V2 feature '{feature_name}' has no router at index {index}"
                    ) from exc
        raise FeatureRegistrationError(f"Unknown V2 feature '{feature_name}'")

    async def startup(self, app: FastAPI) -> None:
        self._assert_app_state(app, (self.INSTALLED, self.STOPPED), "start")
        self._started = []
        current: _ResolvedFeature | None = None
        try:
            for current in self._resolved:
                if current.spec.startup is not None:
                    await self._invoke_hook(current, current.spec.startup, app, "startup")
                self._started.append(current)
        except Exception as exc:
            rollback = [current] if current is not None else []
            rollback.extend(reversed(self._started))
            rollback_errors = await self._rollback(rollback, app)
            self._started = []
            self._state = self.FAILED
            detail = (
                "; rollback failures: " + "; ".join(rollback_errors)
                if rollback_errors
                else ""
            )
            feature_name = current.spec.name if current is not None else "unknown"
            raise FeatureLifecycleError(
                f"V2 feature '{feature_name}' startup failed: {type(exc).__name__}: {exc}{detail}"
            ) from exc
        self._state = self.STARTED

    async def shutdown(self, app: FastAPI) -> None:
        self._assert_app_state(app, (self.STARTED,), "stop")
        errors: list[str] = []
        for feature in reversed(self._started):
            hook = feature.spec.shutdown
            if hook is None:
                continue
            try:
                await self._invoke_hook(feature, hook, app, "shutdown")
            except Exception as exc:
                errors.append(f"{feature.spec.name}: {type(exc).__name__}: {exc}")
        self._started = []
        self._state = self.STOPPED
        if errors:
            raise FeatureLifecycleError(
                "V2 feature shutdown failed: " + "; ".join(errors)
            )

    def _ordered_specs(self) -> tuple[FeatureSpec, ...]:
        return tuple(sorted(self._specs.values(), key=lambda spec: (spec.order, spec.name)))

    @staticmethod
    def _normalize_routers(
        spec: FeatureSpec,
        produced: APIRouter | Sequence[APIRouter],
    ) -> tuple[APIRouter, ...]:
        if isinstance(produced, APIRouter):
            routers = (produced,)
        elif isinstance(produced, Sequence) and not isinstance(produced, (str, bytes)):
            routers = tuple(produced)
        else:
            raise FeatureRegistrationError(
                f"V2 feature '{spec.name}' router factory returned {type(produced).__name__}, expected APIRouter or sequence"
            )
        for router in routers:
            if not isinstance(router, APIRouter):
                raise FeatureRegistrationError(
                    f"V2 feature '{spec.name}' returned non-router value {type(router).__name__}"
                )
        return routers

    def _assert_app_state(
        self,
        app: FastAPI,
        expected: Sequence[str],
        verb: str,
    ) -> None:
        if self._app is not app:
            raise FeatureLifecycleError(
                f"Cannot {verb} V2 features on an application other than the installed owner"
            )
        if self._state not in expected:
            expected_text = " or ".join(f"'{state}'" for state in expected)
            raise FeatureLifecycleError(
                f"Cannot {verb} V2 features from registry state '{self._state}'; expected {expected_text}"
            )

    async def _invoke_hook(
        self,
        feature: _ResolvedFeature,
        hook: LifecycleHook,
        app: FastAPI,
        phase: str,
    ) -> None:
        result = hook(self.context, app)
        if not inspect.isawaitable(result):
            raise TypeError(
                f"{phase} hook for V2 feature '{feature.spec.name}' must be async"
            )
        await result

    async def _rollback(
        self,
        features: Sequence[_ResolvedFeature],
        app: FastAPI,
    ) -> list[str]:
        errors: list[str] = []
        seen: set[str] = set()
        for feature in features:
            if feature.spec.name in seen:
                continue
            seen.add(feature.spec.name)
            hook = feature.spec.shutdown
            if hook is None:
                continue
            try:
                await self._invoke_hook(feature, hook, app, "rollback shutdown")
            except Exception as exc:
                errors.append(f"{feature.spec.name}: {type(exc).__name__}: {exc}")
        return errors
