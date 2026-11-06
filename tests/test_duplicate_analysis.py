"""Tests for the duplicate-analysis seam and endpoint (spec §1.1-1.3)."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    DimensionConceptKeyRecord,
    ExperimentVersionRecord,
    ExperimentVersionResolutionRecord,
    GenerationRunRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    PlanRecord,
    ProductRecord,
    QualityReviewRecord,
    WorkspaceRecord,
)
from app.generation.models import AssetQCAttemptRecord, RunItemRecord
from app.experiments.service import (
    ExperimentConflict,
    ExperimentError,
    ExperimentNotFound,
    FrozenCandidateInput,
    FrozenConceptInput,
    _compute_remap_targets,
    _detect_duplicate_groups,
    _duplicate_group_hash,
    _hash_json,
    add_entry_v2,
    analyze_duplicates,
    bind_candidate_v2,
    close_experiment_v2,
    create_experiment_v2,
    freeze_experiment_v2,
    get_experiment,
    start_experiment_v2,
)
from app.platform.canonical import _canonical_json, _sha256
from app.platform.tenancy import TenantContext


# ---------------------------------------------------------------------------
# Pure, in-memory fixtures for _detect_duplicate_groups (no DB required: the
# detector only reads .id / .content_sha256 off `entry`/`asset_version`).
# ---------------------------------------------------------------------------

def _concept(dimension_key: str, concept_key_id: str, concept_key_name: str = "") -> FrozenConceptInput:
    # value_json/value_hash are concept *identity* (fed into the fingerprint);
    # concept_key_name is presentation-only and must NOT affect value_hash.
    name = concept_key_name or f"Concept {concept_key_id}"
    value_json = {"identity": concept_key_id}
    return FrozenConceptInput(
        dimension_key=dimension_key,
        stable_dimension_signature=f"{dimension_key}:v1",
        concept_key_id=concept_key_id,
        concept_key_scope="workspace",
        concept_key_name=name,
        value_json=value_json,
        value_hash=_hash_json(value_json),
        evidence_json={},
        duplicate_group_hash=_duplicate_group_hash(dimension_key, concept_key_id),
    )


def _fake_source(entry_id: str, av_id: str, index: int, concepts: list[FrozenConceptInput]) -> FrozenCandidateInput:
    return FrozenCandidateInput(
        entry=SimpleNamespace(id=entry_id),
        asset_version=SimpleNamespace(id=av_id, content_sha256=f"{index:0>64x}"),
        source_kind="generation",
        source_run_item_id=f"ritem_{index}",
        source_evidence={},
        marketing_vars_hash=f"mvhash_{index}",
        execution_mode="mock",
        concepts=tuple(concepts),
    )


@pytest.fixture
def sources_no_dups() -> list[FrozenCandidateInput]:
    return [
        _fake_source("e1", "av1", 1, [_concept("creative_angle", "k1")]),
        _fake_source("e2", "av2", 2, [_concept("creative_angle", "k2")]),
    ]


@pytest.fixture
def sources_with_dup_pair() -> list[FrozenCandidateInput]:
    return [
        _fake_source("e1", "av1", 1, [_concept("creative_angle", "k1")]),
        _fake_source("e2", "av2", 2, [_concept("creative_angle", "k1")]),
    ]


@pytest.fixture
def sources_with_triple() -> list[FrozenCandidateInput]:
    return [
        _fake_source("e1", "av1", 1, [_concept("creative_angle", "k1")]),
        _fake_source("e2", "av2", 2, [_concept("creative_angle", "k1")]),
        _fake_source("e3", "av3", 3, [_concept("creative_angle", "k1")]),
    ]


@pytest.fixture
def sources_multi_group() -> list[FrozenCandidateInput]:
    return [
        _fake_source("e1", "av1", 1, [_concept("z_dimension", "k1")]),
        _fake_source("e2", "av2", 2, [_concept("z_dimension", "k1")]),
        _fake_source("e3", "av3", 3, [_concept("a_dimension", "k2")]),
        _fake_source("e4", "av4", 4, [_concept("a_dimension", "k2")]),
    ]


class TestDetectDuplicateGroups:
    def test_no_duplicates_returns_empty(self, sources_no_dups) -> None:
        hash_, has_dups, groups = _detect_duplicate_groups(sources_no_dups)
        assert not has_dups
        assert groups == []
        assert len(hash_) == 64

    def test_duplicate_pair_detected(self, sources_with_dup_pair) -> None:
        hash_, has_dups, groups = _detect_duplicate_groups(sources_with_dup_pair)
        assert has_dups
        assert len(groups) == 1
        assert len(groups[0]["members"]) == 2
        assert "allow_controlled_duplicate" in groups[0]["allowed_actions"]
        assert "exclude_source_candidate" in groups[0]["allowed_actions"]

    def test_triple_group_only_allows_allow(self, sources_with_triple) -> None:
        _, _, groups = _detect_duplicate_groups(sources_with_triple)
        assert len(groups) == 1
        assert groups[0]["allowed_actions"] == ["allow_controlled_duplicate"]

    def test_deterministic_ordering_and_hashes(self, sources_with_dup_pair) -> None:
        h1, _, g1 = _detect_duplicate_groups(sources_with_dup_pair)
        h2, _, g2 = _detect_duplicate_groups(sources_with_dup_pair)
        assert h1 == h2
        assert g1[0]["duplicate_group_hash"] == g2[0]["duplicate_group_hash"]
        assert [m["source_candidate_fingerprint"] for m in g1[0]["members"]] == \
               [m["source_candidate_fingerprint"] for m in g2[0]["members"]]

    def test_groups_sorted_by_dimension_key_then_concept_key(self, sources_multi_group) -> None:
        _, _, groups = _detect_duplicate_groups(sources_multi_group)
        keys = [(g["dimension_key"], g["concept_key_id"]) for g in groups]
        assert keys == sorted(keys)

    def test_presentation_fields_excluded_from_group_identity(self, sources_with_dup_pair) -> None:
        """Renaming a concept key must not change candidate_set_hash or group hash."""
        h1, _, g1 = _detect_duplicate_groups(sources_with_dup_pair)
        renamed = [
            FrozenCandidateInput(
                entry=s.entry,
                asset_version=s.asset_version,
                source_kind=s.source_kind,
                source_run_item_id=s.source_run_item_id,
                source_evidence=s.source_evidence,
                marketing_vars_hash=s.marketing_vars_hash,
                execution_mode=s.execution_mode,
                concepts=tuple(
                    _concept(c.dimension_key, c.concept_key_id, concept_key_name="Renamed")
                    for c in s.concepts
                ),
            )
            for s in sources_with_dup_pair
        ]
        h2, _, g2 = _detect_duplicate_groups(renamed)
        assert h1 == h2
        assert g1[0]["duplicate_group_hash"] == g2[0]["duplicate_group_hash"]


# ---------------------------------------------------------------------------
# DB-backed fixtures for analyze_duplicates / freeze_experiment_v2 parity,
# cross-workspace rejection, and remap-target computation.
# ---------------------------------------------------------------------------

CTX = TenantContext(
    workspace_id="ws_dupan",
    role="owner",
    initiator_id="user_dupan",
    request_id="req_dupan",
    trace_id="trace_dupan",
)
OTHER_CTX = TenantContext(
    workspace_id="ws_dupan_other",
    role="owner",
    initiator_id="user_dupan_other",
    request_id="req_dupan_other",
    trace_id="trace_dupan_other",
)
PROD_ID = "prod_dupan"


def _draft_context(**overrides: object) -> dict:
    base = {
        "market": "US",
        "currency": "USD",
        "placement": "feed",
        "product_version_id": "pv_dupan",
        "offer_version_id": "ov_dupan",
        "budget": {"daily_amount": "100", "total_amount": "3000"},
        "schedule": {
            "start_at": "2026-09-01T00:00:00+00:00",
            "end_at": "2026-12-01T00:00:00+00:00",
            "timezone": "UTC",
        },
        "attribution": {"click_window_days": 7, "view_window_days": 1},
        "statistical_plan": {
            "primary_metric": "ctr",
            "ctr_threshold": "0.01",
            "alpha": "0.05",
            "bootstrap_samples": 2000,
        },
    }
    base.update(overrides)
    return base


def _ensure_workspace(session, workspace_id: str) -> None:
    if session.get(WorkspaceRecord, workspace_id) is None:
        session.add(WorkspaceRecord(id=workspace_id, name=f"Dup analysis {workspace_id}"))
        session.flush()


def _ensure_product(session) -> None:
    _ensure_workspace(session, CTX.workspace_id)
    if session.get(ProductRecord, PROD_ID) is None:
        session.add(ProductRecord(
            id=PROD_ID, workspace_id=CTX.workspace_id, name="P dup-analysis",
            product_profile_entry_id="le_pv_dupan",
        ))
        session.flush()
    else:
        session.get(ProductRecord, PROD_ID).product_profile_entry_id = "le_pv_dupan"


def _seed_library_versions(session) -> None:
    for entry_id, ver_id, etype in [
        ("le_pv_dupan", "pv_dupan", "product"),
        ("le_ov_dupan", "ov_dupan", "offer"),
    ]:
        if session.get(LibraryEntryRecord, entry_id) is None:
            session.add(LibraryEntryRecord(
                id=entry_id, workspace_id=CTX.workspace_id,
                entry_type=etype, display_name=f"Test {etype}",
            ))
            session.flush()
        if session.get(LibraryVersionRecord, ver_id) is None:
            content = {} if etype == "product" else {
                "product_version_id": "pv_dupan",
                "market_scope": ["US"],
                "landing_page_url": "https://example.test/offer",
                "valid_from": "2026-01-01",
                "valid_to": "2027-01-01",
                "source_market_snapshot": {
                    "market": "US", "placement": "feed", "locale": "en-US",
                    "country": "US", "currency": "USD",
                },
            }
            session.add(LibraryVersionRecord(
                id=ver_id, workspace_id=CTX.workspace_id,
                entry_id=entry_id, entry_type=etype,
                version_no=1, content_json=content,
                content_hash=_sha256(_canonical_json(content).encode("utf-8")),
                state="effective", eligibility="eligible", published_by="system",
            ))
            session.flush()


def _seed_asset_versions(session, tag: str, concept_key_ids: list[str]) -> list[str]:
    """Seed one AssetVersion per entry in `concept_key_ids`.

    Two indices sharing the same concept_key_id become a duplicate group.
    """
    _ensure_product(session)

    plan_id = f"plan_{tag}"
    if session.get(PlanRecord, plan_id) is None:
        session.add(PlanRecord(
            id=plan_id, workspace_id=CTX.workspace_id, product_id=PROD_ID,
            mode="full", cap=0, total_full=1, snapshot_hash="a" * 64,
        ))
        session.flush()

    run_id = f"run_{tag}"
    if session.get(GenerationRunRecord, run_id) is None:
        session.add(GenerationRunRecord(
            id=run_id, workspace_id=CTX.workspace_id, product_id=PROD_ID, plan_id=plan_id,
            provider="mock", model="mock", aspect_ratio="1:1", image_size="1024",
            variants=1, use_ref=False, status="done", total=1, done=1, failed=0,
            est_cost=Decimal("0"), currency="USD",
        ))
        session.flush()

    av_ids: list[str] = []
    for i, concept_key_id in enumerate(concept_key_ids):
        if session.get(DimensionConceptKeyRecord, concept_key_id) is None:
            session.add(DimensionConceptKeyRecord(
                id=concept_key_id, workspace_id=CTX.workspace_id, scope="workspace",
                name=f"Concept {concept_key_id}", created_by="test",
            ))
            session.flush()

        run_item_id = f"ritem_{tag}_{i}"
        session.add(RunItemRecord(
            id=run_item_id, workspace_id=CTX.workspace_id, generation_run_id=run_id,
            ordinal=i,
            combination_identity_json={"concepts": [{
                "dimension_key": "creative_angle",
                "concept_key_id": concept_key_id,
                "stable_dimension_signature": "creative_angle:v1",
                "value": {"name": concept_key_id},
            }]},
            combination_fingerprint=_sha256(f"combo:{tag}:{i}".encode()),
            stable_dimension_signature="creative_angle:v1",
            semantic_fingerprint=_sha256(f"semantic:{tag}:{i}".encode()),
            execution_mode="mock", state="succeeded",
        ))
        session.flush()

        asset_id = f"asset_{tag}_{i}"
        av_id = f"av_{tag}_{i}"
        session.add(AssetRecord(
            id=asset_id, workspace_id=CTX.workspace_id, product_id=PROD_ID, run_id=run_id,
            combo_id="C01", combo_code="A1", tags_json={},
            filename=f"{tag}_{i}.png", provider="mock", model="mock",
            prompt="test", fingerprint=f"fp_{tag}_{i}", status="ok",
        ))
        session.flush()
        session.add(AssetVersionRecord(
            id=av_id, workspace_id=CTX.workspace_id, asset_id=asset_id,
            kind="generated", aspect_ratio="1:1", language="en", object_key=f"{tag}_{i}.png",
            content_sha256=f"{i:0>64x}", byte_size=1, mime_type="image/png",
            producing_run_item_id=run_item_id, origin_kind="generation",
            execution_mode="mock", truth_dependency_expected=False,
            truth_dependency_policy_version="truth-dependency-v1",
            truth_dependency_evidence_json={"expected": False},
            truth_dependency_evidence_hash=_sha256(
                _canonical_json({"expected": False}).encode("utf-8")
            ),
        ))
        session.flush()
        session.add(AssetQCAttemptRecord(
            id=f"qc_{tag}_{i}", workspace_id=CTX.workspace_id, asset_version_id=av_id,
            run_item_id=run_item_id, qc_policy_version="qc-v1", rule_set_version="rules-v1",
            input_hash=f"{i:0>64x}", execution_mode="mock", state="passed", attempt_sequence=1,
        ))
        session.add(QualityReviewRecord(
            id=f"review_{tag}_{i}", workspace_id=CTX.workspace_id, product_id=PROD_ID,
            asset_id=asset_id, asset_version_id=av_id, review_value="approved",
            legacy_unscoped=False, actor_id="reviewer",
        ))
        session.flush()
        av_ids.append(av_id)
    return av_ids


def _create_experiment_with_concepts(tag: str, concept_key_ids: list[str]) -> SimpleNamespace:
    with tenant_session(CTX) as s:
        avs = _seed_asset_versions(s, tag, concept_key_ids)
    exp = create_experiment_v2(CTX, product_id=PROD_ID, name=f"DA-{tag}", draft_context=_draft_context())
    rev = 1
    for av in avs:
        result = add_entry_v2(CTX, exp["id"], asset_version_id=av, expected_revision=rev)
        rev = result["revision"]
    return SimpleNamespace(id=exp["id"], revision=rev)


@pytest.fixture(scope="module", autouse=True)
def _migrated_db():
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")
    with tenant_session(CTX) as s:
        _ensure_product(s)
        _seed_library_versions(s)
    yield


@pytest.fixture(scope="module")
def experiment_with_dups() -> SimpleNamespace:
    return _create_experiment_with_concepts("dups1", ["ck_dupan_a", "ck_dupan_a"])


@pytest.fixture(scope="module")
def experiment_with_dups_for_stale() -> SimpleNamespace:
    return _create_experiment_with_concepts("stale1", ["ck_dupan_stale", "ck_dupan_stale"])


@pytest.fixture(scope="module")
def experiment_with_dups_for_revision_stale() -> SimpleNamespace:
    return _create_experiment_with_concepts("revstale1", ["ck_dupan_revstale", "ck_dupan_revstale"])


@pytest.fixture(scope="module")
def experiment_with_dups_for_member_stale() -> SimpleNamespace:
    return _create_experiment_with_concepts("memberstale1", ["ck_dupan_memberstale", "ck_dupan_memberstale"])


@pytest.fixture(scope="module")
def experiment_with_remap_collision() -> SimpleNamespace:
    return _create_experiment_with_concepts(
        "collision1", ["ck_dupan_col_a", "ck_dupan_col_a", "ck_dupan_col_b"]
    )


@pytest.fixture(scope="module")
def experiment_in_workspace_a() -> SimpleNamespace:
    return _create_experiment_with_concepts("wsA1", ["ck_dupan_wsA_1", "ck_dupan_wsA_2"])


class TestAnalysisFreezeDetectionParity:
    def test_analysis_and_freeze_produce_identical_detection(self, experiment_with_dups) -> None:
        """Spec §1.8 #1: analysis and freeze cross the same detection seam."""
        analysis = analyze_duplicates(CTX, experiment_with_dups.id)
        assert analysis["has_duplicates"] is True
        assert len(analysis["duplicate_groups"]) == 1
        assert analysis["analyzed_revision"] == experiment_with_dups.revision

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
        result = freeze_experiment_v2(
            CTX,
            experiment_with_dups.id,
            experiment_with_dups.revision,
            concept_resolution_plan=plan,
            analyzed_candidate_set_hash=analysis["candidate_set_hash"],
        )
        assert result["state"] == "frozen"

    def test_stale_candidate_set_hash_rejected(self, experiment_with_dups_for_stale) -> None:
        """The candidate_set_hash threaded from analysis must still match at freeze time."""
        analysis = analyze_duplicates(CTX, experiment_with_dups_for_stale.id)
        plan = [
            {
                "duplicate_group_hash": g["duplicate_group_hash"],
                "action": "allow_controlled_duplicate",
                "reason": "stale test",
                "source_candidate_fingerprints": sorted(
                    m["source_candidate_fingerprint"] for m in g["members"]
                ),
            }
            for g in analysis["duplicate_groups"]
        ]
        with pytest.raises(ExperimentConflict, match="concept_resolution_stale"):
            freeze_experiment_v2(
                CTX,
                experiment_with_dups_for_stale.id,
                experiment_with_dups_for_stale.revision,
                concept_resolution_plan=plan,
                analyzed_candidate_set_hash="0" * 64,
            )


