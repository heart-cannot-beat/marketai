import { useCallback, useEffect, useRef } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useAssetImageUrl } from '@/hooks/assets/useAssetImageUrl';
import { useAssetActions } from '@/hooks/assets/useAssetActions';
import { AssetSidebar } from './AssetSidebar';
import type { AssetVersionDetail } from './AssetSidebar';
import { ReviewPanel } from './ReviewPanel';
import { RegenerationPanel } from './RegenerationPanel';

interface LightboxModalProps {
  versionId: string;
  onClose: () => void;
  onNavigate?: (versionId: string) => void;
  onPrev?: () => void;
  onNext?: () => void;
}

export function LightboxModal({
  versionId,
  onClose,
  onNavigate,
  onPrev,
  onNext,
}: LightboxModalProps) {
  const { t } = useTranslation('assets');
  const {
    data: detail,
    isLoading,
    problem,
  } = useDomainQuery(
    'get_generation_asset_version',
    // Mirrors `PreviewPanel`'s/`DraftWorkspace`'s cast for the identical
    // `pathParams`-intersected-with-empty-`queryParams` shape.
    { version_id: versionId } as any
  );
  const { blobUrl, isLoading: imageLoading } = useAssetImageUrl(versionId);
  const actions = useAssetActions(
    (detail as AssetVersionDetail | undefined)?.allowed_actions ?? []
  );
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    // jsdom (used by the component test suite) does not implement
    // `HTMLDialogElement.showModal`, so guard the call rather than relying
    // on optional chaining alone -- `showModal` exists as a property but
    // throws "not a function" when invoked under jsdom.
    if (typeof dialogRef.current?.showModal === 'function') {
      dialogRef.current.showModal();
      dialogRef.current.focus();
    }
  }, []);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
      if (e.key === 'ArrowLeft' && onPrev) {
        e.preventDefault();
        onPrev();
      }
      if (e.key === 'ArrowRight' && onNext) {
        e.preventDefault();
        onNext();
      }
    },
    [onClose, onPrev, onNext]
  );

  const handleNavigateVersion = useCallback(
    (vid: string) => {
      if (onNavigate) onNavigate(vid);
    },
    [onNavigate]
  );

  const typedDetail = detail as AssetVersionDetail | undefined;

  return (
    <dialog
      ref={dialogRef}
      tabIndex={-1}
      className="modal modal-open"
      onKeyDown={handleKeyDown}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === dialogRef.current) onClose();
      }}
    >
      <div className="modal-box max-w-7xl w-full h-[90vh] flex p-0">
        {/* Image area */}
        <div className="flex-1 min-w-0 flex items-center justify-center bg-base-200 relative">
          {imageLoading && (
            <span className="loading loading-spinner loading-lg" />
          )}
          {blobUrl && (
            <img
              src={blobUrl}
              alt=""
              className="max-w-full max-h-full object-contain"
            />
          )}
          {problem && (
            <div className="text-error text-center p-4">
              <p className="font-medium" title={problem.code}>
                {t('image-load-failed')}
              </p>
              <p className="text-sm">{t('image-load-failed-hint')}</p>
            </div>
          )}

          {/* Navigation arrows */}
          {onPrev && (
            <button
              type="button"
              className="btn btn-circle btn-ghost absolute left-2 top-1/2 -translate-y-1/2"
              onClick={onPrev}
              aria-label={t('previous-asset')}
            >
              &larr;
            </button>
          )}
          {onNext && (
            <button
              type="button"
              className="btn btn-circle btn-ghost absolute right-2 top-1/2 -translate-y-1/2"
              onClick={onNext}
              aria-label={t('next-asset')}
            >
              &rarr;
            </button>
          )}
        </div>

        {/* Sidebar */}
        <div className="w-80 shrink-0 border-l border-base-300 flex flex-col">
          <div className="flex items-center justify-between px-4 py-2 border-b border-base-300">
            <span
              className="font-medium text-sm"
              data-testid="asset-detail-title"
            >
              {t('asset-detail')}
            </span>
            <button
              type="button"
              className="btn btn-sm btn-ghost btn-circle"
              onClick={onClose}
              aria-label={t('close-lightbox')}
            >
              {t('close-symbol')}
            </button>
          </div>

          {isLoading && (
            <div className="flex-1 flex items-center justify-center">
              <span className="loading loading-spinner" />
            </div>
          )}

          {typedDetail && (
            <>
              <div className="flex-1 min-h-0 overflow-y-auto">
                <AssetSidebar
                  detail={typedDetail}
                  onNavigateVersion={handleNavigateVersion}
                />
              </div>
              <div className="border-t border-base-300 p-3 flex flex-col gap-2">
                {actions.canReview && (
                  <ReviewPanel
                    versionId={versionId}
                    reviewHeadId={typedDetail.review_head_id ?? null}
                    effectiveReview={typedDetail.effective_review_value ?? null}
                  />
                )}
                {actions.canRegenerate && (
                  <RegenerationPanel
                    versionId={versionId}
                    latestQcAttemptId={typedDetail.latest_qc_attempt_id ?? null}
                  />
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </dialog>
  );
}
