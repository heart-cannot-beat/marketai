import { useId } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import type { CommandOutcome } from '@/hooks/ideas/useIdeasCommands';
import { useIntakeImageUrl } from '@/hooks/products/useIntakeImageUrl';
import { errorMessage } from '@/lib/i18n/domain-errors';
import type { IdeasPhoto, PhotoStatus } from '@/lib/ideas/photos';

/**
 * Pieces the check before downloading and the product profile share for one
 * product photo (#229): its picture, what it is, its checking, and the
 * question asked before rejecting it. Both screens say the same about the
 * same photo.
 */

const THUMBNAIL = {
  sm: 'w-20',
  lg: 'w-full',
} as const;

/** The photo itself: the reading's released copy, never a link to keep. */
export function PhotoThumbnail({
  photo,
  alt,
  size = 'sm',
}: {
  photo: IdeasPhoto;
  alt: string;
  size?: keyof typeof THUMBNAIL;
}) {
  const { t } = useTranslation('ideas');
  const { blobUrl, failed } = useIntakeImageUrl(
    photo.intake_id,
    photo.image_id
  );
  return (
    <div
      className={classNames(
        'flex aspect-square shrink-0 items-center justify-center overflow-hidden rounded-lg border border-gray-200 bg-gray-100 dark:border-gray-700 dark:bg-gray-800',
        THUMBNAIL[size]
      )}
    >
      {blobUrl ? (
        // A private blob URL; next/image cannot load it.
        // eslint-disable-next-line @next/next/no-img-element
        <img src={blobUrl} alt={alt} className="h-full w-full object-cover" />
      ) : (
        <span className="p-2 text-center text-xs text-gray-500 dark:text-gray-400">
          {failed ? t('photo-image-unavailable') : t('photo-image-loading')}
        </span>
      )}
    </div>
  );
}

/** "Main photo · from the product page": what the reading took it for, and where it came from. */
export function usePhotoLabel() {
  const { t } = useTranslation('ideas');
  return (photo: IdeasPhoto) => ({
    role: t(`photo-role-${photo.role}`),
    source: t(`photo-source-${photo.source}`),
  });
}

const STATUS_STYLE: Record<PhotoStatus, string> = {
  unverified: 'bg-amber-100 text-amber-900',
  verified: 'bg-emerald-100 text-emerald-900',
  rejected: 'bg-red-100 text-red-900',
  unavailable: 'bg-red-100 text-red-900',
};

export function PhotoStatusBadge({ status }: { status: PhotoStatus }) {
  const { t } = useTranslation('ideas');
  return (
    <span
      className={classNames(
        'inline-block rounded-full px-2 py-px text-xs font-semibold',
        STATUS_STYLE[status]
      )}
    >
      {t(`photo-status-${status}`)}
    </span>
  );
}

/** What checking a photo declares (#199), in the user's words. */
export function useCheckDeclaration() {
  const { t } = useTranslation('ideas');
  return (productName: string | null | undefined) =>
    productName?.trim()
      ? t('photo-declaration', { product: productName.trim() })
      : t('photo-declaration-unnamed');
}

export interface PhotoFailure {
  message: string;
  /** Someone changed these ideas first: refreshing comes before trying again. */
  stale: boolean;
}

/** Codes a photo decision explains in its own words. */
const OWN: Record<string, string> = {
  stale_marketing_revision: 'photo-stale',
  photo_not_found: 'photo-not-found',
  verified_product_unpublishable: 'photo-unpublishable',
};

/** The sentence for a photo decision that did not go through. */
export function usePhotoFailure() {
  const { t } = useTranslation('ideas');
  return (outcome: CommandOutcome<unknown>): PhotoFailure | null => {
    if (outcome.kind === 'done') return null;
    if (outcome.kind === 'uncertain')
      return { message: t('photo-uncertain'), stale: false };
    const own = OWN[outcome.problem.code];
    if (own) return { message: t(own), stale: true };
    return { message: errorMessage(t, outcome.problem), stale: false };
  };
}

export function PhotoFailureNote({
  failure,
  onRefresh,
}: {
  failure: PhotoFailure | null;
  onRefresh: () => void;
}) {
  const { t } = useTranslation('ideas');
  if (!failure) return null;
  return (
    <div
      role="alert"
      className="space-y-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-200"
    >
      <p>{failure.message}</p>
      {failure.stale && (
        <button
          type="button"
          className="btn btn-sm min-h-11"
          onClick={onRefresh}
        >
          {t('photo-refresh')}
        </button>
      )}
    </div>
  );
}

/**
 * Asked before a photo is rejected (#229): how many ads were made with it,
 * that they will need redoing, and that nothing new is made with it.
 * `inDownload` counts the ones the check before downloading was about to
 * download.
 */
export function RejectPhotoQuestion({
  used,
  inDownload,
  busy,
  failure,
  onRefresh,
  onReject,
  onCancel,
}: {
  used: number;
  inDownload?: number;
  busy: boolean;
  failure: PhotoFailure | null;
  onRefresh: () => void;
  onReject: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation('ideas');
  const title = useId();
  return (
    <div
      role="group"
      aria-labelledby={title}
      className="w-full space-y-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
    >
      <p id={title} className="font-semibold">
        {t('photo-reject-title')}
      </p>
      <p>
        {used > 0
          ? t('photo-reject-ads', { count: used })
          : t('photo-reject-no-ads')}
      </p>
      {inDownload !== undefined && inDownload > 0 && (
        <p>{t('photo-reject-in-download', { count: inDownload })}</p>
      )}
      <p>{t('photo-reject-after')}</p>
      <PhotoFailureNote failure={failure} onRefresh={onRefresh} />
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn btn-error btn-sm min-h-11"
          disabled={busy}
          onClick={onReject}
        >
          {busy ? t('photo-saving') : t('photo-reject-confirm')}
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-sm min-h-11"
          disabled={busy}
          onClick={onCancel}
        >
          {t('photo-cancel')}
        </button>
      </div>
    </div>
  );
}
