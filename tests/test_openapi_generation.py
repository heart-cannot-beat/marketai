"""Regression tests for deterministic OpenAPI normalization."""

import json

from scripts.generate_openapi import (
    CONTRACT_PATH,
    _collect_referenced_schemas,
    _normalize_responses,
    _strip_titles,
    extract_spec,
)


def test_openapi_no_manual_drift_for_scale() -> None:
    """The Scale (Issue #25, Task 16) paths/schemas the live FastAPI app
    serves match the committed ``contracts/openapi/marketai-domain-v1.json``
    exactly -- a forgotten ``scripts/generate_openapi.py`` run after editing
    ``app/scale/v1_router.py``/``app/scale/schemas.py`` fails this test
    instead of shipping silent drift between the contract and the BFF
    allowlist/validators/DomainGateway generated from it.

    Scoped to ``/api/v1/scale/*`` rather than a full-spec ``--check`` (which
    `scripts/generate_openapi.py --check` already performs, run manually
    per that script's own docstring): unrelated domains already carry
    pre-existing drift from routes merged in earlier tasks (meta connection
    read/write, launch previews, ``/api/v1/audit``) without a contract
    regeneration -- a repo-wide gap this task does not own and does not
    widen its diff to fix.
    """
    live_spec = extract_spec()
    committed_spec = json.loads(CONTRACT_PATH.read_text())

    def scale_paths(spec: dict) -> dict:
        return {
            path: operations
            for path, operations in spec["paths"].items()
            if path.startswith("/api/v1/scale/")
        }

    live_scale_paths = scale_paths(live_spec)
    committed_scale_paths = scale_paths(committed_spec)
    assert live_scale_paths == committed_scale_paths, (
        "Scale routes have drifted from the committed OpenAPI contract -- "
        "run `python scripts/generate_openapi.py` and commit the result."
    )

    referenced: dict = {}
    _collect_referenced_schemas(
        live_scale_paths, live_spec["components"]["schemas"], referenced
    )
    for name, schema in referenced.items():
        assert committed_spec["components"]["schemas"].get(name) == schema, (
            f"Schema {name!r} referenced by a Scale operation has drifted "
            "from the committed OpenAPI contract."
        )


