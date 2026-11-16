import { useEffect, useId } from 'react';
import { useTranslation } from 'next-i18next';
import { useRouter } from 'next/router';
import useSWR from 'swr';
import { PlusIcon } from '@heroicons/react/24/outline';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useWorkspaceProducts } from '@/hooks/products/useWorkspaceProducts';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { WorkspaceProduct } from '@/lib/products/workspace-products';
import { newProductHref } from '@/lib/workspace-navigator';
import { CompetitorsArea, SCAN_QUERY } from './CompetitorsArea';
import { dataOf } from './shared';

/** The query parameter that names the product whose competitors are shown. */
export const PRODUCT_QUERY = 'product';

/**
 * The market a product's ads go to: the one its newest ad-ideas project was
 * started with. A product made before the redesign has none here, and the
 * scan's market is then the user's to choose.
 */
function useProductMarket(product: WorkspaceProduct | null): string | null {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const projectId = product?.ideas?.projectId ?? null;
  const ideas = useSWR(
    projectId
      ? ['competitors-product-market', current.workspaceId, projectId]
      : null,
    async () =>
      dataOf(
        await gateway.getMarketingIdeas({
          workspaceSlug: current.slug,
          projectId: projectId!,
        })
      )
  );
  return ideas.data?.market?.trim() || null;
}

/**
 * The sidebar's Competitors page (ADR 0018, design §5.6): competitors stay
 * stored per product, so the page asks which product first and shows that
 * product's competitor area -- the same area a marketing test made before
 * the redesign still shows. The chosen product lives in the URL, next to the
 * scan on screen, so a reload keeps both together.
 */
export function ProductCompetitors() {
  const { t } = useTranslation('competitors');
  const { t: tCommon } = useTranslation('common');
  const router = useRouter();
  const { current } = useWorkspace();
  const pickerId = useId();
  const { products, isLoading, problem, refresh } = useWorkspaceProducts();

  const asked =
    typeof router.query[PRODUCT_QUERY] === 'string'
      ? (router.query[PRODUCT_QUERY] as string)
      : null;
  const product =
    products.find((item) => item.productId === asked) ?? products[0] ?? null;
  const market = useProductMarket(product);

  // A missing or unknown product falls back to the first one; the URL then
  // names it, and a scan picked for another product is dropped with it. The
  // area waits for that, so it never opens the other product's scan.
  const shown = product?.productId ?? null;
  const settled = shown !== null && asked === shown;
  useEffect(() => {
    if (isLoading || problem || !shown || asked === shown) return;
    const query = { ...router.query, [PRODUCT_QUERY]: shown };
    delete query[SCAN_QUERY];
    void router.replace({ pathname: router.pathname, query }, undefined, {
      shallow: true,
      scroll: false,
    });
  }, [asked, isLoading, problem, router, shown]);

  function choose(productId: string) {
    const query = { ...router.query, [PRODUCT_QUERY]: productId };
    delete query[SCAN_QUERY];
    void router.replace({ pathname: router.pathname, query }, undefined, {
      shallow: true,
      scroll: false,
    });
  }

  return (
    <div className="space-y-6" data-testid="competitors-page">
      <header className="max-w-3xl space-y-2">
        <h1 className="text-2xl font-bold outline-none">
          {tCommon('competitors-page-title')}
        </h1>
        <p className="text-sm text-gray-600">{t('area-intro')}</p>
      </header>

      {problem ? (
        <div className="space-y-2 text-sm" role="alert">
          <p className="text-gray-700">{t('page-products-problem')}</p>
          <button
            type="button"
            className="link text-sm"
            onClick={() => void refresh()}
          >
            {tCommon('workspace-retry')}
          </button>
        </div>
      ) : isLoading ? (
        <p className="text-sm text-gray-500" role="status">
          {t('page-products-loading')}
        </p>
      ) : !product ? (
        <div
          className="max-w-3xl space-y-3 rounded-xl border border-dashed border-gray-300 p-5"
          data-testid="competitors-page-empty"
        >
          <p className="max-w-prose text-sm text-gray-700">
            {t('page-no-products')}
          </p>
          <GuardedLink
            href={newProductHref(current.slug)}
            className="btn btn-outline btn-sm gap-2"
          >
            <PlusIcon className="h-4 w-4" aria-hidden="true" />
            {tCommon('workspace-nav-new-product')}
          </GuardedLink>
        </div>
      ) : (
        <>
          <div className="flex max-w-3xl flex-col gap-1 text-sm sm:flex-row sm:items-center sm:gap-3">
            <label htmlFor={pickerId} className="font-medium">
              {t('page-product-picker')}
            </label>
            <select
              id={pickerId}
              className="min-h-[44px] w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm sm:w-auto sm:min-w-[16rem]"
              value={product.productId}
              onChange={(event) => choose(event.target.value)}
              data-testid="competitors-product-picker"
            >
              {products.map((item) => (
                <option key={item.productId} value={item.productId}>
                  {item.name ?? tCommon('workspace-unnamed-product')}
                </option>
              ))}
            </select>
          </div>
          {settled && (
            <CompetitorsArea
              key={product.productId}
              productId={product.productId}
              planMarket={market}
              placement="page"
            />
          )}
        </>
      )}
    </div>
  );
}
