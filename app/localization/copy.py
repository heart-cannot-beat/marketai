"""Localization copy module (#24, spec §2F/§2G, §16.7).

Spec: docs/superpowers/specs/2026-09-06-localization-truth-bounded-qc-design.md
§2F ("app/localization/copy.py"), §1.6 (`localized_copy_versions` /
`localized_copy_approvals`), §16.7 (claims, locked copy and QC acceptance
evidence, nine-market matrix).

`LocalizedCopyVersionRecord` rows are immutable market-ready copy --
either produced by one external `localization.translate_copy` call, or by
a deterministic **source-locale projection** when the target locale
equals the source locale. Locale equality only ever controls whether the
*linguistic translation* call happens; Claim decisions (replace/remove/
preserve), Fact replacement, CopyPolicy edits, and downstream QC all stay
in force either way -- a source-locale projection is a real localized
copy, not a policy skip (spec §2F, §16.7 `en-GB`/`en-AU`/`en-CA` rows).

`prepare_copy_segments()` is the shared derivation both creation paths
use: it turns one Inventory's Claims plus one ClaimDecisionSet's resolved
per-Claim decisions into three disjoint buckets --

- `preserve` -> a **translatable** segment carrying the source OCR text;
- `replace` -> an **opaque locked-placeholder token** standing in for the
  resolved Fact value. Translation never receives the raw fact value
  (spec §16.7: "translation receives opaque locked placeholders rather
  than mutable values"); the actual value is injected back
  deterministically, after the external call, by `create_copy_from_translation`
  itself -- never by the translator;
- `remove` -> the Claim's region is dropped entirely and never reaches
  translation at all.

`plan_copy_versions()` is a read-only reuse plan, mirroring
`app.localization.claims.plan_inventories()`: for each requested copy
series it reuses the latest non-superseded `LocalizedCopyVersionRecord`
sharing the exact semantic fingerprint (`copy_series_hash` -- source
AssetVersion, Inventory, target market, TruthPackage, Claim decision set,
CopyPolicy snapshot, copy origin, and -- only when translation was used --
the exact translation recipe/provider/model/adapter/mode/scenario) *only*
when that version's derived evidence tier is at least as strong as the
caller's requested tier and the referenced TruthPackage is currently
fresh (`app.localization.truth.assess_truth_freshness`). It never creates
a row; it only tells the caller which series can reuse, which need a
deterministic source-locale projection, and which still need a
translation call.

`approve_copy_version()` appends an immutable
`LocalizedCopyApprovalRecord` and, the first time a version is approved,
advances its one mutable column (`state`) along the only transitions the
immutable trigger allows (`needs_review` -> `approved`; every copy
version is created directly at `needs_review`, never `draft`, since
`draft -> approved` is not an allowed transition). It does
not create a `QualityReviewRecord` -- a localized AssetVersion built from
an approved copy version is still unreviewed until a human QC decision
exists.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    LibraryVersionRecord,
    LocalizationClaimDecisionRecord,
    LocalizationClaimDecisionSetRecord,
    LocalizedCopyApprovalRecord,
    LocalizedCopyVersionRecord,
    SourceClaimInventoryRecord,
    TruthFactRecord,
    new_id,
)
from ..platform.tenancy import TenantContext
from .evidence_tier import EvidenceTier, tier_of_provider
from .truth import FreshnessReport, assess_truth_freshness

ROLES = ("owner", "member")
COPY_ORIGINS = ("translated", "source_locale_projection")
STATES = ("draft", "needs_review", "approved", "superseded")

# The only transitions the immutable-except-`state` trigger allows (spec
# §1.6): draft -> needs_review|superseded, needs_review -> approved|superseded,
# approved -> superseded. `draft -> approved` is NOT allowed, so a copy
# version is never created directly in `draft` -- by the time either
# creation path returns, translation/injection/validation are already
# complete, so the version starts life at `needs_review` (see
# `create_source_locale_projection()` / `create_copy_from_translation()`).
# `superseded` is terminal.
_ALLOWED_APPROVAL_SOURCE_STATES = ("needs_review",)


class CopyError(RuntimeError):
    """Raised for malformed input the public interface should never let a
    well-behaved caller construct (e.g. an incomplete ClaimDecisionSet, or
    a translated-segment map that does not match the prepared plan)."""


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _placeholder_token(decision_set_id: str, claim_index: int, claim_scope_key: str) -> str:
    """A stable, opaque token for one `replace` Claim decision. Translation
    sees only this token in place of the segment's real text -- never the
    resolved Fact value -- and the token is later located and replaced by
    deterministic injection, never left for the translator to reproduce."""
    digest = hashlib.sha256(
        f"{decision_set_id}:{claim_index}:{claim_scope_key}".encode("utf-8")
    ).hexdigest()[:20]
    return f"⟦LOCK:{digest}⟧"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CopySegment:
    """One `preserve` Claim's source text, ready for linguistic
    translation (or, for a source-locale projection, already in the
    target locale as-is)."""

    claim_index: int
    claim_scope_key: str
    text: str


@dataclass(frozen=True)
class LockedPlaceholder:
    """One `replace` Claim's opaque stand-in. `value_json` is the
    resolved Fact's value -- injected back after translation, never sent
    to it."""

    claim_index: int
    claim_scope_key: str
    token: str
    replacement_fact_id: str
    value_json: object


@dataclass(frozen=True)
class PreparedCopySegments:
    """The result of deriving translation/projection input from one
    Inventory + one ClaimDecisionSet. `source_text_hash` covers every
    Claim's original source text (regardless of action) so a copy
    version's provenance can be tied to the exact source content it was
    built from."""

    translatable: tuple[CopySegment, ...]
    locked_placeholders: tuple[LockedPlaceholder, ...]
    removed_claim_indexes: tuple[int, ...]
    source_text_hash: str


def prepare_copy_segments(
    session: Session,
    ctx: TenantContext,
    *,
    inventory_id: str,
    decision_set_id: str,
) -> PreparedCopySegments:
    """Derive translation/projection input from one Inventory's Claims and
    one ClaimDecisionSet's resolved decisions. Every Claim becomes exactly
    one segment -- see module docstring for the three buckets."""
    inventory = session.scalar(
        select(SourceClaimInventoryRecord).where(
            SourceClaimInventoryRecord.workspace_id == ctx.workspace_id,
            SourceClaimInventoryRecord.id == inventory_id,
        )
    )
    if inventory is None:
        raise CopyError("missing_inventory")

    decision_set = session.scalar(
        select(LocalizationClaimDecisionSetRecord).where(
            LocalizationClaimDecisionSetRecord.workspace_id == ctx.workspace_id,
            LocalizationClaimDecisionSetRecord.id == decision_set_id,
        )
    )
    if decision_set is None:
        raise CopyError("missing_decision_set")
    if decision_set.inventory_id != inventory_id:
        raise CopyError("decision_set_inventory_mismatch")

    decisions = session.scalars(
        select(LocalizationClaimDecisionRecord)
        .where(
            LocalizationClaimDecisionRecord.workspace_id == ctx.workspace_id,
            LocalizationClaimDecisionRecord.decision_set_id == decision_set_id,
        )
        .order_by(LocalizationClaimDecisionRecord.claim_index)
    ).all()
    if len(decisions) != decision_set.decision_count:
        raise CopyError("incomplete_decision_set")

    claims = inventory.claims_json

    translatable: list[CopySegment] = []
    locked: list[LockedPlaceholder] = []
    removed: list[int] = []
    source_texts: list[dict] = []

    for decision in decisions:
        claim = claims[decision.claim_index]
        text = str(claim.get("ocr_text", ""))
        source_texts.append({"claim_index": decision.claim_index, "text": text})

        if decision.action == "remove":
            removed.append(decision.claim_index)
        elif decision.action == "replace":
            fact = session.get(TruthFactRecord, decision.replacement_fact_id)
            if fact is None:
                raise CopyError(f"missing_replacement_fact_claim_{decision.claim_index}")
            token = _placeholder_token(decision_set_id, decision.claim_index, decision.claim_scope_key)
            locked.append(
                LockedPlaceholder(
                    claim_index=decision.claim_index,
                    claim_scope_key=decision.claim_scope_key,
                    token=token,
                    replacement_fact_id=fact.id,
                    value_json=fact.value_json,
                )
            )
        else:  # preserve
            translatable.append(
                CopySegment(
                    claim_index=decision.claim_index,
                    claim_scope_key=decision.claim_scope_key,
                    text=text,
                )
            )

    return PreparedCopySegments(
        translatable=tuple(translatable),
        locked_placeholders=tuple(locked),
        removed_claim_indexes=tuple(removed),
        source_text_hash=_hash(sorted(source_texts, key=lambda row: row["claim_index"])),
    )


def _assemble_content_segments(
    prepared: PreparedCopySegments, *, resolved_text: Mapping[int, str]
) -> list[dict]:
    """Merge the three buckets back into one ordered, claim-index-sorted
    content payload. `resolved_text` supplies the final text for every
    translatable segment -- the translated text for a translation-origin
    copy, or the untouched source text for a source-locale projection."""
    by_index: dict[int, dict] = {}
    for seg in prepared.translatable:
        by_index[seg.claim_index] = {
            "claim_index": seg.claim_index,
            "claim_scope_key": seg.claim_scope_key,
            # "text" is the final linguistic content of a `preserve`
            # segment -- the translated text for a translation-origin
            # copy, or the untouched source text for a source-locale
            # projection (spec §2F: locale equality only skips the
            # external call, never Claim/Fact policy).
            "kind": "text",
            "text": resolved_text[seg.claim_index],
        }
    for lp in prepared.locked_placeholders:
        by_index[lp.claim_index] = {
            "claim_index": lp.claim_index,
            "claim_scope_key": lp.claim_scope_key,
            "kind": "locked_replace",
            "replacement_fact_id": lp.replacement_fact_id,
            "value_json": lp.value_json,
        }
    for claim_index in prepared.removed_claim_indexes:
        by_index[claim_index] = {"claim_index": claim_index, "kind": "removed"}
    return [by_index[i] for i in sorted(by_index)]


# ---------------------------------------------------------------------------
# Copy-series identity and evidence tier
# ---------------------------------------------------------------------------


def _copy_series_hash(
    *,
    source_asset_version_id: str,
    inventory_id: str,
    target_market: str,
    truth_package_id: str,
    claim_decision_set_id: str,
    copy_policy_snapshot_hash: str,
    copy_origin: str,
    translation_execution_mode: str | None = None,
    translation_provider: str | None = None,
    translation_model: str | None = None,
    translation_adapter_version: str | None = None,
    mock_scenario: str | None = None,
    mock_version: str | None = None,
) -> str:
    """Semantic identity (spec §1.6): source AssetVersion + Inventory +
    target market + TruthPackage + Claim decision set + CopyPolicy
    snapshot + copy origin + the exact translation recipe/provider/model/
    adapter/mode/scenario when translation is used. Deliberately excludes
    `content_json` -- reuse is about matching *recipe*, not byte-identical
    prior output."""
    payload = {
        "source_asset_version_id": source_asset_version_id,
        "inventory_id": inventory_id,
        "target_market": target_market,
        "truth_package_id": truth_package_id,
        "claim_decision_set_id": claim_decision_set_id,
        "copy_policy_snapshot_hash": copy_policy_snapshot_hash,
        "copy_origin": copy_origin,
        "translation_execution_mode": translation_execution_mode,
        "translation_provider": translation_provider,
        "translation_model": translation_model,
        "translation_adapter_version": translation_adapter_version,
        "mock_scenario": mock_scenario,
        "mock_version": mock_version,
    }
    return _hash(payload)


def _copy_evidence_tier(copy_origin: str, translation_execution_mode: str | None) -> EvidenceTier:
    """A source-locale projection is deterministic and provider-free, so
    it is never the weak link -- treat it as PRODUCTION tier. A
    translated copy's tier follows its `translation_execution_mode`
    exactly like a Batch's `provider_execution_mode` (spec §3C)."""
    if copy_origin == "source_locale_projection":
        return EvidenceTier.PRODUCTION
    return tier_of_provider(translation_execution_mode)


