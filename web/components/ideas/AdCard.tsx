import { useId, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { useMarketingImageSource } from '@/components/workspace/useMarketingImageSource';
import type {
  CheckView,
  IdeaSlot,
  IdeasAd,
  IdeasFinding,
} from '@/lib/ideas/ideas-view';
import { photoGone } from '@/lib/ideas/photos';
import { useFindingTitle } from './FindingList';

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
  /** Opens "Edit this ad" (#201). */
  onEdit?: (item: IdeasAd) => void;
  /** Where the ad's automatic check stands (#197). */
  check?: CheckView;
  /** The findings still to handle, each shown by name (#201). */
  findings?: IdeasFinding[];
  /**
   * Makes a failed image again: the first batch's failed images, through
   * `resume_marketing_first_batch` (#193, #195). Without it, and without
   * `makeAction`, the entry shows but says it is not available.
   */
  onRetry?: () => void;
  /** The retry button's text when it says what trying again costs. */
  retryLabel?: string;
  /**
   * The paid "make this ad" control of an ad with no image: one never made,
   * or one that failed outside the first batch (#200, #201).
   */
  makeAction?: ReactNode;
}

/**
 * One ad on the ideas page (ADR 0018, design §5.3, #196, #201): its image,
 * its way of telling, its status mark, its check, and the keep and edit
 * buttons; or, while there is no image, why not -- being made, failed, or
 * not asked for yet.
 */
export function AdCard({
  slot,
  busy,
  onToggleKept,
  onEdit,
  check,
  findings = [],
  onRetry,
  retryLabel,
  makeAction,
}: AdCardProps) {
  const { t } = useTranslation('ideas');
  const title = useFindingTitle();
  return (
    <article className="flex min-w-0 flex-col gap-2" data-testid="ad-card">
      {slot.kind === 'ad' ? (
        <AdImage
          versionId={slot.ad.asset_version_id}
          alt={slot.headline ?? t('ideas-ad-image')}
        />
      ) : (
        <EmptyFrame
          slot={slot}
          onRetry={onRetry}
          retryLabel={retryLabel}
          makeAction={makeAction}
        />
      )}
      {slot.kind !== 'ad' && slot.kind !== 'making' && !onRetry && makeAction}
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
        {slot.kind === 'ad' && slot.ad.origin !== 'generated' && (
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {t(`ideas-ad-origin-${slot.ad.origin ?? 'generated'}`)}
          </p>
        )}
        {slot.kind === 'ad' && !slot.ad.used_product_photo && (
          <p className="text-sm text-amber-800 dark:text-amber-300">
            {t('ideas-ad-no-photo')}
          </p>
        )}
        {slot.kind === 'ad' &&
          slot.ad.status === 'needs_redo' &&
          photoGone(slot.ad) && (
            <p className="text-sm text-red-800 dark:text-red-300">
              {t('ideas-ad-photo-gone')}
            </p>
          )}
        {slot.kind === 'ad' && check === 'checking' && (
          <p
            className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300"
            role="status"
          >
            <span
              className="loading loading-spinner loading-xs"
              aria-hidden="true"
            />
            {t('ideas-ad-checking')}
          </p>
        )}
        {slot.kind === 'ad' && check === 'unchecked' && (
          <p className="text-sm text-gray-700 dark:text-gray-300">
            {t('ideas-ad-unchecked')}
          </p>
        )}
        {slot.kind === 'ad' && check === 'findings' && findings.length > 0 && (
          <div className="text-sm text-amber-900 dark:text-amber-200">
            <p>{t('ideas-ad-findings', { count: findings.length })}</p>
            <ul className="list-disc pl-5">
              {findings.map((finding) => (
                <li key={finding.check_id}>{title(finding)}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
      {slot.kind === 'ad' && (onToggleKept || onEdit) && (
        <div className="mt-auto flex flex-col gap-2">
          {onToggleKept && (
            <button
              type="button"
              aria-pressed={slot.ad.kept}
              disabled={busy}
              onClick={() => onToggleKept(slot.ad)}
              className={classNames(
                'min-h-11 rounded-lg border px-3 text-sm font-semibold disabled:opacity-60',
                slot.ad.kept
                  ? 'border-gray-900 bg-gray-900 text-white dark:border-gray-100 dark:bg-gray-100 dark:text-gray-900'
                  : 'border-gray-300 bg-white text-gray-900 hover:border-gray-500 dark:border-gray-600 dark:bg-transparent dark:text-gray-100'
              )}
            >
              {slot.ad.kept ? t('ideas-ad-kept') : t('ideas-ad-keep')}
            </button>
          )}
          {onEdit && (
            <button
              type="button"
              onClick={() => onEdit(slot.ad)}
              className="min-h-11 rounded-lg px-3 text-sm font-medium text-gray-900 underline hover:bg-gray-100 dark:text-gray-100 dark:hover:bg-gray-800"
            >
              {t('ideas-ad-edit')}
            </button>
          )}
        </div>
      )}
    </article>
  );
}

/** One ad's image at full width, as the ideas page and the edit screen show it. */
export function AdImage({
  versionId,
  alt,
}: {
  versionId: string;
  alt: string;
}) {
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

/**
 * A small picture of one ad, so a list names an ad by its image, never by an
 * id (#201): the check sheet, a refused download, the versions.
 */
const THUMBNAIL_WIDTH = { xs: 'w-10', sm: 'w-14', md: 'w-20' } as const;

export function AdThumbnail({
  versionId,
  alt,
  size = 'sm',
}: {
  versionId: string;
  alt: string;
  size?: keyof typeof THUMBNAIL_WIDTH;
}) {
  const { imageUrl, imageFailed, onImageError } = useMarketingImageSource(
    versionId,
    true
  );
  return (
    <div
      className={classNames(
        'aspect-[4/5] shrink-0 overflow-hidden rounded-md border border-gray-200 bg-gray-100 dark:border-gray-700 dark:bg-gray-800',
        THUMBNAIL_WIDTH[size]
      )}
    >
      {imageUrl && !imageFailed && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={imageUrl}
          alt={alt}
          className="h-full w-full object-cover"
          onError={onImageError}
        />
      )}
    </div>
  );
}

function EmptyFrame({
  slot,
  onRetry,
  retryLabel,
  makeAction,
}: {
  slot: Exclude<IdeaSlot, { kind: 'ad' }>;
  onRetry?: () => void;
  retryLabel?: string;
  makeAction?: ReactNode;
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
        {makeAction && !onRetry ? null : (
          <>
            <button
              type="button"
              className="btn btn-sm min-h-11"
              disabled={!onRetry}
              aria-describedby={onRetry ? undefined : hint}
              onClick={onRetry}
            >
              {retryLabel ?? t('ideas-ad-retry')}
            </button>
            {!onRetry && (
              <span
                id={hint}
                className="text-center text-xs text-gray-600 dark:text-gray-400"
              >
                {t('ideas-ad-retry-unavailable')}
              </span>
            )}
          </>
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
