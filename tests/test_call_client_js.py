"""Behavioral browser-state tests for ``static/js/call.js``.

The production module imports the large UI stack, so the harness links its two
imports to narrow synthetic modules and executes the real call module in a VM
browser context. This exercises behavior without relying on source-text checks
or requiring a camera, network, or full browser installation.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent
_CALL_JS = _REPO / "static" / "js" / "call.js"
_HAS_NODE = shutil.which("node") is not None


_HARNESS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const callPath = process.argv.at(-1);
const source = fs.readFileSync(callPath, 'utf8');
const FIXED_CALL_ID = '8e288d0b-f6a8-4ec3-a12f-a30f33d76993';
let loadSequence = 0;

async function loadCall({ secure = true, failKinds = [], deferMedia = false,
  homeUnreachable = false,
  notificationPermission = 'granted', fullscreen = false,
  config = { enabled: true, turn: false, ice_servers: [{ urls: 'stun:test' }],
    can_home_call: false, can_remote_call: false } } = {}) {
  const requests = [];
  const requestUrls = [];
  const errors = [];
  const toasts = [];
  const pcs = [];
  const timers = new Map();
  let nextTimer = 1;
  let eventSource = null;
  const eventSources = [];
  let currentConfig = config;
  let mediaCalls = 0;
  let mediaResolve = null;
  let stoppedTracks = 0;
  let uuidCalls = 0;
  let notificationPermissionRequests = 0;
  let focusCalls = 0;
  const notifications = [];

  const fakeElement = (id = '') => ({
    id,
    style: {},
    className: '',
    innerHTML: '',
    textContent: '',
    srcObject: null,
    addEventListener() {},
    remove() {},
  });
  const elements = new Map();
  const document = {
    fullscreenElement: fullscreen ? {} : null,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, fakeElement(id));
      return elements.get(id);
    },
    createElement() { return fakeElement(); },
    body: { appendChild(el) { if (el.id) elements.set(el.id, el); } },
  };

  class FakeNotification {
    static permission = notificationPermission;
    static async requestPermission() {
      notificationPermissionRequests += 1;
      FakeNotification.permission = 'granted';
      return 'granted';
    }
    constructor(title, options) {
      this.title = title;
      this.options = options;
      this.closed = false;
      notifications.push(this);
    }
    close() { this.closed = true; }
  }

  const audioTrack = { enabled: true, stop() { stoppedTracks += 1; } };
  const videoTrack = { enabled: true, stop() { stoppedTracks += 1; } };
  const stream = {
    getTracks: () => [audioTrack, videoTrack],
    getAudioTracks: () => [audioTrack],
    getVideoTracks: () => [videoTrack],
  };

  class FakeEventSource {
    constructor(url) {
      this.url = String(url); this.listeners = new Map(); this.closed = false;
      eventSource = this; eventSources.push(this);
    }
    addEventListener(kind, fn) { this.listeners.set(kind, fn); }
    close() { this.closed = true; }
    emit(kind, payload) {
      const fn = this.listeners.get(kind);
      if (fn) fn({ data: JSON.stringify(payload) });
    }
  }

  class FakePeerConnection {
    constructor(config) {
      this.config = config;
      this.connectionState = 'new';
      this.remoteDescription = null;
      this.localDescription = null;
      this.remoteSets = [];
      this.candidates = [];
      pcs.push(this);
    }
    addTrack() {}
    async createOffer() { return { type: 'offer', sdp: 'v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n' }; }
    async createAnswer() { return { type: 'answer', sdp: 'v=0\r\no=- 2 2 IN IP4 127.0.0.1\r\n' }; }
    async setLocalDescription(description) {
      this.localDescription = description;
      // Deliberately emit ICE before setLocalDescription resolves. The module
      // must hold it until the offer/answer POST has completed.
      if (this.onicecandidate) {
        this.onicecandidate({ candidate: { toJSON: () => ({
          candidate: 'candidate:1 1 UDP 2122260223 192.0.2.1 5000 typ host',
          sdpMid: '0', sdpMLineIndex: 0, usernameFragment: 'abcd',
        }) } });
        // Firefox exposes the end-of-generation marker as a non-null
        // RTCIceCandidate whose candidate string is empty.
        this.onicecandidate({ candidate: { toJSON: () => ({
          candidate: '', sdpMid: '0', sdpMLineIndex: 0, usernameFragment: 'abcd',
        }) } });
      }
    }
    async setRemoteDescription(description) {
      this.remoteDescription = description;
      this.remoteSets.push(description);
    }
    async addIceCandidate(candidate) { this.candidates.push(candidate); }
    close() { this.connectionState = 'closed'; }
    connect() {
      this.connectionState = 'connected';
      if (this.onconnectionstatechange) this.onconnectionstatechange();
    }
  }

  const response = (ok, status, body) => ({ ok, status, json: async () => body });
  async function fetch(url, opts = {}) {
    if (String(url).endsWith('/api/calls/config')) {
      return response(true, 200, currentConfig);
    }
    const body = JSON.parse(opts.body || '{}');
    requests.push(body);
    requestUrls.push(String(url));
    if (homeUnreachable && String(url).endsWith('/api/homelink/calls/signal')) {
      return response(false, 502, { detail: 'Home server unreachable (ConnectError)' });
    }
    if (failKinds.includes(body.kind)) return response(false, 503, { detail: `${body.kind} unavailable` });
    return response(true, 200, { ok: true });
  }

  const context = vm.createContext({
    console,
    document,
    fetch,
    EventSource: FakeEventSource,
    RTCPeerConnection: FakePeerConnection,
    isSecureContext: secure,
    navigator: {
      mediaDevices: {
        async getUserMedia() {
          mediaCalls += 1;
          if (deferMedia) return new Promise(resolve => { mediaResolve = () => resolve(stream); });
          return stream;
        },
      },
    },
    crypto: {
      randomUUID() { uuidCalls += 1; return FIXED_CALL_ID; },
    },
    Notification: FakeNotification,
    focus() { focusCalls += 1; },
    setTimeout(fn, delay) {
      const id = nextTimer++;
      timers.set(id, { fn, delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
  });

  const ui = {
    showError(message) { errors.push(String(message)); },
    showToast(message) { toasts.push(String(message)); },
    esc(value) { return String(value); },
  };
  const uiModule = new vm.SyntheticModule(['default'], function () {
    this.setExport('default', ui);
  }, { context });
  const zModule = new vm.SyntheticModule(['topPortalZ'], function () {
    this.setExport('topPortalZ', () => 10031);
  }, { context });
  const mod = new vm.SourceTextModule(source, {
    context,
    identifier: `file://${callPath}?test=${++loadSequence}`,
  });
  await mod.link(async (specifier) => {
    if (specifier.endsWith('/ui.js') || specifier === './ui.js') return uiModule;
    if (specifier.endsWith('/toolWindowZOrder.js') || specifier === './toolWindowZOrder.js') return zModule;
    throw new Error(`unexpected import: ${specifier}`);
  });
  await mod.evaluate();

  return {
    ns: mod.namespace,
    requests,
    requestUrls,
    errors,
    toasts,
    pcs,
    eventSource: () => eventSource,
    eventSources,
    setConfig(value) { currentConfig = value; },
    mediaCalls: () => mediaCalls,
    stoppedTracks: () => stoppedTracks,
    resolveMedia() { assert.ok(mediaResolve, 'media request was not pending'); mediaResolve(); },
    uuidCalls: () => uuidCalls,
    notifications,
    notificationPermissionRequests: () => notificationPermissionRequests,
    focusCalls: () => focusCalls,
    async emit(payload) {
      eventSource.emit('call', payload);
      await new Promise(resolve => setImmediate(resolve));
    },
    async runTimer(delay) {
      const match = [...timers.entries()].find(([, timer]) => timer.delay === delay);
      assert.ok(match, `missing ${delay}ms timer`);
      timers.delete(match[0]);
      match[1].fn();
      await new Promise(resolve => setImmediate(resolve));
    },
  };
}

{
  const h = await loadCall({ notificationPermission: 'default', fullscreen: true });
  assert.equal(await h.ns.requestNotificationPermission(), false);
  assert.equal(h.notificationPermissionRequests(), 0,
    'Firefox permission prompts must not run while DOM fullscreen is active');
}

{
  const h = await loadCall();
  await h.ns.init();
  await h.emit({ from: 'alice', call_id: FIXED_CALL_ID, kind: 'offer',
    data: { sdp: 'v=0\r\no=alice\r\n', video: true } });
  assert.equal(h.notifications.length, 1);
  assert.equal(h.notifications[0].options.requireInteraction, true);
  assert.equal(h.notifications[0].options.renotify, true);
  await h.runTimer(12_000);
  assert.equal(h.notifications.length, 2,
    'an unanswered call should re-notify until its ring lifecycle ends');
  h.ns.declineCall();
  assert.equal(h.notifications.at(-1).closed, true,
    'declining must close the persistent notification');
}

{
  const h = await loadCall({ secure: false });
  await h.ns.init();
  await h.ns.startCall('alice', true);
  assert.equal(h.mediaCalls(), 0, 'insecure origins must not request media');
  assert.equal(h.ns.isBusy(), false);
  assert.match(h.errors.at(-1), /HTTPS or on localhost/);
}

{
  const h = await loadCall();
  await h.ns.init();
  await h.ns.startCall('alice', true);
  assert.equal(h.uuidCalls(), 1);
  assert.deepEqual(h.requests.map(r => r.kind), ['offer', 'ice', 'ice'],
    'SDP must precede trickled ICE, including the end-of-candidates marker');
  assert.equal(h.requests[0].call_id, FIXED_CALL_ID);
  assert.equal(h.requests[0].data.video, true);
  assert.equal(h.requests[2].data.candidate.candidate, '',
    'the browser end-of-candidates marker must retain its standard wire shape');

  const pc = h.pcs[0];
  await h.emit({ from: 'mallory', call_id: FIXED_CALL_ID, kind: 'answer',
    data: { sdp: 'v=0\r\no=mallory\r\n' } });
  assert.equal(pc.remoteSets.length, 0, 'same call id from another peer must be ignored');

  await h.emit({ from: 'alice', call_id: FIXED_CALL_ID, kind: 'answer',
    data: { sdp: 'v=0\r\no=alice\r\n' } });
  assert.equal(pc.remoteSets.length, 1);
  for (const lateKind of ['decline', 'busy', 'cancel']) {
    await h.emit({ from: 'alice', call_id: FIXED_CALL_ID, kind: lateKind, data: {} });
    assert.equal(h.ns.isBusy(), true, `late ${lateKind} must not tear down an answered call`);
  }
  await h.runTimer(30_000);
  assert.equal(h.ns.isBusy(), false, 'connect timeout must clean up the call');
  assert.match(h.errors.at(-1), /STUN only/);
}

{
  const h = await loadCall();
  await h.ns.init();
  await h.ns.startCall('alice', false);
  await h.runTimer(45_000);
  assert.equal(h.ns.isBusy(), false, 'ring timeout must clean up the call');
  assert.equal(h.requests.at(-1).kind, 'cancel');
  assert.match(h.toasts.at(-1), /did not answer/);
}

{
  const h = await loadCall({ failKinds: ['offer'] });
  await h.ns.init();
  await h.ns.startCall('alice', true);
  assert.equal(h.ns.isBusy(), false);
  assert.match(h.errors.at(-1), /offer unavailable/);
  assert.doesNotMatch(h.errors.at(-1), /STUN only|TURN relay/,
    'same-origin signaling failures are unrelated to ICE relay availability');
}

{
  const h = await loadCall({ deferMedia: true });
  await h.ns.init();
  const starting = h.ns.startCall('alice', true);
  await new Promise(resolve => setImmediate(resolve));
  h.ns.hangup();
  h.resolveMedia();
  await starting;
  assert.equal(h.ns.isBusy(), false);
  assert.equal(h.requests.some(r => r.kind === 'offer'), false, 'a cancelled media prompt must not send an offer');
  assert.equal(h.stoppedTracks(), 2, 'late media tracks must be stopped after cancellation');
}

{
  const h = await loadCall({ config: {
    enabled: false, turn: false, ice_servers: [], can_home_call: false, can_remote_call: false,
  } });
  await h.ns.init();
  assert.equal(h.eventSources.length, 0, 'disabled calling must not open a retrying SSE stream');
}

{
  const base = {
    enabled: true, turn: true, ice_servers: [{ urls: 'stun:test' }],
    can_home_call: false, can_remote_call: false,
  };
  const h = await loadCall({ config: base });
  await h.ns.init();
  assert.deepEqual(h.eventSources.map(e => e.url), ['/api/calls/stream']);
  h.setConfig({ ...base, can_home_call: true });
  await h.ns.refreshConfig();
  assert.deepEqual(h.eventSources.map(e => e.url),
    ['/api/calls/stream', '/api/homelink/calls/stream']);
  const oldHomeStream = h.eventSources[1];
  await h.ns.refreshConfig();
  assert.equal(oldHomeStream.closed, true, 're-pairing must close the old bearer-backed stream');
  assert.equal(h.eventSources.at(-1).url, '/api/homelink/calls/stream');
  // The local proxy opening is not enough: media must wait until the proxy
  // confirms its authenticated upstream subscription.
  h.eventSources.at(-1).emit('open', {});
  const starting = h.ns.startCall('hub.example', false, { home: true });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.mediaCalls(), 0);
  assert.equal(h.ns.isBusy(), true, 'Home preflight reserves the call slot');
  h.eventSources.at(-1).emit('call-transport', { status: 'ready' });
  await starting;
  assert.ok(h.requestUrls.every(url => url.endsWith('/api/homelink/calls/signal')));
  assert.equal('to' in h.requests[0], false, 'federated envelope must omit profile routing fields');
  h.eventSources.at(-1).onerror();
  await h.runTimer(5_000);
  assert.equal(h.ns.isBusy(), false,
    'an active Home Link call must end if its authenticated signal stream stays down');
  assert.match(h.errors.at(-1), /signaling was lost/);
  assert.doesNotMatch(h.errors.at(-1), /STUN only|TURN relay/);
}

{
  const h = await loadCall({ config: {
    enabled: true, turn: false, ice_servers: [{ urls: 'stun:test' }],
    can_home_call: true, can_remote_call: false,
  } });
  // startCall must also establish its own signaling streams if module init has
  // not completed yet; callers should not be able to bypass the preflight.
  const starting = h.ns.startCall('hub.example', false, { home: true });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(h.eventSources.map(e => e.url),
    ['/api/calls/stream', '/api/homelink/calls/stream']);
  h.eventSources.at(-1).emit('open', {});
  await h.runTimer(8_000);
  await starting;
  assert.equal(h.mediaCalls(), 0, 'an unreachable Home server must fail before media access');
  assert.equal(h.requests.length, 0, 'preflight failure must not emit an offer');
  assert.equal(h.ns.isBusy(), false);
  assert.match(h.errors.at(-1), /Home Link signaling cannot reach/);
  assert.match(h.errors.at(-1), /reconnect Home Link in Messages/);
  assert.match(h.errors.at(-1), /TURN is not involved/);
  assert.doesNotMatch(h.errors.at(-1), /STUN only|configure a TURN relay/);
}

{
  const h = await loadCall({ homeUnreachable: true, config: {
    enabled: true, turn: false, ice_servers: [{ urls: 'stun:test' }],
    can_home_call: true, can_remote_call: false,
  } });
  await h.ns.init();
  h.eventSources.at(-1).emit('call-transport', { status: 'ready' });
  await h.ns.startCall('hub.example', false, { home: true });
  assert.equal(h.ns.isBusy(), false);
  assert.match(h.errors.at(-1), /Home Link signaling cannot reach/);
  assert.match(h.errors.at(-1), /TURN is not involved/);
  assert.doesNotMatch(h.errors.at(-1), /STUN only|configure a TURN relay|ConnectError/);
}

{
  const h = await loadCall({ homeUnreachable: true, config: {
    enabled: true, turn: false, ice_servers: [{ urls: 'stun:test' }],
    can_home_call: true, can_remote_call: false,
  } });
  await h.ns.init();
  h.eventSources.at(-1).emit('call-transport', { status: 'ready' });
  await h.emit({ from: 'hub.example', call_id: FIXED_CALL_ID, kind: 'offer',
    data: { sdp: 'v=0\r\no=hub\r\n', video: false } });
  await h.ns.acceptCall();
  assert.equal(h.ns.isBusy(), false);
  assert.match(h.errors.at(-1), /Home Link signaling cannot reach/);
  assert.match(h.errors.at(-1), /TURN is not involved/);
  assert.doesNotMatch(h.errors.at(-1), /STUN only|configure a TURN relay|ConnectError/);
}

console.log('ok');
"""


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_call_client_security_and_timeout_state_machine():
    proc = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-", str(_CALL_JS)],
        input=_HARNESS,
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
