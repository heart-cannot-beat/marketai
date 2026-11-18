import type { components } from '@/lib/domain-client/generated/domain-v1';
import {
  needsLook,
  openFindings,
  settledByLook,
  type IdeasAd,
  type IdeasDetail,
  type IdeasFinding,
} from './ideas-view';
import { adsWithPhoto, uncheckedPhotosOf, type IdeasPhoto } from './photos';

/**
 * The check before downloading (ADR 0018 §3, design §5.3, #198, #201): what
 * the kept ads need the user to confirm, all at once. Only the kept ads
 * count, and of them only what this confirmation can settle:
 *
 * - `ready`: the ads that can be downloaded once the user confirms below
 *   (already exportable, or a draft only for facts, photos, looks or
 *   findings);
 * - `waiting`: drafts a confirmation cannot settle yet (no check, still being
 *   checked);
 * - `redo`: ads that need redoing (#198): checking cannot rescue them.
 *
 * Facts, photos, looks and findings are listed for the `ready` ads only, so
 * nothing the user ticks is for an ad this download leaves out. A photo is
 * checked here the way the product profile checks it (#229): the same
 * declaration, the same command.
 */

type Fact = components['schemas']['Fact'];
type AdReason = components['schemas']['AdReason'];

/** Why a kept draft cannot be downloaded after this confirmation. */
export type WaitingWhy =
  /** No check yet (a new version), or the check failed: it needs a (paid) check. */
  | 'check_needed'
  /** Its check, or the image's own generation check, is still running. */
  | 'checking'
  /** The check's outcome is not known yet; it is being reconciled. */
  | 'check_uncertain'
  /** The check was made against facts that changed since. */
  | 'check_outdated'
  /**
   * A product photo it used is not checked, and it is not one of the
   * photos these ideas list to check (#229): the product profile is where
   * to look. Not expected; kept so such an ad is never offered as ready.
   */
  | 'photo';

export interface WaitingAd {
  ad: IdeasAd;
  why: WaitingWhy;
}

export interface RedoAd {
  ad: IdeasAd;
  reasons: AdReason[];
}

/**
 * One unchecked product photo the ready ads were made with (#229): checking
 * it here declares it is a real photo of this product that may be used in
 * ads.
 */
export interface SheetPhoto {
  photo: IdeasPhoto;
  /** The ready ads made with it: they wait for this photo. */
  ready: IdeasAd[];
  /** Every ad of these ideas made with it: rejecting it makes them need redoing. */
  used: IdeasAd[];
}

/** One ad's findings to handle: accepted ("this is fine") per ad, check and finding (#198). */
export interface AdFindings {
  ad: IdeasAd;
  checkId: string;
  findings: IdeasFinding[];
}

export interface CheckSheetModel {
  kept: number;
  ready: IdeasAd[];
  waiting: WaitingAd[];
  redo: RedoAd[];
  /** Facts the ready ads cite that the user has not checked, in the understanding's order. */
  facts: Fact[];
  /** Facts the ready ads cite that are checked already: nothing to do. */
  checkedFacts: number;
  /** Unchecked product photos the ready drafts were made with, in the reading's order. */
  photos: SheetPhoto[];
  /** Ready ads made without a product photo whose look is not confirmed yet. */
  looks: IdeasAd[];
  /** Ready ads whose product photos are all checked already. */
  withPhoto: IdeasAd[];
  findings: AdFindings[];
}

const WAITING: Partial<Record<AdReason['code'], WaitingWhy>> = {
  check_missing: 'check_needed',
  check_failed: 'check_needed',
  check_in_progress: 'checking',
  image_check_in_progress: 'checking',
  check_uncertain: 'check_uncertain',
  check_outdated: 'check_outdated',
};

/** The first reason, by importance, that keeps a draft out of this download. */
const WAITING_ORDER: WaitingWhy[] = [
  'checking',
  'check_uncertain',
  'check_needed',
  'check_outdated',
  'photo',
];

