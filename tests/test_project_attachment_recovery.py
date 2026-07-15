"""Cancellation and startup recovery never leak or delete project files."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

import pytest

import routes.project_routes as project_routes
from src.project_storage import (
    ProjectFileStore,
    write_project_attachment_cancellation_safe,
)


def _make_old(path, *, seconds=7_200):
    timestamp = time.time() - seconds
    os.utime(path, (timestamp, timestamp))


def test_cancelled_thread_write_settles_then_deletes_final_file(tmp_path):
    class BlockingStore(ProjectFileStore):
        def __init__(self, root):
            super().__init__(root)
            self.started = threading.Event()
            self.release = threading.Event()
            self.events = []

        def write(self, storage_key, data):
            self.events.append("write-started")
            self.started.set()
            assert self.release.wait(timeout=2), "test did not release writer"
            result = super().write(storage_key, data)
            self.events.append("write-finished")
            return result

        def delete(self, storage_key):
            self.events.append("delete")
            super().delete(storage_key)

    store = BlockingStore(tmp_path / "project-files")
    key = store.storage_key("project-1", "item-1", "attachment-1", ".pdf")

    async def scenario():
        task = asyncio.create_task(
            write_project_attachment_cancellation_safe(
                store,
                key,
                b"%PDF-1.7\n%%EOF",
            )
        )
        started = await asyncio.to_thread(store.started.wait, 1)
        assert started is True
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        # Cancellation is deliberately delayed until the worker settles; an
        # early return (including after a second cancellation) would race
        # cleanup against the thread's os.replace.
        assert task.done() is False
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() is True

    asyncio.run(scenario())

    assert store.events == ["write-started", "write-finished", "delete"]
    assert not store.resolve(key, must_exist=False).exists()
    assert not list(store.root.rglob(".project-file-*"))


def test_reconcile_deletes_only_old_confined_orphans_and_temps(
    tmp_path,
    caplog,
):
    store = ProjectFileStore(tmp_path / "project-files")
    valid_key = store.storage_key("project-1", "item-1", "valid-1", ".pdf")
    orphan_key = store.storage_key("project-1", "item-1", "orphan-1", ".pdf")
    recent_key = store.storage_key("project-1", "item-1", "recent-1", ".pdf")
    missing_key = store.storage_key("project-1", "item-1", "missing-1", ".pdf")

    valid_path = store.write(valid_key, b"%PDF-valid")
    orphan_path = store.write(orphan_key, b"%PDF-orphan")
    recent_path = store.write(recent_key, b"%PDF-recent")
    temporary = orphan_path.parent / ".project-file-crashed-writer"
    temporary.write_bytes(b"partial")
    for old_path in (valid_path, orphan_path, temporary):
        _make_old(old_path)

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    symlink = orphan_path.parent / "symlink.pdf"
    try:
        symlink.symlink_to(outside)
    except (OSError, NotImplementedError):
        symlink = None

    with caplog.at_level(logging.WARNING, logger="src.project_storage"):
        stats = store.reconcile(
            [valid_key, missing_key],
            grace_seconds=3_600,
            max_entries=100,
            time_budget_seconds=5,
        )

    assert valid_path.exists()
    assert recent_path.exists()
    assert not orphan_path.exists()
    assert not temporary.exists()
    assert outside.read_bytes() == b"outside"
    if symlink is not None:
        assert symlink.is_symlink()
    assert stats["referenced"] == 2
    assert stats["missing"] == 1
    assert stats["deleted_orphans"] == 1
    assert stats["deleted_temps"] == 1
    assert stats["skipped_recent"] == 1
    assert stats["truncated"] is False
    assert "missing file" in caplog.text


def test_incomplete_reference_scan_aborts_deletion(tmp_path):
    store = ProjectFileStore(tmp_path / "project-files")
    first_key = store.storage_key("project-1", "item-1", "first-1", ".pdf")
    late_key = store.storage_key("project-1", "item-1", "late-1", ".pdf")
    first_path = store.write(first_key, b"%PDF-first")
    late_path = store.write(late_key, b"%PDF-late")
    _make_old(first_path)
    _make_old(late_path)

    def slow_references():
        yield first_key
        time.sleep(0.02)
        yield late_key

    stats = store.reconcile(
        slow_references(),
        grace_seconds=0,
        max_entries=100,
        time_budget_seconds=0.005,
    )

    assert stats["truncated"] is True
    assert stats["scanned_entries"] == 0
    assert stats["deleted_orphans"] == 0
    assert first_path.exists()
    assert late_path.exists()


def test_project_router_setup_invokes_one_reconciliation_pass(monkeypatch, tmp_path):
    class Query:
        def yield_per(self, _size):
            return iter([("project-1/item-1/file-1.pdf",)])

    class Session:
        def __init__(self):
            self.closed = False

        def query(self, _column):
            return Query()

        def close(self):
            self.closed = True

    session = Session()
    monkeypatch.setattr(project_routes, "SessionLocal", lambda: session)
    store = ProjectFileStore(tmp_path / "project-files")
    calls = []
    monkeypatch.setattr(
        store,
        "reconcile",
        lambda keys: calls.append(list(keys)) or {"truncated": False},
    )

    project_routes.setup_project_routes(store)

    assert calls == [["project-1/item-1/file-1.pdf"]]
    assert session.closed is True
