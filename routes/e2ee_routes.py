# routes/e2ee_routes.py
"""End-to-end encryption key store.

Each account publishes an ECDH P-256 identity: a public key (world-readable to
signed-in accounts, so anyone can encrypt to it) and its private key stored
only WRAPPED — AES-GCM ciphertext the browser produces from a key derived
(PBKDF2) from a separate encryption passphrase that never reaches the server.

The server is deliberately a dumb bag of bytes here: it validates shapes and
sizes but can neither unwrap a private key nor read a message. That is the
whole point — a database leak exposes only ciphertext. The corollary, spelled
out for callers, is that a forgotten passphrase is unrecoverable.

Honest scope note: in a self-hosted, browser-delivered app, E2EE protects
against a leaked database, network/relay observers, and passive server
compromise — NOT against a server operator who serves modified JavaScript.
That residual trust is inherent to web-delivered crypto and is documented for
users rather than papered over.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.database import SessionLocal, UserKey, utcnow_naive
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

MAX_PUBLIC_JWK = 2048          # a P-256 public JWK is ~150 bytes; cap generously
MAX_WRAPPED = 8192             # wrapped private JWK + IV, base64
MAX_SALT = 128
MIN_ITERATIONS = 100_000       # PBKDF2 floor — reject weak client params
MAX_ITERATIONS = 2_000_000


class PublishKeysRequest(BaseModel):
    public_jwk: str
    wrapped_private: str
    kdf_salt: str
    kdf_iterations: int


def _norm(name: Optional[str]) -> str:
    return str(name or "").strip().lower()


def _require_me(request: Request) -> str:
    me = _norm(require_user(request))
    if not me:
        raise HTTPException(403, "End-to-end encryption requires a signed-in account")
    return me


def setup_e2ee_routes():
    router = APIRouter(prefix="/api/e2ee", tags=["e2ee"])

    @router.get("/me")
    async def get_my_keys(request: Request):
        """My own key bundle, including the wrapped private key so I can unlock
        on another device with my passphrase. {exists:false} before setup."""
        me = _require_me(request)
        db = SessionLocal()
        try:
            k = db.query(UserKey).filter(UserKey.username == me).first()
            if not k:
                return {"exists": False}
            return {
                "exists": True,
                "public_jwk": k.public_jwk,
                "wrapped_private": k.wrapped_private,
                "kdf_salt": k.kdf_salt,
                "kdf_iterations": k.kdf_iterations,
            }
        finally:
            db.close()

    @router.post("/me")
    async def publish_my_keys(body: PublishKeysRequest, request: Request):
        """Create or replace my identity. The server validates shapes/sizes
        only — it can't verify the crypto, which is exactly the design. Public
        JWK must be a well-formed ECDH P-256 public key; the private key arrives
        already wrapped."""
        me = _require_me(request)
        pub = (body.public_jwk or "").strip()
        wrapped = (body.wrapped_private or "").strip()
        salt = (body.kdf_salt or "").strip()
        iters = int(body.kdf_iterations or 0)
        if len(pub) > MAX_PUBLIC_JWK or len(wrapped) > MAX_WRAPPED or len(salt) > MAX_SALT:
            raise HTTPException(400, "Key material too large")
        if not (MIN_ITERATIONS <= iters <= MAX_ITERATIONS):
            raise HTTPException(400, f"kdf_iterations must be {MIN_ITERATIONS}..{MAX_ITERATIONS}")
        try:
            jwk = json.loads(pub)
        except (TypeError, ValueError):
            raise HTTPException(400, "public_jwk must be JSON")
        # Only accept a public ECDH P-256 JWK: right curve, no private scalar 'd'.
        if (not isinstance(jwk, dict) or jwk.get("kty") != "EC"
                or jwk.get("crv") != "P-256" or "x" not in jwk or "y" not in jwk):
            raise HTTPException(400, "public_jwk must be an EC P-256 public key")
        if "d" in jwk:
            raise HTTPException(400, "public_jwk must not contain a private component")
        # wrapped_private must parse as {iv, ct}; the server never opens it.
        try:
            w = json.loads(wrapped)
        except (TypeError, ValueError):
            raise HTTPException(400, "wrapped_private must be JSON")
        if not isinstance(w, dict) or not isinstance(w.get("iv"), str) or not isinstance(w.get("ct"), str):
            raise HTTPException(400, "wrapped_private must be {iv, ct}")
        db = SessionLocal()
        try:
            k = db.query(UserKey).filter(UserKey.username == me).first()
            now = utcnow_naive()
            if k is None:
                db.add(UserKey(username=me, public_jwk=pub, wrapped_private=wrapped,
                               kdf_salt=salt, kdf_iterations=iters,
                               created_at=now, updated_at=now))
            else:
                k.public_jwk = pub
                k.wrapped_private = wrapped
                k.kdf_salt = salt
                k.kdf_iterations = iters
                k.updated_at = now
            db.commit()
            logger.info("E2EE identity published for %s", me)
            return {"ok": True}
        finally:
            db.close()

    @router.get("/key/{username}")
    async def get_public_key(username: str, request: Request):
        """Another account's public key, so I can encrypt to them. Signed-in
        accounts only; returns {exists:false} when that user hasn't set up
        E2EE yet (so the caller can fall back / prompt)."""
        _require_me(request)
        target = _norm(username)
        db = SessionLocal()
        try:
            k = db.query(UserKey).filter(UserKey.username == target).first()
            if not k:
                return {"exists": False, "username": target}
            return {"exists": True, "username": target, "public_jwk": k.public_jwk}
        finally:
            db.close()

    return router
