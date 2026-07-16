// Bounded Telegram link-status polling used by Settings.
// Kept DOM-free so its timer and cancellation behavior can be tested directly.

export const TELEGRAM_LINK_POLL_INTERVAL_MS = 2000;

export function createTelegramLinkStatusPoller({
  refresh,
  onLinked = () => {},
  onExpired = () => {},
  isActive = () => true,
  now = () => Date.now(),
  setTimer = (fn, delay) => setTimeout(fn, delay),
  clearTimer = timer => clearTimeout(timer),
  intervalMs = TELEGRAM_LINK_POLL_INTERVAL_MS,
} = {}) {
  if (typeof refresh !== 'function') throw new TypeError('refresh is required');

  let timer = null;
  let expiresAtMs = 0;
  let runId = 0;

  function stop() {
    runId += 1;
    if (timer !== null) clearTimer(timer);
    timer = null;
    expiresAtMs = 0;
  }

  function schedule(id) {
    if (id !== runId || !expiresAtMs) return;
    const remaining = expiresAtMs - now();
    if (remaining <= 0) {
      stop();
      onExpired();
      return;
    }
    timer = setTimer(() => poll(id), Math.min(intervalMs, remaining));
  }

  async function poll(id) {
    timer = null;
    if (id !== runId) return;
    if (!isActive()) {
      stop();
      return;
    }
    if (expiresAtMs <= now()) {
      stop();
      onExpired();
      return;
    }

    let status = null;
    try {
      status = await refresh();
    } catch (_) {
      // Transient status failures are retried only until the server-issued TTL.
    }
    if (id !== runId) return;
    if (status?.linked) {
      stop();
      onLinked(status);
      return;
    }
    if (!isActive()) {
      stop();
      return;
    }
    schedule(id);
  }

  function start(expiresAtSeconds) {
    stop();
    const parsed = Number(expiresAtSeconds);
    expiresAtMs = Number.isFinite(parsed) ? parsed * 1000 : 0;
    if (expiresAtMs <= now()) {
      expiresAtMs = 0;
      onExpired();
      return false;
    }
    const id = runId;
    schedule(id);
    return true;
  }

  return {
    start,
    stop,
    isRunning: () => timer !== null || expiresAtMs > 0,
  };
}
