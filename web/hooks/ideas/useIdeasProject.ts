import { useCallback, useMemo, useRef, useState } from 'react';
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

/** The SWR key of one project's ad ideas; the product profile writes its answers here too. */
export const ideasProjectKey = (workspaceId: string, projectId: string) =>
  ['marketing-ideas', workspaceId, projectId] as const;

export type Loaded =
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

/** A keep the page shows before the server has answered it. */
interface PendingKeep {
  assetVersionId: string;
  kept: boolean;
}

export interface UseIdeasProjectResult {
  detail: IdeasDetail | undefined;
  /** The read was refused or did not arrive. */
  failed: boolean;
  isLoading: boolean;
  refresh: () => Promise<void>;
  /**
   * Marks one ad kept or not; the mark shows at once and is put back if
   * refused. Marks go to the server one at a time, in the order they were
   * made (#285).
   */
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
    ideasProjectKey(current.workspaceId, projectId),
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

  const send = useCallback(
    async (assetVersionId: string, kept: boolean) => {
      const identity = `${projectId}:${assetVersionId}:${kept}`;
      const action = actions.current.get(identity) ?? crypto.randomUUID();
      actions.current.set(identity, action);
      const result = await gateway.setMarketingAdKept({
        workspaceSlug: current.slug,
        actionInstanceId: action,
        projectId,
        assetVersionId,
        body: { kept },
      });
      if (result.kind === 'ok') {
        actions.current.delete(identity);
        await mutate({ detail: result.data }, { revalidate: false });
        return;
      }
      if (result.kind === 'problem') {
        actions.current.delete(identity);
        throw new KeepFailed(result.problem);
      }
      throw new KeepFailed(null, true);
    },
    [current.slug, gateway, mutate, projectId]
  );

  // Two marks sent together can reach the server, and come back, in either
  // order, and each answer holds only what the server had when it took that
  // mark: the page would settle on whichever answer came last, even one
  // missing the other mark (#285). Sent one after the other, each answer
  // holds every earlier mark, so the newest answer is what the server holds.
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  // The marks not answered yet, in the order they were made, shown over the
  // newest answer; a refused one just drops out, leaving every other mark.
  const [pending, setPending] = useState<readonly PendingKeep[]>([]);
  const setKept = useCallback(
    async (assetVersionId: string, kept: boolean) => {
      const mark: PendingKeep = { assetVersionId, kept };
      setPending((marks) => [...marks, mark]);
      const turn = queue.current.then(() => send(assetVersionId, kept));
      queue.current = turn.catch(() => undefined);
      try {
        await turn;
      } finally {
        setPending((marks) => marks.filter((other) => other !== mark));
      }
    },
    [send]
  );

  const answered = query.data?.detail;
  const detail = useMemo(
    () =>
      answered &&
      pending.reduce(
        (shown, mark) => withKept(shown, mark.assetVersionId, mark.kept),
        answered
      ),
    [answered, pending]
  );

  const refresh = useCallback(async () => {
    await mutate();
  }, [mutate]);

  return {
    detail,
    failed: Boolean(query.data?.problem) || Boolean(query.error),
    isLoading: query.data === undefined && !query.error,
    refresh,
    setKept,
  };
}
