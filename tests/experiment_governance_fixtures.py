"""Builders for experiment fixtures that satisfy authoritative freeze gates.

These helpers deliberately create the same persisted evidence production
freeze consumes.  They do not patch or bypass a service gate.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.adapters.persistence.models import (
    AssetRecord,
    AssetVersionRecord,
    DimensionConceptKeyRecord,
    GenerationRunRecord,
    LibraryVersionRecord,
    ProductRecord,
    QualityReviewRecord,
)
from app.generation.models import AssetQCAttemptRecord, RunItemRecord
from app.platform.canonical import canonical_hash
from app.platform.tenancy import TenantContext


def govern_product_offer_versions(
    session,
    context: TenantContext,
    *,
    product_id: str,
    product_version_id: str,
    offer_version_id: str,
    markets: tuple[str, ...] = ("US",),
) -> None:
    product = session.get(ProductRecord, product_id)
    product_version = session.get(LibraryVersionRecord, product_version_id)
    offer_version = session.get(LibraryVersionRecord, offer_version_id)
    assert product is not None
    assert product_version is not None
    assert offer_version is not None

    product.product_profile_entry_id = product_version.entry_id
    product_version.state = "effective"
    product_version.eligibility = "eligible"
    offer_version.state = "effective"
    offer_version.eligibility = "eligible"
    offer_content = dict(offer_version.content_json or {})
    offer_content.update({
        "product_version_id": product_version.id,
        "market_scope": list(markets),
        "landing_page_url": "https://example.test/experiment",
        "valid_from": "2026-01-01",
        "valid_to": "2027-12-31",
        "source_market_snapshot": {
            "market": markets[0],
            "placement": "feed",
            "locale": "en-US",
            "country": markets[0],
            "currency": "USD",
        },
    })
    offer_version.content_json = offer_content
    offer_version.content_hash = canonical_hash(offer_content)
    session.flush()


def govern_asset_versions(
    session,
    context: TenantContext,
    *,
    product_id: str,
    asset_version_ids: list[str],
    tag: str,
) -> None:
    """Persist distinct governed concepts plus exact Review/truth evidence."""
    next_ordinals: dict[str, int] = {}
    for index, asset_version_id in enumerate(asset_version_ids):
        version = session.get(AssetVersionRecord, asset_version_id)
        assert version is not None
        asset = session.get(AssetRecord, version.asset_id)
        assert asset is not None
        run = session.get(GenerationRunRecord, asset.run_id)
        assert run is not None

        identity_seed = canonical_hash([context.workspace_id, tag, asset_version_id])
        concept_key_id = f"ck_{identity_seed[:40]}"
        if session.get(DimensionConceptKeyRecord, concept_key_id) is None:
            session.add(DimensionConceptKeyRecord(
                id=concept_key_id,
                workspace_id=context.workspace_id,
                scope="workspace",
                name=f"Governed concept {identity_seed[:12]}",
                created_by="test-fixture",
            ))
            session.flush()

        run_item = (
            session.get(RunItemRecord, version.producing_run_item_id)
            if version.producing_run_item_id
            else None
        )
        if run_item is None:
            if run.id not in next_ordinals:
                max_ordinal = session.scalar(select(func.max(RunItemRecord.ordinal)).where(
                    RunItemRecord.workspace_id == context.workspace_id,
                    RunItemRecord.generation_run_id == run.id,
                ))
                next_ordinals[run.id] = 0 if max_ordinal is None else max_ordinal + 1
            ordinal = next_ordinals[run.id]
            next_ordinals[run.id] += 1
            run_item = RunItemRecord(
                id=f"ri_{identity_seed[:40]}",
                workspace_id=context.workspace_id,
                generation_run_id=run.id,
                ordinal=ordinal,
                combination_identity_json={},
                combination_fingerprint=canonical_hash([identity_seed, "combination"]),
                stable_dimension_signature="creative_angle:v1",
                semantic_fingerprint=canonical_hash([identity_seed, "semantic"]),
                execution_mode=version.execution_mode or run.execution_mode,
                state="succeeded",
            )
            session.add(run_item)
            session.flush()

        run_item.combination_identity_json = {
            "concepts": [{
                "dimension_key": "creative_angle",
                "concept_key_id": concept_key_id,
                "stable_dimension_signature": "creative_angle:v1",
                "value": {"fixture_identity": identity_seed},
            }],
        }
        run_item.stable_dimension_signature = "creative_angle:v1"
        version.producing_run_item_id = run_item.id
        version.origin_kind = "generation"
        version.execution_mode = run_item.execution_mode
        version.truth_dependency_expected = False
        version.truth_dependency_policy_version = "truth-dependency-v1"
        version.truth_dependency_evidence_json = {
            "expected": False,
            "source": "test-fixture",
        }
        version.truth_dependency_evidence_hash = canonical_hash(
            version.truth_dependency_evidence_json
        )

        qc_id = f"qc_{identity_seed[:40]}"
        if session.get(AssetQCAttemptRecord, qc_id) is None:
            max_sequence = session.scalar(
                select(func.max(AssetQCAttemptRecord.attempt_sequence)).where(
                    AssetQCAttemptRecord.workspace_id == context.workspace_id,
                    AssetQCAttemptRecord.asset_version_id == version.id,
                )
            )
            session.add(AssetQCAttemptRecord(
                id=qc_id,
                workspace_id=context.workspace_id,
                asset_version_id=version.id,
                run_item_id=run_item.id,
                qc_policy_version="qc-v1",
                rule_set_version="rules-v1",
                input_hash=version.content_sha256,
                execution_mode=version.execution_mode,
                state="passed",
                attempt_sequence=(max_sequence or 0) + 1,
            ))

        review_id = f"qr_{identity_seed[:40]}"
        if session.get(QualityReviewRecord, review_id) is None:
            session.add(QualityReviewRecord(
                id=review_id,
                workspace_id=context.workspace_id,
                product_id=product_id,
                asset_id=asset.id,
                asset_version_id=version.id,
                legacy_unscoped=False,
                review_value="approved",
                actor_id="test-reviewer",
            ))
        session.flush()
