import { useCallback, useMemo } from 'react';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useProductTestStates } from '@/hooks/tests/useTestHistory';
import env from '@/lib/env';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import {
  mergeWorkspaceProducts,
  type WorkspaceProduct,
} from '@/lib/products/workspace-products';

type ProductSummary = components['schemas']['ProductSummary'];
type IdeasProjectSummary = components['schemas']['IdeasProjectSummary'];

/** `list_marketing_ideas` allows at most 100 a page; the sidebar reads one. */
const IDEAS_PAGE = { limit: '100' };

export interface UseWorkspaceProductsResult {
  products: WorkspaceProduct[];
  /** True until both lists have answered once. */
  isLoading: boolean;
  /** True until the products' tests have answered once (#258): until then a
   * step may still change to a test step, so the overview waits for it and
   * the sidebar leaves the status line empty. */
  testsLoading: boolean;
  /** Either list's problem. The merged list is not trustworthy then. */
  problem: ProblemDetails | undefined;
  find: (productId: string) => WorkspaceProduct | undefined;
  refresh: () => Promise<void>;
}

/**
 * The Workspace's products as the ads-first navigation shows them (ADR 0018):
 * `list_products` and `list_marketing_ideas`, read once through the SWR
 * cache and shared by the sidebar, the phone menu, the overview and the
 * product views; with the switch on, each product's test in progress too
 * (#258).
 */
export function useWorkspaceProducts(): UseWorkspaceProductsResult {
  const productsQuery = useDomainQuery('list_products');
  const ideasQuery = useDomainQuery('list_marketing_ideas', IDEAS_PAGE);
  const ideasItems = ideasQuery.data?.items as
    | IdeasProjectSummary[]
    | undefined;

  // Only a product with ad ideas can have a test: a test takes kept ads
  // (plan E1). With the switch off nothing more is read.
  const testedIds = useMemo(
    () =>
      Array.from(
        new Set((ideasItems ?? []).map((project) => project.product_id))
      ).sort(),
    [ideasItems]
  );
  const tests = useProductTestStates(testedIds, env.adsFirstEnabled);

  const products = useMemo(
    () =>
      mergeWorkspaceProducts(
        (productsQuery.data?.items ?? []) as ProductSummary[],
        ideasItems ?? [],
        tests.states
      ),
    [productsQuery.data, ideasItems, tests.states]
  );

  const find = useCallback(
    (productId: string) =>
      products.find((product) => product.productId === productId),
    [products]
  );

  const refreshProducts = productsQuery.refresh;
  const refreshIdeas = ideasQuery.refresh;
  const refreshTests = tests.refresh;
  const refresh = useCallback(async () => {
    await Promise.all([refreshProducts(), refreshIdeas(), refreshTests()]);
  }, [refreshProducts, refreshIdeas, refreshTests]);

  const problem = productsQuery.problem ?? ideasQuery.problem;
  return {
    products,
    isLoading:
      !problem &&
      (productsQuery.data === undefined || ideasQuery.data === undefined),
    testsLoading: !problem && tests.loading,
    problem,
    find,
    refresh,
  };
}
