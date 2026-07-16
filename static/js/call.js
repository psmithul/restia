// static/js/call.js
//
// WebRTC audio/video calling. The media is peer-to-peer and encrypted by the
// browser (DTLS-SRTP); the server (routes/call_routes.py) only relays the
// signaling — SDP offer/answer + ICE candidates + call control — over an
// always-on SSE stream, so a call can ring even with the Messages window shut.
//
// State machine: idle → (caller) calling → connecting → in-call, or (callee)
// ringing → connecting → in-call, then back to idle on timeout, hangup,
// decline, or failure. One call at a time; another offer gets `busy`.

import uiModule from './ui.js';
import { topPortalZ } from './toolWindowZOrder.js';

const API = '';

let _config = null;                 // {enabled, turn, ice_servers, can_home_call, can_remote_call}
let _es = null;                     // signaling EventSource
let _homeEs = null;                 // owner-only Home Link signaling stream
let _pc = null;                     // RTCPeerConnection
let _localStream = null;
let _remoteStream = null;
let _state = 'idle';                // idle | calling | ringing | connecting | incall
let _callId = null;
let _peer = null;                   // the other party's username
let _incoming = null;               // {from, call_id, sdp, video} while ringing
let _pendingCandidates = [];        // ICE that arrived before remoteDescription
let _queuedLocalCandidates = [];    // ICE held until offer/answer delivery succeeds
let _outboundSignalReady = false;
let _phaseTimer = null;
let _disconnectTimer = null;
let _homeSignalTimer = null;
let _homeSignalDown = false;
let _homeReadyWaiters = new Set();
let _startPending = false;
let _transport = 'local';           // local | home
let _incomingNotification = null;
let _incomingNotificationTimer = null;

const RING_TIMEOUT_MS = 45_000;
const CONNECT_TIMEOUT_MS = 30_000;
const DISCONNECT_GRACE_MS = 8_000;
const HOME_SIGNAL_GRACE_MS = 5_000;
const HOME_PREFLIGHT_TIMEOUT_MS = 8_000;
const MAX_PENDING_ICE = 64;
const INCOMING_NOTIFICATION_REPEAT_MS = 12_000;

// ── Signaling transport ─────────────────────────────────────────────────────

