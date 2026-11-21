"""Automatic checks of ads-first ad drafts (#197; ADR 0018, design doc §5.3 and §6).

Once its image is made, each ad of ads-first ideas can get a single-image check: the image against the
intent it was made from -- the ideas' combination, claim and way of telling, the product understanding as
it was then, and the facts the ad cites (``ai_qc.ideas_input``). No round is involved and nothing is
compared with other ads; comparisons wait for a frozen round (step 2). The result is a suggestion and
never records a quality approval. Each check records the understanding snapshot it was made against, so
whether it still answers the facts the user later checks can be told from the record.

A check is an ordinary paid call in ``marketing_ai_qc_calls`` with ``project_id`` set: the API admits it
under a quote the user saw, with its own Operation, single-attempt Job and reservation; the Worker
executes and settles it (``ai_qc_worker``). A call whose outcome is uncertain is never sent again, and an
image made in Mock is checked only in Mock: the explicit example never stands in for a real check.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.generation.selection import SelectionError, check_budget
from app.platform.audit import append_event
from . import ai_qc, ideas
from . import ai_qc_text as text
from .ai_qc_models import MarketingAIQCRecord as Call
from .models import MarketingProjectRecord
from .service import MarketingError, digest, ensure_active

#: Checks still waiting for their outcome: never admitted again for the same image.
IN_FLIGHT = ("queued", "running", "uncertain")
#: At most this many images per start, as for a round.
MAX_CALLS = 60


def latest_by_asset(session, ctx, project_id) -> dict:
    """The newest check of each image of these ideas."""
    latest = {}
    for call in session.scalars(select(Call).where(Call.workspace_id == ctx.workspace_id, Call.project_id == project_id)
                                .order_by(Call.sequence.desc(), Call.created_at.desc(), Call.id.desc())).all():
        latest.setdefault(call.asset_version_id, call)
    return latest


def mode_of(version) -> str:
    """The mode an ad's check runs in: its image's own. An uploaded image (#200) has none -- it is the
    user's real material, so only a real check counts for it, never the Controlled Mock example."""
    return version.execution_mode or "byok"


def _ad(session, ctx, row, asset_version_id):
    for version, recipe in ideas.project_ads(session, ctx, row):
        if version.id == asset_version_id:
            return version, recipe
    raise MarketingError("ad_not_found", "这张广告不属于这些广告想法。", 404)


def quote_and_inputs(session, ctx, row, execution_mode, *, only=None):
    """The quote for checking these ideas' unchecked images made in ``execution_mode``, and the frozen inputs.

    An image counts when it has no check yet or its newest check failed with a known outcome. One with a
    check in flight or uncertain, or a finished one, is left alone: the image and the intent it is checked
    against never change, so a finished check stays its answer. An image that cannot be sent as it was
    made (for instance its product photo lost its rights) is listed in ``skipped`` with the reason.
    ``only`` limits the quote to those images (a first batch checks the images it made)."""
    ensure_active(session, ctx)
    cap = ai_qc.mode_capability(session, ctx, execution_mode)
    reason = cap["reason"]
    latest = latest_by_asset(session, ctx, row.id)
    candidates, pending, skipped = [], [], []
    for version, recipe in ideas.project_ads(session, ctx, row):
        if mode_of(version) != execution_mode or (only is not None and version.id not in only):
            continue
        previous = latest.get(version.id)
        if previous is not None and previous.state in IN_FLIGHT:
            pending.append(previous.id)
            continue
        if previous is not None and previous.state == "succeeded":
            continue
        try:
            inputs = ai_qc.ideas_input(session, ctx, row, version, recipe)
        except (MarketingError, SelectionError) as exc:
            skipped.append({"asset_version_id": version.id, "code": exc.code})
            continue
        candidates.append({"asset_version_id": version.id, "input": inputs, "input_hash": digest(inputs),
                           "retry_of_id": previous.id if previous else None,
                           "sequence": previous.sequence + 1 if previous else 1})
    candidates = sorted(candidates, key=lambda item: item["asset_version_id"])[:MAX_CALLS]
    if not candidates:
        reason = reason or ("checks_in_progress" if pending else "nothing_to_check")
    total = Decimal(cap["unit_maximum"]) * len(candidates)
    if execution_mode == "byok" and not reason:
        try:
            check_budget(session, ctx, total)
        except SelectionError as exc:
            reason = exc.code
    fingerprint = digest({"project_id": row.id, "mode": execution_mode, "provider": cap["provider"], "model": cap["model"],
                          "maximum": cap["unit_maximum"], "policy": ai_qc.IDEAS_POLICY,
                          "inputs": [{k: item[k] for k in ("asset_version_id", "input_hash", "retry_of_id")}
                                     for item in candidates]})
    return {"available": reason is None, "unavailable_reason": reason, "execution_mode": execution_mode,
            "provider": cap["provider"], "model": cap["model"], "quantity": len(candidates), "maximum_cost": str(total),
            "currency": "USD", "quote_hash": fingerprint, "asset_version_ids": [c["asset_version_id"] for c in candidates],
            "skipped": skipped}, candidates, cap


def _lock(session, ctx, row) -> None:
    """Serialize admissions on one ideas, so two starts never admit the same image twice."""
    session.execute(select(MarketingProjectRecord.id).where(MarketingProjectRecord.workspace_id == ctx.workspace_id,
                                                            MarketingProjectRecord.id == row.id).with_for_update())


def _admit(session, ctx, row, execution_mode, candidates, cap, details) -> list:
    records = [ai_qc.admit_call(session, ctx, item, cap, kind="single", policy=ai_qc.IDEAS_POLICY,
                                execution_mode=execution_mode, project_id=row.id) for item in candidates]
    if records:
        append_event(session, event_type="marketing.ideas_checks_requested", resource_type="marketing_project",
                     resource_id=row.id, details={"call_ids": [r.id for r in records], "execution_mode": execution_mode,
                                                  **details})
    session.flush()
    return records


def start(session, ctx, row, execution_mode: str, quote_hash: str) -> list:
    """Admit one check per image the confirmed quote counted, in this transaction (design doc §8).

    Without the workspace's key nothing is admitted (``checks_unavailable`` with ``credential_required``)."""
    _lock(session, ctx, row)
    quoted, candidates, cap = quote_and_inputs(session, ctx, row, execution_mode)
    if not quoted["available"]:
        raise MarketingError("checks_unavailable", "现在不能检查这些广告，请先处理提示的问题。", 409,
                             params={"reason": quoted["unavailable_reason"]})
    if quoted["quote_hash"] != quote_hash:
        raise MarketingError("checks_quote_changed", "要检查的广告或费用已变化，请刷新后确认新的最高费用。", 409)
    return _admit(session, ctx, row, execution_mode, candidates, cap,
                  {"quote_hash": quote_hash, "maximum_cost": quoted["maximum_cost"]})


def admit_for_first_batch(session, ctx, row, execution_mode: str, asset_version_ids, first_batch_id: str) -> list:
    """The first batch's check step (maintainer decision on #197): the checks of the images the batch made.

    The batch's advance admits them in the Worker, under the total the user confirmed when starting, so
    there is no quote of its own; the caller has checked that their maxima fit in that total. Images
    whose check is already in flight are left to it. ``MarketingError`` when an image cannot be checked
    as it was made, or no check can be admitted now (the reason is the error's code)."""
    _lock(session, ctx, row)
    quoted, candidates, cap = quote_and_inputs(session, ctx, row, execution_mode, only=set(asset_version_ids))
    if quoted["skipped"]:
        raise MarketingError(quoted["skipped"][0]["code"], "这张广告不能按出图时的样子检查。", 409,
                             params={"asset_version_id": quoted["skipped"][0]["asset_version_id"]})
    if quoted["unavailable_reason"] not in (None, "checks_in_progress", "nothing_to_check"):
        raise MarketingError(quoted["unavailable_reason"], "现在不能检查这些广告。", 409)
    return _admit(session, ctx, row, execution_mode, candidates, cap,
                  {"first_batch_id": first_batch_id, "maximum_cost": quoted["maximum_cost"]})


def input_is_current(session, ctx, call) -> bool:
    """Whether the call's frozen input is still what its image would be sent with now (the Worker's last check)."""
    try:
        project = ideas.get_ideas_project(session, ctx, call.project_id)
        version, recipe = _ad(session, ctx, project, call.asset_version_id)
        return call.input_hash == digest(ai_qc.ideas_input(session, ctx, project, version, recipe, policy=call.policy_version))
    except (MarketingError, SelectionError):
        return False


def projection(call) -> dict:
    """The ``IdeasAdCheck`` of a call: only the findings that need attention, never the passes."""
    result = ai_qc.effective_report(call) if call.state == "succeeded" and call.result_json else None
    checks = call.input_json.get("required_checks") or {}
    findings = []
    for finding in (result or {}).get("findings", []):
        if finding["status"] == "pass":
            continue
        findings.append({"check_id": finding["check_id"], "description": checks.get(finding["check_id"], ""),
                         "status": finding["status"], "confidence": finding["confidence"],
                         "gate_reason": finding.get("gate_reason"), "visual_evidence": finding["visual_evidence"],
                         "rationale": finding["rationale"], "suggested_fix": finding.get("suggested_fix", ""),
                         "fact_ids": finding.get("fact_ids", []), "unsupported_claims": finding.get("unsupported_claims", []),
                         "rendered_text_diff": finding.get("rendered_text_diff", []),
                         "extra_text": finding.get("extra_text", [])})
    return {"id": call.id, "state": call.state, "execution_mode": call.execution_mode,
            "source": result["source"] if result else None,
            "suggestion": result["quality_suggestion"] if result else None, "findings": findings,
            "understanding_hash": call.input_json["understanding_hash"], "fact_ids": call.input_json["evidence_fact_ids"],
            "language": text.record_language(call.input_json), "maximum_cost": call.maximum_cost,
            "error_code": call.error_code, "created_at": call.created_at.isoformat() if call.created_at else ""}


def ad_checks(session, ctx, row) -> dict:
    """The newest check of each image of these ideas, as ``IdeasAdCheck``."""
    return {asset_id: projection(call) for asset_id, call in latest_by_asset(session, ctx, row.id).items()}
