"""Explicit downstream compatibility projection; marketing remains authoritative.

No old experiment is rewritten. These closed records carry a distinct
algorithm and origin, and every downstream admission rechecks current
marketing evidence in addition to its existing truth/delivery gates.
"""
from datetime import datetime, time, timedelta, timezone, date
from decimal import Decimal
from types import SimpleNamespace
from sqlalchemy import select
from pydantic import Field
from app.adapters.persistence import models as m
from .contracts import Document
from .service import MarketingError, digest, ensure_active
from . import workflow as w
from .evaluation import evaluate_snapshot, combination_key

POLICY = "marketing-handoff-v1"
MARKER = "marketing_evidence_handoff"


class CreateEvidenceHandoff(Document):
    evaluation_id: str = Field(min_length=1, max_length=64)
    asset_version_id: str = Field(min_length=1, max_length=64)
    offer_version_id: str = Field(min_length=1, max_length=64)
    confirms_tested_context: bool
    note: str = Field(min_length=1, max_length=2000)


def current_evidence(session, ctx, evaluation_id, asset_id, *, require_combination=False, _seen=None):
    seen = set(_seen or ())
    if evaluation_id in seen:
        raise MarketingError("marketing_evidence_cycle", "支持证据形成循环，不能作为新验证。", 409)
    seen.add(evaluation_id)
    evaluations = w.scoped(session, ctx, w.Evaluation, id=evaluation_id)
    if not evaluations:
        raise MarketingError("marketing_evidence_missing", "找不到这份营销评估。", 404)
    evaluation = evaluations[0]
    row = w.get_round(session, ctx, evaluation.round_id)
    latest = w.latest_evaluation(session, ctx, row)
    batches = w.active_batches(session, ctx, row)
    inputs = evaluation.input_json
    bindings = [{"entry_id": b.entry_id, "ad_id": b.ad_id, **b.conditions_json}
        for b in sorted(w.scoped(session, ctx, w.Binding, round_id=row.id), key=lambda b: b.id)]
    if (not w.evaluation_current(session, ctx, row, evaluation)
            or row.state not in ("evaluated", "closed") or row.execution_mode != "byok"
            or not latest or latest.id != evaluation.id or not batches
            or any(b.metadata_json.get("source_mode") != "customer_file" for b in batches)
            or sorted(inputs["batch_ids"]) != sorted(b.id for b in batches)
            or inputs["rows"] != sorted([item for b in batches for item in b.rows_json], key=lambda d: (d["date"], d["ad_id"]))
            or inputs["bindings"] != bindings or digest(inputs) != evaluation.input_hash
            or inputs["snapshot"] != row.snapshot_json or inputs["round_id"] != row.id
            or inputs["timezone"] != row.timezone or inputs["start_date"] != str(row.start_date)
            or inputs["end_date"] != str(row.end_date)):
        raise MarketingError("marketing_evidence_stale", "营销评估不是当前真实回传证据，请更正数据后重新评估。", 409)
    entries = {e["asset_version_id"]: e for e in inputs["entries"]}
    asset = session.scalar(select(m.AssetVersionRecord).where(m.AssetVersionRecord.workspace_id == ctx.workspace_id,
        m.AssetVersionRecord.id == asset_id))
    recalculated = evaluate_snapshot(inputs)
    if (not asset or asset.execution_mode != "byok" or asset_id not in entries
            or asset.content_sha256 != entries[asset_id]["content_sha256"]
            or not w.ready(session, ctx, row, asset_id)
            or asset_id not in recalculated["qualified_asset_version_ids"]
            or asset_id not in evaluation.result_json.get("qualified_asset_version_ids", [])):
        raise MarketingError("marketing_asset_not_qualified", "需要有成熟真实回传证据且当前质量通过的精确素材；点击率或相对最好不能替代业务达标。", 409)
    confirmations = [c for c in recalculated["confirmed_combinations"] if c["current_asset_version_id"] == asset_id]
    repeatable = False
    for c in confirmations:
        key = combination_key(row.snapshot_json, entries[asset_id])
        for support_id in c["supporting_evaluations"]:
            support_rows = w.scoped(session, ctx, w.Evaluation, id=support_id)
            if not support_rows:
                continue
            support = support_rows[0]
            prior = w.get_round(session, ctx, support.round_id)
            if prior.id == row.id or prior.project_id != row.project_id or prior.state != "closed":
                continue
            for entry in support.input_json["entries"]:
                if (entry["content_sha256"] == entries[asset_id]["content_sha256"]
                        or combination_key(prior.snapshot_json, entry) != key
                        or prior.timezone != row.timezone or prior.snapshot_json["protocol"] != row.snapshot_json["protocol"]):
                    continue
                try:
                    current_evidence(session, ctx, support.id, entry["asset_version_id"], _seen=seen)
                    repeatable = True
                except MarketingError:
                    continue
    if require_combination and not repeatable:
        raise MarketingError("marketing_combination_not_confirmed", "放量候选需要同一适用条件下的新执行和另一小轮的新增真实证据；单张广告成功只支持该素材。", 409)
    return row, evaluation, entries[asset_id], asset, repeatable