async function _loadConfig() {
  if (_config) return _config;
  try { _config = await _api('/api/calls/config'); }
  catch (_) { _config = { enabled: false, ice_servers: [], can_home_call: false, can_remote_call: false }; }
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

function _connectEventStream(path, transport) {
  let stream;
  try { stream = new EventSource(API + path); } catch (_) { return null; }
  if (transport === 'home') _homeSignalDown = true;
  stream.addEventListener('call', (e) => {
    let data; try { data = JSON.parse(e.data); } catch (_) { return; }
    _onSignal(data, transport).catch((err) => {
      console.error('call signal handling failed', err);
      if (_state !== 'idle') {
        uiModule.showError && uiModule.showError('The call received an invalid signal and was ended.');
        _cleanup();
      }
    });
  });
  // For Home Link EventSource's native `open` only means the same-origin
  // proxy responded. The separate call-transport event below is emitted after
  // that proxy has authenticated and subscribed to the remote hub.
  if (transport === 'home') {
    stream.addEventListener('call-transport', (e) => {
      let status;
      try { status = JSON.parse(e.data).status; } catch (_) { return; }
      if (status !== 'ready') return;
      _homeSignalDown = false;
      _clearHomeSignalTimer();
      const waiters = _homeReadyWaiters;
      _homeReadyWaiters = new Set();
      for (const resolve of waiters) resolve();
    });
  }
  stream.onerror = () => {
    // EventSource reconnects automatically. An active Home Link call may wait
    // through a brief network flap, but must not continue indefinitely without
    // an authenticated signaling path (a restarted hub also loses its
    // ephemeral call binding).
    if (transport === 'home') {
      _homeSignalDown = true;
      _armHomeSignalTimeout();
    }
  };
  return stream;
}

function _connectSignaling() {
  if (!_config || !_config.enabled) return;
  if (!_es) _es = _connectEventStream('/api/calls/stream', 'local');
  if (_config && _config.can_home_call && !_homeEs) {
    _homeEs = _connectEventStream('/api/homelink/calls/stream', 'home');
  }
}

function _send(to, kind, data, callId = _callId, transport = _transport) {
  if (!to || !callId) return Promise.reject(new Error('Call signaling state is incomplete'));
  const isHome = transport === 'home';
  const body = { call_id: callId, kind, data: data || {} };
  if (!isHome) body.to = to;
  return _api(isHome ? '/api/homelink/calls/signal' : '/api/calls/signal', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

function _errorText(err, fallback = 'Unknown error') {
  if (err && typeof err.userMessage === 'string') return err.userMessage;
  if (err && typeof err.message === 'string' && err.message) return err.message;
  return fallback;
}

function _turnHint() {
  return _config && !_config.turn
    ? ' This installation is using STUN only; configure a TURN relay for strict NATs.'
    : '';
}

function _homeSignalError() {
  const err = new Error(
    'Home Link signaling cannot reach the linked Restia. Check that the Home server is online and reachable, then reconnect Home Link in Messages if its address changed. TURN is not involved in this failure.',
  );
  err.code = 'home-signaling-unavailable';
  err.userMessage = err.message;
  return err;
}

async function _waitForHomeSignaling() {
  if (!_homeEs) throw _homeSignalError();
  if (!_homeSignalDown) return;
  await new Promise((resolve, reject) => {
    let settled = false;
    const ready = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      _homeReadyWaiters.delete(ready);
      reject(_homeSignalError());
    }, HOME_PREFLIGHT_TIMEOUT_MS);
    _homeReadyWaiters.add(ready);
    // Close the tiny race where readiness arrived between the first check and
    // registering this waiter.
    if (!_homeSignalDown) ready();
  });
}

function _signalingFailureMessage(err, action = 'continue') {
  if (_transport === 'home') {
    return `Could not ${action} the call: ${_homeSignalError().message}`;
  }
  return `Call signaling failed: ${_errorText(err, 'request failed')}.`;
}

function _reportDetachedSendFailure(err, action) {
  console.warn(`call ${action} signal failed`, err);
  uiModule.showToast && uiModule.showToast(`Could not send the ${action} signal`);
}

function _handleActiveSendFailure(err, callId, action = 'signaling') {
  console.error(`call ${action} failed`, err);
  if (callId !== _callId || _state === 'idle') return;
  uiModule.showError && uiModule.showError(
    _transport === 'home'
      ? _signalingFailureMessage(err)
      : `Call ${action} failed: ${_errorText(err, 'request failed')}.`);
  _cleanup();
}

// ── Incoming signaling ──────────────────────────────────────────────────────

async function _onSignal(msg, transport = 'local') {
  const { from, call_id, kind, data } = msg || {};
  if (!from || !call_id || !kind) return;

  if (kind === 'offer') {
    // Busy with a different call → politely refuse.
    if (_state !== 'idle') {
      _send(from, 'busy', {}, call_id, transport)
        .catch((err) => console.warn('call busy signal failed', err));
      return;
    }
    _incoming = { from, call_id, sdp: data && data.sdp, video: !!(data && data.video) };
    _callId = call_id; _peer = from; _transport = transport; _state = 'ringing';
    _renderIncoming();
    _startIncomingNotification();
    _armRingTimeout(true);
    return;
  }
  // A call id alone is not an identity. Bind every follow-up signal to the
  // authenticated peer that started (or received) this exact active call.
  if (call_id !== _callId || from !== _peer || transport !== _transport) return;

  if (kind === 'answer') {
    if (_state === 'calling' && _pc && data && data.sdp) {
      const pc = _pc;
      _clearPhaseTimer();
      _state = 'connecting';
      _renderConnecting();
      _armConnectTimeout();
      await pc.setRemoteDescription({ type: 'answer', sdp: data.sdp });
      if (pc !== _pc || _state === 'idle') return;
      await _flushCandidates(pc);
    }
  } else if (kind === 'ice') {
    if (data && data.candidate) {
      if (_pc && _pc.remoteDescription && _pc.remoteDescription.type) {
        try { await _pc.addIceCandidate(data.candidate); }
        catch (err) { console.warn('rejected remote ICE candidate', err); }
      } else if (_pendingCandidates.length < MAX_PENDING_ICE) {
        _pendingCandidates.push(data.candidate);
      }
    }
  } else if (kind === 'decline') {
    if (_state !== 'calling') return;
    uiModule.showToast && uiModule.showToast(`${_peer} declined`);
    _cleanup();
  } else if (kind === 'busy') {
    if (_state !== 'calling') return;
    uiModule.showToast && uiModule.showToast(`${_peer} is busy`);
    _cleanup();
  } else if (kind === 'cancel') {
    if (_state !== 'ringing') return;
    _cleanup();
  } else if (kind === 'hangup') {
    _cleanup();
  }
}

async function _flushCandidates(pc = _pc) {
  const pending = _pendingCandidates;
  _pendingCandidates = [];
  if (!pc) return;
  for (const c of pending) {
    if (pc !== _pc || _state === 'idle') return;
    try { await pc.addIceCandidate(c); }
    catch (err) { console.warn('rejected queued ICE candidate', err); }
  }
}

// ── Peer connection ─────────────────────────────────────────────────────────

async function _sendIceCandidate(candidate, peer, callId) {
  await _send(peer, 'ice', { candidate }, callId);
}

async function _flushLocalCandidates() {
  const queued = _queuedLocalCandidates;
  _queuedLocalCandidates = [];
  for (const item of queued) {
    if (item.callId !== _callId || item.peer !== _peer || _state === 'idle') continue;
    await _sendIceCandidate(item.candidate, item.peer, item.callId);
  }
}

function _newPeerConnection() {
  const pc = new RTCPeerConnection({ iceServers: (_config && _config.ice_servers) || [] });
  pc.onicecandidate = (e) => {
    if (!e.candidate || !_peer || !_callId || _state === 'idle') return;
    const candidate = e.candidate.toJSON ? e.candidate.toJSON() : e.candidate;
    const item = { candidate, peer: _peer, callId: _callId };
    // SDP must reach the peer before trickled ICE. Otherwise a fast candidate
    // can arrive while the receiver is still idle and be discarded.
    if (!_outboundSignalReady) {
      if (_queuedLocalCandidates.length < MAX_PENDING_ICE) _queuedLocalCandidates.push(item);
      return;
    }
    _sendIceCandidate(candidate, item.peer, item.callId)
      .catch((err) => _handleActiveSendFailure(err, item.callId, 'ICE signaling'));
  };
  pc.onicecandidateerror = (e) => {
    console.warn('ICE server error', e && (e.errorText || e.errorCode || e.url));
  };
  pc.ontrack = (e) => {
    _remoteStream = e.streams[0];
    const v = document.getElementById('call-remote-video');
    if (v && _remoteStream) v.srcObject = _remoteStream;
  };
  pc.onconnectionstatechange = () => {
    if (pc !== _pc || _state === 'idle') return;
    if (pc.connectionState === 'connected') {
      _clearPhaseTimer();
      _clearDisconnectTimer();
      _state = 'incall';
      _renderInCall();
    } else if (pc.connectionState === 'failed') {
      _failConnection('Call could not establish a peer connection.');
    } else if (pc.connectionState === 'disconnected') {
      _armDisconnectTimeout();
    } else if (pc.connectionState === 'closed') {
      _cleanup();
    }
  };
  return pc;
}

function _mediaError(message, code) {
  const err = new Error(message);
  err.userMessage = message;
  err.code = code;
  return err;
}

function _stopStream(stream) {
  if (!stream || typeof stream.getTracks !== 'function') return;
  for (const track of stream.getTracks()) {
    try { track.stop(); } catch (_) {}
  }
}

function _closeIncomingNotification() {
  if (_incomingNotificationTimer !== null) clearTimeout(_incomingNotificationTimer);
  _incomingNotificationTimer = null;
  try { if (_incomingNotification) _incomingNotification.close(); } catch (_) {}
  _incomingNotification = null;
}

function _showIncomingNotification() {
  const NotificationApi = globalThis.Notification;
  if (!NotificationApi || NotificationApi.permission !== 'granted' ||
      _state !== 'ringing' || !_callId || !_peer) return;
  try { if (_incomingNotification) _incomingNotification.close(); } catch (_) {}
  try {
    _incomingNotification = new NotificationApi(
      `Incoming ${_incoming && _incoming.video ? 'video' : 'voice'} call`,
      {
        body: `${_peer} is calling you on Restia.`,
        icon: '/static/favicon.ico',
        tag: `restia-call-${_callId}`,
        renotify: true,
        requireInteraction: true,
      },
    );
    _incomingNotification.onclick = () => {
      try { globalThis.focus(); } catch (_) {}
      try { _incomingNotification && _incomingNotification.close(); } catch (_) {}
    };
  } catch (_) {
    _incomingNotification = null;
  }
}

function _startIncomingNotification() {
  _closeIncomingNotification();
  _showIncomingNotification();
  const callId = _callId;
  const repeat = () => {
    _incomingNotificationTimer = null;
    if (_state !== 'ringing' || _callId !== callId) return;
    _showIncomingNotification();
    _incomingNotificationTimer = setTimeout(repeat, INCOMING_NOTIFICATION_REPEAT_MS);
  };
  if (_state === 'ringing') {
    _incomingNotificationTimer = setTimeout(repeat, INCOMING_NOTIFICATION_REPEAT_MS);
  }
}

export async function requestNotificationPermission() {
  const NotificationApi = globalThis.Notification;
  if (!NotificationApi) return false;
  if (NotificationApi.permission === 'granted') return true;
  if (NotificationApi.permission !== 'default') return false;
  // Firefox rejects permission prompts while DOM fullscreen is active. Keep
  // the request attached to a later explicit Messages click instead of
  // forcing the user out of fullscreen or producing repeated console errors.
  if (document.fullscreenElement) return false;
  try { return (await NotificationApi.requestPermission()) === 'granted'; }
  catch (_) { return false; }
}

function _requireMediaContext() {
  if (globalThis.isSecureContext !== true) {
    throw _mediaError(
      'Calls require a secure context. Open Restia over HTTPS or on localhost.',
      'insecure-context');
  }
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
    throw _mediaError('This browser does not provide camera/microphone access.', 'media-unavailable');
  }
}