function waitingWhy(detail: IdeasDetail, ad: IdeasAd): WaitingWhy | null {
  const reasons = ad.reasons ?? [];
  const found = new Set(
    reasons
      .map((reason) => WAITING[reason.code])
      .filter((why): why is WaitingWhy => Boolean(why))
  );
  // A photo to check is settled here, as long as these ideas list it (#229).
  if (
    reasons.some((reason) => reason.code === 'photo_unverified') &&
    uncheckedPhotosOf(detail, ad).length === 0
  )
    found.add('photo');
  return WAITING_ORDER.find((why) => found.has(why)) ?? null;
}

export function checkSheet(detail: IdeasDetail): CheckSheetModel {
  const kept = detail.ads.filter((item) => item.kept);
  const ready: IdeasAd[] = [];
  const waiting: WaitingAd[] = [];
  const redo: RedoAd[] = [];
  for (const item of kept) {
    if (item.status === 'needs_redo') {
      redo.push({ ad: item, reasons: item.reasons ?? [] });
      continue;
    }
    const why = item.status === 'exportable' ? null : waitingWhy(detail, item);
    if (why) waiting.push({ ad: item, why });
    else ready.push(item);
  }

  // An exportable ad has nothing left to confirm; only the drafts' facts are asked.
  const cited = new Set(ready.flatMap((item) => item.cited_fact_ids ?? []));
  const toConfirm = new Set(
    ready
      .filter((item) => item.status !== 'exportable')
      .flatMap((item) => item.cited_fact_ids ?? [])
  );
  const facts = (detail.understanding?.facts ?? []).filter((fact) =>
    cited.has(fact.id)
  );
  const unchecked = facts.filter(
    (fact) => toConfirm.has(fact.id) && fact.confirmation !== 'confirmed'
  );

  const drafts = ready.filter((item) => item.status !== 'exportable');
  const toCheck = new Set(
    drafts.flatMap((item) =>
      uncheckedPhotosOf(detail, item).map((photo) => photo.photo_id)
    )
  );
  const photos: SheetPhoto[] = (detail.photos ?? [])
    .filter((photo) => toCheck.has(photo.photo_id))
    .map((photo) => ({
      photo,
      ready: drafts.filter((item) =>
        (item.photo_ids ?? []).includes(photo.photo_id)
      ),
      used: adsWithPhoto(detail, photo.photo_id),
    }));

  const looks = ready.filter(needsLook);
  const findings: AdFindings[] = [];
  for (const item of ready) {
    const lookPending = needsLook(item);
    const open = openFindings(item).filter(
      // Confirming the look settles this one (#198): asked once, as the look.
      (finding) => !(lookPending && settledByLook(finding))
    );
    if (open.length > 0 && item.check)
      findings.push({ ad: item, checkId: item.check.id, findings: open });
  }

  return {
    kept: kept.length,
    ready,
    waiting,
    redo,
    facts: unchecked,
    checkedFacts: facts.length - unchecked.length,
    photos,
    looks,
    withPhoto: ready.filter(
      (item) =>
        item.used_product_photo &&
        !(item.reasons ?? []).some(
          (reason) => reason.code === 'photo_unverified'
        )
    ),
    findings,
  };
}

/** What the user ticked on the sheet; each key is one thing to confirm. */
export interface SheetTicks {
  /** `factKey` of each fact read and confirmed as true. */
  facts: ReadonlySet<string>;
  /** Photos the user declares are real photos of this product they may use in ads. */
  photos: ReadonlySet<string>;
  /** Ads whose product the user confirms looks right. */
  looks: ReadonlySet<string>;
  /** `findingKey` of each finding accepted as it is ("this is fine"). */
  findings: ReadonlySet<string>;
}

/** A fact as the user read it: a tick never carries over to changed words. */
export const factKey = (fact: Fact) =>
  `${fact.id}\u0000${fact.statement}\u0000${fact.source_quote}`;

