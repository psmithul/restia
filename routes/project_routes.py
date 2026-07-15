"""Owner/member-scoped Jira-style project workflow API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, BinaryIO, Callable, Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from core.database import (
    Project,
    ProjectActivity,
    ProjectAttachment,
    ProjectChecklistItem,
    ProjectComment,
    ProjectMember,
    ProjectRemoteGrant,
    ProjectQuotaLock,
    ProjectStage,
    ProjectWorkItem,
    LinkGuest,
    RemoteBlock,
    SessionLocal,
    project_owner_quota_lock_key,
    utcnow_naive,
)
from src.auth_helpers import effective_owner, require_user
from src.project_storage import (
    ProjectFileStore,
    read_project_attachment,
    write_project_attachment_cancellation_safe,
)
from src.upload_limits import (
    PROJECT_MAX_ATTACHMENTS_PER_ITEM,
    PROJECT_MAX_ATTACHMENTS_PER_PROJECT,
    PROJECT_MAX_ACTIVE_ITEMS,
    PROJECT_MAX_CHECKLIST_ITEMS_PER_ITEM,
    PROJECT_MAX_COMMENTS_PER_ITEM,
    PROJECT_MAX_ITEMS,
    PROJECT_MAX_ACTIVITY_PER_PROJECT,
    PROJECT_MAX_PROJECTS_PER_OWNER,
    PROJECT_MAX_STAGES_PER_PROJECT,
    PROJECT_GLOBAL_STORAGE_MAX_BYTES,
    PROJECT_OWNER_STORAGE_MAX_BYTES,
    PROJECT_STORAGE_MAX_BYTES,
    format_byte_limit,
)


logger = logging.getLogger(__name__)


EXPLICIT_PROJECT_FALLBACK_OWNER = (
    os.getenv("RESTIA_FALLBACK_OWNER")
    or os.getenv("ODYSSEUS_FALLBACK_OWNER")
    or ""
).strip().lower()
FIRST_RUN_PROJECT_OWNER = "owner@localhost"
FALLBACK_PROJECT_OWNER = EXPLICIT_PROJECT_FALLBACK_OWNER or FIRST_RUN_PROJECT_OWNER

PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,11}$")
PROJECT_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{2})?)?$")
PROJECT_ROLES = {"viewer": 1, "editor": 2, "owner": 3}
REMOTE_GRANT_ROLES = {"viewer", "editor"}
REMOTE_GRANT_STATUSES = {"pending", "active", "declined", "revoked"}
REMOTE_GRANT_PRINCIPAL_PREFIX = "remote:"
REMOTE_INSTANCE_PRINCIPAL_PREFIX = "remote-instance:"
REMOTE_GRANT_TOMBSTONE_LIMIT = 100
STAGE_CATEGORIES = {"backlog", "todo", "in_progress", "review", "done"}
ITEM_TYPES = {"task", "story", "bug", "epic", "subtask"}
PRIORITIES = {"lowest", "low", "medium", "high", "highest", "critical"}
ATTACHMENT_KINDS = {"reference", "draft", "deliverable"}
MAX_ACTIVITY_LIMIT = 200
ITEM_LIST_MAX_LIMIT = min(PROJECT_MAX_ITEMS, 2_000)
ITEM_DETAIL_COMMENT_LIMIT = min(PROJECT_MAX_COMMENTS_PER_ITEM, 200)
ATTACHMENT_INTEGRITY_CHUNK_BYTES = 1024 * 1024


def _bounded_download_concurrency_env(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


PROJECT_ATTACHMENT_DOWNLOAD_CONCURRENCY = _bounded_download_concurrency_env(
    "RESTIA_PROJECT_ATTACHMENT_DOWNLOAD_CONCURRENCY",
    8,
    64,
)
PROJECT_ATTACHMENT_DOWNLOAD_PER_PRINCIPAL = _bounded_download_concurrency_env(
    "RESTIA_PROJECT_ATTACHMENT_DOWNLOAD_PER_PRINCIPAL",
    2,
    16,
)
if (
    PROJECT_ATTACHMENT_DOWNLOAD_PER_PRINCIPAL
    > PROJECT_ATTACHMENT_DOWNLOAD_CONCURRENCY
):
    raise ValueError(
        "RESTIA_PROJECT_ATTACHMENT_DOWNLOAD_PER_PRINCIPAL cannot exceed "
        "RESTIA_PROJECT_ATTACHMENT_DOWNLOAD_CONCURRENCY"
    )
_PROJECT_AUTH_CONTEXT: ContextVar[Optional[tuple[Request, str]]] = ContextVar(
    "project_auth_context", default=None
)
_PROJECT_REMOTE_CONTEXT: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "project_remote_context", default=None
)

PROJECT_TEMPLATES: dict[str, list[tuple[str, str, str]]] = {
    "general": [
        ("Backlog", "backlog", "#64748b"),
        ("To Do", "todo", "#3b82f6"),
        ("In Progress", "in_progress", "#f59e0b"),
        ("Review", "review", "#8b5cf6"),
        ("Done", "done", "#22c55e"),
    ],
    "software": [
        ("Backlog", "backlog", "#64748b"),
        ("Selected", "todo", "#3b82f6"),
        ("In Progress", "in_progress", "#f59e0b"),
        ("Code Review", "review", "#8b5cf6"),
        ("Done", "done", "#22c55e"),
    ],
    "research": [
        ("Questions", "backlog", "#64748b"),
        ("Reading", "todo", "#3b82f6"),
        ("Experiment", "in_progress", "#f59e0b"),
        ("Analysis", "review", "#8b5cf6"),
        ("Done", "done", "#22c55e"),
    ],
    "content": [
        ("Ideas", "backlog", "#64748b"),
        ("Drafting", "in_progress", "#f59e0b"),
        ("Review", "review", "#8b5cf6"),
        ("Published", "done", "#22c55e"),
    ],
    "personal": [
        ("Backlog", "backlog", "#64748b"),
        ("Next", "todo", "#3b82f6"),
        ("Doing", "in_progress", "#f59e0b"),
        ("Waiting", "review", "#8b5cf6"),
        ("Done", "done", "#22c55e"),
    ],
    "coursework": [
        ("Syllabus", "backlog", "#64748b"),
        ("This Week", "todo", "#3b82f6"),
        ("Learning", "in_progress", "#f59e0b"),
        ("Needs Review", "review", "#8b5cf6"),
        ("Mastered", "done", "#22c55e"),
    ],
    "gtm": [
        ("Ideas", "backlog", "#64748b"),
        ("Qualified", "todo", "#3b82f6"),
        ("Executing", "in_progress", "#f59e0b"),
        ("Waiting / Review", "review", "#8b5cf6"),
        ("Shipped", "done", "#22c55e"),
    ],
}


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    key: Optional[str] = Field(default=None, max_length=12)
    description: str = Field(default="", max_length=20_000)
    template: str = "general"
    color: str = Field(default="#5b8abf", max_length=16)
    icon: Optional[str] = Field(default=None, max_length=32)


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    key: Optional[str] = Field(default=None, max_length=12)
    description: Optional[str] = Field(default=None, max_length=20_000)
    color: Optional[str] = Field(default=None, max_length=16)
    icon: Optional[str] = Field(default=None, max_length=32)
    version: int = Field(ge=1)


class VersionRequest(BaseModel):
    version: int = Field(ge=1)


class MemberCreate(BaseModel):
    username: str = Field(min_length=1, max_length=160)
    role: Literal["editor", "viewer"] = "viewer"


class MemberUpdate(BaseModel):
    role: Literal["editor", "viewer"]


class ProjectTransfer(BaseModel):
    username: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)


class RemoteInvitationCreate(BaseModel):
    handle: str = Field(min_length=1, max_length=32)
    role: Literal["editor", "viewer"] = "viewer"


class RemoteGrantUpdate(BaseModel):
    role: Literal["editor", "viewer"]
    version: int = Field(ge=1)


class StageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    category: str = "todo"
    color: str = Field(default="#64748b", max_length=16)
    position: Optional[int] = Field(default=None, ge=0)
    wip_limit: Optional[int] = Field(default=None, ge=1, le=10_000)


class StageUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    category: Optional[str] = None
    color: Optional[str] = Field(default=None, max_length=16)
    position: Optional[int] = Field(default=None, ge=0)
    wip_limit: Optional[int] = Field(default=None, ge=1, le=10_000)
    clear_wip_limit: bool = False


class StageOrder(BaseModel):
    stage_ids: list[str]


class WorkItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=100_000)
    stage_id: Optional[str] = None
    item_type: str = "task"
    priority: str = "medium"
    labels: list[str] = Field(default_factory=list)
    assignee: Optional[str] = None
    start_date: Optional[str] = None
    due_date: Optional[str] = None
    estimate_minutes: int = Field(default=0, ge=0, le=10_000_000)
    logged_minutes: int = Field(default=0, ge=0, le=10_000_000)
    parent_id: Optional[str] = None
    blocked_by_id: Optional[str] = None
    position: Optional[int] = Field(default=None, ge=0)


class WorkItemUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=240)
    description: Optional[str] = Field(default=None, max_length=100_000)
    item_type: Optional[str] = None
    priority: Optional[str] = None
    labels: Optional[list[str]] = None
    assignee: Optional[str] = None
    clear_assignee: bool = False
    start_date: Optional[str] = None
    clear_start_date: bool = False
    due_date: Optional[str] = None
    clear_due_date: bool = False
    estimate_minutes: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    logged_minutes: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    parent_id: Optional[str] = None
    clear_parent: bool = False
    blocked_by_id: Optional[str] = None
    clear_blocked_by: bool = False
    version: int


class WorkItemMove(BaseModel):
    stage_id: str
    position: Optional[int] = Field(default=None, ge=0)
    version: int


class WorkItemOrder(BaseModel):
    stage_id: str
    item_ids: list[str]
    versions: dict[str, int]


class ChecklistCreate(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    position: Optional[int] = Field(default=None, ge=0)


class ChecklistUpdate(BaseModel):
    text: Optional[str] = Field(default=None, min_length=1, max_length=500)
    done: Optional[bool] = None
    position: Optional[int] = Field(default=None, ge=0)


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=50_000)


class CommentUpdate(BaseModel):
    body: str = Field(min_length=1, max_length=50_000)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() + "Z" if value else None


def remote_grant_principal(grant_id: str) -> str:
    return f"{REMOTE_GRANT_PRINCIPAL_PREFIX}{str(grant_id or '').strip().lower()}"


def remote_instance_principal(guest_id: int) -> str:
    return f"{REMOTE_INSTANCE_PRINCIPAL_PREFIX}{int(guest_id)}"


def set_remote_project_context(
    request: Request,
    guest_id: int,
    grant_id: Optional[str] = None,
) -> str:
    """Mark a request as explicitly authenticated by the Home Link gateway.

    This helper is the sole bridge between the bearer dependency and Projects.
    Merely supplying a username-like string never enables the remote path.
    Project authorization still rechecks the active grant in the transaction.
    """

    try:
        normalized_guest_id = int(guest_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(401, "Invalid remote project identity") from exc
    if normalized_guest_id < 1:
        raise HTTPException(401, "Invalid remote project identity")
    normalized_grant_id = str(grant_id or "").strip().lower() or None
    if normalized_grant_id and len(normalized_grant_id) > 36:
        raise HTTPException(401, "Invalid remote project grant")
    principal = (
        remote_grant_principal(normalized_grant_id)
        if normalized_grant_id
        else remote_instance_principal(normalized_guest_id)
    )
    request.state.project_remote = True
    request.state.project_remote_guest_id = normalized_guest_id
    request.state.project_remote_grant_id = normalized_grant_id
    request.state.project_remote_principal = principal
    request.state.current_user = principal
    _PROJECT_REMOTE_CONTEXT.set({
        "guest_id": normalized_guest_id,
        "grant_id": normalized_grant_id,
        "grant_ids": {normalized_grant_id} if normalized_grant_id else set(),
    })
    return principal


def _remote_context() -> Optional[dict[str, Any]]:
    context = _PROJECT_REMOTE_CONTEXT.get()
    return context if isinstance(context, dict) else None


def _remote_guest_id(actor: Optional[str] = None) -> Optional[int]:
    context = _remote_context()
    if context is None:
        return None
    try:
        return int(context.get("guest_id"))
    except (TypeError, ValueError):
        return None


def _remote_grant_id(actor: Optional[str] = None) -> Optional[str]:
    context = _remote_context()
    if context is None:
        return None
    context_id = str(context.get("grant_id") or "").strip().lower()
    if context_id:
        return context_id
    value = str(actor or "").strip().lower()
    if value.startswith(REMOTE_GRANT_PRINCIPAL_PREFIX):
        candidate = value[len(REMOTE_GRANT_PRINCIPAL_PREFIX):] or None
        if candidate in set(context.get("grant_ids") or set()):
            return candidate
    return None


def _is_remote_actor(actor: Optional[str] = None) -> bool:
    return _remote_context() is not None


def _remember_remote_grant(grant: ProjectRemoteGrant) -> None:
    context = _remote_context()
    if context is None:
        return
    grant_ids = set(context.get("grant_ids") or set())
    grant_ids.add(str(grant.id).lower())
    updated = {**context, "grant_ids": grant_ids}
    _PROJECT_REMOTE_CONTEXT.set(updated)


def _remote_identity(value: Optional[str]) -> Optional[str]:
    if value is None or not _remote_context():
        return value
    normalized = str(value).strip().lower()
    if normalized.startswith(REMOTE_GRANT_PRINCIPAL_PREFIX):
        grant_id = normalized[len(REMOTE_GRANT_PRINCIPAL_PREFIX):]
        if grant_id in set((_remote_context() or {}).get("grant_ids") or set()):
            return "me"
        return normalized
    if normalized.startswith(REMOTE_INSTANCE_PRINCIPAL_PREFIX):
        return "me"
    return "instance"


def _canonical_actor(actor: str) -> str:
    if _is_remote_actor(actor):
        grant_id = _remote_grant_id(actor)
        if grant_id:
            return remote_grant_principal(grant_id)
    return str(actor or "").strip().lower()


def _response_actor() -> str:
    context = _PROJECT_AUTH_CONTEXT.get()
    actor = context[1] if context else ""
    canonical = _canonical_actor(actor)
    return str(_remote_identity(canonical) or canonical)


def _actor(request: Request) -> str:
    if bool(getattr(request.state, "project_remote", False)):
        actor = set_remote_project_context(
            request,
            getattr(request.state, "project_remote_guest_id", None),
            getattr(request.state, "project_remote_grant_id", None),
        )
        _PROJECT_AUTH_CONTEXT.set((request, actor))
        return actor

    _PROJECT_REMOTE_CONTEXT.set(None)
    # require_user is the security gate. It returns "" only for explicitly
    # admitted auth-disabled/single-user modes, where a stable non-null owner is
    # needed for unique project keys and deterministic filtering.
    user = require_user(request)
    if user:
        actor = str(user).strip().lower()
    else:
        auth_manager = getattr(request.app.state, "auth_manager", None)
        configured_users = getattr(auth_manager, "users", None)
        if isinstance(configured_users, dict):
            normalized = {
                str(name).strip().lower(): data
                for name, data in configured_users.items()
                if str(name).strip()
            }
            admins = [
                name for name, data in normalized.items()
                if isinstance(data, dict) and data.get("is_admin")
            ]
            if EXPLICIT_PROJECT_FALLBACK_OWNER:
                if EXPLICIT_PROJECT_FALLBACK_OWNER not in normalized:
                    raise HTTPException(409, "RESTIA_FALLBACK_OWNER must name an existing local profile")
                actor = EXPLICIT_PROJECT_FALLBACK_OWNER
            elif len(normalized) == 1:
                actor = next(iter(normalized))
            elif len(admins) == 1:
                actor = admins[0]
            elif normalized:
                raise HTTPException(
                    409,
                    "Project ownership is ambiguous while authentication is disabled. "
                    "Set RESTIA_FALLBACK_OWNER to a local profile.",
                )
            elif bool(getattr(auth_manager, "is_configured", False)):
                raise HTTPException(503, "Local profile ownership could not be resolved")
            else:
                actor = FIRST_RUN_PROJECT_OWNER
        else:
            actor = str(effective_owner(request) or FALLBACK_PROJECT_OWNER).strip().lower()

    _PROJECT_AUTH_CONTEXT.set((request, actor))

    # A project may be created during localhost first-run before an account
    # exists. Once there is exactly one configured user/admin, claim those
    # sentinel-owned projects for that person so enabling auth does not make
    # the operator's pre-setup work disappear. Never claim in a multi-user
    # configuration.
    auth_manager = getattr(request.app.state, "auth_manager", None)
    auth_users = getattr(auth_manager, "users", None)
    normalized_auth_users = (
        {str(name).strip().lower() for name in auth_users if str(name).strip()}
        if isinstance(auth_users, dict)
        else set()
    )
    sentinel_is_real_profile = FIRST_RUN_PROJECT_OWNER in normalized_auth_users
    claim_checks = set(getattr(auth_manager, "_project_first_run_claim_checks", set()) or set())
    if (
        actor != FIRST_RUN_PROJECT_OWNER
        and not sentinel_is_real_profile
        and normalized_auth_users == {actor}
        and actor not in claim_checks
    ):
        # Avoid taking SQLite's global writer reservation on every read after
        # first-run migration is complete. The write transaction rechecks so a
        # concurrent setup/claim remains safe.
        with _db_session(write=False) as db:
            has_sentinel_projects = db.query(Project.id).filter(
                Project.owner == FIRST_RUN_PROJECT_OWNER
            ).first() is not None
        if has_sentinel_projects:
            with _db_session() as db:
                rows = db.query(Project).filter(Project.owner == FIRST_RUN_PROJECT_OWNER).order_by(
                    Project.created_at.asc(), Project.id.asc()
                ).all()
                for project in rows:
                    if db.query(Project.id).filter(
                        Project.owner == actor,
                        Project.key == project.key,
                        Project.id != project.id,
                    ).first():
                        project.key = _available_key(db, actor, None, project.name)
                    project.owner = actor
                    project.updated_at = utcnow_naive()
                    # SessionLocal disables autoflush. Materialize each claimed
                    # key so the next conflict query cannot reuse it.
                    db.flush()
        # Normal requests now resolve directly to the configured profile, so
        # legitimate code cannot create more sentinel-owned rows in this
        # process. A restart/manual repair naturally runs the probe again.
        claim_checks.add(actor)
        setattr(auth_manager, "_project_first_run_claim_checks", claim_checks)
    return actor


@contextmanager
def _db_session(*, write: bool = True):
    db = SessionLocal()
    auth_lock = None
    try:
        context = _PROJECT_AUTH_CONTEXT.get() if write else None
        if context:
            request, actor = context
            remote_request = bool(getattr(request.state, "project_remote", False))
            auth_manager = getattr(request.app.state, "auth_manager", None)
            candidate_lock = getattr(auth_manager, "_config_lock", None)
            if candidate_lock is not None and not remote_request:
                candidate_lock.acquire()
                auth_lock = candidate_lock
            if not remote_request and getattr(auth_manager, "_identity_migrations", None):
                raise HTTPException(
                    409,
                    "A profile identity migration is in progress. Retry this project change shortly.",
                )
            configured_users = getattr(auth_manager, "users", None)
            if not remote_request and isinstance(configured_users, dict) and configured_users:
                configured_names = {
                    str(name).strip().lower() for name in configured_users if str(name).strip()
                }
                if actor not in configured_names:
                    # Revalidate every resolved identity, including
                    # auth-disabled/localhost-bypass requests that have no
                    # request.state.current_user. A profile rename/delete may
                    # have completed between actor resolution and this write.
                    raise HTTPException(
                        409,
                        "This profile changed or was removed. Refresh and sign in again.",
                    )
        if write and db.get_bind().dialect.name == "sqlite":
            # Reserve SQLite's single writer slot before any authorization or
            # WIP reads establish a stale deferred-transaction snapshot. This
            # makes count-then-move/create constraints deterministic under
            # concurrent requests instead of allowing both writers through.
            db.execute(text("BEGIN IMMEDIATE"))
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        if auth_lock is not None:
            auth_lock.release()


def _clean_text(value: object, *, field: str, max_length: int, required: bool = False) -> str:
    text_value = str(value or "").strip()
    if required and not text_value:
        raise HTTPException(400, f"{field} is required")
    if len(text_value) > max_length:
        raise HTTPException(400, f"{field} is too long")
    return text_value


def _normalize_key(value: object) -> str:
    key = re.sub(r"[^A-Za-z0-9]", "", str(value or "").upper())
    if not PROJECT_KEY_RE.fullmatch(key):
        raise HTTPException(400, "Project key must be 2-12 uppercase letters or numbers and start with a letter")
    return key


def _normalize_color(value: object) -> str:
    color = str(value or "").strip()
    if not PROJECT_COLOR_RE.fullmatch(color):
        raise HTTPException(400, "Color must be a #RGB, #RRGGBB, or #RRGGBBAA value")
    return color.lower()


def _suggest_key(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name.upper())
    if len(words) > 1:
        candidate = "".join(word[0] for word in words)[:12]
    else:
        candidate = (words[0] if words else "PR")[:12]
    candidate = re.sub(r"^[^A-Z]+", "", candidate)
    if len(candidate) < 2:
        candidate = (candidate + "PR")[:2]
    return candidate


def _available_key(db, owner: str, requested: Optional[str], name: str) -> str:
    if requested:
        key = _normalize_key(requested)
        exists = db.query(Project.id).filter(Project.owner == owner, Project.key == key).first()
        if exists:
            raise HTTPException(409, "Project key already exists")
        return key
    base = _suggest_key(name)
    key = base
    suffix = 2
    while db.query(Project.id).filter(Project.owner == owner, Project.key == key).first():
        suffix_text = str(suffix)
        key = base[: 12 - len(suffix_text)] + suffix_text
        suffix += 1
    return key


def _normalize_labels(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise HTTPException(400, "labels must be a list")
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        if len(value) > 40:
            raise HTTPException(400, "Labels must be 40 characters or fewer")
        lowered = value.casefold()
        if lowered not in seen:
            seen.add(lowered)
            out.append(value)
        if len(out) > 30:
            raise HTTPException(400, "A work item can have at most 30 labels")
    return out


def _validate_date(value: Optional[str], field: str) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} must be YYYY-MM-DD")


def _remote_handle_blocked(db, project_owner: str, handle: str) -> bool:
    normalized_owner = str(project_owner or "").strip().lower()
    normalized_handle = str(handle or "").strip().lower()
    if not normalized_owner or not normalized_handle:
        return True
    return db.query(RemoteBlock.id).filter(
        func.lower(RemoteBlock.local_user) == normalized_owner,
        func.lower(RemoteBlock.handle) == normalized_handle,
    ).first() is not None


def _remote_guest_blocked(db, project: Project, guest_id: int) -> bool:
    handle = db.query(LinkGuest.handle).filter(
        LinkGuest.id == int(guest_id),
        LinkGuest.status == "approved",
    ).scalar()
    return not handle or _remote_handle_blocked(db, project.owner, handle)


def _project_role(db, project: Project, actor: str) -> Optional[str]:
    if _is_remote_actor(actor):
        guest_id = _remote_guest_id(actor)
        grant_id = _remote_grant_id(actor)
        if guest_id is None:
            return None
        if _remote_guest_blocked(db, project, guest_id):
            return None
        query = db.query(ProjectRemoteGrant).join(
            LinkGuest, LinkGuest.id == ProjectRemoteGrant.guest_id
        ).filter(
            ProjectRemoteGrant.project_id == project.id,
            ProjectRemoteGrant.guest_id == guest_id,
            ProjectRemoteGrant.status == "active",
            LinkGuest.status == "approved",
        )
        if grant_id:
            query = query.filter(ProjectRemoteGrant.id == grant_id)
        grant = query.first()
        if not grant or grant.role not in REMOTE_GRANT_ROLES:
            return None
        _remember_remote_grant(grant)
        return grant.role
    if project.owner == actor:
        return "owner"
    member = db.query(ProjectMember).filter(
        ProjectMember.project_id == project.id,
        func.lower(ProjectMember.username) == actor,
    ).first()
    return member.role if member else None


def _get_project(db, project_id: str, actor: str, *, minimum: str = "viewer", writable: bool = False) -> tuple[Project, str]:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    role = _project_role(db, project, actor)
    if role is None:
        raise HTTPException(404, "Project not found")
    if PROJECT_ROLES.get(role, 0) < PROJECT_ROLES[minimum]:
        raise HTTPException(403, "Project role does not allow this action")
    if writable and project.archived:
        raise HTTPException(409, "Restore the project before changing it")
    return project, role


def _get_stage(db, project_id: str, stage_id: str) -> ProjectStage:
    stage = db.query(ProjectStage).filter(
        ProjectStage.id == stage_id,
        ProjectStage.project_id == project_id,
    ).first()
    if not stage:
        raise HTTPException(404, "Stage not found")
    return stage


def _get_item(db, project_id: str, item_id: str, *, writable: bool = False) -> ProjectWorkItem:
    item = db.query(ProjectWorkItem).filter(
        ProjectWorkItem.id == item_id,
        ProjectWorkItem.project_id == project_id,
    ).first()
    if not item:
        raise HTTPException(404, "Work item not found")
    if writable and item.archived:
        raise HTTPException(409, "Restore the work item before changing it")
    return item


def _get_remote_grant(db, project_id: str, grant_id: str) -> ProjectRemoteGrant:
    grant = db.query(ProjectRemoteGrant).filter(
        ProjectRemoteGrant.id == str(grant_id or "").strip().lower(),
        ProjectRemoteGrant.project_id == project_id,
    ).first()
    if not grant:
        raise HTTPException(404, "Remote project grant not found")
    return grant


def _prune_remote_grant_tombstones(db, project_id: str) -> int:
    """Bound detached audit rows while preserving the newest history."""
    stale_ids = [
        row_id
        for (row_id,) in db.query(ProjectRemoteGrant.id)
        .filter(
            ProjectRemoteGrant.project_id == project_id,
            ProjectRemoteGrant.guest_id.is_(None),
            ProjectRemoteGrant.status.in_(("declined", "revoked")),
        )
        .order_by(
            ProjectRemoteGrant.revoked_at.desc(),
            ProjectRemoteGrant.invited_at.desc(),
            ProjectRemoteGrant.id.desc(),
        )
        .offset(REMOTE_GRANT_TOMBSTONE_LIMIT)
        .all()
    ]
    if not stale_ids:
        return 0
    return int(
        db.query(ProjectRemoteGrant)
        .filter(ProjectRemoteGrant.id.in_(stale_ids))
        .delete(synchronize_session=False)
        or 0
    )


def _check_version(row: Any, expected: Optional[int]) -> None:
    if expected is not None and int(row.version or 0) != int(expected):
        raise HTTPException(409, "This item changed in another tab. Refresh and retry.")


def _claim_version(db, row: Any, expected: int) -> None:
    """Atomically reserve a version for this transaction.

    A Python read/check alone permits two concurrent writers to observe the
    same value. The conditional UPDATE is serialized by SQLite (and is a
    compare-and-swap on other SQL databases), so exactly one writer advances.
    """

    model = type(row)
    current = int(expected)
    result = db.execute(
        update(model)
        .where(model.id == row.id, model.version == current)
        .values(version=current + 1)
    )
    if result.rowcount != 1:
        raise HTTPException(409, "This item changed in another tab. Refresh and retry.")
    row.version = current + 1


def _activity(
    db,
    project_id: str,
    actor: str,
    event_type: str,
    summary: str,
    *,
    work_item_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> ProjectActivity:
    actor = _canonical_actor(actor)
    # Flush the business mutation before taking the activity-stream mutex. This
    # keeps the lock order consistent: domain rows first, retention row last.
    db.flush()
    _lock_named_quota_keys(
        db,
        _activity_quota_lock_key(project_id),
        project_id=project_id,
    )
    row = ProjectActivity(
        id=str(uuid.uuid4()),
        project_id=project_id,
        work_item_id=work_item_id,
        actor=actor,
        event_type=event_type,
        summary=_clean_text(summary, field="Activity summary", max_length=500),
        payload=payload or {},
        created_at=utcnow_naive(),
    )
    db.add(row)
    db.flush([row])

    # Keep the newest bounded window without loading a potentially huge list
    # of activity IDs. The offset row and everything older is discarded.
    overflow = (
        db.query(ProjectActivity.created_at, ProjectActivity.id)
        .filter(ProjectActivity.project_id == project_id)
        .order_by(ProjectActivity.created_at.desc(), ProjectActivity.id.desc())
        .offset(PROJECT_MAX_ACTIVITY_PER_PROJECT)
        .first()
    )
    if overflow:
        db.query(ProjectActivity).filter(
            ProjectActivity.project_id == project_id,
            or_(
                ProjectActivity.created_at < overflow.created_at,
                and_(
                    ProjectActivity.created_at == overflow.created_at,
                    ProjectActivity.id <= overflow.id,
                ),
            ),
        ).delete(synchronize_session=False)
    return row


def _project_dict(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "owner": _remote_identity(project.owner),
        "key": project.key,
        "name": project.name,
        "description": project.description or "",
        "template": project.template,
        "color": project.color,
        "icon": project.icon,
        "archived": bool(project.archived),
        "version": int(project.version or 1),
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
    }


def _member_dict(member: ProjectMember) -> dict[str, Any]:
    return {
        "username": _remote_identity(member.username),
        "kind": "profile",
        "role": member.role,
        "added_by": _remote_identity(member.added_by),
        "joined_at": _iso(member.joined_at),
    }


def _remote_grant_dict(row: ProjectRemoteGrant) -> dict[str, Any]:
    remote = bool(_remote_context())
    principal = remote_grant_principal(row.id)
    username = _remote_identity(principal) if remote else principal
    return {
        "id": row.id,
        "grant_id": row.id,
        "username": username,
        "kind": "instance",
        "handle": username if remote else row.handle_snapshot,
        "name": username if remote else row.handle_snapshot,
        "display_name": username if remote else row.handle_snapshot,
        "role": row.role,
        "status": row.status,
        "version": int(row.version or 1),
        "invited_by": _remote_identity(row.invited_by),
        "invited_at": _iso(row.invited_at),
        "responded_at": _iso(row.responded_at),
        "revoked_at": _iso(row.revoked_at),
        "joined_at": _iso(row.responded_at or row.invited_at),
    }


def _stage_dict(stage: ProjectStage, *, item_count: Optional[int] = None) -> dict[str, Any]:
    out = {
        "id": stage.id,
        "project_id": stage.project_id,
        "name": stage.name,
        "category": stage.category,
        "color": stage.color,
        "position": int(stage.position or 0),
        "wip_limit": stage.wip_limit,
        "created_at": _iso(stage.created_at),
        "updated_at": _iso(stage.updated_at),
    }
    if item_count is not None:
        out["item_count"] = item_count
        out["wip_exceeded"] = bool(stage.wip_limit and item_count > stage.wip_limit)
    return out


def _item_dict(item: ProjectWorkItem, project_key: str) -> dict[str, Any]:
    return {
        "id": item.id,
        "project_id": item.project_id,
        "key": f"{project_key}-{item.item_number}",
        "item_number": item.item_number,
        "stage_id": item.stage_id,
        "item_type": item.item_type,
        "title": item.title,
        "description": item.description or "",
        "priority": item.priority,
        "labels": list(item.labels or []),
        "reporter": _remote_identity(item.reporter),
        "assignee": _remote_identity(item.assignee),
        "start_date": item.start_date,
        "due_date": item.due_date,
        "estimate_minutes": int(item.estimate_minutes or 0),
        "logged_minutes": int(item.logged_minutes or 0),
        "parent_id": item.parent_id,
        "blocked_by_id": item.blocked_by_id,
        "position": int(item.position or 0),
        "archived": bool(item.archived),
        "completed_at": _iso(item.completed_at),
        "version": int(item.version or 1),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def _item_card_dict(item: ProjectWorkItem, project_key: str) -> dict[str, Any]:
    """Serialize the board/list shape without loading large task bodies.

    Descriptions are fetched only when the detail drawer opens. This keeps a
    full 2,000-card board bounded even when tasks contain long specifications.
    """

    return {
        "id": item.id,
        "project_id": item.project_id,
        "key": f"{project_key}-{item.item_number}",
        "item_number": item.item_number,
        "stage_id": item.stage_id,
        "item_type": item.item_type,
        "title": item.title,
        "priority": item.priority,
        "labels": list(item.labels or []),
        "reporter": _remote_identity(item.reporter),
        "assignee": _remote_identity(item.assignee),
        "start_date": item.start_date,
        "due_date": item.due_date,
        "estimate_minutes": int(item.estimate_minutes or 0),
        "logged_minutes": int(item.logged_minutes or 0),
        "parent_id": item.parent_id,
        "blocked_by_id": item.blocked_by_id,
        "position": int(item.position or 0),
        "archived": bool(item.archived),
        "completed_at": _iso(item.completed_at),
        "version": int(item.version or 1),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def _checklist_dict(row: ProjectChecklistItem) -> dict[str, Any]:
    return {
        "id": row.id,
        "work_item_id": row.work_item_id,
        "text": row.text,
        "done": bool(row.is_done),
        "position": int(row.position or 0),
        "created_by": _remote_identity(row.created_by),
        "completed_at": _iso(row.completed_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _comment_dict(row: ProjectComment) -> dict[str, Any]:
    return {
        "id": row.id,
        "work_item_id": row.work_item_id,
        "author": _remote_identity(row.author),
        "body": row.body,
        "edited_at": _iso(row.edited_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _attachment_dict(row: ProjectAttachment) -> dict[str, Any]:
    return {
        "id": row.id,
        "work_item_id": row.work_item_id,
        "uploader": _remote_identity(row.uploader),
        "kind": row.kind,
        "description": row.description or "",
        "name": row.original_name,
        "mime": row.mime,
        "size": int(row.size or 0),
        "sha256": row.sha256,
        "status": row.status,
        "supersedes_id": row.supersedes_id,
        "created_at": _iso(row.created_at),
        "download_url": (
            f"/api/link/projects/attachments/{row.id}/download"
            if _remote_context()
            else f"/api/projects/attachments/{row.id}/download"
        ),
    }


def _verified_attachment_snapshot(
    path: Path,
    expected_size: object,
    expected_sha256: object,
) -> Optional[tuple[BinaryIO, int]]:
    """Return a verified immutable snapshot without retaining the storage path.

    Download responses must not verify one pathname and then reopen it later:
    an atomic replacement in between would serve bytes that were never hashed.
    The spooled snapshot also keeps large attachments off the Python heap.
    """

    try:
        size = int(expected_size)
    except (TypeError, ValueError):
        return None
    digest_text = str(expected_sha256 or "").strip().lower()
    if size < 1 or not re.fullmatch(r"[0-9a-f]{64}", digest_text):
        return None

    snapshot: Optional[BinaryIO] = None
    try:
        snapshot = tempfile.SpooledTemporaryFile(
            max_size=ATTACHMENT_INTEGRITY_CHUNK_BYTES,
            mode="w+b",
        )
        digest = hashlib.sha256()
        bytes_read = 0
        with path.open("rb") as handle:
            if os.fstat(handle.fileno()).st_size != size:
                snapshot.close()
                return None
            while True:
                chunk = handle.read(ATTACHMENT_INTEGRITY_CHUNK_BYTES)
                if not chunk:
                    break
                bytes_read += len(chunk)
                if bytes_read > size:
                    snapshot.close()
                    return None
                digest.update(chunk)
                snapshot.write(chunk)
            if os.fstat(handle.fileno()).st_size != size:
                snapshot.close()
                return None
    except OSError:
        if snapshot is not None:
            snapshot.close()
        return None
    if bytes_read != size or not hmac.compare_digest(digest.hexdigest(), digest_text):
        snapshot.close()
        return None
    snapshot.seek(0)
    return snapshot, size


class _AttachmentDownloadGate:
    """Fail-fast process-local cap for verified attachment snapshots.

    Restia's default launch is one worker. Multi-worker operators get the same
    bounded allowance in each worker instead of one unbounded global pool.
    """

    def __init__(self, total_limit: int, principal_limit: int):
        self.total_limit = int(total_limit)
        self.principal_limit = int(principal_limit)
        self._lock = Lock()
        self._active = 0
        self._by_principal: dict[str, int] = {}

    def try_acquire(self, principal: str) -> Optional[Callable[[], None]]:
        key = str(principal or "unknown")[:200]
        with self._lock:
            principal_active = self._by_principal.get(key, 0)
            if (
                self._active >= self.total_limit
                or principal_active >= self.principal_limit
            ):
                return None
            self._active += 1
            self._by_principal[key] = principal_active + 1

        released = False

        def release() -> None:
            nonlocal released
            with self._lock:
                if released:
                    return
                released = True
                self._active = max(0, self._active - 1)
                remaining = self._by_principal.get(key, 0) - 1
                if remaining > 0:
                    self._by_principal[key] = remaining
                else:
                    self._by_principal.pop(key, None)

        return release

    def active_counts(self) -> tuple[int, dict[str, int]]:
        """Return a test/diagnostic snapshot without exposing mutable state."""

        with self._lock:
            return self._active, dict(self._by_principal)


_attachment_download_gate = _AttachmentDownloadGate(
    PROJECT_ATTACHMENT_DOWNLOAD_CONCURRENCY,
    PROJECT_ATTACHMENT_DOWNLOAD_PER_PRINCIPAL,
)


def _attachment_download_principal(actor: str) -> str:
    guest_id = _remote_guest_id()
    if guest_id is not None:
        return f"guest:{guest_id}"
    return f"profile:{_canonical_actor(actor)}"


async def _stream_verified_attachment(snapshot: BinaryIO):
    while True:
        chunk = await asyncio.to_thread(
            snapshot.read,
            ATTACHMENT_INTEGRITY_CHUNK_BYTES,
        )
        if not chunk:
            break
        yield chunk


class _VerifiedAttachmentResponse(StreamingResponse):
    """Own and close a verified snapshot even when the ASGI send is aborted."""

    def __init__(
        self,
        snapshot: BinaryIO,
        *args,
        on_close: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        self._verified_snapshot = snapshot
        self._verified_on_close = on_close
        super().__init__(_stream_verified_attachment(snapshot), *args, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # SpooledTemporaryFile.close() only releases memory or a local file
            # descriptor; doing it here guarantees cleanup even when Starlette
            # exits the body iterator because the client disconnected.
            try:
                self._verified_snapshot.close()
            finally:
                if self._verified_on_close is not None:
                    on_close, self._verified_on_close = self._verified_on_close, None
                    on_close()


async def _prepare_verified_attachment_snapshot(
    path: Path,
    expected_size: object,
    expected_sha256: object,
) -> Optional[tuple[BinaryIO, int]]:
    task = asyncio.create_task(
        asyncio.to_thread(
            _verified_attachment_snapshot,
            path,
            expected_size,
            expected_sha256,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # ``to_thread`` cannot cancel work already running. Keep this request
        # (and its concurrency slot) alive until the worker stops touching the
        # spool, even under repeated cancellation, then close its late result.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if task.done():
            try:
                verified = task.result()
            except BaseException:
                verified = None
            if verified is not None:
                verified[0].close()
        raise


def _attachment_content_disposition(filename: object) -> str:
    name = str(filename or "attachment").replace("\r", "_").replace("\n", "_")
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")[:180]
    fallback = fallback or "attachment"
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(name, safe='')}"
    )


def _activity_dict(row: ProjectActivity) -> dict[str, Any]:
    remote = bool(_remote_context())
    return {
        "id": row.id,
        "project_id": row.project_id,
        "work_item_id": row.work_item_id,
        "actor": _remote_identity(row.actor),
        "event_type": row.event_type,
        "summary": (
            str(row.event_type or "project_activity").replace("_", " ").capitalize()
            if remote
            else row.summary
        ),
        "payload": {} if remote else (row.payload or {}),
        "created_at": _iso(row.created_at),
    }


def _role_members(db, project: Project) -> list[dict[str, Any]]:
    members = [
        {
            "username": _remote_identity(project.owner),
            "kind": "profile" if not _remote_context() else "instance",
            "role": "owner",
            "added_by": _remote_identity(project.owner),
            "joined_at": _iso(project.created_at),
        }
    ]
    if not _remote_context():
        members.extend(
            _member_dict(row)
            for row in db.query(ProjectMember)
            .filter(ProjectMember.project_id == project.id)
            .order_by(ProjectMember.joined_at.asc(), ProjectMember.username.asc())
            .all()
        )
    visible_statuses = ("active",) if _remote_context() else ("active", "pending")
    grants = (
        db.query(ProjectRemoteGrant)
        .filter(
            ProjectRemoteGrant.project_id == project.id,
            ProjectRemoteGrant.status.in_(visible_statuses),
        )
        .order_by(ProjectRemoteGrant.invited_at.asc(), ProjectRemoteGrant.id.asc())
        .all()
    )
    members.extend(_remote_grant_dict(row) for row in grants)
    return members


def _overview(db, project: Project) -> dict[str, Any]:
    stages = (
        db.query(ProjectStage)
        .filter(ProjectStage.project_id == project.id)
        .order_by(ProjectStage.position.asc(), ProjectStage.created_at.asc())
        .all()
    )
    items = (
        db.query(ProjectWorkItem)
        .options(defer(ProjectWorkItem.description))
        .filter(ProjectWorkItem.project_id == project.id, ProjectWorkItem.archived.is_(False))
        .all()
    )
    stage_map = {stage.id: stage for stage in stages}
    item_map = {item.id: item for item in items}
    counts = {stage.id: 0 for stage in stages}
    done = overdue = blocked = unassigned = 0
    today = date.today().isoformat()
    estimate_minutes = logged_minutes = 0
    for item in items:
        if item.stage_id in counts:
            counts[item.stage_id] += 1
        category = stage_map[item.stage_id].category if item.stage_id in stage_map else None
        is_done = category == "done"
        done += int(is_done)
        overdue += int(bool(item.due_date and item.due_date < today and not is_done))
        blocker = item_map.get(item.blocked_by_id) if item.blocked_by_id else None
        blocker_done = bool(
            blocker
            and blocker.stage_id in stage_map
            and stage_map[blocker.stage_id].category == "done"
        )
        blocked += int(bool(blocker and not blocker_done and not is_done))
        unassigned += int(not item.assignee and not is_done)
        estimate_minutes += int(item.estimate_minutes or 0)
        logged_minutes += int(item.logged_minutes or 0)

    total = len(items)
    completion_percent = round((done / total) * 100) if total else 0
    stage_counts = [
        {
            "stage_id": stage.id,
            "name": stage.name,
            "category": stage.category,
            "count": counts[stage.id],
            "wip_limit": stage.wip_limit,
            "wip_exceeded": bool(stage.wip_limit and counts[stage.id] > stage.wip_limit),
        }
        for stage in stages
    ]
    wip_breaches = sum(int(row["wip_exceeded"]) for row in stage_counts)
    if overdue or blocked:
        health = "at_risk"
    elif wip_breaches or (total and unassigned > max(2, total // 2)):
        health = "attention"
    else:
        health = "on_track"
    return {
        "total_items": total,
        "open_items": total - done,
        "done_items": done,
        "overdue_items": overdue,
        "blocked_items": blocked,
        "unassigned_items": unassigned,
        "archived_items": db.query(ProjectWorkItem)
        .filter(ProjectWorkItem.project_id == project.id, ProjectWorkItem.archived.is_(True))
        .count(),
        "completion_percent": completion_percent,
        "estimate_minutes": estimate_minutes,
        "logged_minutes": logged_minutes,
        "remaining_minutes": max(0, estimate_minutes - logged_minutes),
        "wip_breaches": wip_breaches,
        "health": health,
        "stage_counts": stage_counts,
    }


def _project_payload(db, project: Project, role: str) -> dict[str, Any]:
    stages = (
        db.query(ProjectStage)
        .filter(ProjectStage.project_id == project.id)
        .order_by(ProjectStage.position.asc(), ProjectStage.created_at.asc())
        .all()
    )
    return {
        "actor": _response_actor(),
        "project": {**_project_dict(project), "role": role},
        "stages": [_stage_dict(stage) for stage in stages],
        "members": _role_members(db, project),
        "overview": _overview(db, project),
    }


def _member_names(db, project: Project) -> set[str]:
    names = {project.owner}
    names.update(
        str(value[0]).strip().lower()
        for value in db.query(ProjectMember.username)
        .filter(
            ProjectMember.project_id == project.id,
            ProjectMember.role == "editor",
        )
        .all()
    )
    remote_editors = (
        db.query(ProjectRemoteGrant.id, LinkGuest.handle)
        .join(LinkGuest, LinkGuest.id == ProjectRemoteGrant.guest_id)
        .filter(
            ProjectRemoteGrant.project_id == project.id,
            ProjectRemoteGrant.role == "editor",
            ProjectRemoteGrant.status == "active",
            LinkGuest.status == "approved",
        )
        .all()
    )
    names.update(
        remote_grant_principal(grant_id)
        for grant_id, handle in remote_editors
        if not _remote_handle_blocked(db, project.owner, handle)
    )
    return names


def _validate_assignee(db, project: Project, username: Optional[str]) -> Optional[str]:
    if username in (None, ""):
        return None
    normalized = _clean_text(username, field="Assignee", max_length=160, required=True).lower()
    if _remote_context():
        if normalized == "me":
            grant_id = _remote_grant_id()
            if not grant_id:
                raise HTTPException(400, "Remote project grant is unavailable")
            normalized = remote_grant_principal(grant_id)
        elif normalized == "instance":
            normalized = project.owner
        elif not normalized.startswith(REMOTE_GRANT_PRINCIPAL_PREFIX):
            raise HTTPException(400, "Assignee must be an available project collaborator")
    if normalized not in _member_names(db, project):
        raise HTTPException(400, "Assignee must be the project owner or an editor")
    return normalized


def _assignee_filter_values(db, project: Project, value: str) -> tuple[str, ...]:
    normalized = _clean_text(
        value,
        field="Assignee filter",
        max_length=160,
        required=True,
    ).lower()
    if not _remote_context():
        return (normalized,)
    if normalized == "instance":
        local_profiles = {str(project.owner).strip().lower()}
        local_profiles.update(
            str(username).strip().lower()
            for (username,) in db.query(ProjectMember.username)
            .filter(ProjectMember.project_id == project.id)
            .all()
            if str(username or "").strip()
        )
        return tuple(sorted(local_profiles))
    if normalized == "me":
        grant_id = _remote_grant_id()
        if not grant_id:
            raise HTTPException(400, "Remote project grant is unavailable")
        return (remote_grant_principal(grant_id),)
    if normalized.startswith(REMOTE_GRANT_PRINCIPAL_PREFIX):
        # Opaque active remote principals are visible project collaborators;
        # raw local usernames never are. This closes an assignee-count oracle
        # against the identities redacted to ``instance`` in responses.
        if normalized in _member_names(db, project):
            return (normalized,)
    raise HTTPException(
        400,
        "Remote assignee filters must reference this Restia or a visible linked instance",
    )


def _unassign_member_work(db, project_id: str, username: str) -> int:
    rows = db.query(ProjectWorkItem).filter(
        ProjectWorkItem.project_id == project_id,
        func.lower(ProjectWorkItem.assignee) == username.strip().lower(),
    ).all()
    now = utcnow_naive()
    for row in rows:
        row.assignee = None
        row.version = int(row.version or 1) + 1
        row.updated_at = now
    return len(rows)


def _related_item(db, project_id: str, item_id: Optional[str], field: str) -> Optional[ProjectWorkItem]:
    if not item_id:
        return None
    row = db.query(ProjectWorkItem).filter(
        ProjectWorkItem.id == item_id,
        ProjectWorkItem.project_id == project_id,
    ).first()
    if not row:
        raise HTTPException(404, f"{field} work item not found")
    return row


def _assert_no_link_cycle(
    db,
    project_id: str,
    item_id: str,
    linked_id: Optional[str],
    attribute: str,
) -> None:
    cursor = linked_id
    visited: set[str] = set()
    while cursor:
        if cursor == item_id:
            raise HTTPException(400, "Work item links cannot form a cycle")
        if cursor in visited:
            raise HTTPException(400, "Existing work item links contain a cycle")
        visited.add(cursor)
        row = db.query(ProjectWorkItem).filter(
            ProjectWorkItem.id == cursor,
            ProjectWorkItem.project_id == project_id,
        ).first()
        if not row:
            raise HTTPException(404, "Linked work item not found")
        cursor = getattr(row, attribute)


def _active_stage_items(db, project_id: str, stage_id: str) -> list[ProjectWorkItem]:
    return (
        db.query(ProjectWorkItem)
        .options(defer(ProjectWorkItem.description))
        .filter(
            ProjectWorkItem.project_id == project_id,
            ProjectWorkItem.stage_id == stage_id,
            ProjectWorkItem.archived.is_(False),
        )
        .order_by(ProjectWorkItem.position.asc(), ProjectWorkItem.created_at.asc())
        .all()
    )


def _guard_wip(db, stage: ProjectStage, *, moving_item: Optional[ProjectWorkItem] = None, extra: int = 1) -> None:
    if db.get_bind().dialect.name != "sqlite":
        # Serialize admissions and position rewrites on the destination stage
        # for databases with row-level locks. SQLite is already protected by
        # BEGIN IMMEDIATE. The lock is needed even without a WIP limit because
        # concurrent moves otherwise race while normalizing card positions.
        db.execute(
            update(ProjectStage)
            .where(ProjectStage.id == stage.id)
            .values(position=ProjectStage.position)
        )
    if not stage.wip_limit:
        return
    count = db.query(ProjectWorkItem).filter(
        ProjectWorkItem.project_id == stage.project_id,
        ProjectWorkItem.stage_id == stage.id,
        ProjectWorkItem.archived.is_(False),
    ).count()
    if moving_item and moving_item.stage_id == stage.id and not moving_item.archived:
        extra = 0
    if count + extra > int(stage.wip_limit):
        raise HTTPException(409, f"{stage.name} has reached its WIP limit of {stage.wip_limit}")


def _set_item_order(stage_id: str, rows: list[ProjectWorkItem]) -> None:
    now = utcnow_naive()
    for position, row in enumerate(rows):
        row.stage_id = stage_id
        row.position = position
        row.updated_at = now


def _move_item(db, item: ProjectWorkItem, stage: ProjectStage, position: Optional[int]) -> None:
    _guard_wip(db, stage, moving_item=item)
    old_stage_id = item.stage_id
    old_stage = (
        db.query(ProjectStage).filter(ProjectStage.id == old_stage_id).first()
        if old_stage_id
        else None
    )
    if old_stage_id:
        old_rows = [row for row in _active_stage_items(db, item.project_id, old_stage_id) if row.id != item.id]
        _set_item_order(old_stage_id, old_rows)
    destination = [row for row in _active_stage_items(db, item.project_id, stage.id) if row.id != item.id]
    insert_at = len(destination) if position is None else min(position, len(destination))
    destination.insert(insert_at, item)
    _set_item_order(stage.id, destination)
    if old_stage_id != stage.id:
        if stage.category != "done":
            item.completed_at = None
        elif not old_stage or old_stage.category != "done" or not item.completed_at:
            item.completed_at = utcnow_naive()


def _normalize_stage_order(db, project_id: str) -> list[ProjectStage]:
    rows = (
        db.query(ProjectStage)
        .filter(ProjectStage.project_id == project_id)
        .order_by(ProjectStage.position.asc(), ProjectStage.created_at.asc())
        .all()
    )
    for position, row in enumerate(rows):
        row.position = position
    return rows


def _normalize_checklist_order(db, item_id: str) -> list[ProjectChecklistItem]:
    rows = (
        db.query(ProjectChecklistItem)
        .filter(ProjectChecklistItem.work_item_id == item_id)
        .order_by(ProjectChecklistItem.position.asc(), ProjectChecklistItem.created_at.asc())
        .all()
    )
    for position, row in enumerate(rows):
        row.position = position
    return rows


def _allocate_item_number(db, project_id: str) -> int:
    # This single UPDATE is atomic under SQLite's write lock; unlike a
    # read-then-write counter it cannot issue the same stable key twice.
    next_value = db.execute(
        text(
            "UPDATE projects SET next_item_number = next_item_number + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = :project_id "
            "RETURNING next_item_number"
        ),
        {"project_id": project_id},
    ).scalar_one()
    return int(next_value) - 1


def _item_detail(db, project: Project, item: ProjectWorkItem) -> dict[str, Any]:
    checklist_query = db.query(ProjectChecklistItem).filter(
        ProjectChecklistItem.work_item_id == item.id
    )
    checklist_total = checklist_query.count()
    checklist = (
        checklist_query
        .order_by(ProjectChecklistItem.position.asc(), ProjectChecklistItem.created_at.asc())
        .limit(PROJECT_MAX_CHECKLIST_ITEMS_PER_ITEM)
        .all()
    )
    comment_query = db.query(ProjectComment).filter(
        ProjectComment.work_item_id == item.id
    )
    comment_total = comment_query.count()
    # Keep detail responses bounded while showing the most useful recent
    # discussion in chronological order.
    comments = list(
        reversed(
            comment_query
            .order_by(ProjectComment.created_at.desc(), ProjectComment.id.desc())
            .limit(ITEM_DETAIL_COMMENT_LIMIT)
            .all()
        )
    )
    attachment_query = db.query(ProjectAttachment).filter(
        ProjectAttachment.work_item_id == item.id
    )
    attachment_total = attachment_query.count()
    attachments = (
        attachment_query
        .order_by(ProjectAttachment.created_at.desc())
        .limit(min(PROJECT_MAX_ATTACHMENTS_PER_ITEM, 200))
        .all()
    )
    activity = (
        db.query(ProjectActivity)
        .filter(ProjectActivity.work_item_id == item.id)
        .order_by(ProjectActivity.created_at.desc(), ProjectActivity.id.desc())
        .limit(100)
        .all()
    )
    return {
        "item": _item_dict(item, project.key),
        "checklist": [_checklist_dict(row) for row in checklist],
        "checklist_total": checklist_total,
        "checklist_truncated": checklist_total > len(checklist),
        "comments": [_comment_dict(row) for row in comments],
        "comments_total": comment_total,
        "comments_truncated": comment_total > len(comments),
        "comments_next_before": (
            f"{_iso(comments[0].created_at)}|{comments[0].id}"
            if comment_total > len(comments) and comments
            else None
        ),
        "attachments": [_attachment_dict(row) for row in attachments],
        "attachment_total": attachment_total,
        "attachments_truncated": attachment_total > len(attachments),
        "activity": [_activity_dict(row) for row in activity],
    }


def _guard_attachment_quota(
    db,
    project: Project,
    item: ProjectWorkItem,
    incoming_size: int,
) -> dict[str, int]:
    _lock_named_quota_keys(
        db,
        _GLOBAL_PROJECT_STORAGE_LOCK_KEY,
        _owner_quota_lock_key(project.owner),
    )
    _lock_project_quota(db, project)
    item_count = db.query(ProjectAttachment).filter(
        ProjectAttachment.work_item_id == item.id
    ).count()
    project_query = db.query(ProjectAttachment).join(
        ProjectWorkItem, ProjectWorkItem.id == ProjectAttachment.work_item_id
    ).filter(ProjectWorkItem.project_id == project.id)
    project_count = project_query.count()
    used_bytes = int(
        db.query(func.coalesce(func.sum(ProjectAttachment.size), 0))
        .join(ProjectWorkItem, ProjectWorkItem.id == ProjectAttachment.work_item_id)
        .filter(ProjectWorkItem.project_id == project.id)
        .scalar()
        or 0
    )
    owner_used_bytes = int(
        db.query(func.coalesce(func.sum(ProjectAttachment.size), 0))
        .select_from(ProjectAttachment)
        .join(ProjectWorkItem, ProjectWorkItem.id == ProjectAttachment.work_item_id)
        .join(Project, Project.id == ProjectWorkItem.project_id)
        .filter(Project.owner == project.owner)
        .scalar()
        or 0
    )
    global_used_bytes = int(
        db.query(func.coalesce(func.sum(ProjectAttachment.size), 0)).scalar()
        or 0
    )
    if item_count >= PROJECT_MAX_ATTACHMENTS_PER_ITEM:
        raise HTTPException(413, f"This work item has reached its {PROJECT_MAX_ATTACHMENTS_PER_ITEM}-attachment limit")
    if project_count >= PROJECT_MAX_ATTACHMENTS_PER_PROJECT:
        raise HTTPException(413, f"This project has reached its {PROJECT_MAX_ATTACHMENTS_PER_PROJECT}-attachment limit")
    if used_bytes + incoming_size > PROJECT_STORAGE_MAX_BYTES:
        raise HTTPException(
            413,
            f"Project storage quota exceeded ({format_byte_limit(used_bytes)} used of "
            f"{format_byte_limit(PROJECT_STORAGE_MAX_BYTES)})",
        )
    if owner_used_bytes + incoming_size > PROJECT_OWNER_STORAGE_MAX_BYTES:
        raise HTTPException(
            413,
            f"Profile project-storage quota exceeded ({format_byte_limit(owner_used_bytes)} used of "
            f"{format_byte_limit(PROJECT_OWNER_STORAGE_MAX_BYTES)})",
        )
    if global_used_bytes + incoming_size > PROJECT_GLOBAL_STORAGE_MAX_BYTES:
        raise HTTPException(
            413,
            f"Restia project-storage quota exceeded ({format_byte_limit(global_used_bytes)} used of "
            f"{format_byte_limit(PROJECT_GLOBAL_STORAGE_MAX_BYTES)})",
        )
    return {
        "used_bytes": used_bytes,
        "storage_limit_bytes": PROJECT_STORAGE_MAX_BYTES,
        "owner_used_bytes": owner_used_bytes,
        "owner_storage_limit_bytes": PROJECT_OWNER_STORAGE_MAX_BYTES,
        "global_used_bytes": global_used_bytes,
        "global_storage_limit_bytes": PROJECT_GLOBAL_STORAGE_MAX_BYTES,
        "attachment_count": project_count,
    }


_GLOBAL_PROJECT_STORAGE_LOCK_KEY = "project-storage:global"


def _is_sqlite(db) -> bool:
    return db.get_bind().dialect.name == "sqlite"


def _owner_quota_lock_key(owner: str) -> str:
    return project_owner_quota_lock_key(owner)


def _activity_quota_lock_key(project_id: str) -> str:
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
    return f"activity:{digest}"


def _lock_named_quota_keys(
    db,
    *keys: str,
    project_id: Optional[str] = None,
) -> None:
    """Lock quota domains even when no qualifying ``Project`` row exists."""

    if _is_sqlite(db):
        # Project write sessions use BEGIN IMMEDIATE, which is database-wide.
        return
    for key in sorted(set(keys)):
        exists = db.query(ProjectQuotaLock.key).filter(ProjectQuotaLock.key == key).first()
        if not exists:
            try:
                with db.begin_nested():
                    db.add(ProjectQuotaLock(key=key, project_id=project_id))
                    db.flush()
            except IntegrityError:
                # A concurrent transaction won the one-time sentinel insert.
                # Its unique-key wait has completed, so lock that same row.
                pass
        db.query(ProjectQuotaLock.key).filter(
            ProjectQuotaLock.key == key
        ).with_for_update().one()


def _lock_project_quota(db, project: Project) -> None:
    if not _is_sqlite(db):
        # A real row lock serializes admission without manufacturing a write to
        # the project and works on every SQLAlchemy row-locking backend.
        db.query(Project.id).filter(Project.id == project.id).with_for_update().one()


def _guard_active_work_item_quota(db, project: Project, *, lock: bool = True) -> None:
    if lock:
        _lock_project_quota(db, project)
    active = db.query(ProjectWorkItem.id).filter(
        ProjectWorkItem.project_id == project.id,
        ProjectWorkItem.archived.is_(False),
    ).count()
    if active >= PROJECT_MAX_ACTIVE_ITEMS:
        raise HTTPException(
            409,
            f"This board has reached its {PROJECT_MAX_ACTIVE_ITEMS:,}-active-item limit; "
            "archive completed work before adding or restoring more.",
        )


def _guard_work_item_quota(db, project: Project) -> None:
    _lock_project_quota(db, project)
    total = db.query(ProjectWorkItem.id).filter(
        ProjectWorkItem.project_id == project.id
    ).count()
    if total >= PROJECT_MAX_ITEMS:
        raise HTTPException(
            409,
            f"This project has reached its {PROJECT_MAX_ITEMS:,}-work-item retention limit; "
            "delete obsolete archived work before creating more.",
        )
    _guard_active_work_item_quota(db, project, lock=False)


def _guard_child_row_quota(
    db,
    project: Project,
    model: Any,
    item_id: str,
    limit: int,
    label: str,
) -> None:
    _lock_project_quota(db, project)
    count = db.query(model).filter(model.work_item_id == item_id).count()
    if count >= limit:
        raise HTTPException(409, f"This work item has reached its {limit:,}-{label} limit")


def _accessible_projects(db, actor: str, *, include_archived: bool = False) -> list[Project]:
    if _is_remote_actor(actor):
        guest_id = _remote_guest_id(actor)
        if guest_id is None:
            return []
        guest_handle = db.query(LinkGuest.handle).filter(
            LinkGuest.id == guest_id,
            LinkGuest.status == "approved",
        ).scalar()
        if not guest_handle:
            return []
        blocked_owners = {
            str(owner).strip().lower()
            for (owner,) in db.query(RemoteBlock.local_user)
            .filter(func.lower(RemoteBlock.handle) == str(guest_handle).strip().lower())
            .all()
            if str(owner or "").strip()
        }
        grant_query = (
            db.query(ProjectRemoteGrant)
            .join(LinkGuest, LinkGuest.id == ProjectRemoteGrant.guest_id)
            .join(Project, Project.id == ProjectRemoteGrant.project_id)
            .filter(
                ProjectRemoteGrant.guest_id == guest_id,
                ProjectRemoteGrant.status == "active",
                LinkGuest.status == "approved",
            )
        )
        if blocked_owners:
            grant_query = grant_query.filter(
                ~func.lower(Project.owner).in_(blocked_owners)
            )
        grants = grant_query.all()
        context = _remote_context()
        if context is not None:
            _PROJECT_REMOTE_CONTEXT.set({
                **context,
                "grant_ids": {str(row.id).lower() for row in grants},
            })
        query = db.query(Project).filter(Project.id.in_([row.project_id for row in grants]))
        if not include_archived:
            query = query.filter(Project.archived.is_(False))
        return query.order_by(Project.updated_at.desc(), Project.created_at.desc()).all()
    membership_ids = db.query(ProjectMember.project_id).filter(
        func.lower(ProjectMember.username) == actor
    )
    query = db.query(Project).filter(
        or_(Project.owner == actor, Project.id.in_(membership_ids))
    )
    if not include_archived:
        query = query.filter(Project.archived.is_(False))
    return query.order_by(Project.updated_at.desc(), Project.created_at.desc()).all()


def setup_project_routes(
    file_store: Optional[ProjectFileStore] = None,
    *,
    prefix: str = "/api/projects",
    remote_only: bool = False,
    dependencies: Optional[list[Any]] = None,
) -> APIRouter:
    router = APIRouter(
        prefix=prefix,
        tags=["projects"],
        dependencies=list(dependencies or []),
    )
    store = file_store or ProjectFileStore()
    # One bounded startup/setup pass repairs leftovers from a killed process
    # before normal requests begin. Reconciliation is best-effort: an audit
    # problem is loud in logs but must not make the entire Restia UI unavailable.
    if not remote_only:
        reconciliation_db = None
        try:
            reconciliation_db = SessionLocal()
            referenced_keys = (
                row[0]
                for row in reconciliation_db.query(ProjectAttachment.storage_key)
                .yield_per(1_000)
            )
            store.reconcile(referenced_keys)
        except Exception:
            logger.exception("Project attachment startup reconciliation failed")
        finally:
            if reconciliation_db is not None:
                reconciliation_db.close()

    @router.get("/templates")
    async def list_templates(request: Request):
        _actor(request)
        return {
            "templates": [
                {
                    "id": template_id,
                    "name": template_id.replace("_", " ").title(),
                    "stages": [
                        {"name": name, "category": category, "color": color}
                        for name, category, color in stages
                    ],
                }
                for template_id, stages in PROJECT_TEMPLATES.items()
            ]
        }

    def _require_local_grant_admin(actor: str) -> None:
        if remote_only or _is_remote_actor(actor):
            raise HTTPException(403, "Remote project access cannot manage linked instances")

    @router.get("/linked-instances")
    async def linked_instances(request: Request):
        actor = _actor(request)
        _require_local_grant_admin(actor)
        enabled = os.getenv("LINK_HUB_ENABLED", "false").strip().lower() == "true"
        with _db_session(write=False) as db:
            blocked_handles = {
                str(handle).strip().lower()
                for (handle,) in db.query(RemoteBlock.handle)
                .filter(func.lower(RemoteBlock.local_user) == actor)
                .all()
                if str(handle or "").strip()
            }
            query = db.query(LinkGuest).filter(LinkGuest.status == "approved")
            if blocked_handles:
                query = query.filter(~func.lower(LinkGuest.handle).in_(blocked_handles))
            rows = (
                query
                .order_by(LinkGuest.handle.asc(), LinkGuest.id.asc())
                .all()
            )
            instances = [
                {
                    "id": int(row.id),
                    "handle": row.handle,
                    "status": row.status,
                    "last_seen": _iso(row.last_seen),
                }
                for row in rows
            ]
            return {
                "hub_enabled": enabled,
                "instances": instances,
                "handles": [row["handle"] for row in instances],
            }

    @router.post("/{project_id}/remote-invitations", status_code=201)
    async def create_remote_invitation(
        project_id: str,
        request: Request,
        body: RemoteInvitationCreate,
    ):
        actor = _actor(request)
        _require_local_grant_admin(actor)
        handle = _clean_text(
            body.handle, field="Home Link handle", max_length=32, required=True
        ).lower()
        with _db_session() as db:
            project, _ = _get_project(
                db, project_id, actor, minimum="owner", writable=True
            )
            guest = db.query(LinkGuest).filter(
                func.lower(LinkGuest.handle) == handle,
                LinkGuest.status == "approved",
            ).first()
            if not guest:
                raise HTTPException(404, "Approved linked instance not found")
            if _remote_handle_blocked(db, project.owner, guest.handle):
                raise HTTPException(
                    409,
                    "Unblock this linked instance before inviting it to the project",
                )
            # Serialize invitation creation by project even on databases with
            # row-level locking. This covers both first insert and re-invite,
            # so concurrent requests cannot return conflicting 201 responses.
            if not _is_sqlite(db):
                db.query(Project.id).filter(
                    Project.id == project.id
                ).with_for_update().one()
            grant = db.query(ProjectRemoteGrant).filter(
                ProjectRemoteGrant.project_id == project.id,
                ProjectRemoteGrant.guest_id == guest.id,
            ).first()
            now = utcnow_naive()
            if grant:
                if grant.status not in {"declined", "revoked"}:
                    raise HTTPException(409, "This linked instance already has an invitation")
                grant.handle_snapshot = guest.handle
                grant.role = body.role
                grant.status = "pending"
                grant.invited_by = actor
                grant.invited_at = now
                grant.responded_at = None
                grant.revoked_at = None
                grant.version = int(grant.version or 1) + 1
            else:
                _prune_remote_grant_tombstones(db, project.id)
                grant = ProjectRemoteGrant(
                    id=str(uuid.uuid4()),
                    project_id=project.id,
                    guest_id=guest.id,
                    handle_snapshot=guest.handle,
                    role=body.role,
                    status="pending",
                    invited_by=actor,
                    invited_at=now,
                    version=1,
                )
                db.add(grant)
                try:
                    db.flush()
                except IntegrityError as exc:
                    raise HTTPException(
                        409,
                        "This linked instance already has an invitation",
                    ) from exc
            project.updated_at = now
            _activity(
                db,
                project.id,
                actor,
                "remote_instance_invited",
                f"Invited linked instance {guest.handle} as {body.role}",
                payload={"grant_id": grant.id, "role": body.role},
            )
            db.flush()
            return {"grant": _remote_grant_dict(grant)}

    @router.patch("/{project_id}/remote-grants/{grant_id}")
    async def update_remote_grant(
        project_id: str,
        grant_id: str,
        request: Request,
        body: RemoteGrantUpdate,
    ):
        actor = _actor(request)
        _require_local_grant_admin(actor)
        with _db_session() as db:
            project, _ = _get_project(
                db, project_id, actor, minimum="owner", writable=True
            )
            grant = _get_remote_grant(db, project.id, grant_id)
            if grant.status not in {"pending", "active"}:
                raise HTTPException(409, "Re-invite this linked instance before changing its role")
            _claim_version(db, grant, body.version)
            old_role = grant.role
            grant.role = body.role
            unassigned = (
                _unassign_member_work(db, project.id, remote_grant_principal(grant.id))
                if body.role == "viewer"
                else 0
            )
            project.updated_at = utcnow_naive()
            _activity(
                db,
                project.id,
                actor,
                "remote_instance_updated",
                f"Changed linked instance {grant.handle_snapshot} to {body.role}",
                payload={
                    "grant_id": grant.id,
                    "from": old_role,
                    "to": body.role,
                    "unassigned_items": unassigned,
                },
            )
            db.flush()
            return {
                "grant": _remote_grant_dict(grant),
                "unassigned_items": unassigned,
            }

    @router.delete("/{project_id}/remote-grants/{grant_id}")
    async def revoke_remote_grant(
        project_id: str,
        grant_id: str,
        request: Request,
        version: int = Query(..., ge=1),
    ):
        actor = _actor(request)
        _require_local_grant_admin(actor)
        with _db_session() as db:
            project, _ = _get_project(
                db, project_id, actor, minimum="owner", writable=True
            )
            grant = _get_remote_grant(db, project.id, grant_id)
            _claim_version(db, grant, version)
            now = utcnow_naive()
            grant.status = "revoked"
            grant.responded_at = grant.responded_at or now
            grant.revoked_at = now
            unassigned = _unassign_member_work(
                db, project.id, remote_grant_principal(grant.id)
            )
            project.updated_at = now
            _activity(
                db,
                project.id,
                actor,
                "remote_instance_revoked",
                f"Revoked linked instance {grant.handle_snapshot}",
                payload={"grant_id": grant.id, "unassigned_items": unassigned},
            )
            db.flush()
            return {
                "grant": _remote_grant_dict(grant),
                "unassigned_items": unassigned,
            }

    @router.get("/overview")
    async def global_overview(request: Request):
        actor = _actor(request)
        with _db_session(write=False) as db:
            projects = _accessible_projects(db, actor)
            project_ids = [row.id for row in projects]
            summaries = []
            totals = {
                "projects": len(projects),
                "total_items": 0,
                "open_items": 0,
                "done_items": 0,
                "overdue_items": 0,
                "blocked_items": 0,
            }
            for project in projects:
                overview = _overview(db, project)
                summaries.append(
                    {
                        "project": {
                            **_project_dict(project),
                            "role": _project_role(db, project, actor),
                        },
                        "overview": overview,
                    }
                )
                for key in ("total_items", "open_items", "done_items", "overdue_items", "blocked_items"):
                    totals[key] += int(overview[key])

            if not project_ids:
                return {
                    "totals": totals,
                    "projects": [],
                    "overdue": [],
                    "due_soon": [],
                    "blocked": [],
                    "recent_items": [],
                }

            stage_categories = {
                stage.id: stage.category
                for stage in db.query(ProjectStage)
                .filter(ProjectStage.project_id.in_(project_ids))
                .all()
            }
            project_keys = {project.id: project.key for project in projects}
            active_items = (
                db.query(ProjectWorkItem)
                .options(defer(ProjectWorkItem.description))
                .filter(
                    ProjectWorkItem.project_id.in_(project_ids),
                    ProjectWorkItem.archived.is_(False),
                )
                .order_by(ProjectWorkItem.updated_at.desc())
                .all()
            )
            today = date.today().isoformat()
            due_limit = (date.today() + timedelta(days=7)).isoformat()

            def brief(item: ProjectWorkItem) -> dict[str, Any]:
                return _item_card_dict(item, project_keys[item.project_id])

            open_items = [row for row in active_items if stage_categories.get(row.stage_id) != "done"]
            active_by_id = {row.id: row for row in active_items}

            def actively_blocked(item: ProjectWorkItem) -> bool:
                blocker = active_by_id.get(item.blocked_by_id) if item.blocked_by_id else None
                return bool(blocker and stage_categories.get(blocker.stage_id) != "done")

            return {
                "totals": totals,
                "projects": summaries,
                "overdue": [brief(row) for row in open_items if row.due_date and row.due_date < today][:25],
                "due_soon": [
                    brief(row)
                    for row in open_items
                    if row.due_date and today <= row.due_date <= due_limit
                ][:25],
                "blocked": [brief(row) for row in open_items if actively_blocked(row)][:25],
                "recent_items": [brief(row) for row in active_items[:25]],
            }

    @router.get("")
    async def list_projects(request: Request, include_archived: bool = Query(False)):
        actor = _actor(request)
        with _db_session(write=False) as db:
            projects = _accessible_projects(db, actor, include_archived=include_archived)
            return {
                "projects": [
                    {
                        **_project_dict(project),
                        "role": _project_role(db, project, actor),
                        "overview": _overview(db, project),
                    }
                    for project in projects
                ]
            }

    @router.post("", status_code=201)
    async def create_project(request: Request, body: ProjectCreate):
        actor = _actor(request)
        if remote_only or _is_remote_actor(actor):
            raise HTTPException(403, "Remote project access cannot create projects")
        name = _clean_text(body.name, field="Project name", max_length=160, required=True)
        template = str(body.template or "general").strip().lower()
        if template not in PROJECT_TEMPLATES:
            raise HTTPException(400, "Unknown project template")
        template_stages = PROJECT_TEMPLATES[template]
        if len(template_stages) > PROJECT_MAX_STAGES_PER_PROJECT:
            raise HTTPException(
                409,
                f"This template exceeds the {PROJECT_MAX_STAGES_PER_PROJECT:,}-stage project limit",
            )
        with _db_session() as db:
            _lock_named_quota_keys(db, _owner_quota_lock_key(actor))
            owned_project_count = db.query(Project.id).filter(Project.owner == actor).count()
            if owned_project_count >= PROJECT_MAX_PROJECTS_PER_OWNER:
                raise HTTPException(
                    409,
                    f"This profile has reached its {PROJECT_MAX_PROJECTS_PER_OWNER:,}-project limit; "
                    "delete obsolete archived projects before creating more.",
                )
            key = _available_key(db, actor, body.key, name)
            project = Project(
                id=str(uuid.uuid4()),
                owner=actor,
                key=key,
                name=name,
                description=_clean_text(body.description, field="Description", max_length=20_000),
                template=template,
                color=_normalize_color(body.color),
                icon=_clean_text(body.icon, field="Icon", max_length=32) or None,
                archived=False,
                next_item_number=1,
                version=1,
            )
            db.add(project)
            db.flush()
            for position, (stage_name, category, color) in enumerate(template_stages):
                db.add(
                    ProjectStage(
                        id=str(uuid.uuid4()),
                        project_id=project.id,
                        name=stage_name,
                        category=category,
                        color=color,
                        position=position,
                    )
                )
            _activity(db, project.id, actor, "project_created", f"Created project {project.key}")
            db.flush()
            return _project_payload(db, project, "owner")

    @router.get("/{project_id}")
    async def get_project(project_id: str, request: Request):
        actor = _actor(request)
        with _db_session(write=False) as db:
            project, role = _get_project(db, project_id, actor)
            return _project_payload(db, project, role)

    @router.patch("/{project_id}")
    async def update_project(project_id: str, request: Request, body: ProjectUpdate):
        actor = _actor(request)
        with _db_session() as db:
            project, role = _get_project(db, project_id, actor, minimum="owner", writable=True)
            _claim_version(db, project, body.version)
            changes: dict[str, Any] = {}
            if body.name is not None:
                changes["name"] = [project.name, _clean_text(body.name, field="Project name", max_length=160, required=True)]
                project.name = changes["name"][1]
            if body.key is not None:
                new_key = _normalize_key(body.key)
                conflict = db.query(Project.id).filter(
                    Project.owner == actor,
                    Project.key == new_key,
                    Project.id != project.id,
                ).first()
                if conflict:
                    raise HTTPException(409, "Project key already exists")
                changes["key"] = [project.key, new_key]
                project.key = new_key
            if body.description is not None:
                project.description = _clean_text(body.description, field="Description", max_length=20_000)
                changes["description"] = "updated"
            if body.color is not None:
                project.color = _normalize_color(body.color)
                changes["color"] = project.color
            if body.icon is not None:
                project.icon = _clean_text(body.icon, field="Icon", max_length=32) or None
                changes["icon"] = project.icon
            if changes:
                project.updated_at = utcnow_naive()
                _activity(db, project.id, actor, "project_updated", "Updated project settings", payload=changes)
            db.flush()
            return {"project": {**_project_dict(project), "role": role}}

    @router.post("/{project_id}/archive")
    async def archive_project(project_id: str, request: Request, body: VersionRequest):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="owner")
            _claim_version(db, project, body.version)
            if not project.archived:
                project.archived = True
                project.updated_at = utcnow_naive()
                _activity(db, project.id, actor, "project_archived", f"Archived project {project.key}")
            db.flush()
            return {"project": {**_project_dict(project), "role": "owner"}}

    @router.post("/{project_id}/restore")
    async def restore_project(project_id: str, request: Request, body: VersionRequest):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="owner")
            _claim_version(db, project, body.version)
            if project.archived:
                project.archived = False
                project.updated_at = utcnow_naive()
                _activity(db, project.id, actor, "project_restored", f"Restored project {project.key}")
            db.flush()
            return {"project": {**_project_dict(project), "role": "owner"}}

    @router.delete("/{project_id}")
    async def delete_project(
        project_id: str,
        request: Request,
        confirm_key: str = Query(..., min_length=2, max_length=12),
    ):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="owner")
            if _normalize_key(confirm_key) != project.key:
                raise HTTPException(400, "confirm_key must match the project key")
            db.delete(project)
            db.flush()
        store.delete_project(project_id)
        return {"ok": True}

    @router.get("/{project_id}/overview")
    async def project_overview(project_id: str, request: Request):
        actor = _actor(request)
        with _db_session(write=False) as db:
            project, _ = _get_project(db, project_id, actor)
            return {"overview": _overview(db, project)}

    @router.get("/{project_id}/board")
    async def project_board(project_id: str, request: Request, include_archived: bool = Query(False)):
        actor = _actor(request)
        with _db_session(write=False) as db:
            project, role = _get_project(db, project_id, actor)
            items_query = db.query(ProjectWorkItem).options(
                defer(ProjectWorkItem.description)
            ).filter(ProjectWorkItem.project_id == project.id)
            board_limit = PROJECT_MAX_ITEMS if include_archived else PROJECT_MAX_ACTIVE_ITEMS
            if not include_archived:
                items_query = items_query.filter(ProjectWorkItem.archived.is_(False))
            items = items_query.order_by(
                ProjectWorkItem.stage_id.asc(), ProjectWorkItem.position.asc(), ProjectWorkItem.created_at.asc()
            ).limit(board_limit + 1).all()
            if len(items) > board_limit:
                raise HTTPException(
                    409,
                    f"This board exceeds the {board_limit:,}-item display limit; "
                    "archive completed work before reopening it.",
                )
            stages = db.query(ProjectStage).filter(ProjectStage.project_id == project.id).order_by(
                ProjectStage.position.asc(), ProjectStage.created_at.asc()
            ).all()
            counts = {
                stage.id: sum(int(not item.archived and item.stage_id == stage.id) for item in items)
                for stage in stages
            }
            return {
                "actor": _response_actor(),
                "project": {**_project_dict(project), "role": role},
                "stages": [_stage_dict(stage, item_count=counts[stage.id]) for stage in stages],
                "items": [_item_card_dict(item, project.key) for item in items],
                "members": _role_members(db, project),
                "overview": _overview(db, project),
            }

    @router.get("/{project_id}/members")
    async def list_members(project_id: str, request: Request):
        actor = _actor(request)
        with _db_session(write=False) as db:
            project, _ = _get_project(db, project_id, actor)
            return {"members": _role_members(db, project)}

    @router.post("/{project_id}/transfer")
    async def transfer_project(project_id: str, request: Request, body: ProjectTransfer):
        actor = _actor(request)
        target = _clean_text(body.username, field="Username", max_length=160, required=True).lower()
        if target == actor:
            raise HTTPException(400, "The target already owns this project")
        auth_manager = getattr(request.app.state, "auth_manager", None)
        configured_users = getattr(auth_manager, "users", None)
        if not isinstance(configured_users, dict) or target not in {
            str(name).strip().lower() for name in configured_users
        }:
            raise HTTPException(400, "Project ownership can only be transferred to a local profile")
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="owner", writable=True)
            _lock_named_quota_keys(db, _owner_quota_lock_key(target))
            _lock_project_quota(db, project)
            target_member = db.query(ProjectMember).filter(
                ProjectMember.project_id == project.id,
                func.lower(ProjectMember.username) == target,
            ).first()
            if not target_member:
                raise HTTPException(400, "Add the target as a project member before transferring ownership")
            if db.query(Project.id).filter(
                Project.owner == target,
                Project.key == project.key,
                Project.id != project.id,
            ).first():
                raise HTTPException(409, "The target already owns a project with this key; change the key first")
            target_project_count = db.query(Project.id).filter(Project.owner == target).count()
            if target_project_count >= PROJECT_MAX_PROJECTS_PER_OWNER:
                raise HTTPException(
                    409,
                    f"The target profile has reached its {PROJECT_MAX_PROJECTS_PER_OWNER:,}-project limit",
                )
            project_storage_bytes = int(
                db.query(func.coalesce(func.sum(ProjectAttachment.size), 0))
                .select_from(ProjectAttachment)
                .join(ProjectWorkItem, ProjectWorkItem.id == ProjectAttachment.work_item_id)
                .filter(ProjectWorkItem.project_id == project.id)
                .scalar()
                or 0
            )
            target_storage_bytes = int(
                db.query(func.coalesce(func.sum(ProjectAttachment.size), 0))
                .select_from(ProjectAttachment)
                .join(ProjectWorkItem, ProjectWorkItem.id == ProjectAttachment.work_item_id)
                .join(Project, Project.id == ProjectWorkItem.project_id)
                .filter(Project.owner == target)
                .scalar()
                or 0
            )
            if target_storage_bytes + project_storage_bytes > PROJECT_OWNER_STORAGE_MAX_BYTES:
                raise HTTPException(
                    409,
                    "The target profile does not have enough Project storage quota for this transfer",
                )
            _claim_version(db, project, body.version)
            db.delete(target_member)
            previous_owner_member = db.query(ProjectMember).filter(
                ProjectMember.project_id == project.id,
                func.lower(ProjectMember.username) == actor,
            ).first()
            if previous_owner_member:
                previous_owner_member.role = "editor"
            else:
                db.add(
                    ProjectMember(
                        project_id=project.id,
                        username=actor,
                        role="editor",
                        added_by=actor,
                        joined_at=utcnow_naive(),
                    )
                )
            project.owner = target
            project.updated_at = utcnow_naive()
            _activity(
                db, project.id, actor, "project_transferred", f"Transferred {project.key} to {target}",
                payload={"from": actor, "to": target},
            )
            db.flush()
            return _project_payload(db, project, "editor")

    @router.post("/{project_id}/members", status_code=201)
    async def add_member(project_id: str, request: Request, body: MemberCreate):
        actor = _actor(request)
        username = _clean_text(body.username, field="Username", max_length=160, required=True).lower()
        auth_manager = getattr(request.app.state, "auth_manager", None)
        configured_users = getattr(auth_manager, "users", None)
        if not isinstance(configured_users, dict) or username not in {
            str(name).strip().lower() for name in configured_users
        }:
            raise HTTPException(400, "Create the local profile before adding it as a project member")
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="owner", writable=True)
            if username == project.owner:
                raise HTTPException(409, "The project owner is already a member")
            if db.query(ProjectMember).filter(
                ProjectMember.project_id == project.id,
                func.lower(ProjectMember.username) == username,
            ).first():
                raise HTTPException(409, "Project member already exists")
            member = ProjectMember(
                project_id=project.id,
                username=username,
                role=body.role,
                added_by=actor,
                joined_at=utcnow_naive(),
            )
            db.add(member)
            project.updated_at = utcnow_naive()
            _activity(
                db, project.id, actor, "member_added", f"Added {username} as {body.role}",
                payload={"username": username, "role": body.role},
            )
            db.flush()
            return {"member": _member_dict(member)}

    @router.patch("/{project_id}/members/{username}")
    async def update_member(project_id: str, username: str, request: Request, body: MemberUpdate):
        actor = _actor(request)
        normalized = username.strip().lower()
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="owner", writable=True)
            member = db.query(ProjectMember).filter(
                ProjectMember.project_id == project.id,
                func.lower(ProjectMember.username) == normalized,
            ).first()
            if not member:
                raise HTTPException(404, "Project member not found")
            old_role = member.role
            member.role = body.role
            unassigned = _unassign_member_work(db, project.id, member.username) if body.role == "viewer" else 0
            project.updated_at = utcnow_naive()
            _activity(
                db, project.id, actor, "member_updated", f"Changed {member.username} to {body.role}",
                payload={
                    "username": member.username,
                    "from": old_role,
                    "to": body.role,
                    "unassigned_items": unassigned,
                },
            )
            db.flush()
            return {"member": _member_dict(member)}

    @router.delete("/{project_id}/members/{username}")
    async def remove_member(project_id: str, username: str, request: Request):
        actor = _actor(request)
        normalized = username.strip().lower()
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="owner", writable=True)
            member = db.query(ProjectMember).filter(
                ProjectMember.project_id == project.id,
                func.lower(ProjectMember.username) == normalized,
            ).first()
            if not member:
                raise HTTPException(404, "Project member not found")
            removed = member.username
            unassigned = _unassign_member_work(db, project.id, removed)
            db.delete(member)
            project.updated_at = utcnow_naive()
            _activity(
                db, project.id, actor, "member_removed", f"Removed {removed}",
                payload={"username": removed, "unassigned_items": unassigned},
            )
            return {"ok": True}

    @router.get("/{project_id}/stages")
    async def list_stages(project_id: str, request: Request):
        actor = _actor(request)
        with _db_session(write=False) as db:
            project, _ = _get_project(db, project_id, actor)
            stages = db.query(ProjectStage).filter(ProjectStage.project_id == project.id).order_by(
                ProjectStage.position.asc(), ProjectStage.created_at.asc()
            ).all()
            counts = {
                stage.id: db.query(ProjectWorkItem).filter(
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.stage_id == stage.id,
                    ProjectWorkItem.archived.is_(False),
                ).count()
                for stage in stages
            }
            return {"stages": [_stage_dict(stage, item_count=counts[stage.id]) for stage in stages]}

    @router.post("/{project_id}/stages", status_code=201)
    async def create_stage(project_id: str, request: Request, body: StageCreate):
        actor = _actor(request)
        category = str(body.category or "").strip().lower()
        if category not in STAGE_CATEGORIES:
            raise HTTPException(400, "Invalid stage category")
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="owner", writable=True)
            _lock_project_quota(db, project)
            stage_count = db.query(ProjectStage.id).filter(
                ProjectStage.project_id == project.id
            ).count()
            if stage_count >= PROJECT_MAX_STAGES_PER_PROJECT:
                raise HTTPException(
                    409,
                    f"This project has reached its {PROJECT_MAX_STAGES_PER_PROJECT:,}-stage limit",
                )
            rows = _normalize_stage_order(db, project.id)
            position = len(rows) if body.position is None else min(body.position, len(rows))
            for row in rows[position:]:
                row.position += 1
            stage = ProjectStage(
                id=str(uuid.uuid4()),
                project_id=project.id,
                name=_clean_text(body.name, field="Stage name", max_length=80, required=True),
                category=category,
                color=_normalize_color(body.color),
                position=position,
                wip_limit=body.wip_limit,
            )
            db.add(stage)
            project.updated_at = utcnow_naive()
            _activity(
                db, project.id, actor, "stage_created", f"Created stage {stage.name}",
                payload={"stage_id": stage.id, "category": stage.category},
            )
            db.flush()
            return {"stage": _stage_dict(stage, item_count=0)}

    @router.patch("/{project_id}/stages/{stage_id}")
    async def update_stage(project_id: str, stage_id: str, request: Request, body: StageUpdate):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="owner", writable=True)
            stage = _get_stage(db, project.id, stage_id)
            previous_category = stage.category
            changes: dict[str, Any] = {}
            if body.name is not None:
                stage.name = _clean_text(body.name, field="Stage name", max_length=80, required=True)
                changes["name"] = stage.name
            if body.category is not None:
                category = str(body.category).strip().lower()
                if category not in STAGE_CATEGORIES:
                    raise HTTPException(400, "Invalid stage category")
                stage.category = category
                changes["category"] = category
            if body.color is not None:
                stage.color = _normalize_color(body.color)
                changes["color"] = stage.color
            if body.clear_wip_limit:
                stage.wip_limit = None
                changes["wip_limit"] = None
            elif body.wip_limit is not None:
                active_count = db.query(ProjectWorkItem).filter(
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.stage_id == stage.id,
                    ProjectWorkItem.archived.is_(False),
                ).count()
                if active_count > body.wip_limit:
                    raise HTTPException(409, "WIP limit cannot be lower than the current item count")
                stage.wip_limit = body.wip_limit
                changes["wip_limit"] = body.wip_limit
            if body.position is not None:
                rows = [row for row in _normalize_stage_order(db, project.id) if row.id != stage.id]
                position = min(body.position, len(rows))
                rows.insert(position, stage)
                for index, row in enumerate(rows):
                    row.position = index
                changes["position"] = position
            if stage.category != previous_category:
                now = utcnow_naive()
                for item in db.query(ProjectWorkItem).filter(
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.stage_id == stage.id,
                    ProjectWorkItem.archived.is_(False),
                ).options(defer(ProjectWorkItem.description)).all():
                    item.completed_at = now if stage.category == "done" else None
                    item.version = int(item.version or 1) + 1
            if changes:
                project.updated_at = utcnow_naive()
                _activity(
                    db, project.id, actor, "stage_updated", f"Updated stage {stage.name}",
                    payload={"stage_id": stage.id, **changes},
                )
            db.flush()
            count = db.query(ProjectWorkItem).filter(
                ProjectWorkItem.project_id == project.id,
                ProjectWorkItem.stage_id == stage.id,
                ProjectWorkItem.archived.is_(False),
            ).count()
            return {"stage": _stage_dict(stage, item_count=count)}

    @router.put("/{project_id}/stages/order")
    async def order_stages(project_id: str, request: Request, body: StageOrder):
        actor = _actor(request)
        if len(body.stage_ids) != len(set(body.stage_ids)):
            raise HTTPException(400, "stage_ids contains duplicates")
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="owner", writable=True)
            rows = db.query(ProjectStage).filter(ProjectStage.project_id == project.id).all()
            by_id = {row.id: row for row in rows}
            if set(body.stage_ids) != set(by_id):
                raise HTTPException(400, "stage_ids must contain every project stage exactly once")
            for position, stage_id in enumerate(body.stage_ids):
                by_id[stage_id].position = position
            project.updated_at = utcnow_naive()
            _activity(db, project.id, actor, "stages_reordered", "Reordered project stages")
            db.flush()
            return {"stages": [_stage_dict(by_id[stage_id]) for stage_id in body.stage_ids]}

    @router.delete("/{project_id}/stages/{stage_id}")
    async def delete_stage(
        project_id: str,
        stage_id: str,
        request: Request,
        move_to_stage_id: Optional[str] = Query(None),
    ):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="owner", writable=True)
            stage = _get_stage(db, project.id, stage_id)
            if db.query(ProjectStage).filter(ProjectStage.project_id == project.id).count() <= 1:
                raise HTTPException(409, "A project must keep at least one stage")
            all_items = db.query(ProjectWorkItem).filter(
                ProjectWorkItem.project_id == project.id,
                ProjectWorkItem.stage_id == stage.id,
            ).options(defer(ProjectWorkItem.description)).order_by(
                ProjectWorkItem.position.asc(), ProjectWorkItem.id.asc()
            ).all()
            destination: Optional[ProjectStage] = None
            if all_items:
                if not move_to_stage_id or move_to_stage_id == stage.id:
                    raise HTTPException(409, "Choose another stage for existing work items")
                destination = _get_stage(db, project.id, move_to_stage_id)
                active_moving = sum(int(not item.archived) for item in all_items)
                _guard_wip(db, destination, extra=active_moving)
                destination_rows = _active_stage_items(db, project.id, destination.id)
                for item in all_items:
                    item.stage_id = destination.id
                    item.completed_at = utcnow_naive() if destination.category == "done" else None
                    item.version = int(item.version or 1) + 1
                    if not item.archived:
                        destination_rows.append(item)
                _set_item_order(destination.id, destination_rows)
            removed_name = stage.name
            db.delete(stage)
            db.flush()
            stages = _normalize_stage_order(db, project.id)
            project.updated_at = utcnow_naive()
            _activity(
                db, project.id, actor, "stage_deleted", f"Deleted stage {removed_name}",
                payload={"stage_id": stage_id, "moved_to_stage_id": destination.id if destination else None},
            )
            db.flush()
            return {"stages": [_stage_dict(row) for row in stages]}

    @router.get("/{project_id}/items")
    async def list_items(
        project_id: str,
        request: Request,
        stage_id: Optional[str] = Query(None),
        assignee: Optional[str] = Query(None),
        archived: bool = Query(False),
        q: Optional[str] = Query(None, max_length=240),
        limit: int = Query(500, ge=1, le=ITEM_LIST_MAX_LIMIT),
        offset: int = Query(0, ge=0),
    ):
        actor = _actor(request)
        with _db_session(write=False) as db:
            project, _ = _get_project(db, project_id, actor)
            query = db.query(ProjectWorkItem).filter(
                ProjectWorkItem.project_id == project.id,
                ProjectWorkItem.archived.is_(archived),
            )
            if stage_id:
                _get_stage(db, project.id, stage_id)
                query = query.filter(ProjectWorkItem.stage_id == stage_id)
            if assignee:
                query = query.filter(
                    func.lower(ProjectWorkItem.assignee).in_(
                        _assignee_filter_values(db, project, assignee)
                    )
                )
            if q:
                term = f"%{q.strip()}%"
                query = query.filter(
                    or_(ProjectWorkItem.title.ilike(term), ProjectWorkItem.description.ilike(term))
                )
            total = query.count()
            rows = query.options(defer(ProjectWorkItem.description)).order_by(
                ProjectWorkItem.stage_id.asc(),
                ProjectWorkItem.position.asc(),
                ProjectWorkItem.updated_at.desc(),
                ProjectWorkItem.id.asc(),
            ).offset(offset).limit(limit).all()
            next_offset = offset + len(rows)
            return {
                "items": [_item_card_dict(row, project.key) for row in rows],
                "total": total,
                "next_offset": next_offset if next_offset < total else None,
            }

    @router.post("/{project_id}/items", status_code=201)
    async def create_item(project_id: str, request: Request, body: WorkItemCreate):
        actor = _actor(request)
        item_type = str(body.item_type or "").strip().lower()
        priority = str(body.priority or "").strip().lower()
        if item_type not in ITEM_TYPES:
            raise HTTPException(400, "Invalid work item type")
        if priority not in PRIORITIES:
            raise HTTPException(400, "Invalid priority")
        start_date = _validate_date(body.start_date, "start_date")
        due_date = _validate_date(body.due_date, "due_date")
        if start_date and due_date and start_date > due_date:
            raise HTTPException(400, "due_date cannot be before start_date")
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="editor", writable=True)
            _guard_work_item_quota(db, project)
            if body.stage_id:
                stage = _get_stage(db, project.id, body.stage_id)
            else:
                stage = db.query(ProjectStage).filter(ProjectStage.project_id == project.id).order_by(
                    ProjectStage.position.asc(), ProjectStage.created_at.asc()
                ).first()
                if not stage:
                    raise HTTPException(409, "Create a project stage before adding work")
            _guard_wip(db, stage)
            parent = _related_item(db, project.id, body.parent_id, "Parent")
            blocked_by = _related_item(db, project.id, body.blocked_by_id, "Blocking")
            if parent and parent.archived:
                raise HTTPException(409, "Restore the parent work item before adding subtasks")
            if blocked_by and blocked_by.archived:
                raise HTTPException(400, "An archived work item cannot be an active blocker")
            if item_type == "subtask" and not parent:
                raise HTTPException(400, "A subtask requires parent_id")
            if parent and parent.item_type == "subtask":
                raise HTTPException(400, "A subtask cannot be the parent of another work item")
            item_id = str(uuid.uuid4())
            if blocked_by and blocked_by.id == item_id:
                raise HTTPException(400, "A work item cannot block itself")
            number = _allocate_item_number(db, project.id)
            existing = _active_stage_items(db, project.id, stage.id)
            insert_at = len(existing) if body.position is None else min(body.position, len(existing))
            item = ProjectWorkItem(
                id=item_id,
                project_id=project.id,
                stage_id=stage.id,
                item_number=number,
                item_type=item_type,
                title=_clean_text(body.title, field="Title", max_length=240, required=True),
                description=_clean_text(body.description, field="Description", max_length=100_000),
                priority=priority,
                labels=_normalize_labels(body.labels),
                reporter=_canonical_actor(actor),
                assignee=_validate_assignee(db, project, body.assignee),
                start_date=start_date,
                due_date=due_date,
                estimate_minutes=body.estimate_minutes,
                logged_minutes=body.logged_minutes,
                parent_id=parent.id if parent else None,
                blocked_by_id=blocked_by.id if blocked_by else None,
                position=insert_at,
                archived=False,
                completed_at=utcnow_naive() if stage.category == "done" else None,
                version=1,
            )
            db.add(item)
            existing.insert(insert_at, item)
            _set_item_order(stage.id, existing)
            project.updated_at = utcnow_naive()
            # ProjectActivity has a real FK but no ORM dependency edge to a
            # newly pending item, so materialize the item before its event.
            db.flush()
            _activity(
                db, project.id, actor, "work_item_created", f"Created {project.key}-{number}: {item.title}",
                work_item_id=item.id,
                payload={"key": f"{project.key}-{number}", "stage_id": stage.id, "type": item_type},
            )
            db.flush()
            return {"item": _item_dict(item, project.key)}

    @router.get("/{project_id}/items/{item_id}")
    async def get_item(project_id: str, item_id: str, request: Request):
        actor = _actor(request)
        with _db_session(write=False) as db:
            project, _ = _get_project(db, project_id, actor)
            item = _get_item(db, project.id, item_id)
            return _item_detail(db, project, item)

    @router.patch("/{project_id}/items/{item_id}")
    async def update_item(project_id: str, item_id: str, request: Request, body: WorkItemUpdate):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="editor", writable=True)
            item = _get_item(db, project.id, item_id, writable=True)
            _claim_version(db, item, body.version)
            changes: dict[str, Any] = {}
            if body.title is not None:
                item.title = _clean_text(body.title, field="Title", max_length=240, required=True)
                changes["title"] = item.title
            if body.description is not None:
                item.description = _clean_text(body.description, field="Description", max_length=100_000)
                changes["description"] = "updated"
            if body.item_type is not None:
                value = str(body.item_type).strip().lower()
                if value not in ITEM_TYPES:
                    raise HTTPException(400, "Invalid work item type")
                if value == "subtask" and db.query(ProjectWorkItem.id).filter(
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.parent_id == item.id,
                ).first():
                    raise HTTPException(409, "A work item with subtasks cannot become a subtask")
                item.item_type = value
                changes["item_type"] = value
            if body.priority is not None:
                value = str(body.priority).strip().lower()
                if value not in PRIORITIES:
                    raise HTTPException(400, "Invalid priority")
                item.priority = value
                changes["priority"] = value
            if body.labels is not None:
                item.labels = _normalize_labels(body.labels)
                changes["labels"] = item.labels
            if body.clear_assignee:
                item.assignee = None
                changes["assignee"] = None
            elif body.assignee is not None:
                item.assignee = _validate_assignee(db, project, body.assignee)
                changes["assignee"] = item.assignee
            if body.clear_start_date:
                item.start_date = None
                changes["start_date"] = None
            elif body.start_date is not None:
                item.start_date = _validate_date(body.start_date, "start_date")
                changes["start_date"] = item.start_date
            if body.clear_due_date:
                item.due_date = None
                changes["due_date"] = None
            elif body.due_date is not None:
                item.due_date = _validate_date(body.due_date, "due_date")
                changes["due_date"] = item.due_date
            if item.start_date and item.due_date and item.start_date > item.due_date:
                raise HTTPException(400, "due_date cannot be before start_date")
            if body.estimate_minutes is not None:
                item.estimate_minutes = body.estimate_minutes
                changes["estimate_minutes"] = body.estimate_minutes
            if body.logged_minutes is not None:
                item.logged_minutes = body.logged_minutes
                changes["logged_minutes"] = body.logged_minutes
            if body.clear_parent:
                item.parent_id = None
                changes["parent_id"] = None
            elif body.parent_id is not None:
                parent = _related_item(db, project.id, body.parent_id, "Parent")
                if parent.archived:
                    raise HTTPException(409, "Restore the parent work item before adding subtasks")
                if parent.id == item.id:
                    raise HTTPException(400, "A work item cannot be its own parent")
                if parent.item_type == "subtask":
                    raise HTTPException(400, "A subtask cannot be the parent of another work item")
                _assert_no_link_cycle(db, project.id, item.id, parent.id, "parent_id")
                item.parent_id = parent.id
                changes["parent_id"] = parent.id
            if body.clear_blocked_by:
                item.blocked_by_id = None
                changes["blocked_by_id"] = None
            elif body.blocked_by_id is not None:
                blocked_by = _related_item(db, project.id, body.blocked_by_id, "Blocking")
                if blocked_by.archived:
                    raise HTTPException(400, "An archived work item cannot be an active blocker")
                if blocked_by.id == item.id:
                    raise HTTPException(400, "A work item cannot block itself")
                _assert_no_link_cycle(db, project.id, item.id, blocked_by.id, "blocked_by_id")
                item.blocked_by_id = blocked_by.id
                changes["blocked_by_id"] = blocked_by.id
            if item.item_type == "subtask" and not item.parent_id:
                raise HTTPException(400, "A subtask requires a parent")
            if changes:
                item.updated_at = utcnow_naive()
                project.updated_at = item.updated_at
                _activity(
                    db, project.id, actor, "work_item_updated", f"Updated {project.key}-{item.item_number}",
                    work_item_id=item.id, payload=changes,
                )
            db.flush()
            return {"item": _item_dict(item, project.key)}

    @router.post("/{project_id}/items/{item_id}/move")
    async def move_item(project_id: str, item_id: str, request: Request, body: WorkItemMove):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="editor", writable=True)
            item = _get_item(db, project.id, item_id, writable=True)
            _claim_version(db, item, body.version)
            destination = _get_stage(db, project.id, body.stage_id)
            old_stage_id = item.stage_id
            _move_item(db, item, destination, body.position)
            item.updated_at = utcnow_naive()
            project.updated_at = item.updated_at
            _activity(
                db, project.id, actor, "work_item_moved", f"Moved {project.key}-{item.item_number} to {destination.name}",
                work_item_id=item.id,
                payload={"from_stage_id": old_stage_id, "to_stage_id": destination.id, "position": item.position},
            )
            db.flush()
            return {"item": _item_dict(item, project.key)}

    @router.put("/{project_id}/items/order")
    async def order_items(project_id: str, request: Request, body: WorkItemOrder):
        actor = _actor(request)
        if len(body.item_ids) != len(set(body.item_ids)):
            raise HTTPException(400, "item_ids contains duplicates")
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="editor", writable=True)
            stage = _get_stage(db, project.id, body.stage_id)
            rows = _active_stage_items(db, project.id, stage.id)
            by_id = {row.id: row for row in rows}
            if set(body.item_ids) != set(by_id):
                raise HTTPException(400, "item_ids must contain every active item in the stage exactly once")
            if set(body.versions) != set(body.item_ids):
                raise HTTPException(400, "versions must cover every ordered item")
            for item_id, expected in body.versions.items():
                _claim_version(db, by_id[item_id], expected)
            ordered = [by_id[item_id] for item_id in body.item_ids]
            _set_item_order(stage.id, ordered)
            now = utcnow_naive()
            for row in ordered:
                row.updated_at = now
            project.updated_at = now
            _activity(db, project.id, actor, "work_items_reordered", f"Reordered work in {stage.name}")
            db.flush()
            return {"items": [_item_card_dict(row, project.key) for row in ordered]}

    @router.post("/{project_id}/items/{item_id}/archive")
    async def archive_item(project_id: str, item_id: str, request: Request, body: VersionRequest):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="editor", writable=True)
            item = _get_item(db, project.id, item_id)
            if not item.archived and db.query(ProjectWorkItem.id).filter(
                ProjectWorkItem.project_id == project.id,
                ProjectWorkItem.parent_id == item.id,
                ProjectWorkItem.archived.is_(False),
            ).first():
                raise HTTPException(409, "Archive this work item's active subtasks first")
            if (
                not item.archived
                and db.query(ProjectWorkItem.id)
                .filter(
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.blocked_by_id == item.id,
                    ProjectWorkItem.archived.is_(False),
                )
                .first()
            ):
                raise HTTPException(
                    409,
                    "Clear or archive this work item's dependents before archiving their blocker",
                )
            _claim_version(db, item, body.version)
            if not item.archived:
                if item.stage_id:
                    rows = [row for row in _active_stage_items(db, project.id, item.stage_id) if row.id != item.id]
                    _set_item_order(item.stage_id, rows)
                item.archived = True
                item.updated_at = utcnow_naive()
                project.updated_at = item.updated_at
                _activity(
                    db, project.id, actor, "work_item_archived", f"Archived {project.key}-{item.item_number}",
                    work_item_id=item.id,
                )
            db.flush()
            return {"item": _item_dict(item, project.key)}

    @router.post("/{project_id}/items/{item_id}/restore")
    async def restore_item(project_id: str, item_id: str, request: Request, body: VersionRequest):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="editor", writable=True)
            item = _get_item(db, project.id, item_id)
            if item.archived and item.parent_id:
                parent = _related_item(db, project.id, item.parent_id, "Parent")
                if parent and parent.archived:
                    raise HTTPException(409, "Restore the parent work item before restoring this subtask")
            if item.archived and item.blocked_by_id:
                blocker = _related_item(db, project.id, item.blocked_by_id, "Blocking")
                if blocker and blocker.archived:
                    raise HTTPException(
                        409,
                        "Restore or clear the blocking work item before restoring this dependent",
                    )
            _claim_version(db, item, body.version)
            if item.archived:
                _guard_active_work_item_quota(db, project)
                stage = _get_stage(db, project.id, item.stage_id) if item.stage_id else db.query(ProjectStage).filter(
                    ProjectStage.project_id == project.id
                ).order_by(ProjectStage.position.asc()).first()
                if not stage:
                    raise HTTPException(409, "Create a project stage before restoring this work item")
                _guard_wip(db, stage)
                rows = _active_stage_items(db, project.id, stage.id)
                rows.append(item)
                _set_item_order(stage.id, rows)
                item.archived = False
                item.completed_at = utcnow_naive() if stage.category == "done" else None
                item.updated_at = utcnow_naive()
                project.updated_at = item.updated_at
                _activity(
                    db, project.id, actor, "work_item_restored", f"Restored {project.key}-{item.item_number}",
                    work_item_id=item.id,
                )
            db.flush()
            return {"item": _item_dict(item, project.key)}

    @router.delete("/{project_id}/items/{item_id}")
    async def delete_item(
        project_id: str,
        item_id: str,
        request: Request,
        version: int = Query(..., ge=1),
    ):
        actor = _actor(request)
        storage_keys: list[str] = []
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="owner", writable=True)
            item = _get_item(db, project.id, item_id)
            _claim_version(db, item, version)
            if db.query(ProjectWorkItem.id).filter(
                ProjectWorkItem.project_id == project.id,
                ProjectWorkItem.parent_id == item.id,
            ).first():
                raise HTTPException(409, "Move or delete this work item's subtasks first")
            if item.stage_id and not item.archived:
                rows = [row for row in _active_stage_items(db, project.id, item.stage_id) if row.id != item.id]
                _set_item_order(item.stage_id, rows)
            storage_keys = [
                value[0]
                for value in db.query(ProjectAttachment.storage_key)
                .filter(ProjectAttachment.work_item_id == item.id)
                .all()
            ]
            key = f"{project.key}-{item.item_number}"
            db.query(ProjectActivity).filter(ProjectActivity.work_item_id == item.id).update(
                {ProjectActivity.work_item_id: None}, synchronize_session=False
            )
            db.delete(item)
            project.updated_at = utcnow_naive()
            _activity(db, project.id, actor, "work_item_deleted", f"Deleted {key}", payload={"key": key})
            db.flush()
        for storage_key in storage_keys:
            store.delete(storage_key)
        return {"ok": True}

    @router.post("/{project_id}/items/{item_id}/checklist", status_code=201)
    async def create_checklist_item(
        project_id: str,
        item_id: str,
        request: Request,
        body: ChecklistCreate,
    ):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="editor", writable=True)
            item = _get_item(db, project.id, item_id, writable=True)
            _guard_child_row_quota(
                db,
                project,
                ProjectChecklistItem,
                item.id,
                PROJECT_MAX_CHECKLIST_ITEMS_PER_ITEM,
                "checklist-item",
            )
            rows = _normalize_checklist_order(db, item.id)
            position = len(rows) if body.position is None else min(body.position, len(rows))
            for row in rows[position:]:
                row.position += 1
            checklist_item = ProjectChecklistItem(
                id=str(uuid.uuid4()),
                work_item_id=item.id,
                text=_clean_text(body.text, field="Checklist text", max_length=500, required=True),
                is_done=False,
                position=position,
                created_by=_canonical_actor(actor),
            )
            db.add(checklist_item)
            project.updated_at = utcnow_naive()
            _activity(
                db, project.id, actor, "checklist_added", f"Added checklist item to {project.key}-{item.item_number}",
                work_item_id=item.id, payload={"checklist_id": checklist_item.id},
            )
            db.flush()
            return {"checklist_item": _checklist_dict(checklist_item), "item": _item_dict(item, project.key)}

    @router.patch("/{project_id}/items/{item_id}/checklist/{checklist_id}")
    async def update_checklist_item(
        project_id: str,
        item_id: str,
        checklist_id: str,
        request: Request,
        body: ChecklistUpdate,
    ):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="editor", writable=True)
            item = _get_item(db, project.id, item_id, writable=True)
            row = db.query(ProjectChecklistItem).filter(
                ProjectChecklistItem.id == checklist_id,
                ProjectChecklistItem.work_item_id == item.id,
            ).first()
            if not row:
                raise HTTPException(404, "Checklist item not found")
            changed = False
            if body.text is not None:
                row.text = _clean_text(body.text, field="Checklist text", max_length=500, required=True)
                changed = True
            if body.done is not None and bool(body.done) != bool(row.is_done):
                row.is_done = bool(body.done)
                row.completed_at = utcnow_naive() if row.is_done else None
                changed = True
            if body.position is not None:
                rows = [value for value in _normalize_checklist_order(db, item.id) if value.id != row.id]
                position = min(body.position, len(rows))
                rows.insert(position, row)
                for index, value in enumerate(rows):
                    value.position = index
                changed = True
            if changed:
                project.updated_at = utcnow_naive()
                _activity(
                    db, project.id, actor, "checklist_updated", f"Updated checklist on {project.key}-{item.item_number}",
                    work_item_id=item.id,
                    payload={"checklist_id": row.id, "done": bool(row.is_done)},
                )
            db.flush()
            return {"checklist_item": _checklist_dict(row), "item": _item_dict(item, project.key)}

    @router.delete("/{project_id}/items/{item_id}/checklist/{checklist_id}")
    async def delete_checklist_item(
        project_id: str,
        item_id: str,
        checklist_id: str,
        request: Request,
    ):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="editor", writable=True)
            item = _get_item(db, project.id, item_id, writable=True)
            row = db.query(ProjectChecklistItem).filter(
                ProjectChecklistItem.id == checklist_id,
                ProjectChecklistItem.work_item_id == item.id,
            ).first()
            if not row:
                raise HTTPException(404, "Checklist item not found")
            db.delete(row)
            db.flush()
            _normalize_checklist_order(db, item.id)
            project.updated_at = utcnow_naive()
            _activity(
                db, project.id, actor, "checklist_deleted", f"Removed checklist item from {project.key}-{item.item_number}",
                work_item_id=item.id, payload={"checklist_id": checklist_id},
            )
            return {"ok": True, "item": _item_dict(item, project.key)}

    @router.get("/{project_id}/items/{item_id}/comments")
    async def list_comments(
        project_id: str,
        item_id: str,
        request: Request,
        before: Optional[str] = Query(None, max_length=200),
        limit: int = Query(100, ge=1, le=200),
    ):
        actor = _actor(request)
        with _db_session(write=False) as db:
            project, _ = _get_project(db, project_id, actor)
            item = _get_item(db, project.id, item_id)
            query = db.query(ProjectComment).filter(
                ProjectComment.work_item_id == item.id
            )
            if before:
                try:
                    timestamp_text, separator, cursor_id = before.rpartition("|")
                    if not separator or not cursor_id:
                        raise ValueError
                    cursor = datetime.fromisoformat(timestamp_text.rstrip("Z"))
                except (TypeError, ValueError):
                    raise HTTPException(400, "before must be an ISO timestamp and comment id")
                query = query.filter(
                    or_(
                        ProjectComment.created_at < cursor,
                        and_(
                            ProjectComment.created_at == cursor,
                            ProjectComment.id < cursor_id,
                        ),
                    )
                )
            rows = query.order_by(
                ProjectComment.created_at.desc(), ProjectComment.id.desc()
            ).limit(limit + 1).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            chronological = list(reversed(rows))
            return {
                "comments": [_comment_dict(row) for row in chronological],
                "next_before": (
                    f"{_iso(chronological[0].created_at)}|{chronological[0].id}"
                    if has_more and chronological
                    else None
                ),
            }

    @router.post("/{project_id}/items/{item_id}/comments", status_code=201)
    async def create_comment(project_id: str, item_id: str, request: Request, body: CommentCreate):
        actor = _actor(request)
        with _db_session() as db:
            project, _ = _get_project(db, project_id, actor, minimum="editor", writable=True)
            item = _get_item(db, project.id, item_id, writable=True)
            _guard_child_row_quota(
                db,
                project,
                ProjectComment,
                item.id,
                PROJECT_MAX_COMMENTS_PER_ITEM,
                "comment",
            )
            comment = ProjectComment(
                id=str(uuid.uuid4()),
                work_item_id=item.id,
                author=_canonical_actor(actor),
                body=_clean_text(body.body, field="Comment", max_length=50_000, required=True),
            )
            db.add(comment)
            project.updated_at = utcnow_naive()
            _activity(
                db, project.id, actor, "comment_added", f"Commented on {project.key}-{item.item_number}",
                work_item_id=item.id, payload={"comment_id": comment.id},
            )
            db.flush()
            return {"comment": _comment_dict(comment)}

    @router.patch("/{project_id}/items/{item_id}/comments/{comment_id}")
    async def update_comment(
        project_id: str,
        item_id: str,
        comment_id: str,
        request: Request,
        body: CommentUpdate,
    ):
        actor = _actor(request)
        with _db_session() as db:
            project, role = _get_project(db, project_id, actor, minimum="editor", writable=True)
            item = _get_item(db, project.id, item_id, writable=True)
            comment = db.query(ProjectComment).filter(
                ProjectComment.id == comment_id,
                ProjectComment.work_item_id == item.id,
            ).first()
            if not comment:
                raise HTTPException(404, "Comment not found")
            if comment.author != _canonical_actor(actor) and role != "owner":
                raise HTTPException(403, "Only the comment author or project owner can edit it")
            comment.body = _clean_text(body.body, field="Comment", max_length=50_000, required=True)
            comment.edited_at = utcnow_naive()
            project.updated_at = comment.edited_at
            _activity(
                db, project.id, actor, "comment_updated", f"Edited a comment on {project.key}-{item.item_number}",
                work_item_id=item.id, payload={"comment_id": comment.id},
            )
            db.flush()
            return {"comment": _comment_dict(comment)}

    @router.delete("/{project_id}/items/{item_id}/comments/{comment_id}")
    async def delete_comment(
        project_id: str,
        item_id: str,
        comment_id: str,
        request: Request,
    ):
        actor = _actor(request)
        with _db_session() as db:
            project, role = _get_project(db, project_id, actor, minimum="editor", writable=True)
            item = _get_item(db, project.id, item_id, writable=True)
            comment = db.query(ProjectComment).filter(
                ProjectComment.id == comment_id,
                ProjectComment.work_item_id == item.id,
            ).first()
            if not comment:
                raise HTTPException(404, "Comment not found")
            if comment.author != _canonical_actor(actor) and role != "owner":
                raise HTTPException(403, "Only the comment author or project owner can delete it")
            db.delete(comment)
            project.updated_at = utcnow_naive()
            _activity(
                db, project.id, actor, "comment_deleted", f"Deleted a comment from {project.key}-{item.item_number}",
                work_item_id=item.id, payload={"comment_id": comment_id},
            )
            return {"ok": True}

    @router.get("/{project_id}/items/{item_id}/attachments")
    async def list_attachments(
        project_id: str,
        item_id: str,
        request: Request,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        actor = _actor(request)
        with _db_session(write=False) as db:
            project, _ = _get_project(db, project_id, actor)
            item = _get_item(db, project.id, item_id)
            query = db.query(ProjectAttachment).filter(
                ProjectAttachment.work_item_id == item.id
            )
            total = query.count()
            rows = query.order_by(
                ProjectAttachment.created_at.desc(), ProjectAttachment.id.desc()
            ).offset(offset).limit(limit).all()
            next_offset = offset + len(rows)
            return {
                "attachments": [_attachment_dict(row) for row in rows],
                "total": total,
                "next_offset": next_offset if next_offset < total else None,
            }

    @router.post("/{project_id}/items/{item_id}/attachments", status_code=201)
    async def upload_attachment(
        project_id: str,
        item_id: str,
        request: Request,
        file: UploadFile = File(...),
        kind: str = Form("reference"),
        description: str = Form(""),
        submission_note: str = Form(""),
        transition_stage_id: Optional[str] = Form(None),
        version: Optional[int] = Form(None),
    ):
        actor = _actor(request)
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in ATTACHMENT_KINDS:
            raise HTTPException(400, "Attachment kind must be reference, draft, or deliverable")
        clean_description = _clean_text(description, field="Description", max_length=500)
        clean_note = _clean_text(submission_note, field="Submission note", max_length=5_000)
        if transition_stage_id and version is None:
            raise HTTPException(400, "version is required when submitting into another stage")

        # Fail closed before accepting a potentially large body, but do not
        # hold SQLite's global writer reservation while reading or fsyncing up
        # to 50 MB. State and authorization are checked again in the commit
        # transaction because membership/workflow may change meanwhile.
        with _db_session(write=False) as db:
            project, _ = _get_project(db, project_id, actor, minimum="editor", writable=True)
            item = _get_item(db, project.id, item_id, writable=True)
            if transition_stage_id:
                _check_version(item, version)
                _get_stage(db, project.id, transition_stage_id)
            storage_project_id = project.id
            storage_item_id = item.id

        validated = await read_project_attachment(file)
        attachment_id = str(uuid.uuid4())
        storage_key = store.storage_key(
            storage_project_id, storage_item_id, attachment_id, validated.extension
        )
        metadata_committed = False
        item_payload = None
        attachment_payload = None
        try:
            try:
                await write_project_attachment_cancellation_safe(
                    store,
                    storage_key,
                    validated.data,
                )
            except OSError as exc:
                raise HTTPException(
                    507,
                    "Unable to store attachment; check project storage space and permissions",
                ) from exc
            attachment_name = validated.display_name
            attachment_mime = validated.mime
            attachment_size = validated.size
            attachment_sha256 = validated.sha256
            # Release the largest in-memory reference before waiting for the
            # serialized metadata transaction.
            del validated
            with _db_session() as db:
                project, _ = _get_project(db, project_id, actor, minimum="editor", writable=True)
                item = _get_item(db, project.id, item_id, writable=True)
                destination = None
                if transition_stage_id:
                    _claim_version(db, item, version)
                    destination = _get_stage(db, project.id, transition_stage_id)
                    _guard_wip(db, destination, moving_item=item)
                quota = _guard_attachment_quota(db, project, item, attachment_size)
                attachment = ProjectAttachment(
                    id=attachment_id,
                    work_item_id=item.id,
                    uploader=_canonical_actor(actor),
                    kind=normalized_kind,
                    description=clean_description,
                    original_name=attachment_name,
                    storage_key=storage_key,
                    mime=attachment_mime,
                    size=attachment_size,
                    sha256=attachment_sha256,
                    status="ready",
                )
                db.add(attachment)
                old_stage_id = item.stage_id
                is_submission = bool(clean_note or transition_stage_id or normalized_kind == "deliverable")
                if destination:
                    _move_item(db, item, destination, None)
                    item.updated_at = utcnow_naive()
                project.updated_at = utcnow_naive()
                event_type = "work_submitted" if is_submission else "attachment_added"
                summary = (
                    f"Submitted work for {project.key}-{item.item_number}"
                    if is_submission
                    else f"Attached {attachment_name} to {project.key}-{item.item_number}"
                )
                _activity(
                    db, project.id, actor, event_type, summary,
                    work_item_id=item.id,
                    payload={
                        "attachment_id": attachment.id,
                        "name": attachment.original_name,
                        "kind": attachment.kind,
                        "sha256": attachment.sha256,
                        "submission_note": clean_note,
                        "from_stage_id": old_stage_id,
                        "to_stage_id": destination.id if destination else None,
                        "project_storage_bytes_after": quota["used_bytes"] + attachment_size,
                    },
                )
                db.flush()
                attachment_payload = _attachment_dict(attachment)
                item_payload = _item_dict(item, project.key)
                # Mark durability at the exact commit boundary. The surrounding
                # session context commits again as a harmless no-op, but this
                # explicit commit prevents a later close/finalizer failure from
                # deleting a file whose metadata is already durable.
                db.commit()
                metadata_committed = True
        except BaseException:
            if storage_key and not metadata_committed:
                store.delete(storage_key)
            raise
        return {"attachment": attachment_payload, "item": item_payload}

    @router.delete("/{project_id}/attachments/{attachment_id}")
    async def delete_attachment(project_id: str, attachment_id: str, request: Request):
        actor = _actor(request)
        storage_key = None
        with _db_session() as db:
            project, role = _get_project(db, project_id, actor, minimum="editor", writable=True)
            row = db.query(ProjectAttachment).join(
                ProjectWorkItem, ProjectWorkItem.id == ProjectAttachment.work_item_id
            ).filter(
                ProjectAttachment.id == attachment_id,
                ProjectWorkItem.project_id == project.id,
            ).first()
            if not row:
                raise HTTPException(404, "Attachment not found")
            if row.uploader != _canonical_actor(actor) and role != "owner":
                raise HTTPException(403, "Only the uploader or project owner can delete this attachment")
            item = _get_item(db, project.id, row.work_item_id, writable=True)
            storage_key = row.storage_key
            db.delete(row)
            project.updated_at = utcnow_naive()
            _activity(
                db, project.id, actor, "attachment_deleted", f"Deleted {row.original_name} from {project.key}-{item.item_number}",
                work_item_id=item.id, payload={"attachment_id": attachment_id},
            )
            db.flush()
        if storage_key:
            store.delete(storage_key)
        return {"ok": True}

    @router.get("/attachments/{attachment_id}/download")
    async def download_attachment(attachment_id: str, request: Request):
        actor = _actor(request)
        with _db_session(write=False) as db:
            row = db.query(ProjectAttachment).filter(ProjectAttachment.id == attachment_id).first()
            if not row:
                raise HTTPException(404, "Attachment not found")
            item = db.query(ProjectWorkItem).filter(ProjectWorkItem.id == row.work_item_id).first()
            if not item:
                raise HTTPException(404, "Attachment not found")
            project, _ = _get_project(db, item.project_id, actor)
            # Resolve only after the owner/member check, preventing filesystem
            # existence from becoming an authorization side channel.
            try:
                path = store.resolve(row.storage_key)
            except (HTTPException, OSError) as exc:
                logger.error(
                    "Project attachment %s failed integrity verification: stored file is missing or unsafe",
                    attachment_id,
                )
                raise HTTPException(
                    500, "Stored attachment failed integrity verification"
                ) from exc
            expected_size = row.size
            expected_sha256 = row.sha256
            mime = row.mime
            filename = row.original_name
        release_download = _attachment_download_gate.try_acquire(
            _attachment_download_principal(actor)
        )
        if release_download is None:
            raise HTTPException(
                429,
                "Too many project attachment downloads are already active",
                headers={"Retry-After": "2"},
            )
        snapshot: Optional[BinaryIO] = None
        try:
            verified = await _prepare_verified_attachment_snapshot(
                path,
                expected_size,
                expected_sha256,
            )
            if verified is None:
                logger.error(
                    "Project attachment %s failed integrity verification: size or SHA-256 mismatch",
                    attachment_id,
                )
                raise HTTPException(
                    500, "Stored attachment failed integrity verification"
                )
            snapshot, verified_size = verified
            return _VerifiedAttachmentResponse(
                snapshot,
                on_close=release_download,
                media_type=mime,
                headers={
                    "Content-Length": str(verified_size),
                    "Content-Disposition": _attachment_content_disposition(filename),
                    "Cache-Control": "private, no-store",
                    "Pragma": "no-cache",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "default-src 'none'; sandbox",
                },
            )
        except BaseException:
            if snapshot is not None:
                snapshot.close()
            release_download()
            raise

    @router.get("/{project_id}/activity")
    async def project_activity(
        project_id: str,
        request: Request,
        work_item_id: Optional[str] = Query(None),
        before: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=MAX_ACTIVITY_LIMIT),
    ):
        actor = _actor(request)
        with _db_session(write=False) as db:
            project, _ = _get_project(db, project_id, actor)
            query = db.query(ProjectActivity).filter(ProjectActivity.project_id == project.id)
            if work_item_id:
                _get_item(db, project.id, work_item_id)
                query = query.filter(ProjectActivity.work_item_id == work_item_id)
            if before:
                try:
                    timestamp_text, separator, cursor_id = before.rpartition("|")
                    if not separator:
                        timestamp_text, cursor_id = before, ""
                    cursor = datetime.fromisoformat(timestamp_text.rstrip("Z"))
                except (TypeError, ValueError):
                    raise HTTPException(400, "before must be an ISO timestamp")
                if cursor_id:
                    query = query.filter(
                        or_(
                            ProjectActivity.created_at < cursor,
                            and_(
                                ProjectActivity.created_at == cursor,
                                ProjectActivity.id < cursor_id,
                            ),
                        )
                    )
                else:
                    # Legacy clients sent timestamp-only cursors. Keep them
                    # accepted, while every new cursor is lossless for ties.
                    query = query.filter(ProjectActivity.created_at < cursor)
            rows = query.order_by(ProjectActivity.created_at.desc(), ProjectActivity.id.desc()).limit(limit + 1).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            return {
                "activity": [_activity_dict(row) for row in rows],
                "next_before": (
                    f"{_iso(rows[-1].created_at)}|{rows[-1].id}"
                    if has_more and rows
                    else None
                ),
            }

    return router
