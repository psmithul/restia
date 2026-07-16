"""Single model-call interface for new Restia V2 backend features.

``src.llm_core`` remains the compatibility facade for the existing product.
The adapter below deliberately imports it lazily so importing the V2 bootstrap
does not initialize provider clients or perform model discovery.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from typing import Any


Message = dict[str, Any]
ModelCandidate = tuple[str, str, dict[str, str] | None]


class LLMProvider(ABC):
    """The only model-call capability exposed to new V2 feature modules."""

    @abstractmethod
    async def complete(
        self,
        url: str,
        model: str,
        messages: list[Message],
        **options: Any,
    ) -> str:
        """Return one complete model response."""

    @abstractmethod
    async def complete_with_fallback(
        self,
        candidates: Sequence[ModelCandidate],
        messages: list[Message],
        **options: Any,
    ) -> str:
        """Return one response from an ordered endpoint fallback chain."""

    @abstractmethod
    def stream(
        self,
        url: str,
        model: str,
        messages: list[Message],
        **options: Any,
    ) -> AsyncIterator[str]:
        """Stream the canonical Restia SSE chunks for one endpoint."""

    @abstractmethod
    def stream_with_fallback(
        self,
        candidates: Sequence[ModelCandidate],
        messages: list[Message],
        **options: Any,
    ) -> AsyncIterator[str]:
        """Stream through the canonical ordered endpoint fallback chain."""


class RestiaLLMProvider(LLMProvider):
    """Backward-compatible adapter over Restia's proven ``llm_core`` seam."""

    async def complete(
        self,
        url: str,
        model: str,
        messages: list[Message],
        **options: Any,
    ) -> str:
        from src.llm_core import llm_call_async

        return await llm_call_async(url, model, messages, **options)

    async def complete_with_fallback(
        self,
        candidates: Sequence[ModelCandidate],
        messages: list[Message],
        **options: Any,
    ) -> str:
        from src.llm_core import llm_call_async_with_fallback

        return await llm_call_async_with_fallback(candidates, messages, **options)

    async def stream(
        self,
        url: str,
        model: str,
        messages: list[Message],
        **options: Any,
    ) -> AsyncIterator[str]:
        from src.llm_core import stream_llm

        async for chunk in stream_llm(url, model, messages, **options):
            yield chunk

    async def stream_with_fallback(
        self,
        candidates: Sequence[ModelCandidate],
        messages: list[Message],
        **options: Any,
    ) -> AsyncIterator[str]:
        from src.llm_core import stream_llm_with_fallback

        async for chunk in stream_llm_with_fallback(candidates, messages, **options):
            yield chunk
