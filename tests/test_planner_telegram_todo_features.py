import ast
import asyncio
import zipfile
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]


def _function_source(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.unparse(node)
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name:
                    return ast.unparse(child)
    raise AssertionError(f"{name} not found in {path}")


def test_planner_router_is_mounted():
    src = (REPO / "app.py").read_text(encoding="utf-8")

    assert "from routes.planner_routes import setup_planner_routes" in src
    assert "app.include_router(setup_planner_routes())" in src


def test_planner_docx_writer_creates_valid_package(tmp_path):
    from routes.planner_routes import _write_docx

    out = tmp_path / "plan.docx"
    _write_docx(out, "# Plan\n\n## Followable Tasks\n\n1. [ ] First task")

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert "[Content_Types].xml" in names
        assert "_rels/.rels" in names
        assert "word/document.xml" in names
        document = zf.read("word/document.xml").decode("utf-8")

    assert "Plan" in document
    assert "First task" in document


def test_planner_preserves_task_due_dates_and_creates_planning_items():
    src = (REPO / "routes" / "planner_routes.py").read_text(encoding="utf-8")

    assert 'note_type="todo"' in src
    assert '"due_in_days": due_in_days' in src
    assert '"due_date": due_date' in src
    assert "create_planning_item(" in src


def test_planner_artifact_directories_are_owner_isolated(tmp_path):
    from routes.planner_routes import _artifact_owner_dir

    alice = _artifact_owner_dir(tmp_path, "alice@example.test")
    bob = _artifact_owner_dir(tmp_path, "bob@example.test")

    assert alice.parent == tmp_path
    assert bob.parent == tmp_path
    assert alice != bob
    assert "example.test" not in alice.name
    assert "example.test" not in bob.name


def test_note_update_accepts_explicit_null_due_date():
    src = (REPO / "routes" / "note_routes.py").read_text(encoding="utf-8")

    assert "model_fields_set" in src
    assert 'if "due_date" in fields_set:' in src
    assert "note.due_date = normalize_notification_due_date(user, body.due_date)" in src


def test_telegram_digest_seeded_hourly_and_registered():
    actions = (REPO / "src" / "builtin_actions.py").read_text(encoding="utf-8")
    scheduler = (REPO / "src" / "task_scheduler.py").read_text(encoding="utf-8")

    assert '"telegram_hourly_digest": action_telegram_hourly_digest' in actions
    assert '"telegram_hourly_digest": "Send an hourly Telegram digest' in actions
    assert '"telegram_hourly_digest": {"name": "Telegram Hourly Digest"' in scheduler
    assert '"cron_expression": "0 * * * *"' in scheduler
    assert '"telegram_hourly_digest": {"name": "Telegram Hourly Digest",  "schedule": "cron",  "scheduled_time": None,    "cron_expression": "0 * * * *", "ship_paused": True' in scheduler


def test_telegram_digest_reads_local_email_index_not_imap():
    src = _function_source(REPO / "src" / "builtin_actions.py", "action_telegram_hourly_digest")

    assert "email_message_index" in src
    assert "_imap_connect" not in src


@pytest.mark.asyncio
async def test_telegram_digest_noops_when_telegram_disabled(monkeypatch):
    from src.builtin_actions import TaskNoop, action_telegram_hourly_digest
    from src.telegram_bot import TelegramConfig

    monkeypatch.setattr(
        "src.telegram_bot.load_telegram_config",
        lambda: TelegramConfig(
            enabled=False,
            bot_token="",
            webhook_secret="",
            allowed_chat_ids=frozenset(),
            allow_all_chats=False,
            owner=None,
            session_map={},
        ),
    )

    with pytest.raises(TaskNoop):
        await action_telegram_hourly_digest("alice")
