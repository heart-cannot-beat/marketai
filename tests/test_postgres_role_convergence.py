"""PostgreSQL evidence that converging the domain roles never takes the
runtime roles' privileges away from a live connection.

`deploy/postgres/domain-tenant-roles.sql` revokes every runtime role's table
and sequence privileges and then grants the declared matrix back. A release
runs it twice (`deploy/postgres/release.sh`, steps 1/4 and 4/4) while FastAPI
and the Worker keep serving, so the revoke and the re-grant must become
visible to other sessions together. Golden Path PR Validation run 36517751730
caught the gap: the Worker polled `operation_records` between the two, got
`permission denied for table operation_records` and exited.
"""
from __future__ import annotations

import os
import subprocess
import threading
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
WORKER_URL = os.environ.get("DOMAIN_WORKER_DATABASE_URL")
# What `deploy/postgres/apply-domain-roles.sh` itself requires.
CONVERGENCE_ENV = (
    "SUPERUSER_DATABASE_URL",
    "DOMAIN_API_PASSWORD",
    "DOMAIN_WORKER_PASSWORD",
    "DOMAIN_READONLY_PASSWORD",
    "MARKETAI_TENANT_CONTEXT_SECRET",
)
CONFIGURED = bool(WORKER_URL) and all(os.environ.get(name) for name in CONVERGENCE_ENV)


@unittest.skipUnless(
    CONFIGURED,
    "set DOMAIN_WORKER_DATABASE_URL and the apply-domain-roles.sh environment "
    "against a released domain database",
)
class RoleConvergenceKeepsRuntimeGrantsTests(unittest.TestCase):
    def test_worker_keeps_reading_while_domain_roles_converge(self) -> None:
        from sqlalchemy import create_engine, text
        from sqlalchemy.exc import DBAPIError

        engine = create_engine(WORKER_URL, future=True)
        stop = threading.Event()
        refused: list[str] = []
        reads = 0

        def poll() -> None:
            # The query `jobs.reconcile_admitted_operations` was running when
            # the Worker died, reduced to its privilege check.
            nonlocal reads
            with engine.connect() as connection:
                while not stop.is_set():
                    try:
                        connection.execute(
                            text("SELECT 1 FROM operation_records LIMIT 1")
                        ).all()
                    except DBAPIError as exc:
                        refused.append(str(exc.orig).splitlines()[0])
                    connection.rollback()
                    reads += 1

        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        try:
            for _ in range(3):
                subprocess.run(
                    [str(REPO / "deploy" / "postgres" / "apply-domain-roles.sh")],
                    check=True,
                    capture_output=True,
                    timeout=180,
                )
        finally:
            stop.set()
            poller.join(timeout=30)
            engine.dispose()

        self.assertGreater(reads, 0)
        self.assertFalse(
            refused,
            f"{len(refused)} of {reads} Worker reads were refused while the "
            f"domain roles converged, the first with: {refused[:1]}",
        )


if __name__ == "__main__":
    unittest.main()