def _validate_brand_policy(
    session: Session, ctx: TenantContext, brand_localization_policy_version_id: str | None
) -> None:
    """Spec §1.6: "PostgreSQL trigger verifies any non-null Brand policy
    version has Library entry type `brand_localization_policy`; the
    `copy.py` interface performs the same check on SQLite" -- this is
    that SQLite-side check."""
    if brand_localization_policy_version_id is None:
        return
    version = session.scalar(
        select(LibraryVersionRecord).where(
            LibraryVersionRecord.workspace_id == ctx.workspace_id,
            LibraryVersionRecord.id == brand_localization_policy_version_id,
        )
    )
    if version is None or version.entry_type != "brand_localization_policy":
        raise CopyError("invalid_brand_localization_policy_version")


def _next_version_number(session: Session, ctx: TenantContext, copy_series_hash: str) -> int:
    existing_max = session.scalar(
        select(LocalizedCopyVersionRecord.version_number)
        .where(
            LocalizedCopyVersionRecord.workspace_id == ctx.workspace_id,
            LocalizedCopyVersionRecord.copy_series_hash == copy_series_hash,
        )
        .order_by(LocalizedCopyVersionRecord.version_number.desc())
    )
    return (existing_max or 0) + 1


# ---------------------------------------------------------------------------
# Planning (read-only reuse)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranslationRecipe:
    """The exact translation recipe that participates in `copy_series_hash`
    -- required for any `CopyPlanRequest` whose `source_locale` differs
    from its `target_locale`."""

    execution_mode: Literal["byok", "sandbox", "mock"]
    provider: str
    model: str
    adapter_version: str
    mock_scenario: str | None = None
    mock_version: str | None = None


