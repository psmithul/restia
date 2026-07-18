"""Shared durable reminder cancellation/rearm coordination for Note mutations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.auth_helpers import resolved_runtime_owner
from src.constants import DATA_DIR


def _paths(scheduler: Any = None) -> tuple[Path | None, Path | None]:
    # An initialized scheduler deliberately sets both values to ``None`` to
    # select the canonical SQL authority.  The explicit paths remain only for
    # isolated legacy-compatibility tests and the bounded migration importer.
    if scheduler is not None:
        outbox_value = getattr(scheduler, "_notification_outbox_path", None)
        claims_value = getattr(scheduler, "_reminder_claim_path", None)
        outbox = Path(outbox_value) if outbox_value is not None else None
        claims = Path(claims_value) if claims_value is not None else None
        return outbox, claims
    outbox = Path(DATA_DIR) / "browser_notification_outbox.sqlite3"
    claims = Path(DATA_DIR) / "reminder_delivery_claims.sqlite3"
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
