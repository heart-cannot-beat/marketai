# Experiments Duplicate Resolution & Persisted Browser Acceptance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the experiments duplicate-concept resolution flow end-to-end — analysis endpoint, freeze submission with stale detection, frozen evidence display, FakeMetaAdapter fault consumption, controlled-mock Launch admission, saga/recovery durability — and replace the route-mocked Playwright tests with real PostgreSQL/FakeMetaAdapter browser journeys.

**Architecture:** The backend extracts a shared candidate-loading/detection seam from `_apply_concept_resolution_plan`, exposes it through a new read-only `GET /duplicate-analysis` endpoint, and extends freeze with `analyzed_candidate_set_hash` for stale detection. The frontend adds a resolution panel in the main column, wires freeze submission with the full plan, and displays frozen evidence. E2E tests follow the localization pattern: real PostgreSQL, FakeMetaAdapter, no route interception.

**Tech Stack:** Python/FastAPI/SQLAlchemy (backend), React/TypeScript (frontend), Playwright (E2E), PostgreSQL (test database), OpenAPI codegen pipeline.

**Spec:** `docs/superpowers/specs/2026-09-14-experiments-duplicate-resolution-persisted-acceptance-design.md`

## Global Constraints

- No Meta writes: `META_WRITE_ACCESS_TOKEN` stays empty; FakeMetaAdapter for all sandbox/mock paths.
- All FakeMetaAdapter objects must have `status: "PAUSED"`.
- Fault injection only in `controlled_mock` evidence mode (ADR 0009). `customer_sandbox` remains fault-free.
- `internal_validation` cannot execute Launch writes.
- FastAPI derives Workspace identity from verified BFF claims via `TenantContext`, never from URL paths.
- Immutable versions and append-only evidence. Current state is a projection.
- Recovery never resumes `execute_provisioning` — it only classifies uncertain intents via read-back.
- Winner is written as append-only `ExperimentDecisionRecord` / `ContextualVerdictProjectionRecord`, NOT onto AssetVersion.
- Presentation fields (`display_label`, `concept_key_name`) are excluded from all hashes and ordering identity.
- Run `make verify` to validate all changes.

---

### Task 1: Backend — Shared candidate seam + analysis endpoint

**Files:**
- Modify: `app/experiments/service.py:1090-1282` (extract seam), `:1888-1966` (share loading)
- Modify: `app/experiments/v1_router.py:258-277` (add endpoint)
- Modify: `app/experiments/response_models.py` (add models)
- Test: `tests/test_experiments_v1_api.py` (add analysis tests)

**Interfaces:**
- Consumes: `_apply_concept_resolution_plan` (existing, lines 1090-1282), `_source_fingerprint` (line 1072), `_duplicate_group_hash` (line 937), `_hash_json` (line 933), `freeze_experiment_v2` (line 1888)
- Produces:
  - `_load_candidate_sources(session, context, experiment) -> list[FrozenCandidateInput]` — shared loading
  - `_detect_duplicate_groups(sources: list[FrozenCandidateInput]) -> tuple[str, bool, list[dict]]` — returns `(candidate_set_hash, has_duplicates, groups)`
  - `analyze_duplicates(context, experiment_id) -> dict` — public service function
  - `DuplicateAnalysisResponse`, `DuplicateGroup`, `DuplicateGroupMember`, `RemapTarget` — Pydantic response models
  - `GET /api/v1/experiments/{experiment_id}/duplicate-analysis` — router endpoint

- [ ] **Step 1: Add response models to `response_models.py`**

Add after line 177 (end of file):

```python
class RemapTarget(BaseModel):
    concept_key_id: str
    concept_key_name: str


class DuplicateGroupMember(BaseModel):
    source_entry_id: str
    source_candidate_fingerprint: str = Field(min_length=64, max_length=64)
    asset_version_id: str
    display_label: str


class DuplicateGroup(BaseModel):
    duplicate_group_hash: str = Field(min_length=64, max_length=64)
    dimension_key: str
    concept_key_id: str
    concept_key_name: str
    members: list[DuplicateGroupMember] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    remap_targets: list[RemapTarget] = Field(default_factory=list)


class DuplicateAnalysisResponse(BaseModel):
    experiment_id: str
    analyzed_revision: int
    candidate_set_hash: str = Field(min_length=64, max_length=64)
    has_duplicates: bool
    duplicate_groups: list[DuplicateGroup] = Field(default_factory=list)
```

- [ ] **Step 2: Extract `_load_candidate_sources` from `freeze_experiment_v2`**

In `app/experiments/service.py`, extract lines 1905-1966 (the candidate-loading loop from `freeze_experiment_v2`) into a standalone function. Place it before `_apply_concept_resolution_plan` (before line 1090):

```python
def _load_candidate_sources(
    session: Session,
    context: TenantContext,
    experiment: ExperimentRecord,
) -> list[FrozenCandidateInput]:
    """Load and resolve draft candidate sources for an experiment.

    Shared seam used by both analysis and freeze. Pure read — no mutations.
    """
    entries = session.scalars(
        select(ExperimentEntryRecord)
        .where(ExperimentEntryRecord.experiment_id == experiment.id)
        .order_by(ExperimentEntryRecord.position)
    ).all()

    asset_versions = session.scalars(
        select(AssetVersionRecord).where(
            AssetVersionRecord.workspace_id == context.workspace_id,
            AssetVersionRecord.id.in_(sorted(entry.asset_version_id for entry in entries)),
        ).order_by(AssetVersionRecord.id)
    ).all()
    asset_versions_by_id = {row.id: row for row in asset_versions}

    sources: list[FrozenCandidateInput] = []
    for entry in entries:
        av = asset_versions_by_id.get(entry.asset_version_id)
        asset = session.get(AssetRecord, av.asset_id) if av is not None else None
        if (
            av is None
            or av.workspace_id != context.workspace_id
            or asset is None
            or asset.workspace_id != context.workspace_id
            or asset.product_id != experiment.product_id
        ):
            raise ExperimentError(f"AssetVersion missing: {entry.asset_version_id}")

        mvars_json = _canonical_json(entry.marketing_vars_json)
        mvars_hash = _sha256(mvars_json.encode("utf-8"))

        execution_mode, mode_evidence = _candidate_execution_mode(
            session, asset_version=av, asset=asset,
        )
        concepts, source_run_item_id, concept_source_evidence = _candidate_concepts(
            session, context=context, asset_version=av,
        )
        source_kind = av.origin_kind
        if source_kind == "legacy":
            source_kind = "generation" if source_run_item_id or asset.run_id else "manual"
        elif source_kind == "localized":
            source_kind = "reuse"
        if source_kind not in {"generation", "upload", "reuse", "manual"}:
            raise ExperimentConflict(f"candidate_source_kind_invalid:{av.id}")
        sources.append(FrozenCandidateInput(
            entry=entry,
            asset_version=av,
            source_kind=source_kind,
            source_run_item_id=source_run_item_id,
            source_evidence={
                "asset_origin_kind": av.origin_kind,
                "execution_mode": mode_evidence,
                "concept_source": concept_source_evidence,
            },
            marketing_vars_hash=mvars_hash,
            execution_mode=execution_mode,
            concepts=concepts,
        ))
    return sources
```

Then update `freeze_experiment_v2` (lines 1905-1966) to call `_load_candidate_sources`:

```python
# Replace lines 1905-1966 in freeze_experiment_v2 with:
        sources = _load_candidate_sources(session, context, experiment)
```

The `with_for_update()` lock on asset versions stays in freeze only. The shared loader does not acquire `FOR UPDATE` — analysis is read-only. Add the lock in `freeze_experiment_v2` after calling `_load_candidate_sources`:

```python
        sources = _load_candidate_sources(session, context, experiment)
        # Re-acquire FOR UPDATE on asset versions for freeze atomicity
        session.scalars(
            select(AssetVersionRecord).where(
                AssetVersionRecord.workspace_id == context.workspace_id,
                AssetVersionRecord.id.in_(sorted(s.asset_version.id for s in sources)),
            ).order_by(AssetVersionRecord.id).with_for_update()
        ).all()
```

- [ ] **Step 3: Extract `_detect_duplicate_groups` from `_apply_concept_resolution_plan`**

Extract lines 1098-1113 of `_apply_concept_resolution_plan` into a pure function. Place it just before `_apply_concept_resolution_plan`:

```python
def _detect_duplicate_groups(
    sources: list[FrozenCandidateInput],
) -> tuple[str, bool, list[dict[str, Any]]]:
    """Detect duplicate-concept groups from resolved candidates.

    Returns (candidate_set_hash, has_duplicates, groups).
    Groups are deterministically ordered by (dimension_key, concept_key_id).
    """
    fingerprinted = [
        replace(source, source_fingerprint=_source_fingerprint(source))
        for source in sources
    ]

    candidate_set_hash = _hash_json(
        sorted(source.source_fingerprint for source in fingerprinted)
    )

    all_groups: dict[str, dict[str, Any]] = {}
    for source in fingerprinted:
        for concept in source.concepts:
            group = all_groups.setdefault(concept.duplicate_group_hash, {
                "dimension_key": concept.dimension_key,
                "concept_key_id": concept.concept_key_id,
                "concept_key_name": concept.concept_key_name,
                "members": [],
            })
            group["members"].append(source)

    duplicate_groups = {
        group_hash: group
        for group_hash, group in all_groups.items()
        if len(group["members"]) > 1
    }

    groups_list: list[dict[str, Any]] = []
    for group_hash in sorted(
        duplicate_groups,
        key=lambda gh: (
            duplicate_groups[gh]["dimension_key"],
            duplicate_groups[gh]["concept_key_id"],
        ),
    ):
        group = duplicate_groups[group_hash]
        members_sorted = sorted(
            group["members"],
            key=lambda s: s.source_fingerprint,
        )
        member_count = len(members_sorted)
        allowed_actions = ["allow_controlled_duplicate"]
        if member_count == 2:
            allowed_actions.append("exclude_source_candidate")

        groups_list.append({
            "duplicate_group_hash": group_hash,
            "dimension_key": group["dimension_key"],
            "concept_key_id": group["concept_key_id"],
            "concept_key_name": group["concept_key_name"],
            "members": [
                {
                    "source_entry_id": m.entry.id,
                    "source_candidate_fingerprint": m.source_fingerprint,
                    "asset_version_id": m.asset_version.id,
                    "display_label": m.entry.id,
                }
                for m in members_sorted
            ],
            "allowed_actions": allowed_actions,
            "_sources": members_sorted,
        })

    return candidate_set_hash, bool(duplicate_groups), groups_list
```

Then update `_apply_concept_resolution_plan` to call this:

```python
def _apply_concept_resolution_plan(
    session: Session,
    *,
    context: TenantContext,
    sources: list[FrozenCandidateInput],
    concept_resolution_plan: list[dict] | None,
    candidate_set_hash: str | None = None,
) -> tuple[list[FrozenCandidateInput], list[dict[str, Any]], str]:
    """Validate a command-scoped plan and return survivors + evidence rows."""
    detected_hash, has_duplicates, detected_groups = _detect_duplicate_groups(sources)

    if candidate_set_hash is not None and candidate_set_hash != detected_hash:
        raise ExperimentConflict("concept_resolution_stale")

    # Rebuild the source-fingerprinted list and duplicate_groups dict
    sources = [
        replace(source, source_fingerprint=_source_fingerprint(source))
        for source in sources
    ]
    duplicate_groups: dict[str, dict[str, Any]] = {}
    for source in sources:
        for concept in source.concepts:
            group = duplicate_groups.setdefault(concept.duplicate_group_hash, {
                "dimension_key": concept.dimension_key,
                "concept_key_id": concept.concept_key_id,
                "members": [],
            })
            group["members"].append(source)
    duplicate_groups = {
        gh: g for gh, g in duplicate_groups.items()
        if len(g["members"]) > 1
    }

    # ... rest of the existing validation and application logic unchanged ...
```

Note: keep lines 1115-1282 exactly as they are. Only the first 25 lines of the function body change — the duplicate detection is delegated to `_detect_duplicate_groups`, and a new `candidate_set_hash` parameter enables stale detection.

- [ ] **Step 4: Add remap target computation**

In `_detect_duplicate_groups`, the remap targets need workspace context. Add a helper that the analysis endpoint calls after detection:

