"""Restia V2 application boundary.

New V2 backend features register through :mod:`src.v2.feature_registry` and
receive model access through the single :class:`LLMProvider` interface.
Legacy modules remain available while they are migrated behind this boundary.
"""

from src.v2.feature_registry import (
    FeatureConfigurationError,
    FeatureContext,
    FeatureLifecycleError,
    FeatureRegistrationError,
    FeatureRegistry,
    FeatureSpec,
)
from src.v2.llm_provider import LLMProvider, RestiaLLMProvider

__all__ = [
    "FeatureConfigurationError",
    "FeatureContext",
    "FeatureLifecycleError",
    "FeatureRegistrationError",
    "FeatureRegistry",
    "FeatureSpec",
    "LLMProvider",
    "RestiaLLMProvider",
]
