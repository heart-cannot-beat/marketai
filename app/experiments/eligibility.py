"""Eligibility event processing and projection fold/rebuild."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    DecisionProposalRecord,
    DecisionProposalStateEventRecord,
    ExperimentEligibilityEventRecord,
    ExperimentEligibilityProjectionRecord,
    new_id,
    utcnow,
)
from ..platform.canonical import _canonical_json, _sha256

IRREVERSIBLE_EVENT_TYPES = frozenset({
    "product_version_withdrawn",
    "offer_version_withdrawn",
})

REVERSIBLE_EVENT_TYPES = frozenset({
    "review_rejected",
    "qc_failed_blocking",
    "product_version_eligibility_changed",
    "offer_version_eligibility_changed",
    "stale_truth_detected",
})

RESTORATION_EVENT_TYPES = frozenset({
    "review_re_approved",
    "qc_remediated",
})


class EligibilitySequenceConflict(RuntimeError):
    pass


def _compute_head_hash(
    previous_hash: str,
    event_id: str,
    event_type: str,
    eligibility: str,
) -> str:
    payload = _canonical_json({
        "previous_hash": previous_hash,
        "event_id": event_id,
        "event_type": event_type,
        "eligibility": eligibility,
    })
    return _sha256(payload.encode("utf-8"))


def _initial_head_hash(level: str, target_id: str) -> str:
    payload = _canonical_json({
        "level": level,
        "target_id": target_id,
        "initial": True,
    })
    return _sha256(payload.encode("utf-8"))


def ensure_projection(
    session: Session,
    *,
    workspace_id: str,
    experiment_id: str,
    experiment_version_id: str,
    level: str,
    candidate_id: str | None = None,
) -> ExperimentEligibilityProjectionRecord:
    filters = [
        ExperimentEligibilityProjectionRecord.workspace_id == workspace_id,
        ExperimentEligibilityProjectionRecord.experiment_id == experiment_id,
        ExperimentEligibilityProjectionRecord.experiment_version_id == experiment_version_id,
        ExperimentEligibilityProjectionRecord.level == level,
    ]
    if level == "candidate":
        filters.append(
            ExperimentEligibilityProjectionRecord.candidate_id == candidate_id
        )
    else:
        filters.append(
            ExperimentEligibilityProjectionRecord.candidate_id.is_(None)
        )

    proj = session.scalar(select(ExperimentEligibilityProjectionRecord).where(*filters))
    if proj is not None:
        return proj

    target_id = candidate_id or experiment_version_id
    initial_hash = _initial_head_hash(level, target_id)

    proj = ExperimentEligibilityProjectionRecord(
        id=new_id("eproj"),
        workspace_id=workspace_id,
        experiment_id=experiment_id,
        experiment_version_id=experiment_version_id,
        level=level,
        candidate_id=candidate_id,
        current_eligibility="eligible",
        irreversible_active=False,
        head_event_id=None,
        head_hash=initial_hash,
        head_sequence=0,
    )
    session.add(proj)
    session.flush()
    return proj


def append_eligibility_event(
    session: Session,
    *,
    workspace_id: str,
    experiment_id: str,
    experiment_version_id: str,
    candidate_id: str | None = None,
    upstream_event_type: str,
    upstream_resource_type: str,
    upstream_resource_id: str,
    upstream_event_id: str,
    evidence: dict,
) -> ExperimentEligibilityEventRecord:
    level = "candidate" if candidate_id else "version"
    proj = ensure_projection(
        session,
        workspace_id=workspace_id,
        experiment_id=experiment_id,
        experiment_version_id=experiment_version_id,
        level=level,
        candidate_id=candidate_id,
    )

    is_irreversible = upstream_event_type in IRREVERSIBLE_EVENT_TYPES
    is_restoration = upstream_event_type in RESTORATION_EVENT_TYPES

    if is_restoration:
        new_eligibility = "eligible"
    else:
        new_eligibility = "ineligible"

    now = utcnow()
    if proj.head_event_id is not None:
        latest = session.get(ExperimentEligibilityEventRecord, proj.head_event_id)
        if latest is not None:
            latest_ts = latest.observed_at
            if latest_ts.tzinfo is None:
                from datetime import timezone
                latest_ts = latest_ts.replace(tzinfo=timezone.utc)
            if latest_ts >= now:
                now = latest_ts + timedelta(microseconds=1)

    event = ExperimentEligibilityEventRecord(
        id=new_id("eev"),
        workspace_id=workspace_id,
        experiment_id=experiment_id,
        experiment_version_id=experiment_version_id,
        candidate_id=candidate_id,
        upstream_event_type=upstream_event_type,
        upstream_resource_type=upstream_resource_type,
        upstream_resource_id=upstream_resource_id,
        upstream_event_id=upstream_event_id,
        previous_effective_eligibility=proj.current_eligibility,
        current_effective_eligibility=new_eligibility,
        reversible=not is_irreversible,
        evidence_json=evidence,
        observed_at=now,
    )
    session.add(event)
    session.flush()

    new_head_hash = _compute_head_hash(
        proj.head_hash, event.id, upstream_event_type, new_eligibility,
    )
    new_seq = proj.head_sequence + 1

    result = session.execute(
        update(ExperimentEligibilityProjectionRecord)
        .where(
            ExperimentEligibilityProjectionRecord.id == proj.id,
            ExperimentEligibilityProjectionRecord.head_sequence == proj.head_sequence,
        )
        .values(
            current_eligibility=new_eligibility,
            irreversible_active=proj.irreversible_active or is_irreversible,
            head_event_id=event.id,
            head_hash=new_head_hash,
            head_sequence=new_seq,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        raise EligibilitySequenceConflict("Eligibility projection CAS failed")

    if is_irreversible:
        _invalidate_open_proposals(
            session,
            workspace_id=workspace_id,
            experiment_id=experiment_id,
            experiment_version_id=experiment_version_id,
            event=event,
        )

    return event


def _invalidate_open_proposals(
    session: Session,
    *,
    workspace_id: str,
    experiment_id: str,
    experiment_version_id: str,
    event: ExperimentEligibilityEventRecord,
) -> None:
    proposals = session.scalars(
        select(DecisionProposalRecord).where(
            DecisionProposalRecord.workspace_id == workspace_id,
            DecisionProposalRecord.experiment_id == experiment_id,
            DecisionProposalRecord.experiment_version_id == experiment_version_id,
        )
    ).all()

    for proposal in proposals:
        latest_event = session.scalar(
            select(DecisionProposalStateEventRecord)
            .where(
                DecisionProposalStateEventRecord.proposal_id == proposal.id,
            )
            .order_by(DecisionProposalStateEventRecord.sequence.desc())
            .limit(1)
        )
        if latest_event is None:
            continue
        if latest_event.event_type in ("consumed", "invalidated"):
            continue

        next_seq = latest_event.sequence + 1
        session.add(DecisionProposalStateEventRecord(
            id=new_id("dpse"),
            workspace_id=workspace_id,
            proposal_id=proposal.id,
            event_type="invalidated",
            sequence=next_seq,
            reason=f"irreversible:{event.upstream_event_type}",
            evidence_json={"eligibility_event_id": event.id},
        ))
    session.flush()


def rebuild_projection(
    session: Session,
    *,
    workspace_id: str,
    experiment_id: str,
    experiment_version_id: str,
    level: str,
    candidate_id: str | None = None,
) -> ExperimentEligibilityProjectionRecord:
    target_id = candidate_id or experiment_version_id
    initial_hash = _initial_head_hash(level, target_id)

    filters = [
        ExperimentEligibilityEventRecord.workspace_id == workspace_id,
        ExperimentEligibilityEventRecord.experiment_id == experiment_id,
        ExperimentEligibilityEventRecord.experiment_version_id == experiment_version_id,
    ]
    if level == "candidate":
        filters.append(ExperimentEligibilityEventRecord.candidate_id == candidate_id)
    else:
        filters.append(ExperimentEligibilityEventRecord.candidate_id.is_(None))

    events = session.scalars(
        select(ExperimentEligibilityEventRecord)
        .where(*filters)
        .order_by(
            ExperimentEligibilityEventRecord.observed_at,
            ExperimentEligibilityEventRecord.id,
        )
    ).all()

    eligibility = "eligible"
    irreversible = False
    head_hash = initial_hash
    head_event_id = None
    seq = 0

    for ev in events:
        is_restoration = ev.upstream_event_type in RESTORATION_EVENT_TYPES
        eligibility = "eligible" if is_restoration else "ineligible"
        is_irrev = ev.upstream_event_type in IRREVERSIBLE_EVENT_TYPES
        irreversible = irreversible or is_irrev
        head_hash = _compute_head_hash(head_hash, ev.id, ev.upstream_event_type, eligibility)
        head_event_id = ev.id
        seq += 1

    proj_filters = [
        ExperimentEligibilityProjectionRecord.workspace_id == workspace_id,
        ExperimentEligibilityProjectionRecord.experiment_id == experiment_id,
        ExperimentEligibilityProjectionRecord.experiment_version_id == experiment_version_id,
        ExperimentEligibilityProjectionRecord.level == level,
    ]
    if level == "candidate":
        proj_filters.append(
            ExperimentEligibilityProjectionRecord.candidate_id == candidate_id
        )
    else:
        proj_filters.append(
            ExperimentEligibilityProjectionRecord.candidate_id.is_(None)
        )

    proj = session.scalar(
        select(ExperimentEligibilityProjectionRecord).where(*proj_filters)
    )
    if proj is None:
        proj = ExperimentEligibilityProjectionRecord(
            id=new_id("eproj"),
            workspace_id=workspace_id,
            experiment_id=experiment_id,
            experiment_version_id=experiment_version_id,
            level=level,
            candidate_id=candidate_id,
            current_eligibility=eligibility,
            irreversible_active=irreversible,
            head_event_id=head_event_id,
            head_hash=head_hash,
            head_sequence=seq,
        )
        session.add(proj)
        session.flush()
    else:
        session.execute(
            update(ExperimentEligibilityProjectionRecord)
            .where(ExperimentEligibilityProjectionRecord.id == proj.id)
            .values(
                current_eligibility=eligibility,
                irreversible_active=irreversible,
                head_event_id=head_event_id,
                head_hash=head_hash,
                head_sequence=seq,
                updated_at=utcnow(),
            )
        )
        session.refresh(proj)

    return proj


def is_effectively_eligible(
    session: Session,
    *,
    workspace_id: str,
    experiment_id: str,
    experiment_version_id: str,
) -> tuple[bool, dict]:
    version_proj = session.scalar(
        select(ExperimentEligibilityProjectionRecord).where(
            ExperimentEligibilityProjectionRecord.workspace_id == workspace_id,
            ExperimentEligibilityProjectionRecord.experiment_id == experiment_id,
            ExperimentEligibilityProjectionRecord.experiment_version_id == experiment_version_id,
            ExperimentEligibilityProjectionRecord.level == "version",
        )
    )

    candidate_projs = session.scalars(
        select(ExperimentEligibilityProjectionRecord).where(
            ExperimentEligibilityProjectionRecord.workspace_id == workspace_id,
            ExperimentEligibilityProjectionRecord.experiment_id == experiment_id,
            ExperimentEligibilityProjectionRecord.experiment_version_id == experiment_version_id,
            ExperimentEligibilityProjectionRecord.level == "candidate",
        )
    ).all()

    evidence: dict = {"version_eligible": True, "candidates": {}}
    all_eligible = True

    if version_proj is not None:
        if version_proj.current_eligibility != "eligible":
            all_eligible = False
            evidence["version_eligible"] = False
        if version_proj.irreversible_active:
            all_eligible = False
            evidence["version_irreversible"] = True

    for cp in candidate_projs:
        cand_ok = cp.current_eligibility == "eligible" and not cp.irreversible_active
        evidence["candidates"][cp.candidate_id] = {
            "eligible": cand_ok,
            "irreversible": cp.irreversible_active,
        }
        if not cand_ok:
            all_eligible = False

    return all_eligible, evidence
