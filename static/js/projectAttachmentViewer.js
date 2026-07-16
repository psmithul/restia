// Safe, same-origin attachment preview helpers for Restia Projects.
//
// Rendering stays in projects.js so it can reuse the workspace's accessible
// dialog lifecycle. This module owns the security-sensitive classification,
// URL construction, and bounded text transport as small testable seams.

export const PROJECT_TEXT_PREVIEW_BYTES = 256 * 1024;
export const PROJECT_OFFICE_PREVIEW_BYTES = 2 * 1024 * 1024;

const PREVIEW_MIME_BY_EXTENSION = Object.freeze({
  pdf: 'application/pdf',
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  webp: 'image/webp',
  gif: 'image/gif',
  txt: 'text/plain',
  md: 'text/markdown',
  csv: 'text/csv',
  json: 'application/json',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
});

const TEXT_EXTENSIONS = new Set(['txt', 'md', 'csv', 'json']);
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'webp', 'gif']);
const OFFICE_EXTENSIONS = new Set(['docx', 'xlsx', 'pptx']);
const CAD_EXTENSIONS = new Set(['stl', 'step', 'stp', 'iges', 'igs']);

function extensionOf(name) {
  const value = String(name || '').trim().toLowerCase();
  const dot = value.lastIndexOf('.');
  return dot >= 0 ? value.slice(dot + 1) : '';
}

function mimeBase(mime) {
  return String(mime || '').split(';', 1)[0].trim().toLowerCase();
}

export function attachmentPreviewKind(attachment = {}) {
  const extension = extensionOf(attachment.name || attachment.filename);
  const expectedMime = PREVIEW_MIME_BY_EXTENSION[extension];
  if (!expectedMime || mimeBase(attachment.mime || attachment.content_type) !== expectedMime) {
    return 'fallback';
  }
  if (extension === 'pdf') return 'pdf';
  if (IMAGE_EXTENSIONS.has(extension)) return 'image';
  if (TEXT_EXTENSIONS.has(extension)) return 'text';
  if (OFFICE_EXTENSIONS.has(extension)) return 'office';
  return 'fallback';
}

export function attachmentFallbackCopy(attachment = {}) {
  const extension = extensionOf(attachment.name || attachment.filename);
  if (OFFICE_EXTENSIONS.has(extension)) {
    return 'This Office document could not be previewed safely. Download the original to open it in Word, Excel, PowerPoint, LibreOffice, or another trusted desktop app.';
  }
  if (CAD_EXTENSIONS.has(extension)) {
    return 'CAD exchange files are kept inert in Restia. Download this file to inspect it in your trusted CAD or mesh application.';
  }
  if (extension === 'zip') {
    return 'Archives are never extracted in the browser. Download this ZIP and inspect it with your trusted archive tool.';
  }
  return 'This file type cannot be previewed safely in Restia. Download it to open it with a trusted local app.';
}

export function attachmentViewPath(source, attachmentId) {
  const id = encodeURIComponent(String(attachmentId || ''));
  return source === 'home'
    ? `/api/homelink/projects/attachments/${id}/view`
    : `/api/projects/attachments/${id}/view`;
}

export function attachmentOfficePreviewPath(source, attachmentId) {
  const id = encodeURIComponent(String(attachmentId || ''));
  return source === 'home'
    ? `/api/homelink/projects/attachments/${id}/preview`
    : `/api/projects/attachments/${id}/preview`;
}

export function attachmentDownloadPath(source, attachment = {}) {
  const id = encodeURIComponent(String(attachment.id || attachment.attachment_id || ''));
  if (source === 'home') return `/api/homelink/projects/attachments/${id}/download`;
  return String(attachment.download_url || `/api/projects/attachments/${id}/download`);
}

async function responseError(response) {
  try {
    const payload = await response.json();
    if (typeof payload?.detail === 'string' && payload.detail.length <= 500) return payload.detail;
  } catch (_) {}
  return `Preview request failed (HTTP ${Number(response?.status) || 0})`;
}

