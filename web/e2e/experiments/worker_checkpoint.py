"""Guarded one-shot claimant for the Worker-crash-after-lease scenario.

Leases exactly one expected Job through `app.platform.jobs.claim_next` and
exits *before* the Worker's `mark_running` transition, so an E2E recovery
test can observe the Job/Operation still sitting in the `leased` state a real
crashed Worker would leave behind -- without starting the disposable Worker
container at all for that step. This calls the real, durable queue-lease API
(`marketai_claim_next_job_v2` on PostgreSQL); it does not construct its own
database engine or bypass `app.config.DATABASE_URL` -- set
`MARKETAI_DATABASE_URL` in the environment before running this script, the
same as `seed_prerequisites.py`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))


def checkpoint(*, expected_job_id: str, lease_seconds: int = 5) -> dict:
    """Claim the next Job and verify it is the one Job the test expects."""
    from app.platform.jobs import claim_next

    claimed = claim_next(owner="e2e_worker_checkpoint", lease_seconds=lease_seconds)
    if claimed is None:
        return {"error": "no job available"}
    if claimed.id != expected_job_id:
        return {"error": f"claimed wrong job: {claimed.id} != {expected_job_id}"}
    return {
        "job_id": claimed.id,
        "job_type": claimed.type,
        "attempt": claimed.attempt,
        "max_attempts": claimed.max_attempts,
        "lease_owner": claimed.lease_owner,
        "state": "leased",
        "lease_seconds": lease_seconds,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-job-id", required=True)
    parser.add_argument("--lease-seconds", type=int, default=5)
    args = parser.parse_args()
    result = checkpoint(expected_job_id=args.expected_job_id, lease_seconds=args.lease_seconds)
    print(json.dumps(result))
    sys.exit(1 if "error" in result else 0)