async function _getMedia(video) {
  _requireMediaContext();
  return navigator.mediaDevices.getUserMedia({ audio: true, video: !!video });
}

function _mediaFailureMessage(err, video) {
  if (err && err.userMessage) return err.userMessage;
  if (err && err.name === 'NotAllowedError') {
    return `Allow ${video ? 'camera and microphone' : 'microphone'} access in the browser to place this call.`;
  }
  if (err && err.name === 'NotFoundError') {
    return `No usable ${video ? 'camera/microphone' : 'microphone'} device was found.`;
  }
  return `Could not access ${video ? 'camera/microphone' : 'microphone'}: ${_errorText(err)}`;
}

function _newCallId() {
  if (!globalThis.crypto || typeof globalThis.crypto.randomUUID !== 'function') {
    throw _mediaError('This browser cannot generate a secure call identifier.', 'crypto-unavailable');
  }
  return globalThis.crypto.randomUUID();
}

function _clearPhaseTimer() {
  if (_phaseTimer !== null) clearTimeout(_phaseTimer);
  _phaseTimer = null;
}

function _clearDisconnectTimer() {
  if (_disconnectTimer !== null) clearTimeout(_disconnectTimer);
  _disconnectTimer = null;
}

function _clearHomeSignalTimer() {
  if (_homeSignalTimer !== null) clearTimeout(_homeSignalTimer);
  _homeSignalTimer = null;
}