```python
def _compute_remap_targets(
    session: Session,
    context: TenantContext,
    groups: list[dict[str, Any]],
) -> None:
    """Mutate groups in place to add remap_targets and update allowed_actions."""
    for group in groups:
        if len(group["members"]) != 2:
            group["remap_targets"] = []
            continue
        current_key_id = group["concept_key_id"]
        candidates = session.scalars(
            select(DimensionConceptKeyRecord).where(
                DimensionConceptKeyRecord.id != current_key_id,
                (
                    (DimensionConceptKeyRecord.workspace_id == context.workspace_id)
                    | (DimensionConceptKeyRecord.workspace_id.is_(None))
                ),
            ).order_by(DimensionConceptKeyRecord.id)
        ).all()
        targets = [
            {"concept_key_id": k.id, "concept_key_name": k.name}
            for k in candidates
        ]
        group["remap_targets"] = targets
        if targets:
            group["allowed_actions"].append("remap_before_freeze")
```

- [ ] **Step 5: Add `analyze_duplicates` service function**

```python
def analyze_duplicates(
    context: TenantContext,
    experiment_id: str,
) -> dict:
    """Read-only duplicate-concept analysis for the pre-freeze UI."""
    with tenant_session(context) as session:
        experiment = _get_experiment(session, context, experiment_id)
        _require_schema2(experiment)

        sources = _load_candidate_sources(session, context, experiment)
        candidate_set_hash, has_duplicates, groups = _detect_duplicate_groups(sources)
        _compute_remap_targets(session, context, groups)

        # Strip internal _sources key before returning
        for g in groups:
            g.pop("_sources", None)

        return {
            "experiment_id": experiment.id,
            "analyzed_revision": experiment.revision,
            "candidate_set_hash": candidate_set_hash,
            "has_duplicates": has_duplicates,
            "duplicate_groups": groups,
        }
```

- [ ] **Step 6: Add router endpoint in `v1_router.py`**

Add after the `get_experiment` endpoint and before `freeze_experiment` (around line 258). Also add the import for `DuplicateAnalysisResponse` to the top of the file:

```python
@router.get(
    "/{experiment_id}/duplicate-analysis",
    operation_id="analyze_experiment_duplicates",
    response_model=DuplicateAnalysisResponse,
)
def analyze_experiment_duplicates(
    experiment_id: str,
    context: TenantContext = Depends(request_tenant_context),
):
    return _translate(
        lambda: service.analyze_duplicates(context, experiment_id)
    )
```

- [ ] **Step 7: Write tests for analysis endpoint**

Add to `tests/test_experiments_v1_api.py`:

```python
def test_openapi_has_analyze_duplicates_operation(self) -> None:
    spec = app.openapi()
    path = "/api/v1/experiments/{experiment_id}/duplicate-analysis"
    assert "get" in spec["paths"][path]
    assert spec["paths"][path]["get"]["operationId"] == "analyze_experiment_duplicates"
```

Add a new test file `tests/test_duplicate_analysis.py` for service-level tests:

```python
"""Tests for the duplicate-analysis seam and endpoint (spec §1.1–§1.3)."""
from __future__ import annotations

import pytest
from app.experiments.service import (
    _detect_duplicate_groups,
    _hash_json,
    _source_fingerprint,
    analyze_duplicates,
)


class TestDetectDuplicateGroups:
    def test_no_duplicates_returns_empty(self, sources_no_dups):
        hash_, has_dups, groups = _detect_duplicate_groups(sources_no_dups)
        assert not has_dups
        assert groups == []
        assert len(hash_) == 64

    def test_duplicate_pair_detected(self, sources_with_dup_pair):
        hash_, has_dups, groups = _detect_duplicate_groups(sources_with_dup_pair)
        assert has_dups
        assert len(groups) == 1
        assert len(groups[0]["members"]) == 2
        assert "allow_controlled_duplicate" in groups[0]["allowed_actions"]
        assert "exclude_source_candidate" in groups[0]["allowed_actions"]

    def test_triple_group_only_allows_allow(self, sources_with_triple):
        _, _, groups = _detect_duplicate_groups(sources_with_triple)
        assert len(groups) == 1
        assert groups[0]["allowed_actions"] == ["allow_controlled_duplicate"]

    def test_deterministic_ordering_and_hashes(self, sources_with_dup_pair):
        h1, _, g1 = _detect_duplicate_groups(sources_with_dup_pair)
        h2, _, g2 = _detect_duplicate_groups(sources_with_dup_pair)
        assert h1 == h2
        assert g1[0]["duplicate_group_hash"] == g2[0]["duplicate_group_hash"]
        assert [m["source_candidate_fingerprint"] for m in g1[0]["members"]] == \
               [m["source_candidate_fingerprint"] for m in g2[0]["members"]]

    def test_groups_sorted_by_dimension_key_then_concept_key(self, sources_multi_group):
        _, _, groups = _detect_duplicate_groups(sources_multi_group)
        keys = [(g["dimension_key"], g["concept_key_id"]) for g in groups]
        assert keys == sorted(keys)
```

These tests require fixture helpers that build `FrozenCandidateInput` objects with controlled concept keys. Place those fixtures in a `tests/conftest_experiments.py` or in the same file as `conftest.py`. The fixtures must create real `ExperimentEntryRecord` and `AssetVersionRecord` stubs with matching concept data.

Add the following tests (spec §1.8 items 1, 7, 8, 14):

```python
class TestAnalysisFreezeDetectionParity:
    def test_analysis_and_freeze_produce_identical_detection(self, ctx, experiment_with_dups, valid_hash):
        """Spec §1.8 #1: analysis and freeze cross same detection seam."""
        analysis = analyze_duplicates(ctx, experiment_with_dups.id)
        # Freeze with allow_controlled_duplicate for all groups
        plan = [
            {
                "duplicate_group_hash": g["duplicate_group_hash"],
                "action": "allow_controlled_duplicate",
                "reason": "parity test",
                "source_candidate_fingerprints": sorted(
                    m["source_candidate_fingerprint"] for m in g["members"]
                ),
            }
            for g in analysis["duplicate_groups"]
        ]
        freeze_experiment_v2(
            ctx,
            experiment_with_dups.id,
            experiment_with_dups.revision,
            concept_resolution_plan=plan,
            analyzed_candidate_set_hash=analysis["candidate_set_hash"],
        )
        # Both used the same candidate_set_hash and duplicate_group_hashes

    def test_remap_creating_collision_rejected(self, ctx, experiment_with_remap_collision, valid_hash):
        """Spec §1.8 #7: remap producing new collision → concept_resolution_invalid."""
        analysis = analyze_duplicates(ctx, experiment_with_remap_collision.id)
        group = analysis["duplicate_groups"][0]
        plan = [{
            "duplicate_group_hash": group["duplicate_group_hash"],
            "action": "remap_before_freeze",
            "reason": "test remap",
            "source_candidate_fingerprints": sorted(
                m["source_candidate_fingerprint"] for m in group["members"]
            ),
            "source_candidate_fingerprint": group["members"][0]["source_candidate_fingerprint"],
            "remap_concept_key_id": "key_that_causes_collision",
        }]
        with pytest.raises(ExperimentError, match="remap"):
            freeze_experiment_v2(
                ctx,
                experiment_with_remap_collision.id,
                experiment_with_remap_collision.revision,
                concept_resolution_plan=plan,
                analyzed_candidate_set_hash=analysis["candidate_set_hash"],
            )


class TestCrossWorkspaceRejection:
    def test_analysis_rejects_foreign_workspace(self, other_ctx, experiment_in_workspace_a):
        """Spec §1.8 #8: cross-workspace analysis returns 404."""
        with pytest.raises(ExperimentNotFound):
            analyze_duplicates(other_ctx, experiment_in_workspace_a.id)

    def test_freeze_rejects_foreign_workspace(self, other_ctx, experiment_in_workspace_a):
        """Spec §1.8 #8: cross-workspace freeze returns 404."""
        with pytest.raises(ExperimentNotFound):
            freeze_experiment_v2(
                other_ctx,
                experiment_in_workspace_a.id,
                1,
                analyzed_candidate_set_hash="0" * 64,
            )


class TestRemapTargets:
    def test_includes_platform_and_workspace_keys(self, ctx, sources_with_dup_pair, session):
        """Spec §1.8 #14: remap targets include platform + workspace keys."""
        _, _, groups = _detect_duplicate_groups(sources_with_dup_pair)
        _compute_remap_targets(session, ctx, groups)
        targets = groups[0]["remap_targets"]
        target_ids = {t["concept_key_id"] for t in targets}
        # Must include platform-scoped and workspace-scoped keys
        assert len(targets) > 0
        # Must exclude current concept key
        assert groups[0]["concept_key_id"] not in target_ids

    def test_excludes_current_concept_key(self, ctx, sources_with_dup_pair, session):
        """Spec §1.8 #14: remap targets exclude current key."""
        _, _, groups = _detect_duplicate_groups(sources_with_dup_pair)
        _compute_remap_targets(session, ctx, groups)
        current_key_id = groups[0]["concept_key_id"]
        assert all(
            t["concept_key_id"] != current_key_id
            for t in groups[0]["remap_targets"]
        )

    def test_remap_targets_deterministic_order(self, ctx, sources_with_dup_pair, session):
        """Spec §1.8 #14: targets ordered by concept_key_id."""
        _, _, groups = _detect_duplicate_groups(sources_with_dup_pair)
        _compute_remap_targets(session, ctx, groups)
        ids = [t["concept_key_id"] for t in groups[0]["remap_targets"]]
        assert ids == sorted(ids)
```

- [ ] **Step 8: Run tests**

```bash
pytest tests/test_experiments_v1_api.py tests/test_duplicate_analysis.py -v
```

Expected: All new tests pass, existing tests unbroken.

- [ ] **Step 9: Verify `freeze_experiment_v2` still works**

```bash
pytest tests/test_experiment_lifecycle.py tests/test_freeze_gates.py -v
```

Expected: All existing freeze tests pass — `freeze_experiment_v2` now calls `_load_candidate_sources` instead of inline loading.

- [ ] **Step 10: Commit**

```bash
git add app/experiments/service.py app/experiments/v1_router.py \
       app/experiments/response_models.py tests/test_duplicate_analysis.py \
       tests/test_experiments_v1_api.py
git commit -m "$(cat <<'EOF'
feat(experiments): add shared candidate seam and duplicate-analysis endpoint (#88)

Extract _load_candidate_sources and _detect_duplicate_groups from
freeze_experiment_v2 and _apply_concept_resolution_plan. Add read-only
GET /duplicate-analysis endpoint with DuplicateAnalysisResponse models.
Both analysis and freeze cross the same detection seam.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 2: Backend — Freeze extension with `analyzed_candidate_set_hash` + refined error codes

**Files:**
- Modify: `app/experiments/v1_router.py:52-71` (request model), `:99-117` (error translation)
- Modify: `app/experiments/service.py` (`_apply_concept_resolution_plan`, `freeze_experiment_v2`)
- Test: `tests/test_duplicate_analysis.py`, `tests/test_freeze_gates.py`

**Interfaces:**
- Consumes: `_detect_duplicate_groups` (Task 1), `_load_candidate_sources` (Task 1)
- Produces:
  - `FreezeExperimentRequest.analyzed_candidate_set_hash: str` — new required field
  - `concept_resolution_stale` / `concept_unresolved` / `concept_resolution_invalid` / `revision_conflict` — refined error codes
  - `freeze_experiment_v2(..., analyzed_candidate_set_hash=...)` — new parameter

- [ ] **Step 1: Add `analyzed_candidate_set_hash` to `FreezeExperimentRequest`**

In `app/experiments/v1_router.py`, modify the `FreezeExperimentRequest` at line 68:

```python
class FreezeExperimentRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    analyzed_candidate_set_hash: str = Field(min_length=64, max_length=64)
    concept_resolution_plan: list[ConceptResolutionActionRequest] = Field(default_factory=list)
```

- [ ] **Step 2: Wire the new field through the router**

Update the `freeze_experiment` endpoint (line 270-276) to pass the hash:

```python
    return _translate(
        lambda: service.freeze_experiment_v2(
            context,
            experiment_id,
            body.expected_revision,
            concept_resolution_plan=[item.model_dump() for item in body.concept_resolution_plan],
            analyzed_candidate_set_hash=body.analyzed_candidate_set_hash,
        )
    )
```

- [ ] **Step 3: Update `freeze_experiment_v2` signature and pass hash through**

In `app/experiments/service.py`, update `freeze_experiment_v2` at line 1888:

```python
def freeze_experiment_v2(
    context: TenantContext,
    experiment_id: str,
    expected_revision: int,
    *,
    concept_resolution_plan: list[dict] | None = None,
    analyzed_candidate_set_hash: str | None = None,
) -> dict:
```

Pass it to `_apply_concept_resolution_plan` at lines 1975-1980:

```python
        survivors, resolution_evidence, resolutions_hash = _apply_concept_resolution_plan(
            session,
            context=context,
            sources=sources,
            concept_resolution_plan=concept_resolution_plan,
            candidate_set_hash=analyzed_candidate_set_hash,
        )
