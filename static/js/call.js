// static/js/call.js
//
// WebRTC audio/video calling. The media is peer-to-peer and encrypted by the
// browser (DTLS-SRTP); the server (routes/call_routes.py) only relays the
// signaling — SDP offer/answer + ICE candidates + call control — over an
// always-on SSE stream, so a call can ring even with the Messages window shut.
//
// State machine: idle → (caller) calling → in-call, or (callee) ringing →
// in-call, then back to idle on hangup/decline/failure. One call at a time; a
// second incoming offer while busy is answered with `busy`.

import uiModule from './ui.js';
import { topPortalZ } from './toolWindowZOrder.js';

const API = '';

let _config = null;                 // {enabled, ice_servers}
let _es = null;                     // signaling EventSource
let _pc = null;                     // RTCPeerConnection
let _localStream = null;
let _remoteStream = null;
let _state = 'idle';                // idle | calling | ringing | incall
let _callId = null;
let _peer = null;                   // the other party's username
let _incoming = null;               // {from, call_id, sdp, video} while ringing
let _pendingCandidates = [];        // ICE that arrived before remoteDescription
let _wantVideo = true;

// ── Signaling transport ─────────────────────────────────────────────────────

async function _loadConfig() {
  if (_config) return _config;
  try { _config = await _api('/api/calls/config'); } catch (_) { _config = { enabled: false, ice_servers: [] }; }
  return _config;
}

