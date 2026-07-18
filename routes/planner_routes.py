"""Planner API: goal -> researched plan artifact + notes/todos/calendar updates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
import zipfile
from datetime import datetime, timedelta
from html import escape as _xml_escape
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core.database import Note, SessionLocal
from src.auth_helpers import DEFAULT_LOCAL_OWNER, require_user
from src.constants import DATA_DIR
from src.identity import request_account_transaction
from src.planning import create_planning_item

logger = logging.getLogger(__name__)


class PlannerRequest(BaseModel):
    goal: str
    format: str = Field(default="md", description="md or docx")
    update_notes: bool = True
    update_tasks: bool = True
    update_calendar: bool = True
    web_research: bool = True
    horizon_days: int = Field(default=14, ge=1, le=120)


def _owner(request: Request) -> str:
    return require_user(request) or DEFAULT_LOCAL_OWNER


def _safe_slug(text: str, *, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (slug[:max_len].strip("-") or "plan")


def _clean_line(text: Any, fallback: str = "") -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value or fallback


def _fallback_plan(goal: str, horizon_days: int) -> dict[str, Any]:
    short = _clean_line(goal, "Goal")
    return {
        "summary": f"Make measurable progress on: {short}",
        "research": [
            "Clarify the target outcome, deadline, constraints, and success metric.",
            "Identify the strongest existing examples or benchmarks before committing to execution.",
            "List dependencies, blockers, and decisions that need outside information.",
        ],
        "strategy": (
            "Use a short discovery pass, choose the highest-leverage route, then execute in "
            "visible milestones with a review checkpoint before the horizon ends."
        ),
        "tasks": [
            {"text": "Define the finished-state metric and non-negotiable constraints", "due_in_days": 0},
            {"text": "Collect current references, requirements, and missing facts", "due_in_days": 1},
            {"text": "Compare at least three execution paths and choose one", "due_in_days": 2},
            {"text": "Break the chosen path into milestone-sized work blocks", "due_in_days": 3},
            {"text": "Execute the first milestone and record the result", "due_in_days": 5},
            {"text": "Review progress, prune low-value work, and schedule the next block", "due_in_days": min(horizon_days, 7)},
        ],
        "calendar": [
            {"summary": "Planner kickoff", "day_offset": 1, "duration_minutes": 45},
            {"summary": "Planner review", "day_offset": min(horizon_days, 7), "duration_minutes": 30},
        ],
    }


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _normalize_plan(raw: dict[str, Any], fallback: dict[str, Any], goal: str, horizon_days: int) -> dict[str, Any]:
    out = dict(fallback)
    if isinstance(raw.get("summary"), str):
        out["summary"] = _clean_line(raw["summary"], fallback["summary"])
    if isinstance(raw.get("strategy"), str):
        out["strategy"] = _clean_line(raw["strategy"], fallback["strategy"])
    if isinstance(raw.get("research"), list):
        research = [_clean_line(item) for item in raw["research"] if _clean_line(item)]
        if research:
            out["research"] = research[:8]
    tasks = []
    for item in raw.get("tasks") or []:
        if isinstance(item, str):
            text = _clean_line(item)
            due = None
        elif isinstance(item, dict):
            text = _clean_line(item.get("text") or item.get("title") or item.get("task"))
            due = item.get("due_in_days")
        else:
            continue
        if not text:
            continue
        try:
            due_i = max(0, min(horizon_days, int(due))) if due is not None else None
        except Exception:
            due_i = None
        tasks.append({"text": text, "due_in_days": due_i})
    if tasks:
        out["tasks"] = tasks[:20]
    events = []
    for item in raw.get("calendar") or []:
        if not isinstance(item, dict):
            continue
        summary = _clean_line(item.get("summary") or item.get("title"))
        if not summary:
            continue
        try:
            day_offset = max(0, min(horizon_days, int(item.get("day_offset", 1))))
        except Exception:
            day_offset = 1
        try:
            duration = max(15, min(240, int(item.get("duration_minutes", 30))))
        except Exception:
            duration = 30
        events.append({"summary": summary, "day_offset": day_offset, "duration_minutes": duration})
    if events:
        out["calendar"] = events[:8]
    out["goal"] = goal
    return out


async def _research_goal(goal: str, owner: str, horizon_days: int, web_research: bool) -> tuple[str, str]:
    if not web_research:
        return "", "skipped"
    try:
        from src.deep_research import IterativeResearcher
        from src.endpoint_resolver import resolve_endpoint

        endpoint, model, headers = resolve_endpoint("research", owner=owner)
        if not endpoint or not model:
            return "", "no_model"
        researcher = IterativeResearcher(
            endpoint,
            model,
            headers,
            max_rounds=1,
            min_rounds=1,
            max_time=75,
            max_urls_per_round=2,
            max_content_chars=6000,
            max_report_tokens=2500,
            extraction_timeout=35,
            planning_timeout=25,
            query_timeout=35,
            extraction_concurrency=2,
        )
        question = (
            f"Research the best practical way to accomplish this goal within about "
            f"{horizon_days} days. Focus on concrete constraints, risks, and execution options.\n\nGoal: {goal}"
        )
        report = await asyncio.wait_for(researcher.research(question), timeout=90)
        return report.strip(), "completed" if report.strip() else "empty"
    except asyncio.TimeoutError:
        logger.warning("planner research timed out")
        return "", "timeout"
    except Exception as exc:
        logger.warning("planner research failed: %s", exc)
        return "", "failed"


async def _optimize_plan(goal: str, owner: str, horizon_days: int, research_report: str) -> dict[str, Any]:
    fallback = _fallback_plan(goal, horizon_days)
    try:
        from src.endpoint_resolver import resolve_endpoint
        from src.llm_core import llm_call_async

        endpoint, model, headers = resolve_endpoint("task", owner=owner)
        if not endpoint or not model:
            return fallback
        prompt = (
            "Return ONLY valid JSON for an execution plan. Shape:\n"
            "{\n"
            '  "summary": "one sentence",\n'
            '  "research": ["3-8 concrete research findings or assumptions"],\n'
            '  "strategy": "one paragraph optimized approach",\n'
            '  "tasks": [{"text": "actionable checklist item", "due_in_days": 0}],\n'
            '  "calendar": [{"summary": "event title", "day_offset": 1, "duration_minutes": 30}]\n'
            "}\n"
            "Make tasks specific, ordered, and followable. Use due_in_days within the requested horizon."
        )
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    f"Goal: {goal}\nHorizon days: {horizon_days}\n\n"
                    f"Research report:\n{research_report[:6000] if research_report else '(no external research available)'}"
                ),
            },
        ]
        response = await asyncio.wait_for(
            llm_call_async(endpoint, model, messages, headers=headers, max_tokens=2200, timeout=60, workload="background"),
            timeout=70,
        )
        parsed = _extract_json_object(response)
        if parsed:
            return _normalize_plan(parsed, fallback, goal, horizon_days)
    except Exception as exc:
        logger.warning("planner optimization failed: %s", exc)
    return fallback


def _render_markdown(goal: str, plan: dict[str, Any], research_report: str, research_status: str) -> str:
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        f"# Plan: {goal}",
        "",
        f"Generated: {generated}",
        "",
        "## Optimized Goal",
        "",
        plan.get("summary") or goal,
        "",
        "## Research Snapshot",
        "",
    ]
    for item in plan.get("research") or []:
        lines.append(f"- {item}")
    if research_report:
        lines.extend(["", "### External Research Notes", "", research_report.strip()])
    else:
        lines.extend(["", f"_External research status: {research_status}._"])
    lines.extend(["", "## Strategy", "", plan.get("strategy") or "", "", "## Followable Tasks", ""])
    for idx, item in enumerate(plan.get("tasks") or [], start=1):
        due = item.get("due_in_days")
        due_label = f" (day {due})" if due is not None else ""
        lines.append(f"{idx}. [ ] {item.get('text')}{due_label}")
    lines.extend(["", "## Calendar Checkpoints", ""])
    for item in plan.get("calendar") or []:
        lines.append(f"- Day {item.get('day_offset', 1)}: {item.get('summary')} ({item.get('duration_minutes', 30)} min)")
    lines.append("")
    return "\n".join(lines)


def _write_docx(path: Path, markdown: str) -> None:
    paragraphs = []
    for raw in markdown.splitlines():
        text = raw.strip()
        if not text:
            paragraphs.append("<w:p/>")
            continue
        if text.startswith("# "):
            text = text[2:].strip()
        elif text.startswith("## "):
            text = text[3:].strip()
        elif text.startswith("### "):
            text = text[4:].strip()
        paragraphs.append(
            "<w:p><w:r><w:t xml:space=\"preserve\">"
            + _xml_escape(text)
            + "</w:t></w:r></w:p>"
        )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(paragraphs)
        + "<w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\"/><w:pgMar w:top=\"1440\" w:right=\"1440\" w:bottom=\"1440\" w:left=\"1440\"/></w:sectPr>"
        + "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        docx.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        docx.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        docx.writestr("word/document.xml", document_xml)


def _artifact_response_path(path: Path) -> str:
    return f"/api/planner/artifacts/{path.name}"


def _artifact_owner_dir(plans_dir: Path, owner: str) -> Path:
    """Use a collision-resistant, non-identifying directory per profile."""

    normalized = str(owner or DEFAULT_LOCAL_OWNER).strip().lower() or DEFAULT_LOCAL_OWNER
    readable = _safe_slug(normalized.split("@", 1)[0], max_len=20)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return plans_dir / f"{readable}-{digest}"


def _create_records(
    *,
    request: Request,
    goal: str,
    plan: dict[str, Any],
    artifact_path: Path,
    update_notes: bool,
    update_tasks: bool,
    update_calendar: bool,
) -> dict[str, Any]:
    created: dict[str, Any] = {
        "note_id": None,
        "todo_note_id": None,
        "planning_item_ids": [],
        "calendar_event_ids": [],
    }
    db = SessionLocal()
    try:
        with request_account_transaction(db, request, write=True) as account:
            # The resolved Account username is the compatibility owner alias
            # for the still-legacy Notes/Planning tables.  The calendar itself
            # is always owned by immutable Account.id.
            concrete_owner = account.username
            if update_notes:
                note = Note(
                    id=str(uuid.uuid4()),
                    owner=concrete_owner,
                    title=f"Plan: {goal[:90]}",
                    content=f"{plan.get('summary') or goal}\n\nArtifact: {artifact_path}",
                    note_type="note",
                    label="planner",
                    source="agent",
                    sort_order=0,
                )
                db.add(note)
                created["note_id"] = note.id
            if update_tasks:
                today = datetime.now().astimezone().date()
                items = []
                for task in plan.get("tasks") or []:
                    text = _clean_line(task.get("text")) if isinstance(task, dict) else ""
                    if not text:
                        continue
                    due_in_days = task.get("due_in_days")
                    due_date = None
                    if due_in_days is not None:
                        due_date = (today + timedelta(days=int(due_in_days))).isoformat()
                    items.append({
                        "id": str(uuid.uuid4())[:8],
                        "text": text,
                        "done": False,
                        "due_in_days": due_in_days,
                        "due_date": due_date,
                    })
                    planning = create_planning_item(
                        db,
                        owner=concrete_owner,
                        title=text,
                        due_date=due_date,
                        source="planner",
                    )
                    created["planning_item_ids"].append(planning.id)
                todo = Note(
                    id=str(uuid.uuid4()),
                    owner=concrete_owner,
                    title=f"Tasks: {goal[:90]}",
                    content="Generated by Planner",
                    items=json.dumps(items),
                    note_type="todo",
                    label="planner",
                    due_date=next((item["due_date"] for item in items if item["due_date"]), None),
                    source="agent",
                    sort_order=0,
                )
                db.add(todo)
                created["todo_note_id"] = todo.id
            if update_calendar:
                from src.action_policy import create_action_proposal
                from src.calendar_action_executor import execute_calendar_action
                from src.calendar_service import ensure_default_calendar

                cal = ensure_default_calendar(db, account=account)
                base = datetime.now().replace(second=0, microsecond=0)
                artifact_key = hashlib.sha256(
                    str(artifact_path).encode("utf-8")
                ).hexdigest()
                for index, item in enumerate(plan.get("calendar") or []):
                    day_offset = int(item.get("day_offset", 1) or 1)
                    duration = int(item.get("duration_minutes", 30) or 30)
                    start = (base + timedelta(days=day_offset)).replace(hour=9, minute=0)
                    if day_offset == 0 and start <= base:
                        start = base + timedelta(hours=1)
                    proposal = create_action_proposal(
                        db,
                        owner_id=account.id,
                        domain="calendar",
                        action="create_event",
                        autonomy_level=4,
                        target_type="event",
                        payload={
                            "calendar_id": cal.id,
                            "summary": _clean_line(
                                item.get("summary"), "Planner checkpoint"
                            ),
                            "description": (
                                f"Planner checkpoint for: {goal}\n\n"
                                f"Artifact: {artifact_path}"
                            ),
                            "dtstart": start.isoformat(),
                            "dtend": (
                                start + timedelta(minutes=duration)
                            ).isoformat(),
                            "all_day": False,
                            "event_type": "admin",
                            "importance": "normal",
                        },
                        reason="Planner checkpoint requested by the user",
                        sources={
                            "interface": "web",
                            "source": "planner",
                            "artifact": artifact_key,
                        },
                        external=False,
                        idempotency_key=f"planner:{artifact_key}:{index}",
                    ).proposal
                    executed = execute_calendar_action(
                        db,
                        account=account,
                        proposal_id=proposal.id,
                        expected_version=int(proposal.version or 1),
                    )
                    created["calendar_event_ids"].append(
                        executed.mutation.event.uid
                    )
            db.flush()
            return created
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def setup_planner_routes() -> APIRouter:
    router = APIRouter(prefix="/api/planner", tags=["planner"])
    plans_dir = Path(DATA_DIR) / "plans"

    @router.post("/run")
    async def run_planner(request: Request, body: PlannerRequest):
        owner = _owner(request)
        goal = _clean_line(body.goal)
        if not goal:
            raise HTTPException(400, "goal is required")
        fmt = (body.format or "md").strip().lower()
        if fmt not in {"md", "docx"}:
            raise HTTPException(400, "format must be md or docx")
        research_report, research_status = await _research_goal(goal, owner, body.horizon_days, body.web_research)
        plan = await _optimize_plan(goal, owner, body.horizon_days, research_report)
        markdown = _render_markdown(goal, plan, research_report, research_status)
        owner_plans_dir = _artifact_owner_dir(plans_dir, owner)
        owner_plans_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = owner_plans_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_safe_slug(goal)}-{str(uuid.uuid4())[:8]}.{fmt}"
        if fmt == "docx":
            _write_docx(artifact_path, markdown)
        else:
            artifact_path.write_text(markdown, encoding="utf-8")
        created = _create_records(
            request=request,
            goal=goal,
            plan=plan,
            artifact_path=artifact_path,
            update_notes=body.update_notes,
            update_tasks=body.update_tasks,
            update_calendar=body.update_calendar,
        )
        return {
            "ok": True,
            "goal": goal,
            "format": fmt,
            "artifact_path": str(artifact_path),
            "artifact_url": _artifact_response_path(artifact_path),
            "research_status": research_status,
            "summary": plan.get("summary"),
            **created,
        }

    @router.get("/artifacts/{filename}")
    def get_artifact(request: Request, filename: str):
        owner = _owner(request)
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise HTTPException(404, "Artifact not found")
        path = _artifact_owner_dir(plans_dir, owner) / filename
        if not path.exists() or not path.is_file():
            raise HTTPException(404, "Artifact not found")
        media_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if path.suffix.lower() == ".docx"
            else "text/markdown; charset=utf-8"
        )
        return FileResponse(path, media_type=media_type, filename=path.name)

    return router