```

- [ ] **Step 4: Refine error translation in the router**

Replace the `_translate` function (lines 99-117) with specific error code mapping:

```python
def _translate(call):
    try:
        return call()
    except service.SchemaVersionMismatch as exc:
        raise HTTPException(
            409, {"code": "schema_version_mismatch", "message": str(exc)}
        ) from exc
    except service.ExperimentNotFound as exc:
        raise HTTPException(
            404, {"code": "experiment_not_found", "message": str(exc)}
        ) from exc
    except service.ExperimentConflict as exc:
        msg = str(exc)
        if msg.startswith("concept_resolution_stale"):
            code = "concept_resolution_stale"
        elif msg.startswith("concept_unresolved"):
            code = "concept_unresolved"
        elif msg.startswith("revision_conflict") or msg.startswith("stale_revision"):
            code = "revision_conflict"
        else:
            code = "experiment_conflict"
        raise HTTPException(409, {"code": code, "message": msg}) from exc
    except service.ExperimentError as exc:
        msg = str(exc)
        if any(msg.startswith(p) for p in (
            "exclude target", "remap target", "remap concept key",
            "allow_controlled_duplicate does not accept",
            "exclude_source_candidate does not accept",
            "unknown duplicate group", "invalid concept resolution",
            "concept resolution requires",
        )):
            code = "concept_resolution_invalid"
        else:
            code = "experiment_invalid"
        raise HTTPException(422, {"code": code, "message": msg}) from exc
```

- [ ] **Step 5: Write stale detection tests**

Add to `tests/test_duplicate_analysis.py`:

```python
class TestStaleDetection:
    def test_stale_candidate_set_hash_rejected(self, ctx, experiment_with_dups):
        """Spec §1.4/§3.2 Scenario 2b: fabricated hash with valid revision -> concept_resolution_stale.
        This is the service-level precondition test for Scenario 2b (no browser scenario exists)."""
        with pytest.raises(ExperimentConflict, match="concept_resolution_stale"):
            freeze_experiment_v2(
                ctx,
                experiment_with_dups.id,
                experiment_with_dups.revision,
                analyzed_candidate_set_hash="0" * 64,
                concept_resolution_plan=[],
            )

    def test_stale_revision_rejected(self, ctx, experiment_with_dups):
        """Spec §1.5: wrong expected_revision -> revision_conflict."""
        with pytest.raises(ExperimentConflict, match="stale_revision"):
            freeze_experiment_v2(
                ctx,
                experiment_with_dups.id,
                experiment_with_dups.revision + 999,
                analyzed_candidate_set_hash="0" * 64,
            )

    def test_stale_member_fingerprints_rejected(self, ctx, experiment_with_dups, valid_hash):
        """Spec §1.5: plan with wrong member fingerprints -> concept_resolution_stale."""
        plan = [{
            "duplicate_group_hash": experiment_with_dups.group_hash,
            "action": "allow_controlled_duplicate",
            "reason": "test",
            "source_candidate_fingerprints": ["bad" * 11],  # wrong fingerprints
        }]
        with pytest.raises(ExperimentConflict, match="concept_resolution_stale"):
            freeze_experiment_v2(
                ctx,
                experiment_with_dups.id,
                experiment_with_dups.revision,
                concept_resolution_plan=plan,
                analyzed_candidate_set_hash=valid_hash,
            )
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/test_duplicate_analysis.py tests/test_freeze_gates.py tests/test_experiments_v1_api.py -v
```

Expected: All pass. Existing freeze tests may need their request body updated to include `analyzed_candidate_set_hash`.

- [ ] **Step 7: Update existing freeze tests for new required field**

Search all test files that call the freeze endpoint or `freeze_experiment_v2` and add the `analyzed_candidate_set_hash` parameter. Each must first call `analyze_duplicates` or `_detect_duplicate_groups` to obtain a valid hash, or use a test helper:

```python
def _analyze_and_freeze(ctx, experiment_id, revision, plan=None):
    """Helper: analyze then freeze in one call with valid hash."""
    analysis = analyze_duplicates(ctx, experiment_id)
    return freeze_experiment_v2(
        ctx, experiment_id, revision,
        concept_resolution_plan=plan,
        analyzed_candidate_set_hash=analysis["candidate_set_hash"],
    )
```

- [ ] **Step 8: Commit**

```bash
git add app/experiments/v1_router.py app/experiments/service.py \
       tests/test_duplicate_analysis.py tests/test_freeze_gates.py \
       tests/test_experiment_lifecycle.py