class TestStaleDetection:
    def test_stale_revision_rejected(self, experiment_with_dups_for_revision_stale) -> None:
        """Spec §1.5: wrong expected_revision -> revision_conflict (checked before the
        candidate_set_hash/plan staleness checks in _apply_concept_resolution_plan)."""
        exp = experiment_with_dups_for_revision_stale
        with pytest.raises(ExperimentConflict, match="revision_conflict"):
            freeze_experiment_v2(
                CTX,
                exp.id,
                exp.revision + 999,
                analyzed_candidate_set_hash="0" * 64,
            )

    def test_stale_member_fingerprints_rejected(self, experiment_with_dups_for_member_stale) -> None:
        """Spec §1.5: plan with wrong member fingerprints -> concept_resolution_stale."""
        exp = experiment_with_dups_for_member_stale
        analysis = analyze_duplicates(CTX, exp.id)
        group = analysis["duplicate_groups"][0]
        plan = [{
            "duplicate_group_hash": group["duplicate_group_hash"],
            "action": "allow_controlled_duplicate",
            "reason": "test",
            "source_candidate_fingerprints": ["bad" * 22],  # wrong fingerprints
        }]
        with pytest.raises(ExperimentConflict, match="concept_resolution_stale"):
            freeze_experiment_v2(
                CTX,
                exp.id,
                exp.revision,
                concept_resolution_plan=plan,
                analyzed_candidate_set_hash=analysis["candidate_set_hash"],
            )


