"""forge_artifacts field is present and defaults correctly in all workflow states."""
from forge.workflow.feature.state import create_initial_feature_state
from forge.workflow.bug.state import create_initial_bug_state


def test_feature_state_has_forge_artifacts_default():
    state = create_initial_feature_state("TEST-1")
    assert "forge_artifacts" in state
    assert state["forge_artifacts"] == {}


def test_bug_state_has_forge_artifacts_default():
    state = create_initial_bug_state("BUG-1")
    assert "forge_artifacts" in state
    assert state["forge_artifacts"] == {}


def test_forge_artifacts_is_dict_of_dicts():
    state = create_initial_feature_state("TEST-1")
    state["forge_artifacts"] = {"org/repo": {"handoff.md": "content"}}
    assert state["forge_artifacts"]["org/repo"]["handoff.md"] == "content"
