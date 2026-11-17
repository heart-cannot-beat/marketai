import { useCallback, useRef } from 'react';
import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import {
  stillWorking,
  withKept,
  type IdeasDetail,
} from '@/lib/ideas/ideas-view';

/** How often the page asks again while images are being made. */
export const IDEAS_POLL_MS = 3_000;

type Loaded =
  | { detail: IdeasDetail; problem?: undefined }
  | { detail?: undefined; problem: ProblemDetails };

/** Why a keep did not go through, for the page to say. */
export class KeepFailed extends Error {
  constructor(
    readonly problem: ProblemDetails | null,
    readonly uncertain = false
  ) {
    super('ideas-keep-failed');
  }
}

export interface UseIdeasProjectResult {
  detail: IdeasDetail | undefined;
  /** The read was refused or did not arrive. */
  failed: boolean;
  isLoading: boolean;
  refresh: () => Promise<void>;
  /** Marks one ad kept or not; the mark shows at once and is put back if refused. */
  setKept: (assetVersionId: string, kept: boolean) => Promise<void>;
}

/**
 * One product's ad ideas (`get_marketing_ideas`, ADR 0018) for the ideas
 * page. It asks again every few seconds while an image is queued or being
 * made, or a drafting call is running, and stops once nothing is on its way.
 */
export function useIdeasProject(projectId: string): UseIdeasProjectResult {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const query = useSWR<Loaded>(
    ['marketing-ideas', current.workspaceId, projectId],
    async () => {
      const result = await gateway.getMarketingIdeas({
        workspaceSlug: current.slug,
        projectId,
      });
      return result.kind === 'ok'
        ? { detail: result.data }
        : { problem: result.problem };
    },
    {
      refreshInterval: (latest) =>
        stillWorking(latest?.detail) ? IDEAS_POLL_MS : 0,
    }
  );

  // One action id per intended mark, kept until it is answered, so asking
  // again after a lost reply replays the same command (idempotency).
  const actions = useRef(new Map<string, string>());
  const { mutate } = query;
  const setKept = useCallback(
    async (assetVersionId: string, kept: boolean) => {
      const identity = `${projectId}:${assetVersionId}:${kept}`;
      const action = actions.current.get(identity) ?? crypto.randomUUID();
      actions.current.set(identity, action);
      await mutate(
        async () => {
          const result = await gateway.setMarketingAdKept({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            projectId,
            assetVersionId,
            body: { kept },
          });
          if (result.kind === 'ok') {
            actions.current.delete(identity);
            return { detail: result.data };
          }
          if (result.kind === 'problem') {
            actions.current.delete(identity);
            throw new KeepFailed(result.problem);
          }
          throw new KeepFailed(null, true);
        },
        {
          optimisticData: (latest) =>
            latest?.detail
              ? { detail: withKept(latest.detail, assetVersionId, kept) }
              : (latest as Loaded),
          rollbackOnError: true,
          populateCache: true,
          revalidate: false,
        }
      );
    },
    [current.slug, gateway, mutate, projectId]
  );

  const refresh = useCallback(async () => {
    await mutate();
  }, [mutate]);

  return {
    detail: query.data?.detail,
    failed: Boolean(query.data?.problem) || Boolean(query.error),
    isLoading: query.data === undefined && !query.error,
    refresh,
    setKept,
  };
}
