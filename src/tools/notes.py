"""Notes-domain tool implementations.

Extracted from tool_implementations.py as part of slice 1 (#4082/#4071).
Holds the manage_notes tool (notes + checklists CRUD).
``src.tool_implementations`` re-exports these for backward compatibility.
"""
import json
import logging
import re
from typing import Dict, Optional

from src.tools._common import _parse_tool_args

logger = logging.getLogger(__name__)


async def do_manage_notes(content: str, owner: Optional[str] = None) -> Dict:
    """Handle manage_notes tool calls: CRUD on notes and checklists."""
    import uuid as _uuid
    from core.database import SessionLocal, Note
    from sqlalchemy.orm.attributes import flag_modified

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}

    # Action aliases — match what models actually emit. `create` is the most
    # common alternative to `add`. Hyphenated forms also accepted.
    raw_action = (args.get("action") or "").replace("-", "_").strip().lower()
    action = raw_action
    _NOTE_ACTION_ALIASES = {
        "create": "add",
        "new": "add",
        "save": "add",
        "remind": "add",
        "remove": "delete",
        "remove_item": "toggle_item",
    }
    action = _NOTE_ACTION_ALIASES.get(action, action)
    db = SessionLocal()

    # The UI splits notes into tabs by exact note_type: 'note' (freeform,
    # Notes tab) vs 'todo'/'goal' (checkbox items, To-dos tab). 'checklist'
    # is a legacy synonym for 'todo' that no tab matches — normalize it and
    # the other names models emit so agent-created items are always visible.
    _TODO_LIKE = {"todo", "goal", "checklist"}
    _NOTE_TYPE_ALIASES = {
        "checklist": "todo",
        "todos": "todo",
        "task": "todo",
        "tasks": "todo",
        "to_do": "todo",
        "to-do": "todo",
        "reminder": "todo",
        "notes": "note",
        "text": "note",
    }

    def _norm_note_type(value) -> Optional[str]:
        text = str(value or "").strip().lower()
        if not text:
            return None
        text = _NOTE_TYPE_ALIASES.get(text, text)
        return text if text in ("note", "todo", "goal") else None

    def _norm_repeat(value) -> Optional[str]:
        """Normalize a repeat rule to the grammar the reminder engine knows:
        none/daily/yearly/weekly[:W]/monthly:day:D etc. Natural phrases like
        'every day' map to their canonical form; unknown values are rejected
        so a bad rule can't silently create a reminder that never repeats."""
        text = str(value or "").strip().lower().replace("every ", "")
        if not text:
            return None
        aliases = {
            "day": "daily", "daily": "daily",
            "week": "weekly", "weekly": "weekly",
            "month": "monthly", "monthly": "monthly",
            "year": "yearly", "yearly": "yearly", "annually": "yearly",
            "none": "none", "off": "none", "never": "none",
        }
        if text in aliases:
            return aliases[text]
        if re.fullmatch(r"weekly:[0-6]", text):
            return text
        if re.fullmatch(r"monthly:day:\d{1,2}|monthly:nth:[1-4]:[0-6]|monthly:last:[0-6]", text):
            return text
        return None

    def _norm_note_title(value: str) -> str:
        text = (value or "").strip().lower()
        text = re.sub(r"^\s*reminder\s*:\s*", "", text)
        return re.sub(r"\s+", " ", text)

    def _note_visible_to_owner(note, owner_value: Optional[str]) -> bool:
        # Empty owner_value is single-user / auth-disabled mode. A real
        # authenticated owner must match exactly; null/empty legacy rows are not
        # shared between accounts.
        if not owner_value:
            return True
        return getattr(note, "owner", None) == owner_value

    def _note_by_prefix(note_id: str):
        if not note_id:
            return None
        q = db.query(Note).filter(Note.id.startswith(note_id))
        if owner:
            q = q.filter(Note.owner == owner)
        return q.first()

    def _format_note_list(notes) -> str:
        lines = []
        for n in notes:
            pin = " [PINNED]" if n.pinned else ""
            typ = f" [{n.note_type}]" if n.note_type in _TODO_LIKE else ""
            lbl = f" #{n.label}" if n.label else ""
            title = n.title or "(untitled)"
            lines.append(f"- [{n.id[:8]}] **{title}**{pin}{typ}{lbl}")
            if n.note_type in _TODO_LIKE and n.items:
                try:
                    items = json.loads(n.items)
                    for i, item in enumerate(items):
                        mark = "x" if item.get("done") else " "
                        lines.append(f"  [{mark}] {i}: {item.get('text', '')}")
                except (json.JSONDecodeError, TypeError):
                    pass
            elif n.content:
                snippet = n.content[:80].replace("\n", " ")
                lines.append(f"  {snippet}")
        return "\n".join(lines)

    try:
        if action in ("list", "search", "find"):
            q = db.query(Note)
            if owner is not None:
                q = q.filter(Note.owner == owner)
            label_filter = str(args.get("label") or "").strip()
            if label_filter and label_filter.lower() != "default":
                q = q.filter(Note.label == label_filter)
            type_filter = _norm_note_type(args.get("note_type"))
            if type_filter == "todo":
                # Include legacy 'checklist' rows under the todo umbrella so
                # older data doesn't silently vanish from filtered listings.
                q = q.filter(Note.note_type.in_(["todo", "checklist"]))
            elif type_filter:
                q = q.filter(Note.note_type == type_filter)
            show_archived = args.get("archived", False)
            q = q.filter(Note.archived == show_archived)
            notes = q.order_by(Note.pinned.desc(), Note.updated_at.desc()).all()
            if action in ("search", "find"):
                query = str(
                    args.get("query")
                    or args.get("text")
                    or args.get("title")
                    or args.get("content")
                    or ""
                ).strip().lower()
                if query:
                    filtered = []
                    for n in notes:
                        haystack = " ".join(
                            str(part or "")
                            for part in (n.title, n.content, n.label, n.items)
                        ).lower()
                        if query in haystack:
                            filtered.append(n)
                    notes = filtered
            if not notes:
                return {"response": "No notes found.", "exit_code": 0}
            return {"results": _format_note_list(notes), "exit_code": 0}

        elif action == "view":
            note_id = args.get("id", "")
            note = _note_by_prefix(note_id)
            if not note:
                return {"error": f"Note '{note_id}' not found", "exit_code": 1}
            if not _note_visible_to_owner(note, owner):
                return {"error": "Note not found", "exit_code": 1}
            return {"results": _format_note_list([note]), "exit_code": 0}

        elif action == "add":
            # Accept the various field names models emit: `text` is the most
            # common stand-in for "title or body content" when the model
            # treats the note as a single string. If text was supplied and
            # neither title nor content, use it as the title.
            title = (args.get("title") or "").strip()
            content_raw = args.get("content")
            text_raw = args.get("text") or args.get("body")
            if not title and not content_raw and text_raw:
                title = text_raw.strip()
            elif not content_raw and text_raw:
                content_raw = text_raw
            # Accept both `items` (legacy/internal field) and `checklist_items`
            # (the schema-exposed name used by native function calls). Models
            # following the schema emit `checklist_items`; older code paths
            # and direct API callers still use `items`.
            items_raw = args.get("checklist_items")
            if items_raw is None:
                items_raw = args.get("items")
            # Models sometimes pass checklist items as plain strings instead of
            # {text, done} objects — accept both.
            if isinstance(items_raw, list):
                items_raw = [
                    it if isinstance(it, dict) else {"text": str(it), "done": False}
                    for it in items_raw
                ]
            note_type = _norm_note_type(args.get("note_type"))
            if note_type is None:
                note_type = "todo" if items_raw else "note"
            # Reconcile type with payload so nothing renders blank in the UI:
            # a todo/goal card shows only its checkbox items, a note card shows
            # only its content text.
            if note_type == "note" and items_raw:
                note_type = "todo"
            if note_type in ("todo", "goal") and not items_raw and content_raw:
                items_raw = [
                    {"text": line.strip().lstrip("-*•").strip(), "done": False}
                    for line in str(content_raw).splitlines()
                    if line.strip().lstrip("-*•").strip()
                ]
                content_raw = None
            items_json = json.dumps(items_raw) if items_raw is not None else None
            # Accept natural-language due_date ("tomorrow at 1pm") in
            # addition to ISO. Use the user-tz-aware parser so the LLM's
            # naive times ("today at 9pm") are anchored to the USER's clock,
            # not the server's. Returns ISO with explicit offset so frontend
            # `new Date()` resolves the right absolute moment regardless of
            # where the user is.
            due_raw = args.get("due_date")
            if not due_raw:
                combined_text = " ".join(
                    str(v or "")
                    for v in (title, content_raw, text_raw)
                ).strip()
                lower_combined = combined_text.lower()
                looks_like_reminder = (
                    raw_action in {"remind", "reminder"}
                    or re.search(r"\bremind(?:er)?\b", lower_combined)
                )
                if looks_like_reminder:
                    temporal = re.search(
                        r"\b(?:today|tonight|tomorrow|tmrw|yesterday)\b(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?"
                        r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s+(?:today|tonight|tomorrow|tmrw|yesterday)\b"
                        r"|\bin\s+\d+\s*(?:hour|hr|minute|min|day)s?\b",
                        lower_combined,
                    )
                    if temporal:
                        due_raw = temporal.group(0)
            due_iso = None
            if due_raw:
                try:
                    from routes.calendar_routes import parse_due_for_user as _pdt_user
                    due_iso = _pdt_user(due_raw)
                except Exception:
                    due_iso = due_raw  # fall through; trust the model
            if due_iso and title:
                # Calendar event reminders are represented as Notes. If the
                # model creates a calendar event with reminder_minutes and then
                # also creates a separate note reminder for the same title/time,
                # keep the existing note so the user gets only one dispatch.
                existing_q = db.query(Note).filter(
                    Note.archived == False,  # noqa: E712
                    Note.due_date == due_iso,
                )
                if owner is not None:
                    existing_q = existing_q.filter(Note.owner == owner)
                target_title = _norm_note_title(title)
                for existing in existing_q.limit(25).all():
                    if _norm_note_title(existing.title or "") == target_title:
                        return {
                            "response": f"Reminder already exists: \"{existing.title or title}\" (id: {existing.id[:8]})",
                            "note_id": existing.id,
                            "duplicate": True,
                            "exit_code": 0,
                        }
            repeat_norm = _norm_repeat(args.get("repeat"))
            note = Note(
                id=str(_uuid.uuid4()),
                owner=owner,
                title=title,
                content=content_raw,
                items=items_json,
                note_type=note_type,
                color=args.get("color"),
                label=args.get("label"),
                pinned=args.get("pinned", False),
                due_date=due_iso,
                repeat=repeat_norm or "none",
                source="agent",
                session_id=args.get("session_id"),
            )
            db.add(note)
            db.commit()
            # Return note_id so the chat-side renderer can build a real
            # "View note" button that opens the notes modal at this id.
            # Previously the create response only included a prose
            # confirmation; the model would type "View note" as a markdown
            # link with no target, leaving the user with a click that
            # did nothing and uncertainty about whether the note was made.
            return {
                "response": f"{'Reminder' if due_iso else {'todo': 'To-do', 'goal': 'Goal'}.get(note_type, 'Note')} created: \"{title or '(untitled)'}\" (id: {note.id[:8]})",
                "note_id": note.id,
                "note_title": title or "",
                "open_url": f"/#open=notes&note={note.id}",
                "exit_code": 0,
            }

        elif action == "update":
            note_id = args.get("id", "")
            note = _note_by_prefix(note_id)
            if not note:
                return {"error": f"Note '{note_id}' not found", "exit_code": 1}
            if not _note_visible_to_owner(note, owner):
                return {"error": "Note not found", "exit_code": 1}
            for field in ("title", "content", "color", "label"):
                if field in args and args[field] is not None:
                    setattr(note, field, args[field])
            if args.get("note_type") is not None:
                new_type = _norm_note_type(args["note_type"])
                if new_type:
                    note.note_type = new_type
            if args.get("repeat") is not None:
                new_repeat = _norm_repeat(args["repeat"])
                if new_repeat:
                    note.repeat = new_repeat
            # Parse due_date the same way the `add` action does. The schema
            # advertises natural language ("tomorrow at 9am"), and naive ISO
            # strings need the user's tz offset attached so the frontend's
            # `new Date()` resolves the right absolute moment. Storing the raw
            # value here left updated reminders as unparseable literals that
            # never fired.
            if args.get("due_date") is not None:
                due_raw = args["due_date"]
                try:
                    from routes.calendar_routes import parse_due_for_user as _pdt_user
                    note.due_date = _pdt_user(due_raw)
                except Exception:
                    note.due_date = due_raw  # fall through; trust the model
            new_items = args.get("checklist_items")
            if new_items is None:
                new_items = args.get("items")
            if new_items is not None:
                note.items = json.dumps(new_items)
                flag_modified(note, "items")
            if "pinned" in args:
                note.pinned = args["pinned"]
            if "archived" in args:
                note.archived = args["archived"]
            db.commit()
            return {"response": f"Note updated: \"{note.title or '(untitled)'}\"", "exit_code": 0}

        elif action == "delete":
            note_id = args.get("id", "")
            note = _note_by_prefix(note_id)
            if not note:
                return {"error": f"Note '{note_id}' not found", "exit_code": 1}
            if not _note_visible_to_owner(note, owner):
                return {"error": "Note not found", "exit_code": 1}
            title = note.title
            db.delete(note)
            db.commit()
            return {"response": f"Deleted note: \"{title or '(untitled)'}\"", "exit_code": 0}

        elif action == "toggle_item":
            note_id = args.get("id", "")
            index = args.get("index", 0)
            note = _note_by_prefix(note_id)
            if not note:
                return {"error": f"Note '{note_id}' not found", "exit_code": 1}
            if not _note_visible_to_owner(note, owner):
                return {"error": "Note not found", "exit_code": 1}
            if not note.items:
                return {"error": "Note has no checklist items", "exit_code": 1}
            items = json.loads(note.items)
            if index < 0 or index >= len(items):
                return {"error": f"Item index {index} out of range (0-{len(items)-1})", "exit_code": 1}
            items[index]["done"] = not items[index].get("done", False)
            note.items = json.dumps(items)
            flag_modified(note, "items")
            db.commit()
            mark = "done" if items[index]["done"] else "undone"
            return {"response": f"Item '{items[index].get('text', '')}' marked {mark}", "exit_code": 0}

        else:
            return {"error": f"Unknown action: {action}. Use list/search/view/add/update/delete/toggle_item", "exit_code": 1}
    except Exception as e:
        logger.error(f"manage_notes error: {e}")
        return {"error": str(e), "exit_code": 1}
    finally:
        db.close()
