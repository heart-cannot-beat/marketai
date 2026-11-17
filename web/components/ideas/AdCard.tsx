import { useId } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { useMarketingImageSource } from '@/components/workspace/useMarketingImageSource';
import type { IdeaSlot, IdeasAd } from '@/lib/ideas/ideas-view';

const STATUS_STYLE: Record<IdeasAd['status'], string> = {
  draft: 'bg-amber-100 text-amber-900',
  needs_redo: 'bg-red-100 text-red-900',
  exportable: 'bg-emerald-100 text-emerald-900',
};

const FRAME =
  'relative flex aspect-[4/5] w-full items-center justify-center overflow-hidden rounded-lg border border-gray-200 bg-gray-100 dark:border-gray-700 dark:bg-gray-800';

interface AdCardProps {
  slot: IdeaSlot;
  /** A keep request for this ad is in flight. */
  busy?: boolean;
  onToggleKept?: (item: IdeasAd) => void;
  /**
   * Makes a failed image again. The first batch's image settings come with
   * #193 (`resume_marketing_first_batch`); until a caller wires this, the
   * entry shows but says it is not available yet.
   */
  onRetry?: () => void;
}

/**
 * One ad on the ideas page (ADR 0018, design §5.3, #196): its image, its way
 * of telling, its status mark and the keep button; or, while there is no
 * image, why not -- being made, failed, or not asked for yet.
 */
export function AdCard({ slot, busy, onToggleKept, onRetry }: AdCardProps) {
  const { t } = useTranslation('ideas');
  return (
    <article className="flex min-w-0 flex-col gap-2" data-testid="ad-card">
      {slot.kind === 'ad' ? (
        <AdImage
          versionId={slot.ad.asset_version_id}
          alt={slot.headline ?? t('ideas-ad-image')}
        />
      ) : (
        <EmptyFrame slot={slot} onRetry={onRetry} />
      )}
      <div className="min-w-0 space-y-1">
        <p className="text-sm font-semibold">
          {slot.kind === 'ad' && (
            <span
              className={classNames(
                'mr-2 inline-block rounded-full px-2 py-px align-middle text-xs font-semibold',
                STATUS_STYLE[slot.ad.status]
              )}
            >
              {t(`ideas-ad-status-${slot.ad.status}`)}
            </span>
          )}
          {slot.telling
            ? t('ideas-ad-telling', { name: slot.telling.name })
            : t('ideas-ad-telling-unknown')}
        </p>
        {slot.telling && (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {slot.telling.description}
          </p>
        )}
        {slot.kind === 'ad' && !slot.ad.used_product_photo && (
          <p className="text-sm text-amber-800 dark:text-amber-300">
            {t('ideas-ad-no-photo')}
          </p>
        )}
      </div>
      {slot.kind === 'ad' && onToggleKept && (
        <button
          type="button"
          aria-pressed={slot.ad.kept}
          disabled={busy}
          onClick={() => onToggleKept(slot.ad)}
          className={classNames(
            'mt-auto min-h-11 rounded-lg border px-3 text-sm font-semibold disabled:opacity-60',
            slot.ad.kept
              ? 'border-gray-900 bg-gray-900 text-white dark:border-gray-100 dark:bg-gray-100 dark:text-gray-900'
              : 'border-gray-300 bg-white text-gray-900 hover:border-gray-500 dark:border-gray-600 dark:bg-transparent dark:text-gray-100'
          )}
        >
          {slot.ad.kept ? t('ideas-ad-kept') : t('ideas-ad-keep')}
        </button>
      )}
    </article>
  );
}

function AdImage({ versionId, alt }: { versionId: string; alt: string }) {
  const { t } = useTranslation('ideas');
  const { imageUrl, imageFailed, onImageError } = useMarketingImageSource(
    versionId,
    true
  );
  return (
    <div className={FRAME}>
      {imageUrl && !imageFailed ? (
        // Private blob URL from a download grant; next/image cannot load it.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={imageUrl}
          alt={alt}
          className="h-full w-full object-cover"
          onError={onImageError}
        />
      ) : (
        <span className="p-3 text-center text-xs text-gray-500 dark:text-gray-400">
          {imageFailed
            ? t('ideas-ad-image-unavailable')
            : t('ideas-ad-image-loading')}
        </span>
      )}
    </div>
  );
}

function EmptyFrame({
  slot,
  onRetry,
}: {
  slot: Exclude<IdeaSlot, { kind: 'ad' }>;
  onRetry?: () => void;
}) {
  const { t } = useTranslation('ideas');
  const hint = useId();
  if (slot.kind === 'making')
    return (
      <div className={classNames(FRAME, 'flex-col gap-2')} role="status">
        <span className="loading loading-spinner" aria-hidden="true" />
        <span className="px-3 text-center text-xs text-gray-600 dark:text-gray-300">
          {t('ideas-ad-making')}
        </span>
      </div>
    );
  if (slot.kind === 'failed')
    return (
      <div
        className={classNames(
          FRAME,
          'flex-col gap-2 border-dashed border-red-300 bg-red-50 px-3 dark:border-red-800 dark:bg-red-950'
        )}
      >
        <span className="text-center text-sm text-red-900 dark:text-red-200">
          {t('ideas-ad-failed')}
        </span>
        <button
          type="button"
          className="btn btn-sm min-h-11"
          disabled={!onRetry}
          aria-describedby={onRetry ? undefined : hint}
          onClick={onRetry}
        >
          {t('ideas-ad-retry')}
        </button>
        {!onRetry && (
          <span
            id={hint}
            className="text-center text-xs text-gray-600 dark:text-gray-400"
          >
            {t('ideas-ad-retry-unavailable')}
          </span>
        )}
      </div>
    );
  return (
    <div className={classNames(FRAME, 'border-dashed')}>
      <span className="px-3 text-center text-xs text-gray-500 dark:text-gray-400">
        {t('ideas-ad-not-made')}
      </span>
    </div>
  );
}
