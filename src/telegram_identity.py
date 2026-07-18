"""Canonical SQL authority for Telegram links and conversation bindings.

Runtime readers never consult the legacy settings maps.  The only supported
compatibility path is :func:`adopt_legacy_telegram_identity`, which validates
the complete legacy payload and commits its SQL projection atomically while
leaving ``settings.json`` untouched as a recovery source.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import or_

from core.database import (
    Account,
    SessionLocal,
    TelegramConversationBinding,
    TelegramIdentityImportRun,
    TelegramLinkCode,
    TelegramPrincipal,
    utcnow_naive,
)
from src.identity import ensure_account, find_account, normalize_identity
from src.secret_storage import private_digest


LEGACY_IMPORT_SOURCE = "telegram-settings-v1"
LINK_CODE_TTL_SECONDS = 10 * 60
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_authority_lock = threading.RLock()
_authority_error_code = ""


class TelegramIdentityError(RuntimeError):
    """Base error for unavailable or inconsistent Telegram identity state."""


class TelegramIdentityImportError(TelegramIdentityError):
    """Fail-closed legacy import error with a non-sensitive stable code."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "telegram_identity_import_failed")
        super().__init__(self.code)


@dataclass(frozen=True)
class TelegramAuthorityProjection:
    chat_owners: dict[str, str]
    session_map: dict[str, str]


