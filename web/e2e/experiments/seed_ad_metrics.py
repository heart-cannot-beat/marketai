"""Seed AdDailyMetricRecord rows for the Experiments E2E judgement step (issue #88).

`judge_experiment`'s v2 path (`app.experiments.service.run_judgement_v2`)
matches metrics evidence purely on `(AdDailyMetricRecord.ad_id,
AdDailyMetricRecord.metric_date <= cutoff)` -- no live Meta sync ever runs in
this suite, so the golden-path duplicate-resolution scenario
(`duplicate-resolution.spec.ts`) shells out to this script, once candidates
are bound through the real `bind_experiment_candidate` BFF route, to seed
exactly the daily rows the CTR judge needs to reach a decisive win/lose
verdict for two bound candidates. This mirrors the fixture pattern
`tests/test_experiment_api.py`'s `_seed_meta_metrics` helper uses for the
same backend path (`MetaAccountRecord` + `MetaSyncRunRecord` +
`AdDailyMetricRecord`), just parameterized over the CLI instead of hardcoded
into a unittest fixture.

Mirrors `seed_prerequisites.py`'s mutation-safety preflight (same required
environment, same PostgreSQL/owner-role/local-host checks) -- this never
bypasses the RLS `tenant_session` boundary, it only writes read-only
measurement evidence a real Meta sync would otherwise have produced.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

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
        raise RuntimeError("Experiments E2E metrics-seed preflight failed; " + "; ".join(problems))
    return values


def seed(*, workspace_id: str, candidates: list[dict], days: int) -> dict:
    """Seed `days` daily rows per candidate, ending yesterday (UTC date).

    `judge_experiment`'s cutoff defaults to ``today - 1 day`` (it rejects a
    same-day cutoff as partial), so every row here is dated at or before that
    default cutoff and is guaranteed visible to the very next judgement call.
    """
    _required_environment(workspace_id)

    from sqlalchemy import select

    from app.adapters.persistence.database import tenant_session
    from app.adapters.persistence.models import (
        AdDailyMetricRecord,
        MetaAccountRecord,
        MetaSyncRunRecord,
    )
    from app.platform.tenancy import TenantContext

    ctx = TenantContext(
        workspace_id=workspace_id,
        role="owner",
        initiator_id="e2e-metrics-seed",
        authorization_epoch=0,
    )

    tag = uuid4().hex[:8]
    end_day = date.today() - timedelta(days=1)
    start_day = end_day - timedelta(days=days - 1)
    seeded_rows = 0

    with tenant_session(ctx) as session:
        for index, candidate in enumerate(candidates):
            ad_account_id = str(candidate["ad_account_id"])
            ad_id = str(candidate["ad_id"])
            daily_impressions = int(candidate["daily_impressions"])
            daily_clicks = int(candidate["daily_clicks"])

            account = session.scalar(
                select(MetaAccountRecord).where(
                    MetaAccountRecord.workspace_id == workspace_id,
                    MetaAccountRecord.ad_account_id == ad_account_id,
                )
            )
            if account is None:
                account = MetaAccountRecord(
                    id=f"macct_e2e_{tag}_{index}",
                    workspace_id=workspace_id,
                    ad_account_id=ad_account_id,
                    name=f"E2E metrics account {ad_account_id}",
                    currency="USD",
                    timezone_name="UTC",
                    api_version="v23.0",
                )
                session.add(account)
                session.flush()

            sync_run = MetaSyncRunRecord(
                id=f"sync_e2e_{tag}_{index}",
                workspace_id=workspace_id,
                account_id=account.id,
                state="completed",
                date_start=start_day,
                date_stop=end_day,
                contract_hash="e" * 64,
                contract_json={},
                api_version="v23.0",
                account_currency="USD",
                account_timezone="UTC",
                idempotency_key=f"sync_e2e_{tag}_{index}",
            )
            session.add(sync_run)
            session.flush()

            for offset in range(days):
                metric_date = start_day + timedelta(days=offset)
                session.add(AdDailyMetricRecord(
                    id=f"met_e2e_{tag}_{index}_{offset}",
                    account_id=account.id,
                    sync_run_id=sync_run.id,
                    ad_id=ad_id,
                    metric_date=metric_date,
                    spend=Decimal("10.00"),
                    impressions=daily_impressions,
                    clicks=daily_clicks,
                    purchases=Decimal("0"),
                    purchase_value=Decimal("0"),
                    currency="USD",
                    contract_hash="e" * 64,
                    raw_json={},
                ))
                seeded_rows += 1
        session.flush()

    return {
        "seeded_rows": seeded_rows,
        "start_day": start_day.isoformat(),
        "end_day": end_day.isoformat(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--candidates-json",
        required=True,
        help='JSON list of {"ad_account_id","ad_id","daily_impressions","daily_clicks"}',
    )
    args = parser.parse_args()
    result = seed(
        workspace_id=args.workspace_id,
        candidates=json.loads(args.candidates_json),
        days=args.days,
    )
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
