import env from '@/lib/env';

const FEATURE_AREAS = new Set([
  'products',
  'tests',
  'generate',
  'assets',
  'experiments',
  'legacy-experiments',
  'localization',
  'scale',
  'library',
]);
/** Areas of the ads-first opening (ADR 0018); they exist only while it is on. */
const ADS_FIRST_AREAS = new Set(['overview', 'competitors']);
/** Every member's, though a Member sees only part of it (#126). */
const MEMBER_AREAS = new Set(['settings']);
const OWNER_ONLY_AREAS = new Set(['members']);

/**
 * Where `/w/{slug}` lands: the overview while the ads-first switch is on
 * (ADR 0018, design §5.6), the marketing tests otherwise (#162).
 */
export function workspaceHome(
  slug: string,
  adsFirst: boolean = env.adsFirstEnabled
): string {
  return `/w/${slug}/${adsFirst ? 'overview' : 'experiments'}`;
}

/**
 * Where "New product" goes. The product intake stays the destination until
 * the start page exists (S7 of the ads-first opening points it there while
 * the switch is on); with the switch off it never changes.
 */
export function newProductHref(slug: string): string {
  return `/w/${slug}/products/new`;
}

export function computeTargetRoute(params: {
  targetSlug: string;
  targetRole: 'OWNER' | 'MEMBER';
  currentPathname: string;
  adsFirst?: boolean;
}): string {
  const {
    targetSlug,
    targetRole,
    currentPathname,
    adsFirst = env.adsFirstEnabled,
  } = params;
  const base = `/w/${targetSlug}`;
  const home = workspaceHome(targetSlug, adsFirst);

  const match = currentPathname.match(/^\/w\/[^/]+\/([^/?#]+)/);
  const currentArea = match?.[1];

  if (!currentArea) {
    return home;
  }

  if (
    FEATURE_AREAS.has(currentArea) ||
    MEMBER_AREAS.has(currentArea) ||
    (adsFirst && ADS_FIRST_AREAS.has(currentArea))
  ) {
    return `${base}/${currentArea}`;
  }

  if (OWNER_ONLY_AREAS.has(currentArea) && targetRole === 'OWNER') {
    return `${base}/${currentArea}`;
  }

  // Includes a product's views (`/p/{productId}/...`): a product belongs to
  // one Workspace, so another Workspace never opens the same id.
  return home;
}
