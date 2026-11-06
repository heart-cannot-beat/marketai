import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { useReferenceImageUrl } from '@/hooks/library/useReferenceImageUrl';

interface ProductThumbnailProps {
  referenceVersionId: string | null | undefined;
  name: string;
  className?: string;
}

/**
 * A product's main image (its first-priority reference binding), fetched
 * through a short-lived download intent. Without an image the tile shows
 * the name's first character so cards stay recognisable.
 */
export function ProductThumbnail({
  referenceVersionId,
  name,
  className,
}: ProductThumbnailProps) {
  const { t } = useTranslation('products');
  const { blobUrl, isLoading } = useReferenceImageUrl(
    referenceVersionId ?? undefined
  );
  const initial = Array.from(name.trim())[0] ?? '·';

  return (
    <div
      className={classNames(
        'relative flex items-center justify-center overflow-hidden bg-base-200',
        className
      )}
    >
      {blobUrl ? (
        <img
          src={blobUrl}
          alt={t('image-alt', { name })}
          className="h-full w-full object-contain"
        />
      ) : isLoading ? (
        <span
          className="loading loading-spinner loading-sm text-base-content/40"
          aria-hidden="true"
        />
      ) : (
        <span
          className="text-3xl font-semibold text-base-content/30"
          aria-hidden="true"
        >
          {initial}
        </span>
      )}
    </div>
  );
}
