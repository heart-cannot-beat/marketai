"""Spend reservation for BYOK Generation Runs.

A real provider call costs money, so a ``byok`` Run reserves it where the
entitlements spec puts reservation: at admission, in the same transaction
that creates the work (``submit_generation``) or re-opens it
(``consume_recovery_preview``), never in the Worker. The Worker only
consumes: ``app.generation.byok_dispatch`` hands the item's open reservation
to the dispatch coordinator, which settles it on success and releases it on
a terminal failure.

The pieces are the platform's own: ``EntitlementPolicy`` proves the
Workspace holds a live entitlement lease, ``CostQuoteEngine`` freezes one
quote per call at the list price ``app.generation.pricing`` resolves for the
Run's model and format, and ``ReservationBook`` checks the Workspace's spend
guardrails against the whole command before writing one ``SpendReservation``
(plus its quote/reservation ledger entries) per RunItem. A guardrail breach
therefore rejects the command up front instead of failing items one by one
halfway through a Run.

Since Issue #169 every image of a ``byok`` Run is also quality-checked by a
governed ``llm_chat`` vision call (``app.generation.vlm_qc``) when the
recipe's frozen ``quality_check`` policy says so. That call is paid too, so
the same command reserves it next to the image call: one more
``SpendReservation`` per (item, Job), keyed ``llm_call_key(job, "vlm_qc")``
and priced at the policy's frozen per-image maximum, inside the *same*
``ReservationBook.reserve`` -- the guardrails see the command's whole
exposure (images + checks) at once. The confirmation quote
(:func:`quote_recipe`) adds the same amount per image, so the preview's
Maximum cost, the confirmation and the reservations stay equal.

``SpendGuardrail`` rows are provisioned lazily with the platform's
conservative defaults (``config.MAX_COST_PER_RUN`` per command,
``config.MAX_COST_PER_DAY`` per rolling day) the first time a Workspace
reserves real spend; the Owner raises them through the guardrail API.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from sqlalchemy.orm import Session

from app import config
from app.adapters.persistence.models import GenerationRunRecord, JobRecord, WorkspaceRecord, utcnow
from app.entitlements.cost_models import SpendGuardrailRecord, SpendReservationRecord
from app.entitlements.policy import (
    EntitlementPolicy,
    FeatureNotEntitledError,
    LeaseExpiredError,
    NoEntitlementProjectionError,
    NumericEntitlementExceededError,
    WorkspaceSuspendedError,
)
from app.entitlements.quote_engine import CostQuoteEngine
from app.entitlements.reservation_book import GuardrailExceededError, ReservationBook
from app.entitlements.types import (
    BillableCallSpec,
    EntitlementRequirements,
    QuoteBundle,
    ReservationPlan,
)
from app.generation import pricing, vlm_qc
from app.generation.creative_recipe import reference_count_of
from app.generation.models import GenerationRecipeSnapshotRecord, RunItemRecord
from app.platform.tenancy import TenantContext

#: Modes whose calls cost real money and therefore reserve spend.
CHARGED_EXECUTION_MODES = ("byok",)


class SpendReservationError(ValueError):
    """Admission refused to reserve the command's spend.

    ``code`` is the machine-readable reason the API surfaces
    (``guardrail_exceeded``, ``entitlement_unavailable``, ``unpriced_call``).
    """

    def __init__(self, code: str, detail: str, *, params: dict | None = None):
        self.code = code
        self.detail = detail
        self.params = params or {}
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ReservedSpend:
    """What a command reserved: one reservation id per RunItem, and the total."""

    reservation_id_by_item_id: dict[str, str]
    total_provider_amount: Decimal
    total_budget_amount: Decimal
    currency: str
    #: The quality-check call's reservation per item (Issue #169); empty when
    #: the recipe's check is not a charged VLM call.
    quality_check_reservation_id_by_item_id: dict[str, str] | None = None


def is_charged(execution_mode: str) -> bool:
    return execution_mode in CHARGED_EXECUTION_MODES


@dataclass(frozen=True)
class RecipeQuote:
    amount: Decimal
    currency: str
    calls: int


#: Mirrors ``app.generation.submission._DEFAULT_COMBINATIONS``: a recipe whose
#: strategy was never expanded still produces one combination.
_SINGLE_COMBINATION = 1


def quote_recipe(recipe: dict, execution_mode: str) -> RecipeQuote:
    """Maximum cost of running ``recipe`` once: list price x planned calls.

    Zero for every mode that costs nothing (mock, sandbox); for a charged
    mode the calls are the same ``selected_combinations x variants`` the
    submission expands into RunItems, so the confirmation's quote and the
    per-item reservations add up to the same number. A recipe that sends
    reference images is priced per call with them (Issue #167). An unpriced provider
    quotes zero here and is refused when reservation is attempted. Each
    image's charged quality check (Issue #169) adds its frozen maximum.
    """
    currency = "USD"
    if not is_charged(execution_mode):
        return RecipeQuote(Decimal("0"), currency, 0)
    format_config = dict(recipe.get("format") or {})
    combinations = len(recipe.get("selected_combinations") or []) or _SINGLE_COMBINATION
    calls = combinations * int(format_config.get("variants_per_combination", 1))
    try:
        price = pricing.unit_price(
            str(recipe.get("provider", "")), str(recipe.get("model", "")), format_config,
            reference_images=reference_count_of(recipe),
        )
    except pricing.UnpricedCallError:
        return RecipeQuote(Decimal("0"), currency, calls)
    price += vlm_qc.charged_cost_per_image(vlm_qc.policy_of(recipe))
    return RecipeQuote(price * calls, currency, calls)


def reservation_semantic_key(semantic_fingerprint: str, job_id: str) -> str:
    """The reservation identity of one dispatch of one RunItem.

    Keyed by the item *and* the Job that will dispatch it: a retried item
    whose first reservation was released on a terminal failure needs a
    fresh reservation, and ``spend_reservations.semantic_key_hash`` is unique
    per Workspace.
    """
    return hashlib.sha256(
        f"spend-reservation:generation:{semantic_fingerprint}:{job_id}".encode("utf-8")
    ).hexdigest()


def ensure_spend_guardrail(session: Session, ctx: TenantContext) -> SpendGuardrailRecord:
    """The Workspace's guardrail row, provisioned with platform defaults if absent."""
    guardrail = session.get(SpendGuardrailRecord, ctx.workspace_id)
    if guardrail is not None:
        return guardrail
    workspace = session.get(WorkspaceRecord, ctx.workspace_id)
    now = utcnow()
    per_day = Decimal(config.MAX_COST_PER_DAY)
    guardrail = SpendGuardrailRecord(
        workspace_id=ctx.workspace_id,
        revision=1,
        budget_currency=(workspace.budget_currency if workspace is not None else "USD"),
        per_operation_limit=Decimal(config.MAX_COST_PER_RUN),
        rolling_24h_limit=per_day,
        period_limit=per_day * 30,
        outstanding_exposure_limit=per_day,
        platform_policy_version=1,
        created_at=now,
        updated_at=now,
        updated_by="system:platform-defaults",
    )
    session.add(guardrail)
    session.flush()
    return guardrail


def reserve_run_item_spend(
    session: Session,
    ctx: TenantContext,
    *,
    run: GenerationRunRecord,
    jobs_by_item: Sequence[tuple[RunItemRecord, JobRecord]],
    operation_id: str,
    format_config: dict | None,
) -> ReservedSpend:
    """Reserve one call's spend for every ``(item, job)`` pair of a command.

    Raises :class:`SpendReservationError` when the Workspace cannot pay for
    the command right now; the caller's transaction then rolls back the
    whole command (Run, items, jobs), so nothing is left half-admitted.
    """
    if not jobs_by_item:
        return ReservedSpend({}, Decimal("0"), Decimal("0"), run.currency or "USD")

    try:
        registry = pricing.price_registry_for(
            run.provider, run.model, format_config,
            reference_images=_run_reference_count(session, ctx, run),
        )
    except pricing.UnpricedCallError as exc:
        raise SpendReservationError("unpriced_call", str(exc)) from exc

    now = utcnow()
    try:
        entitlement_ref = EntitlementPolicy().authorize(
            session, ctx,
            EntitlementRequirements(feature_keys=(), numeric_requirements=()),
            now,
        )
    except (
        NoEntitlementProjectionError,
        LeaseExpiredError,
        WorkspaceSuspendedError,
        FeatureNotEntitledError,
        NumericEntitlementExceededError,
    ) as exc:
        raise SpendReservationError("entitlement_unavailable", str(exc)) from exc

    guardrail = ensure_spend_guardrail(session, ctx)
    budget_currency = guardrail.budget_currency or "USD"

    key_by_item_id: dict[str, str] = {}
    job_ids_by_call_key: dict[str, str] = {}
    specs: list[BillableCallSpec] = []
    for item, job in jobs_by_item:
        key = reservation_semantic_key(item.semantic_fingerprint, job.id)
        key_by_item_id[item.id] = key
        job_ids_by_call_key[key] = job.id
        specs.append(BillableCallSpec(
            call_semantic_key_hash=key,
            provider=run.provider,
            model=run.model,
            quantity=1,
        ))

    quote_bundle = CostQuoteEngine(registry, pricing.PRICE_RULE_VERSION).bundle(
        specs, execution_mode=run.execution_mode, budget_currency=budget_currency,
    )
    qc_key_by_item_id, qc_quotes = _quality_check_quotes(
        session, ctx,
        run=run,
        jobs_by_item=jobs_by_item,
        budget_currency=budget_currency,
        job_ids_by_call_key=job_ids_by_call_key,
    )
    if qc_quotes:
        quote_bundle = QuoteBundle(
            call_quotes=quote_bundle.call_quotes + tuple(qc_quotes),
            total_budget_amount=quote_bundle.total_budget_amount
            + sum((quote.budget_amount for quote in qc_quotes), Decimal("0")),
        )
    plan = ReservationPlan(
        quote_bundle=quote_bundle,
        capacity_requests=(),
        entitlement_ref=entitlement_ref,
        operation_id=operation_id,
        job_ids_by_call_key=job_ids_by_call_key,
    )
    try:
        bundle = ReservationBook().reserve(session, ctx, plan)
    except GuardrailExceededError as exc:
        raise SpendReservationError(
            "guardrail_exceeded",
            f"{exc.limit_name}: this command would bring spend to "
            f"{exc.proposed_value} {budget_currency}, above the limit of "
            f"{exc.limit_value} {budget_currency}",
            params={
                "limit_name": exc.limit_name,
                "limit": format(Decimal(str(exc.limit_value)).normalize(), "f"),
                "proposed": format(Decimal(str(exc.proposed_value)).normalize(), "f"),
                "currency": budget_currency,
            },
        ) from exc

    # ReservationBook keys reservations by call; the Worker recomputes the
    # same key from the item and its Job (``open_reservation_for_item``), so
    # no row is touched after insert -- the API role creates reservations and
    # never updates them (``deploy/postgres/domain-tenant-roles.sql``).
    reservation_id_by_item_id = {
        item_id: bundle.spend_reservation_by_call_key[key]
        for item_id, key in key_by_item_id.items()
    }
    quality_check_reservations = {
        item_id: bundle.spend_reservation_by_call_key[key]
        for item_id, key in qc_key_by_item_id.items()
    }

    total_provider = sum((q.provider_amount for q in quote_bundle.call_quotes), Decimal("0"))
    return ReservedSpend(
        reservation_id_by_item_id=reservation_id_by_item_id,
        total_provider_amount=total_provider,
        total_budget_amount=quote_bundle.total_budget_amount,
        currency=budget_currency,
        quality_check_reservation_id_by_item_id=quality_check_reservations or None,
    )


def _run_quality_check_policy(session: Session, ctx: TenantContext, run: GenerationRunRecord) -> dict | None:
    if not run.recipe_snapshot_id:
        return None
    from sqlalchemy import select

    snapshot = session.execute(
        select(GenerationRecipeSnapshotRecord).where(
            GenerationRecipeSnapshotRecord.id == run.recipe_snapshot_id,
            GenerationRecipeSnapshotRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    return vlm_qc.policy_of(snapshot.canonical_json if snapshot is not None else None)


def _quality_check_quotes(
    session: Session,
    ctx: TenantContext,
    *,
    run: GenerationRunRecord,
    jobs_by_item: Sequence[tuple[RunItemRecord, JobRecord]],
    budget_currency: str,
    job_ids_by_call_key: dict[str, str],
) -> tuple[dict[str, str], list]:
    """Quote each item's charged quality-check call (Issue #169).

    Returns ``({item_id: call_key}, [FrozenQuote])`` and registers each call
    key's Job in ``job_ids_by_call_key``; both empty when the recipe's check
    is not a charged VLM call (Mock, no policy, or no OpenAI key at Preview). The check's key is the one the Worker's
    ``llm_chat`` call derives -- ``llm_call_key(<the item's Job>, "vlm_qc")``
    -- and its price is the policy's frozen per-image maximum.
    """
    policy = _run_quality_check_policy(session, ctx, run)
    if not vlm_qc.is_charged(policy):
        return {}, []
    from app.generation.credential_binding import resolve_generation_credential
    from app.providers.llm_chat import llm_call_key

    provider = str(policy.get("provider") or "")
    model = str(policy.get("model") or "")
    if not resolve_generation_credential(session, ctx, provider).get("active"):
        raise SpendReservationError(
            "quality_check_credential_unavailable",
            f"the quality check of every image runs on the Workspace's {provider} key, "
            "which is no longer bound; bind it again in Settings",
        )
    amount = vlm_qc.charged_cost_per_image(policy)
    engine = CostQuoteEngine({provider: {model: amount}}, str(policy.get("price_rule_version") or ""))
    keys: dict[str, str] = {}
    quotes = []
    for item, job in jobs_by_item:
        key = llm_call_key(job.id, vlm_qc.QC_CALL_NAME)
        keys[item.id] = key
        job_ids_by_call_key[key] = job.id
        quotes.append(engine.quote(
            call_semantic_key_hash=key,
            provider=provider,
            model=model,
            execution_mode=run.execution_mode,
            quantity=1,
            budget_currency=budget_currency,
        ))
    return keys, quotes


def _run_reference_count(session: Session, ctx: TenantContext, run: GenerationRunRecord) -> int:
    """Reference images each call of ``run`` sends, from its frozen recipe."""
    if not run.recipe_snapshot_id:
        return 0
    from sqlalchemy import select

    snapshot = session.execute(
        select(GenerationRecipeSnapshotRecord).where(
            GenerationRecipeSnapshotRecord.id == run.recipe_snapshot_id,
            GenerationRecipeSnapshotRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one_or_none()
    return reference_count_of(snapshot.canonical_json if snapshot is not None else None)


def open_reservation_for_item(
    session: Session, ctx: TenantContext, item: RunItemRecord, job_id: str,
) -> SpendReservationRecord | None:
    """The ``reserved`` reservation the command that enqueued ``job_id`` made for ``item``."""
    from sqlalchemy import select

    return session.execute(
        select(SpendReservationRecord).where(
            SpendReservationRecord.workspace_id == ctx.workspace_id,
            SpendReservationRecord.semantic_key_hash == reservation_semantic_key(
                item.semantic_fingerprint, job_id,
            ),
            SpendReservationRecord.state == "reserved",
        )
    ).scalars().first()