def _version(session, ctx, version_id, kind):
    pair = session.execute(select(m.LibraryVersionRecord, m.LibraryEntryRecord).join(m.LibraryEntryRecord,
        (m.LibraryEntryRecord.id == m.LibraryVersionRecord.entry_id) & (m.LibraryEntryRecord.workspace_id == ctx.workspace_id))
        .where(m.LibraryVersionRecord.workspace_id == ctx.workspace_id, m.LibraryVersionRecord.id == version_id,
            m.LibraryVersionRecord.entry_type == kind, m.LibraryVersionRecord.state == "effective",
            m.LibraryVersionRecord.eligibility != "blocked", m.LibraryEntryRecord.lifecycle == "active")).first()
    if not pair:
        raise MarketingError("handoff_commercial_version_unavailable", "产品或销售方案版本未发布、已撤回或不可用。", 409)
    return pair[0]


def commercial_context(session, ctx, row, offer_id):
    offer = _version(session, ctx, offer_id, "offer")
    content = offer.content_json
    product = _version(session, ctx, content.get("product_version_id"), "product")
    plan = row.snapshot_json["plan"]
    from app.localization.source_context import resolve_source_market_snapshot
    context, problem = resolve_source_market_snapshot(SimpleNamespace(market=plan["market"], placement=plan["placement"],
        frozen_snapshot_json={}), content)
    if problem or content.get("landing_page_url") != plan["landing_page"] or context["currency"] != plan["currency"]:
        raise MarketingError("handoff_context_mismatch", "销售方案需要完整源市场信息，并与测试的市场、版位、落地页和币种一致。", 409)
    if product.content_json.get("name") != row.snapshot_json["understanding"]["product_name"]:
        raise MarketingError("handoff_product_mismatch", "所选销售方案的产品与测试中的产品不一致。", 409)
    try:
        start = date.fromisoformat(content["valid_from"])
        end = date.fromisoformat(content["valid_to"]) if content.get("valid_to") else date.max
        if start > row.start_date or end < max(row.end_date, m.utcnow().date()) or plan["market"] not in content["market_scope"]:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise MarketingError("handoff_offer_not_applicable", "销售方案的市场或有效期未覆盖测试与当前使用。", 409) from None
    return product, offer, context


def origin(version):
    return (version.frozen_snapshot_json or {}).get(MARKER)