def test_scale_openapi_has_no_meta_mutation_surface() -> None:
    """Global constraint (Issue #25, ADR 0008): Scale never performs Meta
    writes. The committed OpenAPI contract's Scale surface must not contain
    a Meta mutation route or reference an executable Meta mutation schema --
    every Scale schema stays scoped to Scale's own Enrollment/Episode/
    Advice/Target/CommandOutcome vocabulary (spec §4.1-§4.3)."""
    committed_spec = json.loads(CONTRACT_PATH.read_text())

    scale_paths = {
        path: operations
        for path, operations in committed_spec["paths"].items()
        if path.startswith("/api/v1/scale/")
    }
    assert scale_paths, "expected Scale routes in the committed OpenAPI contract"

    # No Scale route is itself a Meta connection/campaign mutation route.
    assert not any("/meta/" in path for path in scale_paths)
    for operations in scale_paths.values():
        for operation in operations.values():
            assert "meta" not in operation.get("operationId", "").lower()

    referenced: dict = {}
    _collect_referenced_schemas(
        scale_paths, committed_spec["components"]["schemas"], referenced
    )

    expected_scale_schemas = {
        "ActionEligibilityModel",
        "ActivationPreviewCreateRequest",
        "ActivationPreviewResponse",
        "AdviceDispositionRequest",
        "AdviceDispositionResponse",
        "AdviceSnoozeRequest",
        "AdviceSnoozeResponse",
        "AdviceThreadListResponse",
        "AdviceThreadResponse",
        "BreakdownResponse",
        "BreakdownSegmentResponse",
        "CandidateCreateRequest",
        "CandidateResponse",
        "CommandOutcomeResponse",
        "ConversionRequest",
        "ConversionResponse",
        # #129: a DeliveryTarget is a Scale record made from an ad Meta has
        # already reported; picking it reads evidence and writes nothing to Meta.
        "DeliveryTargetCreateRequest",
        "DeliveryTargetResponse",
        "EnrollmentActivateRequest",
        "EnrollmentCloseRequest",
        "EnrollmentCreateRequest",
        "EnrollmentEvidenceResponse",
        "EnrollmentListResponse",
        "EnrollmentPauseRequest",
        "EnrollmentResponse",
        "EnrollmentResumeRequest",
        "EvidenceQualityIssueResponse",
        "EvidenceSignalResponse",
        "EvidenceWindowResponse",
        "EpisodeResponse",
        "ExportCreateRequest",
        "ExportDownloadIntentResponse",
        "ExportResponse",
        "ObservedAdListResponse",
        "ObservedAdResponse",
        "ObservedAdUnavailableResponse",
        "ProblemDetails",
        "TargetListResponse",
        "TargetPublicationPreviewRequest",
        "TargetPublicationPreviewResponse",
        "TargetPublishRequest",
        "TargetResponse",
        "TargetWithdrawRequest",
        "TargetWithdrawalPreviewRequest",
        "TargetWithdrawalPreviewResponse",
        # FastAPI's standard 422 validation-error shape, not Scale-specific.
        "HTTPValidationError",
        "ValidationError",
    }
    unexpected = set(referenced) - expected_scale_schemas
    assert not unexpected, (
        "Scale operations reference schema(s) outside Scale's own vocabulary "
        f"-- verify none is a Meta write/mutation schema: {sorted(unexpected)}"
    )

    meta_write_markers = (
        "create",
        "update",
        "mutate",
        "write",
        "publish",
        "activate",
        "pause",
        "resume",
        "retire",
        "supersede",
        "bind",
    )
    for name in referenced:
        lowered = name.lower()
        if "meta" in lowered:
            assert not any(marker in lowered for marker in meta_write_markers), (
                f"Scale references a Meta-mutation-shaped schema: {name}"
            )


def test_every_operation_with_a_request_body_has_a_bff_body_limit() -> None:
    """The BFF reads a declared JSON body through ``readLimitedBody`` capped
    at the policy's ``maxRequestBytes``, so a 0-byte limit on an operation
    whose contract declares a ``requestBody`` answers every call with 413
    ``request_body_too_large`` before it reaches FastAPI. That shipped twice:
    ``revoke_credential`` (every Settings "Remove") and
    ``archive_library_entry`` (every Library Archive, whose dialog always
    sends ``impact_id``/``impact_hash``)."""
    committed_spec = json.loads(CONTRACT_PATH.read_text())
    policy = json.loads(
        CONTRACT_PATH.with_name("marketai-domain-v1-policy.json").read_text()
    )

    blocked = sorted(
        operation["operationId"]
        for operations in committed_spec["paths"].values()
        for operation in operations.values()
        if isinstance(operation, dict)
        and "requestBody" in operation
        and policy[operation["operationId"]]["maxRequestBytes"] <= 0
    )
    assert not blocked, (
        "Operation(s) declare a request body but the BFF policy allows 0 "
        f"bytes, so every call fails with 413: {blocked}"
    )


def test_strip_titles_preserves_real_title_property() -> None:
    schema = {
        "title": "Problem",
        "properties": {
            "title": {"title": "Title", "type": "string"},
            "status": {"title": "Status", "type": "integer"},
        },
        "required": ["title", "status"],
    }

    assert _strip_titles(schema) == {
        "properties": {
            "title": {"type": "string"},
            "status": {"type": "integer"},
        },
        "required": ["title", "status"],
    }


def test_normalize_responses_projects_runtime_problem_details() -> None:
    responses = {
        "201": {
            "description": "Created",
            "content": {
                "application/json": {"schema": {"type": "object"}}
            },
        },
        "422": {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": "#/components/schemas/HTTPValidationError"
                    }
                }
            },
        },
    }

    normalized = _normalize_responses(responses)

    assert normalized["201"] == responses["201"]
    assert normalized["422"] == {
        "description": "Validation Error",
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"}
            }
        },
    }
