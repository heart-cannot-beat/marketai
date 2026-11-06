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
const OWNER_ONLY_AREAS = new Set(['settings', 'members']);

export function computeTargetRoute(params: {
  targetSlug: string;
  targetRole: 'OWNER' | 'MEMBER';
  currentPathname: string;
}): string {
  const { targetSlug, targetRole, currentPathname } = params;
  const base = `/w/${targetSlug}`;

  const match = currentPathname.match(/^\/w\/[^/]+\/([^/?#]+)/);
  const currentArea = match?.[1];

  if (!currentArea) {
    return `${base}/experiments`;
  }

  if (FEATURE_AREAS.has(currentArea)) {
    return `${base}/${currentArea}`;
  }

  if (OWNER_ONLY_AREAS.has(currentArea) && targetRole === 'OWNER') {
    return `${base}/${currentArea}`;
  }

  return `${base}/experiments`;
}
