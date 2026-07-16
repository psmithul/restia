"""Contracts for the host-neutral, multi-contact Restia invitation UI."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGING = ROOT / "static" / "js" / "messaging.js"
CALLS = ROOT / "static" / "js" / "call.js"
SOURCE = MESSAGING.read_text(encoding="utf-8")
CALL_SOURCE = CALLS.read_text(encoding="utf-8")
HAS_NODE = shutil.which("node") is not None


def test_messages_exposes_generic_restia_invite_and_connect_actions():
    lowered = SOURCE.lower()
    assert "chat with the developer" not in lowered
    assert "msg-dev-tag" not in SOURCE
    assert "Connect another Restia" in SOURCE
    assert "Invite another Restia" in SOURCE
    assert "/api/link/admin/invites" in SOURCE
    assert "/api/homelink/chat/redeem" in SOURCE
    assert "/api/homelink/chat/connect" in SOURCE
    assert "restia-invite:v1?" in SOURCE
    assert "home_url: homeUrl" in SOURCE


def test_chat_only_restia_contacts_never_offer_call_controls():
    assert "meta.can_call === false || meta.chat_only" in CALL_SOURCE
    assert "if (!meta?.chat_only) return '';" in SOURCE
    assert "/api/homelink/chat/${encodeURIComponent(contactId)}/disconnect" in SOURCE


@pytest.mark.skipif(not HAS_NODE, reason="Node.js is required for the frontend parser contract")
def test_invitation_parser_accepts_https_and_loopback_only():
    harness = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(process.argv.at(-1), 'utf8');
const start = source.indexOf('function _parseRestiaInvitation');
const end = source.indexOf('function _setNewChatHeader', start);
assert.ok(start >= 0 && end > start);
const parserSource = source.slice(start, end);
const context = vm.createContext({
  URL,
  URLSearchParams,
  RESTIA_INVITE_PREFIX: 'restia-invite:v1?',
  result: null,
});
vm.runInContext(parserSource, context);

const invite = (hub, scope = 'chat') =>
  `restia-invite:v1?scope=${encodeURIComponent(scope)}&hub=${encodeURIComponent(hub)}&code=secret-code`;
assert.equal(
  JSON.stringify(vm.runInContext(`_parseRestiaInvitation(${JSON.stringify(invite('https://Peer.Example:443/'))})`, context)),
  JSON.stringify({ homeUrl: 'https://peer.example', code: 'secret-code' }),
);
assert.equal(
  vm.runInContext(`_parseRestiaInvitation(${JSON.stringify(invite('http://127.0.0.1:7000'))}).homeUrl`, context),
  'http://127.0.0.1:7000',
);
assert.equal(
  vm.runInContext(`_parseRestiaInvitation(${JSON.stringify(invite('http://localhost:7000'))}).homeUrl`, context),
  'http://localhost:7000',
);
assert.equal(
  vm.runInContext(`_parseRestiaInvitation(${JSON.stringify(invite('http://[::1]:7000'))}).homeUrl`, context),
  'http://[::1]:7000',
);
for (const invalid of [
  invite('http://peer.example'),
  invite('https://user:pass@peer.example'),
  invite('https://peer.example/path'),
  invite('https://peer.example', 'project'),
  'not-an-invitation',
]) {
  assert.throws(() => vm.runInContext(`_parseRestiaInvitation(${JSON.stringify(invalid)})`, context));
}
"""
    subprocess.run(
        ["node", "--input-type=module", "-", str(MESSAGING)],
        input=harness,
        text=True,
        check=True,
        cwd=ROOT,
        capture_output=True,
    )
