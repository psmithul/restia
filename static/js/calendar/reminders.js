// static/js/calendar/reminders.js
//
// Browser-notification poller for calendar reminder notes. Self-contained:
// module-private `_notifFired` Set tracks which note IDs we've already
// notified, persisted to localStorage. Polls `/api/notes?label=calendar`
// every 60 seconds and fires a Notification + toast for any note whose
// `due_date` is in the past but within the staleness window.
//
// `startReminderPoll()` only starts the poll loop. Notification permission is
// requested from the explicit reminder-creation gesture in calendar.js; doing
// it here during page startup is rejected by browsers and can force Firefox
// out of DOM fullscreen.

import uiModule from '../ui.js';
import { notesFromPayload } from './reminderPayload.js';

const API_BASE = window.location.origin;

let _notifFired = new Set(JSON.parse(localStorage.getItem('cal-notif-fired') || '[]'));
const _notifRetryAt = new Map();

// Compute a fresh, system-clock-accurate notification body. Tries the
// note's `event_dtstart` first (set by _createEventReminder); falls back
// to scrubbing stale time tokens out of items[0].text so legacy
// reminders don't show "in 29 min" at 9pm.
function _formatReminderBody(note) {
  const dtstartRaw = note.event_dtstart || note.eventDtstart || null;
  if (dtstartRaw) {
    const start = new Date(dtstartRaw);
    if (!isNaN(start.getTime())) {
      const now = new Date();
      const mins = Math.round((start - now) / 60000);
      const when = start.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      let when2 = '';
      const sameDay = start.toDateString() === now.toDateString();
      if (!sameDay) when2 = ' ' + start.toLocaleDateString([], { month: 'short', day: 'numeric' });
      if (mins >= 1 && mins <= 60) return `Starts in ${mins} min (${when}${when2})`;
      if (mins === 0) return `Starting now (${when}${when2})`;
      if (mins > 60) {
        const h = Math.round(mins / 60);
        return `Starts in ${h} hour${h === 1 ? '' : 's'} (${when}${when2})`;
      }
      if (mins >= -60) return `Started ${Math.abs(mins)} min ago (${when}${when2})`;
      return `Was scheduled for ${when}${when2}`;
    }
  }
  // Legacy notes (no event_dtstart). Scrub stale relative-time strings.
  let body = (note.items || []).map(i => i.text).join('\n') || note.content || '';
  body = body.replace(/\bin\s+\d+\s*(min|minute|hour|hr|day)s?\b/gi, '').trim();
  body = body.replace(/\(\s*\d{1,2}:\d{2}\s*\)/g, '').trim();
  body = body.replace(/\s{2,}/g, ' ');
  return body;
}

// Failed external delivery is retried, not acknowledged. The server owns the
// durable late-reminder catch-up path; this browser poller uses a short backoff
// only to avoid retry storms while the selected channel is unavailable.
const _REMINDER_RETRY_MS = 5 * 60 * 1000;

async function _pollReminders() {
  try {
    const res = await fetch(`${API_BASE}/api/notes?label=calendar`, { credentials: 'same-origin' });
    if (!res.ok) return;
    const payload = await res.json();
    const notes = notesFromPayload(payload);
    const now = new Date();
    for (const note of notes) {
      if (!note.due_date || _notifFired.has(note.id)) continue;
      if ((_notifRetryAt.get(note.id) || 0) > Date.now()) continue;
      const due = new Date(note.due_date);
      if (isNaN(due)) continue;
      if (due > now) continue; // not yet due
      const body = _formatReminderBody(note);
      let acknowledged = false;
      let showLocal = false;
      let result = null;
      try {
        const delivery = await fetch(`${API_BASE}/api/notes/fire-reminder`, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            note_id: note.id,
            title: note.title || 'Calendar Reminder',
            body,
          }),
        });
        if (!delivery.ok) throw new Error(`reminder dispatch failed (${delivery.status})`);
        result = await delivery.json();
        acknowledged = !!(result && (result.acknowledged || result.delivered || result.skipped || result.suppressed));
        // Suppressed and deferred responses are intentional profile choices,
        // not a reason to surface a local Notification/toast. A previously
        // delivered occurrence (`skipped`) is also silent.
        showLocal = !!(result && result.browser_sent && result.show_browser
          && !result.suppressed && !result.skipped);
      } catch (_) {
        // Without a structured response we do not know the profile's channel;
        // retain the browser fallback but leave the occurrence retryable.
        showLocal = true;
      }
      if (showLocal) {
        if ('Notification' in window && Notification.permission === 'granted') {
          new Notification(note.title || 'Calendar Reminder', {
            body,
            icon: '/static/favicon.png',
            tag: `cal-remind-${note.id}`,
          });
        }
        if (uiModule.showToast) uiModule.showToast((note.title || 'Calendar Reminder') + (body ? ' — ' + body : ''));
        if (result && result.browser_notification_id) {
          try {
            const ackRes = await fetch(`${API_BASE}/api/tasks/notifications/ack`, {
              method: 'POST',
              credentials: 'same-origin',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ ids: [result.browser_notification_id] }),
            });
            const ack = ackRes.ok ? await ackRes.json() : null;
            if (ack && ack.reminder_acknowledged > 0) acknowledged = true;
          } catch (_) {}
        }
      }
      if (acknowledged) {
        _notifFired.add(note.id);
        _notifRetryAt.delete(note.id);
      } else {
        _notifRetryAt.set(note.id, Date.now() + _REMINDER_RETRY_MS);
      }
    }
    // Persist fired set (keep last 200)
    const arr = [..._notifFired].slice(-200);
    localStorage.setItem('cal-notif-fired', JSON.stringify(arr));
  } catch (_) {}
}

let _started = false;

// Idempotent: safe to call multiple times. Kicks off the 60s poll loop on the
// first call without prompting for permissions during page startup.
export function startReminderPoll() {
  if (_started) return;
  _started = true;
  _pollReminders();
  setInterval(_pollReminders, 60000);
}
