import asyncio

import pytest


@pytest.mark.asyncio
async def test_memory_dream_offloads_blocking_mnemosyne_call(monkeypatch):
    from src import builtin_actions
    from src import memory as memory_module

    calls = []

    class FakeMnemosyne:
        def sleep_all_sessions(self, *, force):
            raise AssertionError("blocking dream call ran on the event loop")

    class FakeMemoryManager:
        def __init__(self, _data_dir):
            self.mnemo = FakeMnemosyne()

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return {"compressed_clusters": 3}

    monkeypatch.setattr(memory_module, "MemoryManager", FakeMemoryManager)
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    message, ok = await builtin_actions.action_dream_memory("admin")

    assert ok is True
    assert message == "Completed memory dream cycle. Compressed 3 clusters."
    assert len(calls) == 1
    func, args, kwargs = calls[0]
    assert func.__self__.__class__ is FakeMnemosyne
    assert func.__name__ == "sleep_all_sessions"
    assert args == ()
    assert kwargs == {"force": True}
