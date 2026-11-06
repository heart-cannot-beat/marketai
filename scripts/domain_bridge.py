"""The declared state of the transitional ``marketai_domain_app`` exemption.

One parser, read by the release path and by the exit gate, so the intended
state has a single source of truth: the provisioning SQL itself. A second copy
-- in a shell variable, a test constant, a runbook -- is a copy that can be
right while the cluster is wrong.

``rolbypassrls`` is persistent cluster state, so the statement is never simply
absent: the file declares ``BYPASSRLS`` or ``NOBYPASSRLS``, and retiring the
bridge means changing which one.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
ROLES_SQL = REPO / "deploy" / "postgres" / "domain-tenant-roles.sql"

#: The role that still serves requests while the call sites are untenanted.
BRIDGED_ROLE = "marketai_domain_app"

#: The domain database and the only role allowed to change its schema.
DOMAIN_DATABASE = "marketai_domain"
MIGRATION_ROLE = "marketai_domain_owner"

BRIDGE_STATEMENT = re.compile(
    rf"^\s*ALTER\s+ROLE\s+{BRIDGED_ROLE}\s+(NO)?BYPASSRLS\s*;", re.IGNORECASE
)


def _statements() -> list[tuple[str, str]]:
    """(state, statement) for every executable bridge declaration in the file."""
    found = []

    for line in ROLES_SQL.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("--"):
            continue

        match = BRIDGE_STATEMENT.match(line)
        if match:
            state = "NOBYPASSRLS" if match.group(1) else "BYPASSRLS"
            found.append((state, line.strip().rstrip(";")))

    return found


def declared_bridge_state() -> str | None:
    """'BYPASSRLS', 'NOBYPASSRLS', or None when the file declares neither.

    ``None`` is the dangerous case rather than the safe one: a file that says
    nothing leaves an already-provisioned cluster exactly as it was.
    """
    found = _statements()

    if len(found) != 1:
        return None

    return found[0][0]


def declared_bridge_statement() -> str:
    """The statement itself, so a rehearsal replays what provisioning runs."""
    found = _statements()

    if len(found) != 1:
        raise AssertionError(
            f"{ROLES_SQL} must declare exactly one ALTER ROLE {BRIDGED_ROLE} "
            f"(NO)BYPASSRLS statement; found {len(found)}"
        )

    return found[0][1]


def expected_rolbypassrls() -> bool:
    state = declared_bridge_state()

    if state is None:
        raise AssertionError(
            f"{ROLES_SQL} declares no state for {BRIDGED_ROLE}. Absence is not "
            "revocation: rolbypassrls persists on every cluster that ran it."
        )

    return state == "BYPASSRLS"
