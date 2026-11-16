import { useTranslation } from 'next-i18next';
import { PlusIcon } from '@heroicons/react/24/outline';
import classNames from 'classnames';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { useWorkspaceProducts } from '@/hooks/products/useWorkspaceProducts';
import { newProductHref } from '@/lib/workspace-navigator';
import {
  STEP_COPY,
  productViewHref,
  stepView,
  type NextStepKind,
  type WorkspaceProduct,
} from '@/lib/products/workspace-products';

const BADGE: Record<NextStepKind, string> = {
  attention: 'badge-error',
  report_missing: 'badge-info',
  conclusion_unseen: 'badge-success',
  kept_untested: 'badge-warning',
  drafts_unhandled: 'badge-warning',
  preparing: 'badge-ghost',
  no_ideas: 'badge-ghost',
};

function ProductCard({
  slug,
  product,
}: {
  slug: string;
  product: WorkspaceProduct;
}) {
  const { t } = useTranslation('common');
  const copy = STEP_COPY[product.step];
  const headingId = `overview-product-${product.productId}`;
  return (
    <section
      aria-labelledby={headingId}
      data-testid="overview-product"
      data-step={product.step}
      className="flex flex-col gap-4 rounded-xl border border-base-300 bg-base-100 p-4 sm:flex-row sm:items-center sm:p-5"
    >
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2 id={headingId} className="text-lg font-bold">
            {product.name ?? t('workspace-unnamed-product')}
          </h2>
          <span className={classNames('badge', BADGE[product.step])}>
            {t(copy.status, { count: product.stepCount })}
          </span>
        </div>
        <p className="text-sm text-base-content/80">
          {t(copy.line, { count: product.stepCount })}
        </p>
      </div>
      <GuardedLink
        href={productViewHref(slug, product.productId, stepView(product.step))}
        data-testid="overview-product-action"
        className="btn btn-primary w-full sm:w-auto"
      >
        {t(copy.action)}
      </GuardedLink>
    </section>
  );
}

/**
 * The overview (ADR 0018, design §5.6): the landing page of a Workspace that
 * has products. Each product lists the one thing to do now, with exactly one
 * main button.
 */
export function WorkspaceOverview() {
  const { t } = useTranslation('common');
  const { current } = useWorkspace();
  const { products, isLoading, problem, refresh } = useWorkspaceProducts();
  const slug = current.slug;

  const newProduct = (
    <GuardedLink
      href={newProductHref(slug)}
      data-testid="overview-new-product"
      className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-dashed border-base-300 p-4 hover:bg-base-200 sm:p-5"
    >
      <PlusIcon className="h-5 w-5 shrink-0" aria-hidden="true" />
      <span className="font-semibold">{t('workspace-nav-new-product')}</span>
      <span className="text-sm text-base-content/70">
        {t('overview-new-product-hint')}
      </span>
    </GuardedLink>
  );

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold outline-none">
          {t('overview-title')}
        </h1>
        <p className="mt-1 text-sm text-base-content/70">
          {t('overview-intro')}
        </p>
      </div>

      {problem ? (
        <div className="alert alert-error" role="alert">
          <span>{t('overview-problem')}</span>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => void refresh()}
          >
            {t('workspace-retry')}
          </button>
        </div>
      ) : isLoading ? (
        <div className="flex justify-center py-12" role="status">
          <span className="loading loading-spinner" aria-hidden="true" />
          <span className="sr-only">{t('overview-loading')}</span>
        </div>
      ) : products.length === 0 ? (
        <div
          className="space-y-4 rounded-xl border border-base-300 bg-base-100 p-6"
          data-testid="overview-empty"
        >
          <div>
            <h2 className="text-lg font-bold">{t('overview-empty-title')}</h2>
            <p className="mt-1 text-sm text-base-content/70">
              {t('overview-empty-description')}
            </p>
          </div>
          <GuardedLink
            href={newProductHref(slug)}
            data-testid="overview-new-product"
            className="btn btn-primary w-full sm:w-auto"
          >
            <PlusIcon className="h-4 w-4" aria-hidden="true" />
            {t('workspace-nav-new-product')}
          </GuardedLink>
        </div>
      ) : (
        <div className="space-y-3">
          {products.map((product) => (
            <ProductCard
              key={product.productId}
              slug={slug}
              product={product}
            />
          ))}
          {newProduct}
        </div>
      )}
    </div>
  );
}
