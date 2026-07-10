// Update checker — shows a slim banner when a newer version of Restia is
// available upstream. Checks on page load and every 30 minutes. The banner
// is dismissible per-commit (sessionStorage), so it won't nag within the
// same browser session.

const CHECK_INTERVAL_MS = 30 * 60 * 1000; // 30 minutes
const DISMISS_KEY = 'restia-update-dismissed';

let _banner = null;

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

function _timeSince(dateStr) {
  if (!dateStr) return '';
  try {
    const secs = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
    if (secs < 60) return 'just now';
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
    return `${Math.floor(secs / 86400)}d ago`;
  } catch { return ''; }
}

function _dismiss(commit) {
  try { sessionStorage.setItem(DISMISS_KEY, commit); } catch {}
  if (_banner) {
    _banner.classList.add('update-banner-hiding');
    setTimeout(() => { _banner?.remove(); _banner = null; }, 300);
  }
}

function _showBanner(data) {
  // Don't show if already dismissed for this commit
  const updateId = data.latest_commit || data.latest_version || 'latest';
  try {
    if (sessionStorage.getItem(DISMISS_KEY) === updateId) return;
  } catch {}

  // Remove any existing banner
  if (_banner) { _banner.remove(); _banner = null; }

  const ago = _timeSince(data.latest_date);
  const msg = _esc(data.latest_message || 'New update');
  const commit = _esc(data.latest_commit || '');
  const latestVersion = _esc(data.latest_version || '');
  const repo = _esc(data.repo || '');
  const releaseUrl = _esc(data.release_url || `https://github.com/${repo}/releases/latest`);
  const updateLabel = data.channel === 'release' && latestVersion ? `v${latestVersion}` : commit.slice(0, 7);

  const el = document.createElement('div');
  el.className = 'update-banner';
  el.id = 'update-banner';
  el.innerHTML = `
    <div class="update-banner-content">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
        <polyline points="7 10 12 15 17 10"/>
        <line x1="12" y1="15" x2="12" y2="3"/>
      </svg>
      <span class="update-banner-text">
        Update available <code>${updateLabel}</code>${ago ? ' · ' + ago : ''}
        — ${msg}
      </span>
      <a class="update-banner-link" href="${releaseUrl}" target="_blank" rel="noopener">
        Release
      </a>
      <button class="update-banner-link update-banner-copy" type="button" title="Copy the safe update command">Copy update command</button>
      <button class="update-banner-close" title="Dismiss" aria-label="Dismiss update notification">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
          <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
    </div>`;

  el.querySelector('.update-banner-close').addEventListener('click', () => _dismiss(updateId));
  el.querySelector('.update-banner-copy').addEventListener('click', async () => {
    const button = el.querySelector('.update-banner-copy');
    try {
      await navigator.clipboard.writeText(data.update_command || './update.sh');
      button.textContent = 'Copied';
      setTimeout(() => { button.textContent = 'Copy update command'; }, 1800);
    } catch {
      button.textContent = data.update_command || './update.sh';
    }
  });

  // Insert at the very top of <body>
  document.body.prepend(el);
  // Trigger reflow then animate in
  el.offsetHeight; // eslint-disable-line no-unused-expressions
  el.classList.add('update-banner-visible');
  _banner = el;
}

async function check() {
  try {
    const res = await fetch('/api/update-check', { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    if (data.update_available) {
      _showBanner(data);
    } else if (_banner) {
      // Update arrived (user already updated), remove banner
      _banner.remove();
      _banner = null;
    }
  } catch { /* silent */ }
}

// Run on load and set up polling
check();
setInterval(check, CHECK_INTERVAL_MS);

export default { check };