class TestRemapCollision:
    def test_remap_creating_collision_rejected(self, experiment_with_remap_collision) -> None:
        """Spec §1.8 #7: a remap that produces a NEW collision is rejected.

        NOTE: deviates from the brief's illustrative `match="remap"` — the
        actual guard that rejects a remap-created collision is the generic
        post-remap duplicate check in `_apply_concept_resolution_plan`,
        which raises `ExperimentConflict("concept_unresolved:<hash>")`, not
        a remap-specific message. See task-1-report.md self-review notes.
        """
        analysis = analyze_duplicates(CTX, experiment_with_remap_collision.id)
        assert analysis["has_duplicates"] is True
        assert len(analysis["duplicate_groups"]) == 1
        group = analysis["duplicate_groups"][0]
        assert len(group["members"]) == 2

        collision_target_key_id = "ck_dupan_col_b"
        assert collision_target_key_id in {
            t["concept_key_id"] for t in group["remap_targets"]
        }

        plan = [{
            "duplicate_group_hash": group["duplicate_group_hash"],
            "action": "remap_before_freeze",
            "reason": "test remap",
            "source_candidate_fingerprints": sorted(
                m["source_candidate_fingerprint"] for m in group["members"]
            ),
            "source_candidate_fingerprint": group["members"][0]["source_candidate_fingerprint"],
            "remap_concept_key_id": collision_target_key_id,
        }]
        with pytest.raises(ExperimentConflict, match="concept_unresolved"):
            freeze_experiment_v2(
                CTX,
                experiment_with_remap_collision.id,
                experiment_with_remap_collision.revision,
                concept_resolution_plan=plan,
                analyzed_candidate_set_hash=analysis["candidate_set_hash"],
            )


