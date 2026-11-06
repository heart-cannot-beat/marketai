import { useTranslation } from 'next-i18next';
import { useReferenceImageUrl } from '@/hooks/library/useReferenceImageUrl';

interface ReferenceThumbnailProps {
  versionId: string;
  alt: string;
  className?: string;
}

/**
 * Controlled-derivative thumbnail for a published reference. Loads through
 * the signed download-intent seam (`useReferenceImageUrl`); while the bytes
 * are in flight, or when the storage origin refuses them, it renders a
 * neutral placeholder rather than a broken image.
 */
export function ReferenceThumbnail({
  versionId,
  alt,
  className = '',
}: ReferenceThumbnailProps) {
  const { t } = useTranslation('library');
  const { blobUrl, isLoading, problem } = useReferenceImageUrl(versionId);

  if (blobUrl) {
    return (
      <img
        src={blobUrl}
        alt={alt}
        className={`h-full w-full object-cover ${className}`}
        data-testid="reference-thumbnail"
      />
    );
  }

  return (
    <div
      className={`flex h-full w-full items-center justify-center bg-gray-100 text-xs text-gray-400 dark:bg-gray-800 dark:text-gray-500 ${className}`}
      aria-busy={isLoading}
      data-testid="reference-thumbnail-placeholder"
    >
      {problem
        ? t('references.thumbnail-unavailable')
        : t('references.thumbnail-loading')}
    </div>
  );
}
