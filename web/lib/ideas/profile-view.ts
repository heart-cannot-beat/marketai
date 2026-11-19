import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';

/**
 * What the product profile changes and shows (ADR 0018, design §5.6, #202),
 * read from `get_marketing_ideas` and sent back whole through
 * `edit_marketing_ideas_understanding`.
 *
 * The server decides what a change means: an edited or new fact comes back
 * unchecked, an untouched one keeps its checking, and a fact left out is
 * removed. These helpers only build the document the user asked for.
 */

export type Understanding = components['schemas']['IdeasUnderstanding'];
export type Fact = components['schemas']['Fact'];
/** Something the AI read without a source (#224): listed, never used. */
export type UnsupportedClaim = components['schemas']['UnsupportedClaim'];

/** What the user types for a fact: the claim, and the words it was read from. */
export interface FactText {
  statement: string;
  source_quote: string;
}

/** What changing or removing one fact does to the ads and claims that use it. */
export interface FactUse {
  /** Ads whose image stands on the fact: they need making again after a change. */
  ads: number;
  /** Claims that name the fact themselves, not only through an AI guess. A
   * removed fact stays in such a claim, so its ads cannot be made again until
   * the claim itself is changed. */
  claims: number;
}

/** How many ads and claims use one fact, from each ad's `cited_fact_ids` and the ideas. */
export function factUse(detail: IdeasDetail, factId: string): FactUse {
  const ads = detail.ads.filter((ad) =>
    (ad.cited_fact_ids ?? []).includes(factId)
  ).length;
  const claims = (detail.ideas?.angles ?? []).filter(
    (angle) =>
      (angle.fact_ids ?? []).includes(factId) ||
      angle.expressions.some((expression) =>
        (expression.evidence_basis.fact_ids ?? []).includes(factId)
      )
  ).length;
  return { ads, claims };
}

const clean = (text: FactText): FactText => ({
  statement: text.statement.trim(),
  source_quote: text.source_quote.trim(),
});

/** A fact id nothing in the understanding uses yet, and never an old one's:
 * a removed fact's id may still be named by a claim (#198). */
export function newFactId(
  understanding: Understanding,
  random: () => string = () => crypto.randomUUID()
): string {
  const taken = new Set([
    ...(understanding.facts ?? []).map((fact) => fact.id),
    ...understanding.inferences.map((inference) => inference.id),
    ...(understanding.unsupported_claims ?? []).map((claim) => claim.id),
  ]);
  let id = '';
  do {
    id = `fact-${random()
      .replace(/[^a-zA-Z0-9]/g, '')
      .slice(0, 12)}`;
  } while (taken.has(id));
  return id;
}

/** The understanding with one fact reworded or given another source. */
export function withFactChanged(
  understanding: Understanding,
  factId: string,
  text: FactText
): Understanding {
  return {
    ...understanding,
    facts: (understanding.facts ?? []).map((fact) =>
      fact.id === factId ? { ...fact, ...clean(text) } : fact
    ),
  };
}

/** The understanding with a fact the user adds; it is unchecked until checked. */
export function withFactAdded(
  understanding: Understanding,
  text: FactText,
  id: string = newFactId(understanding)
): Understanding {
  return {
    ...understanding,
    facts: [
      ...(understanding.facts ?? []),
      { id, ...clean(text), confirmation: 'unconfirmed' },
    ],
  };
}

/** The understanding without one fact. The AI's guesses stop naming it too,
 * or the server refuses the document. */
export function withFactRemoved(
  understanding: Understanding,
  factId: string
): Understanding {
  return {
    ...understanding,
    facts: (understanding.facts ?? []).filter((fact) => fact.id !== factId),
    inferences: understanding.inferences.map((inference) =>
      (inference.fact_ids ?? []).includes(factId)
        ? {
            ...inference,
            fact_ids: (inference.fact_ids ?? []).filter((id) => id !== factId),
          }
        : inference
    ),
  };
}

/** Something the AI did not know, answered with a source: it becomes an
 * unchecked fact and leaves the list of things the AI does not know. Named by
 * its words, not its place, so a list that changed meanwhile loses the right one. */
export function withUnknownAnswered(
  understanding: Understanding,
  unknown: string,
  text: FactText,
  id: string = newFactId(understanding)
): Understanding {
  const added = withFactAdded(understanding, text, id);
  const unknowns = [...(understanding.unknowns ?? [])];
  const index = unknowns.indexOf(unknown);
  if (index >= 0) unknowns.splice(index, 1);
  return { ...added, unknowns };
}

/**
 * A claim the AI read without a source, given one (#224): it becomes an
 * unchecked fact and leaves the list. The server keeps the list as the AI
 * wrote it and only takes off what the document no longer names.
 */
export function withUnsourcedAnswered(
  understanding: Understanding,
  claimId: string,
  text: FactText,
  id: string = newFactId(understanding)
): Understanding {
  return withUnsourcedDropped(withFactAdded(understanding, text, id), claimId);
}

/** A claim the AI read without a source, taken off the list ("don't need it"). */
export function withUnsourcedDropped(
  understanding: Understanding,
  claimId: string
): Understanding {
  return {
    ...understanding,
    unsupported_claims: (understanding.unsupported_claims ?? []).filter(
      (claim) => claim.id !== claimId
    ),
  };
}

/** The understanding with the audience in the user's words; it is no longer checked. */
export function withAudience(
  understanding: Understanding,
  audience: string
): Understanding {
  return { ...understanding, audience: audience.trim() };
}

/** The most facts one understanding may hold (`Understanding.facts`). */
export const MAX_FACTS = 30;
