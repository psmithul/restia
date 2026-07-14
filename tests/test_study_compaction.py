"""Study Mode compaction preserves teaching state and exact recent history."""

import asyncio

import pytest

from core.models import (
    ChatMessage,
    Session,
    get_session_manager_instance,
    set_session_manager_instance,
)
from src import context_compactor as cc
from src.prompt_security import GUARD_CLOSE, GUARD_OPEN, untrusted_context_message


STUDY_SYSTEM = (
    "You are Restia's Study Mode tutor: teach interactively and do not reveal "
    "the answer before the learner attempts the challenge."
)


class _RecordingManager:
    def __init__(self, session, *, succeeds=True):
        self.session = session
        self.succeeds = succeeds
        self.calls = []
        self.expected_snapshots = []

    def replace_messages(self, session_id, messages, *, expected_history_snapshot=None):
        self.calls.append((session_id, list(messages)))
        self.expected_snapshots.append(expected_history_snapshot)
        if not self.succeeds:
            return False
        self.session.history = list(messages)
        self.session.message_count = len(messages)
        return True


@pytest.fixture(autouse=True)
def _restore_session_manager():
    previous = get_session_manager_instance()
    set_session_manager_instance(None)
    try:
        yield
    finally:
        set_session_manager_instance(previous)


def _session_with_turns(count=8):
    history = [
        ChatMessage(
            role="user" if index % 2 == 0 else "assistant",
            content=f"H{index}",
            metadata={"_db_id": f"db-{index}"},
        )
        for index in range(count)
    ]
    return Session(
        id="study-session",
        name="Study",
        endpoint_url="http://local/v1/chat/completions",
        model="local-model",
        history=history,
        message_count=len(history),
    )


def _assembled_messages(session):
    history = session.get_context_messages()
    goal = untrusted_context_message(
        "learner-provided Study Mode goal",
        "DYNAMIC-GOAL-MARKER master control theory",
    )
    messages = [
        {"role": "system", "content": STUDY_SYSTEM},
        {"role": "system", "content": "DYNAMIC-SAFETY-MARKER"},
        goal,
        *history,
    ]
    # This is how chat_helpers inserts the changing time context: between the
    # prior assistant turn and the latest persisted user turn.
    messages.insert(
        len(messages) - 1,
        untrusted_context_message("current date and time", "DYNAMIC-TIME-MARKER"),
    )
    return messages, history


def _force_compaction(monkeypatch, summaries, captured_calls):
    summary_iter = iter(summaries)

    async def _fake_summary(*args, **kwargs):
        captured_calls.append(args[2])
        return next(summary_iter)

    monkeypatch.setattr(cc, "get_context_length", lambda *_: 100)
    monkeypatch.setattr(cc, "estimate_tokens", lambda _messages: 10_000)
    monkeypatch.setattr(cc, "resolve_endpoint", lambda *_args, **_kwargs: (None, None, None))
    monkeypatch.setattr(cc, "llm_call_async", _fake_summary)


def _compact(session, messages):
    return asyncio.run(
        cc.maybe_compact(
            session,
            session.endpoint_url,
            session.model,
            messages,
            session.headers,
        )
    )


def test_dynamic_preface_compacts_exact_persisted_tail(monkeypatch):
    """Regression: dynamic systems used to skew the database slice by 2+ turns."""

    session = _session_with_turns()
    original_history = list(session.history)
    messages, history_dicts = _assembled_messages(session)
    manager = _RecordingManager(session)
    set_session_manager_instance(manager)
    captured_calls = []
    _force_compaction(monkeypatch, ["structured-study-summary"], captured_calls)

    compacted, context_length, was_compacted = _compact(session, messages)

    assert context_length == 100
    assert was_compacted is True
    assert len(manager.calls) == 1

    # The exact recent half survives in the request, including the turns that
    # the old system-message offset bug silently deleted from persistence.
    for recent in history_dicts[4:]:
        assert any(message is recent for message in compacted)
    compacted_ids = [
        (message.get("metadata") or {}).get("_db_id") for message in compacted
        if (message.get("metadata") or {}).get("_db_id")
    ]
    assert compacted_ids == ["db-4", "db-5", "db-6", "db-7"]
    assert all(f"db-{index}" not in compacted_ids for index in range(4))

    persisted = manager.calls[0][1]
    assert persisted[1:] == original_history[4:]
    assert all(
        message is expected
        for message, expected in zip(persisted[1:], original_history[4:])
    )
    assert [message.content for message in persisted[1:]] == ["H4", "H5", "H6", "H7"]
    assert session.history == persisted