@dataclass(frozen=True)
class CopyPlanRequest:
    """One requested copy series to plan. `source_locale`/`target_locale`
    decide only whether translation is needed -- they never bypass Claim
    decisions, Fact replacement, policy edits, or QC (spec §16.7 nine-
    market matrix)."""

    key: str
    source_asset_version_id: str
    inventory_id: str
    truth_package_id: str
    truth_package_market_id: str
    claim_decision_set_id: str
    target_market: str
    market_policy_version_id: str
    source_locale: str
    target_locale: str
    copy_policy_snapshot: Mapping[str, object]
    translation_recipe: TranslationRecipe | None = None


@dataclass(frozen=True)
class CopyPlan:
    """Result of `plan_copy_versions()`."""

    reused: Mapping[str, str]  # request.key -> existing copy_version_id
    source_locale_projections: tuple[str, ...]  # request.key needing create_source_locale_projection
    missing_translations: tuple[str, ...]  # request.key needing create_copy_from_translation
    stale: Mapping[str, tuple[str, ...]]  # request.key -> freshness reasons blocking reuse


def _copy_origin_for(request: CopyPlanRequest) -> str:
    return "source_locale_projection" if request.source_locale == request.target_locale else "translated"


def _series_hash_for_request(request: CopyPlanRequest, copy_origin: str) -> str:
    recipe = request.translation_recipe
    return _copy_series_hash(
        source_asset_version_id=request.source_asset_version_id,
        inventory_id=request.inventory_id,
        target_market=request.target_market,
        truth_package_id=request.truth_package_id,
        claim_decision_set_id=request.claim_decision_set_id,
        copy_policy_snapshot_hash=_hash(request.copy_policy_snapshot),
        copy_origin=copy_origin,
        translation_execution_mode=recipe.execution_mode if copy_origin == "translated" and recipe else None,
        translation_provider=recipe.provider if copy_origin == "translated" and recipe else None,
        translation_model=recipe.model if copy_origin == "translated" and recipe else None,
        translation_adapter_version=recipe.adapter_version if copy_origin == "translated" and recipe else None,
        mock_scenario=recipe.mock_scenario if copy_origin == "translated" and recipe else None,
        mock_version=recipe.mock_version if copy_origin == "translated" and recipe else None,
    )


