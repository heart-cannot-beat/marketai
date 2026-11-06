"""Manufacture the one state transition issue #88's closed-state-evidence
test needs that no reachable command can produce (issue #88 spec §3.5).

`close_experiment_v2` (`app.experiments.service`) exists, but is never wired
to an HTTP route (grepped `app/experiments/v1_router.py` -- no
`close_experiment_v2` call site), and `ExperimentsWorkspace.tsx` has no
"Close experiment" action either. Per spec §3.5's hybrid-fixture rule, every
other piece of evidence a closed-state read needs (`ExperimentVersionRecord`,
`ExperimentVersionCandidateRecord`, `ConceptResolutionEvidence`/
`concept_resolutions_hash`, ...) must come from the real
`freeze_experiment_v2` service -- this script's only job is the terminal
`state` flip itself, via a direct, RLS-scoped `UPDATE experiments SET
state = 'closed'`. It deliberately does *not* call `close_experiment_v2`
(which would also require a `closure_kind`, append an
`ExperimentStateEventRecord`, and bump `revision`) -- the point is exactly
that this one transition is manufactured, not real closure business logic.

Mirrors `seed_ad_metrics.py`'s mutation-safety preflight (same required
environment, same PostgreSQL/owner-role/local-host checks).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

REQUIRED_ENVIRONMENT = (
    "E2E_EXPERIMENTS_ALLOW_MUTATIONS",
    "E2E_EXPERIMENTS_WORKSPACE_ID",
    "MARKETAI_DATABASE_URL",
    "MARKETAI_TENANT_CONTEXT_SECRET",
)
MUTATION_OPT_IN = "E2E_EXPERIMENTS_ALLOW_MUTATIONS"
SAFE_DATABASE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})
EXPECTED_DATABASE = "marketai_domain"


def _database_url_problems(name: str, database_url: str) -> list[str]:
    parsed = urlsplit(database_url)
    problems: list[str] = []
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
        problems.append(f"{name} must use PostgreSQL")
    host = (parsed.hostname or "").lower()
    if host not in SAFE_DATABASE_HOSTS:
        display_host = host or "<missing>"
        problems.append(f"{name} host is not an approved local/compose target: {display_host}")
    database = parsed.path.removeprefix("/")
    if database != EXPECTED_DATABASE:
        problems.append(f"{name} must target the {EXPECTED_DATABASE} database")
    username = (parsed.username or "").lower()
    if username != "owner" and not username.endswith("_owner"):
        problems.append(f"{name} must use an owner role")
    return problems


def _required_environment(expected_workspace_id: str) -> dict[str, str]:
    values = {name: os.environ.get(name, "").strip() for name in REQUIRED_ENVIRONMENT}
    missing = [name for name, value in values.items() if not value]
    problems: list[str] = []
    if missing:
        problems.append("missing: " + ", ".join(missing))

    opt_in = values.get(MUTATION_OPT_IN, "")
    if opt_in and opt_in != "1":
        problems.append(f"{MUTATION_OPT_IN} must be exactly 1")

    database_url = values.get("MARKETAI_DATABASE_URL", "")
    if database_url:
        problems.extend(_database_url_problems("MARKETAI_DATABASE_URL", database_url))

    if values.get("E2E_EXPERIMENTS_WORKSPACE_ID") and values["E2E_EXPERIMENTS_WORKSPACE_ID"] != expected_workspace_id:
        problems.append("workspace_id does not match E2E_EXPERIMENTS_WORKSPACE_ID")

    if problems:
        raise RuntimeError("Experiments E2E close-state preflight failed; " + "; ".join(problems))
    return values


def set_closed(*, workspace_id: str, experiment_id: str) -> dict:
    _required_environment(workspace_id)

    from sqlalchemy import select, update

    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import ExperimentRecord, utcnow
    from app.platform.tenancy import TenantContext

    ctx = TenantContext(
        workspace_id=workspace_id, role="owner", initiator_id="e2e-close-fixture",
        authorization_epoch=0,
    )

    now = utcnow()
    with tenant_session(ctx) as session:
        experiment = session.scalar(
            select(ExperimentRecord).where(
                ExperimentRecord.id == experiment_id,
                ExperimentRecord.workspace_id == workspace_id,
            )
        )
        if experiment is None:
            raise RuntimeError(f"Experiment {experiment_id!r} not found in workspace {workspace_id!r}")

        from_state = experiment.state
        result = session.execute(
            update(ExperimentRecord)
            .where(
                ExperimentRecord.id == experiment_id,
                ExperimentRecord.workspace_id == workspace_id,
            )
            .values(state="closed", closed_at=now)
        )
        if result.rowcount != 1:
            raise RuntimeError("Direct closed-state update affected an unexpected row count")
        session.flush()

    return {
        "experiment_id": experiment_id,
        "from_state": from_state,
        "state": "closed",
        "closed_at": now.isoformat(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--experiment-id", required=True)
    args = parser.parse_args()
    result = set_closed(workspace_id=args.workspace_id, experiment_id=args.experiment_id)
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
