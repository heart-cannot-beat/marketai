import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { DomainProblem } from '@/lib/i18n/domain-errors';

/**
 * Why an image may not leave through the creative library (Issue #219):
 * it is an ad of ads-first ideas that is still a draft, or one that needs
 * redoing. The server derives it; the pages only say it in plain words.
 */
export type AssetExportBlock = components['schemas']['AssetExportBlock'];

/** One ad a refusal names (`ads_not_exportable`'s `params.ads`). */
export interface RefusedAd extends AssetExportBlock {
  asset_version_id: string;
}

export interface ExportRefusal {
  /** The first refused ads, in the order they were asked for. */
  ads: RefusedAd[];
  /** How many ads were refused in all; `ads` may name fewer. */
  count: number;
}

type Translate = (key: string, options?: Record<string, unknown>) => string;

/** The reason codes the `assets` copy has a sentence for. */
const REASONS = new Set([
  'ideas_changed',
  'facts_changed',
  'facts_removed',
  'photo_unavailable',
  'image_blocked',
  'facts_unverified',
  'photo_unverified',
  'appearance_unconfirmed',
  'image_check_in_progress',
  'check_missing',
  'check_in_progress',
  'check_failed',
  'check_uncertain',
  'findings_open',
  'check_outdated',
]);

/** "Ad draft · can't be exported yet" / "Needs redoing · can't be exported". */
export function exportBlockLabel(
  t: Translate,
  block: Pick<AssetExportBlock, 'status'>
): string {
  return t(
    block.status === 'needs_redo'
      ? 'export-block-needs-redo'
      : 'export-block-draft'
  );
}

/** "Ad draft" / "Ad that needs redoing": what the ad is, in a list of refused ads. */
export function exportBlockStatus(
  t: Translate,
  block: Pick<AssetExportBlock, 'status'>
): string {
  return t(
    block.status === 'needs_redo'
      ? 'export-block-status-needs-redo'
      : 'export-block-status-draft'
  );
}

/**
 * The block's reasons as sentences, each once and in the server's order. A
 * code this page has no sentence for reads as "something isn't checked yet"
 * rather than as the code itself.
 */
export function exportBlockReasons(
  t: Translate,
  block: Pick<AssetExportBlock, 'reasons'>
): string[] {
  const sentences = (block.reasons ?? []).map((reason) =>
    t(
      REASONS.has(reason.code)
        ? `export-block-reason-${reason.code}`
        : 'export-block-reason-other'
    )
  );
  return Array.from(new Set(sentences));
}

/** The ads an `ads_not_exportable` refusal names, or `null` for any other problem. */
export function exportRefusal(
  problem: DomainProblem | null | undefined
): ExportRefusal | null {
  if (problem?.code !== 'ads_not_exportable') return null;
  const params = (problem.params ?? {}) as {
    ads?: RefusedAd[];
    count?: number;
  };
  const ads = Array.isArray(params.ads) ? params.ads : [];
  return {
    ads,
    count: typeof params.count === 'number' ? params.count : ads.length,
  };
}