def test_study_summary_is_guarded_and_excludes_dynamic_context(monkeypatch):
    session = _session_with_turns()
    messages, _ = _assembled_messages(session)
    manager = _RecordingManager(session)
    set_session_manager_instance(manager)
    captured_calls = []
    _force_compaction(monkeypatch, ["structured-study-summary"], captured_calls)

    compacted, _context_length, was_compacted = _compact(session, messages)

    assert was_compacted is True
    assert len(captured_calls) == 1
    summarizer_messages = captured_calls[0]
    study_prompt = summarizer_messages[0]["content"]
    for heading in (
        "### Learning Goal",
        "### Current Concept",
        "### Prerequisite Map",
        "### Open Challenge",
        "### Latest Learner Attempt",
        "### Diagnosed Misconception",
        "### Evidence Ledger",
        "### Hint Level",
        "### Calibration and Transfer",
        "### Review Schedule",
        "### Withheld Answer",
        "### Next Teaching Move",
    ):
        assert heading in study_prompt
    assert "Never include, derive, or reveal the answer" in study_prompt

    source_message = summarizer_messages[1]
    assert source_message["role"] == "user"
    assert source_message["metadata"]["trusted"] is False
    assert GUARD_OPEN in source_message["content"]
    assert GUARD_CLOSE in source_message["content"]
    for marker in ("DYNAMIC-GOAL-MARKER", "DYNAMIC-SAFETY-MARKER", "DYNAMIC-TIME-MARKER"):
        assert marker not in source_message["content"]
    for older_turn in ("H0", "H1", "H2", "H3"):
        assert older_turn in source_message["content"]

    continuity = next(
        message for message in compacted
        if (message.get("metadata") or {}).get("study_summary")
    )
    assert continuity["role"] == "user"
    assert continuity["_protected"] is True
    assert continuity["metadata"] == {
        "trusted": False,
        "source": "non-authoritative Study Mode continuity summary",
        "compacted": True,
        "hidden_from_user_view": True,
        "study_summary": True,
    }
    assert GUARD_OPEN in continuity["content"]
    assert "structured-study-summary" in continuity["content"]

    persisted_summary = manager.calls[0][1][0]
    assert persisted_summary.role == "user"
    assert persisted_summary.metadata["trusted"] is False
    assert persisted_summary.metadata["hidden_from_user_view"] is True
    assert persisted_summary.metadata["summarized_count"] == 4


def test_repeated_study_compaction_keeps_one_rolling_summary(monkeypatch):
    session = _session_with_turns()
    manager = _RecordingManager(session)
    set_session_manager_instance(manager)
    captured_calls = []
    _force_compaction(monkeypatch, ["first-summary", "second-summary"], captured_calls)

    first_messages, _ = _assembled_messages(session)
    first_out, _, first_compacted = _compact(session, first_messages)
    assert first_compacted is True
    assert sum(
        1 for message in first_out
        if (message.get("metadata") or {}).get("study_summary")
    ) == 1

    second_messages, _ = _assembled_messages(session)
    second_out, _, second_compacted = _compact(session, second_messages)
    assert second_compacted is True
    assert sum(
        1 for message in second_out
        if (message.get("metadata") or {}).get("study_summary")
    ) == 1
    persisted_summaries = [
        message for message in session.history
        if (message.metadata or {}).get("study_summary")
    ]
    assert len(persisted_summaries) == 1
    assert "second-summary" in persisted_summaries[0].content
    assert [message.content for message in session.history[1:]] == ["H5", "H6", "H7"]