class TestCrossWorkspaceRejection:
    def test_analysis_rejects_foreign_workspace(self, experiment_in_workspace_a) -> None:
        """Spec §1.8 #8: cross-workspace analysis is rejected as not-found."""
        with pytest.raises(ExperimentNotFound):
            analyze_duplicates(OTHER_CTX, experiment_in_workspace_a.id)

    def test_freeze_rejects_foreign_workspace(self, experiment_in_workspace_a) -> None:
        """Spec §1.8 #8: cross-workspace freeze is rejected as not-found."""
        with pytest.raises(ExperimentNotFound):
            freeze_experiment_v2(
                OTHER_CTX,
                experiment_in_workspace_a.id,
                1,
                analyzed_candidate_set_hash="0" * 64,
            )


# ---------------------------------------------------------------------------
# _compute_remap_targets: isolated, DB-backed, independent of experiments.
# ---------------------------------------------------------------------------

REMAP_CTX = TenantContext(
    workspace_id="ws_dupan_remap",
    role="owner",
    initiator_id="user_dupan_remap",
    request_id="req_dupan_remap",
    trace_id="trace_dupan_remap",
)


@pytest.fixture(scope="module")
def remap_target_keys() -> dict[str, str]:
    with tenant_session(REMAP_CTX) as s:
        _ensure_workspace(s, REMAP_CTX.workspace_id)
        for key_id, scope, ws in [
            ("rmt_current", "workspace", REMAP_CTX.workspace_id),
            ("rmt_workspace_alt", "workspace", REMAP_CTX.workspace_id),
            ("rmt_platform_alt", "platform", None),
        ]:
            if s.get(DimensionConceptKeyRecord, key_id) is None:
                s.add(DimensionConceptKeyRecord(
                    id=key_id, workspace_id=ws, scope=scope,
                    name=f"Key {key_id}", created_by="test",
                ))
        s.flush()
    return {
        "current": "rmt_current",
        "workspace_alt": "rmt_workspace_alt",
        "platform_alt": "rmt_platform_alt",
    }


