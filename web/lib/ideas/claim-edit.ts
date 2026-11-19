import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { IdeasAd, IdeasDetail } from '@/lib/ideas/ideas-view';

/**
 * What changing or deleting one claim touches (#224, design §5.7), read
 * from `get_marketing_ideas`. The server decides what a change means; these
 * helpers only say what the user is about to change.
 */

type Angle = components['schemas']['Angle'];
export type EditClaimBody = components['schemas']['EditIdeasClaim'];

/** Ideas always keep at least two claims (`claims_too_few`). */
export const MIN_CLAIMS = 2;

/**
 * Every fact id the claim names itself: its own and its ways of telling's.
 * A removed fact's id stays here until the claim is changed (#198).
 */
export function claimFactIds(angle: Angle): string[] {
  const ids = [
    ...(angle.fact_ids ?? []),
    ...angle.expressions.flatMap(
      (expression) => expression.evidence_basis.fact_ids ?? []
    ),
  ];
  return ids.filter((id, index) => ids.indexOf(id) === index);
}

/** The ids a claim names that are no fact of the understanding any more. */
export function removedFactsOf(detail: IdeasDetail, angleId: string): string[] {
  const angle = detail.ideas?.angles.find((a) => a.id === angleId);
  if (!angle) return [];
  const facts = new Set((detail.understanding?.facts ?? []).map((f) => f.id));
  return claimFactIds(angle).filter((id) => !facts.has(id));
}

/** The images made for a claim, and how many of them the user kept. */
export function claimImages(
  detail: IdeasDetail,
  angleId: string
): { ads: IdeasAd[]; kept: number } {
  const ads = detail.ads.filter((ad) => ad.angle_id === angleId);
  return { ads, kept: ads.filter((ad) => ad.kept).length };
}

/**
 * What the claim form starts from: the claim's words and the facts it names
 * that still exist, in the understanding's order.
 */
export function claimFormStart(
  detail: IdeasDetail,
  angle: Angle
): EditClaimBody {
  const named = new Set(claimFactIds(angle));
  return {
    proposition: angle.proposition,
    rationale: angle.rationale,
    fact_ids: (detail.understanding?.facts ?? [])
      .map((fact) => fact.id)
      .filter((id) => named.has(id)),
  };
}

/**
 * Whether saving the form changes anything: other words, other facts, or a
 * deleted fact the claim still names (saving takes it off).
 */
export function claimChanged(
  detail: IdeasDetail,
  angle: Angle,
  body: EditClaimBody
): boolean {
  const start = claimFormStart(detail, angle);
  const set = (ids: string[] | undefined) => [...(ids ?? [])].sort().join(' ');
  return (
    body.proposition.trim() !== start.proposition ||
    body.rationale.trim() !== start.rationale ||
    set(body.fact_ids) !== set(start.fact_ids) ||
    removedFactsOf(detail, angle.id).length > 0
  );
}
