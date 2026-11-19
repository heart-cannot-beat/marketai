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
  /** The ad of the ideas this slot is for (`CreativeCombination.id`); never shown. */
  combinationId?: string;
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

/** An image of content its place no longer holds (#198): it needs making again. */
const changed = (item: IdeasAd): boolean =>
  (item.reasons ?? []).some((reason) => reason.code === 'ideas_changed');

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
        combinationId: combination.id,
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
          // An image of earlier content (another claim or other copy here,
          // #224, #228) does not carry the words the ideas hold now.
          headline: changed(item) ? null : base.headline,
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
      // No image says what the ideas hold here now: the place waits for one.
      else if (made.every(changed))
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

/**
 * True while something the page shows is still on its way: an image being
 * made, a drafting call, a check (#197), a crop (#200), or a first batch that
 * is still making or checking its ads.
 */
export function stillWorking(detail: IdeasDetail | undefined): boolean {
  if (!detail) return false;
  if (detail.pending.some((item) => item.state !== 'failed')) return true;
  const call = detail.latest_call?.state;
  if (call === 'queued' || call === 'running') return true;
  if (detail.ads.some((item) => checkRunning(item.check))) return true;
  if ((detail.image_edits ?? []).some((edit) => edit.state === 'queued'))
    return true;
  return firstBatchChecking(detail);
}

export type IdeasCheck = components['schemas']['IdeasAdCheck'];
export type IdeasFinding = components['schemas']['IdeasCheckFinding'];

const checkRunning = (check: IdeasCheck | null | undefined) =>
  check?.state === 'queued' || check?.state === 'running';

/**
 * The first batch has made every image and is checking them (#197): the
 * images are on the page, their checks land one by one.
 */
export function firstBatchChecking(detail: IdeasDetail): boolean {
  return (detail.first_batch?.state as string | undefined) === 'checking';
}

/**
 * Where an ad's automatic check stands, as the page shows it (#197):
 * - `checking`: queued or running, or the first batch is still on its way to it;
 * - `findings`: finished, and some findings are not handled yet;
 * - `clear`: finished, nothing left to handle;
 * - `unchecked`: no check yet (a new version, #200), or it failed.
 */
export type CheckView = 'checking' | 'findings' | 'clear' | 'unchecked';

export function checkView(ad: IdeasAd, detail: IdeasDetail): CheckView {
  const check = ad.check;
  if (checkRunning(check) || check?.state === 'uncertain') return 'checking';
  if (!check || check.state === 'failed')
    return firstBatchChecking(detail) && ad.origin === 'generated'
      ? 'checking'
      : 'unchecked';
  return openFindings(ad).length > 0 ? 'findings' : 'clear';
}

/**
 * The findings of an ad's newest check the user has not handled yet, as the
 * server counts them (`reasons[findings_open].finding_ids`, #198), in the
 * check's order. A finding is never summed up: each is shown on its own.
 */
export function openFindings(ad: IdeasAd): IdeasFinding[] {
  const open = new Set(
    ad.reasons
      ?.filter((reason) => reason.code === 'findings_open')
      .flatMap((reason) => reason.finding_ids ?? []) ?? []
  );
  return (ad.check?.findings ?? []).filter((finding) =>
    open.has(finding.check_id)
  );
}

/**
 * The finding a no-photo ad's look confirmation settles (#198): the check
 * could not judge the product from the image alone. The page asks "please
 * confirm the product's look" for it, never shows it as a fault.
 */
export function settledByLook(finding: IdeasFinding): boolean {
  return (
    finding.check_id === 'product_fidelity' && finding.status === 'unknown'
  );
}

/** Whether an ad still waits for the user to confirm the product looks right. */
export function needsLook(ad: IdeasAd): boolean {
  return (ad.reasons ?? []).some(
    (reason) => reason.code === 'appearance_unconfirmed'
  );
}

/**
 * Every version of the ad `assetVersionId` belongs to (#200): the version it
 * was made from, back to the first, and everything made from any of them;
 * oldest first. Each new version stays an ad of its own.
 */
export function versionsOf(
  detail: IdeasDetail,
  assetVersionId: string
): IdeasAd[] {
  const byId = new Map(detail.ads.map((item) => [item.asset_version_id, item]));
  let root = byId.get(assetVersionId);
  const seen = new Set<string>();
  while (
    root?.source_asset_version_id &&
    byId.has(root.source_asset_version_id) &&
    !seen.has(root.asset_version_id)
  ) {
    seen.add(root.asset_version_id);
    root = byId.get(root.source_asset_version_id);
  }
  if (!root) return [];
  const family = new Set([root.asset_version_id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const item of detail.ads)
      if (
        !family.has(item.asset_version_id) &&
        item.source_asset_version_id &&
        family.has(item.source_asset_version_id)
      ) {
        family.add(item.asset_version_id);
        grew = true;
      }
  }
  return detail.ads
    .filter((item) => family.has(item.asset_version_id))
    .sort(
      (a, b) =>
        a.created_at.localeCompare(b.created_at) ||
        a.asset_version_id.localeCompare(b.asset_version_id)
    );
}

/** Where an ad sits in the ideas: its claim's number, the claim and its way of telling. */
export interface AdPlace {
  claimNumber: number | null;
  proposition: string | null;
  telling: Telling | null;
  headline: string | null;
  bodyCopy: string | null;
  /** The visual mechanism of its way of telling (`Expression.visual_mechanism`). */
  mechanism: string | null;
}

/**
 * Whether an ad is the user's own image (#200): uploaded, or a crop of one
 * (a crop keeps its source's intent, upload included). Such an ad cannot be
 * made again or processed by the supplier's tools.
 */
export function fromUpload(detail: IdeasDetail, ad: IdeasAd): boolean {
  const byId = new Map(detail.ads.map((item) => [item.asset_version_id, item]));
  const seen = new Set<string>();
  let item: IdeasAd | undefined = ad;
  while (item && !seen.has(item.asset_version_id)) {
    if (item.origin === 'uploaded') return true;
    if (item.origin !== 'image_edit' || !item.source_asset_version_id)
      return false;
    seen.add(item.asset_version_id);
    item = byId.get(item.source_asset_version_id);
  }
  return false;
}

/**
 * Where an ad sits in the ideas. An ad whose content the ideas no longer hold
 * (`ideas_changed`, #198) has no place: its combination's words now belong to
 * another ad, so none are shown as this one's.
 */
export function placeOf(detail: IdeasDetail, ad: IdeasAd): AdPlace {
  if ((ad.reasons ?? []).some((reason) => reason.code === 'ideas_changed'))
    return {
      claimNumber: null,
      proposition: null,
      telling: null,
      headline: null,
      bodyCopy: null,
      mechanism: null,
    };
  const angles = detail.ideas?.angles ?? [];
  const index = angles.findIndex((angle) => angle.id === ad.angle_id);
  const angle = index >= 0 ? angles[index] : undefined;
  const expression = angle?.expressions.find((e) => e.id === ad.expression_id);
  const combination = detail.ideas?.combinations.find(
    (c) => c.id === ad.combination_id
  );
  return {
    claimNumber: index >= 0 ? index + 1 : null,
    proposition: angle?.proposition ?? null,
    telling: telling(expression),
    headline: combination?.headline ?? null,
    bodyCopy: combination?.body_copy ?? null,
    mechanism: expression?.visual_mechanism ?? null,
  };
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
