"""E2EE crypto core (static/js/e2ee.js) exercised under Node's webcrypto.

Pins the properties the whole feature rests on:
  - Two parties independently derive the same message key (ECDH), so what one
    encrypts the other decrypts — and a THIRD party's key cannot.
  - The private key round-trips through passphrase wrapping, and a wrong
    passphrase fails closed (throws) rather than yielding garbage.
  - Ciphertext is a versioned envelope and never contains the plaintext.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _node_eval(source: str):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def test_two_party_roundtrip_and_third_party_cannot_read():
    out = _node_eval(
        """
        const E = await import('./static/js/e2ee.js');
        const a = await E.generateIdentity();
        const b = await E.generateIdentity();
        const c = await E.generateIdentity();
        // A encrypts to B.
        const kAB = await E.deriveSharedKey(a.privateJwk, b.publicJwk);
        const env = await E.encryptMessage('meet at noon 🕛', kAB);
        // B derives the same key from its side and reads it.
        const kBA = await E.deriveSharedKey(b.privateJwk, a.publicJwk);
        const plain = await E.decryptMessage(env, kBA);
        // C (an eavesdropper) derives a different key and fails.
        const kCB = await E.deriveSharedKey(c.privateJwk, b.publicJwk);
        let thirdFailed = false;
        try { await E.decryptMessage(env, kCB); } catch { thirdFailed = true; }
        console.log(JSON.stringify({
          plain,
          isEnvelope: E.isEnvelope(env),
          leaksPlaintext: env.includes('meet at noon'),
          thirdFailed,
        }));
        """
    )
    assert out["plain"] == "meet at noon 🕛"
    assert out["isEnvelope"] is True
    assert out["leaksPlaintext"] is False
    assert out["thirdFailed"] is True


def test_private_key_wrap_unwrap_and_wrong_passphrase_fails():
    out = _node_eval(
        """
        const E = await import('./static/js/e2ee.js');
        const id = await E.generateIdentity();
        const w = await E.wrapPrivateKey(id.privateJwk, 'correct horse battery');
        // Right passphrase unwraps to the same key (proven by a working ECDH).
        const priv = await E.unwrapPrivateKey(w.wrapped, w.kdf_salt, w.kdf_iterations, 'correct horse battery');
        const peer = await E.generateIdentity();
        const k1 = await E.deriveSharedKey(id.privateJwk, peer.publicJwk);
        const k2 = await E.deriveSharedKey(priv, peer.publicJwk);
        const env = await E.encryptMessage('secret', k1);
        const same = (await E.decryptMessage(env, k2)) === 'secret';
        // Wrong passphrase throws.
        let wrongFailed = false;
        try { await E.unwrapPrivateKey(w.wrapped, w.kdf_salt, w.kdf_iterations, 'nope'); }
        catch { wrongFailed = true; }
        // The wrapped blob never contains the raw private scalar.
        const leaks = JSON.stringify(w.wrapped).includes(id.privateJwk.d);
        console.log(JSON.stringify({ same, wrongFailed, leaks, iters: w.kdf_iterations }));
        """
    )
    assert out["same"] is True
    assert out["wrongFailed"] is True
    assert out["leaks"] is False
    assert out["iters"] == 210000