def _two_member_group(current_key_id: str) -> list[dict]:
    return [{
        "dimension_key": "creative_angle",
        "concept_key_id": current_key_id,
        "concept_key_name": "Current",
        "members": [object(), object()],
        "allowed_actions": ["allow_controlled_duplicate", "exclude_source_candidate"],
    }]


class TestRemapTargets:
    def test_includes_platform_and_workspace_keys(self, remap_target_keys) -> None:
        """Spec §1.8 #14: remap targets include platform + workspace keys."""
        groups = _two_member_group(remap_target_keys["current"])
        with tenant_session(REMAP_CTX) as s:
            _compute_remap_targets(s, REMAP_CTX, groups)
        target_ids = {t["concept_key_id"] for t in groups[0]["remap_targets"]}
        assert remap_target_keys["workspace_alt"] in target_ids
        assert remap_target_keys["platform_alt"] in target_ids
        assert "remap_before_freeze" in groups[0]["allowed_actions"]

    def test_excludes_current_concept_key(self, remap_target_keys) -> None:
        """Spec §1.8 #14: remap targets exclude current key."""
        groups = _two_member_group(remap_target_keys["current"])
        with tenant_session(REMAP_CTX) as s:
            _compute_remap_targets(s, REMAP_CTX, groups)
        target_ids = {t["concept_key_id"] for t in groups[0]["remap_targets"]}
        assert remap_target_keys["current"] not in target_ids

    def test_remap_targets_deterministic_order(self, remap_target_keys) -> None:
        """Spec §1.8 #14: targets ordered by concept_key_id."""
        groups = _two_member_group(remap_target_keys["current"])
        with tenant_session(REMAP_CTX) as s:
            _compute_remap_targets(s, REMAP_CTX, groups)
        ids = [t["concept_key_id"] for t in groups[0]["remap_targets"]]
        assert ids == sorted(ids)

    def test_three_member_group_gets_no_remap_targets(self, remap_target_keys) -> None:
        groups = _two_member_group(remap_target_keys["current"])
        groups[0]["members"].append(object())
        with tenant_session(REMAP_CTX) as s:
            _compute_remap_targets(s, REMAP_CTX, groups)
        assert groups[0]["remap_targets"] == []
        assert "remap_before_freeze" not in groups[0]["allowed_actions"]


