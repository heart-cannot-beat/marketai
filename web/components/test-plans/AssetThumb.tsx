import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import {
  useAssetImageUrl,
  type AssetThumbnailLink,
} from '@/hooks/assets/useAssetImageUrl';

interface AssetThumbProps {
  versionId: string;
  thumbnail?: AssetThumbnailLink | null;
  alt: string;
  className?: string;
}

/** One generated image: the signed thumbnail when the list carried one,
 * else the original through a download intent (`useAssetImageUrl`). */
export function AssetThumb({
  versionId,
  thumbnail,
  alt,
  className,
}: AssetThumbProps) {
  const { t } = useTranslation('tests');
  const { blobUrl, isLoading, problem } = useAssetImageUrl(versionId, {
    thumbnail: thumbnail ?? null,
  });
  return (
    <div
      className={classNames('relative overflow-hidden bg-base-200', className)}
    >
      {blobUrl ? (
        <img
          src={blobUrl}
          alt={alt}
          className="h-full w-full object-cover"
          loading="lazy"
        />
      ) : isLoading ? (
        <span
          className="loading loading-spinner loading-xs absolute inset-0 m-auto text-base-content/40"
          aria-hidden="true"
        />
      ) : problem ? (
        <span className="absolute inset-0 flex items-center justify-center p-1 text-center text-xs text-base-content/60">
          {t('direction-image-failed')}
        </span>
      ) : null}
    </div>
  );
}
