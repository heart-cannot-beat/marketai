/**
 * "导出本组" from the Run page (Issue #170). The export flow lives on the
 * Assets page and exports the gallery selection, which is page state, so
 * the Run page stashes the group's AssetVersion ids here and opens Assets
 * on the same Run; the Assets page consumes the stash once, selects those
 * versions and opens the export dialog. The ids stay out of the URL
 * (front-flow contract §1 principle 2). Storage can be unavailable; then
 * the user lands on the grouped view and presses 导出本组 there.
 */

interface GroupExportHandoff {
  runId: string;
  versionIds: string[];
}

function key(slug: string): string {
  return `assets:${slug}:group-export`;
}

export function stashGroupExport(
  slug: string,
  runId: string,
  versionIds: string[]
): void {
  try {
    const value: GroupExportHandoff = { runId, versionIds: [...versionIds] };
    window.sessionStorage.setItem(key(slug), JSON.stringify(value));
  } catch {
    // Convenience only.
  }
}

/** The stashed ids for `runId`, removing the stash; `null` when there is
 * none for this Run. A stash for another Run is left alone. */
export function takeGroupExport(slug: string, runId: string): string[] | null {
  try {
    const raw = window.sessionStorage.getItem(key(slug));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<GroupExportHandoff>;
    if (parsed.runId !== runId) return null;
    window.sessionStorage.removeItem(key(slug));
    const ids = Array.isArray(parsed.versionIds)
      ? parsed.versionIds.filter((id): id is string => typeof id === 'string')
      : [];
    return ids.length > 0 ? ids : null;
  } catch {
    return null;
  }
}
