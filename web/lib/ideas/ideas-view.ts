import type { components } from '@/lib/domain-client/generated/domain-v1';

/**
 * What the ad ideas page shows (ADR 0018, design §5.3, #196), read from
 * `get_marketing_ideas`: the ads grouped by claim, each image made, being
 * made, failed or not made yet; and the counts the page and its bottom bar
 * need. Nothing here is computed beyond what the detail says: a claim's
 * competitor line, for one, exists only when the ideas carry a scan.
 */

export type IdeasDetail = components['schemas']['IdeasProjectDetail'];
export type IdeasAd = components['schemas']['AdSummary'];
type Angle = components['schemas']['Angle'];
type Expression = components['schemas']['Expression'];

/** The way of telling an ad uses, as the page names it. */
export interface Telling {
  name: string;
  description: string;
}

interface SlotBase {
  /** Stable React key; never shown. */
  key: string;
  /** The ad's headline, for the image's text alternative. */
  headline: string | null;
  telling: Telling | null;
}

/** One place for an ad on the page: a made image, or where one will be. */
export type IdeaSlot =
  | (SlotBase & { kind: 'ad'; ad: IdeasAd })
  /** Queued or running. */
  | (SlotBase & { kind: 'making' })
  /** The latest attempt at this ad failed. */
  | (SlotBase & { kind: 'failed' })
  /** Nothing has been asked for this ad yet. */
  | (SlotBase & { kind: 'not_made' });

export type CompetitorLine = 'overlap' | 'unclaimed' | null;

export interface ClaimGroup {
  /** Stable React key; never shown. */
  key: string;
  /** 1-based, in the order the ideas list the claims. */
  number: number;
  proposition: string;
  rationale: string;
  competitors: CompetitorLine;
  slots: IdeaSlot[];
}

export interface IdeasView {
  claims: ClaimGroup[];
  /** Ads whose claim is no longer in the ideas; they can still be kept. */
  earlier: IdeaSlot[];
  adCount: number;
  keptCount: number;
  makingCount: number;
  withoutPhotoCount: number;
}

const telling = (expression: Expression | undefined): Telling | null =>
  expression
    ? { name: expression.name, description: expression.description }
    : null;

/**
 * A claim's competitor line (design §5.3): only when the ideas cite a
 * competitor scan. The planner leaves `competitor_overlap` empty today (#190),
 * so the line appears once ideas carry a scan again; no client-side guess.
 */
function competitorLine(angle: Angle, scanned: boolean): CompetitorLine {
  if (!scanned) return null;
  return (angle.competitor_overlap ?? []).length > 0 ? 'overlap' : 'unclaimed';
}

export function ideasView(detail: IdeasDetail): IdeasView {
  const ideas = detail.ideas;
  const angles = ideas?.angles ?? [];
  const combinations = ideas?.combinations ?? [];
  const scanned = Boolean(ideas?.competitor_snapshot_id);

  const adsByCombination = new Map<string, IdeasAd[]>();
  for (const item of detail.ads)
    adsByCombination.set(item.combination_id, [
      ...(adsByCombination.get(item.combination_id) ?? []),
      item,
    ]);
  const pendingByCombination = new Map(
    detail.pending.map((item) => [item.combination_id, item.state])
  );

  const placed = new Set<string>();
  const claims = angles.map((angle, index): ClaimGroup => {
    const slots: IdeaSlot[] = [];
    for (const combination of combinations) {
      if (combination.angle_id !== angle.id) continue;
      const base = {
        headline: combination.headline,
        telling: telling(
          angle.expressions.find((e) => e.id === combination.expression_id)
        ),
      };
      const made = adsByCombination.get(combination.id) ?? [];
      for (const item of made) {
        placed.add(item.asset_version_id);
        slots.push({
          ...base,
          kind: 'ad',
          key: item.asset_version_id,
          ad: item,
        });
      }
      const pending = pendingByCombination.get(combination.id);
      if (pending === 'failed')
        slots.push({
          ...base,
          kind: 'failed',
          key: `${combination.id}:failed`,
        });
      else if (pending)
        slots.push({
          ...base,
          kind: 'making',
          key: `${combination.id}:making`,
        });
      else if (made.length === 0)
        slots.push({
          ...base,
          kind: 'not_made',
          key: `${combination.id}:none`,
        });
    }
    // An ad of this claim whose combination the ideas no longer list (the
    // claim was redrafted) still belongs under its claim.
    for (const item of detail.ads) {
      if (placed.has(item.asset_version_id) || item.angle_id !== angle.id)
        continue;
      placed.add(item.asset_version_id);
      slots.push({
        kind: 'ad',
        key: item.asset_version_id,
        ad: item,
        headline: null,
        telling: telling(
          angle.expressions.find((e) => e.id === item.expression_id)
        ),
      });
    }
    return {
      key: angle.id,
      number: index + 1,
      proposition: angle.proposition,
      rationale: angle.rationale,
      competitors: competitorLine(angle, scanned),
      slots,
    };
  });

  const earlier: IdeaSlot[] = detail.ads
    .filter((item) => !placed.has(item.asset_version_id))
    .map((item) => ({
      kind: 'ad',
      key: item.asset_version_id,
      ad: item,
      headline: null,
      telling: null,
    }));

  return {
    claims,
    earlier,
    adCount: detail.ads.length,
    keptCount: detail.ads.filter((item) => item.kept).length,
    makingCount: detail.pending.filter((item) => item.state !== 'failed')
      .length,
    withoutPhotoCount: detail.ads.filter((item) => !item.used_product_photo)
      .length,
  };
}

/** True while something the page shows is still on its way. */
export function stillWorking(detail: IdeasDetail | undefined): boolean {
  if (!detail) return false;
  if (detail.pending.some((item) => item.state !== 'failed')) return true;
  const call = detail.latest_call?.state;
  return call === 'queued' || call === 'running';
}

/** The detail with one ad's keep mark set, as the page shows it at once. */
export function withKept(
  detail: IdeasDetail,
  assetVersionId: string,
  kept: boolean
): IdeasDetail {
  return {
    ...detail,
    ads: detail.ads.map((item) =>
      item.asset_version_id === assetVersionId ? { ...item, kept } : item
    ),
  };
}
