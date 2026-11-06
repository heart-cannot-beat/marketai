import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { PlusIcon } from '@heroicons/react/24/outline';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { FeatureEmptyState } from '@/components/workspace/FeatureEmptyState';
import {
  useProductDirectory,
  type ProductSummary,
} from '@/hooks/products/useProductDirectory';
import {
  stashProductHandoff,
  takeProductHandoff,
} from '@/lib/products/handoff';
import { GenerationDefaultsBanner } from '@/components/test-plans/GenerationDefaultsBanner';
import { ProductThumbnail } from './ProductThumbnail';
import { ProductReferenceSettings } from './ProductReferenceSettings';
import { ProductBrandKitSettings } from './ProductBrandKitSettings';

const KNOWN_RUN_STATUSES = new Set([
  'queued',
  'running',
  'succeeded',
  'partially_succeeded',
  'failed',
  'cancelled',
]);

function ProductHomeCard({
  product,
  slug,
  highlighted,
  onChanged,
}: {
  product: ProductSummary;
  slug: string;
  highlighted: boolean;
  onChanged: () => void;
}) {
  const { t } = useTranslation('products');
  const name = product.name?.trim() || t('unnamed');
  const run = product.latest_run;
  const runStatus = run
    ? KNOWN_RUN_STATUSES.has(run.status)
      ? t(`run-status-${run.status}`)
      : t('run-status-unknown')
    : null;
  const ref = useRef<HTMLLIElement>(null);

  useEffect(() => {
    if (highlighted) ref.current?.scrollIntoView?.({ block: 'nearest' });
  }, [highlighted]);

  return (
    <li
      ref={ref}
      className={classNames(
        'flex flex-col overflow-hidden rounded-box border bg-base-100',
        highlighted
          ? 'border-primary ring-2 ring-primary/30'
          : 'border-base-300'
      )}
      data-testid="product-card"
      aria-current={highlighted ? 'true' : undefined}
    >
      <ProductThumbnail
        referenceVersionId={product.primary_reference_version_id}
        name={name}
        className="aspect-[4/3] w-full border-b border-base-300"
      />
      <div className="flex flex-1 flex-col gap-1 p-4">
        <h2 className="text-base font-semibold leading-snug">{name}</h2>
        {product.variant_label && (
          <p className="text-sm text-base-content/70">
            {product.variant_label}
          </p>
        )}
        <p className="text-sm text-base-content/60">
          {run
            ? t('latest-run', {
                status: runStatus,
                done: run.done,
                total: run.total,
              })
            : t('latest-run-none')}
        </p>
        {!product.ready && (
          <p className="text-sm text-base-content/70">
            <span className="badge badge-warning badge-sm mr-2">
              {t('not-ready')}
            </span>
            {t('not-ready-hint')}
          </p>
        )}
        <ProductReferenceSettings product={product} onChanged={onChanged} />
        <ProductBrandKitSettings product={product} />
        <div className="mt-auto pt-3">
          {product.ready ? (
            <Link
              href={`/w/${slug}/experiments`}
              className="btn btn-primary btn-sm w-full"
              title={t('start-test-hint')}
              onClick={() =>
                stashProductHandoff('marketing-test', slug, product.product_id)
              }
            >
              {t('start-test')}
            </Link>
          ) : (
            <button
              type="button"
              className="btn btn-sm w-full"
              disabled
              title={t('not-ready-hint')}
            >
              {t('start-test')}
            </button>
          )}
        </div>
      </div>
    </li>
  );
}

/**
 * The product home (`/w/{slug}/products`, Issue #162): one card per product
 * with its main image, name, confirmed model and latest Run. "Start test"
 * starts a marketing test (`/w/{slug}/experiments`, J-32 #230) from this
 * product's confirmed card and verified photos through a one-shot handoff,
 * so no id lands in the URL. The learnings card (#166) is hidden since the
 * legacy flow was frozen (#337): it only knows legacy plans' assets.
 */
export function ProductHome() {
  const { t } = useTranslation('products');
  const { current } = useWorkspace();
  const slug = current.slug;
  const { products, isLoading, problem, refresh } = useProductDirectory();
  const [highlightId, setHighlightId] = useState<string | null>(null);

  useEffect(() => {
    setHighlightId(takeProductHandoff('highlight', slug));
    // The list may predate the confirm in the SWR cache.
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  const highlighted = highlightId
    ? products.find((product) => product.product_id === highlightId)
    : undefined;
  const newProductHref = `/w/${slug}/products/new`;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="max-w-prose">
          <h1 className="text-2xl font-bold outline-none">{t('page-title')}</h1>
          <p className="mt-1 text-sm text-base-content/70">{t('page-intro')}</p>
        </div>
        {products.length > 0 && (
          <Link href={newProductHref} className="btn btn-primary btn-sm">
            <PlusIcon className="h-4 w-4" aria-hidden="true" />
            {t('add-product')}
          </Link>
        )}
      </div>

      <GenerationDefaultsBanner />

      {highlighted && (
        <div className="alert alert-success" role="status">
          <span>
            {t('added-banner', {
              name: highlighted.name?.trim() || t('unnamed'),
            })}
          </span>
        </div>
      )}

      {isLoading && products.length === 0 && (
        <div className="flex justify-center py-12" role="status">
          <span className="loading loading-spinner" aria-hidden="true" />
          <span className="sr-only">{t('list-loading')}</span>
        </div>
      )}

      {!isLoading && problem && products.length === 0 && (
        <div className="alert alert-error" role="alert">
          <span>{t('list-error')}</span>
          <button type="button" className="btn btn-sm" onClick={refresh}>
            {t('retry')}
          </button>
        </div>
      )}

      {!isLoading && !problem && products.length === 0 && (
        <FeatureEmptyState
          title={t('empty-title')}
          description={t('empty-description')}
          action={
            <Link href={newProductHref} className="btn btn-primary btn-sm mt-2">
              {t('add-product')}
            </Link>
          }
        />
      )}

      {products.length > 0 && (
        <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {products.map((product) => (
            <ProductHomeCard
              key={product.product_id}
              product={product}
              slug={slug}
              highlighted={product.product_id === highlightId}
              onChanged={() => void refresh()}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