def plan_copy_versions(
    session: Session,
    ctx: TenantContext,
    *,
    requests: Sequence[CopyPlanRequest],
    requested_evidence_tier: EvidenceTier,
    at: datetime,
) -> CopyPlan:
    """Read-only reuse plan over `LocalizedCopyVersionRecord`.

    For each requested series: reuses the latest non-superseded version
    sharing the exact `copy_series_hash` when its derived evidence tier is
    at least as strong as `requested_evidence_tier` *and*
    `assess_truth_freshness()` reports the referenced TruthPackage as
    currently current; otherwise reports the series as needing a
    deterministic source-locale projection or a translation call,
    whichever `source_locale`/`target_locale` implies. Never creates a
    row.
    """
    reused: dict[str, str] = {}
    projections: list[str] = []
    translations: list[str] = []
    stale: dict[str, tuple[str, ...]] = {}

    for request in requests:
        copy_origin = _copy_origin_for(request)
        if copy_origin == "translated" and request.translation_recipe is None:
            raise CopyError(f"request_{request.key}_missing_translation_recipe")

        series_hash = _series_hash_for_request(request, copy_origin)

        existing = session.scalar(
            select(LocalizedCopyVersionRecord)
            .where(
                LocalizedCopyVersionRecord.workspace_id == ctx.workspace_id,
                LocalizedCopyVersionRecord.copy_series_hash == series_hash,
                LocalizedCopyVersionRecord.state != "superseded",
            )
            .order_by(LocalizedCopyVersionRecord.version_number.desc())
        )

        reusable = False
        if existing is not None:
            tier = _copy_evidence_tier(existing.copy_origin, existing.translation_execution_mode)
            if tier >= requested_evidence_tier:
                freshness: FreshnessReport = assess_truth_freshness(
                    session, ctx, request.truth_package_id, at=at
                )
                if freshness.is_current:
                    reused[request.key] = existing.id
                    reusable = True
                else:
                    stale[request.key] = freshness.reasons

        if not reusable:
            if copy_origin == "source_locale_projection":
                projections.append(request.key)
            else:
                translations.append(request.key)

    return CopyPlan(
        reused=reused,
        source_locale_projections=tuple(projections),
        missing_translations=tuple(translations),
        stale=stale,
    )