def check_projection(session, ctx, version, candidate, *, require_combination=False):
    proof = origin(version)
    if not proof:
        return
    if (proof.get("policy") != POLICY or not candidate or candidate.asset_version_id != proof.get("asset_version_id")
            or digest(version.frozen_snapshot_json) != version.frozen_snapshot_hash):
        raise MarketingError("marketing_handoff_invalid", "营销交接血缘不完整。", 409)
    row, evaluation, entry, asset, repeatable = current_evidence(session, ctx,
        proof["evaluation_id"], proof["asset_version_id"], require_combination=require_combination)
    product, offer, context = commercial_context(session, ctx, row, version.offer_version_id)
    if (evaluation.input_hash != proof["evaluation_input_hash"] or digest(evaluation.result_json) != proof["evaluation_result_hash"]
            or proof["round_id"] != row.id or product.id != version.product_version_id
            or proof.get("landing_page_version_id") != offer.id
            or context != version.frozen_snapshot_json.get("source_market_snapshot")):
        raise MarketingError("marketing_handoff_stale", "营销证据或适用条件已变化，请重新确认交接。", 409)


def options(session, ctx, row):
    evaluation = w.latest_evaluation(session, ctx, row) if row.state in ("evaluated", "closed") else None
    assets = []
    if evaluation:
        for asset_id in evaluation.result_json.get("qualified_asset_version_ids", []):
            try:
                _, _, entry, _, repeatable = current_evidence(session, ctx, evaluation.id, asset_id)
                assets.append({"asset_version_id": asset_id, "label": entry.get("display_name") or entry["ad_name"], "localization": True,
                    "scale": repeatable, "scope": "repeatable_combination" if repeatable else "exact_asset_only"})
            except MarketingError:
                continue
    offers = []
    if assets:
        versions = session.scalars(select(m.LibraryVersionRecord).where(m.LibraryVersionRecord.workspace_id == ctx.workspace_id,
            m.LibraryVersionRecord.entry_type == "offer", m.LibraryVersionRecord.state == "effective")).all()
        for offer in versions:
            try:
                product, offer, context = commercial_context(session, ctx, row, offer.id)
                offers.append({"id": offer.id, "label": str(offer.content_json.get("name") or offer.content_json.get("item", {}).get("sku") or "Offer"),
                    "product": product.content_json.get("name"), "context": context, "item": offer.content_json.get("item"),
                    "landing_page": offer.content_json.get("landing_page_url")})
            except MarketingError:
                continue
    existing = []
    for version in session.scalars(select(m.ExperimentVersionRecord).where(m.ExperimentVersionRecord.workspace_id == ctx.workspace_id,
            m.ExperimentVersionRecord.canonicalization_version == POLICY)):
        proof = origin(version)
        if proof and proof.get("round_id") == row.id:
            candidate = session.scalar(select(m.ExperimentVersionCandidateRecord).where(m.ExperimentVersionCandidateRecord.workspace_id == ctx.workspace_id,
                m.ExperimentVersionCandidateRecord.experiment_version_id == version.id))
            valid = True
            try:
                check_projection(session, ctx, version, candidate)
            except MarketingError:
                valid = False
            existing.append({"decision_id": proof["decision_id"], "asset_version_id": proof["asset_version_id"],
                "evaluation_id": proof["evaluation_id"], "current": valid, "scope": proof["scope"]})
    return {"evaluation_id": evaluation.id if evaluation else None, "assets": assets, "offers": offers, "existing": existing,
        "evidence_source": "user_supplied_delivery", "no_meta_writes": True}