@dataclass(frozen=True)
class TelegramImportResult:
    state: str
    principals: int
    bindings: int
    link_codes: int
    source_sha256: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hex64(value: object, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if _HEX64_RE.fullmatch(normalized) is None:
        raise ValueError(f"{field} must be a 64-character digest")
    return normalized


def _chat_id(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        raise ValueError("Telegram chat principal is invalid")
    return normalized


def _session_id(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
    ):
        raise ValueError("Telegram conversation binding is invalid")
    return normalized


def _code(value: object) -> str:
    normalized = str(value or "").strip().upper()
    if (
        not normalized
        or len(normalized) > 64
        or not normalized.isascii()
        or any(ord(char) < 33 or ord(char) == 127 for char in normalized)
    ):
        raise ValueError("Telegram link code is invalid")
    return normalized


def telegram_bot_fingerprint(*, bot_id: object = "", bot_token: object = "") -> str:
    """Return a stable keyed bot scope without exposing bot ID or token."""

    identity = str(bot_id or "").strip() or str(bot_token or "").strip()
    if not identity:
        return ""
    return private_digest("telegram-bot-scope-v1", identity)


def telegram_chat_digest(bot_fingerprint: str, chat_id: object) -> str:
    scope = _hex64(bot_fingerprint, field="bot fingerprint")
    return private_digest(f"telegram-chat-principal-v1:{scope}", _chat_id(chat_id))


def telegram_code_digest(bot_fingerprint: str, code: object) -> str:
    scope = _hex64(bot_fingerprint, field="bot fingerprint")
    return private_digest(f"telegram-link-code-v1:{scope}", _code(code))


def lock_telegram_identity_authority(code: str) -> None:
    global _authority_error_code
    with _authority_lock:
        _authority_error_code = str(code or "telegram_identity_import_failed")


def unlock_telegram_identity_authority() -> None:
    global _authority_error_code
    with _authority_lock:
        _authority_error_code = ""


def assert_telegram_identity_ready() -> None:
    with _authority_lock:
        code = _authority_error_code
    if code:
        raise TelegramIdentityError(
            f"Telegram identity authority is locked ({code})"
        )


def _principal_for_chat(db, bot_fingerprint: str, chat_id: object):
    scope = _hex64(bot_fingerprint, field="bot fingerprint")
    digest = telegram_chat_digest(scope, chat_id)
    return db.query(TelegramPrincipal).filter(
        TelegramPrincipal.bot_fingerprint == scope,
        TelegramPrincipal.chat_id_digest == digest,
        TelegramPrincipal.state == "linked",
    ).first()


def load_telegram_authority_projection(
    bot_fingerprint: str,
) -> TelegramAuthorityProjection:
    """Read the active bot-scoped projection used by every Telegram consumer."""

    assert_telegram_identity_ready()
    if not bot_fingerprint:
        return TelegramAuthorityProjection(chat_owners={}, session_map={})
    scope = _hex64(bot_fingerprint, field="bot fingerprint")
    db = SessionLocal()
    try:
        rows = (
            db.query(TelegramPrincipal, Account)
            .join(Account, Account.id == TelegramPrincipal.account_id)
            .filter(
                TelegramPrincipal.bot_fingerprint == scope,
                TelegramPrincipal.state == "linked",
                Account.status == "active",
            )
            .all()
        )
        principal_ids = [principal.id for principal, _account in rows]
        bindings = {}
        if principal_ids:
            bindings = {
                row.principal_id: str(row.session_id or "")
                for row in db.query(TelegramConversationBinding).filter(
                    TelegramConversationBinding.principal_id.in_(principal_ids)
                ).all()
                if str(row.session_id or "")
            }
        owners: dict[str, str] = {}
        sessions: dict[str, str] = {}
        for principal, account in rows:
            chat = str(principal.chat_id or "")
            if not chat:
                # A wrong encryption key or damaged envelope must never become
                # an empty/wildcard principal.
                continue
            owners[chat] = str(account.username)
            session = bindings.get(principal.id)
            if session:
                sessions[chat] = session
        return TelegramAuthorityProjection(
            chat_owners=owners,
            session_map=sessions,
        )
    finally:
        db.close()


def create_link_code_for_owner(
    owner: str,
    bot_fingerprint: str,
    *,
    ttl_seconds: int = LINK_CODE_TTL_SECONDS,
) -> tuple[str, int]:
    """Create one account-bound link code and invalidate older live codes."""

    assert_telegram_identity_ready()
    scope = _hex64(bot_fingerprint, field="bot fingerprint")
    owner_key = normalize_identity(owner)
    if not owner_key:
        raise ValueError("A Restia user is required")
    ttl = max(60, min(int(ttl_seconds), 3600))
    # The plaintext is shown exactly once. Keep enough entropy that the code
    # remains a strong bearer credential even if a user leaves the full
    # one-hour TTL configured.
    code = os.urandom(16).hex().upper()
    now = _utcnow()
    expires = now + timedelta(seconds=ttl)
    db = SessionLocal()
    try:
        account = ensure_account(db, owner_key)
        db.query(TelegramLinkCode).filter(
            TelegramLinkCode.account_id == account.id,
            TelegramLinkCode.bot_fingerprint == scope,
            TelegramLinkCode.consumed_at.is_(None),
            TelegramLinkCode.invalidated_at.is_(None),
        ).update(
            {TelegramLinkCode.invalidated_at: now},
            synchronize_session=False,
        )
        db.add(TelegramLinkCode(
            id=str(uuid.uuid4()),
            account_id=account.id,
            bot_fingerprint=scope,
            code_digest=telegram_code_digest(scope, code),
            digest_scheme="hmac_sha256_v1",
            expires_at=expires,
        ))
        db.commit()
        return code, int(expires.replace(tzinfo=timezone.utc).timestamp())
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def consume_link_code_for_chat(
    code: str,
    chat_id: str,
    bot_fingerprint: str,
) -> str | None:
    """Atomically consume a code and link the chat to its Account.id owner."""

    assert_telegram_identity_ready()
    scope = _hex64(bot_fingerprint, field="bot fingerprint")
    normalized_code = _code(code)
    normalized_chat = _chat_id(chat_id)
    now = _utcnow()
    keyed_digest = telegram_code_digest(scope, normalized_code)
    legacy_digest = hashlib.sha256(normalized_code.encode("utf-8")).hexdigest()
    db = SessionLocal()
    try:
        record = (
            db.query(TelegramLinkCode)
            .filter(
                TelegramLinkCode.bot_fingerprint == scope,
                or_(
                    TelegramLinkCode.code_digest == keyed_digest,
                    TelegramLinkCode.code_digest == legacy_digest,
                ),
                TelegramLinkCode.consumed_at.is_(None),
                TelegramLinkCode.invalidated_at.is_(None),
                TelegramLinkCode.expires_at > now,
            )
            .with_for_update()
            .first()
        )
        if record is None:
            db.rollback()
            return None
        expected = (
            keyed_digest
            if record.digest_scheme == "hmac_sha256_v1"
            else legacy_digest
        )
        if record.code_digest != expected:
            db.rollback()
            return None
        claimed = db.query(TelegramLinkCode).filter(
            TelegramLinkCode.id == record.id,
            TelegramLinkCode.consumed_at.is_(None),
            TelegramLinkCode.invalidated_at.is_(None),
            TelegramLinkCode.expires_at > now,
        ).update(
            {TelegramLinkCode.consumed_at: now},
            synchronize_session=False,
        )
        if claimed != 1:
            db.rollback()
            return None

        account = db.query(Account).filter(
            Account.id == record.account_id,
            Account.status == "active",
        ).first()
        if account is None:
            raise TelegramIdentityError("Telegram link account is unavailable")
        chat_digest = telegram_chat_digest(scope, normalized_chat)
        principal = db.query(TelegramPrincipal).filter(
            TelegramPrincipal.bot_fingerprint == scope,
            TelegramPrincipal.chat_id_digest == chat_digest,
        ).with_for_update().first()
        if principal is None:
            principal = TelegramPrincipal(
                id=str(uuid.uuid4()),
                account_id=account.id,
                bot_fingerprint=scope,
                chat_id=normalized_chat,
                chat_id_digest=chat_digest,
                state="linked",
                linked_at=now,
                version=1,
            )
            db.add(principal)
        else:
            if principal.account_id != account.id:
                # A conversation belongs to its previous Restia account. Never
                # carry it across a relink, even when the chat itself is the
                # same Telegram principal.
                db.query(TelegramConversationBinding).filter(
                    TelegramConversationBinding.principal_id == principal.id
                ).delete(synchronize_session=False)
            principal.account_id = account.id
            principal.chat_id = normalized_chat
            principal.state = "linked"
            principal.linked_at = now
            principal.revoked_at = None
            principal.version = int(principal.version or 0) + 1
        db.commit()
        return str(account.username)
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def get_conversation_binding(bot_fingerprint: str, chat_id: str) -> str | None:
    assert_telegram_identity_ready()
    if not bot_fingerprint:
        return None
    db = SessionLocal()
    try:
        principal = _principal_for_chat(db, bot_fingerprint, chat_id)
        if principal is None:
            return None
        binding = db.query(TelegramConversationBinding).filter(
            TelegramConversationBinding.principal_id == principal.id,
            TelegramConversationBinding.account_id == principal.account_id,
        ).first()
        if binding is None:
            return None
        return str(binding.session_id or "") or None
    finally:
        db.close()


def set_conversation_binding(
    bot_fingerprint: str,
    chat_id: str,
    session_id: str,
) -> None:
    assert_telegram_identity_ready()
    scope = _hex64(bot_fingerprint, field="bot fingerprint")
    session = _session_id(session_id)
    db = SessionLocal()
    try:
        principal = _principal_for_chat(db, scope, chat_id)
        if principal is None:
            raise TelegramIdentityError("Telegram chat is not linked")
        binding = db.query(TelegramConversationBinding).filter(
            TelegramConversationBinding.principal_id == principal.id,
        ).with_for_update().first()
        if binding is None:
            db.add(TelegramConversationBinding(
                id=str(uuid.uuid4()),
                principal_id=principal.id,
                account_id=principal.account_id,
                session_id=session,
                version=1,
            ))
        else:
            if binding.account_id != principal.account_id:
                raise TelegramIdentityError(
                    "Telegram conversation owner does not match its principal"
                )
            binding.session_id = session
            binding.version = int(binding.version or 0) + 1
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def clear_conversation_binding(bot_fingerprint: str, chat_id: str) -> None:
    assert_telegram_identity_ready()
    if not bot_fingerprint:
        return
    db = SessionLocal()
    try:
        principal = _principal_for_chat(db, bot_fingerprint, chat_id)
        if principal is not None:
            db.query(TelegramConversationBinding).filter(
                TelegramConversationBinding.principal_id == principal.id,
                TelegramConversationBinding.account_id == principal.account_id,
            ).delete(synchronize_session=False)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def unlink_owner_from_bot(owner: str, bot_fingerprint: str) -> int:
    assert_telegram_identity_ready()
    if not bot_fingerprint:
        return 0
    scope = _hex64(bot_fingerprint, field="bot fingerprint")
    owner_key = normalize_identity(owner)
    db = SessionLocal()
    try:
        account = find_account(db, owner_key)
        if account is None:
            db.rollback()
            return 0
        principals = db.query(TelegramPrincipal).filter(
            TelegramPrincipal.account_id == account.id,
            TelegramPrincipal.bot_fingerprint == scope,
            TelegramPrincipal.state == "linked",
        ).with_for_update().all()
        ids = [row.id for row in principals]
        if ids:
            db.query(TelegramConversationBinding).filter(
                TelegramConversationBinding.principal_id.in_(ids)
            ).delete(synchronize_session=False)
        now = _utcnow()
        for principal in principals:
            principal.state = "unlinked"
            principal.revoked_at = now
            principal.version = int(principal.version or 0) + 1
        db.query(TelegramLinkCode).filter(
            TelegramLinkCode.account_id == account.id,
            TelegramLinkCode.bot_fingerprint == scope,
            TelegramLinkCode.consumed_at.is_(None),
            TelegramLinkCode.invalidated_at.is_(None),
        ).update(
            {TelegramLinkCode.invalidated_at: now},
            synchronize_session=False,
        )
        db.commit()
        return len(principals)
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def revoke_bot_authority(bot_fingerprint: str) -> int:
    """Invalidate every principal, binding, and live code for one bot scope."""

    assert_telegram_identity_ready()
    if not bot_fingerprint:
        return 0
    scope = _hex64(bot_fingerprint, field="bot fingerprint")
    db = SessionLocal()
    try:
        principals = db.query(TelegramPrincipal).filter(
            TelegramPrincipal.bot_fingerprint == scope,
            TelegramPrincipal.state == "linked",
        ).with_for_update().all()
        ids = [row.id for row in principals]
        if ids:
            db.query(TelegramConversationBinding).filter(
                TelegramConversationBinding.principal_id.in_(ids)
            ).delete(synchronize_session=False)
        now = _utcnow()
        for principal in principals:
            principal.state = "unlinked"
            principal.revoked_at = now
            principal.version = int(principal.version or 0) + 1
        db.query(TelegramLinkCode).filter(
            TelegramLinkCode.bot_fingerprint == scope,
            TelegramLinkCode.consumed_at.is_(None),
            TelegramLinkCode.invalidated_at.is_(None),
        ).update(
            {TelegramLinkCode.invalidated_at: now},
            synchronize_session=False,
        )
        db.commit()
        return len(principals)
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _legacy_map(value: object, *, field: str) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict) or len(value) > 10_000:
        raise TelegramIdentityImportError(f"invalid_{field}")
    return {str(key): item for key, item in value.items()}


def _legacy_chat_ids(value: object) -> set[str]:
    if value in (None, [], ""):
        return set()
    if isinstance(value, str):
        raw = value.replace(" ", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raise TelegramIdentityImportError("invalid_allowed_chat_ids")
    if len(raw) > 10_000:
        raise TelegramIdentityImportError("invalid_allowed_chat_ids")
    try:
        return {_chat_id(item) for item in raw if str(item or "").strip()}
    except ValueError as exc:
        raise TelegramIdentityImportError("invalid_allowed_chat_ids") from exc


def _legacy_owner(value: object) -> str:
    owner = normalize_identity(value)
    if not owner or len(owner) > 160:
        raise TelegramIdentityImportError("invalid_owner")
    return owner


def _legacy_source(
    settings: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, Any]], set[str], str]:
    owners_raw = _legacy_map(
        settings.get("telegram_chat_owners"), field="chat_owners",
    )
    sessions_raw = _legacy_map(
        settings.get("telegram_session_map"), field="session_map",
    )
    codes_raw = _legacy_map(
        settings.get("telegram_link_codes"), field="link_codes",
    )
    owners: dict[str, str] = {}
    sessions: dict[str, str] = {}
    codes: dict[str, dict[str, Any]] = {}
    try:
        for raw_chat, raw_owner in owners_raw.items():
            owners[_chat_id(raw_chat)] = _legacy_owner(raw_owner)
        for raw_chat, raw_session in sessions_raw.items():
            sessions[_chat_id(raw_chat)] = _session_id(raw_session)
    except ValueError as exc:
        raise TelegramIdentityImportError("invalid_legacy_binding") from exc
    for raw_digest, raw_record in codes_raw.items():
        digest = str(raw_digest or "").strip().lower()
        if _HEX64_RE.fullmatch(digest) is None or not isinstance(raw_record, dict):
            raise TelegramIdentityImportError("invalid_link_codes")
        try:
            owner = _legacy_owner(raw_record.get("owner"))
            expires_at = int(raw_record.get("expires_at"))
        except (TypeError, ValueError, TelegramIdentityImportError) as exc:
            raise TelegramIdentityImportError("invalid_link_codes") from exc
        if expires_at <= 0:
            raise TelegramIdentityImportError("invalid_link_codes")
        codes[digest] = {"owner": owner, "expires_at": expires_at}
    allowed = _legacy_chat_ids(settings.get("telegram_allowed_chat_ids"))
    default_owner = normalize_identity(settings.get("telegram_owner"))
    if default_owner and len(default_owner) > 160:
        raise TelegramIdentityImportError("invalid_owner")
    return owners, sessions, codes, allowed, default_owner


