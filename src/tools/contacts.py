"""Owner-explicit contacts-domain tool implementations."""

from __future__ import annotations

import asyncio
from typing import Dict, Optional

from core.database import SessionLocal
from src.contact_service import (
    ContactServiceError,
    create_contact,
    delete_contact,
    find_duplicate,
    list_contacts,
    update_contact,
)
from src.contact_delivery import drain_contact_deliveries
from src.audit_context import bind_service_audit_context
from src.identity import find_account
from src.tools._common import _parse_tool_args


def _owner(value: object) -> str:
    owner = str(value or "").strip().lower()
    if not owner or owner == "api":
        raise ContactServiceError("A concrete contact owner is required")
    return owner


def _with_owner(owner: str, operation, *, write: bool):
    db = SessionLocal()
    try:
        account = find_account(db, _owner(owner))
        if account is None:
            raise ContactServiceError("Contact owner is not linked to an account")
        owner_id = account.id
        db.rollback()
        drain_contact_deliveries(SessionLocal, owner_id=owner_id, limit=1)
        bind_service_audit_context(
            db,
            account_id=owner_id,
            interface="internal_tool",
            actor_type="agent_tool",
            credential_type="internal",
        )
        result = operation(db, owner_id)
        db.commit()
        if write:
            drain_contact_deliveries(SessionLocal, owner_id=owner_id, limit=1)
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _list_for_owner(owner: str, *, refresh: bool) -> list[dict]:
    return _with_owner(
        owner,
        lambda db, owner_id: list_contacts(
            db,
            owner_id=owner_id,
            refresh=refresh,
            create_local=False,
        ),
        write=False,
    )


def _current_contact(
    db, *, owner_id: str, uid: str, source_id: str | None = None,
) -> dict:
    matches = []
    for contact in list_contacts(
        db, owner_id=owner_id, refresh=False, create_local=False,
    ):
        if str(contact.get("uid") or "") != str(uid):
            continue
        if source_id and str(contact.get("source_id") or "") != str(source_id):
            continue
        matches.append(contact)
    if len(matches) > 1:
        raise ContactServiceError(
            "Contact identifier is ambiguous; provide source_id"
        )
    if matches:
        return matches[0]
    raise ContactServiceError("Contact not found")


def _update_current_contact(
    db, *, owner_id: str, uid: str, name: str,
    emails: list[str], phones: list[str], address: str,
    source_id: str | None = None,
):
    current = _current_contact(
        db, owner_id=owner_id, uid=uid, source_id=source_id,
    )
    return update_contact(
        db,
        owner_id=owner_id,
        uid=uid,
        name=name,
        emails=emails,
        phones=phones,
        address=address,
        expected_version=int(current.get("version") or 1),
        source_id=str(current.get("source_id") or "") or None,
    )


def _delete_current_contact(
    db, *, owner_id: str, uid: str, source_id: str | None = None,
) -> bool:
    current = _current_contact(
        db, owner_id=owner_id, uid=uid, source_id=source_id,
    )
    return delete_contact(
        db,
        owner_id=owner_id,
        uid=uid,
        expected_version=int(current.get("version") or 1),
        source_id=str(current.get("source_id") or "") or None,
    )


async def do_resolve_contact(content: str, owner: Optional[str] = None) -> Dict:
    """Look up one owner's contacts, then their owner-attributed email history."""

    import httpx

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    name = str(args.get("name") or "").strip()
    if not name:
        return {"error": "name is required", "exit_code": 1}
    try:
        explicit_owner = _owner(owner)
    except ContactServiceError as exc:
        return {"error": str(exc), "exit_code": 1}

    matches: dict[str, dict] = {}
    try:
        rows = await asyncio.to_thread(
            _list_for_owner, explicit_owner, refresh=False,
        )
        query = name.lower()
        for contact in rows:
            if not (
                query in str(contact.get("name") or "").lower()
                or any(
                    query in str(value or "").lower()
                    for value in contact.get("emails") or []
                )
            ):
                continue
            has_email = False
            for raw_email in contact.get("emails") or []:
                email = str(raw_email or "").strip().lower()
                if email and "@" in email:
                    matches[email] = {
                        "name": contact.get("name") or email,
                        "source": "contacts",
                    }
                    has_email = True
            if not has_email:
                for raw_phone in contact.get("phones") or []:
                    phone = str(raw_phone or "").strip()
                    if phone:
                        matches[phone] = {
                            "name": contact.get("name") or phone,
                            "source": "contacts",
                            "phone": phone,
                        }
    except ContactServiceError as exc:
        return {"error": str(exc), "exit_code": 1}

    try:
        from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
        from src.tool_implementations import _INTERNAL_BASE

        headers = {
            INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN,
            "X-Restia-Owner": explicit_owner,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{_INTERNAL_BASE}/api/email/resolve-contact",
                params={"name": name},
                headers=headers,
            )
            if response.status_code == 200:
                for contact in response.json().get("contacts") or []:
                    email = str(contact.get("email") or "").strip().lower()
                    if email and email not in matches:
                        matches[email] = {
                            "name": contact.get("name") or email,
                            "source": "email history",
                        }
    except Exception:
        pass

    if not matches:
        return {"output": f"No contacts found matching '{name}'.", "exit_code": 0}
    lines = [f"Contacts matching '{name}':"]
    for key, info in matches.items():
        if info.get("phone"):
            lines.append(f"- {info['name']} — phone: {info['phone']} ({info['source']})")
        else:
            lines.append(f"- {info['name']} <{key}> ({info['source']})")
    return {"output": "\n".join(lines), "exit_code": 0}