# ---------------------------------------------------------------------------
# Creation (write-once)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CopyCreationResult:
    copy_version: LocalizedCopyVersionRecord


def create_source_locale_projection(
    session: Session,
    ctx: TenantContext,
    *,
    source_asset_version_id: str,
    inventory_id: str,
    truth_package_id: str,
    truth_package_market_id: str,
    claim_decision_set_id: str,
    target_market: str,
    market_policy_version_id: str,
    copy_policy_snapshot: Mapping[str, object],
    brand_localization_policy_version_id: str | None = None,
    at: datetime | None = None,
    now: datetime | None = None,
) -> CopyCreationResult:
    """Deterministic projection for a target locale equal to the source
    locale: no `localization.translate_copy` call. Claim decisions
    (replace/remove/preserve) and Fact replacement still apply in full --
    this is a real localized copy, not a policy skip (spec §2F, §16.7
    `en-GB`/`en-AU`/`en-CA` rows).
    """
    _validate_brand_policy(session, ctx, brand_localization_policy_version_id)

    freshness = assess_truth_freshness(session, ctx, truth_package_id, at=at or now or datetime.now(timezone.utc))
    if not freshness.is_current:
        raise CopyError(f"stale_truth_package:{','.join(freshness.reasons)}")

    prepared = prepare_copy_segments(
        session, ctx, inventory_id=inventory_id, decision_set_id=claim_decision_set_id
    )
    # No translation call: every `preserve` segment's resolved text is its
    # own untouched source text.
    resolved_text = {seg.claim_index: seg.text for seg in prepared.translatable}
    content_segments = _assemble_content_segments(prepared, resolved_text=resolved_text)
    content = {"segments": content_segments, "source_text_hash": prepared.source_text_hash}

    copy_policy_snapshot_hash = _hash(copy_policy_snapshot)
    series_hash = _copy_series_hash(
        source_asset_version_id=source_asset_version_id,
        inventory_id=inventory_id,
        target_market=target_market,
        truth_package_id=truth_package_id,
        claim_decision_set_id=claim_decision_set_id,
        copy_policy_snapshot_hash=copy_policy_snapshot_hash,
        copy_origin="source_locale_projection",
    )
    version_number = _next_version_number(session, ctx, series_hash)

    evidence_hash = _hash(
        {
            "content": content,
            "copy_origin": "source_locale_projection",
            "truth_package_id": truth_package_id,
            "claim_decision_set_id": claim_decision_set_id,
            "source_text_hash": prepared.source_text_hash,
        }
    )

    copy_version = LocalizedCopyVersionRecord(
        id=new_id("lcv"),
        workspace_id=ctx.workspace_id,
        copy_series_hash=series_hash,
        version_number=version_number,
        source_asset_version_id=source_asset_version_id,
        inventory_id=inventory_id,
        truth_package_id=truth_package_id,
        truth_package_market_id=truth_package_market_id,
        claim_decision_set_id=claim_decision_set_id,
        target_market=target_market,
        market_policy_version_id=market_policy_version_id,
        content_json=content,
        copy_origin="source_locale_projection",
        translation_execution_mode=None,
        translation_provider=None,
        translation_model=None,
        translation_adapter_version=None,
        mock_scenario=None,
        mock_version=None,
        brand_localization_policy_version_id=brand_localization_policy_version_id,
        copy_policy_snapshot_json=dict(copy_policy_snapshot),
        copy_policy_snapshot_hash=copy_policy_snapshot_hash,
        # Translation/injection/validation are already complete by the
        # time this constructor runs, and `draft -> approved` is not an
        # allowed transition -- so the version starts at `needs_review`,
        # never `draft`.
        state="needs_review",
        evidence_hash=evidence_hash,
        created_at=now or datetime.now(timezone.utc),
    )
    session.add(copy_version)
    session.flush()
    return CopyCreationResult(copy_version=copy_version)


