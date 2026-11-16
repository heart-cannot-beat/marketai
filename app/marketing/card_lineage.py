"""Which marketing tests stand for the same product card (J-32, #7).

A test started from a product card records that card's id in its first
revision (``created``, revision 1). Each ``confirm_understanding`` then
freezes the confirmed understanding as the test's own ``products`` row, its
anchor, and the test's creatives hang on that anchor. A test whose source
text was revised (``revise_source``) no longer stands for its card.

The product home folds anchors back into their card (``app.products.catalog``)
and reuse lets the tests of one card share creatives (``app.marketing.reuse``);
both read the lineage from here so they cannot drift apart. The link lives
only in the append-only revisions, never in a name, label or file hash
(Creative Library contract: business identity is not inferred).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.adapters.persistence.models import ProductRecord

from .models import MarketingRevisionRecord as Revision


@dataclass(frozen=True)
class CardTest:
    """A test started from a product card whose source text was never revised."""

    project_id: str
    #: The card id revision 1 recorded: a bridged ``prod_`` id, or a Library
    #: entry id for tests from before the bridge (see ``card_ids``).
    source: str
    #: Product anchors this test's understanding confirmations minted, oldest first.
    anchors: tuple[str, ...]


def card_tests(session, ctx) -> dict[str, CardTest]:
    """Every card-started, unrevised test of the Workspace, by project id."""
    chosen = Revision.content_json["product_id"].as_string()
    rows = session.execute(
        select(Revision.project_id, Revision.revision, Revision.reason, chosen)
        .where(
            Revision.workspace_id == ctx.workspace_id,
            Revision.reason.in_(("created", "confirm_understanding", "revise_source")),
        )
        .order_by(Revision.project_id, Revision.revision)
    ).all()
    revised = {project for project, _, reason, _ in rows if reason == "revise_source"}
    sources = {
        project: product for project, revision, reason, product in rows
        if reason == "created" and revision == 1 and product and project not in revised
    }
    anchors: dict[str, list[str]] = {}
    for project, _, reason, product in rows:
        if reason == "confirm_understanding" and product and project in sources and product != sources[project]:
            anchors.setdefault(project, []).append(product)
    return {project: CardTest(project, source, tuple(anchors.get(project, ()))) for project, source in sources.items()}


def card_ids(session, ctx, sources) -> dict[str, str]:
    """Each recorded source mapped to its card's product id.

    New tests record the bridged ``prod_`` id already; older ones recorded the
    Library entry id, which the bridged product names in ``product_profile_entry_id``.
    """
    sources = {source for source in sources if source}
    if not sources:
        return {}
    bridged = dict(session.execute(
        select(ProductRecord.product_profile_entry_id, ProductRecord.id).where(
            ProductRecord.workspace_id == ctx.workspace_id,
            ProductRecord.product_profile_entry_id.in_(sources),
        )
    ).all())
    return {source: bridged.get(source, source) for source in sources}


def same_card(session, ctx, project_id: str) -> tuple[str, list[CardTest]] | None:
    """The card this test stands for and every test of that card (itself included), or ``None``."""
    tests = card_tests(session, ctx)
    if project_id not in tests:
        return None
    cards = card_ids(session, ctx, (test.source for test in tests.values()))
    card = cards[tests[project_id].source]
    return card, [test for test in tests.values() if cards[test.source] == card]
