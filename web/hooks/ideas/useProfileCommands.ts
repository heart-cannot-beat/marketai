import { useCallback, useRef } from 'react';
import { useSWRConfig } from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { ReconciliationRequired } from '@/lib/domain-client/intent';
import type {
  DomainResultFor,
  ProblemDetails,
} from '@/lib/domain-client/result-types';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import type { Understanding } from '@/lib/ideas/profile-view';
import { ideasProjectKey, type Loaded } from './useIdeasProject';

/** How a change or a check on the product profile ended. */
export type ProfileOutcome =
  | { kind: 'done' }
  /** Refused; `problem.code` says why (409 `stale_marketing_revision`: someone changed it first). */
  | { kind: 'refused'; problem: ProblemDetails }
  /** No answer arrived; asking again repeats the same command. */
  | { kind: 'uncertain' };

/** Sends one of the two commands under the given action id. */
type Send = (
  action: string
) => Promise<
  | DomainResultFor<'edit_marketing_ideas_understanding'>
  | DomainResultFor<'verify_marketing_ideas'>
  | ReconciliationRequired
>;

export interface ProfileCommands {
  /** Sends the whole understanding back, with material the user adds for a new quote. */
  editUnderstanding: (
    understanding: Understanding,
    sourceAddition?: string
  ) => Promise<ProfileOutcome>;
  /** The user read this fact and says it is true: it becomes checked. */
  checkFact: (factId: string) => Promise<ProfileOutcome>;
}

/**
 * The product profile's two commands (ADR 0018, #198, #202):
 * `edit_marketing_ideas_understanding` and `verify_marketing_ideas`. Both
 * carry the revision the user saw (If-Match); the answer is the project as it
 * is now, written into the ideas page's cache so both views agree.
 */
export function useProfileCommands(
  projectId: string,
  detail: IdeasDetail | undefined
): ProfileCommands {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const { mutate } = useSWRConfig();
  // One action id per intended command, kept until it is answered, so asking
  // again after a lost reply replays the same command (idempotency).
  const actions = useRef(new Map<string, string>());
  const revision = detail?.revision;

  const run = useCallback(
    async (identity: string, send: Send): Promise<ProfileOutcome> => {
      const action = actions.current.get(identity) ?? crypto.randomUUID();
      actions.current.set(identity, action);
      let result;
      try {
        result = await send(action);
      } catch {
        return { kind: 'uncertain' };
      }
      if (result.kind === 'ok') {
        actions.current.delete(identity);
        const loaded: Loaded = { detail: result.data };
        await mutate(ideasProjectKey(current.workspaceId, projectId), loaded, {
          revalidate: false,
        });
        return { kind: 'done' };
      }
      if (result.kind === 'problem') {
        actions.current.delete(identity);
        return { kind: 'refused', problem: result.problem };
      }
      return { kind: 'uncertain' };
    },
    [current.workspaceId, mutate, projectId]
  );

  const editUnderstanding = useCallback(
    (understanding: Understanding, sourceAddition?: string) => {
      const addition = sourceAddition?.trim() || undefined;
      const body = {
        understanding,
        ...(addition ? { source_addition: addition } : {}),
      };
      return run(`edit:${revision}:${JSON.stringify(body)}`, (action) =>
        gateway.editMarketingIdeasUnderstanding({
          workspaceSlug: current.slug,
          actionInstanceId: action,
          expectedRevision: revision,
          projectId,
          body,
        })
      );
    },
    [current.slug, gateway, projectId, revision, run]
  );

  const checkFact = useCallback(
    (factId: string) =>
      run(`check:${revision}:${factId}`, (action) =>
        gateway.verifyMarketingIdeas({
          workspaceSlug: current.slug,
          actionInstanceId: action,
          expectedRevision: revision,
          projectId,
          body: {
            fact_ids: [factId],
            appearance_confirmed: [],
            accepted_findings: [],
          },
        })
      ),
    [current.slug, gateway, projectId, revision, run]
  );

  return { editUnderstanding, checkFact };
}
