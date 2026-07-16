"""Exercise the DOM-free Inbox parsing and API contract in shipped JavaScript."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INBOX_JS = ROOT / "static" / "js" / "inbox.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _node_eval(body: str):
    source = f"""
      import * as inbox from {json.dumps(INBOX_JS.as_uri())};
      {body}
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_response_unwrapping_accepts_direct_and_wrapped_shapes():
    result = _node_eval(
        """
        const direct = [{ id: 'one', content: 'One' }];
        const wrapped = { items: [{ id: 'two', content: 'Two' }], count: 1 };
        const single = { item: { id: 'three', content: 'Three' } };
        console.log(JSON.stringify({
          direct: inbox.unwrapInboxItems(direct).map(row => row.id),
          wrapped: inbox.unwrapInboxItems(wrapped).map(row => row.id),
          singleList: inbox.unwrapInboxItems(single).map(row => row.id),
          single: inbox.unwrapInboxItem(single).id,
          directItem: inbox.unwrapInboxItem({ id: 'four', content: 'Four' }).id,
        }));
        """
    )

    assert result == {
        "direct": ["one"],
        "wrapped": ["two"],
        "singleList": ["three"],
        "single": "three",
        "directItem": "four",
    }


def test_normalization_keeps_textual_source_classification_and_reason():
    result = _node_eval(
        """
        const item = inbox.normalizeInboxItem({
          item_id: 'capture-1', text: 'First line\\nMore detail', source: 'telegram',
          classification: { label: 'project_information', confidence: 0.82, reason: 'project cue' },
          status: 'inbox', version: 4, updatedAt: '2026-07-16T12:00:00Z',
        });
        console.log(JSON.stringify({
          id: item.id, title: item.title, content: item.content, source: item.sourceType,
          kind: item.kind, confidence: item.confidence, reason: item.reason,
          version: item.version, confidenceCopy: inbox.__test.formatConfidence(item.confidence),
        }));
        """
    )

    assert result == {
        "id": "capture-1",
        "title": "First line",
        "content": "First line\nMore detail",
        "source": "telegram",
        "kind": "project_information",
        "confidence": 0.82,
        "reason": "project cue",
        "version": 4,
        "confidenceCopy": "Confidence 82%",
    }


def test_capture_patch_and_actions_use_canonical_versioned_payloads():
    result = _node_eval(
        """
        const calls = [];
        globalThis.fetch = async (url, options = {}) => {
          calls.push({ url, method: options.method, body: JSON.parse(options.body || '{}') });
          return {
            ok: true, status: 200,
            text: async () => JSON.stringify({ item: {
              id: 'item-1', title: '', content: 'Remember this', kind: 'task',
              status: 'inbox', source_type: 'user', version: 2,
            }}),
          };
        };
        await inbox.createInboxCapture('Remember this', { idempotencyKey: 'capture-retry-key' });
        await inbox.createInboxCapture('Remember this', { idempotencyKey: 'capture-retry-key' });
        await inbox.patchInboxItem('item-1', 2, { title: 'Remember' });
        await inbox.mutateInboxItem('item-1', 'classify', 3);
        await inbox.mutateInboxItem('item-1', 'process', 4);
        await inbox.mutateInboxItem('item-1', 'archive', 5);
        console.log(JSON.stringify(calls));
        """
    )

    assert result[0] == {
        "url": "/api/inbox",
        "method": "POST",
        "body": {
            "title": "Remember this",
            "content": "Remember this",
            "source_type": "user",
            "idempotency_key": "capture-retry-key",
        },
    }
    assert result[1]["body"]["idempotency_key"] == "capture-retry-key"
    assert result[2] == {
        "url": "/api/inbox/item-1",
        "method": "PATCH",
        "body": {"title": "Remember", "version": 2},
    }
    assert [row["url"] for row in result[3:]] == [
        "/api/inbox/item-1/classify",
        "/api/inbox/item-1/process",
        "/api/inbox/item-1/archive",
    ]
    assert [row["body"]["version"] for row in result[3:]] == [3, 4, 5]


