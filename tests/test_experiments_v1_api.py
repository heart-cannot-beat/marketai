"""Contract tests for the OpenAPI-first Experiments workspace interface."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient

from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import TenantContext


CTX = TenantContext(
    workspace_id="ws-experiment-v1",
    role="member",
    initiator_id="user-experiment-v1",
    initiator_role="member",
    executor="user:user-experiment-v1",
    request_id="request-experiment-v1",
    trace_id="trace-experiment-v1",
)


def _detail(*, state: str = "draft", proposals=None, decisions=None) -> dict:
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    return {
        "id": "exp_v1",
        "product_id": "prod_v1",
        "name": "Feed creative test",
        "state": state,
        "revision": 1,
        "record_schema_version": 2,
        "draft_context": {
            "market": "US",
            "currency": "USD",
            "placement": "feed",
        },
        "created_at": now,
        "entries": [],
        "candidates": [],
        "judgements": [],
        "proposals": proposals or [],
        "decisions": decisions or [],
    }


class TestExperimentsV1Api:
    def setup_method(self) -> None:
        app.dependency_overrides[request_tenant_context] = lambda: CTX

    def teardown_method(self) -> None:
        app.dependency_overrides.pop(request_tenant_context, None)

    def test_openapi_has_stable_operation_ids(self) -> None:
        expected = {
            "GET /api/v1/experiments": "list_experiments",
            "POST /api/v1/experiments": "create_experiment",
            "GET /api/v1/experiments/{experiment_id}": "get_experiment",
            "POST /api/v1/experiments/{experiment_id}/freeze": "freeze_experiment",
            "POST /api/v1/experiments/{experiment_id}/judgements": "judge_experiment",
            "POST /api/v1/experiments/{experiment_id}/proposals/{proposal_id}/confirm": (
                "confirm_experiment_proposal"
            ),
            "POST /api/v1/launch/previews": "create_meta_launch_preview",
            "POST /api/v1/launch/previews/{preview_id}/confirm": (
                "confirm_meta_launch_preview"
            ),
        }
        spec = app.openapi()
        for operation, operation_id in expected.items():
            method, path = operation.split(" ", 1)
            assert spec["paths"][path][method.lower()]["operationId"] == operation_id

    def test_list_is_forced_to_schema_two_and_request_context(self) -> None:
        now = datetime(2026, 9, 9, tzinfo=timezone.utc)
        with mock.patch(
            "app.experiments.v1_router.service.list_experiments",
            return_value={
                "items": [{
                    "id": "exp_v1",
                    "name": "Test",
                    "state": "draft",
                    "revision": 1,
                    "record_schema_version": 2,
                    "created_at": now,
                }]
            },
        ) as list_experiments:
            response = TestClient(app).get("/api/v1/experiments")

        assert response.status_code == 200
        list_experiments.assert_called_once_with(
            CTX, cursor=None, limit=20, state=None, schema_version=2
        )

    def test_create_requires_idempotency_and_returns_typed_detail(self) -> None:
        with mock.patch(
            "app.experiments.v1_router.service.create_experiment_v2",
            return_value={"id": "exp_v1"},
        ), mock.patch(
            "app.experiments.v1_router.service.get_experiment",
            return_value=_detail(),
        ):
            client = TestClient(app)
            body = {
                "product_id": "prod_v1",
                "name": "Feed creative test",
                "draft_context": {},
            }
            missing = client.post("/api/v1/experiments", json=body)
            created = client.post(
                "/api/v1/experiments",
                json=body,
                headers={"Idempotency-Key": "intent-experiment-create"},
            )

        assert missing.status_code == 422
        assert missing.headers["content-type"].startswith("application/problem+json")
        assert created.status_code == 201
        assert created.json()["record_schema_version"] == 2

    def test_detail_keeps_proposal_separate_from_confirmed_decision(self) -> None:
        proposal = {
            "id": "dp_1",
            "judgement_id": "judge_1",
            "proposal_hash": "a" * 64,
            "state": "created",
            "sequence": 1,
            "proposed_entries": [{"entry_id": "cand_1", "verdict": "winner"}],
            "evidence": {"eligible": True},
            "created_at": datetime(2026, 9, 9, tzinfo=timezone.utc),
        }
        with mock.patch(
            "app.experiments.v1_router.service.get_experiment",
            return_value=_detail(state="running", proposals=[proposal]),
        ):
            response = TestClient(app).get("/api/v1/experiments/exp_v1")

        assert response.status_code == 200
        payload = response.json()
        assert payload["proposals"][0]["state"] == "created"
        assert payload["decisions"] == []

    def test_human_confirmation_passes_bff_idempotency_hash(self) -> None:
        decision = {
            "id": "ed_1",
            "experiment_id": "exp_v1",
            "verdict": "winner",
            "metric": "ctr",
            "candidate_id": "cand_1",
            "proposal_id": "dp_1",
            "confirmation_id": "dc_1",
            "confirmed_at": datetime(2026, 9, 9, tzinfo=timezone.utc),
        }
        with mock.patch(
            "app.experiments.v1_router.service.confirm_proposal_v2",
            return_value={"confirmation_id": "dc_1", "decisions": [decision]},
        ) as confirm:
            response = TestClient(app).post(
                "/api/v1/experiments/exp_v1/proposals/dp_1/confirm",
                json={"expected_proposal_sequence": 1},
                headers={"Idempotency-Key": "intent-confirm-proposal"},
            )

        assert response.status_code == 200
        assert response.json()["decisions"][0]["verdict"] == "winner"
        assert confirm.call_args.kwargs["idempotency_key_hash"]
        assert confirm.call_args.kwargs["idempotency_key_hash"] != "intent-confirm-proposal"

    def test_openapi_has_analyze_duplicates_operation(self) -> None:
        spec = app.openapi()
        path = "/api/v1/experiments/{experiment_id}/duplicate-analysis"
        assert "get" in spec["paths"][path]
        assert spec["paths"][path]["get"]["operationId"] == "analyze_experiment_duplicates"

    def test_analyze_duplicates_routes_to_service(self) -> None:
        with mock.patch(
            "app.experiments.v1_router.service.analyze_duplicates",
            return_value={
                "experiment_id": "exp_v1",
                "analyzed_revision": 3,
                "candidate_set_hash": "a" * 64,
                "has_duplicates": True,
                "duplicate_groups": [{
                    "duplicate_group_hash": "b" * 64,
                    "dimension_key": "creative_angle",
                    "concept_key_id": "key_1",
                    "concept_key_name": "Concept One",
                    "members": [
                        {
                            "source_entry_id": "entry_1",
                            "source_candidate_fingerprint": "c" * 64,
                            "asset_version_id": "av_1",
                            "display_label": "entry_1",
                        },
                        {
                            "source_entry_id": "entry_2",
                            "source_candidate_fingerprint": "d" * 64,
                            "asset_version_id": "av_2",
                            "display_label": "entry_2",
                        },
                    ],
                    "allowed_actions": ["allow_controlled_duplicate", "exclude_source_candidate"],
                    "remap_targets": [],
                }],
            },
        ) as analyze:
            response = TestClient(app).get(
                "/api/v1/experiments/exp_v1/duplicate-analysis"
            )

        assert response.status_code == 200
        analyze.assert_called_once_with(CTX, "exp_v1")
        payload = response.json()
        assert payload["has_duplicates"] is True
        assert payload["duplicate_groups"][0]["allowed_actions"] == [
            "allow_controlled_duplicate", "exclude_source_candidate",
        ]
