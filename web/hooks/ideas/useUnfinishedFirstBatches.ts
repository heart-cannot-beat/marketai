import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { DomainProblemError } from '@/lib/i18n/domain-errors';

export type UnfinishedFirstBatch = components['schemas']['FirstBatchListItem'];

/** How often the list is asked again while one of its readings is on its way. */
export const UNFINISHED_POLL_MS = 5_000;

/**
 * The first batches that have no product yet and have not ended
 * (`list_marketing_first_batches`, #204): a reading the user left, or one
 * stopped before the product existed (several models on the page). Nothing
 * else lists them, so the overview and the start page show them as the way
 * back to their reading page.
 */
export function useUnfinishedFirstBatches() {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const query = useSWR(
    ['marketing-first-batches', current.workspaceId],
    async () => {
      const result = await gateway.listMarketingFirstBatches({
        workspaceSlug: current.slug,
      });
      if (result.kind === 'ok')
        return result.data.items as UnfinishedFirstBatch[];
      throw new DomainProblemError(result.problem);
    },
    {
      refreshInterval: (latest) =>
        latest?.some((batch) => !batch.needs_attention)
          ? UNFINISHED_POLL_MS
          : 0,
    }
  );
  return {
    batches: query.data ?? [],
    /** Asked and answered: an empty list then means there is none. */
    loaded: query.data !== undefined,
    failed: Boolean(query.error),
  };
}