git commit -m "$(cat <<'EOF'
feat(experiments): add analyzed_candidate_set_hash to freeze + refined error codes (#88)

FreezeExperimentRequest now requires analyzed_candidate_set_hash for
stale detection. Error translation maps specific ExperimentConflict
prefixes to concept_resolution_stale, concept_unresolved, revision_conflict,
and concept_resolution_invalid problem-detail codes.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 3: Backend — Frozen resolution evidence + three-way runtime invariant

**Files:**
- Modify: `app/experiments/response_models.py` (add evidence models)
- Modify: `app/experiments/service.py` (populate evidence, add invariant check)
- Test: `tests/test_duplicate_analysis.py` (evidence + invariant tests)

**Interfaces:**
- Consumes: `ExperimentVersionResolutionRecord` (existing model), `ExperimentVersionRecord.concept_resolutions_hash` (existing column), `ExperimentVersionConceptRecord` (existing model), `_hash_json` (Task 1)
- Produces:
  - `ConceptResolutionEvidence` on `ExperimentDetail` — `concept_resolution_evidence` field
  - `ResolutionEvidenceRow` — per-resolution row
  - Three-way invariant check in `_experiment_dict_v2`

- [ ] **Step 1: Add evidence models to `response_models.py`**

```python
class ResolutionEvidenceRow(BaseModel):
    duplicate_group_hash: str
    dimension_key: str
    concept_key_id: str
    concept_key_name: str
    action: str
    reason: str
    affected_candidate_fingerprint: str | None = None
    remap_destination_concept_key_id: str | None = None
    remap_destination_concept_key_name: str | None = None
    actor_role: str
    resolved_at: datetime


class ConceptResolutionEvidence(BaseModel):
    concept_resolutions_hash: str
    had_duplicates: bool
    resolutions: list[ResolutionEvidenceRow] = Field(default_factory=list)
```

Add `concept_resolution_evidence` to `ExperimentDetail`:

```python
class ExperimentDetail(BaseModel):
    # ... existing fields ...
    concept_resolution_evidence: ConceptResolutionEvidence | None = None
```

- [ ] **Step 2: Implement evidence population in `_experiment_dict_v2`**

Find `_experiment_dict_v2` in `service.py`. When an `ExperimentVersionRecord` exists, query resolution records and build evidence:

```python
    # After loading version_record, if it exists:
    if version_record is not None:
        resolution_records = list(session.scalars(
            select(ExperimentVersionResolutionRecord).where(
                ExperimentVersionResolutionRecord.experiment_version_id == version_record.id,
            ).order_by(ExperimentVersionResolutionRecord.duplicate_group_hash)
        ).all())

        concept_records_by_key: dict[str, ExperimentVersionConceptRecord] = {}
        for cr in session.scalars(
            select(ExperimentVersionConceptRecord).where(
                ExperimentVersionConceptRecord.experiment_version_id == version_record.id,
            )
        ).all():
            concept_records_by_key[cr.concept_key_id] = cr

        evidence_rows = []
        for res in resolution_records:
            outcome = res.outcome_evidence_json or {}
            concept_rec = concept_records_by_key.get(res.concept_key_id)
            concept_key_name = concept_rec.concept_key_name if concept_rec else res.concept_key_id

            affected_fp = None
            remap_dest_id = None
            remap_dest_name = None
            if res.action == "exclude_source_candidate":
                excluded = outcome.get("excluded_source_fingerprints", [])
                affected_fp = excluded[0] if excluded else None
            elif res.action == "remap_before_freeze":
                affected_fp = outcome.get("remapped_source_fingerprint")
                remap_dest_id = outcome.get("to_concept_key_id")
                remap_dest_name = outcome.get("to_concept_key_name")

            evidence_rows.append({
                "duplicate_group_hash": res.duplicate_group_hash,
                "dimension_key": res.dimension_key,
                "concept_key_id": res.concept_key_id,
                "concept_key_name": concept_key_name,
                "action": res.action,
                "reason": res.reason,
                "affected_candidate_fingerprint": affected_fp,
                "remap_destination_concept_key_id": remap_dest_id,
                "remap_destination_concept_key_name": remap_dest_name,
                "actor_role": res.actor_role,
                "resolved_at": res.resolved_at,
            })

        # Three-way invariant check
        _verify_resolution_invariant(
            version_record, resolution_records, evidence_rows
        )

        detail["concept_resolution_evidence"] = {
            "concept_resolutions_hash": version_record.concept_resolutions_hash,
            "had_duplicates": len(resolution_records) > 0,
            "resolutions": evidence_rows,
        }
```

- [ ] **Step 3: Implement three-way invariant check**

```python
def _verify_resolution_invariant(
    version_record: ExperimentVersionRecord,
    resolution_records: list[ExperimentVersionResolutionRecord],
    evidence_rows: list[dict[str, Any]],
) -> None:
    """Three-way consistency check (spec §1.6): rows, hash, and snapshot must agree."""
    # 1. Recompute hash from normalized resolution rows
    canonical_rows = [
        {
            "duplicate_group_hash": r.duplicate_group_hash,
            "dimension_key": r.dimension_key,
            "concept_key_id": r.concept_key_id,
            "source_candidate_fingerprints": (
                r.source_candidate_fingerprints_json or {}
            ).get("fingerprints", []),
            "action": r.action,
            "outcome": r.outcome_evidence_json or {},
            "reason": r.reason,
        }
        for r in sorted(resolution_records, key=lambda r: r.duplicate_group_hash)
    ]
    recomputed_hash = _hash_json(canonical_rows)

    # 2. Compare to stored hash
    if recomputed_hash != version_record.concept_resolutions_hash:
        import logging
        logging.getLogger(__name__).error(
            "Resolution invariant violation: recomputed hash %s != stored hash %s "
            "for version %s",
            recomputed_hash, version_record.concept_resolutions_hash, version_record.id,
        )
        raise ExperimentError("resolution_integrity_violation")

    # 3. Compare snapshot plan hash
    snapshot = version_record.frozen_snapshot_json or {}
    snapshot_plan = snapshot.get("concept_resolution_plan", [])
    snapshot_hash = _hash_json(snapshot_plan) if snapshot_plan else _hash_json([])
    if snapshot_hash != version_record.concept_resolutions_hash:
        import logging
        logging.getLogger(__name__).error(
            "Resolution invariant violation: snapshot plan hash %s != stored hash %s "
            "for version %s",
            snapshot_hash, version_record.concept_resolutions_hash, version_record.id,
        )
        raise ExperimentError("resolution_integrity_violation")
```

- [ ] **Step 4: Store `to_concept_key_name` in remap outcome evidence**

In `_apply_concept_resolution_plan`, when building the outcome for `remap_before_freeze` (around line 1206-1211), add the display name:

```python
            outcome = {
                "remaining_source_fingerprints": member_fingerprints,
                "remapped_source_fingerprint": target,
                "from_concept_key_id": group["concept_key_id"],
                "to_concept_key_id": target_key.id,
                "to_concept_key_name": target_key.name,
            }
```

- [ ] **Step 5: Store plan in frozen snapshot**

In `freeze_experiment_v2`, when building `frozen_snapshot_json`, include the canonical resolution plan:

```python
        snapshot_plan = [
            {
                "duplicate_group_hash": row["duplicate_group_hash"],
                "dimension_key": row["dimension_key"],
                "concept_key_id": row["concept_key_id"],
                "source_candidate_fingerprints": row["source_candidate_fingerprints_json"]["fingerprints"],
                "action": row["action"],
                "outcome": row["outcome_evidence_json"],
                "reason": row["reason"],
            }
            for row in resolution_evidence
        ]
        # Include in frozen_snapshot_json:
        frozen_snapshot["concept_resolution_plan"] = snapshot_plan
```

- [ ] **Step 6: Write invariant tests**

```python
class TestThreeWayInvariant:
    def test_valid_freeze_passes_invariant(self, ctx, frozen_experiment):
        """Detail read after freeze passes three-way check."""
        detail = get_experiment_v2(ctx, frozen_experiment.id)
        evidence = detail["concept_resolution_evidence"]
        assert evidence is not None
        assert evidence["concept_resolutions_hash"]

    def test_tampered_resolution_row_fails(self, ctx, frozen_experiment, db_session):
        """Simulated row edit -> 500 (spec §1.8 item 12)."""
        res_row = db_session.scalars(
            select(ExperimentVersionResolutionRecord).where(
                ExperimentVersionResolutionRecord.experiment_version_id
                == frozen_experiment.version_id
            )
        ).first()
        res_row.reason = "TAMPERED"
        db_session.flush()
        with pytest.raises(ExperimentError, match="resolution_integrity_violation"):
            get_experiment_v2(ctx, frozen_experiment.id)

    def test_tampered_snapshot_fails(self, ctx, frozen_experiment, db_session):
        """Simulated snapshot edit -> 500 (spec §1.8 item 12)."""
        version = db_session.get(ExperimentVersionRecord, frozen_experiment.version_id)
        snapshot = dict(version.frozen_snapshot_json)
        snapshot["concept_resolution_plan"] = []
        version.frozen_snapshot_json = snapshot
        db_session.flush()
        with pytest.raises(ExperimentError, match="resolution_integrity_violation"):
            get_experiment_v2(ctx, frozen_experiment.id)

    def test_no_duplicates_returns_explicit_empty_evidence(self, ctx, frozen_no_dups):
        """Spec §1.8 item 10."""
        detail = get_experiment_v2(ctx, frozen_no_dups.id)
        evidence = detail["concept_resolution_evidence"]
        assert evidence["had_duplicates"] is False
        assert evidence["resolutions"] == []

    def test_evidence_identical_across_states(self, ctx, frozen_experiment, running_experiment, closed_experiment):
        """Spec §1.8 item 9: frozen/running/closed show same evidence."""
        e_frozen = get_experiment_v2(ctx, frozen_experiment.id)["concept_resolution_evidence"]
        e_running = get_experiment_v2(ctx, running_experiment.id)["concept_resolution_evidence"]
        e_closed = get_experiment_v2(ctx, closed_experiment.id)["concept_resolution_evidence"]
        assert e_frozen == e_running == e_closed
```

- [ ] **Step 7: Run tests**

```bash
pytest tests/test_duplicate_analysis.py -v -k "Invariant or evidence"
```

- [ ] **Step 8: Commit**

```bash
git add app/experiments/service.py app/experiments/response_models.py \
       tests/test_duplicate_analysis.py
git commit -m "$(cat <<'EOF'
feat(experiments): add frozen resolution evidence + three-way invariant (#88)

ExperimentDetail now includes concept_resolution_evidence whenever an
ExperimentVersionRecord exists. Display names sourced from frozen records,
not live registry. Three-way runtime invariant verifies normalized rows,
concept_resolutions_hash, and frozen_snapshot_json plan all agree.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 4: Backend — FakeMetaAdapter fault consumption

**Files:**
- Modify: `app/adapters/meta/fake_adapter.py:92-130` (consume `_fault_config`)
- Test: `tests/test_fake_meta_adapter.py`

**Interfaces:**
- Consumes: `self._fault_config` (already stored, line 71), `MetaAdapterPort.ensure_object` (existing signature unchanged)
- Produces:
  - `ensure_object` with four deterministic effects: `uncertain_before_apply`, `uncertain_after_apply`, `pause_before_apply`, `return_state_patch`
  - `AdapterFaultTriggered` exception (raised by uncertainty effects)

- [ ] **Step 1: Define `AdapterFaultTriggered` exception**

Add at the top of `fake_adapter.py` after the imports:

```python
import time


class AdapterFaultTriggered(RuntimeError):
    """Raised when a controlled-mock fault injects uncertainty."""

    def __init__(self, effect: str, object_type: str, idempotency_key: str) -> None:
        self.effect = effect
        self.object_type = object_type
        self.idempotency_key = idempotency_key
        super().__init__(f"Fault triggered: {effect} on {object_type}/{idempotency_key}")
```

- [ ] **Step 2: Add fault-matching logic**

Add a method to `FakeMetaAdapter`:

```python
    def _match_fault(self, object_type: str) -> dict[str, Any] | None:
        """Check if a fault rule matches this ensure_object call.

        Rules use match_ordinal (1-based) among ensure_object calls of the
        same object_type within this scenario version.
        """
        rules = (self._fault_config or {}).get("rules", [])
        if not rules:
            return None

        ordinal_key = f"_ordinal_{object_type}"
        current = getattr(self, ordinal_key, 0) + 1
        setattr(self, ordinal_key, current)

        for rule in rules:
            if (
                rule.get("object_type") == object_type
                and rule.get("match_ordinal") == current
            ):
                return rule
        return None
```

- [ ] **Step 3: Rewrite `ensure_object` with fault consumption**

Replace the `ensure_object` method:

```python
    def ensure_object(
        self,
        object_type: str,
        idempotency_key: str,
        state: dict[str, Any],
    ) -> FakeAdapterResult:
        remote_id = self._deterministic_id(object_type, idempotency_key)

        # Idempotent return for already-created objects
        existing = self._session.execute(
            select(FakeMetaObjectRecord).where(
                FakeMetaObjectRecord.workspace_id == self._workspace_id,
                FakeMetaObjectRecord.meta_mode == self._meta_mode,
                FakeMetaObjectRecord.scenario_version_id == self._scenario_version_id,
                FakeMetaObjectRecord.remote_object_id == remote_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return FakeAdapterResult(
                remote_object_id=remote_id,
                object_type=object_type,
                recovered=True,
                state=existing.state,
            )

        fault = self._match_fault(object_type)
        effect = fault.get("effect") if fault else None

        if effect == "uncertain_before_apply":
            raise AdapterFaultTriggered(effect, object_type, idempotency_key)

        if effect == "pause_before_apply":
            duration_ms = fault.get("duration_ms", 0)
            if not isinstance(duration_ms, (int, float)) or duration_ms <= 0:
                raise ValueError("pause_before_apply requires positive duration_ms")
            time.sleep(duration_ms / 1000.0)

        applied_state = dict(state)
        if effect == "return_state_patch":
            patch = fault.get("state_patch", {})
            if "status" in patch:
                raise ValueError("state_patch may not modify status")
            applied_state.update(patch)

        obj = FakeMetaObjectRecord(
            id=new_id("fmo"),
            workspace_id=self._workspace_id,
            meta_mode=self._meta_mode,
            scenario_version_id=self._scenario_version_id,
            object_type=object_type,
            remote_object_id=remote_id,
            state=applied_state,
        )
        self._session.add(obj)
        self._session.flush()

        if effect == "uncertain_after_apply":
            raise AdapterFaultTriggered(effect, object_type, idempotency_key)

        return FakeAdapterResult(
            remote_object_id=remote_id,
            object_type=object_type,
            recovered=False,
            state=applied_state,
        )
```

- [ ] **Step 4: Write fault-effect tests**

In `tests/test_fake_meta_adapter.py`:

```python
class TestFaultConsumption:
    def test_uncertain_before_apply_raises_without_creating(self, db_session):
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id="ws", meta_mode="controlled_mock",
            scenario_version_id="sv1",
            fault_config={"rules": [
                {"object_type": "campaign", "match_ordinal": 1, "effect": "uncertain_before_apply"}
            ]},
        )
        with pytest.raises(AdapterFaultTriggered, match="uncertain_before_apply"):
            adapter.ensure_object("campaign", "key1", {"status": "PAUSED"})
        assert adapter.read_object("campaign", adapter.deterministic_object_id("campaign", "key1")) is None

    def test_uncertain_after_apply_creates_then_raises(self, db_session):
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id="ws", meta_mode="controlled_mock",
            scenario_version_id="sv2",
            fault_config={"rules": [
                {"object_type": "ad", "match_ordinal": 1, "effect": "uncertain_after_apply"}
            ]},
        )
        with pytest.raises(AdapterFaultTriggered, match="uncertain_after_apply"):
            adapter.ensure_object("ad", "key1", {"status": "PAUSED"})
        state = adapter.read_object("ad", adapter.deterministic_object_id("ad", "key1"))
        assert state is not None
        assert state["status"] == "PAUSED"

    def test_return_state_patch_applies_patch(self, db_session):
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id="ws", meta_mode="controlled_mock",
            scenario_version_id="sv3",
            fault_config={"rules": [
                {"object_type": "adset", "match_ordinal": 1,
                 "effect": "return_state_patch",
                 "state_patch": {"daily_budget_minor": 99999}}
            ]},
        )
        result = adapter.ensure_object("adset", "key1", {"status": "PAUSED", "daily_budget_minor": 1000})
        assert result.state["daily_budget_minor"] == 99999
        assert result.state["status"] == "PAUSED"

    def test_state_patch_cannot_modify_status(self, db_session):
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id="ws", meta_mode="controlled_mock",
            scenario_version_id="sv4",
            fault_config={"rules": [
                {"object_type": "ad", "match_ordinal": 1,
                 "effect": "return_state_patch",
                 "state_patch": {"status": "ACTIVE"}}
            ]},
        )
        with pytest.raises(ValueError, match="status"):
            adapter.ensure_object("ad", "key1", {"status": "PAUSED"})

    def test_ordinal_targets_correct_call(self, db_session):
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id="ws", meta_mode="controlled_mock",
            scenario_version_id="sv5",
            fault_config={"rules": [
                {"object_type": "ad", "match_ordinal": 2, "effect": "uncertain_after_apply"}
            ]},
        )
        r1 = adapter.ensure_object("ad", "key1", {"status": "PAUSED"})
        assert not r1.recovered  # first call succeeds
        with pytest.raises(AdapterFaultTriggered):
            adapter.ensure_object("ad", "key2", {"status": "PAUSED"})  # second call faults

    def test_no_fault_config_is_noop(self, db_session):
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id="ws", meta_mode="controlled_mock",
            scenario_version_id="sv6",
        )
        result = adapter.ensure_object("campaign", "key1", {"status": "PAUSED"})
        assert result.state["status"] == "PAUSED"

    def test_customer_sandbox_never_receives_faults(self, db_session):
        """Faults only in controlled_mock per ADR 0009."""
        adapter = FakeMetaAdapter(
            session=db_session, workspace_id="ws", meta_mode="customer_sandbox",
            scenario_version_id="sv7",
        )
        # fault_config is None by default for customer_sandbox
        result = adapter.ensure_object("campaign", "key1", {"status": "PAUSED"})
        assert result.state["status"] == "PAUSED"
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_fake_meta_adapter.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/adapters/meta/fake_adapter.py tests/test_fake_meta_adapter.py
git commit -m "$(cat <<'EOF'
feat(fake-adapter): consume _fault_config in ensure_object (#88)

Implement four deterministic effects: uncertain_before_apply,
uncertain_after_apply, pause_before_apply, return_state_patch.
Ordinal matching targets the Nth ensure_object call per object_type.
State patches cannot modify status (PAUSED enforced).

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 5: Backend — Controlled-mock Launch admission + connections scenario_config

**Files:**
- Modify: `app/launch/api.py:400-409` (mode check)
- Modify: `app/meta/connections.py` (scenario_config param)
- Test: `tests/test_controlled_mock.py`, `tests/test_launch_multi_candidate.py`

**Interfaces:**
- Consumes: `execute_multi_candidate_preview_v1` (line 392), `create_connection` (connections.py line 148)
- Produces:
  - Execute endpoint admits `controlled_mock` alongside `customer_sandbox`
  - `create_connection(..., scenario_config=None)` — optional initial config

- [ ] **Step 1: Update mode check in `api.py`**

Replace lines 402-409:

```python
    if effective.meta_mode not in ("customer_sandbox", "controlled_mock"):
        raise HTTPException(
            403,
            {
                "code": "launch_mode_forbidden",
                "message": (
                    "PAUSED provisioning is available only in "
                    "customer_sandbox and controlled_mock modes"
                ),
            },
        )
```

- [ ] **Step 2: Add `scenario_config` param to `create_connection`**

In `app/meta/connections.py`, find `create_connection` and add the parameter:

```python
def create_connection(
    session: Session,
    *,
    context: TenantContext,
    ad_account_id: str,
    scenario_config: dict[str, Any] | None = None,
) -> MetaConnectionRecord:
```

When creating the default `ScenarioVersionRecord`, use the provided config:

```python
    scenario = ScenarioVersionRecord(
        # ... existing fields ...
        scenario_config=scenario_config or {},
    )
```

- [ ] **Step 3: Write tests**

```python
class TestControlledMockAdmission:
    def test_controlled_mock_admitted(self, ctx_controlled_mock, preview_id):
        """Execute endpoint admits controlled_mock."""
        # Should not raise launch_mode_forbidden
        result = execute_multi_candidate_preview_v1(ctx_controlled_mock, preview_id)
        assert result is not None

    def test_internal_validation_still_rejected(self, ctx_internal_validation, preview_id):
        """internal_validation remains forbidden."""
        with pytest.raises(HTTPException) as exc_info:
            execute_multi_candidate_preview_v1(ctx_internal_validation, preview_id)
        assert exc_info.value.detail["code"] == "launch_mode_forbidden"


class TestConnectionScenarioConfig:
    def test_connection_with_fault_config(self, db_session, ctx):
        faults = {"faults": {"rules": [
            {"object_type": "ad", "match_ordinal": 2, "effect": "uncertain_after_apply"}
        ]}}
        conn = create_connection(db_session, context=ctx, ad_account_id="act_123", scenario_config=faults)
        scenario = db_session.scalars(
            select(ScenarioVersionRecord).where(
                ScenarioVersionRecord.meta_connection_id == conn.id
            )
        ).first()
        assert scenario.scenario_config == faults

    def test_connection_without_config_defaults_empty(self, db_session, ctx):
        conn = create_connection(db_session, context=ctx, ad_account_id="act_456")
        scenario = db_session.scalars(
            select(ScenarioVersionRecord).where(
                ScenarioVersionRecord.meta_connection_id == conn.id
            )
        ).first()
        assert scenario.scenario_config == {}
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_controlled_mock.py tests/test_launch_multi_candidate.py -v
```

- [ ] **Step 5: Commit**

```bash
git add app/launch/api.py app/meta/connections.py tests/test_controlled_mock.py
git commit -m "$(cat <<'EOF'
feat(launch): admit controlled_mock through execute endpoint (#88)

Allow both customer_sandbox and controlled_mock through the PAUSED
provisioning execute endpoint. internal_validation remains forbidden.
Add optional scenario_config parameter to create_connection for
pre-seeding fault configuration on controlled-mock connections.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 6: Backend — Saga durable parity failure + recovery parity reconstruction

**Files:**
- Modify: `app/launch/saga.py:436-444` (durable commit before ProvisioningError)
- Modify: `app/launch/recovery.py:296-374` (read-only parity reconstruction)
- Test: `tests/test_launch_saga.py`, `tests/test_launch_recovery.py`

**Interfaces:**
- Consumes: `_durable_commit` (saga.py), `_transition_provisioning_state` (saga.py), `LaunchParityCheckRecord` (existing model), `_derive_manifest_outcome` (recovery.py line 296)
- Produces:
  - Saga: durable commit of failed parity evidence before raising `ProvisioningError`
  - Recovery: read-only parity reconstruction after complete reconciled write manifest

- [ ] **Step 1: Add durable commit before `ProvisioningError` in saga**

In `app/launch/saga.py`, the parity failure block at lines 436-444 currently writes the parity record and transitions to failed, then raises without a durable commit. Add `_durable_commit` before the raise:

```python
        if not passed:
            _transition_provisioning_state(
                session, context, prov, "failed", reason="post-provisioning parity check failed",
            )
            _durable_commit(session, context)
            raise ProvisioningError(
                f"provisioning {provisioning_id} failed parity check: "
                f"all_present={all_present} budget_ok={budget_ok} "
                f"config_ok={config_ok} status_ok={status_ok}"
            )
```

The `except ProvisioningError: raise` block at line 454 re-raises without further transition, so this is safe.

- [ ] **Step 2: Add read-only parity reconstruction in recovery**

In `app/launch/recovery.py`, after `_derive_manifest_outcome` confirms all expected writes succeeded, add parity reconstruction logic. Add a new function before `_derive_manifest_outcome`:

```python
def _reconstruct_parity_after_reconciliation(
    session: Session,
    context: TenantContext,
    *,
    provisioning: LaunchProvisioningRecord,
    adapter: FakeMetaAdapter,
    candidates: list[LaunchCandidateRecord],
    remote_campaign_id: str,
) -> LaunchParityCheckRecord:
    """Read-only parity reconstruction after complete reconciled manifest.

    Recovery reads adapter state, never calls ensure_object().
    """
    campaign_state = adapter.read_object("campaign", remote_campaign_id)

    candidate_results = []
    for candidate in candidates:
        adset_id = adapter.deterministic_object_id("adset", candidate.adset_idempotency_key)
        ad_id = adapter.deterministic_object_id("ad", candidate.ad_idempotency_key)
        adset_state = adapter.read_object("adset", adset_id)
        ad_state = adapter.read_object("ad", ad_id)
        candidate_results.append({
            "candidate_id": candidate.id,
            "adset_remote_id": adset_id,
            "ad_remote_id": ad_id,
            "adset_state": adset_state,
            "ad_state": ad_state,
        })

    preview = session.get(LaunchMultiCandidatePreviewRecord, provisioning.preview_id)
    all_present = len(candidate_results) == len(candidates) == preview.candidate_count
    budget_ok = all(
        (r["adset_state"] or {}).get("daily_budget_minor") == preview.daily_budget_minor
        for r in candidate_results
    )
    config_ok = all(r["ad_state"] is not None for r in candidate_results)
    status_ok = (
        (campaign_state or {}).get("status") == "PAUSED"
        and all((r["adset_state"] or {}).get("status") == "PAUSED" for r in candidate_results)
        and all((r["ad_state"] or {}).get("status") == "PAUSED" for r in candidate_results)
    )
    passed = all_present and budget_ok and config_ok and status_ok

    parity_record = LaunchParityCheckRecord(
        id=new_id("lpc"),
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        provisioning_id=provisioning.id,
        check_type="post_provisioning",
        all_candidates_present=all_present,
        budget_parity_verified=budget_ok,
        config_parity_verified=config_ok,
        status_parity_verified=status_ok,
        detail={
            "source": "reconciliation",
            "remote_campaign_id": remote_campaign_id,
            "campaign_state": campaign_state,
            "candidates": candidate_results,
        },
        passed=passed,
        executor=context.executor,
    )
    session.add(parity_record)
    session.flush()
    return parity_record
```

Then integrate into `_derive_manifest_outcome`: when all expected writes succeeded but no existing post_provisioning parity record exists, call the reconstruction:

```python
    # In _derive_manifest_outcome, before the final return block:
    if expected and succeeded == expected and not parity_passed:
        # All writes succeeded but no parity record — reconstruct read-only
        # This happens when reconciliation confirmed all writes after uncertainty
        adapter = FakeMetaAdapter(
            session=session,
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            scenario_version_id=provisioning.scenario_version_id,
        )
        campaign_step = next(
            a for a in attempts
            if a.step_type == "create_campaign" and a.state == "applied"
        )
        remote_campaign_id = adapter.deterministic_object_id(
            "campaign", campaign_step.idempotency_key
        )
        parity = _reconstruct_parity_after_reconciliation(
            session, context,
            provisioning=provisioning,
            adapter=adapter,
            candidates=candidates,
            remote_campaign_id=remote_campaign_id,
        )
        if parity.passed:
            return "succeeded", provisioning
        return "partially_succeeded", provisioning
```

- [ ] **Step 3: Write saga durable parity test**

```python
class TestDurableParityFailure:
    def test_failed_parity_persisted_before_exception(self, ...):
        """Spec scenario 9: parity failure committed before ProvisioningError."""
        # Configure return_state_patch with budget mismatch
        # Execute provisioning
        # Verify LaunchParityCheckRecord exists with passed=False
        # Verify provisioning state is "failed"
        # Verify ProvisioningError was raised
```

- [ ] **Step 4: Write recovery parity reconstruction test**

```python
class TestRecoveryParityReconstruction:
    def test_complete_reconciled_manifest_gets_parity(self, ...):
        """Spec scenario 4: reconciliation confirms all writes -> reconstructed parity."""
        # Set up uncertain_after_apply on final ad
        # Run saga (gets ProvisioningUncertain)
        # Run recovery - read-back confirms object exists
        # Verify post_provisioning parity record written by recovery
        # Verify detail.source == "reconciliation"

    def test_incomplete_manifest_is_partially_provisioned(self, ...):
        """Missing writes -> partially_provisioned (terminal)."""
        # Set up uncertain_before_apply on an ad
        # Run saga -> uncertain
        # Run recovery - read-back confirms object missing
        # Verify outcome is partially_succeeded -> partially_provisioned
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_launch_saga.py tests/test_launch_recovery.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/launch/saga.py app/launch/recovery.py \
       tests/test_launch_saga.py tests/test_launch_recovery.py
git commit -m "$(cat <<'EOF'
feat(launch): durable parity failure + recovery parity reconstruction (#88)

Saga now durably commits failed parity evidence and terminal
provisioning state before raising ProvisioningError. Recovery adds
read-only parity reconstruction when reconciliation confirms a
complete write manifest with no existing parity record.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 7: Contract — OpenAPI regeneration + TypeScript codegen

**Files:**
- Modify: `contracts/openapi/marketai-domain-v1.json` (regenerated)
- Modify: `web/lib/domain-client/generated/` (all generated files)

**Interfaces:**
- Consumes: Updated FastAPI endpoint and model definitions (Tasks 1-3)
- Produces:
  - Updated OpenAPI spec with `analyze_experiment_duplicates` operation
  - Updated TypeScript types: `DuplicateAnalysisResponse`, `DuplicateGroup`, etc.
  - Updated `FreezeExperimentRequest` with required `analyzed_candidate_set_hash`
  - `ConceptResolutionEvidence` on `ExperimentDetail`
  - Updated allowlist, paths, validators, operation map

- [ ] **Step 1: Regenerate OpenAPI spec**

```bash
python scripts/generate_openapi.py
```

- [ ] **Step 2: Verify new operation appears in spec**

```bash
python -c "
import json
with open('contracts/openapi/marketai-domain-v1.json') as f:
    spec = json.load(f)
path = '/api/v1/experiments/{experiment_id}/duplicate-analysis'
assert path in spec['paths'], f'Missing path: {path}'
assert 'get' in spec['paths'][path], 'Missing GET method'
op = spec['paths'][path]['get']
assert op['operationId'] == 'analyze_experiment_duplicates'
print('OK: analyze_experiment_duplicates operation present')

# Check FreezeExperimentRequest has analyzed_candidate_set_hash
freeze = spec['paths']['/api/v1/experiments/{experiment_id}/freeze']['post']
body_ref = freeze['requestBody']['content']['application/json']['schema']
print(f'Freeze request schema: {body_ref}')
"
```

- [ ] **Step 3: Run TypeScript codegen pipeline**

```bash
cd web && npm run generate:all
```

This runs `scripts/generate_all.sh` which produces:
- `domain-v1.d.ts` — new types
- `operation-map.ts` — new operation
- `operation-registry.ts` — operation metadata
- `validators.ts` — response validators
- `paths.ts` — path mappings
- `allowlist.ts` — BFF allowlist
- `allowlist-binding.ts` — binding

- [ ] **Step 4: Verify generated types**

```bash
grep -n "analyze_experiment_duplicates" web/lib/domain-client/generated/operation-map.ts
grep -n "DuplicateAnalysisResponse" web/lib/domain-client/generated/domain-v1.d.ts
grep -n "analyzed_candidate_set_hash" web/lib/domain-client/generated/domain-v1.d.ts
grep -n "concept_resolution_evidence" web/lib/domain-client/generated/domain-v1.d.ts
```

Expected: All grep commands find matching lines.

- [ ] **Step 5: Run CI drift check**

```bash
cd web && npm run verify
```

Expected: No drift between generated files and spec.

- [ ] **Step 6: Commit**

```bash
git add contracts/openapi/ web/lib/domain-client/generated/
git commit -m "$(cat <<'EOF'
chore: regenerate OpenAPI spec and TypeScript client (#88)

Adds analyze_experiment_duplicates GET operation,
analyzed_candidate_set_hash on FreezeExperimentRequest,
concept_resolution_evidence on ExperimentDetail, and
DuplicateAnalysis response models.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 8: Frontend — Analysis query + resolution panel + freeze submission

**Files:**
- Create: `web/components/experiments/DuplicateResolutionPanel.tsx`
- Modify: `web/hooks/experiments/useExperimentsWorkspace.ts` (add query, update freeze)
- Modify: `web/components/experiments/ExperimentsWorkspace.tsx:32,693-699,1217-1255` (integrate panel, update submission)
- Modify: DomainGateway + useDomainQuery switch files (register route)

**Interfaces:**
- Consumes: Generated types `DuplicateAnalysisResponse`, `ConceptResolutionActionRequest`, `FreezeExperimentRequest` (Task 7)
- Produces:
  - `useAnalyzeDuplicates(experimentId)` — query hook
  - `DuplicateResolutionPanel` — resolution UI component
  - Updated freeze submission with `analyzed_candidate_set_hash` + `concept_resolution_plan`

- [ ] **Step 1: Register the analysis route in DomainGateway**

In the gateway interface file, add the new method signature:

```typescript
analyzeExperimentDuplicates(params: {
  pathParams: { experiment_id: string };
}): Promise<DomainResultFor<'analyze_experiment_duplicates'>>;
```

In the gateway implementation, add:

```typescript
async analyzeExperimentDuplicates(params: {
  pathParams: { experiment_id: string };
}): Promise<DomainResultFor<'analyze_experiment_duplicates'>> {
  const path = buildBffPath('analyze_experiment_duplicates', params.pathParams);
  return this.sendSafe(path, 'GET');
}
```

- [ ] **Step 2: Add the operation to `useDomainQuery` switch**

In the `ReadOperationId` union type, add `'analyze_experiment_duplicates'`.

In the `callReadOperation` switch, add:

```typescript
case 'analyze_experiment_duplicates':
  return gateway.analyzeExperimentDuplicates({
    pathParams: params.pathParams as { experiment_id: string },
  });
```

- [ ] **Step 3: Add `useAnalyzeDuplicates` to hooks**

In `web/hooks/experiments/useExperimentsWorkspace.ts`:

```typescript
export function useAnalyzeDuplicates(experimentId: string | null) {
  return useDomainQuery(
    'analyze_experiment_duplicates',
    experimentId
      ? { pathParams: { experiment_id: experimentId } }
      : null,
  );
}
```

- [ ] **Step 4: Update freeze command to include new fields**

In `useExperimentsWorkspace.ts`, update the freeze command's type usage. The `freeze` command already uses the generated `FreezeExperimentRequest` — the new `analyzed_candidate_set_hash` field will be available automatically from Task 7's codegen.

- [ ] **Step 5: Create `DuplicateResolutionPanel.tsx`**

```tsx
import { useState, useCallback, useMemo } from 'react';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type DuplicateGroup = components['schemas']['DuplicateGroup'];
type RemapTarget = components['schemas']['RemapTarget'];

interface GroupResolution {
  action: string;
  reason: string;
  affectedCandidateFingerprint?: string;
  remapTargetId?: string;
}

interface DuplicateResolutionPanelProps {
  groups: DuplicateGroup[];
  onResolutionsChange: (resolutions: Map<string, GroupResolution>) => void;
  stateKey: string;
}

export function DuplicateResolutionPanel({
  groups,
  onResolutionsChange,
  stateKey,
}: DuplicateResolutionPanelProps) {
  const [resolutions, setResolutions] = useState<Map<string, GroupResolution>>(
    () => new Map()
  );
  const [resetKey, setResetKey] = useState(stateKey);

  if (resetKey !== stateKey) {
    setResetKey(stateKey);
    setResolutions(new Map());
  }

  const updateGroup = useCallback(
    (groupHash: string, update: Partial<GroupResolution>) => {
      setResolutions((prev) => {
        const next = new Map(prev);
        const existing = next.get(groupHash) || { action: '', reason: '' };
        next.set(groupHash, { ...existing, ...update });
        onResolutionsChange(next);
        return next;
      });
    },
    [onResolutionsChange],
  );

  return (
    <div className="space-y-6">
      <h3 className="text-lg font-semibold">Duplicate Concept Resolution</h3>
      <p className="text-sm text-gray-600">
        The following concept groups have duplicate entries. Resolve each before
        freezing.
      </p>
      {groups.map((group) => (
        <GroupCard
          key={group.duplicate_group_hash}
          group={group}
          resolution={resolutions.get(group.duplicate_group_hash)}
          onUpdate={(update) => updateGroup(group.duplicate_group_hash, update)}
        />
      ))}
    </div>
  );
}

function GroupCard({
  group,
  resolution,
  onUpdate,
}: {
  group: DuplicateGroup;
  resolution?: GroupResolution;
  onUpdate: (update: Partial<GroupResolution>) => void;
}) {
  const needsAffectedCandidate =
    resolution?.action === 'exclude_source_candidate' ||
    resolution?.action === 'remap_before_freeze';
  const needsRemapTarget = resolution?.action === 'remap_before_freeze';

  return (
    <div className="rounded-lg border border-gray-200 p-4 space-y-3">
      <div className="font-medium">
        {group.dimension_key} — {group.concept_key_name}
      </div>
      <div className="text-sm text-gray-500">
        {group.members.length} candidates share this concept
      </div>
      <ul className="text-sm space-y-1">
        {group.members.map((m) => (
          <li key={m.source_candidate_fingerprint} className="font-mono text-xs">
            {m.display_label} ({m.asset_version_id.slice(0, 12)}…)
          </li>
        ))}
      </ul>

      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Action</legend>
        {group.allowed_actions.map((action) => (
          <label key={action} className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name={`action-${group.duplicate_group_hash}`}
              value={action}
              checked={resolution?.action === action}
              onChange={() =>
                onUpdate({
                  action,
                  affectedCandidateFingerprint: undefined,
                  remapTargetId: undefined,
                })
              }
            />
            {actionLabel(action)}
          </label>
        ))}
      </fieldset>

      {needsAffectedCandidate && (
        <div className="space-y-1">
          <label className="text-sm font-medium">Affected candidate</label>
          <select
            className="w-full rounded border px-2 py-1 text-sm"
            value={resolution?.affectedCandidateFingerprint ?? ''}
            onChange={(e) =>
              onUpdate({ affectedCandidateFingerprint: e.target.value || undefined })
            }
          >
            <option value="">Select…</option>
            {group.members.map((m) => (
              <option key={m.source_candidate_fingerprint} value={m.source_candidate_fingerprint}>
                {m.display_label}
              </option>
            ))}
          </select>
        </div>
      )}

      {needsRemapTarget && (
        <div className="space-y-1">
          <label className="text-sm font-medium">Remap to concept</label>
          <select
            className="w-full rounded border px-2 py-1 text-sm"
            value={resolution?.remapTargetId ?? ''}
            onChange={(e) => onUpdate({ remapTargetId: e.target.value || undefined })}
          >
            <option value="">Select…</option>
            {group.remap_targets.map((t: RemapTarget) => (
              <option key={t.concept_key_id} value={t.concept_key_id}>
                {t.concept_key_name}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="space-y-1">
        <label className="text-sm font-medium">Reason (required)</label>
        <textarea
          className="w-full rounded border px-2 py-1 text-sm"
          rows={2}
          maxLength={2000}
          value={resolution?.reason ?? ''}
          onChange={(e) => onUpdate({ reason: e.target.value })}
          placeholder="Explain this resolution decision…"
        />
      </div>
    </div>
  );
}

function actionLabel(action: string): string {
  switch (action) {
    case 'allow_controlled_duplicate':
      return 'Allow (controlled duplicate)';
    case 'exclude_source_candidate':
      return 'Exclude one candidate';
    case 'remap_before_freeze':
      return 'Remap to different concept';
    default:
      return action;
  }
}
```

- [ ] **Step 6: Integrate into `ExperimentsWorkspace.tsx`**

Import the new component and analysis hook. Add the analysis query trigger and resolution panel to the main column. Update the freeze submission to include the full plan.

In the component body, add:

```typescript
// After existing state declarations:
const analysis = useAnalyzeDuplicates(
  experiment?.state === 'draft' && (experiment?.entries?.length ?? 0) >= 2
    ? experiment.id
    : null,
);

const [resolutions, setResolutions] = useState<Map<string, GroupResolution>>(new Map());

const analysisStateKey = analysis.data
  ? `${experiment?.id}-${analysis.data.analyzed_revision}-${analysis.data.candidate_set_hash}`
  : '';

// Reset resolutions and acknowledgement when analysis changes
useEffect(() => {
  setResolutions(new Map());
  setFreezeAcknowledged(false);
}, [analysisStateKey]);
```

In the main column, after the candidates section, add:

```tsx
{analysis.data?.has_duplicates && (
  <DuplicateResolutionPanel
    groups={analysis.data.duplicate_groups}
    onResolutionsChange={setResolutions}
    stateKey={analysisStateKey}
  />
)}
```

Update the freeze gate (the freeze button's disabled condition):

```typescript
const allGroupsResolved = !analysis.data?.has_duplicates || (
  analysis.data.duplicate_groups.every((g) => {
    const r = resolutions.get(g.duplicate_group_hash);
    if (!r?.action || !r?.reason?.trim()) return false;
    if (
      (r.action === 'exclude_source_candidate' || r.action === 'remap_before_freeze') &&
      !r.affectedCandidateFingerprint
    ) return false;
    if (r.action === 'remap_before_freeze' && !r.remapTargetId) return false;
    return true;
  })
);

const freezeDisabled =
  !freezeAcknowledged ||
  entries.length < 2 ||
  workspace.commands.freeze.isPending ||
  !analysis.data ||
  analysis.data.analyzed_revision !== experiment.revision ||
  !allGroupsResolved;
```

Update the freeze submission (lines 693-699):

```typescript
const freeze = () => {
  if (!analysis.data) return;
  const plan = analysis.data.has_duplicates
    ? analysis.data.duplicate_groups.map((g) => {
        const r = resolutions.get(g.duplicate_group_hash)!;
        return {
          duplicate_group_hash: g.duplicate_group_hash,
          action: r.action as 'allow_controlled_duplicate' | 'exclude_source_candidate' | 'remap_before_freeze',
          reason: r.reason,
          source_candidate_fingerprints: g.members
            .map((m) => m.source_candidate_fingerprint)
            .sort(),
          source_candidate_fingerprint: r.affectedCandidateFingerprint,
          remap_concept_key_id: r.remapTargetId,
        };
      })
    : [];

  run(() =>
    workspace.commands.freeze.execute({
      actionInstanceId: newActionId(),
      pathParams: { experiment_id: experiment.id },
      body: {
        expected_revision: experiment.revision,
        analyzed_candidate_set_hash: analysis.data!.candidate_set_hash,
        concept_resolution_plan: plan,
      },
    })
  );
};
```

- [ ] **Step 7: Add error handling for stale responses**

In the freeze error handler, detect `concept_resolution_stale` and `revision_conflict`:

```typescript
// In the run() error callback or catch:
const handleFreezeError = (error: ProblemDetails) => {
  if (error.code === 'revision_conflict' || error.code === 'concept_resolution_stale') {
    workspace.detail.refetch();
    analysis.refetch();
    setResolutions(new Map());
    setFreezeAcknowledged(false);
    // Show change notice to user
  }
};
```

- [ ] **Step 8: Verify TypeScript compiles**

```bash
cd web && npx tsc --noEmit
```

- [ ] **Step 9: Commit**

```bash
git add web/components/experiments/DuplicateResolutionPanel.tsx \
       web/components/experiments/ExperimentsWorkspace.tsx \
       web/hooks/experiments/useExperimentsWorkspace.ts \
       web/lib/domain-client/
git commit -m "$(cat <<'EOF'
feat(experiments-ui): add duplicate resolution panel + freeze submission (#88)

Wire GET /duplicate-analysis through useDomainQuery. Add
DuplicateResolutionPanel in the main column. Freeze submission now
sends analyzed_candidate_set_hash and concept_resolution_plan.
Error handling resets state on revision_conflict and
concept_resolution_stale.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 9: Frontend — Evidence display + mode selector

**Files:**
- Create: `web/components/experiments/FrozenResolutionEvidence.tsx`
- Modify: `web/components/experiments/ExperimentsWorkspace.tsx:32` (mode selector)
- Modify: `web/hooks/experiments/useExperimentsWorkspace.ts:56-74` (mode passthrough)

**Interfaces:**
- Consumes: `ExperimentDetail.concept_resolution_evidence` (Task 7 generated type), `useMetaLaunchPreview`, `useMetaLaunchProvisioning`
- Produces:
  - `FrozenResolutionEvidence` component
  - Meta mode selector replacing hard-coded `META_MODE` constant

- [ ] **Step 1: Create `FrozenResolutionEvidence.tsx`**

```tsx
import type { components } from '@/lib/domain-client/generated/domain-v1';

type ConceptResolutionEvidence = components['schemas']['ConceptResolutionEvidence'];
type ResolutionEvidenceRow = components['schemas']['ResolutionEvidenceRow'];

interface FrozenResolutionEvidenceProps {
  evidence: ConceptResolutionEvidence;
}

export function FrozenResolutionEvidence({ evidence }: FrozenResolutionEvidenceProps) {
  if (!evidence.had_duplicates) {
    return (
      <section className="space-y-2">
        <h3 className="text-lg font-semibold">Concept Resolution</h3>
        <p className="text-sm text-gray-600">
          No duplicate concepts detected at freeze time.
        </p>
      </section>
    );
  }

  return (
    <section className="space-y-4">
      <h3 className="text-lg font-semibold">Concept Resolution</h3>
      {evidence.resolutions.map((row) => (
        <EvidenceCard key={row.duplicate_group_hash} row={row} />
      ))}
    </section>
  );
}

function EvidenceCard({ row }: { row: ResolutionEvidenceRow }) {
  return (
    <div className="rounded-lg border border-gray-200 p-4 space-y-2">
      <div className="font-medium">
        {row.dimension_key} — {row.concept_key_name}
      </div>
      <div className="flex flex-wrap gap-4 text-sm">
        <span className="rounded bg-blue-50 px-2 py-0.5 text-blue-700">
          {actionDisplayLabel(row.action)}
        </span>
        <span className="text-gray-500">
          {row.actor_role} · {new Date(row.resolved_at).toLocaleDateString()}
        </span>
      </div>
      <p className="text-sm text-gray-700">{row.reason}</p>
      {row.action === 'exclude_source_candidate' && row.affected_candidate_fingerprint && (
        <p className="text-xs text-gray-500 font-mono">
          Excluded: {row.affected_candidate_fingerprint.slice(0, 16)}…
        </p>
      )}
      {row.action === 'remap_before_freeze' && (
        <p className="text-xs text-gray-500">
          Remapped to: {row.remap_destination_concept_key_name}
        </p>
      )}
    </div>
  );
}

function actionDisplayLabel(action: string): string {
  switch (action) {
    case 'allow_controlled_duplicate': return 'Allowed duplicate';
    case 'exclude_source_candidate': return 'Excluded candidate';
    case 'remap_before_freeze': return 'Remapped concept';
    default: return action;
  }
}
```

- [ ] **Step 2: Integrate evidence display in `ExperimentsWorkspace.tsx`**

In the detail view, after the candidates section and after any existing frozen sections, add:

```tsx
{experiment.concept_resolution_evidence && (
  <FrozenResolutionEvidence evidence={experiment.concept_resolution_evidence} />
)}
```

Import at the top:

```typescript
import { FrozenResolutionEvidence } from './FrozenResolutionEvidence';
```

- [ ] **Step 3: Replace `META_MODE` constant with a selector**

Replace line 32:

```typescript
// Remove: const META_MODE = 'customer_sandbox' as const;

// Add state:
const [metaMode, setMetaMode] = useState<'customer_sandbox' | 'controlled_mock'>('customer_sandbox');
```

Add the selector UI in the sidebar, above the Launch section:

```tsx
<div className="space-y-1">
  <label className="text-sm font-medium">Evidence mode</label>
  <select
    className="w-full rounded border px-2 py-1 text-sm"
    value={metaMode}
    onChange={(e) => setMetaMode(e.target.value as 'customer_sandbox' | 'controlled_mock')}
  >
    <option value="customer_sandbox">Customer Sandbox</option>
    <option value="controlled_mock">Controlled Mock (decision-ineligible)</option>
  </select>
</div>
```

Replace all references to `META_MODE` with `metaMode` in the component.

- [ ] **Step 4: Pass selected mode through preview/provisioning hooks**

Update `useMetaLaunchPreview` and `useMetaLaunchProvisioning` to accept mode:

```typescript
export function useMetaLaunchPreview(previewId: string | null, metaMode: string) {
  return useDomainQuery(
    'get_meta_launch_preview',
    previewId
      ? { pathParams: { preview_id: previewId }, queryParams: { meta_mode: metaMode } }
      : null,
  );
}

export function useMetaLaunchProvisioning(provisioningId: string | null, metaMode: string) {
  return useDomainQuery(
    'get_meta_launch_provisioning',
    provisioningId
      ? { pathParams: { provisioning_id: provisioningId }, queryParams: { meta_mode: metaMode } }
      : null,
  );
}
```

Update call sites in `ExperimentsWorkspace.tsx` to pass `metaMode`.

- [ ] **Step 5: Verify TypeScript compiles**

```bash
cd web && npx tsc --noEmit
```

- [ ] **Step 6: Commit**

```bash
git add web/components/experiments/FrozenResolutionEvidence.tsx \
       web/components/experiments/ExperimentsWorkspace.tsx \
       web/hooks/experiments/useExperimentsWorkspace.ts
git commit -m "$(cat <<'EOF'
feat(experiments-ui): add frozen evidence display + mode selector (#88)

FrozenResolutionEvidence renders concept resolution evidence in the
detail view. Meta mode selector replaces the hard-coded customer_sandbox
constant, allowing controlled_mock Launch for fault-injection E2E.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 10: E2E — Infrastructure (fixtures, helpers, seed, config)

**Files:**
- Create: `web/e2e/experiments/helpers.ts`
- Create: `web/e2e/experiments/seed_prerequisites.py`
- Create: `web/e2e/experiments/worker_checkpoint.py`
- Modify: `web/e2e/experiments/fixtures.ts` (replace route mocks with real-backend fixtures)
- Delete: `web/e2e/experiments/golden-path.spec.ts`

**Interfaces:**
- Consumes: Localization e2e pattern (`web/e2e/localization/`), `app.platform.jobs.claim_next` (for worker checkpoint), database models
- Produces:
  - `seedExperimentPrerequisites()` — Python script for PostgreSQL fixture setup
  - `createAuthenticatedContext()` — Playwright fixture helper
  - `inspectFakeMetaObjects()`, `inspectCallIntents()` — database inspection helpers
  - `restartApiServer()`, `restartWorker()` — process restart helpers
  - `WORKSPACE_SLUG`, `WORKSPACE_ID`, unique test identity constants

- [ ] **Step 1: Create `seed_prerequisites.py`**

Follow the localization pattern (`web/e2e/localization/seed_prerequisites.py`):

```python
"""Seed PostgreSQL prerequisites for experiments E2E.

Run once before the Playwright suite. Creates:
- Workspace with unique test identity
- User membership
- Product with asset versions sharing concept keys (for duplicate groups)
- Meta connections for customer_sandbox and controlled_mock modes
- controlled_mock connection with optional fault configuration
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.adapters.persistence.models import new_id, utcnow
from app.platform.tenancy import TenantContext, tenant_session


def seed(*, fault_config: dict | None = None) -> dict:
    """Seed and return identity constants for the test suite."""
    workspace_id = new_id("ws_e2e_exp")
    user_id = new_id("user_e2e_exp")
    ctx = TenantContext(
        workspace_id=workspace_id,
        role="owner",
        initiator_id=user_id,
        initiator_role="owner",
        executor=f"user:{user_id}",
        request_id="seed",
        trace_id="seed",
    )

    with tenant_session(ctx) as session:
        # Create workspace, product, asset versions with concept keys,
        # customer_sandbox connection, controlled_mock connection with faults
        # ... (follows the localization seed pattern)
        pass

    return {
        "workspace_id": workspace_id,
        "workspace_slug": f"e2e-exp-{workspace_id[:8]}",
        "user_id": user_id,
        # ... other identity constants
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fault-config", type=str, default=None)
    args = parser.parse_args()
    fc = json.loads(args.fault_config) if args.fault_config else None
    result = seed(fault_config=fc)
    print(json.dumps(result))
```

- [ ] **Step 2: Create `helpers.ts`**

```typescript
import { type Page, expect } from '@playwright/test';

export const API_BASE = process.env.API_BASE_URL ?? 'http://localhost:8000';

export async function inspectFakeMetaObjects(
  workspaceId: string,
  metaMode: string,
  scenarioVersionId: string,
): Promise<Array<{ object_type: string; remote_object_id: string; state: Record<string, unknown> }>> {
  const res = await fetch(
    `${API_BASE}/api/internal/fake-meta-objects?` +
      new URLSearchParams({ workspace_id: workspaceId, meta_mode: metaMode, scenario_version_id: scenarioVersionId }),
  );
  return res.json();
}

export async function inspectCallIntents(
  operationId: string,
): Promise<Array<{ id: string; meta_api_method: string; network_dispatched_at: string | null }>> {
  const res = await fetch(
    `${API_BASE}/api/internal/call-intents?operation_id=${operationId}`,
  );
  return res.json();
}

export async function restartApiServer(): Promise<void> {
  const { execSync } = await import('child_process');
  execSync('docker compose restart api', { stdio: 'pipe', timeout: 30_000 });
  // Wait for health check
  for (let i = 0; i < 30; i++) {
    try {
      const res = await fetch(`${API_BASE}/healthz`);
      if (res.ok) return;
    } catch { /* retry */ }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error('API server did not restart in time');
}

export async function restartWorker(): Promise<void> {
  const { execSync } = await import('child_process');
  execSync('docker compose restart worker', { stdio: 'pipe', timeout: 30_000 });
}

export async function stopWorker(): Promise<void> {
  const { execSync } = await import('child_process');
  execSync('docker compose stop worker', { stdio: 'pipe', timeout: 15_000 });
}

export async function startWorker(): Promise<void> {
  const { execSync } = await import('child_process');
  execSync('docker compose start worker', { stdio: 'pipe', timeout: 15_000 });
}

export async function waitForOperationState(
  page: Page,
  experimentId: string,
  expectedState: string,
  timeoutMs = 30_000,
): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    // Check Operation state via API or UI polling
    await page.waitForTimeout(1000);
    // ... implementation depends on how Operation state is surfaced in UI
  }
}
```

- [ ] **Step 3: Create `worker_checkpoint.py`**

```python
"""Guarded one-shot claimant for Scenario 5b.

Leases one exact expected Job through jobs.claim_next() and exits
before mark_running(). Exercises the real queue API without mutating
the Job row beyond the lease.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.platform.jobs import claim_next


def checkpoint(*, expected_job_id: str, lease_seconds: int = 5) -> dict:
    """Claim the next job and verify it matches expectations."""
    engine = create_engine(
        "postgresql://marketai:marketai@localhost:5432/marketai_test"
    )
    with Session(engine) as session:
        claimed = claim_next(session, owner="e2e_checkpoint", lease_seconds=lease_seconds)
        if claimed is None:
            return {"error": "no job available"}
        if claimed.job_id != expected_job_id:
            return {"error": f"claimed wrong job: {claimed.job_id} != {expected_job_id}"}
        session.commit()
        return {
            "job_id": claimed.job_id,
            "state": "leased",
            "lease_seconds": lease_seconds,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-job-id", required=True)
    parser.add_argument("--lease-seconds", type=int, default=5)
    args = parser.parse_args()
    import json
    result = checkpoint(expected_job_id=args.expected_job_id, lease_seconds=args.lease_seconds)
    print(json.dumps(result))
    sys.exit(1 if "error" in result else 0)
```

- [ ] **Step 4: Replace `fixtures.ts` with real-backend fixtures**

Delete the route-mock content and replace with:

```typescript
import { type Page, test as base } from '@playwright/test';
import { execSync } from 'child_process';

export const WORKSPACE_SLUG = process.env.E2E_WORKSPACE_SLUG!;
export const WORKSPACE_ID = process.env.E2E_WORKSPACE_ID!;
export const USER_ID = process.env.E2E_USER_ID!;

interface ExperimentFixtures {
  authenticatedPage: Page;
}

export const test = base.extend<ExperimentFixtures>({
  authenticatedPage: async ({ page }, use) => {
    // Set auth cookies/headers for the E2E user
    // Navigate to workspace experiments page
    await page.goto(`/w/${WORKSPACE_SLUG}/experiments`);
    await use(page);
  },
});

export function seedPrerequisites(faultConfig?: Record<string, unknown>): Record<string, string> {
  const args = faultConfig ? `--fault-config '${JSON.stringify(faultConfig)}'` : '';
  const output = execSync(
    `python web/e2e/experiments/seed_prerequisites.py ${args}`,
    { encoding: 'utf-8' },
  );
  return JSON.parse(output.trim());
}
```

- [ ] **Step 5: Delete `golden-path.spec.ts`**

```bash
rm web/e2e/experiments/golden-path.spec.ts
```

- [ ] **Step 6: Commit**

```bash
git add web/e2e/experiments/
git commit -m "$(cat <<'EOF'
feat(e2e): replace route-mocked experiments fixtures with real-backend infrastructure (#88)

Delete route-mocked golden-path.spec.ts and replace fixtures.ts with
real PostgreSQL/FakeMetaAdapter fixtures. Add seed_prerequisites.py,
helpers.ts (inspection/restart), and worker_checkpoint.py for the
leased-before-running crash checkpoint.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 11: E2E — Duplicate resolution scenarios (1, 2, 8)

**Files:**
- Create: `web/e2e/experiments/duplicate-resolution.spec.ts`

**Interfaces:**
- Consumes: `fixtures.ts` (Task 10), `helpers.ts` (Task 10), `seed_prerequisites.py` (Task 10)
- Produces: Playwright tests for Scenarios 1 (golden path), 2a (stale revision), 8 (no-duplicate freeze)

- [ ] **Step 1: Create `duplicate-resolution.spec.ts` skeleton**

```typescript
import { expect } from '@playwright/test';
import { test, WORKSPACE_SLUG, seedPrerequisites } from './fixtures';
import { inspectFakeMetaObjects, inspectCallIntents } from './helpers';

test.describe('Experiments duplicate resolution', () => {
  test.describe.configure({ mode: 'serial' });

  test('Scenario 1: golden path — resolve → freeze → Launch → judgement → Winner', async ({
    authenticatedPage: page,
  }) => {
    // 1. Seed experiment with entries sharing concept keys
    // 2. Navigate to experiment
    // 3. Open "Review & freeze" — verify analysis returns groups
    // 4. Resolve all groups (exercise each action type)
    // 5. Acknowledge and freeze
    // 6. Verify frozen evidence visible
    // 7. Preview Launch (customer_sandbox)
    // 8. Confirm Launch
    // 9. Wait for provisioning — verify PAUSED objects
    // 10. Bind candidates
    // 11. Start experiment
    // 12. Seed metrics, run judgement
    // 13. Verify proposal is NOT confirmed Winner
    // 14. Acknowledge and confirm Winner
    // 15. Verify ExperimentDecisionRecord (not on AssetVersion)
    // 16. Browser refresh at durable milestones
  });

  test('Scenario 2a: stale revision detection', async ({
    authenticatedPage: page,
  }) => {
    // 1. Create experiment with duplicates
    // 2. Open analysis
    // 3. Mutate entry server-side (increment revision)
    // 4. Submit freeze with stale revision → revision_conflict
    // 5. Verify UI resets: re-fetches, clears state, shows notice
  });

  test('Scenario 8: no-duplicate freeze', async ({
    authenticatedPage: page,
  }) => {
    // 1. Create experiment with distinct concept keys
    // 2. Analysis returns has_duplicates: false
    // 3. Freeze with empty plan + valid hash
    // 4. Verify had_duplicates: false evidence
  });
});
```

- [ ] **Step 2: Implement Scenario 1 test body**

The test exercises the full lifecycle through the real UI and backend. Each step uses Playwright locators to interact with the UI and assertions to verify state. The test should be broken into clearly labeled sections matching the spec's Scenario 1 steps.

Key implementation details:
- Use `page.getByRole`, `page.getByText`, `page.locator` for UI interaction
- After freeze: refresh page and verify evidence section persists
- After provisioning: inspect `FakeMetaObjectRecord` via helpers, verify PAUSED
- After judgement: verify proposal is NOT a confirmed Winner
- After confirmation: verify `ExperimentDecisionRecord` visible

- [ ] **Step 3: Implement Scenario 2a test body**

- [ ] **Step 4: Implement Scenario 8 test body**

- [ ] **Step 5: Run the test suite**

```bash
cd web && npx playwright test e2e/experiments/duplicate-resolution.spec.ts
```

- [ ] **Step 6: Commit**

```bash
git add web/e2e/experiments/duplicate-resolution.spec.ts
git commit -m "$(cat <<'EOF'
test(e2e): add duplicate resolution scenarios 1, 2a, 8 (#88)

Scenario 1 (golden path): resolve → freeze → PAUSED Launch → judgement
→ human Winner with browser refresh at durable milestones.
Scenario 2a: stale revision → revision_conflict → UI reset.
Scenario 8: no-duplicate freeze with explicit empty evidence.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 12: E2E — Launch recovery scenarios (4, 5, 9)

**Files:**
- Create: `web/e2e/experiments/launch-recovery.spec.ts`

**Interfaces:**
- Consumes: `fixtures.ts` (Task 10), `helpers.ts` (Task 10), `worker_checkpoint.py` (Task 10)
- Produces: Playwright tests for Scenarios 4 (uncertain dispatch → reconciliation), 5a-c (restart recovery), 9 (parity failure)

- [ ] **Step 1: Create `launch-recovery.spec.ts` skeleton**

```typescript
import { expect } from '@playwright/test';
import { test, seedPrerequisites } from './fixtures';
import {
  inspectFakeMetaObjects,
  inspectCallIntents,
  restartApiServer,
  restartWorker,
  stopWorker,
  startWorker,
} from './helpers';

test.describe('Launch recovery', () => {
  test.describe.configure({ mode: 'serial', workers: 1 });

  test('Scenario 4: uncertain dispatch → reconciliation → no duplicates', async ({
    authenticatedPage: page,
  }) => {
    // controlled_mock with uncertain_after_apply on final ad
    // Verify Operation uncertain
    // Reconciliation read-back → succeeded or failed
    // No duplicate objects
  });

  test('Scenario 5a: API restart — durable reads recover', async ({
    authenticatedPage: page,
  }) => {
    // pause_before_apply, restart API mid-pause
    // UI recovers after API restart
    // Worker completes independently
  });

  test('Scenario 5b: Worker restart after lease before dispatch', async ({
    authenticatedPage: page,
  }) => {
    // Stop worker, confirm Launch, run checkpoint helper
    // Wait for lease expiry, start worker
    // Verify single execution, no duplicates
  });

  test('Scenario 5c: Worker restart after dispatch → uncertain → reconciliation', async ({
    authenticatedPage: page,
  }) => {
    // pause_before_apply on create_campaign
    // Poll until dispatch marker exists
    // Terminate and restart Worker
    // Operation → uncertain, reconciliation → failed
  });

  test('Scenario 9: definitive parity failure', async ({
    authenticatedPage: page,
  }) => {
    // return_state_patch with budget mismatch on AdSet
    // Saga completes writes, parity check fails
    // Operation → failed, Job → failed (not retryable)
    // Verify specific parity evidence visible
  });
});
```

- [ ] **Step 2: Implement each scenario body**

Each scenario uses `controlled_mock` mode with specific fault configurations seeded through `seedPrerequisites`. The tests verify:
- Object state via `inspectFakeMetaObjects` (database, not network)
- Intent state via `inspectCallIntents` (dispatch markers)
- Operation/Job terminal states via UI assertions
- No duplicate objects after recovery

- [ ] **Step 3: Run the test suite**

```bash
cd web && npx playwright test e2e/experiments/launch-recovery.spec.ts --workers=1
```

- [ ] **Step 4: Commit**

```bash
git add web/e2e/experiments/launch-recovery.spec.ts
git commit -m "$(cat <<'EOF'
test(e2e): add launch recovery scenarios 4, 5a-c, 9 (#88)

Scenario 4: uncertain_after_apply → reconciliation without duplicates.
Scenario 5a: API restart, Worker independent.
Scenario 5b: lease expiry → safe requeue → single execution.
Scenario 5c: post-dispatch → uncertain → reconciliation → failed.
Scenario 9: return_state_patch budget mismatch → parity failure terminal.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 13: E2E — Judgement, authorization, mode contamination, closed-state evidence

**Files:**
- Create: `web/e2e/experiments/judgement-winner.spec.ts`
- Create: `web/e2e/experiments/authorization-mode.spec.ts`
- Create: `web/e2e/experiments/closed-state-evidence.spec.ts`

**Interfaces:**
- Consumes: `fixtures.ts` (Task 10), `helpers.ts` (Task 10)
- Produces: Playwright tests for Scenarios 3, 6, 7, and §3.5

- [ ] **Step 1: Create `judgement-winner.spec.ts`**

```typescript
import { expect } from '@playwright/test';
import { test } from './fixtures';

test.describe('Judgement and Winner', () => {
  test('Scenario 3: inconclusive judgement', async ({ authenticatedPage: page }) => {
    // Freeze and launch through golden path
    // Seed insufficient metric data
    // Run judgement → inconclusive
    // Verify no ExperimentDecisionRecord
    // Refresh → inconclusive state persists
  });
});
```

- [ ] **Step 2: Create `authorization-mode.spec.ts`**

```typescript
import { expect } from '@playwright/test';
import { test } from './fixtures';

test.describe('Authorization and mode contamination', () => {
  test('Scenario 6: Member and Owner can complete lifecycle', async ({ authenticatedPage: page }) => {
    // Both roles complete full lifecycle in own workspace
    // Cross-workspace access → 404, no leakage
  });

  test('Scenario 7: mode contamination prevention', async ({ authenticatedPage: page }) => {
    // customer_sandbox tagged correctly
    // controlled_mock tagged correctly
    // Cross-mode boundary → rejected
    // Mode badge visible and accurate
    // controlled_mock is decision-ineligible
  });
});
```

- [ ] **Step 3: Create `closed-state-evidence.spec.ts`**

```typescript
import { expect } from '@playwright/test';
import { test } from './fixtures';

test.describe('Closed-state evidence display', () => {
  test('§3.5: evidence produced by real freeze, only closed state manufactured', async ({
    authenticatedPage: page,
  }) => {
    // 1. Create and freeze experiment through real service (with resolution)
    // 2. Directly set experiment state to "closed" via fixture
    // 3. Navigate to experiment
    // 4. Verify evidence section renders identically to frozen state
    // 5. Compare evidence hash, resolutions, display
  });
});
```

- [ ] **Step 4: Run all E2E suites**

```bash
cd web && npx playwright test e2e/experiments/
```

- [ ] **Step 5: Commit**

```bash
git add web/e2e/experiments/judgement-winner.spec.ts \
       web/e2e/experiments/authorization-mode.spec.ts \
       web/e2e/experiments/closed-state-evidence.spec.ts
git commit -m "$(cat <<'EOF'
test(e2e): add judgement, authorization, mode, and closed-state scenarios (#88)

Scenario 3: inconclusive judgement creates no decision records.
Scenario 6: Member/Owner lifecycle + cross-workspace rejection.
Scenario 7: mode contamination prevention + badge accuracy.
§3.5: evidence from real freeze, only closed state manufactured.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```

---

### Task 14: Verify + BFF allowlist + PR

**Files:**
- Modify: BFF allowlist (auto-generated in Task 7)
- Verify: All test suites

**Interfaces:**
- Consumes: All preceding tasks
- Produces: Green `make verify`, passing E2E suite, PR referencing issue #88

- [ ] **Step 1: Run full verification**

```bash
make verify
```

- [ ] **Step 2: Run web verification**

```bash
make web-verify
```

- [ ] **Step 3: Run E2E experiments suite**

```bash
cd web && npx playwright test e2e/experiments/
```

- [ ] **Step 4: Fix any issues found**

- [ ] **Step 5: Open PR**

```bash
gh pr create --title "feat: complete experiments duplicate resolution and persisted acceptance (#88)" --body "$(cat <<'EOF'
## Summary

- Add read-only `GET /duplicate-analysis` endpoint with shared candidate-loading/detection seam
- Extend freeze with `analyzed_candidate_set_hash` for stale detection and refined error codes
- Add frozen resolution evidence on ExperimentDetail with three-way runtime invariant
- FakeMetaAdapter consumes `_fault_config` with four deterministic effects
- Admit `controlled_mock` through Launch execute endpoint
- Saga durably persists parity failure before raising; recovery reconstructs parity read-only
- Frontend: resolution panel, freeze submission with full plan, evidence display, mode selector
- E2E: replace route-mocked tests with real PostgreSQL/FakeMetaAdapter browser journeys

## Test plan

- [ ] `make verify` passes
- [ ] `make web-verify` passes
- [ ] `npx playwright test e2e/experiments/` — all scenarios pass
- [ ] Scenario 1: golden path duplicate resolution → freeze → Launch → judgement → Winner
- [ ] Scenario 2a: stale revision detected and UI resets
- [ ] Scenario 3: inconclusive judgement produces no decision records
- [ ] Scenario 4: uncertain dispatch → reconciliation → no duplicate objects
- [ ] Scenario 5a-c: restart recovery at three distinct checkpoints
- [ ] Scenario 8: no-duplicate freeze with explicit empty evidence
- [ ] Scenario 9: definitive parity failure terminal

Closes #88

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01LvG4B6de2Tr6gvQwezmKJL
EOF
)"
```