def create_copy_from_translation(
    session: Session,
    ctx: TenantContext,
    *,
    source_asset_version_id: str,
    inventory_id: str,
    truth_package_id: str,
    truth_package_market_id: str,
    claim_decision_set_id: str,
    target_market: str,
    market_policy_version_id: str,
    copy_policy_snapshot: Mapping[str, object],
    translated_segments: Mapping[int, str],
    translation_execution_mode: Literal["byok", "sandbox", "mock"],
    translation_provider: str,
    translation_model: str,
    translation_adapter_version: str,
    mock_scenario: str | None = None,
    mock_version: str | None = None,
    brand_localization_policy_version_id: str | None = None,
    at: datetime | None = None,
    now: datetime | None = None,
) -> CopyCreationResult:
    """Write-once completion of one `localization.translate_copy` call.

    `translated_segments` is the external call's result: translated text
    keyed by `claim_index`, for exactly the `preserve`-action segments
    `prepare_copy_segments()` marked translatable -- never for a
    `replace`/`remove` Claim. Deterministic locked-value injection and
    placeholder-integrity validation happen here, after the call, exactly
    as spec §2F requires ("Deterministic injection and validation happen
    after the external call").
    """
    _validate_brand_policy(session, ctx, brand_localization_policy_version_id)

    if translation_execution_mode == "mock":
        if not mock_scenario or not mock_version:
            raise CopyError("mock_translation_requires_scenario_and_version")
    elif mock_scenario or mock_version:
        raise CopyError("mock_fields_require_mock_execution_mode")

    freshness = assess_truth_freshness(session, ctx, truth_package_id, at=at or now or datetime.now(timezone.utc))
    if not freshness.is_current:
        raise CopyError(f"stale_truth_package:{','.join(freshness.reasons)}")

    prepared = prepare_copy_segments(
        session, ctx, inventory_id=inventory_id, decision_set_id=claim_decision_set_id
    )

    expected = {seg.claim_index for seg in prepared.translatable}
    provided = set(translated_segments.keys())
    if provided != expected:
        findings = []
        missing = sorted(expected - provided)
        extra = sorted(provided - expected)
        if missing:
            findings.append(f"missing_translated_segments:{missing}")
        if extra:
            findings.append(f"unexpected_translated_segments:{extra}")
        raise CopyError(";".join(findings) or "translated_segments_mismatch")

    # A locked placeholder token must never leak into the translator's
    # output -- neither the token this decision set minted for another
    # segment (a duplication/reorder bug) nor any other token-shaped text
    # (spec §16.7: "missing, duplicated, reordered-invalid or additional
    # placeholders fail validation").
    all_tokens = {lp.token for lp in prepared.locked_placeholders}
    for claim_index, text in translated_segments.items():
        for token in all_tokens:
            if token in text:
                raise CopyError(f"locked_placeholder_leaked_into_translation_claim_{claim_index}")

    content_segments = _assemble_content_segments(prepared, resolved_text=translated_segments)
    content = {"segments": content_segments, "source_text_hash": prepared.source_text_hash}

    copy_policy_snapshot_hash = _hash(copy_policy_snapshot)
    series_hash = _copy_series_hash(
        source_asset_version_id=source_asset_version_id,
        inventory_id=inventory_id,
        target_market=target_market,
        truth_package_id=truth_package_id,
        claim_decision_set_id=claim_decision_set_id,
        copy_policy_snapshot_hash=copy_policy_snapshot_hash,
        copy_origin="translated",
        translation_execution_mode=translation_execution_mode,
        translation_provider=translation_provider,
        translation_model=translation_model,
        translation_adapter_version=translation_adapter_version,
        mock_scenario=mock_scenario,
        mock_version=mock_version,
    )
    version_number = _next_version_number(session, ctx, series_hash)

    evidence_hash = _hash(
        {
            "content": content,
            "copy_origin": "translated",
            "truth_package_id": truth_package_id,
            "claim_decision_set_id": claim_decision_set_id,
            "source_text_hash": prepared.source_text_hash,
            "translation_provider": translation_provider,
            "translation_model": translation_model,
            "translation_adapter_version": translation_adapter_version,
            "translation_execution_mode": translation_execution_mode,
        }
    )

    copy_version = LocalizedCopyVersionRecord(
        id=new_id("lcv"),
        workspace_id=ctx.workspace_id,
        copy_series_hash=series_hash,
        version_number=version_number,
        source_asset_version_id=source_asset_version_id,
        inventory_id=inventory_id,
        truth_package_id=truth_package_id,
        truth_package_market_id=truth_package_market_id,
        claim_decision_set_id=claim_decision_set_id,
        target_market=target_market,
        market_policy_version_id=market_policy_version_id,
        content_json=content,
        copy_origin="translated",
        translation_execution_mode=translation_execution_mode,
        translation_provider=translation_provider,
        translation_model=translation_model,
        translation_adapter_version=translation_adapter_version,
        mock_scenario=mock_scenario,
        mock_version=mock_version,
        brand_localization_policy_version_id=brand_localization_policy_version_id,
        copy_policy_snapshot_json=dict(copy_policy_snapshot),
        copy_policy_snapshot_hash=copy_policy_snapshot_hash,
        # Translation/injection/validation are already complete by the
        # time this constructor runs, and `draft -> approved` is not an
        # allowed transition -- so the version starts at `needs_review`,
        # never `draft`.
        state="needs_review",
        evidence_hash=evidence_hash,
        created_at=now or datetime.now(timezone.utc),
    )
    session.add(copy_version)
    session.flush()
    return CopyCreationResult(copy_version=copy_version)


