"""Study Mode stays on the ordinary chat path and persists its own mode.

The streaming handler is intentionally large and tightly coupled to providers,
session managers, research, tools, and SSE.  These tests execute the small mode
normalization expressions extracted from its AST, then use structural checks
only for the two control-flow invariants that are impractical to drive without
mocking the entire endpoint.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path


_CHAT_ROUTES = Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"
_CHAT_HELPERS = Path(__file__).resolve().parents[1] / "routes" / "chat_helpers.py"


def _chat_stream_tree():
    source = _CHAT_ROUTES.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_CHAT_ROUTES))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat_stream"
    )
    return source, function


def _assigns_name(node: ast.AST, name: str, value=...):
    for child in ast.walk(node):
        if not isinstance(child, (ast.Assign, ast.AnnAssign)):
            continue
        targets = child.targets if isinstance(child, ast.Assign) else [child.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        assigned = child.value
        if value is ...:
            return True
        if isinstance(assigned, ast.Constant) and assigned.value == value:
            return True
    return False


def _load_request_mode_parser():
    """Execute only the handler's request-mode normalization statements."""

    _, function = _chat_stream_tree()
    selected = []
    wanted_assignments = {"preset_id", "chat_mode", "study_mode"}
    found_assignments = set()

    for statement in function.body:
        if isinstance(statement, ast.Assign):
            names = {
                target.id
                for target in statement.targets
                if isinstance(target, ast.Name)
            }
            wanted = names & wanted_assignments
            if wanted and not (wanted & found_assignments):
                selected.append(copy.deepcopy(statement))
                found_assignments.update(wanted)
                continue

        if not isinstance(statement, ast.If):
            continue
        condition = ast.unparse(statement.test)
        if "chat_mode not in" in condition:
            selected.append(copy.deepcopy(statement))
        elif condition == "study_mode" and _assigns_name(statement, "chat_mode", "chat"):
            selected.append(copy.deepcopy(statement))

    assert found_assignments == wanted_assignments
    assert len(selected) == 5

    loader = ast.FunctionDef(
        name="parse_request_mode",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="form_data"), ast.arg(arg="body")],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=selected
        + [
            ast.Return(
                value=ast.Tuple(
                    elts=[
                        ast.Name(id="chat_mode", ctx=ast.Load()),
                        ast.Name(id="study_mode", ctx=ast.Load()),
                        ast.Name(id="preset_id", ctx=ast.Load()),
                    ],
                    ctx=ast.Load(),
                )
            )
        ],
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[loader], type_ignores=[]))
    namespace = {}
    exec(compile(module, str(_CHAT_ROUTES), "exec"), namespace)
    return namespace["parse_request_mode"]


def _load_effective_mode():
    """Execute the production expression that chooses the persisted mode."""

    _, function = _chat_stream_tree()
    assignment = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_effective_mode"
            for target in node.targets
        )
    )
    loader = ast.FunctionDef(
        name="effective_mode",
        args=ast.arguments(
            posonlyargs=[],
            args=[
                ast.arg(arg="effective_do_research"),
                ast.arg(arg="study_mode"),
                ast.arg(arg="chat_mode"),
            ],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=[ast.Return(value=copy.deepcopy(assignment.value))],
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[loader], type_ignores=[]))
    namespace = {}
    exec(compile(module, str(_CHAT_ROUTES), "exec"), namespace)
    return namespace["effective_mode"]


def _parents(root: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(root)
        for child in ast.iter_child_nodes(parent)
    }


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_stream_accepts_study_flag_from_form_and_json_and_clears_preset():
    parse = _load_request_mode_parser()

    assert parse(
        {"mode": "agent", "study_mode": "true", "preset_id": "character-1"},
        None,
    ) == ("chat", True, None)
    assert parse(
        {},
        {"mode": "agent", "study_mode": True, "preset_id": "character-1"},
    ) == ("chat", True, None)


