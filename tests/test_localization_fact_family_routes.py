"""#26: `POST /api/v1/localization/batches/{id}/start-preparation` with a Manual
source never answers 500 for a fact it cannot resolve.

Before #26 a policy that requested `tax` or `discount` from a ManualFactSet
holding that row made `_build_fact_value` raise (`ValueError` /
`KeyError`), and the route answered 500. Versions published before #26 can
still name a legacy fact type or hold a row that breaks its family's value
structure: published versions are never rewritten, so the policy's
`on_failure` decides the market instead.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    LocalizationBatchRecord,
    MarketPolicyVersionRecord,
    TruthPackageMarketRecord,
    TruthSourceAttemptRecord,
)
from app.main import app
from app.platform.http_identity import request_tenant_context
from tests import test_localization_routes as legacy_routes
from tests.test_localization_batch import _make_version

_TAGS = itertools.count(1)


def _row(fact_id: str, fact_type: str, country: str, **values: object) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "fact_id": fact_id,
        "fact_type": fact_type,
        "country_code": country,
        "valid_from": (now - timedelta(days=1)).isoformat(),
        "valid_to": (now + timedelta(days=30)).isoformat(),
        **values,
    }


def _sources(ctx, tag: str, country: str, *, rules: dict[str, str], extra_rows: list[dict]):
    """Publish (pre-#26 style, without publish validation) a Manual-only
    policy, its DataSource and a ManualFactSet with a valid price row."""
    with tenant_session(ctx) as session:
        policy_id = _make_version(
            session, ctx.workspace_id, "truth_resolution_policy", tag,
            content={"rules": [
                {
                    "fact_type": fact_type,
                    "source_priority": ["manual"],
                    "on_failure": on_failure,
                    "max_staleness_seconds": 86400 * 30,
                }
                for fact_type, on_failure in rules.items()
            ]},
        )
        data_source_id = _make_version(
            session, ctx.workspace_id, "data_source", tag,
            content={"adapter_kind": "manual", "execution_mode": "real"},
        )
        fact_set_id = _make_version(
            session, ctx.workspace_id, "manual_fact_set", tag,
            content={"facts": [
                _row("price-1", "price", country, amount="19.99", currency="USD"),
                *extra_rows,
            ]},
        )
    return policy_id, data_source_id, fact_set_id


class _Batch:
    """One draft batch (one real-mode market) driven through the HTTP routes."""

    def __init__(self) -> None:
        self.tag = f"f26-{next(_TAGS)}"
        self.harness = legacy_routes.TestLocalizationRoutes(methodName="test_create_draft")
        self.harness.setUp()
        self.ctx = self.harness.ctx
        self.client = self.harness.client
        self.draft = self.harness._create_draft_via_http(self.tag)
        self.batch_id = self.draft["batch_id"]
        self.recipe_hash = self.harness._analysis_recipe_hash(self.tag)
        self.harness._seed_analysis(self.draft["winner"], self.recipe_hash, self.tag)
        with tenant_session(self.ctx) as session:
            self.country = session.get(
                MarketPolicyVersionRecord, self.draft["market_policy_version_id"]
            ).country

    def close(self) -> None:
        self.harness.doCleanups()

    def revision(self) -> int:
        return self.client.get(f"/api/v1/localization/batches/{self.batch_id}").json()["revision"]

    def start(self, policy_id: str, data_source_id: str, fact_set_id: str, *, revision=None):
        if revision is None:
            revision = self.revision()
        return self.client.post(
            f"/api/v1/localization/batches/{self.batch_id}/start-preparation",
            json={
                "truth_resolution_policy_version_id": policy_id,
                "data_source_version_ids": [data_source_id],
                "manual_fact_set_version_id": fact_set_id,
                "analysis_recipe_hash": self.recipe_hash,
                "analysis_provider": "mock-vision",
                "analysis_model": "mock-vision-1",
                "analysis_adapter_version": "mock-v1",
            },
            headers={"Idempotency-Key": f"{self.tag}-start", "If-Match": str(revision)},
        )

    def truth_package_id(self) -> str | None:
        with tenant_session(self.ctx) as session:
            return session.get(LocalizationBatchRecord, self.batch_id).truth_package_id


@pytest.fixture(scope="module")
def _schema():
    legacy_routes.TestLocalizationRoutes.setUpClass()


@pytest.fixture
def batch(_schema):
    created = _Batch()
    try:
        yield created
    finally:
        created.close()


@pytest.mark.parametrize(
    ("fact_type", "extra_row", "on_failure", "failure_code", "readiness", "detail"),
    [
        pytest.param(
            "tax", {"rate": "0.19"}, "block", "fact_type_unsupported", "failed",
            {"reason": "fact_missing_blocked", "blocked": ["tax"], "missing": ["tax"]},
            id="legacy-name-block",
        ),
        pytest.param(
            "delivery_time", {"amount": "3", "currency": "USD"}, "block",
            "fact_type_unsupported", "failed",
            {"reason": "fact_missing_blocked", "blocked": ["delivery_time"],
             "missing": ["delivery_time"]},
            id="legacy-delivery-time-block",
        ),
        pytest.param(
            "discount", {"amount": "2.00", "currency": "USD"}, "block",
            "fact_value_invalid", "failed",
            {"reason": "fact_missing_blocked", "blocked": ["discount"], "missing": ["discount"]},
            id="invalid-value-block",
        ),
        pytest.param(
            "availability", None, "block", "fact_type_not_in_manual_set", "failed",
            {"reason": "fact_missing_blocked", "blocked": ["availability"],
             "missing": ["availability"]},
            id="type-not-in-set-block",
        ),
        pytest.param(
            "tax", {"rate": "0.19"}, "degrade", "fact_type_unsupported", "degraded",
            {"reason": "fact_missing_permitted", "missing": ["tax"]},
            id="legacy-name-degrade",
        ),
    ],
)
def test_unresolvable_manual_fact_follows_on_failure_instead_of_500(
    batch, fact_type, extra_row, on_failure, failure_code, readiness, detail,
):
    extra_rows = (
        [] if extra_row is None
        else [_row(f"{fact_type}-1", fact_type, batch.country, **extra_row)]
    )
    ids = _sources(
        batch.ctx, batch.tag, batch.country,
        rules={"price": "block", fact_type: on_failure}, extra_rows=extra_rows,
    )

    response = batch.start(*ids)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["truth_status"] == readiness
    with tenant_session(batch.ctx) as session:
        market = session.query(TruthPackageMarketRecord).filter_by(
            truth_package_id=body["truth_package_id"]
        ).one()
        assert (market.readiness, market.readiness_detail_json) == (readiness, detail)
        attempts = {
            (attempt.fact_type, attempt.outcome, attempt.failure_code, attempt.skip_code)
            for attempt in session.query(TruthSourceAttemptRecord).filter_by(
                truth_package_id=body["truth_package_id"]
            )
        }
    assert attempts == {
        ("price", "accepted", None, None),
        (fact_type, "failed", failure_code, None),
        (fact_type, "skipped", None, f"on_failure_{on_failure}"),
    }


def test_start_preparation_cannot_read_another_workspaces_fact_set(batch):
    other = batch.harness._make_context(role="owner")
    policy_id, data_source_id, _ = _sources(
        batch.ctx, batch.tag, batch.country, rules={"price": "block"}, extra_rows=[],
    )
    _, _, foreign_fact_set_id = _sources(
        other, f"{batch.tag}-other", batch.country, rules={"price": "block"},
        extra_rows=[_row("tax-1", "tax", batch.country, rate="0.19")],
    )

    response = batch.start(policy_id, data_source_id, foreign_fact_set_id)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "truth_configuration_invalid"
    assert batch.truth_package_id() is None


def test_another_workspace_cannot_start_preparation_on_the_batch(batch):
    other = batch.harness._make_context(role="owner")
    other_ids = _sources(
        other, f"{batch.tag}-other", batch.country, rules={"price": "block", "tax": "block"},
        extra_rows=[_row("tax-1", "tax", batch.country, rate="0.19")],
    )
    revision = batch.revision()
    app.dependency_overrides[request_tenant_context] = lambda: other
    try:
        response = batch.start(*other_ids, revision=revision)
    finally:
        app.dependency_overrides[request_tenant_context] = lambda: batch.ctx

    assert response.status_code == 404, response.text
    assert batch.truth_package_id() is None