# ---------------------------------------------------------------------------
# Frozen resolution evidence + three-way runtime invariant (spec §1.6, §1.8
# items 9, 10, 12).
# ---------------------------------------------------------------------------

def _freeze_with_allow_plan(exp: SimpleNamespace) -> SimpleNamespace:
    """Freeze `exp` resolving every detected duplicate group via
    allow_controlled_duplicate. Returns the frozen experiment identity."""
    analysis = analyze_duplicates(CTX, exp.id)
    plan = [
        {
            "duplicate_group_hash": g["duplicate_group_hash"],
            "action": "allow_controlled_duplicate",
            "reason": "evidence test",
            "source_candidate_fingerprints": sorted(
                m["source_candidate_fingerprint"] for m in g["members"]
            ),
        }
        for g in analysis["duplicate_groups"]
    ]
    frozen = freeze_experiment_v2(
        CTX,
        exp.id,
        exp.revision,
        concept_resolution_plan=plan,
        analyzed_candidate_set_hash=analysis["candidate_set_hash"],
    )
    return SimpleNamespace(
        id=exp.id, revision=frozen["revision"], version_id=frozen["version_id"]
    )


@pytest.fixture(scope="module")
def frozen_experiment() -> SimpleNamespace:
    """One duplicate group, resolved via allow_controlled_duplicate."""
    exp = _create_experiment_with_concepts("evfrz1", ["ck_dupan_ev_a", "ck_dupan_ev_a"])
    return _freeze_with_allow_plan(exp)


