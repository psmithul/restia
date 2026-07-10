// static/js/voiceMode.js

/**
 * Voice conversation mode — a push-to-talk loop for hands-free chat:
 *
 *   hold PTT (strip button or Space) → record → release → STT transcribes
 *   into the composer → auto-send → reply streams with TTS auto-play →
 *   back to ready for the next press.
 *
 * Entry point is the pill on the welcome screen (above the app symbol);
 * while active, a strip docked above the composer hosts the PTT button.
 * Requires an STT provider (Settings → Voice); spoken replies additionally
 * need TTS. Tap (<350ms) toggles listening instead of holding; pressing
 * while the reply is being read aloud interrupts it and starts listening.
 */

import voiceRecorderModule from './voiceRecorder.js';
import uiModule from './ui.js';

// off | ready | listening | transcribing | thinking | speaking
let state = 'off';
let prevAutoPlay = null;   // aiTTSManager.autoPlay before voice mode enabled
let pendingSend = false;   // transcription landed in composer, send on recorder reset
let holdStartedAt = 0;     // pointer/key down time — distinguishes tap from hold
let spaceHeld = false;
const TAP_MS = 350;

const LABELS = {
  ready: 'Hold to talk',
  listening: 'Listening… release to send',
  transcribing: 'Transcribing…',
  thinking: 'Thinking…',
  speaking: 'Speaking — press to interrupt',
};

function _strip() { return document.getElementById('voice-mode-strip'); }
function _pill() { return document.getElementById('voice-mode-btn'); }

function _sttEnabled() {
  return voiceRecorderModule._sttProvider && voiceRecorderModule._sttProvider !== 'disabled';
}

function _render() {
  const strip = _strip();
  const pill = _pill();
  if (pill) pill.classList.toggle('active', state !== 'off');
  if (!strip) return;
  strip.hidden = state === 'off';
  strip.dataset.state = state;
  const label = document.getElementById('voice-ptt-label');
  if (label) label.textContent = LABELS[state] || LABELS.ready;
}

function setState(next) {
  state = next;
  _render();
}

function startListening() {
  if (!_sttEnabled()) {
    uiModule.showError && uiModule.showError('Voice mode needs Speech-to-Text — enable an STT provider in Settings.');
    disable();
    return;
  }
  pendingSend = false;
  // Barge-in: cut off any reply still being read aloud
  if (window.aiTTSManager) window.aiTTSManager.stop();
  setState('listening');
  // No onFileCreated: in voice mode a failed/empty transcription should not
  // leave a stray audio attachment in the composer.
  voiceRecorderModule.startRecording(null, uiModule.showToast, uiModule.showError);
}

function stopListening() {
  setState('transcribing');
  voiceRecorderModule.stopRecording();
}

function _send() {
  pendingSend = false;
  const input = document.getElementById('message');
  if (!input || !input.value.trim()) { setState('ready'); return; }
  const sendBtn = document.querySelector('.send-btn');
  if (!sendBtn || sendBtn.dataset.mode === 'streaming') { setState('ready'); return; }
  setState('thinking');
  sendBtn.click();
}

// ── Push-to-talk (shared by strip button and Space key) ──

function pttDown() {
  if (state === 'listening') {
    // Second tap of tap-to-talk — stop; the paired release must not restart
    holdStartedAt = 0;
    stopListening();
    return;
  }
  if (state === 'ready' || state === 'speaking') {
    holdStartedAt = Date.now();
    startListening();
  }
  // transcribing/thinking: inert — the loop is mid-turn
}

function pttUp() {
  if (state !== 'listening' || !holdStartedAt) return;
  if (Date.now() - holdStartedAt < TAP_MS) return; // tap → stay listening
  stopListening();
}

function _spaceIsPtt(e) {
  const t = e.target;
  if (!t || t === document.body) return true;
  // Empty composer doubles as a PTT surface; anywhere else typing wins
  if (t.id === 'message') return !t.value;
  return !(t.closest && t.closest('input, textarea, select, [contenteditable]'));
}

function _onKeyDown(e) {
  if (state === 'off' || e.code !== 'Space' || e.repeat) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (!_spaceIsPtt(e)) return;
  e.preventDefault();
  spaceHeld = true;
  pttDown();
}

