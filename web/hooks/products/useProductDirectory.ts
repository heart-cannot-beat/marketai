import { useCallback, useMemo } from 'react';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

export type ProductSummary = components['schemas']['ProductSummary'];

export interface UseProductDirectoryResult {
  products: ProductSummary[];
  isLoading: boolean;
  problem: ProblemDetails | undefined;
  /**
   * The display name for a product id. `undefined` while the list is still
   * loading; `null` once loaded when the product is unknown or unnamed --
   * callers render "未命名产品" then, never the id.
   */
  nameFor: (productId: string | null | undefined) => string | null | undefined;
  find: (productId: string | null | undefined) => ProductSummary | undefined;
  refresh: () => Promise<void>;
}

/**
 * The Workspace's products (`list_products`), shared through the SWR cache
 * by every page that needs a product's name instead of its `prod_` id: one
 * batched read, not one request per row.
 */
export function useProductDirectory(options?: {
  enabled?: boolean;
}): UseProductDirectoryResult {
  const query = useDomainQuery('list_products', undefined, {
    enabled: options?.enabled ?? true,
  });
  const products = useMemo<ProductSummary[]>(
    () => (query.data?.items ?? []) as ProductSummary[],
    [query.data]
  );
  const byId = useMemo(() => {
    const map = new Map<string, ProductSummary>();
    for (const product of products) map.set(product.product_id, product);
    return map;
  }, [products]);

  const loaded = query.data !== undefined;
  const find = useCallback(
    (productId: string | null | undefined) =>
      productId ? byId.get(productId) : undefined,
    [byId]
  );
  const nameFor = useCallback(
    (productId: string | null | undefined) => {
      if (!loaded) return query.problem ? null : undefined;
      const name = productId ? byId.get(productId)?.name?.trim() : '';
      return name ? name : null;
    },
    [byId, loaded, query.problem]
  );

  return {
    products,
    isLoading: query.isLoading,
    problem: query.problem,
    nameFor,
    find,
    refresh: query.refresh,
  };
}