@pytest.fixture(scope="module")
def frozen_no_dups() -> SimpleNamespace:
    """No duplicate groups at all -> evidence must be explicitly empty."""
    exp = _create_experiment_with_concepts("evnodup1", ["ck_dupan_ev_n1", "ck_dupan_ev_n2"])
    frozen = freeze_experiment_v2(CTX, exp.id, exp.revision)
    return SimpleNamespace(
        id=exp.id, revision=frozen["revision"], version_id=frozen["version_id"]
    )


@pytest.fixture
def frozen_experiment_for_row_tamper() -> SimpleNamespace:
    """Own experiment so mutating its resolution row doesn't poison other tests."""
    exp = _create_experiment_with_concepts("evtamperrow1", ["ck_dupan_ev_tr_a", "ck_dupan_ev_tr_a"])
    return _freeze_with_allow_plan(exp)


@pytest.fixture
def frozen_experiment_for_snapshot_tamper() -> SimpleNamespace:
    """Own experiment so mutating its frozen snapshot doesn't poison other tests."""
    exp = _create_experiment_with_concepts("evtampersnap1", ["ck_dupan_ev_ts_a", "ck_dupan_ev_ts_a"])
    return _freeze_with_allow_plan(exp)


@pytest.fixture(scope="module")
def frozen_experiment_with_remap() -> SimpleNamespace:
    """One duplicate group, resolved via remap_before_freeze to an unrelated key."""
    exp = _create_experiment_with_concepts("evremap1", ["ck_dupan_ev_rm_a", "ck_dupan_ev_rm_a"])
    with tenant_session(CTX) as s:
        if s.get(DimensionConceptKeyRecord, "ck_dupan_ev_rm_target") is None:
            s.add(DimensionConceptKeyRecord(
                id="ck_dupan_ev_rm_target", workspace_id=CTX.workspace_id,
                scope="workspace", name="Remap Target", created_by="test",
            ))
            s.flush()
    analysis = analyze_duplicates(CTX, exp.id)
    group = analysis["duplicate_groups"][0]
    plan = [{
        "duplicate_group_hash": group["duplicate_group_hash"],
        "action": "remap_before_freeze",
        "reason": "evidence remap test",
        "source_candidate_fingerprints": sorted(
            m["source_candidate_fingerprint"] for m in group["members"]
        ),
        "source_candidate_fingerprint": group["members"][0]["source_candidate_fingerprint"],
        "remap_concept_key_id": "ck_dupan_ev_rm_target",
    }]
    frozen = freeze_experiment_v2(
        CTX,
        exp.id,
        exp.revision,
        concept_resolution_plan=plan,
        analyzed_candidate_set_hash=analysis["candidate_set_hash"],
    )
    return SimpleNamespace(
        id=exp.id, revision=frozen["revision"], version_id=frozen["version_id"]
    )