function _onKeyUp(e) {
  if (state === 'off' || e.code !== 'Space' || !spaceHeld) return;
  spaceHeld = false;
  pttUp();
}

// ── Mode toggle ──

async function enable() {
  if (!_sttEnabled()) {
    // Provider is fetched async at startup — retry once before giving up
    try { await voiceRecorderModule.refreshSttProvider(); } catch (_) {}
  }
  if (!_sttEnabled()) {
    uiModule.showError && uiModule.showError('Voice mode needs Speech-to-Text — enable an STT provider in Settings.');
    return;
  }
  const mgr = window.aiTTSManager;
  prevAutoPlay = mgr ? mgr.autoPlay : null;
  if (mgr && mgr.available) {
    mgr.autoPlay = true;
  } else if (uiModule.showToast) {
    uiModule.showToast('TTS unavailable — replies will be text-only');
  }
  setState('ready');
}

function disable() {
  const wasRecording = voiceRecorderModule.getIsRecording && voiceRecorderModule.getIsRecording();
  pendingSend = false;
  spaceHeld = false;
  setState('off');
  if (wasRecording) voiceRecorderModule.stopRecording();
  const mgr = window.aiTTSManager;
  if (mgr) {
    mgr.stop();
    if (prevAutoPlay !== null) mgr.autoPlay = prevAutoPlay;
  }
  prevAutoPlay = null;
}

function toggle() {
  if (state === 'off') enable();
  else disable();
}

// ── Wiring ──

export function init() {
  const pill = _pill();
  const strip = _strip();
  if (!pill && !strip) return;

  if (pill) pill.addEventListener('click', toggle);

  const exitBtn = document.getElementById('voice-mode-exit');
  if (exitBtn) exitBtn.addEventListener('click', disable);

  const ptt = document.getElementById('voice-ptt-btn');
  if (ptt) {
    ptt.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      try { ptt.setPointerCapture(e.pointerId); } catch (_) {}
      pttDown();
    });
    ptt.addEventListener('pointerup', pttUp);
    ptt.addEventListener('pointercancel', pttUp);
    // Mobile long-press must not open a context menu mid-recording
    ptt.addEventListener('contextmenu', (e) => e.preventDefault());
  }

  document.addEventListener('keydown', _onKeyDown);
  document.addEventListener('keyup', _onKeyUp);
  window.addEventListener('blur', () => {
    if (spaceHeld) { spaceHeld = false; pttUp(); }
  });

  // Recorder started (possibly via the composer mic button) — reflect it
  window.addEventListener('odysseus:stt-started', () => {
    if (state === 'ready') setState('listening');
  });

  // Transcription landed in the composer — flag it for sending. The actual
  // send waits for stt-ended: the recorder's isRecording flag is still true
  // here, and clicking send while it's set stops the recording instead.
  window.addEventListener('odysseus:stt-result', () => {
    if (state === 'listening' || state === 'transcribing') pendingSend = true;
  });

  // Recorder fully reset — send the pending transcription, or rearm
  window.addEventListener('odysseus:stt-ended', () => {
    if (state === 'off') return;
    if (pendingSend) _send();
    else if (state === 'listening' || state === 'transcribing') setState('ready');
  });

  // Streaming lifecycle → thinking, then speaking (TTS busy) or ready
  window.addEventListener('odysseus:chat-busy-change', (e) => {
    if (state === 'off') return;
    if (e.detail && e.detail.active) { setState('thinking'); return; }
    const mgr = window.aiTTSManager;
    const ttsBusy = mgr && (mgr.isPlaying || mgr._processing || (mgr._queue && mgr._queue.length));
    setState(ttsBusy ? 'speaking' : 'ready');
  });

  // TTS queue can start draining after the stream already ended
  window.addEventListener('odysseus:tts-active', () => {
    if (state === 'ready') setState('speaking');
  });
  window.addEventListener('odysseus:tts-idle', () => {
    if (state === 'speaking') setState('ready');
  });
}

const voiceModeModule = { init, toggle, disable, get state() { return state; } };
export default voiceModeModule;
