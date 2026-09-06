"""Tests for parse_comment_command functionality."""

from typing import Any

import pytest

from forge.workflow.utils import parse_comment_command


def test_parse_remove_command_success() -> None:
    """Test successful parsing of remove command."""
    result = parse_comment_command("/forge remove 2")
    assert result == {"command": "remove", "id": 2}

    # Case insensitivity
    result = parse_comment_command("  /FORGE Remove 42  ")
    assert result == {"command": "remove", "id": 42}


def test_parse_remove_command_failures() -> None:
    """Test parsing failures of remove command."""
    # Missing ID
    result = parse_comment_command("/forge remove")
    assert result is not None
    assert "error" in result
    assert result["command"] == "remove"

    # Invalid ID (string)
    result = parse_comment_command("/forge remove abc")
    assert result is not None
    assert "error" in result
    assert result["command"] == "remove"

    # Invalid ID (negative)
    result = parse_comment_command("/forge remove -5")
    assert result is not None
    assert "error" in result
    assert result["command"] == "remove"


def test_parse_exclude_command_success() -> None:
    """Test successful parsing of exclude command."""
    result = parse_comment_command("/forge exclude 3")
    assert result == {"command": "exclude", "id": 3}


def test_parse_exclude_command_failures() -> None:
    """Test parsing failures of exclude command."""
    result = parse_comment_command("/forge exclude")
    assert result is not None
    assert "error" in result
    assert result["command"] == "exclude"

    result = parse_comment_command("/forge exclude xyz")
    assert result is not None
    assert "error" in result
    assert result["command"] == "exclude"


def test_parse_approve_command() -> None:
    """Test parsing the draft approval command."""
    assert parse_comment_command("/forge approve") == {"command": "approve"}
    assert parse_comment_command("  /FORGE approve  ") == {"command": "approve"}
    assert parse_comment_command("/forge approve 1") == {
        "command": "approve",
        "error": "The approve command does not accept parameters",
    }


def test_parse_add_command_success() -> None:
    """Test successful parsing of add command."""
    result = parse_comment_command(
        '/forge add summary="Implement API" repo="core-api" description="Set up endpoints"'
    )
    assert result == {
        "command": "add",
        "params": {
            "summary": "Implement API",
            "repo": "core-api",
            "description": "Set up endpoints",
        },
    }

    # Mix of double, single and no quotes
    result = parse_comment_command("/forge add summary='test single' count=42 name=\"quoted\"")
    assert result == {
        "command": "add",
        "params": {
            "summary": "test single",
            "count": "42",
            "name": "quoted",
        },
    }


def test_parse_add_command_failures() -> None:
    """Test parsing failures of add command."""
    # Missing parameters
    result = parse_comment_command("/forge add")
    assert result is not None
    assert "error" in result
    assert result["command"] == "add"

    # Malformed parameter (no key)
    result = parse_comment_command("/forge add =value")
    assert result is not None
    assert "error" in result
    assert result["command"] == "add"

    # Malformed parameters (trailing junk)
    result = parse_comment_command('/forge add key="value" junk')
    assert result is not None
    assert "error" in result
    assert result["command"] == "add"


def test_parse_update_command_success() -> None:
    """Test successful parsing of update command."""
    result = parse_comment_command('/forge update 1 summary="New Summary"')
    assert result == {
        "command": "update",
        "id": 1,
        "params": {"summary": "New Summary"},
    }

    result = parse_comment_command("/forge update 100")
    assert result == {
        "command": "update",
        "id": 100,
        "params": {},
    }


def test_parse_update_command_failures() -> None:
    """Test parsing failures of update command."""
    # Missing everything
    result = parse_comment_command("/forge update")
    assert result is not None
    assert "error" in result
    assert result["command"] == "update"

    # Missing ID but has parameters
    result = parse_comment_command('/forge update summary="test"')
    assert result is not None
    assert "error" in result
    assert result["command"] == "update"

    # Invalid ID
    result = parse_comment_command('/forge update abc summary="test"')
    assert result is not None
    assert "error" in result
    assert result["command"] == "update"

    # Malformed parameters
    result = parse_comment_command('/forge update 1 summary="test" junk')
    assert result is not None
    assert "error" in result
    assert result["command"] == "update"


def test_parse_command_non_matching() -> None:
    """Test that unrelated texts or other /forge commands return None."""
    assert parse_comment_command("/forge skip-gate build") is None
    assert parse_comment_command("/forge unskip-gate test") is None
    assert parse_comment_command("/forge rebase") is None
    assert parse_comment_command("/forge foo") is None
    assert parse_comment_command("?what is this?") is None
    assert parse_comment_command("!please update") is None
    assert parse_comment_command("") is None


