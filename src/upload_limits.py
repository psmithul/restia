"""Small helpers for route-local upload size caps."""

import os

from fastapi import HTTPException, UploadFile

DEFAULT_CHAT_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
CHAT_UPLOAD_MAX_BYTES_ENV = "RESTIA_CHAT_UPLOAD_MAX_BYTES"


def format_byte_limit(limit: int) -> str:
    if limit % (1024 * 1024) == 0:
        return f"{limit // (1024 * 1024)} MB"
    if limit % 1024 == 0:
        return f"{limit // 1024} KB"
    return f"{limit} bytes"


def read_byte_limit_env(name: str, default: int) -> int:
    source_name = name
    raw = os.getenv(name)
    if (raw is None or not raw.strip()) and name.startswith("RESTIA_"):
        old_name = "ODYSSEUS_" + name[len("RESTIA_"):]
        raw = os.getenv(old_name)
        if raw is not None and raw.strip():
            source_name = old_name
    if raw is None or not raw.strip():
        return default
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError(f"{source_name} must be an integer byte count") from exc
    if limit < 1:
        raise ValueError(f"{source_name} must be greater than 0")
    return limit


def get_chat_upload_max_bytes() -> int:
    return read_byte_limit_env(CHAT_UPLOAD_MAX_BYTES_ENV, DEFAULT_CHAT_UPLOAD_MAX_BYTES)


# Per-route upload byte-limits, single-sourced here (issue #3364). Each is
# validated + env-overridable via read_byte_limit_env: set the matching
# RESTIA_*_MAX_BYTES env var to an integer byte count to tune it; an invalid
# value fails fast at import rather than crashing mid-request. Defaults match
# the prior per-route values, so behavior is unchanged unless an env var is set.
GALLERY_UPLOAD_MAX_BYTES = read_byte_limit_env(
    "RESTIA_GALLERY_UPLOAD_MAX_BYTES", 100 * 1024 * 1024
)
GALLERY_TRANSFORM_UPLOAD_MAX_BYTES = read_byte_limit_env(
    "RESTIA_GALLERY_TRANSFORM_UPLOAD_MAX_BYTES", 25 * 1024 * 1024
)
MEMORY_IMPORT_MAX_BYTES = read_byte_limit_env(
    "RESTIA_MEMORY_IMPORT_MAX_BYTES", 10 * 1024 * 1024
)
PERSONAL_UPLOAD_MAX_BYTES = read_byte_limit_env(
    "RESTIA_PERSONAL_UPLOAD_MAX_BYTES", 25 * 1024 * 1024
)
EMAIL_COMPOSE_UPLOAD_MAX_BYTES = read_byte_limit_env(
    "RESTIA_EMAIL_COMPOSE_UPLOAD_MAX_BYTES", 25 * 1024 * 1024
)
STT_MAX_AUDIO_BYTES = read_byte_limit_env(
    "RESTIA_STT_MAX_AUDIO_BYTES", 25 * 1024 * 1024
)
ICS_MAX_BYTES = read_byte_limit_env(
    "RESTIA_ICS_MAX_BYTES", 10 * 1024 * 1024
)
PROJECT_ATTACHMENT_MAX_BYTES = read_byte_limit_env(
    "RESTIA_PROJECT_ATTACHMENT_MAX_BYTES", 50 * 1024 * 1024
)
PROJECT_STORAGE_MAX_BYTES = read_byte_limit_env(
    "RESTIA_PROJECT_STORAGE_MAX_BYTES", 5 * 1024 * 1024 * 1024
)
PROJECT_OWNER_STORAGE_MAX_BYTES = read_byte_limit_env(
    "RESTIA_PROJECT_OWNER_STORAGE_MAX_BYTES", 50 * 1024 * 1024 * 1024
)
PROJECT_GLOBAL_STORAGE_MAX_BYTES = read_byte_limit_env(
    "RESTIA_PROJECT_GLOBAL_STORAGE_MAX_BYTES", 200 * 1024 * 1024 * 1024
)
PROJECT_MAX_PROJECTS_PER_OWNER = read_byte_limit_env(
    "RESTIA_PROJECT_MAX_PROJECTS_PER_OWNER", 200
)
PROJECT_MAX_STAGES_PER_PROJECT = read_byte_limit_env(
    "RESTIA_PROJECT_MAX_STAGES_PER_PROJECT", 50
)
PROJECT_MAX_ACTIVITY_PER_PROJECT = read_byte_limit_env(
    "RESTIA_PROJECT_MAX_ACTIVITY_PER_PROJECT", 20_000
)
PROJECT_MAX_ATTACHMENTS_PER_ITEM = read_byte_limit_env(
    "RESTIA_PROJECT_MAX_ATTACHMENTS_PER_ITEM", 200
)
PROJECT_MAX_ATTACHMENTS_PER_PROJECT = read_byte_limit_env(
    "RESTIA_PROJECT_MAX_ATTACHMENTS_PER_PROJECT", 5_000
)
PROJECT_MAX_ACTIVE_ITEMS = read_byte_limit_env(
    "RESTIA_PROJECT_MAX_ACTIVE_ITEMS", 2_000
)
PROJECT_MAX_ITEMS = read_byte_limit_env(
    "RESTIA_PROJECT_MAX_ITEMS", 10_000
)
PROJECT_MAX_CHECKLIST_ITEMS_PER_ITEM = read_byte_limit_env(
    "RESTIA_PROJECT_MAX_CHECKLIST_ITEMS_PER_ITEM", 500
)
PROJECT_MAX_COMMENTS_PER_ITEM = read_byte_limit_env(
    "RESTIA_PROJECT_MAX_COMMENTS_PER_ITEM", 1_000
)


async def read_upload_limited(upload: UploadFile, limit: int, label: str = "Upload") -> bytes:
    """Read an UploadFile with a hard byte cap."""
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"{label} exceeds {format_byte_limit(limit)} limit",
        )
    return data
