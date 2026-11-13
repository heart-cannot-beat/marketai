"""Launch Provisioning Saga (spec §5.3-§5.7).

Executes the multi-candidate provisioning saga for one frozen, confirmed
Launch preview: one shared Campaign plus one AdSet and one Ad per
candidate, all created PAUSED, each followed by a readback call, and
concluded with a set-level parity check.

Per spec §5.3, every Meta API "call" in the saga is persisted individually:
resolve the adapter, commit an intent + attempt pair, mark dispatch, invoke
the adapter, record the outcome, and advance the attempt's state. Nothing
here ever performs a real network call -- #23 scope uses ``FakeMetaAdapter``
exclusively (sandbox/mock), resolved the same way any other Meta-domain
caller resolves it (``app.meta.credential_resolution.resolve_adapter``), and
every intent it creates carries ``write_permitted = False`` (enforced at
the database level by ``MetaCallIntentRecord``'s check constraint).

Fencing (spec §5.5): before doing any work, the saga CAS-increments
``LaunchProvisioningRecord.fencing_token``. A caller that supplies a stale
``expected_fencing_token`` (one a newer worker has already superseded) is
rejected with :class:`ProvisioningError` before any saga step runs.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select, update

from ..adapters.persistence.models import new_id, utcnow
from ..meta.credential_resolution import create_call_intent, resolve_adapter
from ..meta.models import MetaCallOutcomeRecord, MetaConnectionRecord, MetaDataSourceBindingRecord
from ..platform.canonical import canonical_hash
from ..platform.tenancy import TenantContext, apply_to_session
from .models import (
    LaunchCandidateRecord,
    LaunchParityCheckRecord,
    LaunchProvisioningRecord,
    LaunchProvisioningStatusHistoryRecord,
    LaunchSagaStepAttemptRecord,
    MetaLaunchPreviewRecord,
)
from ..platform.runtime_role import WORKER_ROLE, runs_as


class ProvisioningError(RuntimeError):
    """Raised for saga admission failures (stale fencing, bad state, step failure)."""


class ProvisioningUncertain(ProvisioningError):
    """A dispatched call has no outcome and must be reconciled, not retried."""


# Provisioning states from which a saga run may (re)start. Terminal states
# ('provisioned', 'partially_provisioned', 'failed') refuse re-execution --
# recovery/retry of a terminal saga is a Task 14+ concern out of #23 scope.
_RESUMABLE_STATES = frozenset({"pending", "provisioning"})


def _durable_commit(session, context: TenantContext) -> None:
    """Commit one saga boundary and restore PostgreSQL SET LOCAL context."""
    had_context = session.info.get("tenant_context") is not None
    session.commit()
    if had_context:
        session.begin()
        apply_to_session(session, context)


def _cas_increment_fencing_token(session, provisioning_id: str, expected: int) -> int:
    """Spec §5.5: ``UPDATE ... SET fencing_token = fencing_token + 1 WHERE
    fencing_token = :expected``. A stale ``expected`` (already superseded by
    a newer worker's lease) affects zero rows -- that is the CAS failure
    signal, not an exception from the database.
    """
    result = session.execute(
        update(LaunchProvisioningRecord)
        .where(
            LaunchProvisioningRecord.id == provisioning_id,
            LaunchProvisioningRecord.fencing_token == expected,
        )
        .values(
            fencing_token=LaunchProvisioningRecord.fencing_token + 1,
            updated_at=utcnow(),
        )
    )
    if result.rowcount != 1:
        raise ProvisioningError(
            f"stale fencing token: expected {expected} for provisioning "
            f"{provisioning_id}, but it has already advanced"
        )
    return expected + 1


def _transition_provisioning_state(
    session,
    context: TenantContext,
    prov: LaunchProvisioningRecord,
    to_state: str,
    *,
    reason: str = "",
) -> None:
    from_state = prov.provisioning_state
    prov.provisioning_state = to_state
    prov.updated_at = utcnow()
    session.add(LaunchProvisioningStatusHistoryRecord(
        id=new_id("lpsh"), workspace_id=prov.workspace_id, meta_mode=prov.meta_mode,
        provisioning_id=prov.id, field="provisioning_state",
        from_state=from_state, to_state=to_state, reason=reason or None,
        executor=context.executor,
    ))
    session.flush()


def record_provisioning_failure(
    session,
    context: TenantContext,
    provisioning_id: str,
    *,
    reason: str,
) -> LaunchProvisioningRecord | None:
    """Persist a terminal failure the saga could not keep on its own.

    ``execute_provisioning`` records ``-> failed`` in the caller's unit of
    work, which is rolled back as the error propagates; a database error also
    aborts that transaction before the transition can even flush. The Worker
    calls this in a fresh unit of work so the durable record matches the
    failed Job. Only resumable states move: provisioned, partially
    provisioned or already failed evidence is never rewritten.
    """
    prov = session.execute(
        select(LaunchProvisioningRecord).where(
            LaunchProvisioningRecord.id == provisioning_id,
            LaunchProvisioningRecord.workspace_id == context.workspace_id,
            LaunchProvisioningRecord.meta_mode == context.meta_mode,
        ).with_for_update()
    ).scalar_one_or_none()
    if prov is None or prov.provisioning_state not in _RESUMABLE_STATES:
        return prov
    _transition_provisioning_state(
        session, context, prov, "failed", reason=reason[:500],
    )
    return prov


def _execute_step(
    session,
    context: TenantContext,
    prov: LaunchProvisioningRecord,
    connection: MetaConnectionRecord,
    *,
    step_type: str,
    candidate_id: str | None,
    object_type: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
    ad_account_id: str,
    validate_eligibility: Callable[[], None],
):
    """One saga "write" call (spec §5.3): resolve -> intent+attempt -> dispatch
    -> invoke -> outcome -> applied. Returns the adapter's
    :class:`~app.adapters.meta.fake_adapter.FakeAdapterResult`.
    """
    validate_eligibility()
    resolved = resolve_adapter(
        session, workspace_id=context.workspace_id, meta_mode=context.meta_mode,
        connection_id=connection.id, ad_account_id=ad_account_id,
    )
    intent = create_call_intent(
        session, workspace_id=context.workspace_id, meta_mode=context.meta_mode,
        connection=connection, binding=resolved.binding, operation_id=prov.operation_id,
        meta_api_method="POST", meta_api_endpoint=f"/{object_type}s",
        request_parameters=request_payload, call_type="saga_step",
        scenario_version_id=resolved.scenario_version_id, executor=context.executor,
    )
    attempt = LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=context.workspace_id, meta_mode=context.meta_mode,
        provisioning_id=prov.id, intent_id=intent.id, candidate_id=candidate_id,
        step_type=step_type, attempt_number=1, fencing_token=prov.fencing_token,
        state="intent_committed", executor=context.executor,
    )
    session.add(attempt)
    session.flush()

    # Dispatch marker (spec §5.3 step 2) -- the point past which a missing
    # outcome means "uncertain", not "never happened".
    intent.network_dispatched_at = utcnow()
    session.flush()
    _durable_commit(session, context)

    try:
        result = resolved.adapter.ensure_object(object_type, idempotency_key, request_payload)
    except Exception as exc:  # the dispatch marker is durable; outcome is unknown
        raise ProvisioningUncertain(
            f"{step_type} dispatched without outcome: {type(exc).__name__}: {exc}"
        ) from exc

    session.add(MetaCallOutcomeRecord(
        id=new_id("mco"), workspace_id=context.workspace_id, meta_mode=context.meta_mode,
        intent_id=intent.id, operation_id=prov.operation_id, network_attempted=True,
        http_status=200, response_evidence_hash=canonical_hash({"state": result.state}),
        response_summary={
            "remote_object_id": result.remote_object_id,
            "object_type": result.object_type,
            "recovered": result.recovered,
        },
        executor=context.executor,
    ))
    attempt.state = "outcome_received"
    attempt.remote_object_id = result.remote_object_id
    session.flush()
    _durable_commit(session, context)

    attempt.state = "applied"
    attempt.completed_at = utcnow()
    session.flush()
    _durable_commit(session, context)

    return result


def _execute_readback(
    session,
    context: TenantContext,
    prov: LaunchProvisioningRecord,
    connection: MetaConnectionRecord,
    *,
    step_type: str,
    candidate_id: str | None,
    object_type: str,
    remote_object_id: str,
    ad_account_id: str,
    validate_eligibility: Callable[[], None],
) -> dict[str, Any] | None:
    """A readback call (spec §5.6): a separate intent+outcome pair that reads
    back the object just created, feeding the parity check below.
    """
    validate_eligibility()
    resolved = resolve_adapter(
        session, workspace_id=context.workspace_id, meta_mode=context.meta_mode,
        connection_id=connection.id, ad_account_id=ad_account_id,
    )
    intent = create_call_intent(
        session, workspace_id=context.workspace_id, meta_mode=context.meta_mode,
        connection=connection, binding=resolved.binding, operation_id=prov.operation_id,
        meta_api_method="GET", meta_api_endpoint=f"/{object_type}s/{remote_object_id}",
        request_parameters={"object_type": object_type, "remote_object_id": remote_object_id},
        call_type="saga_step", scenario_version_id=resolved.scenario_version_id,
        executor=context.executor,
    )
    attempt = LaunchSagaStepAttemptRecord(
        id=new_id("lssa"), workspace_id=context.workspace_id, meta_mode=context.meta_mode,
        provisioning_id=prov.id, intent_id=intent.id, candidate_id=candidate_id,
        step_type=step_type, attempt_number=1, fencing_token=prov.fencing_token,
        state="intent_committed", remote_object_id=remote_object_id, executor=context.executor,
    )
    session.add(attempt)
    session.flush()

    intent.network_dispatched_at = utcnow()
    session.flush()
    _durable_commit(session, context)

    try:
        state = resolved.adapter.read_object(object_type, remote_object_id)
    except Exception as exc:
        raise ProvisioningUncertain(
            f"{step_type} dispatched without outcome: {type(exc).__name__}: {exc}"
        ) from exc

    session.add(MetaCallOutcomeRecord(
        id=new_id("mco"), workspace_id=context.workspace_id, meta_mode=context.meta_mode,
        intent_id=intent.id, operation_id=prov.operation_id, network_attempted=True,
        http_status=200 if state is not None else 404,
        response_evidence_hash=canonical_hash({"state": state or {}}),
        response_summary={"remote_object_id": remote_object_id, "found": state is not None},
        executor=context.executor,
    ))
    attempt.state = "outcome_received"
    session.flush()
    _durable_commit(session, context)

    attempt.state = "applied"
    attempt.completed_at = utcnow()
    session.flush()
    _durable_commit(session, context)

    return state


@runs_as(WORKER_ROLE)
def execute_provisioning(
    session,
    context: TenantContext,
    provisioning_id: str,
    expected_fencing_token: int | None = None,
    *,
    eligibility_validator: Callable[
        [object, TenantContext, MetaLaunchPreviewRecord], None
    ],
) -> LaunchProvisioningRecord:
    """Run the provisioning saga for one ``LaunchProvisioningRecord``.

    Steps (spec §5.1-§5.7): CAS-increment the fencing token; transition
    ``pending -> provisioning``; create the shared Campaign; per candidate
    create an AdSet and an Ad; readback every created object; run the
    set-level parity check; transition ``provisioning -> provisioned`` (or
    ``-> failed`` on any error, including a failed parity check).
    """
    prov = session.execute(
        select(LaunchProvisioningRecord).where(
            LaunchProvisioningRecord.id == provisioning_id,
            LaunchProvisioningRecord.workspace_id == context.workspace_id,
            LaunchProvisioningRecord.meta_mode == context.meta_mode,
        )
    ).scalar_one_or_none()
    if prov is None:
        raise ProvisioningError(
            f"provisioning {provisioning_id} not found for workspace "
            f"{context.workspace_id} mode {context.meta_mode}"
        )

    if prov.provisioning_state not in _RESUMABLE_STATES:
        raise ProvisioningError(
            f"provisioning {provisioning_id} is {prov.provisioning_state!r}; "
            "not resumable by execute_provisioning"
        )

    if expected_fencing_token is None:
        expected_fencing_token = prov.fencing_token

    new_token = _cas_increment_fencing_token(session, provisioning_id, expected_fencing_token)
    prov.fencing_token = new_token
    session.flush()

    if prov.provisioning_state == "pending":
        _transition_provisioning_state(session, context, prov, "provisioning")

    connection = session.get(MetaConnectionRecord, prov.meta_connection_id)
    if connection is None:
        raise ProvisioningError(f"connection {prov.meta_connection_id} not found")

    preview = session.get(MetaLaunchPreviewRecord, prov.preview_id)
    if preview is None:
        raise ProvisioningError(f"preview {prov.preview_id} not found")

    binding = session.get(MetaDataSourceBindingRecord, preview.meta_binding_id)
    if binding is None:
        raise ProvisioningError(f"binding {preview.meta_binding_id} not found")
    ad_account_id = binding.ad_account_id

    candidates = session.execute(
        select(LaunchCandidateRecord)
        .where(LaunchCandidateRecord.preview_id == prov.preview_id)
        .order_by(LaunchCandidateRecord.candidate_index)
    ).scalars().all()

    def validate_eligibility() -> None:
        eligibility_validator(session, context, preview)

    try:
        validate_eligibility()
        campaign_payload = {
            "objective": preview.campaign_config.get("objective", "OUTCOME_TRAFFIC"),
            "campaign_budget_optimization": False,
            "daily_budget_minor": preview.daily_budget_minor,
            "currency": preview.currency,
            "status": "PAUSED",
        }
        campaign_result = _execute_step(
            session, context, prov, connection,
            step_type="create_campaign", candidate_id=None, object_type="campaign",
            idempotency_key=prov.campaign_idempotency_key, request_payload=campaign_payload,
            ad_account_id=ad_account_id,
            validate_eligibility=validate_eligibility,
        )
        remote_campaign_id = campaign_result.remote_object_id
        prov.remote_campaign_id = remote_campaign_id
        session.flush()

        campaign_readback = _execute_readback(
            session, context, prov, connection,
            step_type="readback_campaign", candidate_id=None, object_type="campaign",
            remote_object_id=remote_campaign_id, ad_account_id=ad_account_id,
            validate_eligibility=validate_eligibility,
        )

        candidate_results: list[dict[str, Any]] = []
        for candidate in candidates:
            adset_payload = {
                "campaign_id": remote_campaign_id,
                "daily_budget_minor": preview.daily_budget_minor,
                "candidate_index": candidate.candidate_index,
                "status": "PAUSED",
            }
            adset_result = _execute_step(
                session, context, prov, connection,
                step_type="create_adset", candidate_id=candidate.id, object_type="adset",
                idempotency_key=candidate.adset_idempotency_key, request_payload=adset_payload,
                ad_account_id=ad_account_id,
                validate_eligibility=validate_eligibility,
            )

            ad_payload = {
                "adset_id": adset_result.remote_object_id,
                "creative_config": candidate.creative_config,
                "status": "PAUSED",
            }
            ad_result = _execute_step(
                session, context, prov, connection,
                step_type="create_ad", candidate_id=candidate.id, object_type="ad",
                idempotency_key=candidate.ad_idempotency_key, request_payload=ad_payload,
                ad_account_id=ad_account_id,
                validate_eligibility=validate_eligibility,
            )

            adset_readback = _execute_readback(
                session, context, prov, connection,
                step_type="readback_adset", candidate_id=candidate.id, object_type="adset",
                remote_object_id=adset_result.remote_object_id, ad_account_id=ad_account_id,
                validate_eligibility=validate_eligibility,
            )
            ad_readback = _execute_readback(
                session, context, prov, connection,
                step_type="readback_ad", candidate_id=candidate.id, object_type="ad",
                remote_object_id=ad_result.remote_object_id, ad_account_id=ad_account_id,
                validate_eligibility=validate_eligibility,
            )

            candidate_results.append({
                "candidate_id": candidate.id,
                "adset_remote_id": adset_result.remote_object_id,
                "ad_remote_id": ad_result.remote_object_id,
                "adset_state": adset_readback,
                "ad_state": ad_readback,
            })

        # Parity check (spec §5.7): all candidates present, budgets equal
        # to the frozen preview budget, and every object still PAUSED.
        all_present = (
            len(candidate_results) == len(candidates) == preview.candidate_count
        )
        budget_ok = all(
            (result["adset_state"] or {}).get("daily_budget_minor") == preview.daily_budget_minor
            for result in candidate_results
        )
        config_ok = all(
            result["ad_state"] is not None for result in candidate_results
        )
        status_ok = (
            (campaign_readback or {}).get("status") == "PAUSED"
            and all((result["adset_state"] or {}).get("status") == "PAUSED" for result in candidate_results)
            and all((result["ad_state"] or {}).get("status") == "PAUSED" for result in candidate_results)
        )
        passed = all_present and budget_ok and config_ok and status_ok

        session.add(LaunchParityCheckRecord(
            id=new_id("lpc"), workspace_id=context.workspace_id, meta_mode=context.meta_mode,
            provisioning_id=prov.id, check_type="post_provisioning",
            all_candidates_present=all_present, budget_parity_verified=budget_ok,
            config_parity_verified=config_ok, status_parity_verified=status_ok,
            detail={
                "remote_campaign_id": remote_campaign_id,
                "campaign_state": campaign_readback,
                "candidates": candidate_results,
            },
            passed=passed, executor=context.executor,
        ))
        session.flush()

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

        _transition_provisioning_state(session, context, prov, "provisioned")
        return prov
    except ProvisioningUncertain as exc:
        _transition_provisioning_state(
            session, context, prov, "partially_provisioned", reason=str(exc)[:500],
        )
        _durable_commit(session, context)
        raise
    except ProvisioningError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any unexpected saga failure is terminal
        _transition_provisioning_state(
            session, context, prov, "failed", reason=f"{type(exc).__name__}: {exc}"[:500],
        )
        raise ProvisioningError(f"provisioning {provisioning_id} failed: {exc}") from exc
