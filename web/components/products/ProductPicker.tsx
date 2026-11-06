import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useProductDirectory } from '@/hooks/products/useProductDirectory';
import { ProductThumbnail } from './ProductThumbnail';

interface ProductPickerProps {
  id: string;
  value: string;
  onChange: (productId: string) => void;
  disabled?: boolean;
}

/**
 * Picks a product by its card (main image, name, model) instead of typing a
 * `prod_` id (front-flow contract §1 principle 2). Products that are not
 * published yet are shown but cannot be chosen.
 */
export function ProductPicker({
  id,
  value,
  onChange,
  disabled = false,
}: ProductPickerProps) {
  const { t } = useTranslation('products');
  const { current } = useWorkspace();
  const { products, isLoading, problem, refresh } = useProductDirectory();
  const labelId = `${id}-label`;

  return (
    <fieldset className="form-control" id={id} aria-labelledby={labelId}>
      <legend id={labelId} className="label">
        <span className="label-text">{t('picker-label')}</span>
      </legend>

      {isLoading && products.length === 0 ? (
        <p
          className="flex items-center gap-2 text-sm text-base-content/60"
          role="status"
        >
          <span
            className="loading loading-spinner loading-xs"
            aria-hidden="true"
          />
          {t('picker-loading')}
        </p>
      ) : problem && products.length === 0 ? (
        <div
          className="flex items-center gap-2 text-sm text-error"
          role="alert"
        >
          <span>{t('picker-error')}</span>
          <button type="button" className="btn btn-xs" onClick={refresh}>
            {t('retry')}
          </button>
        </div>
      ) : products.length === 0 ? (
        <p className="text-sm text-base-content/70">
          {t('picker-empty')}{' '}
          <Link
            href={`/w/${current.slug}/products/new`}
            className="link link-primary"
          >
            {t('picker-add')}
          </Link>
        </p>
      ) : (
        <ul
          role="radiogroup"
          aria-labelledby={labelId}
          className="grid max-h-64 gap-2 overflow-y-auto pr-1"
        >
          {products.map((product) => {
            const name = product.name?.trim() || t('unnamed');
            const checked = value === product.product_id;
            const unavailable = !product.ready;
            return (
              <li key={product.product_id}>
                <label
                  className={classNames(
                    'flex items-center gap-3 rounded-lg border p-2',
                    checked ? 'border-primary bg-primary/5' : 'border-base-300',
                    unavailable || disabled
                      ? 'cursor-not-allowed opacity-60'
                      : 'cursor-pointer'
                  )}
                >
                  <input
                    type="radio"
                    name={`${id}-choice`}
                    className="radio radio-sm"
                    value={product.product_id}
                    checked={checked}
                    disabled={unavailable || disabled}
                    onChange={() => onChange(product.product_id)}
                    aria-label={name}
                  />
                  <ProductThumbnail
                    referenceVersionId={product.primary_reference_version_id}
                    name={name}
                    className="h-12 w-12 shrink-0 rounded-md"
                  />
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{name}</span>
                    {product.variant_label && (
                      <span className="block truncate text-sm text-base-content/60">
                        {product.variant_label}
                      </span>
                    )}
                    {unavailable && (
                      <span className="badge badge-warning badge-sm">
                        {t('picker-not-ready')}
                      </span>
                    )}
                  </span>
                </label>
              </li>
            );
          })}
        </ul>
      )}
    </fieldset>
  );
}
