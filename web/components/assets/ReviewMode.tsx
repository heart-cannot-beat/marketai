import { useCallback, useEffect, useMemo, useRef } from 'react';
import { useTranslation } from 'next-i18next';
import { useAssets } from './AssetsProvider';
import {
  reviewQueueStorageKey,
  useReviewMode,
  type ReviewableVersion,
} from '@/hooks/assets/useReviewMode';
import { useAssetGallery } from '@/hooks/assets/useAssetGallery';
import { useAssetImageUrl } from '@/hooks/assets/useAssetImageUrl';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { isActionAllowed } from '@/hooks/assets/useAssetActions';
import { ReviewProgress } from './ReviewProgress';
import { ReviewSummary } from './ReviewSummary';

interface ReviewModeProps {
  onExit: () => void;
}

export function ReviewMode({ onExit }: ReviewModeProps) {
  const { t } = useTranslation('assets');
  const { urlState } = useAssets();
  const { current } = useWorkspace();
  const gallery = useAssetGallery(urlState.filters);
  const fallbackQueueIds = useMemo(
    () => gallery.items.map((item) => item.id),
    [gallery.items]
  );
  const storageKey = useMemo(
    () => reviewQueueStorageKey(current.slug, urlState.filters),
    [current.slug, urlState.filters]
  );
  const mode = useReviewMode(fallbackQueueIds, {
    storageKey,
    isQueueLoading: gallery.isLoading,
  });
  const detailQuery = useDomainQuery(
    'get_generation_asset_version',
    mode.currentVersionId
      ? ({ version_id: mode.currentVersionId } as any)
      : undefined,
    { enabled: !!mode.currentVersionId }
  );
  const currentVersion = detailQuery.data as ReviewableVersion | undefined;
  const canReview = isActionAllowed(currentVersion?.allowed_actions, 'review');
  const { blobUrl, isLoading: imageLoading } = useAssetImageUrl(
    mode.currentVersionId
  );
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    containerRef.current?.focus();
  }, [mode.isInitializing, mode.isComplete, mode.currentVersionId]);

  const handleExit = useCallback(() => {
    mode.clearQueue();
    onExit();
  }, [mode, onExit]);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      if (event.nativeEvent.isComposing || event.repeat || mode.isSubmitting) {
        return;
      }
      const target = event.target as HTMLElement;
      if (
        target?.tagName === 'INPUT' ||
        target?.tagName === 'TEXTAREA' ||
        target?.isContentEditable
      ) {
        return;
      }

      if (mode.isComplete) {
        if (event.key === 'Escape') handleExit();
        return;
      }

      switch (event.key) {
        case 'a':
        case 'A':
          event.preventDefault();
          if (canReview && currentVersion) {
            void mode.submitReview('approved', currentVersion);
          }
          break;
        case 'c':
        case 'C':
          event.preventDefault();
          if (canReview && currentVersion) {
            void mode.submitReview('needs_changes', currentVersion);
          }
          break;
        case 'x':
        case 'X':
          event.preventDefault();
          if (canReview && currentVersion) {
            void mode.submitReview('rejected', currentVersion);
          }
          break;
        case ' ':
          event.preventDefault();
          mode.skip();
          break;
        case 'ArrowLeft':
          event.preventDefault();
          mode.goBack();
          break;
        case 'Escape':
          event.preventDefault();
          handleExit();
          break;
      }
    },
    [canReview, currentVersion, handleExit, mode]
  );

  if (mode.isComplete) {
    return (
      <div className="fixed inset-0 z-50 bg-base-100">
        <ReviewSummary stats={mode.stats} onClose={handleExit} />
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="fixed inset-0 z-50 bg-base-100 flex flex-col outline-none"
      tabIndex={-1}
      data-testid="review-mode"
      onKeyDown={handleKeyDown}
    >
      {mode.isInitializing ? (
        <div className="flex flex-1 items-center justify-center">
          <span className="loading loading-spinner loading-lg" />
        </div>
      ) : (
        <>
          <ReviewProgress stats={mode.stats} currentIndex={mode.currentIndex} />

          <div className="flex-1 min-h-0 flex items-center justify-center bg-base-200 p-4">
            {(imageLoading || detailQuery.isLoading) && (
              <span className="loading loading-spinner loading-lg" />
            )}
            {blobUrl && (
              <img
                src={blobUrl}
                alt=""
                className="max-w-full max-h-full object-contain"
              />
            )}
          </div>

          <div
            className="px-4 py-3 border-t border-base-300 flex items-center justify-center gap-4"
            data-testid="review-shortcuts"
          >
            <span className={canReview ? '' : 'opacity-40'}>
              <kbd className="kbd kbd-sm">{t('review-key-approve')}</kbd>{' '}
              {t('review-shortcut-approve')}
            </span>
            <span className={canReview ? '' : 'opacity-40'}>
              <kbd className="kbd kbd-sm">{t('review-key-changes')}</kbd>{' '}
              {t('review-shortcut-changes')}
            </span>
            <span className={canReview ? '' : 'opacity-40'}>
              <kbd className="kbd kbd-sm">{t('review-key-reject')}</kbd>{' '}
              {t('review-shortcut-reject')}
            </span>
            <span>
              <kbd className="kbd kbd-sm">{t('review-key-skip')}</kbd>{' '}
              {t('review-shortcut-skip')}
            </span>
            <span>
              <kbd className="kbd kbd-sm">{t('review-key-back')}</kbd>{' '}
              {t('review-shortcut-back')}
            </span>
            <span>
              <kbd className="kbd kbd-sm">{t('review-key-exit')}</kbd>{' '}
              {t('review-shortcut-exit')}
            </span>
          </div>
        </>
      )}
    </div>
  );
}