def _resolve_import_account(db, owner: str, *, auth_enabled: bool) -> Account:
    account = find_account(db, owner)
    if account is not None:
        return account
    if not auth_enabled:
        from src.auth_helpers import DEFAULT_LOCAL_OWNER, configured_single_user_owner

        expected = normalize_identity(
            configured_single_user_owner() or DEFAULT_LOCAL_OWNER
        )
        if owner == expected:
            return ensure_account(db, owner)
    raise TelegramIdentityImportError("unknown_owner")


def adopt_legacy_telegram_identity(
    *,
    settings: Mapping[str, Any] | None = None,
    source_path: str | Path | None = None,
    auth_enabled: bool = True,
) -> TelegramImportResult:
    """Transactionally adopt the legacy settings maps without modifying them."""

    if settings is None:
        from src.settings import load_settings

        settings = load_settings()
    if not isinstance(settings, Mapping):
        raise TelegramIdentityImportError("invalid_settings")

    # Environment values historically overrode these configuration fields.
    # Fold them into the one explicit import, then runtime readers use SQL only.
    legacy = dict(settings)
    env_allowed = str(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()
    env_owner = str(os.getenv("TELEGRAM_OWNER") or "").strip()
    env_token = str(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if env_allowed:
        legacy["telegram_allowed_chat_ids"] = env_allowed
    if env_owner:
        legacy["telegram_owner"] = env_owner
    env_allow_all = os.getenv("TELEGRAM_ALLOW_ALL_CHATS")
    if env_allow_all is not None:
        legacy["telegram_allow_all_chats"] = env_allow_all

    raw_allow_all = legacy.get("telegram_allow_all_chats", False)
    if isinstance(raw_allow_all, bool):
        allow_all = raw_allow_all
    elif str(raw_allow_all or "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        allow_all = True
    elif str(raw_allow_all or "").strip().lower() in {
        "", "0", "false", "no", "off",
    }:
        allow_all = False
    else:
        raise TelegramIdentityImportError("invalid_allow_all_chats")
    if allow_all:
        # An open-ended rule cannot be projected to account-bound principals.
        # Lock the domain until the operator provides an explicit allowlist;
        # silently attributing every future chat to one account would defeat
        # the cross-interface identity boundary.
        raise TelegramIdentityImportError("unsupported_allow_all_chats")

    owners, sessions, codes, allowed, default_owner = _legacy_source(legacy)
    bot_scope = telegram_bot_fingerprint(
        bot_id=legacy.get("telegram_bot_id"),
        bot_token=env_token or legacy.get("telegram_bot_token"),
    )
    has_state = bool(owners or sessions or codes or allowed)
    if has_state and not bot_scope:
        raise TelegramIdentityImportError("missing_bot_identity")

    # Every allowlist/session-only chat needs one explicit owner. Ambiguous
    # multi-user legacy state is rejected rather than guessed.
    for chat in allowed | set(sessions):
        if chat not in owners:
            if not default_owner:
                raise TelegramIdentityImportError("ambiguous_chat_owner")
            owners[chat] = _legacy_owner(default_owner)

    canonical = {
        "bot_fingerprint": bot_scope,
        "chat_owners": sorted(owners.items()),
        "session_map": sorted(sessions.items()),
        "link_codes": sorted(
            (digest, record["owner"], record["expires_at"])
            for digest, record in codes.items()
        ),
    }
    source_sha256 = hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    source_label = str(Path(source_path).resolve()) if source_path else "settings.json"

    db = SessionLocal()
    try:
        existing = db.query(TelegramIdentityImportRun).filter(
            TelegramIdentityImportRun.source_kind == LEGACY_IMPORT_SOURCE
        ).first()
        if existing is not None:
            if existing.state != "completed":
                raise TelegramIdentityImportError("incomplete_previous_import")
            # Once a verified import completed, an explicit configuration
            # save may clear the inactive legacy maps. Empty is a valid
            # decommissioned source; any new non-empty divergent map is a
            # conflict and is never silently re-imported.
            if existing.source_sha256 != source_sha256 and has_state:
                raise TelegramIdentityImportError("legacy_source_changed")
            details = existing.details if isinstance(existing.details, dict) else {}
            unlock_telegram_identity_authority()
            return TelegramImportResult(
                state="already_completed",
                principals=int(details.get("principals") or 0),
                bindings=int(details.get("bindings") or 0),
                link_codes=int(details.get("link_codes") or 0),
                source_sha256=source_sha256,
            )

        account_by_owner = {
            owner: _resolve_import_account(
                db, owner, auth_enabled=auth_enabled,
            )
            for owner in sorted(
                set(owners.values())
                | {record["owner"] for record in codes.values()}
            )
        }
        principal_by_chat: dict[str, TelegramPrincipal] = {}
        for chat, owner in sorted(owners.items()):
            account = account_by_owner[owner]
            digest = telegram_chat_digest(bot_scope, chat)
            principal = db.query(TelegramPrincipal).filter(
                TelegramPrincipal.bot_fingerprint == bot_scope,
                TelegramPrincipal.chat_id_digest == digest,
            ).first()
            if principal is not None and principal.account_id != account.id:
                raise TelegramIdentityImportError("identity_conflict")
            if principal is None:
                principal = TelegramPrincipal(
                    id=str(uuid.uuid4()),
                    account_id=account.id,
                    bot_fingerprint=bot_scope,
                    chat_id=chat,
                    chat_id_digest=digest,
                    state="linked",
                    linked_at=_utcnow(),
                    version=1,
                )
                db.add(principal)
                db.flush()
            elif principal.state != "linked":
                raise TelegramIdentityImportError("identity_conflict")
            principal_by_chat[chat] = principal

        for chat, session in sorted(sessions.items()):
            principal = principal_by_chat.get(chat)
            if principal is None:
                raise TelegramIdentityImportError("binding_without_principal")
            binding = db.query(TelegramConversationBinding).filter(
                TelegramConversationBinding.principal_id == principal.id,
            ).first()
            if binding is not None and (
                binding.account_id != principal.account_id
                or str(binding.session_id or "") != session
            ):
                raise TelegramIdentityImportError("binding_conflict")
            if binding is None:
                db.add(TelegramConversationBinding(
                    id=str(uuid.uuid4()),
                    principal_id=principal.id,
                    account_id=principal.account_id,
                    session_id=session,
                    version=1,
                ))

        now = _utcnow()
        imported_codes = 0
        active_code_owners: set[str] = set()
        for digest, record in sorted(codes.items()):
            expires = datetime.fromtimestamp(
                int(record["expires_at"]), tz=timezone.utc,
            ).replace(tzinfo=None)
            if expires <= now:
                continue
            if record["owner"] in active_code_owners:
                raise TelegramIdentityImportError("multiple_active_link_codes")
            active_code_owners.add(record["owner"])
            account = account_by_owner[record["owner"]]
            existing_code = db.query(TelegramLinkCode).filter(
                TelegramLinkCode.bot_fingerprint == bot_scope,
                TelegramLinkCode.code_digest == digest,
            ).first()
            if existing_code is not None and (
                existing_code.account_id != account.id
                or existing_code.digest_scheme != "legacy_sha256_v1"
                or existing_code.expires_at != expires
            ):
                raise TelegramIdentityImportError("link_code_conflict")
            if existing_code is None:
                db.add(TelegramLinkCode(
                    id=str(uuid.uuid4()),
                    account_id=account.id,
                    bot_fingerprint=bot_scope,
                    code_digest=digest,
                    digest_scheme="legacy_sha256_v1",
                    expires_at=expires,
                ))
            imported_codes += 1

        details = {
            "principals": len(principal_by_chat),
            "bindings": len(sessions),
            "link_codes": imported_codes,
            "source_retained": True,
        }
        run = TelegramIdentityImportRun(
            id=str(uuid.uuid4()),
            source_kind=LEGACY_IMPORT_SOURCE,
            state="completed",
            source_sha256=source_sha256,
            source_path=source_label,
            details=details,
            completed_at=utcnow_naive(),
        )
        db.add(run)
        db.flush()

        # Verify the exact imported projection before making the import marker
        # durable. The source file is not altered before or after this check.
        for chat, principal in principal_by_chat.items():
            stored = db.query(TelegramPrincipal).filter(
                TelegramPrincipal.id == principal.id,
                TelegramPrincipal.account_id == principal.account_id,
                TelegramPrincipal.chat_id_digest
                == telegram_chat_digest(bot_scope, chat),
                TelegramPrincipal.state == "linked",
            ).first()
            if stored is None or str(stored.chat_id or "") != chat:
                raise TelegramIdentityImportError("verification_failed")
        db.commit()
        unlock_telegram_identity_authority()
        return TelegramImportResult(
            state="completed",
            principals=len(principal_by_chat),
            bindings=len(sessions),
            link_codes=imported_codes,
            source_sha256=source_sha256,
        )
    except TelegramIdentityImportError as exc:
        db.rollback()
        lock_telegram_identity_authority(exc.code)
        raise
    except BaseException:
        db.rollback()
        lock_telegram_identity_authority("telegram_identity_import_failed")
        raise
    finally:
        db.close()
