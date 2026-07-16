from __future__ import annotations

import asyncio

from src.v2.llm_provider import RestiaLLMProvider


def test_restia_llm_provider_delegates_every_model_mode(monkeypatch):
    calls = []

    async def complete(url, model, messages, **options):
        calls.append(("complete", url, model, messages, options))
        return "done"

    async def complete_fallback(candidates, messages, **options):
        calls.append(("complete-fallback", candidates, messages, options))
        return "fallback-done"

    async def stream(url, model, messages, **options):
        calls.append(("stream", url, model, messages, options))
        yield "one"
        yield "two"

    async def stream_fallback(candidates, messages, **options):
        calls.append(("stream-fallback", candidates, messages, options))
        yield "fallback-one"

    import src.llm_core as llm_core

    monkeypatch.setattr(llm_core, "llm_call_async", complete)
    monkeypatch.setattr(llm_core, "llm_call_async_with_fallback", complete_fallback)
    monkeypatch.setattr(llm_core, "stream_llm", stream)
    monkeypatch.setattr(llm_core, "stream_llm_with_fallback", stream_fallback)

    provider = RestiaLLMProvider()
    messages = [{"role": "user", "content": "hello"}]
    candidates = [("http://local/v1", "model", None)]

    async def exercise():
        assert await provider.complete(
            "http://local/v1", "model", messages, workload="background"
        ) == "done"
        assert await provider.complete_with_fallback(
            candidates, messages, timeout=12
        ) == "fallback-done"
        assert [chunk async for chunk in provider.stream(
            "http://local/v1", "model", messages, timeout=9
        )] == ["one", "two"]
        assert [chunk async for chunk in provider.stream_with_fallback(
            candidates, messages, timeout=8
        )] == ["fallback-one"]

    asyncio.run(exercise())

    assert [call[0] for call in calls] == [
        "complete",
        "complete-fallback",
        "stream",
        "stream-fallback",
    ]