async def do_manage_contact(content: str, owner: Optional[str] = None) -> Dict:
    """List or mutate contacts only through the explicit owner's DB authority."""

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}
    try:
        explicit_owner = _owner(owner)
    except ContactServiceError as exc:
        return {"error": str(exc), "exit_code": 1}
    action = str(args.get("action") or "").strip().lower()

    try:
        if action == "list":
            rows = await asyncio.to_thread(
                _list_for_owner,
                explicit_owner,
                refresh=bool(args.get("refresh", False)),
            )
            if not rows:
                return {"output": "No contacts.", "exit_code": 0}
            lines = [f"{len(rows)} contacts:"]
            for contact in rows:
                emails = ", ".join(contact.get("emails") or [])
                lines.append(
                    f"- {contact.get('name') or '(no name)'} <{emails}>  "
                    f"[uid={contact.get('uid', '')}; "
                    f"source_id={contact.get('source_id', '')}]"
                )
            return {"output": "\n".join(lines), "exit_code": 0}

        if action == "add":
            email = str(args.get("email") or "").strip()
            phones = [
                str(value or "").strip()
                for value in (args.get("phones") or [])
                if str(value or "").strip()
            ]
            phone = str(args.get("phone") or "").strip()
            if phone and phone not in phones:
                phones.insert(0, phone)
            address = str(args.get("address") or "").strip()
            name = str(args.get("name") or "").strip()
            if not name and email:
                name = email.split("@", 1)[0]
            if not name and not email and not phones and not address:
                return {
                    "error": "name plus email, phone, or address is required for add",
                    "exit_code": 1,
                }
            if not name:
                name = email.split("@", 1)[0] if email else (
                    phones[0] if phones else "Contact"
                )

            def add(db, owner_id):
                duplicate = find_duplicate(
                    db, owner_id=owner_id, email=email, phones=phones,
                )
                if duplicate is not None:
                    return duplicate, False
                return create_contact(
                    db,
                    owner_id=owner_id,
                    name=name,
                    email=email,
                    phones=phones,
                    address=address,
                ), True

            _contact, created = await asyncio.to_thread(
                _with_owner, explicit_owner, add, write=True,
            )
            detail = email or ", ".join(phones) or address
            verb = "Added" if created else "Already had"
            return {"output": f"{verb} {name} ({detail}).", "exit_code": 0}

        if action in ("update", "edit"):
            uid = str(args.get("uid") or "").strip()
            source_id = str(args.get("source_id") or "").strip() or None
            if not uid:
                return {
                    "error": "uid is required for update (use action=list to find it)",
                    "exit_code": 1,
                }
            name = str(args.get("name") or "").strip()
            emails = args.get("emails")
            if emails is None and args.get("email"):
                emails = [args["email"]]
            clean_emails = [
                str(value or "").strip()
                for value in (emails or [])
                if str(value or "").strip()
            ]
            clean_phones = [
                str(value or "").strip()
                for value in (args.get("phones") or [])
                if str(value or "").strip()
            ]
            address = str(args.get("address") or "").strip()
            if not name and not clean_emails and not clean_phones and not address:
                return {
                    "error": "Provide a name, emails, phones, or address to update",
                    "exit_code": 1,
                }
            if not name and clean_emails:
                name = clean_emails[0].split("@", 1)[0]
            await asyncio.to_thread(
                _with_owner,
                explicit_owner,
                lambda db, owner_id: _update_current_contact(
                    db, owner_id=owner_id, uid=uid, name=name,
                    emails=clean_emails, phones=clean_phones,
                    address=address, source_id=source_id,
                ),
                write=True,
            )
            return {"output": "Contact updated.", "exit_code": 0}

        if action == "delete":
            uid = str(args.get("uid") or "").strip()
            source_id = str(args.get("source_id") or "").strip() or None
            if not uid:
                return {
                    "error": "uid is required for delete (use action=list to find it)",
                    "exit_code": 1,
                }
            await asyncio.to_thread(
                _with_owner,
                explicit_owner,
                lambda db, owner_id: _delete_current_contact(
                    db, owner_id=owner_id, uid=uid, source_id=source_id,
                ),
                write=True,
            )
            return {"output": "Contact deleted.", "exit_code": 0}

        return {
            "error": f"Unknown action '{action}'. Use list, add, update, or delete.",
            "exit_code": 1,
        }
    except ContactServiceError as exc:
        return {"error": f"Contact operation failed: {exc}", "exit_code": 1}
    except Exception as exc:
        return {
            "error": f"Contact operation failed: {type(exc).__name__}",
            "exit_code": 1,
        }


__all__ = ["do_manage_contact", "do_resolve_contact"]
