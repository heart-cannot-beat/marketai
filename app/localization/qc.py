"""Localization deterministic QC module (#24, spec §2G/§2H, §3H, §16.7).

Spec: docs/superpowers/specs/2026-09-06-localization-truth-bounded-qc-design.md
§2G ("`localization.run_deterministic_qc` | none | locked Fact/number/
currency/Claim-decision checks"), §2H/§3H ("There is no QC waiver and no
`AttemptOutput.qc_passed` field ... adoption eligibility is derived from
the complete LocalizationQCRecord chain"), §16.7 (claims/locked-copy/QC
acceptance evidence), §1.8 (`localization_qc_records` -- append-only,
`UNIQUE(workspace_id, attempt_id, check_slot_key)`).

Every check this module runs is local: no provider call, no OCR, no image
inspection. It re-derives what a finalized Attempt's copy *should* contain
straight from the same durable, already-validated inputs `copy.py` used
to build `LocalizedCopyVersionRecord.content_json` -- the frozen Claim
decisions and the Facts they resolved to -- and compares that "expected"
side against an "observed" side describing what the deterministic edit
step actually rendered. `DeterministicQCInput`/`ObservedSegment` are how a
caller supplies that observed side; when a claim's observed segment is
omitted, this module assumes the edit rendered it exactly as frozen (the
happy path), so a QC record is still appended -- unconditionally -- for
every slot in the frozen plan (spec §16.7: "structured QC feedback ... is
included in the frozen input evidence").

Public entry points:

- `get_frozen_qc_plan()` derives the exact set of deterministic check
  slots for one Item's frozen Claim decisions and market QC configuration
  (`MarketPolicyVersionRecord.qc_rules_json` -- see below). Two calls for
  the same Item always produce the same plan; it never writes.
- `run_deterministic_qc()` evaluates every slot in that plan for one
  Attempt and appends one `LocalizationQcRecordRecord` per slot that does
  not already have one -- the append-only "idempotent fold": re-running it
  for the same Attempt with the same observed evidence never creates a
  second record for an already-recorded slot (`UNIQUE(workspace_id,
  attempt_id, check_slot_key)` both enforces and names this).
- `derive_qc_eligibility()` reads back the current `LocalizationQcRecordRecord`
  chain for one Attempt and reports live whether every blocking slot in
  the frozen plan has a passing record. There is deliberately no stored
  `qc_passed` column anywhere in this module or its ORM records (spec
  §3H) -- eligibility is recomputed from evidence on every call, and a
  missing blocking check is exactly as ineligible as a failed one. There
  is no waiver path: the only way a blocking slot's ineligibility clears
  is a later, still-append-only QC record for that same slot recording a
  pass (which `run_deterministic_qc()` itself refuses once a record for
  that slot already exists) -- practically, a blocking failure is
  resolved only by a new Attempt.
- `frozen_external_qc_steps()` / `passed_external_qc_steps()` /
  `missing_external_qc_steps()` / `external_qc_steps_all_passed()` answer
  the same question for the *external* half of the frozen plan, which
  lives on the Batch's `frozen_recipe_json` rather than in the
  deterministic slot plan above. They exist here, rather than in either
  caller, because the Worker's finalize gate and
  `app.localization.batch.authorize_isolated_qc`/`adopt_isolated_output`
  must agree exactly on which steps an Attempt still owes -- two copies of
  that answer is how the recovery path drifts from the normal one.

`qc_rules_json` shape (this module's own contract for that opaque
per-`MarketPolicyVersionRecord` JSON column -- spec §1.2 declares the
column but leaves its content to the consuming module):

    {
        "forbidden_terms": ["guaranteed", ...],       # blocking
        "warning_terms": ["amazing", ...],             # non-blocking
        "commercial_claim_keywords": ["lifetime warranty", ...],  # blocking
    }

Deterministic check kinds (spec §1.8's `check_kind` examples):

- `locked_fact` -- one slot per `replace` Claim decision: the observed
  segment's `value_json` must equal the current `TruthFactRecord.value_json`
  it was locked to. Any mismatch is blocking (spec §16.7: "wrong number,
  currency, unit, certification or delivery range is blocking").
- `number_match` / `currency_match` -- narrower slots over the same
  `replace` decision when the locked Fact's value carries an `amount` /
  `currency` key, so a caller can isolate exactly which half of a
  price-shaped Fact diverged.
- `claim_decision` -- one slot per Claim decision of any action: the
  observed segment kind must match what that action requires
  (`replace` -> `locked_replace`, `remove` -> `removed` with its region
  reported clear, `preserve` -> `text`).
- `placeholder_count` -- one slot per Attempt: the number of segments
  actually rendered as locked placeholders must equal the frozen count
  (spec §16.7: "missing, duplicated, reordered-invalid or additional
  placeholders fail validation").
- `forbidden_term` -- one slot per configured term (`forbidden_terms` is
  blocking, `warning_terms` is a `severity="warning"` slot using the same
  check kind -- spec §16.7: "non-blocking warnings remain visible for
  human review").
- `new_commercial_claim` -- one slot per configured `commercial_claim_keywords`
  entry: any occurrence in the observed text is an unsupported new claim
  and is always blocking.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    LocalizationAttemptOutputRecord,
    LocalizationAttemptRecord,
    LocalizationClaimDecisionRecord,
    LocalizationItemRecord,
    LocalizationQcRecordRecord,
    LocalizedCopyVersionRecord,
    MarketPolicyVersionRecord,
    TruthFactRecord,
    new_id,
)
from ..platform.tenancy import TenantContext

DEFAULT_RULE_VERSION = "qc-rule-v1"

SEVERITIES = ("blocking", "warning")
RESULTS = ("pass", "fail")

_EXPECTED_SEGMENT_KIND = {"replace": "locked_replace", "remove": "removed", "preserve": "text"}


class QCError(RuntimeError):
    """Raised for malformed input the public interface should never let a
    well-behaved caller construct (e.g. an Attempt with no isolated
    output yet, or an Item whose CopyVersion is missing)."""


def _stringify(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


# ---------------------------------------------------------------------------
# Caller-supplied "observed" evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservedSegment:
    """What actually got produced for one Claim's segment, as reported by
    the deterministic edit step -- never OCR (spec §2G: `run_deterministic_qc`
    makes no external call). Any field left `None` falls back to the
    frozen `LocalizedCopyVersionRecord` segment itself, i.e. the happy
    path where the edit rendered exactly what was locked."""

    kind: str | None = None
    value_json: object | None = None
    text: str | None = None
    region_clear: bool | None = None


@dataclass(frozen=True)
class DeterministicQCInput:
    """The observed side of every deterministic check for one Attempt.

    `observed_segments` overrides individual Claim segments by
    `claim_index`; any Claim absent here is assumed to have rendered
    exactly as frozen. `extra_placeholder_count` reports spurious locked
    placeholders detected beyond every known Claim's segment (a
    duplicated or additional placeholder -- spec §16.7). `full_text_override`,
    when given, replaces the assembled `preserve`-segment text used by the
    `forbidden_term`/`new_commercial_claim` scans; omitted, that text is
    assembled from the frozen/observed `preserve` segments themselves.
    """

    observed_segments: Mapping[int, ObservedSegment] = field(default_factory=dict)
    extra_placeholder_count: int = 0
    full_text_override: str | None = None


# ---------------------------------------------------------------------------
# Frozen QC plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QCPlanSlot:
    check_kind: str
    check_slot_key: str
    severity: str


@dataclass(frozen=True)
class FrozenQCPlan:
    item_id: str
    slots: tuple[QCPlanSlot, ...]

    @property
    def slot_keys(self) -> tuple[str, ...]:
        return tuple(s.check_slot_key for s in self.slots)


def _load_item(session: Session, ctx: TenantContext, item_id: str, batch_id: str) -> LocalizationItemRecord:
    item = session.scalar(
        select(LocalizationItemRecord).where(
            LocalizationItemRecord.workspace_id == ctx.workspace_id,
            LocalizationItemRecord.id == item_id,
            LocalizationItemRecord.batch_id == batch_id,
        )
    )
    if item is None:
        raise QCError("missing_item")
    return item


def _load_attempt(
    session: Session, ctx: TenantContext, attempt_id: str, item_id: str, batch_id: str
) -> LocalizationAttemptRecord:
    attempt = session.scalar(
        select(LocalizationAttemptRecord).where(
            LocalizationAttemptRecord.workspace_id == ctx.workspace_id,
            LocalizationAttemptRecord.id == attempt_id,
            LocalizationAttemptRecord.item_id == item_id,
            LocalizationAttemptRecord.batch_id == batch_id,
        )
    )
    if attempt is None:
        raise QCError("missing_attempt")
    return attempt


def _load_decisions(
    session: Session, ctx: TenantContext, decision_set_id: str
) -> list[LocalizationClaimDecisionRecord]:
    return list(
        session.scalars(
            select(LocalizationClaimDecisionRecord)
            .where(
                LocalizationClaimDecisionRecord.workspace_id == ctx.workspace_id,
                LocalizationClaimDecisionRecord.decision_set_id == decision_set_id,
            )
            .order_by(LocalizationClaimDecisionRecord.claim_index)
        ).all()
    )


def get_frozen_qc_plan(
    session: Session, ctx: TenantContext, *, item_id: str, batch_id: str
) -> FrozenQCPlan:
    """Derive the exact deterministic check slots for one Item -- purely
    from its frozen Claim decisions, the Facts they resolved to, and its
    target market's `qc_rules_json`. Read-only; never writes.

    Two calls for the same Item (and unchanged upstream evidence) always
    return the identical plan -- `run_deterministic_qc()` relies on that
    to know which slots are outstanding, and `derive_qc_eligibility()`
    relies on it to know which slots are required.
    """
    item = _load_item(session, ctx, item_id, batch_id)
    decisions = _load_decisions(session, ctx, item.claim_decision_set_id)
    market_policy = session.get(MarketPolicyVersionRecord, item.market_policy_version_id)
    qc_rules = (market_policy.qc_rules_json or {}) if market_policy is not None else {}

    slots: list[QCPlanSlot] = []
    for decision in decisions:
        slots.append(
            QCPlanSlot("claim_decision", f"claim_decision:{decision.claim_index}", "blocking")
        )
        if decision.action == "replace":
            fact = session.get(TruthFactRecord, decision.replacement_fact_id)
            value = fact.value_json if fact is not None else None
            slots.append(
                QCPlanSlot("locked_fact", f"locked_fact:{decision.claim_index}", "blocking")
            )
            if isinstance(value, dict) and "amount" in value:
                slots.append(
                    QCPlanSlot("number_match", f"number_match:{decision.claim_index}", "blocking")
                )
            if isinstance(value, dict) and "currency" in value:
                slots.append(
                    QCPlanSlot("currency_match", f"currency_match:{decision.claim_index}", "blocking")
                )

    slots.append(QCPlanSlot("placeholder_count", "placeholder_count", "blocking"))

    for term in sorted(qc_rules.get("forbidden_terms") or []):
        slots.append(
            QCPlanSlot("forbidden_term", f"forbidden_term:blocking:{term}", "blocking")
        )
    for term in sorted(qc_rules.get("warning_terms") or []):
        slots.append(
            QCPlanSlot("forbidden_term", f"forbidden_term:warning:{term}", "warning")
        )
    for keyword in sorted(qc_rules.get("commercial_claim_keywords") or []):
        slots.append(
            QCPlanSlot("new_commercial_claim", f"new_commercial_claim:{keyword}", "blocking")
        )

    return FrozenQCPlan(item_id=item_id, slots=tuple(slots))


# ---------------------------------------------------------------------------
# run_deterministic_qc
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeterministicQCReport:
    attempt_id: str
    records: tuple[LocalizationQcRecordRecord, ...]

    @property
    def all_blocking_passed(self) -> bool:
        return all(r.result == "pass" for r in self.records if r.severity == "blocking")


def _assembled_text(segments_by_index: Mapping[int, dict], observed: DeterministicQCInput) -> str:
    if observed.full_text_override is not None:
        return observed.full_text_override
    parts: list[str] = []
    for claim_index in sorted(segments_by_index):
        segment = segments_by_index[claim_index]
        if segment.get("kind") != "text":
            continue
        override = observed.observed_segments.get(claim_index)
        text = override.text if override is not None and override.text is not None else segment.get("text", "")
        parts.append(text)
    return " ".join(parts)


def _evaluate_slot(
    slot: QCPlanSlot,
    *,
    segments_by_index: Mapping[int, dict],
    decisions_by_index: Mapping[int, LocalizationClaimDecisionRecord],
    facts_by_claim_index: Mapping[int, TruthFactRecord | None],
    observed: DeterministicQCInput,
    assembled_text: str,
    workspace_id: str,
    attempt_id: str,
    item_id: str,
    batch_id: str,
    rule_version: str,
    now: datetime,
) -> LocalizationQcRecordRecord:
    kind = slot.check_kind

    if kind == "claim_decision":
        claim_index = int(slot.check_slot_key.split(":", 1)[1])
        decision = decisions_by_index[claim_index]
        segment = segments_by_index.get(claim_index, {})
        expected_kind = _EXPECTED_SEGMENT_KIND[decision.action]
        override = observed.observed_segments.get(claim_index)
        observed_kind = (
            override.kind if override is not None and override.kind is not None else segment.get("kind")
        )
        region_clear = True
        if decision.action == "remove" and override is not None and override.region_clear is False:
            region_clear = False
        result = "pass" if (observed_kind == expected_kind and region_clear) else "fail"
        expected_json = {"claim_index": claim_index, "action": decision.action, "expected_kind": expected_kind}
        observed_json = {"observed_kind": observed_kind, "region_clear": region_clear}

    elif kind == "locked_fact":
        claim_index = int(slot.check_slot_key.split(":", 1)[1])
        fact = facts_by_claim_index.get(claim_index)
        expected_value = fact.value_json if fact is not None else None
        segment = segments_by_index.get(claim_index, {})
        override = observed.observed_segments.get(claim_index)
        observed_value = (
            override.value_json
            if override is not None and override.value_json is not None
            else segment.get("value_json")
        )
        result = "pass" if observed_value == expected_value else "fail"
        expected_json = {
            "claim_index": claim_index,
            "replacement_fact_id": fact.id if fact is not None else None,
            "value_json": expected_value,
        }
        observed_json = {"value_json": observed_value}

    elif kind == "number_match":
        claim_index = int(slot.check_slot_key.split(":", 1)[1])
        fact = facts_by_claim_index.get(claim_index)
        expected_amount = (
            _stringify((fact.value_json or {}).get("amount")) if fact is not None else None
        )
        segment = segments_by_index.get(claim_index, {})
        override = observed.observed_segments.get(claim_index)
        observed_value = (
            override.value_json
            if override is not None and override.value_json is not None
            else segment.get("value_json")
        )
        observed_amount = (
            _stringify(observed_value.get("amount")) if isinstance(observed_value, dict) else None
        )
        result = "pass" if observed_amount == expected_amount else "fail"
        expected_json = {"claim_index": claim_index, "amount": expected_amount}
        observed_json = {"amount": observed_amount}

    elif kind == "currency_match":
        claim_index = int(slot.check_slot_key.split(":", 1)[1])
        fact = facts_by_claim_index.get(claim_index)
        expected_currency = (fact.value_json or {}).get("currency") if fact is not None else None
        segment = segments_by_index.get(claim_index, {})
        override = observed.observed_segments.get(claim_index)
        observed_value = (
            override.value_json
            if override is not None and override.value_json is not None
            else segment.get("value_json")
        )
        observed_currency = (
            observed_value.get("currency") if isinstance(observed_value, dict) else None
        )
        result = "pass" if observed_currency == expected_currency else "fail"
        expected_json = {"claim_index": claim_index, "currency": expected_currency}
        observed_json = {"currency": observed_currency}

    elif kind == "placeholder_count":
        expected_count = sum(1 for seg in segments_by_index.values() if seg.get("kind") == "locked_replace")
        observed_count = 0
        for claim_index, segment in segments_by_index.items():
            override = observed.observed_segments.get(claim_index)
            observed_kind = (
                override.kind if override is not None and override.kind is not None else segment.get("kind")
            )
            if observed_kind == "locked_replace":
                observed_count += 1
        observed_count += observed.extra_placeholder_count
        result = "pass" if observed_count == expected_count else "fail"
        expected_json = {"count": expected_count}
        observed_json = {"count": observed_count}

    elif kind == "forbidden_term":
        _, category, term = slot.check_slot_key.split(":", 2)
        found = term.lower() in assembled_text.lower()
        result = "fail" if found else "pass"
        expected_json = {"term": term, "policy": category}
        observed_json = {"found": found}

    elif kind == "new_commercial_claim":
        _, keyword = slot.check_slot_key.split(":", 1)
        found = keyword.lower() in assembled_text.lower()
        result = "fail" if found else "pass"
        expected_json = {"keyword": keyword}
        observed_json = {"found": found}

    else:  # pragma: no cover -- defensive; get_frozen_qc_plan only emits known kinds
        raise QCError(f"unknown_check_kind:{kind}")

    return LocalizationQcRecordRecord(
        id=new_id("lqr"),
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        item_id=item_id,
        batch_id=batch_id,
        check_kind=kind,
        check_slot_key=slot.check_slot_key,
        expected_json=expected_json,
        observed_json=observed_json,
        evidence_location_json=None,
        severity=slot.severity,
        result=result,
        rule_version=rule_version,
        created_at=now,
    )


def run_deterministic_qc(
    session: Session,
    ctx: TenantContext,
    *,
    attempt_id: str,
    item_id: str,
    batch_id: str,
    observed: DeterministicQCInput | None = None,
    rule_version: str = DEFAULT_RULE_VERSION,
    now: datetime | None = None,
) -> DeterministicQCReport:
    """`localization.run_deterministic_qc` (spec §2G): "locked Fact/number/
    currency/Claim-decision checks", no external call. Appends one
    `LocalizationQcRecordRecord` per outstanding slot in
    `get_frozen_qc_plan()`; a slot that already has a record from an
    earlier call is left untouched and its existing record is returned
    instead -- the "idempotent fold per check slot" (spec §1.8:
    `UNIQUE(workspace_id, attempt_id, check_slot_key)` -- "idempotent fold
    yields one authoritative result per frozen check slot").

    Requires an immutable isolated `LocalizationAttemptOutputRecord` to
    already exist for this Attempt (spec §2G: "Run deterministic QC
    first" happens only after a successful edit response is persisted).
    """
    now = now or datetime.now(timezone.utc)
    observed = observed or DeterministicQCInput()

    _load_attempt(session, ctx, attempt_id, item_id, batch_id)
    output = session.scalar(
        select(LocalizationAttemptOutputRecord).where(
            LocalizationAttemptOutputRecord.workspace_id == ctx.workspace_id,
            LocalizationAttemptOutputRecord.attempt_id == attempt_id,
        )
    )
    if output is None:
        raise QCError("no_attempt_output")

    item = _load_item(session, ctx, item_id, batch_id)
    if item.copy_version_id is None:
        raise QCError("missing_copy_version_on_item")
    copy_version = session.get(LocalizedCopyVersionRecord, item.copy_version_id)
    if copy_version is None:
        raise QCError("missing_copy_version")

    decisions = _load_decisions(session, ctx, item.claim_decision_set_id)
    decisions_by_index = {d.claim_index: d for d in decisions}
    segments_by_index = {
        seg["claim_index"]: seg for seg in copy_version.content_json.get("segments", [])
    }
    facts_by_claim_index = {
        d.claim_index: session.get(TruthFactRecord, d.replacement_fact_id)
        for d in decisions
        if d.action == "replace"
    }

    assembled_text = _assembled_text(segments_by_index, observed)
    plan = get_frozen_qc_plan(session, ctx, item_id=item_id, batch_id=batch_id)

    records: list[LocalizationQcRecordRecord] = []
    for slot in plan.slots:
        existing = session.scalar(
            select(LocalizationQcRecordRecord).where(
                LocalizationQcRecordRecord.workspace_id == ctx.workspace_id,
                LocalizationQcRecordRecord.attempt_id == attempt_id,
                LocalizationQcRecordRecord.check_slot_key == slot.check_slot_key,
            )
        )
        if existing is not None:
            records.append(existing)
            continue

        record = _evaluate_slot(
            slot,
            segments_by_index=segments_by_index,
            decisions_by_index=decisions_by_index,
            facts_by_claim_index=facts_by_claim_index,
            observed=observed,
            assembled_text=assembled_text,
            workspace_id=ctx.workspace_id,
            attempt_id=attempt_id,
            item_id=item_id,
            batch_id=batch_id,
            rule_version=rule_version,
            now=now,
        )
        session.add(record)
        session.flush()
        records.append(record)

    return DeterministicQCReport(attempt_id=attempt_id, records=tuple(records))


# ---------------------------------------------------------------------------
# derive_qc_eligibility
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QCEligibility:
    """Live-derived eligibility over the current `LocalizationQcRecordRecord`
    chain for one Attempt -- never a stored `qc_passed` flag (spec §3H).
    A blocking slot missing its record is exactly as ineligible as one
    whose record failed; there is no waiver that clears either without a
    new, still-append-only passing record for that same slot."""

    attempt_id: str
    frozen_slot_keys: tuple[str, ...]
    recorded_slot_keys: tuple[str, ...]
    missing_slot_keys: tuple[str, ...]
    blocking_failure_slot_keys: tuple[str, ...]
    warning_failure_slot_keys: tuple[str, ...]

    @property
    def is_eligible(self) -> bool:
        return not self.missing_slot_keys and not self.blocking_failure_slot_keys


def frozen_external_qc_steps(batch: LocalizationBatchRecord) -> tuple[str, ...]:
    """The external QC steps the Batch froze at `confirm()` time.

    The frozen recipe is the only authority here: a step added to the
    product after a Batch was confirmed is not part of that Batch's plan,
    and a step removed from it does not stop being required for Batches
    already running under it.
    """
    steps = (batch.frozen_recipe_json or {}).get("external_qc_steps") or []
    return tuple(str(step) for step in steps)


def passed_external_qc_steps(
    session: Session, ctx: TenantContext, *, attempt_id: str
) -> frozenset[str]:
    """The frozen external steps that have actually recorded a ``pass``."""
    records = session.scalars(
        select(LocalizationQcRecordRecord).where(
            LocalizationQcRecordRecord.workspace_id == ctx.workspace_id,
            LocalizationQcRecordRecord.attempt_id == attempt_id,
        )
    ).all()
    return frozenset(
        record.check_kind for record in records if record.result == "pass"
    )


def missing_external_qc_steps(
    session: Session, ctx: TenantContext, *, batch: LocalizationBatchRecord,
    attempt_id: str,
) -> tuple[str, ...]:
    """Frozen external steps this Attempt has not yet passed, in plan order.

    This is what `authorize_isolated_qc()` may admit and what
    `adopt_isolated_output()` refuses adoption over. Re-authorizing a step
    that already passed would re-run (and, for a chargeable step, re-bill)
    a check whose evidence is already durable.
    """
    passed = passed_external_qc_steps(session, ctx, attempt_id=attempt_id)
    return tuple(
        step for step in frozen_external_qc_steps(batch) if step not in passed
    )


def external_qc_steps_all_passed(
    session: Session, ctx: TenantContext, *, batch: LocalizationBatchRecord,
    attempt_id: str,
) -> bool:
    return not missing_external_qc_steps(
        session, ctx, batch=batch, attempt_id=attempt_id
    )


def derive_qc_eligibility(
    session: Session, ctx: TenantContext, *, attempt_id: str, item_id: str, batch_id: str
) -> QCEligibility:
    """Recompute QC eligibility for one Attempt from its frozen plan and
    its current QC evidence -- both re-read on every call. Adoption
    (`app.localization.batch.adopt_isolated_output`) is refused outright
    while `is_eligible` is False; nothing here can be waived."""
    plan = get_frozen_qc_plan(session, ctx, item_id=item_id, batch_id=batch_id)
    records = session.scalars(
        select(LocalizationQcRecordRecord).where(
            LocalizationQcRecordRecord.workspace_id == ctx.workspace_id,
            LocalizationQcRecordRecord.attempt_id == attempt_id,
        )
    ).all()
    by_slot = {r.check_slot_key: r for r in records}

    missing = tuple(key for key in plan.slot_keys if key not in by_slot)
    blocking_failures = tuple(
        slot.check_slot_key
        for slot in plan.slots
        if slot.severity == "blocking"
        and slot.check_slot_key in by_slot
        and by_slot[slot.check_slot_key].result == "fail"
    )
    warning_failures = tuple(
        slot.check_slot_key
        for slot in plan.slots
        if slot.severity == "warning"
        and slot.check_slot_key in by_slot
        and by_slot[slot.check_slot_key].result == "fail"
    )

    return QCEligibility(
        attempt_id=attempt_id,
        frozen_slot_keys=plan.slot_keys,
        recorded_slot_keys=tuple(sorted(by_slot)),
        missing_slot_keys=missing,
        blocking_failure_slot_keys=blocking_failures,
        warning_failure_slot_keys=warning_failures,
    )