def test_repeated_compaction_preserves_complete_study_ledger(monkeypatch):
    session = _session_with_turns()
    manager = _RecordingManager(session)
    set_session_manager_instance(manager)
    captured_calls = []
    long_summary = (
        "## Study Continuity\n"
        "### Learning Goal\nmaster control theory\n"
        + ("x" * 2600)
        + "\n### Prerequisite Map\nstate-space before observers; state-space clean"
        "\n### Evidence Ledger\nindependent pole-placement derivation; observers weak"
        + "\n### Hint Level\n2; governing principle already given"
        "\n### Calibration and Transfer\n80% confidence; near transfer passed; novel transfer untested"
        "\n### Review Schedule\nobservers due 2026-07-15T09:00:00Z"
        "\n### Withheld Answer\nWITHHELD"
        "\n### Next Teaching Move\nask for the learner's next substep"
    )
    _force_compaction(monkeypatch, [long_summary, "second-summary"], captured_calls)

    first_messages, _ = _assembled_messages(session)
    _compact(session, first_messages)
    second_messages, _ = _assembled_messages(session)
    _compact(session, second_messages)

    second_source = captured_calls[1][1]["content"]
    assert "### Prerequisite Map\nstate-space before observers" in second_source
    assert "### Evidence Ledger\nindependent pole-placement derivation" in second_source
    assert "### Hint Level\n2; governing principle already given" in second_source
    assert "### Calibration and Transfer\n80% confidence" in second_source
    assert "### Review Schedule\nobservers due 2026-07-15" in second_source
    assert "### Withheld Answer\nWITHHELD" in second_source
    assert "### Next Teaching Move\nask for the learner's next substep" in second_source


def test_failed_atomic_history_replace_keeps_original_messages(monkeypatch):
    session = _session_with_turns()
    original_history = list(session.history)
    messages, _ = _assembled_messages(session)
    manager = _RecordingManager(session, succeeds=False)
    set_session_manager_instance(manager)
    captured_calls = []
    _force_compaction(monkeypatch, ["unused-summary"], captured_calls)

    compacted, _context_length, was_compacted = _compact(session, messages)

    assert was_compacted is False
    assert compacted is messages
    assert session.history == original_history
    assert all(
        actual is expected
        for actual, expected in zip(session.history, original_history)
    )


def test_concurrent_same_count_edit_aborts_compaction_before_replace(monkeypatch):
    session = _session_with_turns()
    messages, _ = _assembled_messages(session)
    manager = _RecordingManager(session)
    set_session_manager_instance(manager)
    captured_calls = []
    _force_compaction(monkeypatch, ["unused-summary"], captured_calls)

    async def _summary_then_edit(*args, **kwargs):
        captured_calls.append(args[2])
        session.history[0].content = "edited while summary was running"
        return "summary from stale history"

    monkeypatch.setattr(cc, "llm_call_async", _summary_then_edit)

    compacted, _context_length, was_compacted = _compact(session, messages)

    assert was_compacted is False
    assert compacted is messages
    assert manager.calls == []
    assert session.history[0].content == "edited while summary was running"


def test_reloaded_study_summary_survives_last_resort_trimming():
    summary = {
        "role": "user",
        "content": "guarded continuity state",
        "metadata": {
            "compacted": True,
            "trusted": False,
            "study_summary": True,
            "hidden_from_user_view": True,
        },
    }
    messages = [
        {"role": "system", "content": "Study Mode tutor"},
        summary,
        *[
            {"role": "user", "content": f"old-{index} " + ("x" * 1000)}
            for index in range(12)
        ],
        {"role": "user", "content": "current challenge"},
    ]

    trimmed = cc.trim_for_context(messages, context_length=1024, reserve_tokens=256)

    assert summary in trimmed
    assert any(message.get("content") == "current challenge" for message in trimmed)
