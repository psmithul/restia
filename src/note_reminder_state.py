"""Shared durable reminder cancellation/rearm coordination for Note mutations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.auth_helpers import resolved_runtime_owner
from src.constants import DATA_DIR


def _paths(scheduler: Any = None) -> tuple[Path, Path]:
    outbox = Path(
        getattr(
            scheduler,
            "_notification_outbox_path",
            Path(DATA_DIR) / "browser_notification_outbox.sqlite3",
        )
    )
    claims = Path(
        getattr(
            scheduler,
            "_reminder_claim_path",
            Path(DATA_DIR) / "reminder_delivery_claims.sqlite3",
        )
    )
    return outbox, claims


def cancel_note_reminder(
    owner: str | None,
    note_id: str,
    *,
    occurrence: str | None = None,
    scheduler: Any = None,
) -> dict[str, int]:
    """Cancel one occurrence, or every occurrence when ``occurrence`` is None."""

    from src.browser_notification_outbox import cancel_browser_notifications_for_reminder
    from src.reminder_delivery_claims import cancel_reminder_deliveries

    concrete_owner = resolved_runtime_owner(owner)
    outbox_path, claim_path = _paths(scheduler)
    outbox = cancel_browser_notifications_for_reminder(
        outbox_path,
        concrete_owner,
        note_id,
        occurrence=occurrence,
    )
    claims = cancel_reminder_deliveries(
        claim_path,
        owner=concrete_owner,
        note_id=note_id,
        occurrence=occurrence,
    )
    return {"outbox": outbox, "claims": claims}


def rearm_note_reminder(
    owner: str | None,
    note_id: str,
    *,
    occurrence: str | None = None,
    scheduler: Any = None,
) -> dict[str, int]:
    """Re-enable a deliberately restored/rescheduled reminder."""

    from src.browser_notification_outbox import rearm_browser_notifications_for_reminder
    from src.reminder_delivery_claims import rearm_reminder_deliveries

    concrete_owner = resolved_runtime_owner(owner)
    outbox_path, claim_path = _paths(scheduler)
    outbox = rearm_browser_notifications_for_reminder(
        outbox_path,
        concrete_owner,
        note_id,
        occurrence=occurrence,
    )
    claims = rearm_reminder_deliveries(
        claim_path,
        owner=concrete_owner,
        note_id=note_id,
        occurrence=occurrence,
    )
    return {"outbox": outbox, "claims": claims}