def test_stream_clamps_unknown_modes_instead_of_falling_into_agent():
    parse = _load_request_mode_parser()

    assert parse({"mode": "study", "preset_id": "p1"}, None) == (
        "chat",
        False,
        "p1",
    )
    assert parse({"mode": "something-unsafe"}, None) == ("chat", False, None)
    assert parse({"mode": "agent", "preset_id": "p1"}, None) == (
        "agent",
        False,
        "p1",
    )


def test_every_chat_to_agent_promotion_is_disabled_for_study_mode():
    """AST is deliberate: the promotions sit inside a huge streaming route."""

    _, function = _chat_stream_tree()
    parents = _parents(function)
    assignments = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "chat_mode"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and node.value.value == "agent"
    ]
    assert len(assignments) >= 3

    plan_mode_is_disabled = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "plan_mode"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and node.value.value is False
        for node in ast.walk(function)
    )
    assert plan_mode_is_disabled

    unsafe = []
    for assignment in assignments:
        conditions = []
        parent = parents.get(assignment)
        while parent is not None:
            if isinstance(parent, ast.If):
                conditions.append(ast.unparse(parent.test))
            parent = parents.get(parent)
        if not any("not study_mode" in condition for condition in conditions):
            # The only exception is the dead legacy plan-mode branch, whose
            # input is hard-clamped to False immediately above normalization.
            if "plan_mode" not in conditions:
                unsafe.append((assignment.lineno, conditions))

    assert not unsafe, f"unguarded chat-to-agent assignments: {unsafe}"


def test_study_chat_branch_cannot_reach_stream_agent_loop():
    """The real agent call must remain exclusively in the chat branch's else."""

    _, function = _chat_stream_tree()
    agent_call = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and _call_name(node) == "stream_agent_loop"
    )
    chat_branch = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If)
        and ast.unparse(node.test).replace('"', "'") == "chat_mode == 'chat'"
        and any(
            child is agent_call
            for statement in node.orelse
            for child in ast.walk(statement)
        )
    )

    assert any(
        isinstance(node, ast.Call) and _call_name(node) == "stream_llm_with_fallback"
        for statement in chat_branch.body
        for node in ast.walk(statement)
    )
    assert not any(
        isinstance(node, ast.Call) and _call_name(node) == "stream_agent_loop"
        for statement in chat_branch.body
        for node in ast.walk(statement)
    )


def test_study_mode_is_forwarded_to_context_and_persisted():
    effective_mode = _load_effective_mode()
    assert effective_mode(False, True, "chat") == "study"
    assert effective_mode(False, False, "agent") == "agent"
    assert effective_mode(False, False, "chat") == "chat"

    _, function = _chat_stream_tree()
    build_context = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and _call_name(node) == "build_chat_context"
    )
    keyword = next(item for item in build_context.keywords if item.arg == "study_mode")
    assert isinstance(keyword.value, ast.Name) and keyword.value.id == "study_mode"

    parents = _parents(function)
    persist_call = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and _call_name(node) == "set_session_mode"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Name)
        and node.args[1].id == "_effective_mode"
    )
    parent = parents.get(persist_call)
    while parent is not None and not isinstance(parent, ast.If):
        parent = parents.get(parent)
    assert isinstance(parent, ast.If)
    assert "'study'" in ast.unparse(parent.test).replace('"', "'")


def test_study_context_uses_the_current_chat_session_id():
    source = _CHAT_HELPERS.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_CHAT_HELPERS))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "build_chat_context"
    )
    call = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and _call_name(node) == "study_context_messages_for_owner"
    )

    assert [ast.unparse(argument) for argument in call.args] == ["user", "session_id"]


def test_research_pending_auto_trigger_is_disabled_for_study_mode():
    """A stale research-pending session must not override the Study contract."""

    _, function = _chat_stream_tree()
    pending_check = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If)
        and any(
            isinstance(call, ast.Call) and _call_name(call) == "get_session_mode"
            for call in ast.walk(node.test)
        )
    )
    parents = _parents(function)
    guard = parents.get(pending_check)
    while guard is not None and not isinstance(guard, ast.If):
        guard = parents.get(guard)

    assert isinstance(guard, ast.If)
    assert "not study_mode" in ast.unparse(guard.test)
