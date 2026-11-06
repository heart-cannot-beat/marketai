"""Cross-stack compatibility test for the public Operation state vocabulary.

Fails when the Python enum, OpenAPI contract, architecture contract document,
or frontend STATUS_CONFIG diverge on the set of allowed Operation states.
"""

import json
import re
from pathlib import Path

from app.platform.operations import OperationState

ROOT = Path(__file__).resolve().parent.parent
OPENAPI_PATH = ROOT / "contracts" / "openapi" / "marketai-domain-v1.json"
CONTRACT_PATH = (
    ROOT / "docs" / "architecture" / "saas-ui-fastapi-integration-contract.md"
)
STATUS_COMPONENT_PATH = (
    ROOT / "web" / "components" / "workspace" / "OperationStatus.tsx"
)


def _python_states() -> set[str]:
    return {member.value for member in OperationState}


def _openapi_states() -> set[str]:
    spec = json.loads(OPENAPI_PATH.read_text())
    state_enum = (
        spec["components"]["schemas"]["OperationDetailResponse"]["properties"][
            "state"
        ]["enum"]
    )
    return set(state_enum)


def _contract_doc_states() -> set[str]:
    text = CONTRACT_PATH.read_text()
    return set(re.findall(r"^- `(\w+)`", text, re.MULTILINE))


def _frontend_status_config_keys() -> set[str]:
    source = STATUS_COMPONENT_PATH.read_text()
    match = re.search(
        r"const STATUS_CONFIG.*?=\s*\{(.*?)\};", source, re.DOTALL
    )
    assert match, "STATUS_CONFIG not found in OperationStatus.tsx"
    return set(re.findall(r"^\s+(\w+)\s*:\s*\{", match.group(1), re.MULTILINE))


def test_all_sources_agree_on_operation_states() -> None:
    python = _python_states()
    openapi = _openapi_states()
    contract = _contract_doc_states()
    frontend = _frontend_status_config_keys()

    assert python == openapi, (
        f"Python OperationState enum and OpenAPI spec disagree.\n"
        f"  Python only: {sorted(python - openapi)}\n"
        f"  OpenAPI only: {sorted(openapi - python)}"
    )
    assert python == contract, (
        f"Python OperationState enum and architecture contract disagree.\n"
        f"  Python only: {sorted(python - contract)}\n"
        f"  Contract only: {sorted(contract - python)}"
    )
    assert python == frontend, (
        f"Python OperationState enum and frontend STATUS_CONFIG disagree.\n"
        f"  Python only: {sorted(python - frontend)}\n"
        f"  Frontend only: {sorted(frontend - python)}"
    )


def test_no_undocumented_state_in_transition_table() -> None:
    """Every state in VALID_TRANSITIONS must be a member of OperationState."""
    from app.platform.operations import VALID_TRANSITIONS

    all_states = _python_states()
    transition_states: set[str] = set()
    for src, targets in VALID_TRANSITIONS.items():
        transition_states.add(src.value)
        transition_states.update(t.value for t in targets)

    undocumented = transition_states - all_states
    assert not undocumented, (
        f"VALID_TRANSITIONS references states not in OperationState: "
        f"{sorted(undocumented)}"
    )
