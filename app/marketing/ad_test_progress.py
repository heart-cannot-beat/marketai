"""A test round's running numbers, recorded deviations and changed-ad hints (#254, step 2 design §5.7, §5.8).

* **The numbers so far.** For each compared group of ads: spend, orders and cost per order over what the
  round's confirmed reports count today (``workflow.counted_report_rows``), and how many orders it is short
  of the protocol's minimum. Reading them evaluates nothing and writes nothing: they are not a conclusion.
* **Deviations.** Whenever the user notices that something ran differently from the plan, they record it
  for the ads it concerns; each record is a new ``MarketingRoundDeviationRecord`` and none is rewritten. The
  evaluation reads a record as a deviation noted when those ads' Meta ads were matched (``bind``): under the
  existing rule the compared groups holding them draw no conclusion this round. Only test rounds have them.
* **Changed ads.** A running test stands on what was checked when it started: a later change to its
  ads-first ideas changes neither its evaluation nor its ad package. Its page still says which of its ads
  now cite a changed fact, use a rejected photo or no longer show what the ideas say, because the Meta ad
  still shows the old content. An ad the test took from the ideas is a reuse record of kind ``ideas`` (plan
  E4) naming the intent it started on; it is judged by the ads-first rules (``ideas_export.statuses``).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.adapters.persistence.models import new_id
from app.platform.audit import append_event
from .models import MarketingProjectRecord
from .service import MarketingError, digest
from .workflow_models import (MarketingCreativeReuseRecord as Reuse, MarketingHandoffRecord as Handoff,
    MarketingRoundDeviationRecord as Deviation)

#: The reuse records of the ads a test took from its ads-first ideas (plan E4).
IDEAS_REUSE = "ideas"
#: The ads-first reasons that say the ideas changed what an ad stands on, by the kind of change they show.
CHANGES = {"facts_changed": "fact", "facts_removed": "fact", "photo_unavailable": "photo", "ideas_changed": "copy"}
KINDS = ("fact", "photo", "copy")
CENT = Decimal("0.01")


def recorded(session, ctx, row) -> list[Deviation]:
    """The round's recorded deviations, oldest first."""
    return list(session.scalars(select(Deviation).where(Deviation.workspace_id == ctx.workspace_id,
        Deviation.round_id == row.id).order_by(Deviation.created_at, Deviation.id)).all())


def _handoff(session, ctx, row):
    return session.scalar(select(Handoff).where(Handoff.workspace_id == ctx.workspace_id, Handoff.round_id == row.id).limit(1))


def _entries(handoff) -> list[dict]:
    return handoff.manifest_json["entries"] if handoff else []


def with_recorded_deviations(session, ctx, row, bindings) -> tuple[list[dict], list[str] | None]:
    """An evaluation's matched Meta ads with each recorded deviation added to those of its ads, and the ids.

    A deviation recorded later counts as one noted when the ad was matched (design §5.7), whichever came
    first. A round without one -- every round of an earlier test -- gets ``bindings`` unchanged and ``None``,
    so its evaluation inputs stay exactly what they were."""
    from .workflow import full_report_round
    if not full_report_round(session, ctx, row):
        return bindings, None
    found = recorded(session, ctx, row)
    if not found:
        return bindings, None
    entry_of = {e["asset_version_id"]: e["entry_id"] for e in _entries(_handoff(session, ctx, row))}
    notes = {}
    for deviation in found:
        for version in deviation.asset_version_ids:
            if version in entry_of:
                notes.setdefault(entry_of[version], []).append(deviation.note)
    merged = [{**b, "deviations": [*(b.get("deviations") or []), *notes[b["entry_id"]]]} if b["entry_id"] in notes else b
              for b in bindings]
    return merged, [d.id for d in found]


def _groups(row, entries) -> list[tuple[dict, list[dict]]]:
    """Each compared group of the frozen plan with the package entries it holds."""
    return [(group, [e for e in entries if e["combination_id"] in group["combination_ids"]])
            for group in row.snapshot_json["plan"]["groups"]]


def _comparison_ids(groups, asset_version_ids) -> list[str]:
    return [group["id"] for group, members in groups if any(e["asset_version_id"] in asset_version_ids for e in members)]


def record(session, ctx, row, body) -> Deviation:
    """Record that these ads of the round's package ran differently from the plan (design §5.7).

    Ads can only have run once the package was made; after that, at any time -- also once the round ended."""
    handoff = _handoff(session, ctx, row)
    if handoff is None or handoff.state != "ready":
        raise MarketingError("handoff_not_ready", "广告包还没有生成，这一轮还没有开始投放。", 409)
    ids = list(body.asset_version_ids)
    if len(ids) != len(set(ids)):
        raise MarketingError("duplicate_asset", "同一素材不能重复计入。")
    packaged = {e["asset_version_id"] for e in _entries(handoff)}
    if any(version not in packaged for version in ids):
        raise MarketingError("ad_not_in_package", "补记的广告要是这一轮广告包里的广告。")
    deviation = Deviation(id=new_id("mrd"), workspace_id=ctx.workspace_id, round_id=row.id, asset_version_ids=ids,
                          note=body.note, actor_id=ctx.initiator_id)
    session.add(deviation)
    session.flush()
    append_event(session, event_type="marketing.round_deviation_recorded", resource_type="marketing_round",
                 resource_id=row.id, details={"deviation_id": deviation.id, "asset_version_ids": ids,
                                              "comparison_ids": _comparison_ids(_groups(row, _entries(handoff)), ids)})
    return deviation