# ---------------------------------------------------------------------------
# Approval (append-only)
# ---------------------------------------------------------------------------


def approve_copy_version(
    session: Session,
    ctx: TenantContext,
    *,
    copy_version_id: str,
    actor_id: str,
    role: str,
    now: datetime | None = None,
) -> LocalizedCopyApprovalRecord:
    """Append an immutable approval by Owner or Member (spec §2F).

    The first approval advances the copy version's one mutable column
    (`state`) from `needs_review` to `approved` (every copy version is
    created directly at `needs_review`); a later approval
    of an already-`approved` version still appends its own record (e.g. a
    second required approver) without re-transitioning state. A
    `superseded` version can never be approved -- it is terminal. This
    never creates a `QualityReviewRecord`.
    """
    if role not in ROLES:
        raise CopyError("invalid_role")

    copy_version = session.scalar(
        select(LocalizedCopyVersionRecord).where(
            LocalizedCopyVersionRecord.workspace_id == ctx.workspace_id,
            LocalizedCopyVersionRecord.id == copy_version_id,
        )
    )
    if copy_version is None:
        raise CopyError("missing_copy_version")
    if copy_version.state == "superseded":
        raise CopyError("cannot_approve_superseded_copy_version")

    approval = LocalizedCopyApprovalRecord(
        id=new_id("lca"),
        workspace_id=ctx.workspace_id,
        copy_version_id=copy_version_id,
        actor_id=actor_id,
        role=role,
        created_at=now or datetime.now(timezone.utc),
    )
    session.add(approval)

    if copy_version.state in _ALLOWED_APPROVAL_SOURCE_STATES:
        copy_version.state = "approved"

    session.flush()
    return approval
