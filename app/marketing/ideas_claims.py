"""Change or delete one claim of ads-first ideas in the user's own words (#224, design doc §5.7).

Both are free: nothing is drafted by AI. A claim keeps its id, and so do its ways of telling and ads; what
an image made before the change becomes follows the status rules (#198): an image whose claim now says
something else, or stands on other facts, is not the ad the claim describes any more and needs making
again (``ideas_changed``). Deleting a claim takes its ways of telling and ads out of the ideas; the images
made for them stay ads of the project, needing making again, and are never handed out.

The claim's ads keep their copy and image instructions (the copy is changed by ``ideas_copy``, #228), and a
new image of the claim is made the usual way (``prepare_marketing_ideas_generation``), checked against the
claim as it is now and standing on the facts it names now.
"""
from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select

from app.platform.audit import append_event
from . import ideas, service
from .claims import unsourced_uses, unsupported_claims
from .ideas_contracts import Ideas, IdeasUnderstanding
from .service import MarketingError

#: Ideas hold at least two claims (``Ideas.angles``; the first batch never draws fewer, #193).
MIN_CLAIMS = 2
#: What a way of telling says when the edited claim no longer names any fact it stood on.
GAP = {"zh-CN": "改主张时去掉了这里原来引用的事实，暂时没有资料支持。",
       "en-US": "The facts this stood on were taken off the claim when it was edited; nothing supports it yet."}


def editable(session, ctx, row):
    """Ideas the user may change now -- a claim (#224) or an ad's copy (#228): drafted, and not still being
    made by the first batch."""
    from .first_batch import ENDED
    from .models import MarketingFirstBatchRecord
    service.ensure_active(session, ctx)
    if row.state != "ideas_ready" or not row.ideas_json:
        raise MarketingError("ideas_not_ready", "请先起草广告主张。", 409)
    batch = session.scalar(select(MarketingFirstBatchRecord).where(MarketingFirstBatchRecord.workspace_id == ctx.workspace_id,
                                                                   MarketingFirstBatchRecord.project_id == row.id))
    if batch is not None and batch.state not in ENDED:
        # The first batch makes an image for every ad the ideas hold, as they were when it was confirmed.
        raise MarketingError("first_batch_running", "首批还没有完成，完成后再改。", 409)
    return Ideas.model_validate(row.ideas_json)


def _angle(doc, angle_id):
    for index, angle in enumerate(doc.angles):
        if angle.id == angle_id:
            return index, angle
    raise MarketingError("angle_not_found", "找不到这个主张。", 404)


def unchanged(row, revision):
    """Nothing to change: the ideas as they are, at the revision the user saw (no revision is spent)."""
    if row.revision != revision:
        raise MarketingError("stale_marketing_revision", "内容已更新，请刷新后重试。", 409)
    return row


def _claim_ads(session, ctx, row, angle_id) -> list[str]:
    return [version.id for version, recipe in ideas.project_ads(session, ctx, row)
            if (ideas.intent_of(recipe).get("combination") or {}).get("angle_id") == angle_id]