def changed_ads(session, ctx, test_row, row) -> list[dict]:
    """The round's ads from its ads-first ideas whose fact, photo or copy changed since the test started (§5.8).

    Each ad is judged by the ads-first rules against the intent it started on (the reuse record's
    ``intent_hash``). Ideas that can no longer be read, or an image that is not the one the test started on,
    say only that the ad's content is no longer what it shows."""
    from . import ideas, ideas_export
    links = [link for link in session.scalars(select(Reuse).where(Reuse.workspace_id == ctx.workspace_id,
             Reuse.round_id == row.id).order_by(Reuse.created_at, Reuse.id)).all()
             if (link.source_json or {}).get("kind") == IDEAS_REUSE]
    by_ideas = {}
    for link in links:
        by_ideas.setdefault(link.source_json.get("ideas_project_id") or test_row.source_project_id, []).append(link)
    judged = {}
    for ideas_id, group in by_ideas.items():
        ideas_row = session.scalar(select(MarketingProjectRecord).where(MarketingProjectRecord.workspace_id == ctx.workspace_id,
            MarketingProjectRecord.id == ideas_id, MarketingProjectRecord.flow == ideas.FLOW))
        try:
            ads = {version.id: (version, recipe) for version, recipe in ideas.project_ads(session, ctx, ideas_row)} \
                if ideas_row is not None else {}
            started = [ads[link.asset_version_id] for link in group if link.asset_version_id in ads
                       and digest(ideas.intent_of(ads[link.asset_version_id][1])) == link.source_json.get("intent_hash")]
            judged.update(ideas_export.statuses(session, ctx, ideas_row, ads=started) if started else {})
        except (ValueError, LookupError, StopIteration, TypeError):
            # Stored ideas a later contract no longer reads, as ``ideas_export`` treats them.
            continue
    found = []
    for link in links:
        status = judged.get(link.asset_version_id)
        if status is None:
            kinds, fact_ids = {"copy"}, set()
        else:
            kinds = {CHANGES[r["code"]] for r in status["reasons"] if r["code"] in CHANGES}
            fact_ids = {fid for r in status["reasons"] if CHANGES.get(r["code"]) == "fact" for fid in r["fact_ids"]}
        if kinds:
            found.append({"asset_version_id": link.asset_version_id, "changes": [k for k in KINDS if k in kinds],
                          "fact_ids": sorted(fact_ids)})
    return found


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT))


def _count(value: Decimal) -> str:
    return format(value.normalize(), "f")


def progress(session, ctx, test_row, row) -> dict:
    """``MarketingTestProgress``: each compared group's numbers so far, the deviations and the changed ads."""
    from .ad_test_schedule import remaining_purchases, schedule
    from .workflow import counted_report_rows, evaluation_bindings
    dates = schedule(session, ctx, test_row, row)
    handoff = _handoff(session, ctx, row)
    groups = _groups(row, _entries(handoff))
    bindings, _ = evaluation_bindings(session, ctx, row)
    rows = counted_report_rows(session, ctx, row)
    deviations = recorded(session, ctx, row)
    min_purchases = row.snapshot_json["protocol"]["min_purchases"]
    comparisons = []
    for group, members in groups:
        versions = [e["asset_version_id"] for e in members]
        entry_ids = {e["entry_id"] for e in members}
        matched = [b for b in bindings if b["entry_id"] in entry_ids]
        ads = {b["ad_id"] for b in matched}
        spend = sum((Decimal(str(r["spend"])) for r in rows if r["ad_id"] in ads and r.get("spend") is not None), Decimal("0"))
        purchases = sum((Decimal(str(r["purchases"])) for r in rows if r["ad_id"] in ads and r.get("purchases") is not None),
                        Decimal("0"))
        comparisons.append({"id": group["id"], "label": group["name"], "asset_version_ids": versions,
            "spend": _money(spend), "purchases": _count(purchases),
            "cost_per_purchase": _money(spend / purchases) if purchases > 0 else None,
            "remaining_purchases": remaining_purchases(min_purchases, purchases),
            "conditions_changed": any(not b.get("conditions_match", True) or b.get("deviations") for b in matched)
                or any(version in d.asset_version_ids for d in deviations for version in versions)})
    return {"test_id": test_row.id, "round_id": row.id, "revision": row.revision, "provisional": True,
            "currency": row.snapshot_json["currency"], "target_cost": str(row.snapshot_json["plan"]["rules"]["threshold"]),
            "min_purchases": min_purchases, "effective_start_date": dates["effective_start_date"],
            "effective_end_date": dates["effective_end_date"], "result_date": dates["result_date"],
            "data_through": max((date.fromisoformat(r["date"]) for r in rows), default=None),
            "comparisons": comparisons,
            "deviations": [{"id": d.id, "asset_version_ids": list(d.asset_version_ids), "note": d.note,
                            "comparison_ids": _comparison_ids(groups, d.asset_version_ids), "actor_id": d.actor_id,
                            "created_at": d.created_at.isoformat()} for d in deviations],
            "changed_ads": changed_ads(session, ctx, test_row, row)}
