"""Pin the model contract required by version-fenced calendar writes."""


def _manage_calendar_schema():
    # Import through the facade so ToolBlock/TOOL_TAGS are initialized before
    # the canonical schema module is loaded (the production import order).
    from src.agent_tools import FUNCTION_TOOL_SCHEMAS

    return next(
        row["function"]
        for row in FUNCTION_TOOL_SCHEMAS
        if row["function"]["name"] == "manage_calendar"
    )


def test_manage_calendar_schema_advertises_version_fenced_updates():
    function = _manage_calendar_schema()
    properties = function["parameters"]["properties"]
    version = properties["version"]
    assert version["type"] == "integer"
    assert version["minimum"] == 1
    assert "Required for update_event and cancel_event" in version["description"]
    assert "uid and version" in function["description"]
    assert "cancel_event" in properties["action"]["enum"]
    assert "never executes" in function["description"]
    assert "never" in function["description"]
    assert "approval token" in function["description"]
    assert properties["linked_entity_ids"] == {
        "type": "array",
        "maxItems": 50,
        "items": {"type": "string", "minLength": 1, "maxLength": 255},
        "description": (
            "Owner-scoped Life Graph entity IDs to link to the event, such as a "
            "person, project, note, file, previous meeting, decision, or follow-up task."
        ),
    }


def test_manage_calendar_schema_has_no_hidden_reminder_write_contract():
    function = _manage_calendar_schema()
    properties = function["parameters"]["properties"]
    assert "reminder_minutes" not in properties
    assert "separate explicit manage_notes action" in function["description"]
