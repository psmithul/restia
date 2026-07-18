"""Owner-scoped contacts and CardDAV API.

The database is the only runtime authority.  Legacy JSON helpers are retained
only as fail-closed import/test compatibility symbols; routes and tools never
read or write those files after cutover.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from core.database import SessionLocal
from src import carddav_contacts as carddav
from src.constants import (
    CONTACTS_FILE as _CONTACTS_FILE,
    DATA_DIR as _DATA_DIR,
    SETTINGS_FILE as _SETTINGS_FILE,
)
from src.contact_service import (
    ContactConflict,
    ContactNotFound,
    ContactServiceError,
    clear_local_contacts,
    contacts_to_csv,
    contacts_to_vcf,
    create_contact,
    delete_contact as delete_contact_record,
    find_duplicate,
    get_carddav_source,
    get_contact_config,
    import_csv_contacts,
    import_vcards,
    list_contacts as list_contact_records,
    refresh_contact_source_detached,
    resolve_contact_conflict_detached,
    search_contacts as search_contact_records,
    update_contact as update_contact_record,
    upsert_carddav_config,
)
from src.audit_context import SESSION_AUDIT_CONTEXT_KEY
from src.identity import request_account_transaction
from src.url_safety import check_outbound_url


DATA_DIR = Path(_DATA_DIR)
SETTINGS_FILE = Path(_SETTINGS_FILE)
LOCAL_CONTACTS_FILE = Path(_CONTACTS_FILE)


# Protocol compatibility exports. They keep parser/security callers stable but
# contain no owner selection or persistent authority.
_parse_vcards = carddav.parse_vcards
_normalize_contact = carddav.normalize_contact
_contacts_to_vcf = contacts_to_vcf
_contacts_to_csv = contacts_to_csv


def _build_vcard(
    name: str,
    email: str = "",
    uid: str | None = None,
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    address: str = "",
) -> str:
    return carddav.build_vcard(
        name,
        email,
        uid,
        emails=emails,
        phones=phones,
        address=address,
    )


def _validate_carddav_url(url: object) -> str:
    """Compatibility wrapper whose safety hook remains monkeypatchable."""

    cleaned = (url if isinstance(url, str) else "").strip().rstrip("/")
    ok, reason = check_outbound_url(
        cleaned,
        block_private=os.getenv(
            "CARDDAV_BLOCK_PRIVATE_IPS", "false"
        ).lower() == "true",
    )
    if not ok:
        raise ValueError(f"Rejected CardDAV URL: {reason}")
    return cleaned


def _load_settings() -> dict[str, Any]:
    """Read-only legacy inspection for recovery tooling.

    Runtime contact paths never call this helper.  The dedicated importer does
    bounded, race-safe reads before cutover instead.
    """

    if not SETTINGS_FILE.exists():
        return {}
    value = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _save_settings(_settings: dict[str, Any]) -> None:
    raise RuntimeError("Legacy contacts settings are rollback-only after database cutover")


def _get_carddav_config() -> dict[str, str]:
    """Read-only legacy configuration view for recovery/security shims."""

    settings = _load_settings()
    stored_password = settings.get("carddav_password")
    if stored_password not in (None, ""):
        from src.secret_storage import decrypt

        password = decrypt(str(stored_password))
    else:
        password = str(os.environ.get("CARDDAV_PASSWORD") or "")
    return {
        "url": str(settings.get("carddav_url") or os.environ.get("CARDDAV_URL") or ""),
        "username": str(
            settings.get("carddav_username")
            or os.environ.get("CARDDAV_USERNAME")
            or ""
        ),
        "password": password,
    }


def _abs_url(href: str) -> str:
    cfg = _get_carddav_config()
    # Preserve the legacy wrapper's monkeypatchable validation boundary.
    base = _validate_carddav_url(cfg.get("url") or "")
    from urllib.parse import urljoin, urlparse, urlunparse

    base_parts = urlparse(base)
    joined = urljoin(base.rstrip("/") + "/", str(href or ""))
    joined_parts = urlparse(joined)
    if (joined_parts.scheme, joined_parts.netloc) != (
        base_parts.scheme, base_parts.netloc,
    ):
        joined = urlunparse((
            base_parts.scheme,
            base_parts.netloc,
            joined_parts.path or "/",
            "",
            joined_parts.query,
            "",
        ))
    return _validate_carddav_url(joined)


def _vcard_url(uid: str) -> str:
    from urllib.parse import quote

    base = _validate_carddav_url(_get_carddav_config().get("url") or "")
    return base + "/" + quote(str(uid), safe="") + ".vcf"


def _removed_global_authority(*_args, **_kwargs):
    raise RuntimeError("Contacts require an explicit owner-scoped database service")


# Existing extensions that still try these names fail loudly instead of
# silently reaching rollback JSON or a process-global cache.
_fetch_contacts = _removed_global_authority
_create_contact = _removed_global_authority
_update_contact = _removed_global_authority
_delete_contact = _removed_global_authority
_import_vcards = _removed_global_authority
_import_csv_contacts = _removed_global_authority


def _raise_service_error(exc: ContactServiceError) -> None:
    if isinstance(exc, ContactNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, ContactConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def _normalize_add_payload(data: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    name = str(data.get("name") or "").strip()
    email = str(data.get("email") or "").strip()
    phone = str(data.get("phone") or "").strip()
    phones = [
        str(value or "").strip()
        for value in (data.get("phones") or [])
        if str(value or "").strip()
    ]
    if phone and phone not in phones:
        phones.insert(0, phone)
    address = str(data.get("address") or "").strip()
    if not name and email:
        name = email.split("@", 1)[0]
    if not name and not email and not phones and not address:
        return {}, "Name, email, phone, or address required"
    if not name:
        name = email.split("@", 1)[0] if email else (
            phones[0] if phones else "Contact"
        )
    return {
        "name": name,
        "email": email,
        "phones": phones,
        "address": address,
    }, None


def _normalize_import_payload(
    data: dict[str, Any],
) -> tuple[str, str, str | None]:
    text = str(data.get("vcf") or data.get("text") or "")
    csv_text = str(data.get("csv") or "")
    if text.strip() and "BEGIN:VCARD" not in text.upper():
        return text, csv_text, "No vCard data found"
    if not text.strip() and not csv_text.strip():
        return text, csv_text, "No contact data found"
    return text, csv_text, None


def _require_contact_authority(request: Request) -> None:
    error_code = getattr(
        getattr(request.app, "state", None), "contacts_store_error", None,
    )
    if error_code:
        raise HTTPException(
            503,
            "Contacts database cutover requires administrator recovery",
        )


def setup_contacts_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(
        prefix="/api/contacts",
        tags=["contacts"],
        dependencies=[Depends(_require_contact_authority)],
    )

    @router.get("/list")
    def list_contacts(request: Request) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("contacts:read",), write=False,
            ) as account:
                contacts = (
                    list_contact_records(
                        db,
                        owner_id=account.id,
                        refresh=False,
                        create_local=False,
                    )
                    if account is not None else []
                )
                return {"contacts": contacts, "count": len(contacts)}
        except ContactServiceError as exc:
            _raise_service_error(exc)
        finally:
            db.close()

    @router.get("/search")
    def search_contacts(
        request: Request, q: str = Query(default=""),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("contacts:read",), write=False,
            ) as account:
                results = (
                    search_contact_records(
                        db,
                        owner_id=account.id,
                        query=q,
                        refresh=False,
                        limit=10,
                    )
                    if account is not None else []
                )
                return {"results": results}
        except ContactServiceError as exc:
            _raise_service_error(exc)
        finally:
            db.close()

    @router.post("/add")
    def add_contact(request: Request, data: dict[str, Any]) -> dict[str, Any]:
        normalized, error = _normalize_add_payload(data)
        if error:
            return {"success": False, "error": error}
        name = normalized["name"]
        email = normalized["email"]
        phones = normalized["phones"]
        address = normalized["address"]

        db = session_factory()
        result: dict[str, Any]
        try:
            with request_account_transaction(
                db, request, required_scopes=("contacts:write",), write=True,
            ) as account:
                duplicate = find_duplicate(
                    db,
                    owner_id=account.id,
                    email=email,
                    phones=phones,
                )
                if duplicate is not None:
                    result = {
                        "success": True,
                        "message": "Already exists",
                        "contact": duplicate,
                    }
                else:
                    contact = create_contact(
                        db,
                        owner_id=account.id,
                        name=name,
                        email=email,
                        phones=phones,
                        address=address,
                    )
                    result = {"success": True, "contact": contact}
        except ContactServiceError as exc:
            _raise_service_error(exc)
        finally:
            db.close()
        return result

    @router.post("/import")
    def import_contacts(request: Request, data: dict[str, Any]) -> dict[str, Any]:
        text, csv_text, error = _normalize_import_payload(data)
        if error:
            return {"success": False, "error": error}
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("contacts:write",), write=True,
            ) as account:
                result = (
                    import_vcards(db, owner_id=account.id, text=text)
                    if text.strip()
                    else import_csv_contacts(
                        db, owner_id=account.id, text=csv_text,
                    )
                )
                result["success"] = int(result.get("imported", 0)) > 0
        except ContactServiceError as exc:
            _raise_service_error(exc)
        finally:
            db.close()
        return result

    @router.post("/refresh")
    def refresh_contacts(request: Request) -> dict[str, Any]:
        """Explicit CardDAV refresh; identity/SQL sessions end before HTTP."""

        db = session_factory()
        owner_id: str | None = None
        source_id: str | None = None
        audit_context: dict[str, Any] = {}
        local_result: dict[str, Any] | None = None
        try:
            with request_account_transaction(
                db, request, required_scopes=("contacts:read",), write=False,
            ) as account:
                if account is None:
                    local_result = {"contacts": [], "count": 0}
                else:
                    owner_id = account.id
                    source = get_carddav_source(db, owner_id=owner_id)
                    if source is None:
                        contacts = list_contact_records(
                            db,
                            owner_id=owner_id,
                            refresh=False,
                            create_local=False,
                        )
                        local_result = {
                            "contacts": contacts,
                            "count": len(contacts),
                        }
                    else:
                        source_id = source.id
                        audit_context = dict(
                            db.info.get(SESSION_AUDIT_CONTEXT_KEY) or {}
                        )
        except ContactServiceError as exc:
            _raise_service_error(exc)
        finally:
            db.close()
        if local_result is not None:
            return local_result
        try:
            refresh_contact_source_detached(
                session_factory,
                owner_id=str(owner_id),
                source_id=str(source_id),
                raise_errors=True,
                audit_context=audit_context,
            )
        except ContactServiceError as exc:
            _raise_service_error(exc)
        read_db = session_factory()
        try:
            contacts = list_contact_records(
                read_db,
                owner_id=str(owner_id),
                refresh=False,
                create_local=False,
            )
            read_db.rollback()
        finally:
            read_db.close()
        return {"contacts": contacts, "count": len(contacts)}

    @router.get("/export")
    def export_contacts(
        request: Request,
        format: str = Query(default="vcf", pattern="^(vcf|csv)$"),
    ) -> Response:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("contacts:read",), write=False,
            ) as account:
                # Export the durable authority snapshot. A user can explicitly
                # refresh first without making every download inherit a remote
                # server timeout.
                contacts = (
                    list_contact_records(
                        db,
                        owner_id=account.id,
                        refresh=False,
                        create_local=False,
                    )
                    if account is not None else []
                )
                if format == "csv":
                    content = contacts_to_csv(contacts)
                    media_type = "text/csv; charset=utf-8"
                    filename = "restia-contacts.csv"
                else:
                    content = contacts_to_vcf(contacts)
                    media_type = "text/vcard; charset=utf-8"
                    filename = "restia-contacts.vcf"
                return Response(
                    content=content,
                    media_type=media_type,
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"'
                    },
                )
        except ContactServiceError as exc:
            _raise_service_error(exc)
        finally:
            db.close()

    @router.get("/config")
    def get_config(request: Request) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db,
                request,
                required_scopes=("contacts:configure",),
                write=False,
            ) as account:
                return (
                    get_contact_config(db, owner_id=account.id)
                    if account is not None
                    else {"url": "", "username": "", "password": ""}
                )
        finally:
            db.close()

    @router.put("/config")
    def update_config(request: Request, data: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if "carddav_url" in data or "url" in data:
            kwargs["url"] = data.get("carddav_url", data.get("url"))
        if "carddav_username" in data or "username" in data:
            kwargs["username"] = data.get(
                "carddav_username", data.get("username")
            )
        if "carddav_password" in data or "password" in data:
            kwargs["password"] = data.get(
                "carddav_password", data.get("password")
            )
        expected_version = data.get("expected_version", data.get("version"))
        if expected_version is not None:
            try:
                kwargs["expected_version"] = int(expected_version)
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, "Contact configuration version is invalid") from exc
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("contacts:configure",), write=True,
            ) as account:
                try:
                    source = upsert_carddav_config(
                        db, owner_id=account.id, **kwargs,
                    )
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                return {
                    "success": True,
                    "version": int(source.config_version or 1),
                }
        except ContactServiceError as exc:
            _raise_service_error(exc)
        finally:
            db.close()

    @router.delete("/clear")
    def clear_contacts(request: Request) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("contacts:write",), write=True,
            ) as account:
                clear_local_contacts(db, owner_id=account.id)
                return {"success": True}
        except ContactServiceError as exc:
            _raise_service_error(exc)
        finally:
            db.close()

    # Literal routes stay above the UID routes.
    @router.post("/{uid}/resolve-conflict")
    def resolve_contact_conflict(
        request: Request, uid: str, data: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            expected_version = int(
                data.get("expected_version", data.get("version"))
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(428, "Contact version is required") from exc
        resolution = str(data.get("resolution") or "").strip().lower()
        if resolution not in {"keep_local", "use_remote"}:
            raise HTTPException(
                400, "resolution must be keep_local or use_remote",
            )

        db = session_factory()
        owner_id: str | None = None
        audit_context: dict[str, Any] = {}
        try:
            with request_account_transaction(
                db, request, required_scopes=("contacts:write",), write=False,
            ) as account:
                if account is None:
                    raise ContactNotFound("Contact owner not found")
                owner_id = account.id
                audit_context = dict(
                    db.info.get(SESSION_AUDIT_CONTEXT_KEY) or {}
                )
        except ContactServiceError as exc:
            _raise_service_error(exc)
        finally:
            db.close()
        try:
            contact = resolve_contact_conflict_detached(
                session_factory,
                owner_id=str(owner_id),
                uid=uid,
                source_id=str(data.get("source_id") or "").strip() or None,
                expected_version=expected_version,
                resolution=resolution,
                audit_context=audit_context,
            )
        except ContactServiceError as exc:
            _raise_service_error(exc)
        return {"success": True, "contact": contact}

    @router.put("/{uid}")
    def edit_contact(
        request: Request, uid: str, data: dict[str, Any],
    ) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        emails = data.get("emails")
        if emails is None and data.get("email"):
            emails = [data["email"]]
        clean_emails = [
            str(value or "").strip()
            for value in (emails or [])
            if str(value or "").strip()
        ]
        clean_phones = [
            str(value or "").strip()
            for value in (data.get("phones") or [])
            if str(value or "").strip()
        ]
        address = str(data.get("address") or "").strip()
        try:
            expected_version = int(data.get("expected_version", data.get("version")))
        except (TypeError, ValueError) as exc:
            raise HTTPException(428, "Contact version is required") from exc
        if not name and not clean_emails and not clean_phones and not address:
            return {
                "success": False,
                "error": "Name, email, phone, or address required",
            }
        if not name and clean_emails:
            name = clean_emails[0].split("@", 1)[0]
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("contacts:write",), write=True,
            ) as account:
                contact = update_contact_record(
                    db,
                    owner_id=account.id,
                    uid=uid,
                    name=name,
                    emails=clean_emails,
                    phones=clean_phones,
                    address=address,
                    expected_version=expected_version,
                    source_id=str(data.get("source_id") or "").strip() or None,
                )
        except ContactServiceError as exc:
            _raise_service_error(exc)
        finally:
            db.close()
        return {"success": True, "contact": contact}

    @router.delete("/{uid}")
    def delete_contact(
        request: Request,
        uid: str,
        expected_version: int = Query(..., ge=1),
        source_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        if not uid:
            return {"success": False, "error": "UID required"}
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("contacts:write",), write=True,
            ) as account:
                success = delete_contact_record(
                    db,
                    owner_id=account.id,
                    uid=uid,
                    expected_version=expected_version,
                    source_id=source_id,
                )
        except ContactServiceError as exc:
            _raise_service_error(exc)
        finally:
            db.close()
        return {"success": success}

    return router


__all__ = ["setup_contacts_routes"]