function _armHomeSignalTimeout() {
  if (_transport !== 'home' || _state === 'idle' || _homeSignalTimer !== null) return;
  const callId = _callId;
  _homeSignalTimer = setTimeout(() => {
    _homeSignalTimer = null;
    if (_transport !== 'home' || _state === 'idle' || _callId !== callId) return;
    _failConnection(
      'Home Link signaling was lost. Check that the Home server is online, then retry. TURN is not involved in this failure.',
      false,
    );
  }, HOME_SIGNAL_GRACE_MS);
}

function _armRingTimeout(incoming) {
  _clearPhaseTimer();
  const expectedState = incoming ? 'ringing' : 'calling';
  const callId = _callId;
  const peer = _peer;
  _phaseTimer = setTimeout(() => {
    if (_state !== expectedState || _callId !== callId || _peer !== peer) return;
    const kind = incoming ? 'decline' : 'cancel';
    _send(peer, kind, {}, callId)
      .catch((err) => console.warn(`call ${kind} timeout signal failed`, err));
    uiModule.showToast && uiModule.showToast(incoming ? `Missed call from ${peer}` : `${peer} did not answer`);
    _cleanup();
  }, RING_TIMEOUT_MS);
}

function _armConnectTimeout() {
  _clearPhaseTimer();
  const callId = _callId;
  const peer = _peer;
  _phaseTimer = setTimeout(() => {
    if (_state !== 'connecting' || _callId !== callId || _peer !== peer) return;
    _send(peer, 'hangup', {}, callId)
      .catch((err) => console.warn('call connect-timeout hangup signal failed', err));
    uiModule.showError && uiModule.showError(`Call timed out while connecting.${_turnHint()}`);
    _cleanup();
  }, CONNECT_TIMEOUT_MS);
}