@pytest.fixture
def sample_draft_json() -> list[dict[str, Any]]:
    return [
        {
            "id": 1,
            "summary": "Implement login route",
            "description": "Create a POST route for user login",
            "repo": "auth-api",
            "acceptance_criteria": ["POST /login returns JWT on success"],
            "excluded": False,
        },
        {
            "id": 2,
            "summary": "Implement signup route",
            "description": "Create a POST route for user registration",
            "repo": "auth-api",
            "acceptance_criteria": ["POST /signup registers user"],
            "excluded": False,
        },
        {
            "id": 3,
            "summary": "Add database migration",
            "description": "Write migration script for users table",
            "repo": "db-migration",
            "acceptance_criteria": ["Users table has id, email, password"],
            "excluded": True,
        },
    ]


def test_apply_draft_modification_remove_success(sample_draft_json) -> None:
    """Test successful removal and re-sequencing."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {"command": "remove", "id": 2}
    result = DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)

    assert len(result) == 2
    # Verify remaining items are re-sequenced
    assert result[0]["id"] == 1
    assert result[0]["summary"] == "Implement login route"
    assert result[1]["id"] == 2
    assert result[1]["summary"] == "Add database migration"


def test_apply_draft_modification_remove_missing_id(sample_draft_json) -> None:
    """Test that removal fails if ID is missing."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {"command": "remove"}
    with pytest.raises(ValueError, match="Missing ID for removal"):
        DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)


def test_apply_draft_modification_remove_not_found(sample_draft_json) -> None:
    """Test that removal fails if ID is not found."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {"command": "remove", "id": 99}
    with pytest.raises(ValueError, match="Item with ID 99 not found for removal"):
        DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)


def test_apply_draft_modification_add_success(sample_draft_json) -> None:
    """Test successful addition with next sequential ID."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {
        "command": "add",
        "params": {
            "summary": "New task",
            "description": "Task description",
            "repo": "test-repo",
            "acceptance_criteria": ["Criteria 1", "Criteria 2"],
            "epic_key": "EPIC-123",
        },
    }
    result = DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)

    assert len(result) == 4
    new_item = result[-1]
    assert new_item["id"] == 4
    assert new_item["summary"] == "New task"
    assert new_item["description"] == "Task description"
    assert new_item["repo"] == "test-repo"
    assert new_item["acceptance_criteria"] == ["Criteria 1", "Criteria 2"]
    assert new_item["excluded"] is False
    assert new_item["epic_key"] == "EPIC-123"


def test_apply_draft_modification_add_defaults(sample_draft_json) -> None:
    """Test addition using only some parameters, relying on defaults for others."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {
        "command": "add",
        "params": {
            "summary": "Minimal task",
        },
    }
    result = DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)

    assert len(result) == 4
    new_item = result[-1]
    assert new_item["id"] == 4
    assert new_item["summary"] == "Minimal task"
    assert new_item["description"] == ""
    assert new_item["repo"] == ""
    assert new_item["acceptance_criteria"] == []
    assert new_item["excluded"] is False


def test_apply_draft_modification_update_success(sample_draft_json) -> None:
    """Test successful update of target fields."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {
        "command": "update",
        "id": 2,
        "params": {
            "summary": "Updated summary",
            "acceptance_criteria": ["New AC"],
            "excluded": True,
        },
    }
    result = DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)

    assert len(result) == 3
    updated_item = result[1]
    assert updated_item["id"] == 2
    assert updated_item["summary"] == "Updated summary"
    # Unchanged fields remain
    assert updated_item["description"] == "Create a POST route for user registration"
    assert updated_item["repo"] == "auth-api"
    assert updated_item["acceptance_criteria"] == ["New AC"]
    assert updated_item["excluded"] is True


def test_apply_draft_modification_update_missing_id(sample_draft_json) -> None:
    """Test update raises error if ID is missing."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {"command": "update", "params": {"summary": "No ID"}}
    with pytest.raises(ValueError, match="Missing ID for update"):
        DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)


def test_apply_draft_modification_update_not_found(sample_draft_json) -> None:
    """Test update raises error if ID is not found."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {"command": "update", "id": 99, "params": {"summary": "Not found"}}
    with pytest.raises(ValueError, match="Item with ID 99 not found for update"):
        DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)


