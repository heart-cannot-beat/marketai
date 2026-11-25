import { useCallback, useMemo } from 'react';
import useSWR, { useSWRConfig, type ScopedMutator } from 'swr';
import { useSession } from 'next-auth/react';
import type { TeamMember, User } from '@prisma/client';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { DomainGateway } from '@/lib/domain-client/gateway';
import fetcher from '@/lib/fetcher';
import type { ProductTestState } from '@/lib/products/workspace-products';
import {
  emptyHistory,
  localDay,
  needsResult,
  productTestState,
  runningTest,
  type TestHistory,
} from '@/lib/tests/test-history';
import type { TestResult } from '@/lib/tests/test-in-progress';
import type { ApiResponse } from 'types';

/** The SWR key of a product's tests; starting or cancelling one refreshes it. */
export const productTestsKey = (workspaceId: string, productId: string) =>
  ['marketing-tests', workspaceId, productId] as const;

/** The overview's read of every product's tests, under the same prefix. */
const overviewTestsKey = (workspaceId: string, productIds: string[]) =>
  ['marketing-tests', workspaceId, 'overview', productIds.join('\n')] as const;

/**
 * A product's tests (`list_marketing_tests`, #255). A product with no card of
 * its own has none (`product_not_found`); any other problem is `null`, which
 * the views read as "could not read".
 */
export async function readProductTests(
  gateway: DomainGateway,
  workspaceSlug: string,
  productId: string
): Promise<TestHistory | null> {
  const result = await gateway.listMarketingTests({ workspaceSlug, productId });
  if (result.kind === 'ok') return result.data;
  if (
    result.kind === 'problem' &&
    (result as { problem: { code?: string } }).problem.code ===
      'product_not_found'
  )
    return emptyHistory(productId);
  return null;
}

/**
 * Read every product test list of the Workspace again: the "take to test"
 * screen, the test page and the history view call it after a command, so
 * the sidebar and the overview follow (#258).
 */
export function refreshProductTests(
  mutate: ScopedMutator,
  workspaceId: string
): Promise<unknown> {
  return mutate(
    (key) =>
      Array.isArray(key) &&
      key[0] === 'marketing-tests' &&
      key[1] === workspaceId
  );
}

/** `refreshProductTests` for the current Workspace. */
export function useRefreshProductTests(): () => Promise<void> {
  const { mutate } = useSWRConfig();
  const { current } = useWorkspace();
  const workspaceId = current.workspaceId;
  return useCallback(async () => {
    await refreshProductTests(mutate, workspaceId);
  }, [mutate, workspaceId]);
}

export interface UseTestHistoryResult {
  history: TestHistory | undefined;
  loading: boolean;
  /** The list could not be read; the view offers to read it again. */
  failed: boolean;
  refresh: () => Promise<void>;
}

/** One product's tests and earlier tests, each round with its reports (design §5.10). */
export function useTestHistory(productId: string): UseTestHistoryResult {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const refresh = useRefreshProductTests();
  const query = useSWR(
    productTestsKey(current.workspaceId, productId),
    () => readProductTests(gateway, current.slug, productId),
    { revalidateOnFocus: false }
  );
  return {
    history: query.data ?? undefined,
    loading: query.data === undefined && !query.error,
    failed: query.data === null || Boolean(query.error),
    refresh,
  };
}

/**
 * Each product's test in progress, as the sidebar and the overview count it
 * (#258): one `list_marketing_tests` per product, all at once, and the
 * round's result only where a report is in. Products the list cannot read
 * are left out, so they keep the steps their ads give them.
 */
export function useProductTestStates(
  productIds: string[],
  enabled: boolean
): {
  states: Readonly<Record<string, ProductTestState | null>>;
  loading: boolean;
  refresh: () => Promise<void>;
} {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const slug = current.slug;
  const key =
    enabled && productIds.length > 0
      ? overviewTestsKey(current.workspaceId, productIds)
      : null;
  const query = useSWR(
    key,
    async () => {
      const today = localDay();
      const entries = await Promise.all(
        productIds.map(async (productId) => {
          const history = await readProductTests(
            gateway,
            slug,
            productId
          ).catch(() => null);
          if (!history) return null;
          const running = runningTest(history);
          let result: TestResult | null = null;
          if (running && needsResult(running)) {
            const answer = await gateway
              .getMarketingTestResult({
                workspaceSlug: slug,
                testId: running.test.id,
                roundId: running.round.id,
              })
              .catch(() => null);
            result = answer?.kind === 'ok' ? answer.data : null;
          }
          return [productId, productTestState(history, result, today)] as const;
        })
      );
      return Object.fromEntries(
        entries.filter((entry) => entry !== null)
      ) as Record<string, ProductTestState | null>;
    },
    { keepPreviousData: true }
  );
  const { mutate } = query;
  const refresh = useCallback(async () => {
    await mutate();
  }, [mutate]);
  const states = useMemo(() => query.data ?? {}, [query.data]);
  return {
    states,
    loading: key !== null && query.data === undefined && !query.error,
    refresh,
  };
}

type Member = TeamMember & { user: User };

/**
 * Who the reader is, and the names an Owner can read from the member list.
 * A Member cannot read that list, so their view says "another member".
 */
export function useMemberNames(): {
  selfId: string | null;
  names: ReadonlyMap<string, string>;
} {
  const { current } = useWorkspace();
  const { data: session } = useSession();
  const members = useSWR<ApiResponse<Member[]>>(
    current.role === 'OWNER' ? `/api/teams/${current.slug}/members` : null,
    fetcher
  );
  const names = useMemo(
    () =>
      new Map(
        (members.data?.data ?? []).map((member) => [
          member.userId,
          member.user.name || member.user.email,
        ])
      ),
    [members.data]
  );
  const selfId = session?.user?.id ?? null;
  return useMemo(() => ({ selfId, names }), [selfId, names]);
}