def create(session, ctx, row, body):
    ensure_active(session, ctx)
    w.serialize_workspace(session, ctx)
    if not body.confirms_tested_context or not body.note.strip():
        raise MarketingError("handoff_confirmation_required", "请确认准确产品、销售方案及实际投放条件，并记录依据。")
    selected_round_id = row.id
    row, evaluation, entry, asset, repeatable = current_evidence(session, ctx, body.evaluation_id, body.asset_version_id)
    if row.id != selected_round_id:
        raise MarketingError("marketing_evidence_wrong_round", "评估不属于这轮测试。", 409)
    product, offer, source = commercial_context(session, ctx, row, body.offer_version_id)
    key = digest([POLICY, row.id, evaluation.id, asset.id, offer.id])
    ids = {k: f"mh{k}_{key[:36]}" for k in ("exp", "ver", "entry", "cand", "judge", "prop", "conf", "decision", "projection")}
    previous = session.scalar(select(m.ExperimentDecisionRecord).where(m.ExperimentDecisionRecord.workspace_id == ctx.workspace_id,
        m.ExperimentDecisionRecord.id == ids["decision"]))
    if previous:
        return previous
    now = m.utcnow()
    proof = {"policy": POLICY, "project_id": row.project_id, "round_id": row.id, "evaluation_id": evaluation.id,
        "evaluation_input_hash": evaluation.input_hash, "evaluation_result_hash": digest(evaluation.result_json),
        "asset_version_id": asset.id, "decision_id": ids["decision"], "source": "user_supplied_delivery",
        "scope": "repeatable_combination" if repeatable else "exact_asset_only", "relative_win_claimed": False,
        "landing_page_version_id": offer.id,
        "confirmed_by": ctx.initiator_id or ctx.executor, "note": body.note}
    snapshot = {MARKER: proof, "source_market_snapshot": source, "marketing_snapshot": row.snapshot_json}
    shared = {"workspace_id": ctx.workspace_id}
    experiment = m.ExperimentRecord(id=ids["exp"], **shared, product_id=session.get(m.AssetRecord, asset.asset_id).product_id,
        name="Marketing evidence handoff · " + entry["ad_name"], state="closed", revision=1, record_schema_version=2,
        config_json={"origin": POLICY}, metric_contract_hash=digest(row.snapshot_json["protocol"]),
        frozen_snapshot_json=snapshot, frozen_snapshot_hash=digest(snapshot), frozen_at=now, closed_at=now)
    session.add(experiment); session.flush()
    frozen_entry = m.ExperimentEntryRecord(id=ids["entry"], **shared, experiment_id=experiment.id, asset_version_id=asset.id,
        marketing_vars_json={"origin": POLICY}, position=0)
    version = m.ExperimentVersionRecord(id=ids["ver"], **shared, experiment_id=experiment.id,
        canonicalization_version=POLICY, frozen_snapshot_json=snapshot, frozen_snapshot_hash=digest(snapshot),
        metric_contract_hash=experiment.metric_contract_hash, market=source["market"], placement=source["placement"],
        landing_page_evidence_hash=digest({"url": source.get("landing_page", offer.content_json["landing_page_url"])}),
        audience_snapshot_json={"audience": row.snapshot_json["plan"]["audience"]}, audience_snapshot_hash=digest(row.snapshot_json["plan"]["audience"]),
        daily_budget_amount=Decimal(row.snapshot_json["plan"]["small_round_budget"])/Decimal(row.snapshot_json["plan"]["duration_days"]),
        total_budget_cap=Decimal(row.snapshot_json["plan"]["small_round_budget"]), budget_currency="USD",
        schedule_start_at=datetime.combine(row.start_date,time.min,tzinfo=timezone.utc),
        schedule_end_at=datetime.combine(row.end_date+timedelta(days=1),time.min,tzinfo=timezone.utc), schedule_timezone=row.timezone,
        attribution_click_window_days=7, attribution_view_window_days=1, statistical_plan_json=row.snapshot_json["protocol"],
        statistical_plan_hash=digest(row.snapshot_json["protocol"]), marketing_vars_schema_version=POLICY,
        product_version_id=product.id, offer_version_id=offer.id, candidates_hash=digest(entry), concept_resolutions_hash=digest([]))
    session.add_all([frozen_entry,version]);session.flush()
    candidate = m.ExperimentVersionCandidateRecord(id=ids["cand"], **shared, experiment_version_id=version.id,
        source_entry_id=frozen_entry.id, source_kind="reuse", source_evidence_json=proof, source_evidence_hash=digest(proof),
        asset_version_id=asset.id, asset_version_content_sha256=asset.content_sha256, position=0,
        marketing_vars_json={"origin": POLICY}, marketing_vars_hash=digest({"origin": POLICY}), execution_mode="byok",
        eligibility_evidence_json=proof, eligibility_evidence_hash=digest(proof), concepts_hash=digest([]), candidate_hash=digest(entry), evaluated_at=now)
    judgement = m.JudgementRecord(id=ids["judge"], **shared, experiment_id=experiment.id, experiment_version_id=version.id,
        algorithm_version=POLICY, as_of=row.end_date, snapshot_hash=evaluation.input_hash, input_snapshot_json=evaluation.input_json,
        result_json={"marketing_evaluation_id": evaluation.id, "business_status": "passes", "relative_win_claimed": False}, record_schema_version=2)
    session.add_all([candidate,judgement]);session.flush()
    proposal = m.DecisionProposalRecord(id=ids["prop"], **shared, experiment_id=experiment.id, experiment_version_id=version.id,
        judgement_id=judgement.id, eligibility_context_hash=key, proposal_hash=key, proposed_entries_json={"asset_version_id":asset.id}, evidence_json=proof)
    session.add(proposal);session.flush()
    confirmation = m.DecisionConfirmationRecord(id=ids["conf"], **shared, proposal_id=proposal.id, proposal_hash=key,
        expected_proposal_sequence=1, idempotency_key_hash=key, request_fingerprint_hash=key, actor_id=ctx.initiator_id or ctx.executor,
        role=ctx.role, authorization_epoch=ctx.authorization_epoch, authorization_evidence_json={"policy":POLICY,"note":body.note}, confirmed_at=now)
    session.add(confirmation);session.flush()
    result = next(item for item in evaluation.result_json["results"] if item["id"] == "asset:"+asset.id)
    decision = m.ExperimentDecisionRecord(id=ids["decision"], **shared, experiment_id=experiment.id, judgement_id=judgement.id,
        experiment_entry_id=frozen_entry.id, experiment_version_id=version.id, experiment_version_candidate_id=candidate.id,
        proposal_id=proposal.id, confirmation_id=confirmation.id, verdict="win", metric=evaluation.result_json["metric"],
        estimate=result["estimate"], interval_low=result["interval_low"], interval_high=result["interval_high"],
        reason_codes_json=[POLICY,"business_target_passes",proof["scope"]], actor_id=confirmation.actor_id,
        confirmed_at=now, context_hash=key, decision_schema_version=2)
    session.add(decision);session.flush()
    session.add(m.ContextualVerdictProjectionRecord(id=ids["projection"], **shared, experiment_id=experiment.id,
        experiment_version_id=version.id, candidate_id=candidate.id, decision_id=decision.id, confirmation_id=confirmation.id,
        verdict="win", metric=decision.metric, estimate=decision.estimate, asset_version_id=asset.id, product_version_id=product.id,
        offer_version_id=offer.id, market=version.market, placement=version.placement,
        landing_page_evidence_hash=version.landing_page_evidence_hash, confirmed_at=now))
    session.flush()
    w.audit(session,row,"evidence_handed_off",proof)
    return decision


def scale_block_reason(session, ctx, decision_id):
    """Recheck marketing-origin enrollments without changing legacy behavior."""
    if not decision_id.startswith("mhdecision_"):
        return None
    decision = session.scalar(select(m.ExperimentDecisionRecord).where(m.ExperimentDecisionRecord.workspace_id == ctx.workspace_id,
        m.ExperimentDecisionRecord.id == decision_id))
    if not decision:
        return "marketing_handoff_invalid" if decision_id.startswith("mhdecision_") else None
    version = session.scalar(select(m.ExperimentVersionRecord).where(m.ExperimentVersionRecord.workspace_id == ctx.workspace_id,
        m.ExperimentVersionRecord.id == decision.experiment_version_id))
    if not version or not origin(version):
        return "marketing_handoff_invalid" if decision_id.startswith("mhdecision_") else None
    candidate = session.scalar(select(m.ExperimentVersionCandidateRecord).where(m.ExperimentVersionCandidateRecord.workspace_id == ctx.workspace_id,
        m.ExperimentVersionCandidateRecord.id == decision.experiment_version_candidate_id))
    try:
        check_projection(session, ctx, version, candidate, require_combination=True)
    except MarketingError as exc:
        return exc.code
    return None
