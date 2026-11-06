import { useEffect, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { components } from '@/lib/domain-client/generated/domain-v1';

export type RunAssetVersion = components['schemas']['AssetVersionSummary'];

/** A full test is at most 32 combinations x 4 variants plus their
 * regenerations -- well under 10 pages of 100. */
const MAX_PAGES = 10;
const PAGE_SIZE = 100;

export interface UseRunAssetVersionsResult {
  versions: RunAssetVersion[];
  isLoading: boolean;
  failed: boolean;
}

/**
 * Every AssetVersion of one generation Run (`list_generation_assets?run_id`,
 * newest first), with its effective review value and signed thumbnail.
 * Read directly rather than through the gallery's SWR infinite key: that
 * key does not carry the page size, so sharing it with the gallery's
 * 50-per-page list of the same Run would mix the two. It refetches
 * whenever `version` changes -- the caller bumps it on Run progress and
 * after a review.
 */
export function useRunAssetVersions(
  runId: string | null | undefined,
  version: string
): UseRunAssetVersionsResult {
  const { current } = useWorkspace();
  const slug = current.slug;
  const gateway = useDomainGateway();
  const [versions, setVersions] = useState<RunAssetVersion[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!runId) {
      setVersions([]);
      return;
    }
    let cancelled = false;
    setIsLoading(true);
    (async () => {
      const collected: RunAssetVersion[] = [];
      let cursor: string | undefined;
      for (let page = 0; page < MAX_PAGES; page++) {
        const result = await gateway.listGenerationAssets({
          workspaceSlug: slug,
          runId,
          cursor,
          limit: PAGE_SIZE,
        });
        if (cancelled) return;
        if (result.kind !== 'ok') {
          setFailed(true);
          setIsLoading(false);
          return;
        }
        const data = result.data as {
          items?: RunAssetVersion[];
          next_cursor?: string | null;
        };
        collected.push(...(data.items ?? []));
        if (!data.next_cursor) break;
        cursor = data.next_cursor;
      }
      if (cancelled) return;
      setVersions(collected);
      setFailed(false);
      setIsLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [gateway, runId, slug, version]);

  return { versions, isLoading, failed };
}