def test_apply_draft_modification_exclude_success(sample_draft_json) -> None:
    """Test flipping the excluded boolean key."""
    from forge.workflow.utils.draft_manager import DraftManager

    # Flip from False to True
    parsed_cmd1 = {"command": "exclude", "id": 1}
    result1 = DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd1)
    assert result1[0]["excluded"] is True

    # Flip from True to False
    parsed_cmd2 = {"command": "exclude", "id": 3}
    result2 = DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd2)
    assert result2[2]["excluded"] is False

    # Flip when excluded field is completely missing (defaults to False, so flips to True)
    draft_without_excluded = [
        {
            "id": 1,
            "summary": "No excluded field",
            "description": "Desc",
            "repo": "repo",
            "acceptance_criteria": [],
        }
    ]
    parsed_cmd3 = {"command": "exclude", "id": 1}
    result3 = DraftManager.apply_draft_modification(draft_without_excluded, parsed_cmd3)
    assert result3[0]["excluded"] is True


def test_apply_draft_modification_exclude_missing_id(sample_draft_json) -> None:
    """Test exclude raises error if ID is missing."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {"command": "exclude"}
    with pytest.raises(ValueError, match="Missing ID for exclude command"):
        DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)


def test_apply_draft_modification_exclude_not_found(sample_draft_json) -> None:
    """Test exclude raises error if ID is not found."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {"command": "exclude", "id": 99}
    with pytest.raises(ValueError, match="Item with ID 99 not found for exclude"):
        DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)


@pytest.mark.parametrize(
    "invalid_params, expected_error",
    [
        ({"summary": 123}, "Field 'summary' must be a string"),
        ({"description": ["not a string"]}, "Field 'description' must be a string"),
        ({"repo": True}, "Field 'repo' must be a string"),
        (
            {"acceptance_criteria": "string-instead-of-list"},
            "Field 'acceptance_criteria' must be a list of strings",
        ),
        ({"acceptance_criteria": [123]}, "Field 'acceptance_criteria' must be a list of strings"),
        ({"excluded": "True"}, "Field 'excluded' must be a boolean"),
        ({"unknown_field": "some-val"}, "Unknown field 'unknown_field'"),
    ],
)
def test_apply_draft_modification_type_validation_add(
    sample_draft_json, invalid_params, expected_error
) -> None:
    """Test strict type validation for the 'add' command."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {"command": "add", "params": invalid_params}
    with pytest.raises(ValueError, match=expected_error):
        DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)


@pytest.mark.parametrize(
    "invalid_params, expected_error",
    [
        ({"summary": 123}, "Field 'summary' must be a string"),
        ({"description": ["not a string"]}, "Field 'description' must be a string"),
        ({"repo": True}, "Field 'repo' must be a string"),
        (
            {"acceptance_criteria": "string-instead-of-list"},
            "Field 'acceptance_criteria' must be a list of strings",
        ),
        ({"acceptance_criteria": [123]}, "Field 'acceptance_criteria' must be a list of strings"),
        ({"excluded": "True"}, "Field 'excluded' must be a boolean"),
        ({"unknown_field": "some-val"}, "Unknown field 'unknown_field'"),
    ],
)
def test_apply_draft_modification_type_validation_update(
    sample_draft_json, invalid_params, expected_error
) -> None:
    """Test strict type validation for the 'update' command."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {"command": "update", "id": 2, "params": invalid_params}
    with pytest.raises(ValueError, match=expected_error):
        DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)


def test_apply_draft_modification_parsing_error(sample_draft_json) -> None:
    """Test that if the parsed_command dictionary contains an 'error' key, ValueError is raised."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {
        "command": "remove",
        "error": "Missing integer ID for remove command",
    }
    with pytest.raises(
        ValueError, match="Invalid command parameters: Missing integer ID for remove command"
    ):
        DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)


def test_apply_draft_modification_deepcopy_isolation(sample_draft_json) -> None:
    """Test that apply_draft_modification doesn't modify the input draft_json in place."""
    from forge.workflow.utils.draft_manager import DraftManager

    parsed_cmd = {
        "command": "update",
        "id": 1,
        "params": {
            "summary": "Completely new summary",
        },
    }
    import copy

    original_copy = copy.deepcopy(sample_draft_json)

    result = DraftManager.apply_draft_modification(sample_draft_json, parsed_cmd)

    assert result[0]["summary"] == "Completely new summary"
    assert sample_draft_json == original_copy


def test_apply_draft_modification_invalid_command_failures(sample_draft_json) -> None:
    """Test that invalid command types raise ValueError."""
    from forge.workflow.utils.draft_manager import DraftManager

    with pytest.raises(ValueError, match="Command type is missing in parsed command."):
        DraftManager.apply_draft_modification(sample_draft_json, {})

    with pytest.raises(ValueError, match="Unsupported modification command type: 'invalid'"):
        DraftManager.apply_draft_modification(sample_draft_json, {"command": "invalid"})