class TestConceptResolutionEvidence:
    def test_evidence_present_with_expected_fields(self, frozen_experiment) -> None:
        detail = get_experiment(CTX, frozen_experiment.id)
        evidence = detail["concept_resolution_evidence"]
        assert evidence is not None
        assert evidence["had_duplicates"] is True
        assert len(evidence["resolutions"]) == 1
        row = evidence["resolutions"][0]
        assert row["action"] == "allow_controlled_duplicate"
        assert row["concept_key_id"] == "ck_dupan_ev_a"
        assert row["concept_key_name"]
        assert row["actor_role"] == CTX.role
        assert row["resolved_at"] is not None
        assert row["affected_candidate_fingerprint"] is None
        assert row["remap_destination_concept_key_id"] is None

    def test_remap_evidence_includes_destination_name(self, frozen_experiment_with_remap) -> None:
        """Spec: remap outcome evidence carries to_concept_key_name (Step 4)."""
        detail = get_experiment(CTX, frozen_experiment_with_remap.id)
        evidence = detail["concept_resolution_evidence"]
        remap_rows = [r for r in evidence["resolutions"] if r["action"] == "remap_before_freeze"]
        assert len(remap_rows) == 1
        row = remap_rows[0]
        assert row["remap_destination_concept_key_id"] == "ck_dupan_ev_rm_target"
        assert row["remap_destination_concept_key_name"] == "Remap Target"
        assert row["affected_candidate_fingerprint"] is not None


class TestThreeWayInvariant:
    def test_valid_freeze_passes_invariant(self, frozen_experiment) -> None:
        """Detail read after freeze passes the three-way check."""
        detail = get_experiment(CTX, frozen_experiment.id)
        evidence = detail["concept_resolution_evidence"]
        assert evidence is not None
        assert evidence["concept_resolutions_hash"]

    def test_tampered_resolution_row_fails(self, frozen_experiment_for_row_tamper) -> None:
        """Simulated row edit -> resolution_integrity_violation (spec §1.8 item 12)."""
        with tenant_session(CTX) as s:
            res_row = s.scalars(
                select(ExperimentVersionResolutionRecord).where(
                    ExperimentVersionResolutionRecord.experiment_version_id
                    == frozen_experiment_for_row_tamper.version_id
                )
            ).first()
            assert res_row is not None
            res_row.reason = "TAMPERED"
            s.flush()
        with pytest.raises(ExperimentError, match="resolution_integrity_violation"):
            get_experiment(CTX, frozen_experiment_for_row_tamper.id)

    def test_tampered_snapshot_fails(self, frozen_experiment_for_snapshot_tamper) -> None:
        """Simulated snapshot edit -> resolution_integrity_violation (spec §1.8 item 12)."""
        with tenant_session(CTX) as s:
            version = s.get(ExperimentVersionRecord, frozen_experiment_for_snapshot_tamper.version_id)
            snapshot = dict(version.frozen_snapshot_json)
            snapshot["concept_resolution_plan"] = []
            version.frozen_snapshot_json = snapshot
            s.flush()
        with pytest.raises(ExperimentError, match="resolution_integrity_violation"):
            get_experiment(CTX, frozen_experiment_for_snapshot_tamper.id)

    def test_no_duplicates_returns_explicit_empty_evidence(self, frozen_no_dups) -> None:
        """Spec §1.8 item 10."""
        detail = get_experiment(CTX, frozen_no_dups.id)
        evidence = detail["concept_resolution_evidence"]
        assert evidence["had_duplicates"] is False
        assert evidence["resolutions"] == []


class TestEvidenceStableAcrossLifecycleStates:
    def test_evidence_identical_frozen_running_closed(self) -> None:
        """Spec §1.8 item 9: frozen/running/closed reads show identical evidence."""
        exp = _create_experiment_with_concepts(
            "evstates1", ["ck_dupan_ev_st_a", "ck_dupan_ev_st_a"]
        )
        frozen_ns = _freeze_with_allow_plan(exp)
        evidence_frozen = get_experiment(CTX, frozen_ns.id)["concept_resolution_evidence"]

        frozen_detail = get_experiment(CTX, frozen_ns.id)
        for cand in frozen_detail["candidates"]:
            bind_candidate_v2(
                CTX, frozen_ns.id, cand["id"],
                ad_account_id=f"act_evstates1_{cand['position']}",
                ad_id=f"ad_evstates1_{cand['position']}",
                expected_revision=frozen_ns.revision,
            )
        running = start_experiment_v2(CTX, frozen_ns.id, expected_revision=frozen_ns.revision)
        evidence_running = get_experiment(CTX, frozen_ns.id)["concept_resolution_evidence"]

        close_experiment_v2(
            CTX, frozen_ns.id, expected_revision=running["revision"],
            closure_kind="aborted",
        )
        evidence_closed = get_experiment(CTX, frozen_ns.id)["concept_resolution_evidence"]

        assert evidence_frozen["had_duplicates"] is True
        assert evidence_frozen == evidence_running == evidence_closed
