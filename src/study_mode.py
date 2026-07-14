"""Persistent Study Mode state and its built-in teaching contract."""

from __future__ import annotations

import json
from datetime import datetime
from functools import wraps
from threading import RLock
from typing import Optional
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from core.database import SessionLocal, StudyState, utcnow_naive


LOCAL_OWNER_KEY = "local:default"
DEFAULT_STUDY_GOAL = (
    "Build deep, transferable mastery through first-principles explanations, "
    "retrieval, derivation, and deliberate practice."
)
DEFAULT_TARGET_MINUTES = 60
_STATE_LOCK = RLock()


class StudyGoalConflictError(ValueError):
    """Raised when replacing a goal would discard logged effort implicitly."""


class StudyGoalRequiredError(ValueError):
    """Raised when a focus timer is started before a goal exists."""


def _serialized_state_access(function):
    """Keep owner-state initialization and timer mutations ordered in-process."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with _STATE_LOCK:
            return function(*args, **kwargs)

    return wrapped


def owner_key(owner: Optional[str]) -> str:
    """Return a stable, non-secret primary key for an owner scope."""

    value = str(owner or "").strip()
    return f"user:{value}" if value else LOCAL_OWNER_KEY


def _now() -> datetime:
    """Clock seam kept small so timer behavior is deterministic in tests."""

    return utcnow_naive()


def _find_state(db, owner: Optional[str]) -> Optional[StudyState]:
    """Find the owner's row by mutable owner identity, not its creation key.

    Profile rename migrates every ``owner`` column transactionally. Looking up
    authenticated rows by that column means a rename cannot orphan Study data
    even though the row's primary key still reflects its original username.
    """

    value = str(owner or "").strip()
    if value:
        return db.query(StudyState).filter(StudyState.owner == value).first()
    return (
        db.query(StudyState)
        .filter(StudyState.id == LOCAL_OWNER_KEY, StudyState.owner.is_(None))
        .first()
    )


def _next_state_key(db, owner: Optional[str]) -> str:
    """Return the normal owner key, or a collision-safe replacement."""

    key = owner_key(owner)
    if db.query(StudyState).filter(StudyState.id == key).first() is not None:
        key = f"{key}:{uuid4().hex}"
    return key


def _apply_starter_goal(state: StudyState) -> bool:
    """Repair legacy/empty rows so Study Mode is usable on first open."""

    changed = False
    if not (state.goal_text or "").strip():
        state.goal_text = DEFAULT_STUDY_GOAL
        changed = True
    if int(state.target_minutes or 0) <= 0:
        state.target_minutes = DEFAULT_TARGET_MINUTES
        changed = True
    return changed


def _get_or_create(db, owner: Optional[str]) -> StudyState:
    state = _find_state(db, owner)
    if state is not None:
        _apply_starter_goal(state)
        return state

    value = str(owner or "").strip()
    state = StudyState(
        id=_next_state_key(db, owner),
        owner=value or None,
        goal_text=DEFAULT_STUDY_GOAL,
        target_minutes=DEFAULT_TARGET_MINUTES,
    )
    db.add(state)
    try:
        db.flush()
    except IntegrityError:
        # Two tabs can initialize Study Mode at the same time. The deterministic
        # primary key makes one insert win; recover the committed owner row
        # rather than surfacing a misleading 500 to the second tab.
        db.rollback()
        state = _find_state(db, owner)
        if state is None:
            raise
        _apply_starter_goal(state)
    return state


def _timer_seconds(state: StudyState, now: Optional[datetime] = None) -> int:
    elapsed = max(0, int(state.current_session_seconds or 0))
    if state.timer_running and state.timer_started_at:
        current = now or _now()
        elapsed += max(0, int((current - state.timer_started_at).total_seconds()))
    return elapsed


def serialize_study_state(state: Optional[StudyState], now: Optional[datetime] = None) -> dict:
    """Serialize stored state with live timer and goal-distance calculations."""

    if state is None:
        return {
            "goal_text": "",
            "target_minutes": 0,
            "target_date": None,
            "timer_running": False,
            "timer_seconds": 0,
            "total_seconds": 0,
            "studied_seconds": 0,
            "remaining_seconds": 0,
            "progress_percent": 0.0,
        }

    timer_seconds = _timer_seconds(state, now=now)
    total_seconds = max(0, int(state.total_seconds or 0))
    studied_seconds = total_seconds + timer_seconds
    target_seconds = max(0, int(state.target_minutes or 0)) * 60
    remaining_seconds = max(0, target_seconds - studied_seconds)
    progress_percent = (
        min(100.0, round((studied_seconds / target_seconds) * 100, 1))
        if target_seconds
        else 0.0
    )
    return {
        "goal_text": state.goal_text or "",
        "target_minutes": max(0, int(state.target_minutes or 0)),
        "target_date": state.target_date or None,
        "timer_running": bool(state.timer_running),
        "timer_seconds": timer_seconds,
        "total_seconds": total_seconds,
        "studied_seconds": studied_seconds,
        "remaining_seconds": remaining_seconds,
        "progress_percent": progress_percent,
    }


@_serialized_state_access
def get_study_state(owner: Optional[str]) -> dict:
    db = SessionLocal()
    try:
        state = _get_or_create(db, owner)
        db.commit()
        db.refresh(state)
        return serialize_study_state(state)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_serialized_state_access
def save_study_goal(
    owner: Optional[str],
    goal_text: str,
    target_minutes: int,
    target_date: Optional[str],
    reset_progress: bool = False,
) -> dict:
    db = SessionLocal()
    try:
        state = _get_or_create(db, owner)
        cleaned_goal = goal_text.strip()
        goal_changed = (state.goal_text or "").strip() != cleaned_goal
        has_progress = bool(
            state.timer_running
            or int(state.current_session_seconds or 0) > 0
            or int(state.total_seconds or 0) > 0
        )
        if goal_changed and has_progress and not reset_progress:
            raise StudyGoalConflictError(
                "Starting a new goal requires confirmation because it resets logged study effort."
            )
        if reset_progress or goal_changed:
            state.total_seconds = 0
            state.current_session_seconds = 0
            state.timer_started_at = None
            state.timer_running = False
        state.goal_text = cleaned_goal
        state.target_minutes = int(target_minutes)
        state.target_date = target_date or None
        db.commit()
        db.refresh(state)
        return serialize_study_state(state)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_serialized_state_access
def start_study_timer(owner: Optional[str]) -> dict:
    """Start or resume the focus timer; repeated starts are idempotent."""

    db = SessionLocal()
    try:
        state = _get_or_create(db, owner)
        if state is None or not (state.goal_text or "").strip() or int(state.target_minutes or 0) <= 0:
            raise StudyGoalRequiredError("Set a study goal before starting the focus timer.")
        if not state.timer_running:
            state.timer_started_at = _now()
            state.timer_running = True
        db.commit()
        db.refresh(state)
        return serialize_study_state(state)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_serialized_state_access
def pause_study_timer(owner: Optional[str]) -> dict:
    """Pause without losing the current focus block."""

    db = SessionLocal()
    try:
        state = _get_or_create(db, owner)
        now = _now()
        if state.timer_running:
            state.current_session_seconds = _timer_seconds(state, now=now)
            state.timer_started_at = None
            state.timer_running = False
        db.commit()
        db.refresh(state)
        return serialize_study_state(state, now=now)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_serialized_state_access
def finish_study_timer(owner: Optional[str]) -> dict:
    """Commit the current focus block to goal progress and reset the clock."""

    db = SessionLocal()
    try:
        state = _get_or_create(db, owner)
        now = _now()
        completed = _timer_seconds(state, now=now)
        if completed:
            state.total_seconds = max(0, int(state.total_seconds or 0)) + completed
        state.current_session_seconds = 0
        state.timer_started_at = None
        state.timer_running = False
        db.commit()
        db.refresh(state)
        return serialize_study_state(state, now=now)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def build_study_system_prompt() -> str:
    """Return the non-optional teaching behavior for a Study Mode turn."""

    return f"""You are Restia's Study Mode tutor: demanding, patient, precise, and deeply interactive.