/**
 * One finding of one check of one ad (#198): accepting it holds for that
 * check only, so a newer check has to be looked at again.
 */
export const findingKey = (
  assetVersionId: string,
  checkId: string,
  findingId: string
) => `${assetVersionId}\u0000${checkId}\u0000${findingId}`;

/** How many things are left to tick. */
export function leftToTick(model: CheckSheetModel, ticks: SheetTicks): number {
  return (
    model.facts.filter((fact) => !ticks.facts.has(factKey(fact))).length +
    model.photos.filter(({ photo }) => !ticks.photos.has(photo.photo_id))
      .length +
    model.looks.filter((item) => !ticks.looks.has(item.asset_version_id))
      .length +
    model.findings
      .flatMap(({ ad, checkId, findings }) =>
        findings.map((finding) =>
          findingKey(ad.asset_version_id, checkId, finding.check_id)
        )
      )
      .filter((key) => !ticks.findings.has(key)).length
  );
}

/** Every fact, photo, look and finding ticked: only then can the user confirm. */
export function allTicked(model: CheckSheetModel, ticks: SheetTicks): boolean {
  return leftToTick(model, ticks) === 0;
}

export type VerifyBody = components['schemas']['VerifyIdeas'];

/**
 * The one `verify_marketing_ideas` body for this confirmation (#198), or
 * `null` when there is nothing to record (every ad is exportable already).
 */
export function verifyBody(
  model: CheckSheetModel,
  ticks: SheetTicks
): VerifyBody | null {
  const body: VerifyBody = {
    fact_ids: model.facts
      .filter((fact) => ticks.facts.has(factKey(fact)))
      .map((fact) => fact.id),
    appearance_confirmed: model.looks
      .map((item) => item.asset_version_id)
      .filter((id) => ticks.looks.has(id)),
    accepted_findings: model.findings
      .map(({ ad, checkId, findings }) => ({
        asset_version_id: ad.asset_version_id,
        check_id: checkId,
        finding_ids: findings
          .map((finding) => finding.check_id)
          .filter((id) =>
            ticks.findings.has(findingKey(ad.asset_version_id, checkId, id))
          ),
      }))
      .filter((item) => item.finding_ids.length > 0),
  };
  const photos = model.photos
    .map(({ photo }) => photo.photo_id)
    .filter((id) => ticks.photos.has(id));
  if (photos.length > 0) body.photo_ids = photos;
  return body.fact_ids?.length ||
    body.appearance_confirmed?.length ||
    body.accepted_findings?.length ||
    body.photo_ids?.length
    ? body
    : null;
}

/** The reasons that make an ad need redoing (#198); checking cannot settle them. */
export const REDO_REASONS: ReadonlySet<string> = new Set([
  'ideas_changed',
  'facts_changed',
  'facts_removed',
  'photo_unavailable',
  'image_blocked',
]);

/**
 * The reasons worth saying for one ad: for an ad that needs redoing, only
 * why it needs redoing -- what a check would settle no longer matters.
 */
export function mainReasons(ad: {
  status: string;
  reasons?: AdReason[] | null;
}): AdReason['code'][] {
  const codes = Array.from(
    new Set((ad.reasons ?? []).map((reason) => reason.code))
  );
  return ad.status === 'needs_redo'
    ? codes.filter((code) => REDO_REASONS.has(code))
    : codes;
}

/** One ad a download refused, as `ads_not_exportable` names it (#198). */
export interface RefusedAd {
  asset_version_id: string;
  status: string;
  reasons: AdReason[];
}

/** The refused ads of a 409 `ads_not_exportable`, read defensively. */
export function refusedAds(
  params: Record<string, unknown> | null | undefined
): RefusedAd[] {
  const ads = params?.ads;
  if (!Array.isArray(ads)) return [];
  return ads.filter(
    (item): item is RefusedAd =>
      typeof item === 'object' &&
      item !== null &&
      typeof (item as RefusedAd).asset_version_id === 'string'
  );
}
