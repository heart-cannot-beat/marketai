import type { ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { useWorkspaceProducts } from '@/hooks/products/useWorkspaceProducts';
import {
  PRODUCT_VIEWS,
  productViewHref,
  type ProductView,
  type WorkspaceProduct,
} from '@/lib/products/workspace-products';

const VIEW_LABELS: Record<ProductView, string> = {
  ideas: 'product-view-ideas',
  tests: 'product-view-tests',
  results: 'product-view-results',
  profile: 'product-view-profile',
};

interface ProductFrameProps {
  productId: string;
  view: ProductView;
  /** The view's body, given the product once the list has it. */
  children: (product: WorkspaceProduct) => ReactNode;
}

/**
 * One product's page (ADR 0018, design §5.6): its name, then its four views
 * -- Ad ideas, Tests, Results, Product profile -- at the top. On a phone the
 * views stay on one line and scroll sideways (design §5.8).
 */
export function ProductFrame({ productId, view, children }: ProductFrameProps) {
  const { t } = useTranslation('common');
  const { current } = useWorkspace();
  const { find, isLoading, problem, refresh } = useWorkspaceProducts();
  const product = find(productId);
  const overviewHref = `/w/${current.slug}/overview`;

  if (problem) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold outline-none">
          {t('product-frame-problem')}
        </h1>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => void refresh()}
        >
          {t('workspace-retry')}
        </button>
      </div>
    );
  }

  if (!product) {
    if (isLoading) {
      return (
        <div className="flex items-center gap-3 py-12" role="status">
          <span className="loading loading-spinner" aria-hidden="true" />
          <h1 className="text-base font-normal outline-none">
            {t('product-frame-loading')}
          </h1>
        </div>
      );
    }
    return (
      <div className="space-y-3" data-testid="product-frame-not-found">
        <h1 className="text-2xl font-bold outline-none">
          {t('product-frame-not-found')}
        </h1>
        <p className="text-sm text-base-content/70">
          {t('product-frame-not-found-description')}
        </p>
        <GuardedLink href={overviewHref} className="btn btn-sm">
          {t('product-frame-back-to-overview')}
        </GuardedLink>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <header className="space-y-3">
        <h1 className="text-2xl font-bold outline-none">
          {product.name ?? t('workspace-unnamed-product')}
        </h1>
        <nav
          aria-label={t('product-frame-views')}
          className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0"
        >
          <ul
            role="list"
            className="flex min-w-max gap-1 border-b border-gray-200 dark:border-gray-700"
          >
            {PRODUCT_VIEWS.map((item) => {
              const active = item === view;
              return (
                <li key={item}>
                  <GuardedLink
                    href={productViewHref(current.slug, productId, item)}
                    aria-current={active ? 'page' : undefined}
                    data-testid={`product-view-${item}`}
                    className={classNames(
                      '-mb-px inline-flex min-h-11 items-center whitespace-nowrap border-b-2 px-3 text-sm',
                      active
                        ? 'border-gray-900 font-semibold text-gray-900 dark:border-gray-100 dark:text-gray-100'
                        : 'border-transparent text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
                    )}
                  >
                    {t(VIEW_LABELS[item])}
                  </GuardedLink>
                </li>
              );
            })}
          </ul>
        </nav>
      </header>
      {children(product)}
    </div>
  );
}