async function _api(path, opts) {
  const res = await fetch(API + path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try { const j = await res.json(); msg = j.detail || j.error || msg; } catch (_) {}
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function _connectSignaling() {
  if (_es) return;
  try { _es = new EventSource(API + '/api/calls/stream'); } catch (_) { return; }
  _es.addEventListener('call', (e) => {
    let data; try { data = JSON.parse(e.data); } catch (_) { return; }
    _onSignal(data);
  });
  _es.onerror = () => {
    // Let the browser auto-reconnect; nothing to do but wait.
  };
}

function _send(to, kind, data) {
  return _api('/api/calls/signal', {
    method: 'POST',
    body: JSON.stringify({ to, call_id: _callId, kind, data: data || {} }),
  }).catch(() => {});
}

// ── Incoming signaling ──────────────────────────────────────────────────────

async function _onSignal(msg) {
  const { from, call_id, kind, data } = msg || {};
  if (!from || !kind) return;

  if (kind === 'offer') {
    // Busy with a different call → politely refuse.
    if (_state !== 'idle') {
      _api('/api/calls/signal', { method: 'POST',
        body: JSON.stringify({ to: from, call_id, kind: 'busy', data: {} }) }).catch(() => {});
      return;
    }
    _incoming = { from, call_id, sdp: data && data.sdp, video: !!(data && data.video) };
    _callId = call_id; _peer = from; _state = 'ringing';
    _renderIncoming();
    return;
  }
  // Everything else must match the active call.
  if (call_id !== _callId) return;

  if (kind === 'answer') {
    if (_pc && data && data.sdp) {
      await _pc.setRemoteDescription({ type: 'answer', sdp: data.sdp });
      await _flushCandidates();
      _state = 'incall';
      _renderInCall();
    }
  } else if (kind === 'ice') {
    if (data && data.candidate) {
      if (_pc && _pc.remoteDescription && _pc.remoteDescription.type) {
        try { await _pc.addIceCandidate(data.candidate); } catch (_) {}
      } else {
        _pendingCandidates.push(data.candidate);
      }
    }
  } else if (kind === 'decline') {
    uiModule.showToast && uiModule.showToast(`${_peer} declined`);
    _cleanup();
  } else if (kind === 'busy') {
    uiModule.showToast && uiModule.showToast(`${_peer} is busy`);
    _cleanup();
  } else if (kind === 'hangup' || kind === 'cancel') {
    _cleanup();
  }
}

async function _flushCandidates() {
  const pending = _pendingCandidates;
  _pendingCandidates = [];
  for (const c of pending) { try { await _pc.addIceCandidate(c); } catch (_) {} }
}

// ── Peer connection ─────────────────────────────────────────────────────────

function _newPeerConnection() {
  const pc = new RTCPeerConnection({ iceServers: (_config && _config.ice_servers) || [] });
  pc.onicecandidate = (e) => { if (e.candidate) _send(_peer, 'ice', { candidate: e.candidate.toJSON ? e.candidate.toJSON() : e.candidate }); };
  pc.ontrack = (e) => {
    _remoteStream = e.streams[0];
    const v = document.getElementById('call-remote-video');
    if (v && _remoteStream) v.srcObject = _remoteStream;
  };
  pc.onconnectionstatechange = () => {
    if (['failed', 'disconnected', 'closed'].includes(pc.connectionState) && _state === 'incall') {
      uiModule.showToast && uiModule.showToast('Call ended');
      _cleanup();
    }
  };
  return pc;
}

async function _getMedia(video) {
  return navigator.mediaDevices.getUserMedia({ audio: true, video: !!video });
}

// ── Public: start / accept / decline / hangup ───────────────────────────────

export async function startCall(peer, video = true) {
  if (!peer) return;
  const cfg = await _loadConfig();
  if (!cfg.enabled) {
    uiModule.showError && uiModule.showError(
      'Calling is not configured on this instance (a TURN server is required).');
    return;
  }
  if (_state !== 'idle') { uiModule.showToast && uiModule.showToast('Already in a call'); return; }
  _peer = peer; _wantVideo = !!video; _callId = 'c-' + Math.random().toString(36).slice(2, 10);
  _state = 'calling';
  _renderCalling();
  try {
    _localStream = await _getMedia(video);
  } catch (e) {
    uiModule.showError && uiModule.showError('Could not access camera/microphone');
    _cleanup();
    return;
  }
  _attachLocalPreview();
  _pc = _newPeerConnection();
  for (const t of _localStream.getTracks()) _pc.addTrack(t, _localStream);
  const offer = await _pc.createOffer();
  await _pc.setLocalDescription(offer);
  await _send(_peer, 'offer', { sdp: offer.sdp, video: !!video });
}

export async function acceptCall() {
  if (_state !== 'ringing' || !_incoming) return;
  _wantVideo = _incoming.video;
  try {
    _localStream = await _getMedia(_incoming.video);
  } catch (e) {
    uiModule.showError && uiModule.showError('Could not access camera/microphone');
    _send(_peer, 'decline', {});
    _cleanup();
    return;
  }
  _pc = _newPeerConnection();
  await _pc.setRemoteDescription({ type: 'offer', sdp: _incoming.sdp });
  for (const t of _localStream.getTracks()) _pc.addTrack(t, _localStream);
  await _flushCandidates();
  const answer = await _pc.createAnswer();
  await _pc.setLocalDescription(answer);
  await _send(_peer, 'answer', { sdp: answer.sdp });
  _state = 'incall';
  _incoming = null;
  _renderInCall();
  _attachLocalPreview();
}

export function declineCall() {
  if (_state !== 'ringing') return;
  _send(_peer, 'decline', {});
  _cleanup();
}

export function hangup() {
  if (_state === 'idle') return;
  _send(_peer, _state === 'calling' ? 'cancel' : 'hangup', {});
  _cleanup();
}

function _toggleMute() {
  if (!_localStream) return;
  const track = _localStream.getAudioTracks()[0];
  if (track) { track.enabled = !track.enabled; _renderControls(); }
}

function _toggleCamera() {
  if (!_localStream) return;
  const track = _localStream.getVideoTracks()[0];
  if (track) { track.enabled = !track.enabled; _renderControls(); }
}

function _cleanup() {
  try { if (_pc) _pc.close(); } catch (_) {}
  if (_localStream) { for (const t of _localStream.getTracks()) { try { t.stop(); } catch (_) {} } }
  _pc = null; _localStream = null; _remoteStream = null;
  _state = 'idle'; _callId = null; _peer = null; _incoming = null; _pendingCandidates = [];
  document.getElementById('call-overlay')?.remove();
  document.getElementById('call-incoming')?.remove();
}

// ── UI ───────────────────────────────────────────────────────────────────────

function _esc(s) { return uiModule.esc ? uiModule.esc(s) : String(s); }

function _renderIncoming() {
  document.getElementById('call-incoming')?.remove();
  const el = document.createElement('div');
  el.id = 'call-incoming';
  el.className = 'call-incoming';
  el.style.zIndex = String(topPortalZ());   // above modals/portals
  el.innerHTML = `
    <div class="call-incoming-card">
      <div class="call-incoming-icon">${_incoming.video ? '📹' : '📞'}</div>
      <div class="call-incoming-name">${_esc(_peer)}</div>
      <div class="call-incoming-sub">Incoming ${_incoming.video ? 'video' : 'voice'} call…</div>
      <div class="call-incoming-actions">
        <button type="button" class="call-btn call-decline" id="call-decline-btn" title="Decline">✕</button>
        <button type="button" class="call-btn call-accept" id="call-accept-btn" title="Accept">✓</button>
      </div>
    </div>`;
  document.body.appendChild(el);
  document.getElementById('call-accept-btn').addEventListener('click', () => acceptCall());
  document.getElementById('call-decline-btn').addEventListener('click', () => declineCall());
}

function _buildOverlay(statusText) {
  document.getElementById('call-incoming')?.remove();
  let ov = document.getElementById('call-overlay');
  if (!ov) {
    ov = document.createElement('div');
    ov.id = 'call-overlay';
    ov.className = 'call-overlay';
    ov.style.zIndex = String(topPortalZ());   // above modals/portals
    ov.innerHTML = `
      <video id="call-remote-video" class="call-remote" autoplay playsinline></video>
      <video id="call-local-video" class="call-local" autoplay playsinline muted></video>
      <div class="call-head"><span class="call-peer" id="call-peer-name"></span>
        <span class="call-status" id="call-status"></span></div>
      <div class="call-controls" id="call-controls"></div>`;
    document.body.appendChild(ov);
  }
  document.getElementById('call-peer-name').textContent = _peer || '';
  document.getElementById('call-status').textContent = statusText || '';
  _renderControls();
  _attachLocalPreview();
}

function _attachLocalPreview() {
  const v = document.getElementById('call-local-video');
  if (v && _localStream) v.srcObject = _localStream;
  const r = document.getElementById('call-remote-video');
  if (r && _remoteStream) r.srcObject = _remoteStream;
}

function _renderControls() {
  const host = document.getElementById('call-controls');
  if (!host) return;
  const muted = _localStream && _localStream.getAudioTracks()[0] && !_localStream.getAudioTracks()[0].enabled;
  const camOff = _localStream && _localStream.getVideoTracks()[0] && !_localStream.getVideoTracks()[0].enabled;
  const hasVideo = _localStream && _localStream.getVideoTracks().length > 0;
  host.innerHTML = `
    <button type="button" class="call-ctl${muted ? ' off' : ''}" id="call-mute" title="${muted ? 'Unmute' : 'Mute'}">${muted ? '🔇' : '🎙'}</button>
    ${hasVideo ? `<button type="button" class="call-ctl${camOff ? ' off' : ''}" id="call-cam" title="Camera">${camOff ? '📷' : '📹'}</button>` : ''}
    <button type="button" class="call-ctl call-hangup" id="call-hangup" title="Hang up">📞</button>`;
  document.getElementById('call-mute')?.addEventListener('click', _toggleMute);
  document.getElementById('call-cam')?.addEventListener('click', _toggleCamera);
  document.getElementById('call-hangup')?.addEventListener('click', () => hangup());
}

function _renderCalling() { _buildOverlay('Calling…'); }
function _renderInCall() { _buildOverlay('Connected'); }

// ── Lifecycle ────────────────────────────────────────────────────────────────

export async function init() {
  const cfg = await _loadConfig();
  // Always connect the signaling stream so calls can ring; if TURN isn't
  // configured, outgoing calls are blocked but we can still receive the
  // 'busy'/'decline' housekeeping and show a helpful message.
  _connectSignaling();
  return cfg;
}

export function isEnabled() { return !!(_config && _config.enabled); }
export function isBusy() { return _state !== 'idle'; }

const callModule = { init, startCall, acceptCall, declineCall, hangup, isEnabled, isBusy };
export default callModule;
