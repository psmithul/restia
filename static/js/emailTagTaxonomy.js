/** Canonical filter metadata for every email tag the API can return. */
export const EMAIL_TAG_FILTERS = Object.freeze([
  { tag: 'urgent', label: 'Urgent', keywords: ['urgent', 'critical'] },
  { tag: 'reply-soon', label: 'Reply soon', keywords: ['reply soon', 'reply', 'follow up'] },
  { tag: 'action-needed', label: 'Action needed', keywords: ['action needed', 'action', 'needs action'] },
  { tag: 'work', label: 'Work', keywords: ['work', 'professional', 'company', 'client'] },
  { tag: 'personal', label: 'Personal', keywords: ['personal', 'private', 'family', 'friend'] },
  { tag: 'finance', label: 'Finance', keywords: ['finance', 'bank', 'investment', 'money'] },
  { tag: 'bills', label: 'Bills', keywords: ['bill', 'bills', 'billing', 'amount due'] },
  { tag: 'receipt', label: 'Receipt', keywords: ['receipt', 'receipts', 'purchase', 'order'] },
  { tag: 'legal', label: 'Legal', keywords: ['legal', 'law', 'contract', 'court'] },
  { tag: 'travel', label: 'Travel', keywords: ['travel', 'trip', 'booking', 'ticket'] },
  { tag: 'newsletter', label: 'Newsletter', keywords: ['newsletter', 'digest', 'roundup'] },
  { tag: 'marketing', label: 'Marketing', keywords: ['marketing', 'promotion', 'promo', 'offer', 'sale'] },
  { tag: 'notification', label: 'Notification', keywords: ['notification', 'alert', 'update'] },
  { tag: 'security', label: 'Security', keywords: ['security', 'login', 'password', 'verification'] },
  { tag: 'social', label: 'Social', keywords: ['social', 'message', 'connection', 'comment'] },
  { tag: 'shopping', label: 'Shopping', keywords: ['shopping', 'shop', 'product', 'cart'] },
  { tag: 'calendar', label: 'Calendar', keywords: ['calendar', 'meeting', 'event', 'appointment'] },
  { tag: 'support', label: 'Support', keywords: ['support', 'helpdesk', 'case', 'ticket'] },
  { tag: 'spam', label: 'Spam', keywords: ['spam', 'junk', 'phishing', 'scam'] },
].map(item => Object.freeze({ ...item, keywords: Object.freeze(item.keywords) })));

export const EMAIL_FILTERABLE_TAGS = Object.freeze(EMAIL_TAG_FILTERS.map(item => item.tag));

const EMAIL_RENDERABLE_TAGS = new Set(
  EMAIL_FILTERABLE_TAGS.filter(tag => tag !== 'spam'),
);
const DONE_RESPONSE_TAGS = new Set(['urgent', 'reply-soon', 'action-needed']);

/** Mirror the API's canonicalization at the final rendering boundary. */
export function normalizeEmailTagsForRender(tags, { answered = false } = {}) {
  const result = [];
  const seen = new Set();
  for (const raw of (Array.isArray(tags) ? tags : [])) {
    let tag = String(raw || '').trim().toLowerCase().replace(/_/g, '-');
    if (tag === 'promo') tag = 'marketing';
    if (!EMAIL_RENDERABLE_TAGS.has(tag) || seen.has(tag)) continue;
    if (answered && DONE_RESPONSE_TAGS.has(tag)) continue;
    seen.add(tag);
    result.push(tag);
  }
  return result;
}

export function clearAnsweredEmailTags(email) {
  if (!email || !Array.isArray(email.tags)) return [];
  email.tags = normalizeEmailTagsForRender(email.tags, { answered: true });
  return email.tags;
}
