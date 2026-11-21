"""Change one ad's headline and body copy in the user's own words (#228, design doc §5.3).

Free: nothing is drafted by AI. The copy belongs to one place of the ideas -- a claim told one way (a
combination) -- and every image made there shares it. The image model renders it into the picture
(``service.image_brief``), so an image never follows a change of copy: the images made there before are not
the ad the place describes any more and need making again (``ideas_changed``, #198); a new image is made the
usual way (``prepare_marketing_ideas_generation``) with the new copy, and is checked and verified like any
other before it may leave. An uploaded image is the exception: its picture and its words are the user's own,
which the place's copy never described, so it is left as it is (``ideas.same_content``).

Only the copy changes: the claim, the way of telling, the facts they name and the English image brief stay.
The copy is the user's own words and still keeps honesty rule 5 (design doc §5.3): saving refuses a number,
price or certification the facts the place cites do not state, and any claim the understanding lists as
having no source. Everything else is left to the check of the new image, which looks for unsourced claims in
what it renders.
"""
from __future__ import annotations

from copy import deepcopy

from app.generation.selection import MAX_PROMPT_LENGTH
from app.platform.audit import append_event
from . import ideas, service
from .claims import unsourced_uses, unsupported_claims
from .ideas_claims import editable, unchanged
from .ideas_contracts import Ideas, IdeasUnderstanding
from .languages import script_matches
from .service import MarketingError


def _place(doc, combination_id: str) -> int:
    for index, combination in enumerate(doc.combinations):
        if combination.id == combination_id:
            return index
    raise MarketingError("combination_not_found", "这些广告想法里没有这张广告的位置。", 404)


def _unsupported(row, combination_id: str, words: str) -> list[str]:
    """What ``words`` say that the facts this place cites do not (``claims.unsupported_claims``), and the
    claims the understanding lists as unsourced that they use (``claims.unsourced_uses``)."""
    understanding = IdeasUnderstanding.model_validate(row.understanding_json)
    cited = ideas.citations(row, combination_id)
    support = "\n".join(f"{fact.statement}\n{fact.source_quote}" for fact in understanding.facts if fact.id in cited)
    listed = {item.id: item.text for item in understanding.unsupported_claims}
    sourced = "\n".join(f"{fact.statement}\n{fact.source_quote}" for fact in understanding.facts)
    used = [listed[claim_id] for claim_id in unsourced_uses(words, list(listed.items()), sourced)]
    return [*unsupported_claims(words, support), *used][:20]


def _overflow(session, ctx, row, doc, combination) -> int:
    """By how many characters the image prompt of this place would pass the limit with the new copy.

    Measured as the largest prompt preparing could write (``ideas._prompt``): the brand baseline's lines with
    its logo, and the product photo line. A copy saved here can then always be made into an image."""
    from . import brand_kits
    try:
        kit = brand_kits.resolve_ref(session, ctx, doc.brand_kit) if doc.brand_kit else None
    except MarketingError:
        kit = None  # preparing refuses an unresolved brand baseline on its own
    intent = ideas.ideas_intent(row, combination.id)
    prompt = ideas._prompt(combination, intent, kit, logo=bool(kit and kit.content_json.get("logo")), references=True,
                           policy=ideas.PROMPT_POLICY)
    return len(prompt) - MAX_PROMPT_LENGTH


def edit_copy(session, ctx, row, revision, combination_id: str, *, headline: str, body_copy: str):
    """Change the headline and body copy of one place of the ideas (``EditIdeasCopy``).

    Refused when the place is not in the ideas (``combination_not_found``), a text is not in the ads'
    language (``copy_language_mismatch``), the words state what the place's facts do not or use a listed
    unsourced claim (``copy_unsupported``, ``params.claims``), or the image prompt could not hold the copy
    (``ad_copy_too_long``, ``params.over``). Nothing changed: no revision is spent."""
    doc = editable(session, ctx, row)
    index = _place(doc, combination_id)
    merged = deepcopy(row.ideas_json)
    stored = merged["combinations"][index]
    before = {key: stored.get(key) for key in ideas.COPY_KEYS}
    stored.update(headline=headline, body_copy=body_copy)
    if merged == row.ideas_json:
        return unchanged(row, revision)
    wrong = [f"body.{name}" for name, text in (("headline", headline), ("body_copy", body_copy))
             if not script_matches(text, doc.ad_language)]
    if wrong:
        raise MarketingError("copy_language_mismatch", "Ad headline and body copy must use the selected advertising language.",
                             fields=wrong)
    claims = _unsupported(row, combination_id, f"{headline}\n{body_copy}")
    if claims:
        raise MarketingError("copy_unsupported", "文案里有这条广告引用的事实找不到出处的说法，请改掉，或先在产品资料里补上出处。",
                             params={"claims": claims})
    validated = Ideas.model_validate(merged)
    over = _overflow(session, ctx, row, doc, validated.combinations[index])
    if over > 0:
        raise MarketingError("ad_copy_too_long", "标题和正文太长，放进出图说明会超出上限，请缩短一些。",
                             params={"over": over})
    combination = validated.combinations[index]
    affected = _affected(session, ctx, row, combination_id)
    service.advance(session, ctx, row, revision, "copy_edited", ideas_json=merged)
    append_event(session, event_type="marketing.ideas_copy_edited", resource_type="marketing_project", resource_id=row.id,
                 details={"revision": row.revision, "combination_id": combination_id, "angle_id": combination.angle_id,
                          "expression_id": combination.expression_id,
                          "changed": [key for key in ideas.COPY_KEYS if before[key] != stored[key]],
                          "asset_version_ids": affected})
    session.flush()
    return row


def _affected(session, ctx, row, combination_id: str) -> list[str]:
    """The images made at this place that are its ad now and stop being so with other copy: an image made
    for earlier content of the place already needs making again, and an uploaded one keeps its own words."""
    try:
        now = ideas.ideas_intent(row, combination_id)
    except LookupError:
        return []
    return [version.id for version, recipe in ideas.project_ads(session, ctx, row)
            if (ideas.intent_of(recipe).get("combination") or {}).get("id") == combination_id
            and "upload" not in ideas.intent_of(recipe) and ideas.same_content(now, ideas.intent_of(recipe))]