function _armDisconnectTimeout() {
  if (_disconnectTimer !== null) return;
  const callId = _callId;
  _disconnectTimer = setTimeout(() => {
    _disconnectTimer = null;
    if (_state === 'idle' || _callId !== callId || !_pc || _pc.connectionState !== 'disconnected') return;
    _failConnection('The peer connection was lost.');
  }, DISCONNECT_GRACE_MS);
}

function _failConnection(message, includeTurnHint = true) {
  if (_state === 'idle') return;
  const callId = _callId;
  const peer = _peer;
  if (callId && peer) {
    _send(peer, 'hangup', {}, callId)
      .catch((err) => console.warn('call failure hangup signal failed', err));
  }
  uiModule.showError && uiModule.showError(
    `${message}${includeTurnHint ? _turnHint() : ''}`);
  _cleanup();
}

// ── Public: start / accept / decline / hangup ───────────────────────────────

export async function startCall(peer, video = true, options = {}) {
  if (!peer) return;
  const cfg = await _loadConfig();
  if (!cfg.enabled) {
    uiModule.showError && uiModule.showError('Calling is disabled on this installation.');
    return;
  }
  const requestedTransport = options && options.home ? 'home' : 'local';
  if (requestedTransport === 'home' && !cfg.can_home_call) {
    uiModule.showError && uiModule.showError('Only the profile that connected Home Link can call this contact.');
    return;
  }
  if (options && options.remote && !cfg.can_remote_call) {
    uiModule.showError && uiModule.showError('Only this hub\'s configured owner can call linked users.');
    return;
  }
  if (_state !== 'idle' || _startPending) {
    uiModule.showToast && uiModule.showToast('Already in a call');
    return;
  }
  _startPending = true;
  let phase = requestedTransport === 'home' ? 'home-preflight' : 'media';
  try {
    // Do not open the microphone/camera or create an offer until the browser
    // knows that the remote, authenticated Home signaling queue is live. A
    // reconnect that succeeds inside this bounded window recovers
    // automatically; a persistent outage fails before requesting media.
    if (requestedTransport === 'home' && !_homeEs) _connectSignaling();
    if (requestedTransport === 'home' && (!_homeEs || _homeSignalDown)) {
      uiModule.showToast && uiModule.showToast('Checking Home Link signaling…');
      await _waitForHomeSignaling();
    }
    phase = 'media';
    _requireMediaContext();
    const callId = _newCallId();
    _callId = callId;
    _peer = peer;
    _transport = requestedTransport;
    _state = 'calling';
    if (_transport === 'home' && _homeSignalDown) _armHomeSignalTimeout();
    _outboundSignalReady = false;
    _queuedLocalCandidates = [];
    _renderCalling();
    if (!cfg.turn) {
      uiModule.showToast && uiModule.showToast(
        'Using STUN only — configure TURN for reliable calls across strict NATs.');
    }
    const stream = await _getMedia(video);
    if (callId !== _callId || _state !== 'calling') {
      _stopStream(stream);
      return;
    }
    _localStream = stream;
    _attachLocalPreview();
    const pc = _newPeerConnection();
    _pc = pc;
    for (const t of stream.getTracks()) pc.addTrack(t, stream);
    const offer = await pc.createOffer();
    if (callId !== _callId || pc !== _pc || _state !== 'calling') return;
    await pc.setLocalDescription(offer);
    if (callId !== _callId || pc !== _pc || _state !== 'calling') return;
    const localOffer = pc.localDescription || offer;
    phase = 'signaling';
    await _send(_peer, 'offer', { sdp: localOffer.sdp, video: !!video }, callId);
    if (callId !== _callId || _state !== 'calling') return;
    _outboundSignalReady = true;
    await _flushLocalCandidates();
    if (callId !== _callId || _state !== 'calling') return;
    _armRingTimeout(false);
  } catch (err) {
    const message = err && (err.code === 'insecure-context' || err.code === 'media-unavailable' ||
      err.name === 'NotAllowedError' || err.name === 'NotFoundError')
      ? _mediaFailureMessage(err, video)
      : phase === 'home-preflight' || (phase === 'signaling' && requestedTransport === 'home')
        ? `Could not start the call: ${_homeSignalError().message}`
        : `Could not start the call: ${_errorText(err)}.`;
    uiModule.showError && uiModule.showError(message);
    _cleanup();
  } finally {
    _startPending = false;
  }
}