export async function loadTextAttachmentPreview(url, {
  fetchImpl = globalThis.fetch,
  signal,
  maxBytes = PROJECT_TEXT_PREVIEW_BYTES,
} = {}) {
  if (typeof fetchImpl !== 'function') throw new Error('File preview is unavailable in this browser');
  const boundedMax = Math.max(1024, Math.min(Number(maxBytes) || PROJECT_TEXT_PREVIEW_BYTES, PROJECT_TEXT_PREVIEW_BYTES));
  const response = await fetchImpl(url, {
    method: 'GET',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { Range: `bytes=0-${boundedMax - 1}` },
    signal,
  });
  if (!response?.ok) throw new Error(await responseError(response));
  const contentType = mimeBase(response.headers?.get?.('content-type'));
  if (!['text/plain', 'text/markdown', 'text/csv', 'application/json'].includes(contentType)) {
    throw new Error('The server returned an unsafe text preview type');
  }
  const declaredLength = Number(response.headers?.get?.('content-length'));
  if (Number.isFinite(declaredLength) && declaredLength > boundedMax) {
    throw new Error('The server returned more preview data than requested');
  }
  const text = await response.text();
  const byteLength = typeof TextEncoder === 'function'
    ? new TextEncoder().encode(text).byteLength
    : text.length;
  if (byteLength > boundedMax) throw new Error('The text preview exceeded its safety limit');

  let truncated = false;
  let totalBytes = Number.isFinite(declaredLength) ? declaredLength : byteLength;
  const contentRange = String(response.headers?.get?.('content-range') || '');
  const rangeMatch = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(contentRange);
  if (response.status === 206) {
    if (!rangeMatch) throw new Error('The server returned invalid preview range metadata');
    const [, startText, endText, totalText] = rangeMatch;
    const start = Number(startText);
    const end = Number(endText);
    totalBytes = Number(totalText);
    const rangedBytes = end - start + 1;
    if (
      start !== 0
      || end < start
      || (Number.isFinite(declaredLength) && rangedBytes !== declaredLength)
      || byteLength > rangedBytes
      || totalBytes < rangedBytes
    ) {
      throw new Error('The server returned inconsistent preview range metadata');
    }
    truncated = end + 1 < totalBytes;
  }
  return { text, truncated, totalBytes, contentType };
}

function cleanOfficeText(value, maxLength) {
  const text = String(value ?? '')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '')
    .replace(/\r\n?/g, '\n');
  if (text.length > maxLength) throw new Error('The Office preview exceeded its safety limit');
  return text;
}

function validateOfficePreview(payload) {
  if (!payload || typeof payload !== 'object' || payload.version !== 1) {
    throw new Error('The server returned an invalid Office preview');
  }
  const format = String(payload.format || '').toLowerCase();
  if (!OFFICE_EXTENSIONS.has(format) || !Array.isArray(payload.sections)) {
    throw new Error('The server returned an invalid Office preview');
  }
  if (!payload.sections.length || payload.sections.length > 80) {
    throw new Error('The server returned an invalid Office preview');
  }
  let totalCells = 0;
  let totalChars = 0;
  const sections = payload.sections.map((section) => {
    if (!section || typeof section !== 'object') {
      throw new Error('The server returned an invalid Office preview');
    }
    const title = cleanOfficeText(section.title || 'Section', 160) || 'Section';
    if (section.kind === 'text') {
      const text = cleanOfficeText(section.text, 384 * 1024);
      totalChars += text.length;
      return { kind: 'text', title, text };
    }
    if (section.kind === 'table' && Array.isArray(section.rows) && section.rows.length <= 250) {
      const rows = section.rows.map((rawRow) => {
        if (!Array.isArray(rawRow) || rawRow.length > 50) {
          throw new Error('The server returned invalid Office table data');
        }
        return rawRow.map((rawCell) => {
          totalCells += 1;
          if (totalCells > 10_000) throw new Error('The Office preview contains too many cells');
          const cell = cleanOfficeText(rawCell, 1_000);
          totalChars += cell.length;
          return cell;
        });
      });
      return { kind: 'table', title, rows };
    }
    throw new Error('The server returned an invalid Office preview section');
  });
  if (totalChars > 384 * 1024) throw new Error('The Office preview exceeded its safety limit');
  return { version: 1, format, sections, truncated: Boolean(payload.truncated) };
}

export async function loadOfficeAttachmentPreview(url, {
  fetchImpl = globalThis.fetch,
  signal,
  maxBytes = PROJECT_OFFICE_PREVIEW_BYTES,
} = {}) {
  if (typeof fetchImpl !== 'function') throw new Error('File preview is unavailable in this browser');
  const boundedMax = Math.max(16 * 1024, Math.min(
    Number(maxBytes) || PROJECT_OFFICE_PREVIEW_BYTES,
    PROJECT_OFFICE_PREVIEW_BYTES,
  ));
  const response = await fetchImpl(url, {
    method: 'GET',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { Accept: 'application/json' },
    signal,
  });
  if (!response?.ok) throw new Error(await responseError(response));
  if (mimeBase(response.headers?.get?.('content-type')) !== 'application/json') {
    throw new Error('The server returned an unsafe Office preview type');
  }
  const declaredLength = Number(response.headers?.get?.('content-length'));
  if (Number.isFinite(declaredLength) && declaredLength > boundedMax) {
    throw new Error('The Office preview exceeded its safety limit');
  }
  if (typeof response.arrayBuffer !== 'function') {
    throw new Error('The browser cannot read this Office preview safely');
  }
  const buffer = await response.arrayBuffer();
  if (buffer.byteLength > boundedMax) throw new Error('The Office preview exceeded its safety limit');
  let payload;
  try {
    payload = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(buffer));
  } catch (_) {
    throw new Error('The server returned malformed Office preview data');
  }
  return validateOfficePreview(payload);
}

export const __test = Object.freeze({ extensionOf, mimeBase, validateOfficePreview });
