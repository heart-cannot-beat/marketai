import { useCallback, useEffect, useState } from 'react';
import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import {
  queryOf,
  type TestAnswers,
  type TestQuote,
} from '@/lib/tests/take-to-test';

/** How long the page waits after the last keystroke before asking again. */
export const QUOTE_DEBOUNCE_MS = 350;

type Loaded =
  | { kind: 'ok'; data: TestQuote; query: string }
  | { kind: 'problem'; problem: ProblemDetails; query: string };

export type UseTestQuote =
  /** The three answers are not all given yet: nothing to ask. */
  | { kind: 'idle'; refresh: () => Promise<void> }
  | { kind: 'loading'; refresh: () => Promise<void> }
  | {
      kind: 'ok';
      data: TestQuote;
      /** The quote answers exactly the answers on the page now; only then may it start a test. */
      current: boolean;
      refresh: () => Promise<void>;
    }
  | {
      kind: 'problem';
      problem: ProblemDetails;
      current: boolean;
      refresh: () => Promise<void>;
    };

function useDebounced<T>(value: T, delay: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return settled;
}

/**
 * The test quote (`get_marketing_test_quote`, #248) for the answers on the
 * "take to test" screen: what the round compares, whether its money is
 * enough, when the result is due and what starting costs at most. Read
 * only. It asks again a moment after the answers change, and whenever the
 * kept ads change (`signature`); the last quote stays on screen meanwhile,
 * marked as not current, so the start button never confirms a quote of
 * other answers.
 */
export function useTestQuote(
  projectId: string,
  query: TestAnswers | null,
  signature: string
): UseTestQuote {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const serialized = query ? JSON.stringify(query) : null;
  const settled = useDebounced(serialized, QUOTE_DEBOUNCE_MS);
  const swr = useSWR<Loaded>(
    settled
      ? [
          'marketing-test-quote',
          current.workspaceId,
          projectId,
          signature,
          settled,
        ]
      : null,
    async () => {
      const result = await gateway.getMarketingTestQuote({
        workspaceSlug: current.slug,
        projectId,
        query: queryOf(JSON.parse(settled!) as TestAnswers),
      });
      return result.kind === 'ok'
        ? { kind: 'ok', data: result.data, query: settled! }
        : {
            kind: 'problem',
            problem: (result as { problem: ProblemDetails }).problem,
            query: settled!,
          };
    },
    { keepPreviousData: true, revalidateOnFocus: false }
  );
  const { mutate } = swr;
  const refresh = useCallback(async () => {
    await mutate();
  }, [mutate]);

  if (!serialized) return { kind: 'idle', refresh };
  const loaded = swr.data;
  if (!loaded) return { kind: 'loading', refresh };
  const fresh = loaded.query === serialized && !swr.isValidating;
  return loaded.kind === 'ok'
    ? { kind: 'ok', data: loaded.data, current: fresh, refresh }
    : { kind: 'problem', problem: loaded.problem, current: fresh, refresh };
}