export async function acceptCall() {
  if (_state !== 'ringing' || !_incoming) return;
  const incoming = _incoming;
  const callId = _callId;
  const peer = _peer;
  let phase = 'media';
  _clearPhaseTimer();
  _closeIncomingNotification();
  try {
    const stream = await _getMedia(incoming.video);
    if (callId !== _callId || peer !== _peer || _state !== 'ringing') {
      _stopStream(stream);
      return;
    }
    _localStream = stream;
    _outboundSignalReady = false;
    _queuedLocalCandidates = [];
    const pc = _newPeerConnection();
    _pc = pc;
    await pc.setRemoteDescription({ type: 'offer', sdp: incoming.sdp });
    if (callId !== _callId || peer !== _peer || pc !== _pc || _state !== 'ringing') return;
    for (const t of stream.getTracks()) pc.addTrack(t, stream);
    await _flushCandidates(pc);
    if (callId !== _callId || peer !== _peer || pc !== _pc || _state !== 'ringing') return;
    const answer = await pc.createAnswer();
    if (callId !== _callId || peer !== _peer || pc !== _pc || _state !== 'ringing') return;
    await pc.setLocalDescription(answer);
    if (callId !== _callId || peer !== _peer || pc !== _pc || _state !== 'ringing') return;
    const localAnswer = pc.localDescription || answer;
    _state = 'connecting';
    _incoming = null;
    _renderConnecting();
    _attachLocalPreview();
    _armConnectTimeout();
    phase = 'signaling';
    await _send(peer, 'answer', { sdp: localAnswer.sdp }, callId);
    if (callId !== _callId || peer !== _peer || _state === 'idle') return;
    _outboundSignalReady = true;
    await _flushLocalCandidates();
  } catch (err) {
    _send(peer, 'decline', {}, callId)
      .catch((sendErr) => console.warn('call decline-after-failure signal failed', sendErr));
    const message = err && (err.code === 'insecure-context' || err.code === 'media-unavailable' ||
      err.name === 'NotAllowedError' || err.name === 'NotFoundError')
      ? _mediaFailureMessage(err, incoming.video)
      : phase === 'signaling' && _transport === 'home'
        ? `Could not accept the call: ${_homeSignalError().message}`
        : `Could not accept the call: ${_errorText(err)}.`;
    uiModule.showError && uiModule.showError(message);
    _cleanup();
  }
}