def edit_claim(session, ctx, row, revision, angle_id: str, *, proposition: str, rationale: str, fact_ids: list):
    """Change what one claim says, why it is worth trying and the facts it stands on (``EditIdeasClaim``).

    Refused when a named fact is not a fact of the understanding now (``claim_fact_not_found``: a removed
    fact, an unsourced claim, a guess), or when the words state a number, price or certification the named
    facts do not -- or a listed unsourced claim (``claim_unsupported``). Each way of telling keeps only the
    facts the claim still names; one left with none says so in its evidence gap. Nothing changed: no
    revision is spent."""
    doc = editable(session, ctx, row)
    index, angle = _angle(doc, angle_id)
    understanding = IdeasUnderstanding.model_validate(row.understanding_json)
    facts = {fact.id: fact for fact in understanding.facts}
    fact_ids = list(dict.fromkeys(fact_ids))
    missing = [fid for fid in fact_ids if fid not in facts]
    if missing:
        raise MarketingError("claim_fact_not_found", "主张只能引用产品资料里现有的事实，请刷新后重选。",
                             params={"fact_ids": missing})
    # What the claim stands on: the facts it names and those behind the AI guesses it cites.
    inferences = {item.id: item for item in understanding.inferences}
    supporting = [*fact_ids, *(fid for iid in angle.inference_ids if iid in inferences for fid in inferences[iid].fact_ids)]
    support = "\n".join(f"{facts[fid].statement}\n{facts[fid].source_quote}" for fid in dict.fromkeys(supporting) if fid in facts)
    words = f"{proposition}\n{rationale}"
    unsupported = unsupported_claims(words, support)
    listed = {item.id: item.text for item in understanding.unsupported_claims}
    sourced = "\n".join(f"{fact.statement}\n{fact.source_quote}" for fact in understanding.facts)
    used = [listed[claim_id] for claim_id in unsourced_uses(words, list(listed.items()), sourced)]
    if unsupported or used:
        raise MarketingError("claim_unsupported", "主张的说法里有引用的事实找不到出处的内容，请改掉，或先在产品资料里补上出处。",
                             params={"claims": [*unsupported, *used][:20]})
    merged = deepcopy(row.ideas_json)
    stored = merged["angles"][index]
    stored.update(proposition=proposition, rationale=rationale, fact_ids=fact_ids)
    gap = GAP.get(doc.strategy_language, GAP["en-US"])
    for expression in stored["expressions"]:
        basis = expression["evidence_basis"]
        kept = [fid for fid in basis.get("fact_ids", []) if fid in fact_ids]
        basis["fact_ids"] = kept
        if not kept and not basis.get("gap"):
            basis["gap"] = gap
    Ideas.model_validate(merged)
    if merged == row.ideas_json:
        return unchanged(row, revision)
    before = row.ideas_json["angles"][index]
    affected = _claim_ads(session, ctx, row, angle_id)
    service.advance(session, ctx, row, revision, "claim_edited", ideas_json=merged)
    append_event(session, event_type="marketing.ideas_claim_edited", resource_type="marketing_project", resource_id=row.id,
                 details={"revision": row.revision, "angle_id": angle_id,
                          "changed": [key for key in ("proposition", "rationale", "fact_ids") if before.get(key) != stored.get(key)],
                          "fact_ids_before": before.get("fact_ids", []), "fact_ids_after": fact_ids,
                          "asset_version_ids": affected})
    session.flush()
    return row


def delete_claim(session, ctx, row, revision, angle_id: str):
    """Take one claim, its ways of telling and its ads out of the ideas.

    Refused when it would leave fewer than two claims (``claims_too_few``). The images made for the claim
    stay ads of these ideas -- listed, kept or not as the user left them -- and need making again
    (``ideas_changed``): their claim is gone, so none can be downloaded or exported."""
    doc = editable(session, ctx, row)
    _angle(doc, angle_id)
    if len(doc.angles) <= MIN_CLAIMS:
        raise MarketingError("claims_too_few", "至少要留两个主张：可以改这个主张，或先再加一个主张再删。", 409,
                             params={"minimum": MIN_CLAIMS})
    merged = deepcopy(row.ideas_json)
    removed = [combo["id"] for combo in merged["combinations"] if combo["angle_id"] == angle_id]
    merged["angles"] = [angle for angle in merged["angles"] if angle["id"] != angle_id]
    merged["combinations"] = [combo for combo in merged["combinations"] if combo["angle_id"] != angle_id]
    Ideas.model_validate(merged)
    affected = _claim_ads(session, ctx, row, angle_id)
    service.advance(session, ctx, row, revision, "claim_deleted", ideas_json=merged)
    append_event(session, event_type="marketing.ideas_claim_deleted", resource_type="marketing_project", resource_id=row.id,
                 details={"revision": row.revision, "angle_id": angle_id, "combination_ids": removed,
                          "asset_version_ids": affected})
    session.flush()
    return row
