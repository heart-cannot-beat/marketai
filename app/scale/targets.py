"""ScaleTargetVersion lifecycle: context resolution, publication preview,
publish, withdrawal preview, withdraw, list, get (spec §10, §21).

Issue #87 Task 2.

This module owns the deep-module boundary around ``ScaleTargetVersionRecord``
economic targets -- Owner-published immutable CPA/ROAS/spend floors that
participate in Scale evaluations. Distinct from ``app.scale.delivery_targets``
(``ScaleDeliveryTargetRecord`` -- Meta delivery slot bindings).

Publication and withdrawal follow the two-phase preview->confirm pattern
established by ``app.scale.activation_preview``: create an expiring preview
(validates, computes impact), then consume it atomically. Both confirm
commands are Owner-only; MFA is checked only at login (ADR 0015).

One exact context has at most one active Target (PostgreSQL's partial unique
index ``ix_stgt_active_target``; SQLite relies on the checks here alone).
Publishing into a context that already has one replaces it in the same
transaction (#398): the old Target is withdrawn with a ``withdraw`` event
whose reason is ``replaced_by:<new Target id>``, then the new one is
published. The preview's impact hash names the Target it replaces, and
publish recomputes it, so a context that changed since the preview is
refused with ``preview_hash_mismatch``.

Both preview and publish check a Target's minimum economic spend against
the platform floor (#134, §9.2: the platform threshold is a minimum, and an
Owner may only raise it). The floor is the one the Policy in force for the
caller's mode sets for the Target's currency -- the Policy a live run would
freeze (``app.scale.live_trigger.eligible_live_policy``), read through the
evaluator's own lookup (``app.scale.evidence.policy_spend_floor``). A
Target with only a ROAS must fill the minimum; any Target that fills it
may not go below the floor. Targets published before the check stay as
they are: the evaluator already takes the largest of the floor, the
Target's minimum and 3x target CPA, and lists and contexts report the
floor so the page can say which Targets fall below it.

Publishing and withdrawing each enqueue one ``scale_target_change`` Job in
the same transaction, keyed by the lifecycle event (#113): the Worker
supersedes the Advice the change made inapplicable and re-evaluates
Episodes whose evidence is still fresh (``app.scale.target_change``).
A replayed command returns before it, so it enqueues nothing twice.

Currency is not a column on ``ScaleEnrollmentRecord`` -- it lives on
``ScaleEpisodeRecord`` (one row per observation interval, spec §1.5).
``resolve_target_contexts`` therefore joins each distinct enrollment context
through its most recent Episode to discover the currency in force for that
context, rather than reading a non-existent ``ScaleEnrollmentRecord.currency``
column.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id
from app.platform.action_eligibility import ActionEligibility, compute_actions
from app.platform.tenancy import TenantContext

from .command_journal import claim_command, command_request_fingerprint
from .evidence import policy_spend_floor
from .models import (
    ScaleAdviceThreadRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeRecord,
    ScalePolicyVersionRecord,
    ScaleTargetLifecycleEventRecord,
    ScaleTargetPublicationPreviewRecord,
    ScaleTargetVersionRecord,
    ScaleTargetWithdrawalPreviewRecord,
)

__all__ = [
    "PlatformSpendFloor",
    "TargetLifecycleError",
    "TargetNotFoundError",
    "TargetPreviewExpiredError",
    "TargetPreviewConsumedError",
    "applicable_policy",
    "create_publication_preview",
    "publish_target",
    "create_withdrawal_preview",
    "withdraw_target",
    "list_targets",
    "get_target",
    "platform_spend_floor",
    "resolve_target_contexts",
    "target_below_platform_floor",
    "target_actions",
]

_PREVIEW_TTL = timedelta(minutes=15)

_TARGET_ACTION_CODES = ["withdraw"]

_ACTIVE_ENROLLMENT_LIFECYCLES = ("active", "paused")


class TargetLifecycleError(RuntimeError):
    """Base error for every Target lifecycle violation. ``code`` is the
    stable machine-readable reason (matched by callers/tests); ``detail`` is
    optional human context."""

    def __init__(self, code: str, detail: str = "", *, params: dict | None = None) -> None:
        self.code = code
        self.detail = detail
        #: What the code's copy interpolates (``app.library.api``'s
        #: problem ``params``), e.g. the floor a minimum fell below.
        self.params = params
        super().__init__(f"{code}: {detail}" if detail else code)


class TargetNotFoundError(TargetLifecycleError):
    def __init__(self, target_id: str) -> None:
        super().__init__("target_not_found", target_id)


class TargetPreviewExpiredError(TargetLifecycleError):
    def __init__(self, preview_id: str) -> None:
        super().__init__("preview_expired", preview_id)


class TargetPreviewConsumedError(TargetLifecycleError):
    def __init__(self, preview_id: str) -> None:
        super().__init__("preview_already_consumed", preview_id)


def _is_owner(context: TenantContext) -> bool:
    return context.role in ("owner", "system")


def _require_owner(context: TenantContext) -> None:
    if not _is_owner(context):
        raise TargetLifecycleError("owner_required")


def _for_update(session: Session, stmt):
    """PostgreSQL row locking. SQLite (tests, bounded local path) has no
    ``FOR UPDATE`` and SQLAlchemy leaves it out there, but the statement
    keeps the lock, so the SQLite grant guard (#59) sees it."""
    return stmt.with_for_update()


def _scenario_scope_key(
    session: Session,
    context: TenantContext,
    product_version_id: str,
    offer_version_id: str,
    market_code: str,
) -> str | None:
    """The ``scenario_scope_key`` a Target command claims its journal row
    under (#56).

    ``claim_command`` requires an explicit one under ``controlled_mock``. A
    Target is Workspace-wide and has no scenario of its own, so, like the
    other Scale commands that read it from the row they act on, this reads
    the scenario of the context's Enrollment in this mode (the most recent
    one, should the context have Enrollments under several scenarios). The
    read takes no lock, so it may run before the claim (``command_journal``
    module docstring). A context with no Enrollment in ``controlled_mock``
    has no controlled-Mock scenario to journal under and is refused.

    Every other mode returns ``None``: the claim's no-scenario marker, with
    no Enrollment required.
    """
    if context.meta_mode != "controlled_mock":
        return None
    scenario_scope_key = session.execute(
        select(ScaleEnrollmentRecord.scenario_scope_key)
        .filter_by(
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            product_version_id=product_version_id,
            offer_version_id=offer_version_id,
            market_code=market_code,
        )
        .order_by(ScaleEnrollmentRecord.created_at.desc(), ScaleEnrollmentRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if scenario_scope_key is None:
        raise TargetLifecycleError(
            "no_controlled_mock_enrollment",
            f"{product_version_id}/{offer_version_id}/{market_code}",
        )
    return scenario_scope_key


def _amount(value: Decimal | None) -> str | None:
    """One spelling per amount: the request's ``20.00`` and the column's
    ``20.000000000000`` must hash alike when publish recomputes the hash."""
    return format(value.normalize(), "f") if value is not None else None


def _impact_hash(
    product_version_id: str,
    offer_version_id: str,
    market_code: str,
    currency: str,
    target_cpa: Decimal | None,
    breakeven_roas: Decimal | None,
    minimum_economic_spend: Decimal | None,
    affected_count: int,
    replaced_target_ids: list[str] | tuple[str, ...] = (),
) -> str:
    payload = json.dumps(
        {
            "product_version_id": product_version_id,
            "offer_version_id": offer_version_id,
            "market_code": market_code,
            "currency": currency,
            "target_cpa": _amount(target_cpa),
            "breakeven_roas": _amount(breakeven_roas),
            "minimum_economic_spend": _amount(minimum_economic_spend),
            "affected_enrollment_count": affected_count,
            "replaced_target_version_ids": sorted(replaced_target_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _count_affected_enrollments(
    session: Session,
    workspace_id: str,
    product_version_id: str,
    offer_version_id: str,
    market_code: str,
) -> int:
    return session.execute(
        select(func.count()).select_from(ScaleEnrollmentRecord).filter(
            ScaleEnrollmentRecord.workspace_id == workspace_id,
            ScaleEnrollmentRecord.product_version_id == product_version_id,
            ScaleEnrollmentRecord.offer_version_id == offer_version_id,
            ScaleEnrollmentRecord.market_code == market_code,
            ScaleEnrollmentRecord.lifecycle.in_(_ACTIVE_ENROLLMENT_LIFECYCLES),
        )
    ).scalar_one()


def _active_targets(
    session: Session,
    workspace_id: str,
    product_version_id: str,
    offer_version_id: str,
    market_code: str,
    currency: str,
    *,
    lock: bool = False,
) -> list[ScaleTargetVersionRecord]:
    """The active Targets of one exact context, keyed exactly like
    ``ix_stgt_active_target``. At most one on PostgreSQL; a SQLite database
    written before #398 may hold more, and the next publication replaces
    them all."""
    stmt = select(ScaleTargetVersionRecord).filter_by(
        workspace_id=workspace_id,
        product_version_id=product_version_id,
        offer_version_id=offer_version_id,
        market_code=market_code,
        currency=currency,
        lifecycle_state="active",
    ).order_by(ScaleTargetVersionRecord.id)
    if lock:
        stmt = _for_update(session, stmt)
    return list(session.execute(stmt).scalars().all())


#: Why a currency has no platform floor to check against (#134). The first
#: is ``app.scale.live_trigger``'s skip reason for the same lookup; the
#: second is the evaluator's reason for an economic signal it cannot form.
NO_ELIGIBLE_POLICY = "no_eligible_policy"
SPEND_FLOOR_CURRENCY_UNSUPPORTED = "spend_floor_currency_unsupported"


@dataclass(frozen=True)
class PlatformSpendFloor:
    """The platform economic floor one currency is held to under the Policy
    in force: ``amount`` (minimum total spend of a 7-day window), or
    ``None`` with the ``unavailable_reason``."""

    currency: str
    amount: Decimal | None
    policy_version_id: str | None
    unavailable_reason: str | None

    @property
    def amount_text(self) -> str | None:
        """``amount`` as the API spells it (``"16000"``, not ``"1.6E+4"``)."""
        return _amount(self.amount)


def applicable_policy(session: Session, context: TenantContext) -> ScalePolicyVersionRecord | None:
    """The Policy a live run in ``context``'s mode freezes now, or ``None``
    when none permits that mode (``controlled_mock``: explicit evaluation
    names its Policy itself)."""
    # Imported here: ``app.scale.live_trigger`` imports this module.
    from .live_trigger import eligible_live_policy

    return eligible_live_policy(session, context.meta_mode)


def platform_spend_floor(policy: ScalePolicyVersionRecord | None, currency: str) -> PlatformSpendFloor:
    """``policy``'s floor in ``currency``, as the evaluator reads it."""
    if policy is None:
        return PlatformSpendFloor(currency, None, None, NO_ELIGIBLE_POLICY)
    amount = policy_spend_floor(policy, currency)
    if amount is None:
        return PlatformSpendFloor(currency, None, policy.id, SPEND_FLOOR_CURRENCY_UNSUPPORTED)
    return PlatformSpendFloor(currency, amount, policy.id, None)


def _has_minimum(minimum_economic_spend: Decimal | None) -> bool:
    """A minimum of zero is the same as none (``app.scale.schemas``)."""
    return minimum_economic_spend is not None and minimum_economic_spend > 0


def _check_minimum_economic_spend(
    floor: PlatformSpendFloor,
    *,
    target_cpa: Decimal | None,
    breakeven_roas: Decimal | None,
    minimum_economic_spend: Decimal | None,
) -> None:
    """#134, §9.2: a Target with only a ROAS fills a minimum economic
    spend, and a Target that fills one may not go below the platform floor
    in its currency; equal to the floor passes. A Target with a CPA and no
    minimum needs no floor here: the evaluator holds it to the larger of
    the floor and 3x its CPA."""
    has_minimum = _has_minimum(minimum_economic_spend)
    roas_only = target_cpa is None and breakeven_roas is not None
    if not (has_minimum or roas_only):
        return
    params = {"currency": floor.currency}
    if floor.unavailable_reason == NO_ELIGIBLE_POLICY:
        raise TargetLifecycleError(
            "no_eligible_policy", "no published Policy permits this mode", params=params,
        )
    if floor.amount is None:
        raise TargetLifecycleError(
            "spend_floor_currency_unsupported",
            f"Policy {floor.policy_version_id} has no economic floor in {floor.currency}",
            params=params,
        )
    params["floor"] = floor.amount_text
    if not has_minimum:
        raise TargetLifecycleError(
            "minimum_economic_spend_required",
            f"a ROAS-only Target needs a minimum economic spend of at least {params['floor']}",
            params=params,
        )
    if minimum_economic_spend < floor.amount:
        raise TargetLifecycleError(
            "minimum_economic_spend_below_platform_floor",
            f"{_amount(minimum_economic_spend)} is below the platform floor {params['floor']}",
            params=params,
        )


def target_below_platform_floor(
    target: ScaleTargetVersionRecord, floor: PlatformSpendFloor,
) -> bool:
    """Whether an active Target misses the publication check (#134) against
    a floor it can be compared with: only a ROAS and no minimum, or a
    minimum below the floor. Its evaluations hold it to the platform floor
    instead (the evaluator's largest-of rule); the Owner may republish it.
    A Target that is not active is not in force and never is."""
    if target.lifecycle_state != "active" or floor.amount is None:
        return False
    if _has_minimum(target.minimum_economic_spend):
        return target.minimum_economic_spend < floor.amount
    return target.target_cpa is None and target.breakeven_roas is not None


def resolve_target_contexts(session: Session, context: TenantContext) -> list[dict]:
    """Every distinct (ProductVersion, OfferVersion, market) context with a
    currently active or paused Enrollment in this Workspace, with the
    currency observed on that context's most recent Episode and whether an
    active Target already exists for it.

    A context with no Episode yet (no currency observed) is omitted --
    publication requires a caller-supplied currency, but discovery here is
    only meaningful once at least one Episode has established what currency
    that context actually delivers in.

    Each context also carries the platform floor a Target published into it
    is checked against (#134): ``platform_spend_floor`` in its currency, or
    ``None`` with ``platform_spend_floor_unavailable_reason``.
    """
    policy = applicable_policy(session, context)
    rows = session.execute(
        select(
            ScaleEnrollmentRecord.product_version_id,
            ScaleEnrollmentRecord.offer_version_id,
            ScaleEnrollmentRecord.market_code,
        ).filter(
            ScaleEnrollmentRecord.workspace_id == context.workspace_id,
            ScaleEnrollmentRecord.lifecycle.in_(_ACTIVE_ENROLLMENT_LIFECYCLES),
        ).distinct()
    ).all()

    contexts: list[dict] = []
    for row in rows:
        currency = session.execute(
            select(ScaleEpisodeRecord.currency)
            .filter(
                ScaleEpisodeRecord.workspace_id == context.workspace_id,
                ScaleEpisodeRecord.product_version_id == row.product_version_id,
                ScaleEpisodeRecord.offer_version_id == row.offer_version_id,
                ScaleEpisodeRecord.market_code == row.market_code,
            )
            .order_by(ScaleEpisodeRecord.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if currency is None:
            continue

        active_targets = _active_targets(
            session, context.workspace_id,
            row.product_version_id, row.offer_version_id, row.market_code, currency,
        )
        floor = platform_spend_floor(policy, currency)
        contexts.append({
            "product_version_id": row.product_version_id,
            "offer_version_id": row.offer_version_id,
            "market_code": row.market_code,
            "currency": currency,
            "has_active_target": bool(active_targets),
            "platform_spend_floor": floor.amount_text,
            "platform_spend_floor_unavailable_reason": floor.unavailable_reason,
        })
    return contexts


def create_publication_preview(
    session: Session,
    context: TenantContext,
    *,
    product_version_id: str,
    offer_version_id: str,
    market_code: str,
    currency: str,
    target_cpa: Decimal | None = None,
    breakeven_roas: Decimal | None = None,
    minimum_economic_spend: Decimal | None = None,
    idempotency_key: str,
) -> ScaleTargetPublicationPreviewRecord:
    """Owner-only. Freeze the impact of publishing a new Target for one
    exact (ProductVersion, OfferVersion, market, currency) context: how many
    active/paused Enrollments would be affected, and which active Target it
    would replace, as of now.

    A Target whose minimum economic spend misses the platform floor is
    refused here (#134), after the journal claim, so a retry of a preview
    already made returns it whatever the Policy says now."""
    _require_owner(context)
    if target_cpa is None and breakeven_roas is None:
        raise TargetLifecycleError("at_least_one_target")

    affected = _count_affected_enrollments(
        session, context.workspace_id,
        product_version_id, offer_version_id, market_code,
    )
    replaced = _active_targets(
        session, context.workspace_id,
        product_version_id, offer_version_id, market_code, currency,
    )
    impact_hash = _impact_hash(
        product_version_id, offer_version_id, market_code, currency,
        target_cpa, breakeven_roas, minimum_economic_spend, affected,
        [t.id for t in replaced],
    )

    preview_id = new_id("stpp")
    fingerprint = command_request_fingerprint(
        product_version_id=product_version_id,
        offer_version_id=offer_version_id,
        market_code=market_code,
        currency=currency,
        target_cpa=str(target_cpa) if target_cpa is not None else None,
        breakeven_roas=str(breakeven_roas) if breakeven_roas is not None else None,
        minimum_economic_spend=(
            str(minimum_economic_spend) if minimum_economic_spend is not None else None
        ),
    )
    claim = claim_command(
        session, context,
        command_type="create_target_publication_preview",
        deduplication_key=idempotency_key,
        result_id=preview_id,
        request_fingerprint=fingerprint,
        scenario_scope_key=_scenario_scope_key(
            session, context, product_version_id, offer_version_id, market_code,
        ),
        result_resource_type="target_publication_preview",
    )
    if claim.replay:
        existing = session.get(
            ScaleTargetPublicationPreviewRecord, claim.record.result_resource_id
        )
        if existing is None:
            raise TargetLifecycleError(
                "idempotency_result_missing", claim.record.result_resource_id
            )
        return existing

    _check_minimum_economic_spend(
        platform_spend_floor(applicable_policy(session, context), currency),
        target_cpa=target_cpa,
        breakeven_roas=breakeven_roas,
        minimum_economic_spend=minimum_economic_spend,
    )

    now = datetime.now(timezone.utc)
    preview = ScaleTargetPublicationPreviewRecord(
        id=preview_id,
        workspace_id=context.workspace_id,
        product_version_id=product_version_id,
        offer_version_id=offer_version_id,
        market_code=market_code,
        currency=currency,
        target_cpa=target_cpa,
        breakeven_roas=breakeven_roas,
        minimum_economic_spend=minimum_economic_spend,
        affected_enrollment_count=affected,
        policy_floor_check_passed=True,
        impact_hash=impact_hash,
        creator_id=context.initiator_id or context.executor,
        expires_at=now + _PREVIEW_TTL,
    )
    session.add(preview)
    session.flush()
    return preview


def publish_target(
    session: Session,
    context: TenantContext,
    *,
    preview_id: str,
    idempotency_key: str,
) -> ScaleTargetVersionRecord:
    """Owner-only. Consume a publication preview and create the active
    Target it described, withdrawing the context's active Target in the
    same transaction (#398). The preview's amounts are checked against the
    platform floor again (#134), so a preview made under an older Policy
    cannot publish below the floor in force.

    The impact is recomputed under row locks and must match the preview's
    hash; otherwise, or when a concurrent first publication of the same
    context wins the unique index, this raises ``preview_hash_mismatch``
    and writes nothing. A replay of a committed publication returns its
    Target before any of these checks."""
    _require_owner(context)

    target_id = new_id("stgt")
    fingerprint = command_request_fingerprint(preview_id=preview_id)
    scenario_scope_key = None
    if context.meta_mode == "controlled_mock":
        # Columns, not the row: the preview is locked after the claim, and
        # an ORM read here would answer that locked read from the identity
        # map instead of the locked row.
        subject = session.execute(
            select(
                ScaleTargetPublicationPreviewRecord.product_version_id,
                ScaleTargetPublicationPreviewRecord.offer_version_id,
                ScaleTargetPublicationPreviewRecord.market_code,
            ).filter_by(workspace_id=context.workspace_id, id=preview_id)
        ).one_or_none()
        if subject is None:
            raise TargetNotFoundError(preview_id)
        scenario_scope_key = _scenario_scope_key(session, context, *subject)
    claim = claim_command(
        session, context,
        command_type="publish_scale_target",
        deduplication_key=idempotency_key,
        result_id=target_id,
        request_fingerprint=fingerprint,
        scenario_scope_key=scenario_scope_key,
        result_resource_type="target_version",
    )
    if claim.replay:
        existing = session.get(ScaleTargetVersionRecord, claim.record.result_resource_id)
        if existing is None:
            raise TargetLifecycleError(
                "idempotency_result_missing", claim.record.result_resource_id
            )
        return existing

    # Lock order after the journal claim: the preview, then the context's
    # active Target. A second publication of this preview waits on the
    # first lock, of this context on the second (or, with no active Target
    # yet, on the unique index below), and then sees what the first did.
    preview = session.execute(
        _for_update(session, select(ScaleTargetPublicationPreviewRecord).filter_by(
            workspace_id=context.workspace_id, id=preview_id,
        ))
    ).scalar_one_or_none()
    if preview is None:
        raise TargetNotFoundError(preview_id)
    if preview.consumed_at is not None:
        raise TargetPreviewConsumedError(preview_id)
    now = datetime.now(timezone.utc)
    expires_at = preview.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise TargetPreviewExpiredError(preview_id)

    # #134: checked again against the Policy in force now, which a newer
    # published version may have changed since the preview.
    _check_minimum_economic_spend(
        platform_spend_floor(applicable_policy(session, context), preview.currency),
        target_cpa=preview.target_cpa,
        breakeven_roas=preview.breakeven_roas,
        minimum_economic_spend=preview.minimum_economic_spend,
    )

    replaced = _active_targets(
        session, context.workspace_id,
        preview.product_version_id, preview.offer_version_id,
        preview.market_code, preview.currency,
        lock=True,
    )
    affected = _count_affected_enrollments(
        session, context.workspace_id,
        preview.product_version_id, preview.offer_version_id, preview.market_code,
    )
    impact_hash = _impact_hash(
        preview.product_version_id, preview.offer_version_id,
        preview.market_code, preview.currency,
        preview.target_cpa, preview.breakeven_roas, preview.minimum_economic_spend,
        affected, [t.id for t in replaced],
    )
    if impact_hash != preview.impact_hash:
        raise TargetLifecycleError(
            "preview_hash_mismatch", f"the context changed since preview {preview_id}",
        )

    target = ScaleTargetVersionRecord(
        id=target_id,
        workspace_id=context.workspace_id,
        product_version_id=preview.product_version_id,
        offer_version_id=preview.offer_version_id,
        market_code=preview.market_code,
        currency=preview.currency,
        target_cpa=preview.target_cpa,
        breakeven_roas=preview.breakeven_roas,
        minimum_economic_spend=preview.minimum_economic_spend,
        lifecycle_state="active",
        creator_id=context.initiator_id or context.executor,
        revision=1,
        published_at=now,
    )
    event = ScaleTargetLifecycleEventRecord(
        id=new_id("stge"),
        target_version_id=target_id,
        workspace_id=context.workspace_id,
        event_type="publish",
        previous_lifecycle_state=None,
        current_lifecycle_state="active",
        reason="publication_confirmed",
        semantic_event_sequence=1,
        executor_id=context.executor,
        original_initiator_id=context.initiator_id or context.executor,
        revision_at_event=1,
    )

    # One savepoint, like ``app.scale.enrollment._commit_transition``: a
    # concurrent first publication of this context that committed after
    # the check above fails the partial unique index here, which is the
    # same refusal as a hash mismatch, not a 500.
    try:
        with session.begin_nested():
            preview.consumed_at = now
            preview.result_target_version_id = target_id
            for old in replaced:
                old.lifecycle_state = "withdrawn"
                old.withdrawn_at = now
                old.revision += 1
                session.add(ScaleTargetLifecycleEventRecord(
                    id=new_id("stge"),
                    target_version_id=old.id,
                    workspace_id=context.workspace_id,
                    event_type="withdraw",
                    previous_lifecycle_state="active",
                    current_lifecycle_state="withdrawn",
                    reason=f"replaced_by:{target_id}",
                    semantic_event_sequence=old.revision,
                    executor_id=context.executor,
                    original_initiator_id=context.initiator_id or context.executor,
                    revision_at_event=old.revision,
                ))
            # Flush stages: the withdrawals before the new Target, so the
            # unique index never sees two active Targets in one context;
            # the new Target before its event, because SQLAlchemy's
            # automatic flush-ordering across mapped classes with no ORM
            # ``relationship()`` does not reliably topo-sort composite,
            # non-primary-key ``ForeignKeyConstraint``s (every Scale table
            # uses this shape; mirrors
            # ``app.scale.enrollment.create_draft_enrollment``'s note).
            session.flush()
            session.add(target)
            session.flush()
            session.add(event)
            session.flush()
    except IntegrityError:
        for obj in (preview, *replaced):
            session.expire(obj)
        raise TargetLifecycleError(
            "preview_hash_mismatch", f"another publication of this context won over preview {preview_id}",
        )
    # One Job for the publication and the withdrawals it implies (#113).
    _enqueue_target_change(session, context, event)
    return target


def create_withdrawal_preview(
    session: Session,
    context: TenantContext,
    *,
    target_id: str,
    reason: str,
    idempotency_key: str,
) -> ScaleTargetWithdrawalPreviewRecord:
    """Owner-only. Freeze the impact of withdrawing an active Target: how
    many Enrollments and active Advice threads would be affected."""
    _require_owner(context)

    target = session.execute(
        select(ScaleTargetVersionRecord).filter_by(
            workspace_id=context.workspace_id, id=target_id,
        )
    ).scalar_one_or_none()
    if target is None:
        raise TargetNotFoundError(target_id)
    if target.lifecycle_state != "active":
        raise TargetLifecycleError(
            "invalid_lifecycle", f"cannot withdraw {target.lifecycle_state} target"
        )

    dependent_threads = session.execute(
        select(func.count()).select_from(ScaleAdviceThreadRecord).filter(
            ScaleAdviceThreadRecord.workspace_id == context.workspace_id,
            ScaleAdviceThreadRecord.lifecycle == "active",
        )
    ).scalar_one()

    affected = _count_affected_enrollments(
        session, context.workspace_id,
        target.product_version_id, target.offer_version_id, target.market_code,
    )
    impact_hash = _impact_hash(
        target.product_version_id, target.offer_version_id,
        target.market_code, target.currency,
        target.target_cpa, target.breakeven_roas, target.minimum_economic_spend,
        affected,
    )

    preview_id = new_id("stwp")
    fingerprint = command_request_fingerprint(target_id=target_id, reason=reason)
    claim = claim_command(
        session, context,
        command_type="create_target_withdrawal_preview",
        deduplication_key=idempotency_key,
        result_id=preview_id,
        request_fingerprint=fingerprint,
        scenario_scope_key=_scenario_scope_key(
            session, context,
            target.product_version_id, target.offer_version_id, target.market_code,
        ),
        result_resource_type="target_withdrawal_preview",
    )
    if claim.replay:
        existing = session.get(
            ScaleTargetWithdrawalPreviewRecord, claim.record.result_resource_id
        )
        if existing is None:
            raise TargetLifecycleError(
                "idempotency_result_missing", claim.record.result_resource_id
            )
        return existing

    now = datetime.now(timezone.utc)
    preview = ScaleTargetWithdrawalPreviewRecord(
        id=preview_id,
        workspace_id=context.workspace_id,
        target_version_id=target_id,
        reason=reason,
        affected_enrollment_count=affected,
        dependent_advice_thread_count=dependent_threads,
        impact_hash=impact_hash,
        creator_id=context.initiator_id or context.executor,
        expires_at=now + _PREVIEW_TTL,
    )
    session.add(preview)
    session.flush()
    return preview


def withdraw_target(
    session: Session,
    context: TenantContext,
    *,
    preview_id: str,
    idempotency_key: str,
) -> ScaleTargetVersionRecord:
    """Owner-only. Consume a withdrawal preview and move its Target to
    ``withdrawn``."""
    _require_owner(context)

    fingerprint = command_request_fingerprint(preview_id=preview_id)
    scenario_scope_key = None
    if context.meta_mode == "controlled_mock":
        # Columns, not rows: the Target is locked after the claim (see
        # ``publish_target``).
        subject = session.execute(
            select(
                ScaleTargetVersionRecord.product_version_id,
                ScaleTargetVersionRecord.offer_version_id,
                ScaleTargetVersionRecord.market_code,
            ).join(
                ScaleTargetWithdrawalPreviewRecord,
                ScaleTargetWithdrawalPreviewRecord.target_version_id == ScaleTargetVersionRecord.id,
            ).filter(
                ScaleTargetWithdrawalPreviewRecord.workspace_id == context.workspace_id,
                ScaleTargetWithdrawalPreviewRecord.id == preview_id,
                ScaleTargetVersionRecord.workspace_id == context.workspace_id,
            )
        ).one_or_none()
        if subject is None:
            raise TargetNotFoundError(preview_id)
        scenario_scope_key = _scenario_scope_key(session, context, *subject)
    claim = claim_command(
        session, context,
        command_type="withdraw_scale_target",
        deduplication_key=idempotency_key,
        result_id=preview_id,
        request_fingerprint=fingerprint,
        scenario_scope_key=scenario_scope_key,
        result_resource_type="target_withdrawal_preview",
    )
    if claim.replay:
        replay_preview = session.get(
            ScaleTargetWithdrawalPreviewRecord, claim.record.result_resource_id
        )
        if replay_preview is None:
            raise TargetLifecycleError(
                "idempotency_result_missing", claim.record.result_resource_id
            )
        target = session.get(ScaleTargetVersionRecord, replay_preview.target_version_id)
        if target is None:
            raise TargetNotFoundError(replay_preview.target_version_id)
        return target

    preview = session.get(ScaleTargetWithdrawalPreviewRecord, preview_id)
    if preview is None or preview.workspace_id != context.workspace_id:
        raise TargetNotFoundError(preview_id)
    if preview.consumed_at is not None:
        raise TargetPreviewConsumedError(preview_id)
    now = datetime.now(timezone.utc)
    expires_at = preview.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise TargetPreviewExpiredError(preview_id)

    # Locked: a publication replacing this Target withdraws it too (#398),
    # and only one of the two may.
    target = session.execute(
        _for_update(session, select(ScaleTargetVersionRecord).filter_by(
            workspace_id=context.workspace_id, id=preview.target_version_id,
        ))
    ).scalar_one_or_none()
    if target is None:
        raise TargetNotFoundError(preview.target_version_id)
    if target.lifecycle_state != "active":
        raise TargetLifecycleError(
            "invalid_lifecycle", f"cannot withdraw {target.lifecycle_state} target"
        )

    preview.consumed_at = now
    preview.withdrawn_at = now

    old_state = target.lifecycle_state
    target.lifecycle_state = "withdrawn"
    target.withdrawn_at = now
    target.revision += 1

    event = ScaleTargetLifecycleEventRecord(
        id=new_id("stge"),
        target_version_id=target.id,
        workspace_id=context.workspace_id,
        event_type="withdraw",
        previous_lifecycle_state=old_state,
        current_lifecycle_state="withdrawn",
        reason=preview.reason,
        semantic_event_sequence=target.revision,
        executor_id=context.executor,
        original_initiator_id=context.initiator_id or context.executor,
        revision_at_event=target.revision,
    )
    session.add(event)
    session.flush()
    _enqueue_target_change(session, context, event)
    return target


def _enqueue_target_change(
    session: Session, context: TenantContext, event: ScaleTargetLifecycleEventRecord,
) -> None:
    """Re-evaluation after the change, in the command's transaction (#113)."""
    # Imported here: ``app.scale.target_change`` imports ``live_trigger``,
    # which imports this module.
    from .target_change import enqueue_target_change

    enqueue_target_change(session, context, event)


def list_targets(
    session: Session,
    context: TenantContext,
    *,
    lifecycle_state: str | None = None,
    product_version_id: str | None = None,
    offer_version_id: str | None = None,
    market_code: str | None = None,
) -> list[ScaleTargetVersionRecord]:
    stmt = (
        select(ScaleTargetVersionRecord)
        .filter_by(workspace_id=context.workspace_id)
        .order_by(ScaleTargetVersionRecord.created_at.desc())
    )
    if lifecycle_state:
        stmt = stmt.filter(ScaleTargetVersionRecord.lifecycle_state == lifecycle_state)
    if product_version_id:
        stmt = stmt.filter(ScaleTargetVersionRecord.product_version_id == product_version_id)
    if offer_version_id:
        stmt = stmt.filter(ScaleTargetVersionRecord.offer_version_id == offer_version_id)
    if market_code:
        stmt = stmt.filter(ScaleTargetVersionRecord.market_code == market_code)
    return list(session.execute(stmt).scalars().all())


def get_target(
    session: Session, context: TenantContext, target_id: str,
) -> ScaleTargetVersionRecord:
    target = session.execute(
        select(ScaleTargetVersionRecord).filter_by(
            workspace_id=context.workspace_id, id=target_id,
        )
    ).scalar_one_or_none()
    if target is None:
        raise TargetNotFoundError(target_id)
    return target


def target_actions(
    target: ScaleTargetVersionRecord, context: TenantContext,
) -> list[ActionEligibility]:
    # Lifecycle first: a withdrawn Target is not withdrawable for anyone.
    active = target.lifecycle_state == "active"
    is_owner = _is_owner(context)
    checks = {
        "withdraw": (active and is_owner, "invalid_lifecycle" if not active else "owner_required"),
    }
    return compute_actions(_TARGET_ACTION_CODES, checks)
