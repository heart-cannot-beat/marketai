import { useTranslation } from 'next-i18next';
import { useProductDirectory } from '@/hooks/products/useProductDirectory';

/**
 * A product's display name in place of its `prod_` id (front-flow contract
 * §1 principle 2). While the shared product list loads it renders a quiet
 * placeholder; an unknown or unnamed product reads "未命名产品" -- the id is
 * never a fallback.
 */
export function ProductName({ productId }: { productId: string | null }) {
  const { t } = useTranslation('products');
  const { nameFor } = useProductDirectory();
  const name = nameFor(productId);
  if (name === undefined) {
    return (
      <span
        className="inline-block h-[1em] w-32 animate-pulse rounded bg-base-300 align-middle motion-reduce:animate-none"
        aria-label={t('name-loading')}
        role="status"
      />
    );
  }
  return <>{name ?? t('unnamed')}</>;
}