def test_list_pagination_sends_cursor_and_merges_pages_without_duplicates():
    result = _node_eval(
        """
        const calls = [];
        const responses = [
          { items: [
              { id: 'three', title: 'Three', kind: 'note', status: 'inbox' },
              { id: 'two', title: 'Two', kind: 'note', status: 'inbox' },
            ], count: 2, truncated: true, next_cursor: 'signed.cursor/one' },
          { items: [
              { id: 'two', title: 'Stale duplicate', kind: 'note', status: 'inbox' },
              { id: 'one', title: 'One', kind: 'note', status: 'inbox' },
            ], count: 2, truncated: false, next_cursor: null },
        ];
        globalThis.fetch = async (url, options = {}) => {
          calls.push({ url, method: options.method });
          return {
            ok: true, status: 200,
            text: async () => JSON.stringify(responses.shift()),
          };
        };
        const first = await inbox.listInboxItems('inbox', { limit: 2 });
        const second = await inbox.listInboxItems('inbox', {
          limit: 2, cursor: first.nextCursor,
        });
        const merged = inbox.mergeInboxItems(first.items, second.items);
        console.log(JSON.stringify({
          calls: calls.map(call => {
            const url = new URL(call.url, 'http://restia.test');
            return {
              method: call.method,
              status: url.searchParams.get('status'),
              limit: url.searchParams.get('limit'),
              cursor: url.searchParams.get('cursor'),
            };
          }),
          first: {
            ids: first.items.map(item => item.id), nextCursor: first.nextCursor,
            count: first.count, truncated: first.truncated,
          },
          second: {
            ids: second.items.map(item => item.id), nextCursor: second.nextCursor,
            count: second.count, truncated: second.truncated,
          },
          merged: merged.map(item => ({ id: item.id, title: item.title })),
        }));
        """
    )

    assert result["calls"] == [
        {"method": "GET", "status": "inbox", "limit": "2", "cursor": None},
        {
            "method": "GET",
            "status": "inbox",
            "limit": "2",
            "cursor": "signed.cursor/one",
        },
    ]
    assert result["first"] == {
        "ids": ["three", "two"],
        "nextCursor": "signed.cursor/one",
        "count": 2,
        "truncated": True,
    }
    assert result["second"] == {
        "ids": ["two", "one"],
        "nextCursor": None,
        "count": 2,
        "truncated": False,
    }
    assert result["merged"] == [
        {"id": "three", "title": "Three"},
        {"id": "two", "title": "Two"},
        {"id": "one", "title": "One"},
    ]


def test_unsupported_processing_is_disabled_and_409_is_not_reported_as_success():
    result = _node_eval(
        """
        const availability = Object.fromEntries(['task', 'archive', 'note', 'project_information'].map(kind => {
          const item = inbox.normalizeInboxItem({ id: kind, content: kind, kind, status: 'inbox' });
          return [kind, inbox.__test.processAvailability(item)];
        }));
        globalThis.fetch = async () => ({
          ok: false, status: 409,
          text: async () => JSON.stringify({
            detail: { status: 'unsupported', kind: 'email', message: 'No safe adapter yet' },
          }),
        });
        let error = null;
        try { await inbox.mutateInboxItem('email-1', 'process', 1); }
        catch (caught) { error = { message: caught.message, status: caught.status }; }
        console.log(JSON.stringify({ availability, error }));
        """
    )

    assert result["availability"]["task"]["enabled"] is True
    assert result["availability"]["archive"]["enabled"] is False
    assert "Use Archive below" in result["availability"]["archive"]["message"]
    assert result["availability"]["note"]["enabled"] is False
    assert "No safe automatic processor" in result["availability"]["note"]["message"]
    assert result["availability"]["project_information"]["enabled"] is False
    assert "project destination" in result["availability"]["project_information"]["message"]
    assert result["error"] == {"message": "No safe adapter yet (Email)", "status": 409}
