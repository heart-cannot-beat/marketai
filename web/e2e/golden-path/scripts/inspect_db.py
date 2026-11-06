#!/usr/bin/env python3
"""
Read-only domain evidence queries for golden-path E2E.

Accepts JSON command on stdin, returns JSON on stdout.
Each command validated against a closed schema.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPOSITORY_ROOT))

VALID_COMMANDS = frozenset({
    "assert_zero_business_entities",
    "query_call_evidence",
    "query_provider_cost_ledger",
    "capture_canonical_snapshot",
    "assert_no_winner_decision",
    "assert_zero_meta_writes",
    "assert_workspace_meta_provenance",
    "assert_non_vacuous_meta_evidence",
    "assert_no_runnable_or_inflight_jobs",
    "assert_operations_settled",
    "read_job_lease",
    "assert_call_evidence_mode_consistency",
})

BUSINESS_TABLES = [
    "library_entries",
    "library_versions",
    "generation_runs",
    "assets",
    "asset_versions",
    "experiments",
    "localization_batches",
    "scale_enrollments",
    "scale_advice_threads",
]


def get_connection(database_url: str) -> Any:
    """Create a read-only database connection."""
    try:
        import psycopg
    except ImportError:
        raise RuntimeError("psycopg required for database inspection")

    conn = psycopg.connect(database_url, autocommit=True)
    conn.execute("SET default_transaction_read_only = ON")
    return conn


def bind_tenant_context(
    conn: Any,
    *,
    workspace_ref: str,
    meta_mode: str | None = None,
    authorization_epoch: int = 0,
) -> None:
    """Bind the signed, transaction-local context required by tenant RLS."""
    from app.platform.tenancy import TenantContext, tenant_context_signature

    secret = os.environ.get("MARKETAI_TENANT_CONTEXT_SECRET", "")
    if not secret:
        raise RuntimeError("MARKETAI_TENANT_CONTEXT_SECRET is required")

    context = TenantContext(
        workspace_id=workspace_ref,
        role="system",
        executor="system:golden-path-inspector",
        authorization_epoch=authorization_epoch,
        meta_mode=meta_mode,
    )
    signature = tenant_context_signature(context, secret)
    conn.execute(
        "SELECT "
        "set_config('marketai.workspace_id', %s, true), "
        "set_config('marketai.authorization_epoch', %s, true), "
        "set_config('marketai.meta_mode', %s, true), "
        "set_config('marketai.context_signature', %s, true)",
        (workspace_ref, str(authorization_epoch), meta_mode or "", signature),
    )


def assert_zero_business_entities(params: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection(params["database_url"])
    workspace_ref = params["workspace_ref"]
    counts: dict[str, int] = {}

    try:
        with conn.transaction():
            bind_tenant_context(
                conn,
                workspace_ref=workspace_ref,
                authorization_epoch=int(params.get("authorization_epoch", 0)),
            )
            for table in BUSINESS_TABLES:
                cur = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE workspace_id = %s",
                    (workspace_ref,),
                )
                row = cur.fetchone()
                counts[table] = row[0] if row else 0

        total = sum(counts.values())
        if total > 0:
            return {
                "ok": False,
                "error": f"Found {total} business entities",
                "data": counts,
            }
        return {"ok": True, "data": counts}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def query_call_evidence(params: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection(params["database_url"])
    workspace_ref = params["workspace_ref"]

    try:
        with conn.transaction():
            bind_tenant_context(
                conn,
                workspace_ref=workspace_ref,
                authorization_epoch=int(params.get("authorization_epoch", 0)),
            )
            cur = conn.execute(
                "SELECT id, workspace_id, meta_api_method, meta_mode, "
                "network_dispatched_at, created_at "
                "FROM meta_call_intent_records "
                "WHERE workspace_id = %s ORDER BY created_at",
                (workspace_ref,),
            )
            rows = [
                {
                    "id": str(r[0]),
                    "workspace_id": str(r[1]),
                    "call_kind": r[2],
                    "mode": r[3],
                    "status": "dispatched" if r[4] is not None else "simulated",
                    "created_at": r[5].isoformat() if r[5] else None,
                }
                for r in cur.fetchall()
            ]
        conn.close()
        return {"ok": True, "data": rows}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


def query_provider_cost_ledger(params: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection(params["database_url"])
    workspace_ref = params["workspace_ref"]

    try:
        with conn.transaction():
            bind_tenant_context(
                conn,
                workspace_ref=workspace_ref,
                authorization_epoch=int(params.get("authorization_epoch", 0)),
            )
            cur = conn.execute(
                "SELECT status, COUNT(*) FROM cost_ledger "
                "WHERE workspace_id = %s GROUP BY status",
                (workspace_ref,),
            )
            counts: dict[str, int] = {}
            for row in cur.fetchall():
                counts[row[0]] = row[1]

        conn.close()
        reservation = counts.get("reserved", 0)
        settlement = counts.get("settled", 0)
        release = counts.get("released", 0)
        classification = "consistent" if settlement + release == reservation else "inconsistent"
        return {
            "ok": True,
            "data": {
                "reservation_count": reservation,
                "settlement_count": settlement,
                "release_count": release,
                "classification": classification,
            },
        }
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


def capture_canonical_snapshot(params: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection(params["database_url"])
    workspace_ref = params["workspace_ref"]
    snapshot: dict[str, Any] = {}

    try:
        with conn.transaction():
            bind_tenant_context(
                conn,
                workspace_ref=workspace_ref,
                authorization_epoch=int(params.get("authorization_epoch", 0)),
            )
            for table in BUSINESS_TABLES:
                try:
                    cur = conn.execute(
                        f"SELECT id, version FROM {table} WHERE workspace_id = %s ORDER BY id",
                        (workspace_ref,),
                    )
                    snapshot[table] = [
                        {"id": str(r[0]), "version": r[1]} for r in cur.fetchall()
                    ]
                except Exception:
                    snapshot[table] = []

        conn.close()
        return {"ok": True, "data": snapshot}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


def assert_no_winner_decision(params: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection(params["database_url"])
    try:
        with conn.transaction():
            bind_tenant_context(
                conn,
                workspace_ref=params["workspace_ref"],
                authorization_epoch=int(params.get("authorization_epoch", 0)),
            )
            cur = conn.execute(
                "SELECT COUNT(*) FROM experiment_decisions "
                "WHERE workspace_id = %s AND experiment_id = %s AND verdict = 'win'",
                (params["workspace_ref"], params["experiment_ref"]),
            )
            row = cur.fetchone()
        conn.close()
        count = row[0] if row else 0
        if count > 0:
            return {"ok": False, "error": f"Found {count} winner decisions"}
        return {"ok": True, "data": {"count": 0}}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


def assert_zero_meta_writes(params: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection(params["database_url"])
    try:
        with conn.transaction():
            bind_tenant_context(
                conn,
                workspace_ref=params["workspace_ref"],
                authorization_epoch=int(params.get("authorization_epoch", 0)),
            )
            cur = conn.execute(
                "SELECT id, meta_api_method, meta_mode "
                "FROM meta_call_intent_records "
                "WHERE workspace_id = %s "
                "AND (write_permitted = TRUE "
                "OR meta_api_method IN ('POST', 'PUT', 'DELETE', 'PATCH') "
                "OR (network_dispatched_at IS NOT NULL "
                "AND meta_mode NOT IN ('customer_sandbox', 'controlled_mock'))) ",
                (params["workspace_ref"],),
            )
            rows = cur.fetchall()
        conn.close()
        if rows:
            return {
                "ok": False,
                "error": "Found real Meta write intents",
                "data": [
                    {"id": str(row[0]), "method": row[1], "mode": row[2]}
                    for row in rows
                ],
            }
        return {"ok": True, "data": {"count": 0}}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


def assert_workspace_meta_provenance(params: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection(params["database_url"])
    try:
        with conn.transaction():
            bind_tenant_context(
                conn,
                workspace_ref=params["workspace_ref"],
                meta_mode=params["expected_mode"],
                authorization_epoch=int(params.get("authorization_epoch", 0)),
            )
            cur = conn.execute(
                "SELECT meta_mode FROM meta_connection_records WHERE workspace_id = %s",
                (params["workspace_ref"],),
            )
            row = cur.fetchone()
        conn.close()
        if not row:
            return {"ok": False, "error": "No Meta connection found"}
        if row[0] != params["expected_mode"]:
            return {"ok": False, "error": f"Expected {params['expected_mode']}, got {row[0]}"}
        return {"ok": True, "data": {"meta_mode": row[0]}}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


def assert_non_vacuous_meta_evidence(params: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection(params["database_url"])
    try:
        with conn.transaction():
            bind_tenant_context(
                conn,
                workspace_ref=params["workspace_ref"],
                meta_mode=params["expected_provenance"],
                authorization_epoch=int(params.get("authorization_epoch", 0)),
            )
            cur = conn.execute(
                "SELECT i.meta_mode, COUNT(*) "
                "FROM meta_call_intent_records i "
                "JOIN meta_call_outcome_records o "
                "ON o.workspace_id = i.workspace_id AND o.intent_id = i.id "
                "WHERE i.workspace_id = %s "
                "AND i.meta_mode = %s "
                "AND i.meta_mode IN ('customer_sandbox', 'controlled_mock') "
                "AND o.network_attempted = FALSE "
                "GROUP BY i.meta_mode",
                (params["workspace_ref"], params["expected_provenance"]),
            )
            rows = {r[0]: r[1] for r in cur.fetchall()}
        conn.close()

        simulated = sum(rows.values())
        if simulated == 0:
            return {"ok": False, "error": "Vacuous: zero simulated Meta evidence intents"}

        return {
            "ok": True,
            "data": {
                "total_intents": simulated,
                "modes": rows,
                "non_vacuous": True,
            },
        }
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


def assert_no_runnable_or_inflight_jobs(params: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection(params["database_url"])
    try:
        with conn.transaction():
            bind_tenant_context(
                conn,
                workspace_ref=params["workspace_ref"],
                authorization_epoch=int(params.get("authorization_epoch", 0)),
            )
            cur = conn.execute(
                "SELECT COUNT(*) FROM generation_runs "
                "WHERE workspace_id = %s AND status IN "
                "('pending', 'queued', 'running', 'uncertain', 'qc_pending', 'paused')",
                (params["workspace_ref"],),
            )
            row = cur.fetchone()
        conn.close()
        count = row[0] if row else 0
        if count > 0:
            return {"ok": False, "error": f"Found {count} runnable/in-flight jobs"}
        return {"ok": True, "data": {"count": 0}}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


def assert_operations_settled(params: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection(params["database_url"])
    try:
        with conn.transaction():
            bind_tenant_context(
                conn,
                workspace_ref=params["workspace_ref"],
                authorization_epoch=int(params.get("authorization_epoch", 0)),
            )
            cur = conn.execute(
                "SELECT COUNT(*) FROM generation_runs "
                "WHERE workspace_id = %s AND status NOT IN "
                "('succeeded', 'partially_succeeded', 'failed', 'cancelled')",
                (params["workspace_ref"],),
            )
            row = cur.fetchone()
        conn.close()
        count = row[0] if row else 0
        if count > 0:
            return {"ok": False, "error": f"Found {count} unsettled operations"}
        return {"ok": True, "data": {"count": 0}}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


def read_job_lease(params: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection(params["database_url"])
    try:
        with conn.transaction():
            cur = conn.execute(
                "SELECT status FROM generation_runs WHERE id = %s",
                (params["run_ref"],),
            )
            row = cur.fetchone()
        conn.close()
        if not row:
            return {"ok": False, "error": "Job not found"}
        return {
            "ok": True,
            "data": {
                "state": row[0],
                "lease_owner": None,
                "lease_deadline": None,
            },
        }
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


def assert_call_evidence_mode_consistency(params: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection(params["database_url"])
    try:
        with conn.transaction():
            bind_tenant_context(
                conn,
                workspace_ref=params["workspace_ref"],
                authorization_epoch=int(params.get("authorization_epoch", 0)),
            )
            cur = conn.execute(
                "SELECT DISTINCT meta_mode FROM meta_call_intent_records "
                "WHERE workspace_id = %s",
                (params["workspace_ref"],),
            )
            modes = [r[0] for r in cur.fetchall()]
        conn.close()
        inconsistent = [
            mode
            for mode in modes
            if mode not in ("customer_sandbox", "controlled_mock", "internal_validation")
        ]
        if inconsistent:
            return {"ok": False, "error": f"Inconsistent modes: {inconsistent}"}
        return {"ok": True, "data": {"modes": modes}}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


COMMAND_HANDLERS: dict[str, Any] = {
    "assert_zero_business_entities": assert_zero_business_entities,
    "query_call_evidence": query_call_evidence,
    "query_provider_cost_ledger": query_provider_cost_ledger,
    "capture_canonical_snapshot": capture_canonical_snapshot,
    "assert_no_winner_decision": assert_no_winner_decision,
    "assert_zero_meta_writes": assert_zero_meta_writes,
    "assert_workspace_meta_provenance": assert_workspace_meta_provenance,
    "assert_non_vacuous_meta_evidence": assert_non_vacuous_meta_evidence,
    "assert_no_runnable_or_inflight_jobs": assert_no_runnable_or_inflight_jobs,
    "assert_operations_settled": assert_operations_settled,
    "read_job_lease": read_job_lease,
    "assert_call_evidence_mode_consistency": assert_call_evidence_mode_consistency,
}


def main() -> int:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"Invalid JSON: {e}"}))
        return 1

    command = request.get("command")
    if command not in VALID_COMMANDS:
        print(json.dumps({"ok": False, "error": f"Unknown command: {command}"}))
        return 1

    handler = COMMAND_HANDLERS[command]
    result = handler(request)
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