export function declineCall() {
  if (_state !== 'ringing') return;
  _send(_peer, 'decline', {}, _callId)
    .catch((err) => _reportDetachedSendFailure(err, 'decline'));
  _cleanup();
}

export function hangup() {
  if (_state === 'idle') return;
  const kind = _state === 'calling' ? 'cancel' : 'hangup';
  _send(_peer, kind, {}, _callId)
    .catch((err) => _reportDetachedSendFailure(err, kind));
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
  _clearPhaseTimer();
  _clearDisconnectTimer();
  _clearHomeSignalTimer();
  const pc = _pc;
  _state = 'idle';
  _pc = null;
  try { if (pc) pc.close(); } catch (_) {}
  _stopStream(_localStream);
  _stopStream(_remoteStream);
  _localStream = null; _remoteStream = null;
  _callId = null; _peer = null; _incoming = null; _pendingCandidates = [];
  _queuedLocalCandidates = []; _outboundSignalReady = false;
  _transport = 'local';
  _closeIncomingNotification();
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

function _renderCalling() {
  _buildOverlay(_config && !_config.turn ? 'Calling… · STUN only' : 'Calling…');
}
function _renderConnecting() {
  _buildOverlay(_config && !_config.turn ? 'Connecting… · STUN only' : 'Connecting…');
}
function _renderInCall() { _buildOverlay('Connected'); }

// ── Lifecycle ────────────────────────────────────────────────────────────────

export async function init() {
  const cfg = await _loadConfig();
  // Always connect the signaling stream so calls can ring. STUN-only installs
  // still support friendly NATs/local networks; the call UI makes that reduced
  // reliability explicit instead of silently pretending TURN is configured.
  if (cfg.enabled) _connectSignaling();
  return cfg;
}

export async function refreshConfig() {
  _config = null;
  const cfg = await _loadConfig();
  // Home Link ownership can change while the page is open. Rebuild only that
  // auxiliary stream; an active local call and its signaling stream remain
  // untouched.
  if (_homeEs) {
    _homeSignalDown = true;
    _armHomeSignalTimeout();
    try { _homeEs.close(); } catch (_) {}
    _homeEs = null;
  }
  if (cfg.enabled) _connectSignaling();
  return cfg;
}

export function isEnabled() { return !!(_config && _config.enabled); }
export function canCall(meta) {
  if (!isEnabled() || !meta) return false;
  if (meta.home) return !!_config.can_home_call;
  if (meta.remote) return !!_config.can_remote_call;
  return true;
}
export function isBusy() { return _state !== 'idle' || _startPending; }

const callModule = {
  init, refreshConfig, startCall, acceptCall, declineCall, hangup,
  requestNotificationPermission, isEnabled, canCall, isBusy,
};
export default callModule;
