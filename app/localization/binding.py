"""Localization binding module (#24, spec §2I/§2J, §16.11/§16.12).

Spec: docs/superpowers/specs/2026-09-06-localization-truth-bounded-qc-design.md
§2I ("Binding, freshness, and precise downstream invalidation"), §2J
("app/localization/binding.py: publication, eligibility, stale
propagation").

This module owns `market_asset_bindings` and its append-only
`market_asset_binding_events` stream -- the durable relationship between a
source AssetVersion and its target-market representation.
`app.localization.worker.handle_finalize_item` calls `publish_binding()` in
the same transaction that creates the child AssetVersion, publication,
lineage, and successful Item transition. A failure therefore exposes none of
that publication state, matching the atomic finalization contract in §2G.

Four functions, the first three matching spec §2I verbatim:

- `publish_binding()`: "atomically supersedes the active binding for the
  same (workspace, source_asset_version, target_market, offer) and inserts
  the new `localized` or `source_reuse` binding." A new binding's
  `review_state` is the bound AssetVersion's current effective review
  (`app.generation.review.project_effective_review`), never inherited from
  whatever binding it supersedes. A localized child starts with no human
  QualityReview and no Winner verdict (spec §16.11), so its binding starts
  `pending_review` and not experiment-ready; a `source_reuse` binding
  follows the review the exact source version already has, without
  copying the review row.
- `assess_binding_eligibility()`: "requires active lifecycle, a currently
  Approved exact AssetVersion with no blocking QC failure, synchronous
  current Truth, eligible upstream source/Winner context, and compatible
  rights/evidence tier." This module has no QC-failure or Approved-review
  table of its own to consult beyond `review_state` (which
  `apply_asset_version_review()` keeps equal to the bound version's
  effective review); it defers exact-version source/Winner eligibility to
  `app.localization.eligibility.validate_source_for_batch` (unchanged,
  reused) and Truth currency to `app.localization.truth.
  assess_truth_freshness` (unchanged, reused) -- never redeclaring either
  check locally.
- `apply_dependency_change()`: "folds a durable dependency-change event
  into affected binding events/projections." A DataSourceVersion,
  ManualFactSetVersion, MarketSourceMappingVersion, ProductVersion,
  OfferVersion, or TruthResolutionPolicyVersion change is resolved to
  affected TruthPackages through `FactLibraryDependencyRecord`; a
  MarketPolicyVersion change is resolved through
  `FactMarketPolicyDependencyRecord` (spec §2I: "An idempotent Worker
  resolves exact affected TruthPackages through `fact_library_dependencies`
  or `fact_market_policy_dependencies`"). CredentialVersion rotation is
  deliberately *not* a tracked dependency kind here -- spec §16.12:
  "Credential rotation is a negative control and must not stale Truth" --
  so it is a documented no-op, never silently matched against an unrelated
  dependency table.
- `apply_asset_version_review()`: the user's exact-version review in the
  Assets area drives the binding (localization contract §3 step 11, spec
  §16.11, #128). `app.generation.review.create_review` calls it in the
  transaction that writes the review row; every active binding whose
  `bound_asset_version_id` is the reviewed version takes the version's
  effective review head as its `review_state`. There is no separate
  binding-approval command.

Every mutation is CAS (`UPDATE ... WHERE revision = expected`, raising
`app.localization.batch.StaleRevisionError` on conflict -- reused, not
redefined, per this task's brief) and every state change appends an
immutable `MarketAssetBindingEventRecord` with a monotonic per-binding
`sequence`. Stale propagation is idempotent: `apply_dependency_change()`
derives one deterministic `dependency_change_id` from
`(change_kind, changed_version_id)` and skips any binding that already
carries an event with that id (the `uq_mabe_dependency_change` partial
unique index is the durable backstop; this module checks first so a
redelivered change is a clean no-op rather than a constraint violation).
`ready_for_experiment` is never written directly by a caller -- it is
always derived as `review_state == 'approved' AND truth_freshness ==
'current'` at the moment of every CAS write in this module, matching spec
§1.10's `market_asset_bindings.ready_for_experiment` column comment
verbatim ("derived from review_state + truth_freshness + upstream
checks").

Historical data is never touched here: `apply_dependency_change()` reads
`TruthPackageRecord`/`TruthFactRecord`/`AssetVersionRecord` state nowhere
and writes to none of those tables -- only `market_asset_bindings` and
`market_asset_binding_events` (spec: "without rewriting TruthPackages,
AssetVersions, or completed Attempts").
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    FactLibraryDependencyRecord,
    FactMarketPolicyDependencyRecord,
    LocalizationBatchRecord,
    LocalizationItemRecord,
    LocalizationOutputPublicationRecord,
    MarketAssetBindingEventRecord,
    MarketAssetBindingRecord,
    new_id,
)
from ..generation.review import project_effective_review
from ..platform.tenancy import TenantContext
from .batch import StaleRevisionError
from .eligibility import validate_source_for_batch
from .truth import assess_truth_freshness

# `FactLibraryDependencyRecord.dependency_kind` vocabulary (spec §1.5,
# `ck_fld_dependency_kind`): fact-level kinds require `truth_fact_id`,
# package-level kinds require it NULL. `apply_dependency_change()` accepts
# either flavor -- a caller names the exact dependency kind that changed,
# never a table shape.
_LIBRARY_DEPENDENCY_KINDS = frozenset({
    "data_source_version",
    "manual_fact_set_version",
    "market_source_mapping_version",
    "product_version",
    "offer_version",
    "truth_resolution_policy_version",
})
_MARKET_POLICY_DEPENDENCY_KIND = "market_policy_version"

# Recognized but deliberately untracked: CredentialVersion rotation never
# staled Truth (spec §16.12 negative control). Any other unrecognized
# `change_kind` is a caller error, not silently treated as untracked.
_UNTRACKED_DEPENDENCY_KINDS = frozenset({"credential_version"})

# The bound AssetVersion's effective review value (spec §0.3,
# `project_effective_review`) -> the binding's `review_state`. A `cleared`
# head projects to None ("no effective review"), as does a version nobody
# has reviewed; both leave the binding waiting for review. Any value outside
# this map fails closed to `pending_review` as well.
_REVIEW_STATE_BY_EFFECTIVE_REVIEW: Mapping[str | None, str] = {
    "approved": "approved",
    "rejected": "rejected",
    "needs_changes": "needs_changes",
    None: "pending_review",
}

# Event type appended when a review moves a binding's `review_state`.
REVIEW_EVENT_TYPE = "review_state_changed"
# Namespace of the per-review `dependency_change_id` (`uq_mabe_dependency_
# change` then lets one review move one binding at most once).
_REVIEW_CHANGE_KIND = "quality_review"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BindingError(RuntimeError):
    """Base for every error this module raises."""


class BindingValidationError(BindingError):
    """A `publish_binding()`/`apply_dependency_change()` input could not be
    resolved against already-persisted state."""


class BindingNotFoundError(BindingError):
    def __init__(self, binding_id: str) -> None:
        self.binding_id = binding_id
        super().__init__(f"market_asset_binding {binding_id} not found")


# ---------------------------------------------------------------------------
# Value objects -- results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishBindingResult:
    binding_id: str
    binding_kind: str  # 'localized' | 'source_reuse'
    bound_asset_version_id: str
    superseded_binding_id: str | None
    review_state: str
    truth_freshness: str
    ready_for_experiment: bool
    replayed: bool


@dataclass(frozen=True)
class EligibilityReport:
    binding_id: str
    active: bool
    review_state: str
    source_valid: bool
    truth_current: bool
    eligible_for_experiment: bool
    findings: tuple[str, ...]


@dataclass(frozen=True)
class DependencyChangeResult:
    change_kind: str
    changed_version_id: str
    dependency_change_id: str
    tracked: bool
    staled_binding_ids: tuple[str, ...]
    skipped_binding_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReviewApplicationResult:
    asset_version_id: str
    review_id: str
    review_state: str
    changed_binding_ids: tuple[str, ...]
    unchanged_binding_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Hashing / snapshot helpers
# ---------------------------------------------------------------------------


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _dependency_change_id(change_kind: str, changed_version_id: str) -> str:
    """Deterministic identity for one durable dependency-change event.

    Every binding affected by the *same* upstream change (same
    `change_kind`/`changed_version_id`) receives an event carrying this
    same id, so a redelivered change is recognized per-binding via
    `uq_mabe_dependency_change` without the caller supplying its own
    idempotency token (spec §2I: "Event identity includes the source
    event ID, dependency kind/version, new state, and binding ID" -- the
    binding ID half of that identity is the event's own `binding_id`
    column; this hash carries the dependency-kind/version half).
    """
    return _hash({"change_kind": change_kind, "changed_version_id": changed_version_id})


def _snapshot(binding: MarketAssetBindingRecord) -> dict:
    return {
        "review_state": binding.review_state,
        "truth_freshness": binding.truth_freshness,
        "ready_for_experiment": binding.ready_for_experiment,
        "active": binding.active,
        "revision": binding.revision,
    }


def _ready_for_experiment(review_state: str, truth_freshness: str) -> bool:
    """The one readiness rule every write in this module applies (spec
    §1.10 column comment): Approved exact version and current Truth."""
    return review_state == "approved" and truth_freshness == "current"


def _review_state_of(session: Session, ctx: TenantContext, asset_version_id: str) -> str:
    """The `review_state` a binding of `asset_version_id` must carry: the
    version's effective review head, mapped by
    `_REVIEW_STATE_BY_EFFECTIVE_REVIEW`."""
    effective = project_effective_review(session, ctx, asset_version_id)
    return _REVIEW_STATE_BY_EFFECTIVE_REVIEW.get(effective, "pending_review")


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _load_item(
    session: Session, ctx: TenantContext, batch_id: str, item_id: str
) -> LocalizationItemRecord:
    item = session.scalar(
        select(LocalizationItemRecord).where(
            LocalizationItemRecord.workspace_id == ctx.workspace_id,
            LocalizationItemRecord.id == item_id,
            LocalizationItemRecord.batch_id == batch_id,
        )
    )
    if item is None:
        raise BindingValidationError(f"item {item_id} not found in batch {batch_id}")
    return item


def _load_batch(session: Session, ctx: TenantContext, batch_id: str) -> LocalizationBatchRecord:
    batch = session.scalar(
        select(LocalizationBatchRecord).where(
            LocalizationBatchRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchRecord.id == batch_id,
        )
    )
    if batch is None:
        raise BindingValidationError(f"batch {batch_id} not found")
    return batch


def _load_binding(session: Session, ctx: TenantContext, binding_id: str) -> MarketAssetBindingRecord:
    binding = session.scalar(
        select(MarketAssetBindingRecord).where(
            MarketAssetBindingRecord.workspace_id == ctx.workspace_id,
            MarketAssetBindingRecord.id == binding_id,
        )
    )
    if binding is None:
        raise BindingNotFoundError(binding_id)
    return binding


def _next_sequence(session: Session, ctx: TenantContext, binding_id: str) -> int:
    last = session.scalar(
        select(func.max(MarketAssetBindingEventRecord.sequence)).where(
            MarketAssetBindingEventRecord.workspace_id == ctx.workspace_id,
            MarketAssetBindingEventRecord.binding_id == binding_id,
        )
    )
    return (last or 0) + 1


def _append_event(
    session: Session,
    ctx: TenantContext,
    *,
    binding: MarketAssetBindingRecord,
    event_type: str,
    dependency_change_id: str | None,
    previous_state: Mapping[str, object],
    new_state: Mapping[str, object],
    actor_id: str,
    now: datetime,
) -> MarketAssetBindingEventRecord:
    event = MarketAssetBindingEventRecord(
        id=new_id("mabe"),
        workspace_id=ctx.workspace_id,
        binding_id=binding.id,
        sequence=_next_sequence(session, ctx, binding.id),
        event_type=event_type,
        dependency_change_id=dependency_change_id,
        previous_state_json=dict(previous_state),
        new_state_json=dict(new_state),
        actor_id=actor_id,
        created_at=now,
    )
    session.add(event)
    session.flush()
    return event


# ---------------------------------------------------------------------------
# publish_binding
# ---------------------------------------------------------------------------


def publish_binding(
    session: Session,
    ctx: TenantContext,
    *,
    item_id: str,
    batch_id: str,
    publication_id: str | None,
    actor_id: str,
    now: datetime | None = None,
) -> PublishBindingResult:
    """Spec §2I: "atomically supersedes the active binding for the same
    (workspace, source_asset_version, target_market, offer) and inserts
    the new `localized` or `source_reuse` binding."

    `publication_id` distinguishes the two `binding_kind`s this function
    produces: a non-null id is a `localized` binding pointing at the
    `LocalizationOutputPublicationRecord`'s child AssetVersion (spec
    §16.11: "successful publication creates `origin_kind=localized`");
    `None` is a `source_reuse` binding that references the exact existing
    source AssetVersion without creating a new one or cloning its review
    row (spec §16.11: "`source_reuse` references the exact existing
    reviewed source version and does not clone its review row"). Either
    way the new binding starts `truth_freshness='current'` with the bound
    version's current effective review as its `review_state` -- never
    inherited from whatever binding it supersedes. A localized child has
    no review yet, so it starts `pending_review` and is not
    experiment-ready (spec §16.11); a `source_reuse` binding of an Approved
    source starts `approved` and experiment-ready, reading the source's
    review rather than copying it.

    Idempotent per publication: a second call with the same
    (non-null) `publication_id` returns the already-published binding
    rather than violating `uq_mab_publication` or superseding it again.
    """
    now = now or datetime.now(timezone.utc)
    item = _load_item(session, ctx, batch_id, item_id)
    batch = _load_batch(session, ctx, batch_id)

    if publication_id is not None:
        already = session.scalar(
            select(MarketAssetBindingRecord).where(
                MarketAssetBindingRecord.workspace_id == ctx.workspace_id,
                MarketAssetBindingRecord.publication_id == publication_id,
            )
        )
        if already is not None:
            return PublishBindingResult(
                binding_id=already.id,
                binding_kind=already.binding_kind,
                bound_asset_version_id=already.bound_asset_version_id,
                superseded_binding_id=None,
                review_state=already.review_state,
                truth_freshness=already.truth_freshness,
                ready_for_experiment=already.ready_for_experiment,
                replayed=True,
            )

        publication = session.scalar(
            select(LocalizationOutputPublicationRecord).where(
                LocalizationOutputPublicationRecord.workspace_id == ctx.workspace_id,
                LocalizationOutputPublicationRecord.id == publication_id,
            )
        )
        if publication is None:
            raise BindingValidationError(f"publication {publication_id} not found")
        if publication.item_id != item_id or publication.batch_id != batch_id:
            raise BindingValidationError(
                f"publication {publication_id} does not belong to item {item_id} "
                f"in batch {batch_id}"
            )
        bound_asset_version_id = publication.asset_version_id
        binding_kind = "localized"
    else:
        bound_asset_version_id = item.source_asset_version_id
        binding_kind = "source_reuse"

    existing_active = session.scalar(
        select(MarketAssetBindingRecord).where(
            MarketAssetBindingRecord.workspace_id == ctx.workspace_id,
            MarketAssetBindingRecord.source_asset_version_id == item.source_asset_version_id,
            MarketAssetBindingRecord.target_market == item.target_market,
            MarketAssetBindingRecord.offer_version_id == batch.offer_version_id,
            MarketAssetBindingRecord.active.is_(True),
        )
    )

    superseded_binding_id: str | None = None
    if existing_active is not None:
        previous_state = _snapshot(existing_active)
        result = session.execute(
            update(MarketAssetBindingRecord)
            .where(
                MarketAssetBindingRecord.id == existing_active.id,
                MarketAssetBindingRecord.workspace_id == ctx.workspace_id,
                MarketAssetBindingRecord.revision == existing_active.revision,
            )
            .values(active=False, revision=existing_active.revision + 1, updated_at=now)
        )
        if result.rowcount != 1:
            raise StaleRevisionError(
                "market_asset_binding", existing_active.id, existing_active.revision
            )
        session.flush()
        session.refresh(existing_active)
        _append_event(
            session, ctx, binding=existing_active, event_type="superseded",
            dependency_change_id=None, previous_state=previous_state,
            new_state=_snapshot(existing_active), actor_id=actor_id, now=now,
        )
        superseded_binding_id = existing_active.id

    review_state = _review_state_of(session, ctx, bound_asset_version_id)
    binding = MarketAssetBindingRecord(
        id=new_id("mab"),
        workspace_id=ctx.workspace_id,
        source_asset_version_id=item.source_asset_version_id,
        target_market=item.target_market,
        offer_version_id=batch.offer_version_id,
        bound_asset_version_id=bound_asset_version_id,
        binding_kind=binding_kind,
        publication_id=publication_id,
        review_state=review_state,
        truth_freshness="current",
        ready_for_experiment=_ready_for_experiment(review_state, "current"),
        active=True,
        batch_id=batch_id,
        item_id=item_id,
        truth_package_id=item.truth_package_id,
        market_policy_version_id=item.market_policy_version_id,
        evidence_tier=batch.evidence_tier,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    session.add(binding)
    session.flush()

    _append_event(
        session, ctx, binding=binding, event_type="published",
        dependency_change_id=None, previous_state={},
        new_state=_snapshot(binding), actor_id=actor_id, now=now,
    )

    return PublishBindingResult(
        binding_id=binding.id,
        binding_kind=binding_kind,
        bound_asset_version_id=bound_asset_version_id,
        superseded_binding_id=superseded_binding_id,
        review_state=binding.review_state,
        truth_freshness=binding.truth_freshness,
        ready_for_experiment=binding.ready_for_experiment,
        replayed=False,
    )


# ---------------------------------------------------------------------------
# assess_binding_eligibility
# ---------------------------------------------------------------------------


def assess_binding_eligibility(
    session: Session,
    ctx: TenantContext,
    *,
    binding_id: str,
    at: datetime | None = None,
) -> EligibilityReport:
    """Spec §2I: "requires active lifecycle, a currently Approved exact
    AssetVersion with no blocking QC failure, synchronous current Truth,
    eligible upstream source/Winner context, and compatible rights/
    evidence tier."

    Read-only: never mutates the binding. Source/Winner eligibility is
    delegated to `app.localization.eligibility.validate_source_for_batch`
    (the sole authoritative gate, spec §2F/§16.1) and Truth currency to
    `app.localization.truth.assess_truth_freshness` -- this function never
    re-derives either check itself, only folds their reports together
    with the binding's own persisted `review_state`/`active` columns.
    """
    now = at or datetime.now(timezone.utc)
    binding = _load_binding(session, ctx, binding_id)
    item = _load_item(session, ctx, binding.batch_id, binding.item_id)

    report = validate_source_for_batch(
        session, ctx, binding.source_asset_version_id, item.decision_id,
        requested_evidence_tier=binding.evidence_tier,
    )
    freshness = assess_truth_freshness(session, ctx, binding.truth_package_id, at=now)

    findings: list[str] = list(report.findings) + list(freshness.reasons)
    if not binding.active:
        findings.append("binding_inactive")
    if binding.review_state != "approved":
        findings.append(f"review_state:{binding.review_state}")

    eligible = (
        binding.active
        and binding.review_state == "approved"
        and freshness.is_current
        and report.valid
    )

    return EligibilityReport(
        binding_id=binding.id,
        active=binding.active,
        review_state=binding.review_state,
        source_valid=report.valid,
        truth_current=freshness.is_current,
        eligible_for_experiment=eligible,
        findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# apply_dependency_change
# ---------------------------------------------------------------------------


def apply_dependency_change(
    session: Session,
    ctx: TenantContext,
    *,
    change_kind: str,
    changed_version_id: str,
    actor_id: str,
    now: datetime | None = None,
) -> DependencyChangeResult:
    """Spec §2I: "An idempotent Worker resolves exact affected
    TruthPackages through `fact_library_dependencies` or
    `fact_market_policy_dependencies`, appends deduplicated
    `market_asset_binding_events`, and CAS-updates the rebuildable binding
    projection."

    `change_kind` names the exact upstream dependency that changed --
    `data_source_version`, `manual_fact_set_version`,
    `market_source_mapping_version`, `product_version`, `offer_version`,
    or `truth_resolution_policy_version` (all resolved through
    `FactLibraryDependencyRecord.dependency_kind`), or
    `market_policy_version` (resolved through
    `FactMarketPolicyDependencyRecord`). `credential_version` is a
    recognized but deliberately untracked kind (spec §16.12: "Credential
    rotation is a negative control and must not stale Truth") -- it
    returns `tracked=False` and staless nothing, rather than silently
    matching zero rows in an unrelated table. Any other `change_kind` is
    a caller error.

    Only active bindings are staled -- an already-superseded binding's
    historical `truth_freshness` is not rewritten (spec: "without
    rewriting TruthPackages, AssetVersions, or completed Attempts", which
    applies equally to a binding's own prior lifecycle once it is no
    longer active).

    Idempotent: every binding this call would affect receives an event
    carrying the same deterministic `dependency_change_id` (derived from
    `change_kind`/`changed_version_id`, spec: "Event identity includes
    the source event ID, dependency kind/version, ... and binding ID").
    A binding that already carries an event with that id is skipped --
    its CAS revision is not bumped and no duplicate event is appended.
    """
    now = now or datetime.now(timezone.utc)
    dependency_change_id = _dependency_change_id(change_kind, changed_version_id)

    if change_kind in _UNTRACKED_DEPENDENCY_KINDS:
        return DependencyChangeResult(
            change_kind=change_kind, changed_version_id=changed_version_id,
            dependency_change_id=dependency_change_id, tracked=False,
            staled_binding_ids=(), skipped_binding_ids=(),
        )

    if change_kind in _LIBRARY_DEPENDENCY_KINDS:
        truth_package_ids = set(
            session.scalars(
                select(FactLibraryDependencyRecord.truth_package_id).where(
                    FactLibraryDependencyRecord.workspace_id == ctx.workspace_id,
                    FactLibraryDependencyRecord.dependency_kind == change_kind,
                    FactLibraryDependencyRecord.dependency_version_id == changed_version_id,
                )
            ).all()
        )
    elif change_kind == _MARKET_POLICY_DEPENDENCY_KIND:
        truth_package_ids = set(
            session.scalars(
                select(FactMarketPolicyDependencyRecord.truth_package_id).where(
                    FactMarketPolicyDependencyRecord.workspace_id == ctx.workspace_id,
                    FactMarketPolicyDependencyRecord.market_policy_version_id
                    == changed_version_id,
                )
            ).all()
        )
    else:
        raise BindingValidationError(f"unknown dependency change_kind {change_kind!r}")

    if not truth_package_ids:
        return DependencyChangeResult(
            change_kind=change_kind, changed_version_id=changed_version_id,
            dependency_change_id=dependency_change_id, tracked=True,
            staled_binding_ids=(), skipped_binding_ids=(),
        )

    bindings: Sequence[MarketAssetBindingRecord] = session.scalars(
        select(MarketAssetBindingRecord).where(
            MarketAssetBindingRecord.workspace_id == ctx.workspace_id,
            MarketAssetBindingRecord.truth_package_id.in_(truth_package_ids),
            MarketAssetBindingRecord.active.is_(True),
        )
    ).all()

    staled: list[str] = []
    skipped: list[str] = []
    for binding in bindings:
        already = session.scalar(
            select(MarketAssetBindingEventRecord).where(
                MarketAssetBindingEventRecord.workspace_id == ctx.workspace_id,
                MarketAssetBindingEventRecord.binding_id == binding.id,
                MarketAssetBindingEventRecord.dependency_change_id == dependency_change_id,
            )
        )
        if already is not None:
            skipped.append(binding.id)
            continue

        previous_state = _snapshot(binding)
        result = session.execute(
            update(MarketAssetBindingRecord)
            .where(
                MarketAssetBindingRecord.id == binding.id,
                MarketAssetBindingRecord.workspace_id == ctx.workspace_id,
                MarketAssetBindingRecord.revision == binding.revision,
            )
            .values(
                truth_freshness="stale", ready_for_experiment=False,
                revision=binding.revision + 1, updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise StaleRevisionError("market_asset_binding", binding.id, binding.revision)
        session.flush()
        session.refresh(binding)

        _append_event(
            session, ctx, binding=binding, event_type="stale_truth",
            dependency_change_id=dependency_change_id, previous_state=previous_state,
            new_state=_snapshot(binding), actor_id=actor_id, now=now,
        )
        staled.append(binding.id)

    return DependencyChangeResult(
        change_kind=change_kind, changed_version_id=changed_version_id,
        dependency_change_id=dependency_change_id, tracked=True,
        staled_binding_ids=tuple(staled), skipped_binding_ids=tuple(skipped),
    )


# ---------------------------------------------------------------------------
# apply_asset_version_review
# ---------------------------------------------------------------------------


def apply_asset_version_review(
    session: Session,
    ctx: TenantContext,
    *,
    asset_version_id: str,
    review_id: str,
    actor_id: str,
    now: datetime | None = None,
) -> ReviewApplicationResult:
    """Move every active binding of `asset_version_id` to its effective review.

    Localization contract §3 step 11: the user reviews the exact
    AssetVersion in the Assets area, and an Approved review makes the
    target-market binding `ready_for_experiment`. Spec §16.11: approval is
    exact-version, and a later rejection removes readiness without deleting
    history. `app.generation.review.create_review` calls this in the
    transaction that writes review `review_id`, after the row is flushed,
    so the effective head read here already includes it -- whether the
    review is the version's first or supersedes an earlier one.

    Only active bindings whose `bound_asset_version_id` is the reviewed
    version move: a localized binding through its published child, a
    `source_reuse` binding through the source version it references. A
    superseded binding keeps the review state it ended with. Each binding
    whose `review_state` or derived readiness changes is CAS-updated and
    gets one `review_state_changed` event; `ready_for_experiment` is
    re-derived with the module's one rule (Approved and current Truth), so
    an approval never revives a binding whose Truth went stale. A binding
    already in the target state is left alone, without an event.

    The event carries a `dependency_change_id` derived from the review id,
    so `uq_mabe_dependency_change` lets a review move a binding at most
    once; the rows are locked first so a concurrent dependency change
    serializes behind the review instead of failing its CAS.
    """
    now = now or datetime.now(timezone.utc)
    review_state = _review_state_of(session, ctx, asset_version_id)
    dependency_change_id = _dependency_change_id(_REVIEW_CHANGE_KIND, review_id)

    bindings: Sequence[MarketAssetBindingRecord] = session.scalars(
        select(MarketAssetBindingRecord)
        .where(
            MarketAssetBindingRecord.workspace_id == ctx.workspace_id,
            MarketAssetBindingRecord.bound_asset_version_id == asset_version_id,
            MarketAssetBindingRecord.active.is_(True),
        )
        .order_by(MarketAssetBindingRecord.id)
        .with_for_update()
    ).all()

    changed: list[str] = []
    unchanged: list[str] = []
    for binding in bindings:
        ready = _ready_for_experiment(review_state, binding.truth_freshness)
        if binding.review_state == review_state and binding.ready_for_experiment == ready:
            unchanged.append(binding.id)
            continue

        previous_state = _snapshot(binding)
        result = session.execute(
            update(MarketAssetBindingRecord)
            .where(
                MarketAssetBindingRecord.id == binding.id,
                MarketAssetBindingRecord.workspace_id == ctx.workspace_id,
                MarketAssetBindingRecord.revision == binding.revision,
            )
            .values(
                review_state=review_state, ready_for_experiment=ready,
                revision=binding.revision + 1, updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise StaleRevisionError("market_asset_binding", binding.id, binding.revision)
        session.flush()
        session.refresh(binding)

        _append_event(
            session, ctx, binding=binding, event_type=REVIEW_EVENT_TYPE,
            dependency_change_id=dependency_change_id, previous_state=previous_state,
            new_state={**_snapshot(binding), "quality_review_id": review_id},
            actor_id=actor_id, now=now,
        )
        changed.append(binding.id)

    return ReviewApplicationResult(
        asset_version_id=asset_version_id, review_id=review_id,
        review_state=review_state, changed_binding_ids=tuple(changed),
        unchanged_binding_ids=tuple(unchanged),
    )
