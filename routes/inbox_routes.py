"""Owner-scoped V3 universal inbox API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.life_ingestion import (
    LifeIngestionError,
    capture_ingestion_metadata,
    normalize_capture_source_category,
)
from src.inbox_pagination import (
    InboxCursorError,
    decode_inbox_cursor,
    encode_inbox_cursor,
)
from src.life_core import (
    LifeCoreConflict,
    LifeCoreError,
    LifeCoreNotFound,
    LifeCoreUnsupported,
    archive_inbox_item,
    classify_inbox_item,
    create_inbox_item,
    get_inbox_item,
    list_inbox_items,
    normalize_kind,
    process_inbox_item,
    serialize_inbox_item,
    update_inbox_item,
)


class InboxCreate(BaseModel):
    title: str = Field(default="", max_length=240)
    content: str = Field(default="", max_length=100_000)
    kind: str | None = None
    source_type: str = Field(default="text", max_length=48)
    source_ref: str | None = Field(default=None, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)


class InboxUpdate(BaseModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    content: str | None = Field(default=None, max_length=100_000)
    kind: str | None = None
    source_ref: str | None = Field(default=None, max_length=500)
    metadata: dict[str, Any] | None = None


class InboxVersion(BaseModel):
    version: int = Field(ge=1)


class InboxProcess(InboxVersion):
    project_id: str | None = Field(default=None, max_length=36)


def _raise_domain_error(exc: LifeCoreError) -> None:
    if isinstance(exc, LifeCoreNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, LifeCoreConflict):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, LifeCoreUnsupported):
        raise HTTPException(409, {
            "status": "unsupported",
            "kind": exc.kind,
            "message": str(exc),
        }) from exc
    raise HTTPException(400, str(exc)) from exc


def setup_inbox_routes(
    *, session_factory=SessionLocal, cursor_signing_key: bytes | None = None
) -> APIRouter:
    router = APIRouter(prefix="/api/inbox", tags=["inbox"])

    def signing_key() -> bytes:
        if cursor_signing_key is not None:
            return cursor_signing_key
        from src.secret_storage import _load_or_create_key

        return _load_or_create_key()

    @router.get("")
    def list_items(
        request: Request,
        status: str = Query(default="inbox"),
        kind: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            normalized_status = str(status or "inbox").strip().lower()
            if normalized_status not in {"all", "inbox", "processed", "archived"}:
                raise LifeCoreError(
                    "status must be all, inbox, processed, or archived"
                )
            normalized_kind = normalize_kind(kind) if kind else None
            with request_account_transaction(
                db, request, required_scopes=("todos:read",), write=False
            ) as account:
                if account is None:
                    if cursor:
                        # A principal without V3 rows still receives the same
                        # fail-closed cursor contract; do not silently accept
                        # an unverifiable/tampered continuation token.
                        raise HTTPException(400, "Invalid inbox cursor")
                    return {
                        "items": [],
                        "count": 0,
                        "truncated": False,
                        "next_cursor": None,
                    }
                page_cursor = None
                if cursor:
                    try:
                        page_cursor = decode_inbox_cursor(
                            cursor,
                            signing_key=signing_key(),
                            owner_id=account.id,
                            status=normalized_status,
                            kind=normalized_kind,
                        )
                    except InboxCursorError as exc:
                        raise HTTPException(400, "Invalid inbox cursor") from exc
                items, truncated = list_inbox_items(
                    db,
                    owner_id=account.id,
                    status=normalized_status,
                    kind=normalized_kind,
                    limit=limit,
                    before_updated_at=(page_cursor.updated_at if page_cursor else None),
                    before_id=(page_cursor.item_id if page_cursor else None),
                )
                next_cursor = None
                if truncated and items:
                    final = items[-1]
                    next_cursor = encode_inbox_cursor(
                        signing_key=signing_key(),
                        owner_id=account.id,
                        status=normalized_status,
                        kind=normalized_kind,
                        updated_at=final.updated_at,
                        item_id=final.id,
                    )
                return {
                    "items": [serialize_inbox_item(item) for item in items],
                    "count": len(items),
                    "truncated": truncated,
                    "next_cursor": next_cursor,
                }
        except LifeCoreError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("", status_code=201)
    def create_item(request: Request, body: InboxCreate) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("todos:write",), write=True
            ) as account:
                source_type = normalize_capture_source_category(body.source_type)
                item, created = create_inbox_item(
                    db,
                    account=account,
                    title=body.title,
                    content=body.content,
                    kind=body.kind,
                    source_type=source_type,
                    source_ref=body.source_ref,
                    metadata=capture_ingestion_metadata(
                        source_type=source_type,
                        metadata=body.metadata,
                    ),
                    idempotency_key=body.idempotency_key,
                )
                return {"item": serialize_inbox_item(item), "created": created}
        except LifeIngestionError as exc:
            db.rollback()
            raise HTTPException(400, str(exc)) from exc
        except LifeCoreError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/{item_id}")
    def get_item(request: Request, item_id: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("todos:read",), write=False
            ) as account:
                if account is None:
                    raise LifeCoreNotFound("Inbox item not found")
                item = get_inbox_item(db, owner_id=account.id, item_id=item_id)
                return {"item": serialize_inbox_item(item)}
        except LifeCoreError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.patch("/{item_id}")
    def update_item(
        request: Request, item_id: str, body: InboxUpdate
    ) -> dict[str, Any]:
        fields = getattr(body, "model_fields_set", None)
        if fields is None:
            fields = getattr(body, "__fields_set__", set())
        kwargs = {
            field: getattr(body, field)
            for field in ("title", "content", "kind", "source_ref", "metadata")
            if field in fields
        }
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("todos:write",), write=True
            ) as account:
                if "metadata" in kwargs:
                    current = get_inbox_item(
                        db, owner_id=account.id, item_id=item_id
                    )
                    kwargs["metadata"] = capture_ingestion_metadata(
                        source_type=current.source_type,
                        metadata=kwargs["metadata"],
                    )
                item = update_inbox_item(
                    db,
                    owner_id=account.id,
                    item_id=item_id,
                    expected_version=body.version,
                    **kwargs,
                )
                return {"item": serialize_inbox_item(item)}
        except LifeCoreError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/{item_id}/classify")
    def classify_item(
        request: Request, item_id: str, body: InboxVersion
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("todos:write",), write=True
            ) as account:
                item = classify_inbox_item(
                    db,
                    owner_id=account.id,
                    item_id=item_id,
                    expected_version=body.version,
                )
                return {"item": serialize_inbox_item(item)}
        except LifeCoreError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/{item_id}/process")
    def process_item(
        request: Request, item_id: str, body: InboxProcess
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("todos:write",), write=True
            ) as account:
                item = process_inbox_item(
                    db,
                    account=account,
                    item_id=item_id,
                    expected_version=body.version,
                    project_id=body.project_id,
                )
                return {"item": serialize_inbox_item(item)}
        except LifeCoreError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/{item_id}/archive")
    def archive_item(
        request: Request, item_id: str, body: InboxVersion
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("todos:write",), write=True
            ) as account:
                item = archive_inbox_item(
                    db,
                    owner_id=account.id,
                    item_id=item_id,
                    expected_version=body.version,
                )
                return {"item": serialize_inbox_item(item)}
        except LifeCoreError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router
