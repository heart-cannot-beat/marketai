import { useCallback, memo } from 'react';
import { useTranslation } from 'next-i18next';
import {
  useAssetImageUrl,
  type AssetThumbnailLink,
} from '@/hooks/assets/useAssetImageUrl';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';
import { reviewStatus } from '@/lib/assets/status-labels';

export interface AssetCardProps {
  versionId: string;
  displayNumber: number;
  aspectRatio: string;
  provider: string;
  effectiveReview: string | null;
  executionMode: string;
  /** The gallery page's signed 512px thumbnail link for this version
   * (`AssetVersionSummary.thumbnail`); `null`/absent loads the original
   * through the download-intent route, as the Lightbox always does. */
  thumbnail?: AssetThumbnailLink | null;
  isSelected: boolean;
  onSelect: (versionId: string) => void;
  onOpen: (versionId: string) => void;
}

export const AssetCard = memo(function AssetCard({
  versionId,
  displayNumber,
  aspectRatio,
  provider,
  effectiveReview,
  executionMode,
  thumbnail = null,
  isSelected,
  onSelect,
  onOpen,
}: AssetCardProps) {
  const { t } = useTranslation('assets');
  const { t: tc } = useTranslation('common');
  const review = reviewStatus(effectiveReview);
  const reviewLabel = tc(review.labelKey);
  const { blobUrl, isLoading, problem, source } = useAssetImageUrl(versionId, {
    thumbnail,
  });

  const handleOpen = useCallback(() => onOpen(versionId), [versionId, onOpen]);
  const handleSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      e.stopPropagation();
      onSelect(versionId);
    },
    [versionId, onSelect]
  );
  const handleKeyOpen = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onOpen(versionId);
      }
    },
    [versionId, onOpen]
  );

  return (
    <div className="relative group" data-testid="asset-card">
      <input
        type="checkbox"
        checked={isSelected}
        onChange={handleSelect}
        className="checkbox checkbox-sm absolute top-2 left-2 z-10 opacity-0 group-hover:opacity-100 checked:opacity-100"
        aria-label={t('select-asset', { number: displayNumber })}
        data-testid="asset-select"
      />

      {executionMode === 'mock' && (
        <div
          className="absolute top-2 right-2 badge badge-xs badge-error z-10"
          data-testid="asset-mock-badge"
        >
          {t('controlled-mock')}
        </div>
      )}

      <button
        type="button"
        onClick={handleOpen}
        onKeyDown={handleKeyOpen}
        className="w-full rounded-lg overflow-hidden border border-base-300 hover:border-primary focus:outline-none focus:ring-2 focus:ring-primary transition-colors"
        aria-label={t('asset-open-label', {
          number: displayNumber,
          aspect: aspectRatio,
          review: reviewLabel,
        })}
        data-testid="asset-open"
      >
        <div
          className="relative bg-base-200"
          style={{ aspectRatio: aspectRatio.replace(':', '/') }}
        >
          {isLoading && (
            <div className="absolute inset-0 flex items-center justify-center">
              <span className="loading loading-spinner loading-sm" />
            </div>
          )}
          {blobUrl && (
            <img
              src={blobUrl}
              alt=""
              className="w-full h-full object-cover"
              loading="lazy"
              data-image-source={source ?? undefined}
            />
          )}
          {problem && (
            <div className="absolute inset-0 flex items-center justify-center text-error text-xs p-2 text-center">
              {problem.code === 'asset_blob_unavailable'
                ? t('image-unavailable')
                : t('image-load-failed')}
            </div>
          )}
        </div>
      </button>

      <div className="flex items-center gap-1 mt-1 px-1">
        <ExecutionModeTag mode={executionMode} provider={provider} />
        {effectiveReview && effectiveReview !== 'cleared' && (
          <span
            className={`badge badge-xs ${review.badgeClass}`}
            data-testid="asset-review-badge"
            data-review={effectiveReview}
          >
            {reviewLabel}
          </span>
        )}
      </div>
    </div>
  );
});
