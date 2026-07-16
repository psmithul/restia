from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mcp_startup_propagates_cancellation_and_stops_before_disconnect():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    startup = source[
        source.index("    async def _startup_mcp_connections():"):
        source.index("    _startup_tasks.append(asyncio.create_task(_startup_mcp_connections()))")
    ]
    shutdown = source[source.index("async def _shutdown_event():"):]

    # Registration and connect are separate cancellation points. Neither may
    # swallow CancelledError and continue into a late reconnect.
    assert startup.count("except asyncio.CancelledError:") == 2
    assert startup.count("except asyncio.CancelledError:\n            raise") == 2

    cancel = shutdown.index("task.cancel()")
    await_tasks = shutdown.index("await asyncio.gather(*startup_tasks")
    disconnect = shutdown.index("await mcp_manager.disconnect_all()")
    assert cancel < await_tasks < disconnect
