import { useEffect, useRef } from 'react';
import { useTranslation } from 'next-i18next';
import { AssetCard } from './AssetCard';
import type { AssetVersionSummary } from '@/hooks/assets/useAssetGallery';
import { useAssets } from './AssetsProvider';

export interface AssetGridProps {
  items: AssetVersionSummary[];
  hasMore: boolean;
  isLoading: boolean;
  isLoadingMore: boolean;
  onLoadMore: () => void;
  onOpenVersion: (versionId: string) => void;
}

export function AssetGrid({
  items,
  hasMore,
  isLoading,
  isLoadingMore,
  onLoadMore,
  onOpenVersion,
}: AssetGridProps) {
  const { t } = useTranslation('assets');
  const { selection } = useAssets();
  const sentinelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !hasMore) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          onLoadMore();
        }
      },
      { rootMargin: '200px' }
    );

    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, onLoadMore]);

  if (isLoading && items.length === 0) {
    return (
      <div className="flex items-center justify-center py-16">
        <span className="loading loading-spinner loading-lg" />
      </div>
    );
  }

  if (!isLoading && items.length === 0) {
    return (
      <div className="text-center py-16 text-base-content/60">
        <p className="text-lg font-medium">{t('gallery-empty-title')}</p>
        <p className="text-sm mt-1">{t('gallery-empty-description')}</p>
      </div>
    );
  }

  return (
    <>
      <div
        className="grid gap-3 p-4"
        style={{
          gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))',
        }}
      >
        {items.map((item, index) => (
          <AssetCard
            key={item.id}
            versionId={item.id}
            displayNumber={index + 1}
            aspectRatio={item.aspect_ratio}
            provider={item.provider}
            effectiveReview={item.effective_review_value}
            executionMode={item.execution_mode}
            thumbnail={item.thumbnail ?? null}
            isSelected={selection.isSelected(item.id)}
            onSelect={selection.toggle}
            onOpen={onOpenVersion}
          />
        ))}
      </div>

      {hasMore && (
        <div ref={sentinelRef} className="flex justify-center py-4">
          {isLoadingMore && (
            <span className="loading loading-spinner loading-sm" />
          )}
        </div>
      )}
    </>
  );
}
