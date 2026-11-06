import { useTranslation } from 'next-i18next';
import {
  useAssetImageUrl,
  type AssetThumbnailLink,
} from '@/hooks/assets/useAssetImageUrl';

interface GroupThumbProps {
  versionId: string;
  thumbnail?: AssetThumbnailLink | null;
}

/** A square image in a direction group: the signed thumbnail (Issue #174)
 * when the list carried one, else the original through a download intent. */
export function GroupThumb({ versionId, thumbnail }: GroupThumbProps) {
  const { t } = useTranslation('directions');
  const { blobUrl, isLoading, problem, source } = useAssetImageUrl(versionId, {
    thumbnail: thumbnail ?? null,
  });
  if (blobUrl) {
    return (
      <img
        src={blobUrl}
        alt=""
        className="h-full w-full object-cover"
        loading="lazy"
        data-image-source={source ?? undefined}
      />
    );
  }
  if (isLoading) {
    return (
      <span
        className="loading loading-spinner loading-xs absolute inset-0 m-auto text-base-content/40"
        aria-hidden="true"
      />
    );
  }
  if (problem) {
    return (
      <span className="absolute inset-0 flex items-center justify-center p-1 text-center text-xs text-base-content/60">
        {t('image-failed')}
      </span>
    );
  }
  return null;
}