TEACHING METHOD
1. Start a new topic by finding the learner's current level, desired depth, and prior mental model. Ask at most two focused diagnostic questions when that information is missing.
2. Use a Feynman loop: explain one idea in plain language, ask the learner to restate or apply it, expose the exact gap, then rebuild the explanation from first principles.
3. Use Lewin-style physical and experimental intuition: connect abstractions to observable phenomena, units, limiting cases, thought experiments, and simple demonstrations. For quantitative topics, derive important equations instead of presenting them as magic.
4. Teach in short conceptual chunks. Do not dump a complete textbook chapter in one reply. After a meaningful chunk, ask one specific retrieval, prediction, derivation, or transfer question and wait for the learner.
5. Go deep when the learner is ready: definitions, causal mechanism, derivation, worked example, common misconception, and a novel application. Never trade correctness for a catchy analogy; state where an analogy breaks.
6. Prefer hints before solutions. When an answer is wrong, identify the misconception without shaming, give the smallest useful hint, and make the learner try again. Do not answer your own check question immediately unless asked or the learner is stuck.
7. Revisit weak ideas with spaced retrieval and interleave them with the current topic. Periodically ask the learner for a one-minute teach-back and use it as evidence of understanding.
8. End natural milestones with a compact mastery check: what is understood, what is still weak, and the best next exercise. The timer measures effort, not mastery; never claim the learner understands something merely because time was logged.

STYLE
- Be energetic and intellectually serious, not theatrical.
- Use clear headings only when they aid learning, clean notation, and concrete examples.
- Adapt difficulty from the learner's answers. Challenge them just beyond their current level.
- Keep the conversation centered on the stated goal, while allowing necessary prerequisite detours.
"""


def study_context_messages_for_owner(owner: Optional[str]) -> list[dict]:
    """Return trusted teaching rules plus untrusted user-owned goal context.

    Goal text is editable user input, so it must never be interpolated into the
    system message. The standard untrusted-context wrapper keeps it available
    to the tutor without letting goal text redefine the teaching contract.
    """

    from src.prompt_security import untrusted_context_message

    state = get_study_state(owner)
    context = {
        "goal": state.get("goal_text") or None,
        "target_minutes": state.get("target_minutes") or 0,
        "target_date": state.get("target_date"),
        "focused_minutes": round((state.get("studied_seconds") or 0) / 60, 1),
        "remaining_minutes": round((state.get("remaining_seconds") or 0) / 60, 1),
        "effort_progress_percent": state.get("progress_percent") or 0,
        "progress_note": "Time measures effort, not demonstrated mastery.",
    }
    return [
        {"role": "system", "content": build_study_system_prompt()},
        untrusted_context_message(
            "learner-provided Study Mode goal and focus progress",
            json.dumps(context, ensure_ascii=False),
        ),
    ]
