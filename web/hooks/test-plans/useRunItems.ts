import { useEffect, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { PlanRunItem } from '@/lib/test-plans/plan-model';

/** Upper bound of pages read for one Run (a full test is at most 32
 * combinations x 4 variants, well under 3 pages of 100). */
const MAX_PAGES = 5;
const PAGE_SIZE = 100;

export interface UseRunItemsResult {
  items: PlanRunItem[];
  isLoading: boolean;
  failed: boolean;
}

/**
 * Every item of one generation Run (`list_generation_run_items`, which
 * carries each item's combination tags, bound AssetVersion and signed
 * thumbnail). It refetches whenever `version` changes -- the plan page
 * passes the Run's progress (done / failed / status), which its own plan
 * polling already keeps current, so no second polling loop runs here.
 */
export function useRunItems(
  runId: string | null | undefined,
  version: string
): UseRunItemsResult {
  const { current } = useWorkspace();
  const slug = current.slug;
  const gateway = useDomainGateway();
  const [items, setItems] = useState<PlanRunItem[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!runId) {
      setItems([]);
      return;
    }
    let cancelled = false;
    setIsLoading(true);
    (async () => {
      const collected: PlanRunItem[] = [];
      let cursor: string | undefined;
      for (let page = 0; page < MAX_PAGES; page++) {
        const result = await gateway.listGenerationRunItems({
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
          items?: unknown[];
          next_cursor?: string | null;
        };
        collected.push(...((data.items ?? []) as PlanRunItem[]));
        if (!data.next_cursor) break;
        cursor = data.next_cursor;
      }
      if (cancelled) return;
      setItems(collected);
      setFailed(false);
      setIsLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [gateway, runId, slug, version]);

  return { items, isLoading, failed };
}
