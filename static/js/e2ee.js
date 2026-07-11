// static/js/e2ee.js
//
// End-to-end encryption primitives for direct messages, built entirely on the
// Web Crypto API (SubtleCrypto) — no dependencies, and the same code runs
// under Node's webcrypto for tests.
//
// Scheme (v1):
//   • Identity: an ECDH P-256 keypair per account. P-256 is chosen over
//     X25519 because SubtleCrypto supports it everywhere; X25519 is still
//     patchy across browsers.
//   • Private-key protection: wrapped with AES-256-GCM under a key derived by
//     PBKDF2-SHA256 from the user's *encryption passphrase* (separate from the
//     login password, never sent to the server). The server stores only the
//     public key and this wrapped blob.
//   • Message key: static-static ECDH between the two identities gives a shared
//     secret; the same AES-GCM key falls out on both sides. A fresh random
//     96-bit IV per message. Envelope: "e2ee:v1:<b64 iv>.<b64 ciphertext>".
//
// Honest limits: this provides confidentiality against a leaked database, the
// relay/hub, and passive server compromise. It is NOT forward-secret (no
// double ratchet) and cannot defend against a server that serves malicious JS
// — inherent to web-delivered crypto. Both are documented for users.

const subtle = globalThis.crypto.subtle;

export const ENVELOPE_PREFIX = 'e2ee:v1:';
export const DEFAULT_ITERATIONS = 210000;

// ── base64 (ArrayBuffer <-> string), portable across browser + Node ─────────
function _b64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}
function _unb64(s) {
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

const _enc = new TextEncoder();
const _dec = new TextDecoder();

// ── Identity keypair ────────────────────────────────────────────────────────

export async function generateIdentity() {
  const pair = await subtle.generateKey(
    { name: 'ECDH', namedCurve: 'P-256' },
    true,                    // extractable — we export to JWK to persist/wrap
    ['deriveKey', 'deriveBits'],
  );
  const publicJwk = await subtle.exportKey('jwk', pair.publicKey);
  const privateJwk = await subtle.exportKey('jwk', pair.privateKey);
  return { publicJwk, privateJwk };
}

// ── Passphrase → wrapping key (PBKDF2) ──────────────────────────────────────

async function _wrappingKey(passphrase, saltBytes, iterations) {
  const base = await subtle.importKey('raw', _enc.encode(passphrase), 'PBKDF2', false, ['deriveKey']);
  return subtle.deriveKey(
    { name: 'PBKDF2', salt: saltBytes, iterations, hash: 'SHA-256' },
    base,
    { name: 'AES-GCM', length: 256 },
    false,
    ['wrapKey', 'unwrapKey', 'encrypt', 'decrypt'],
  );
}

// Wrap a freshly generated private JWK under the passphrase. Returns the blob
// the server stores plus the KDF parameters needed to reproduce the key.
export async function wrapPrivateKey(privateJwk, passphrase, iterations = DEFAULT_ITERATIONS) {
  const salt = globalThis.crypto.getRandomValues(new Uint8Array(16));
  const key = await _wrappingKey(passphrase, salt, iterations);
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
  const ct = await subtle.encrypt({ name: 'AES-GCM', iv }, key, _enc.encode(JSON.stringify(privateJwk)));
  return {
    wrapped: { iv: _b64(iv), ct: _b64(ct) },
    kdf_salt: _b64(salt),
    kdf_iterations: iterations,
  };
}

// Reverse of wrapPrivateKey. Throws (AES-GCM auth failure) on a wrong
// passphrase — callers treat any throw as "wrong passphrase".
export async function unwrapPrivateKey(wrapped, saltB64, iterations, passphrase) {
  const key = await _wrappingKey(passphrase, _unb64(saltB64), iterations);
  const pt = await subtle.decrypt(
    { name: 'AES-GCM', iv: _unb64(wrapped.iv) }, key, _unb64(wrapped.ct));
  return JSON.parse(_dec.decode(pt));
}

// ── Shared message key (ECDH) ───────────────────────────────────────────────
// Both parties derive the identical AES-GCM key from their own private JWK and
// the peer's public JWK, because ECDH(a_priv, b_pub) == ECDH(b_priv, a_pub).

export async function deriveSharedKey(myPrivateJwk, theirPublicJwk) {
  const priv = await subtle.importKey('jwk', myPrivateJwk,
    { name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveKey', 'deriveBits']);
  const pub = await subtle.importKey('jwk', theirPublicJwk,
    { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  return subtle.deriveKey(
    { name: 'ECDH', public: pub },
    priv,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt'],
  );
}

// ── Message encryption ──────────────────────────────────────────────────────

export function isEnvelope(s) {
  return typeof s === 'string' && s.startsWith(ENVELOPE_PREFIX);
}

export async function encryptMessage(plaintext, sharedKey) {
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
  const ct = await subtle.encrypt({ name: 'AES-GCM', iv }, sharedKey, _enc.encode(String(plaintext)));
  return ENVELOPE_PREFIX + _b64(iv) + '.' + _b64(ct);
}

// Returns the plaintext, or throws on a malformed/undecryptable envelope so the
// UI can show a locked placeholder rather than garbage.
export async function decryptMessage(envelope, sharedKey) {
  if (!isEnvelope(envelope)) throw new Error('not an e2ee envelope');
  const payload = envelope.slice(ENVELOPE_PREFIX.length);
  const dot = payload.indexOf('.');
  if (dot < 0) throw new Error('malformed envelope');
  const iv = _unb64(payload.slice(0, dot));
  const ct = _unb64(payload.slice(dot + 1));
  const pt = await subtle.decrypt({ name: 'AES-GCM', iv }, sharedKey, ct);
  return _dec.decode(pt);
}

export default {
  ENVELOPE_PREFIX, DEFAULT_ITERATIONS,
  generateIdentity, wrapPrivateKey, unwrapPrivateKey,
  deriveSharedKey, isEnvelope, encryptMessage, decryptMessage,
};
