import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { PhotoIcon } from '@heroicons/react/24/outline';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import type { CompetitorAd } from './shared';

/** The Ad Library page of one ad; `library_url` is the provider's `url`. */
export function libraryUrlOf(ad: {
  library_url?: string | null;
  ad_archive_id?: string | null;
}) {
  if (ad.library_url) return ad.library_url;
  return ad.ad_archive_id
    ? `https://www.facebook.com/ads/library/?id=${encodeURIComponent(ad.ad_archive_id)}`
    : null;
}

/** The thumbnail is a video's poster frame when no card has an image. */
export function isVideo(ad: CompetitorAd) {
  const cards = ad.cards ?? [];
  return (
    ad.display_format === 'VIDEO' ||
    (!cards.some((card) => card.image) &&
      cards.some((card) => card.video_poster))
  );
}

/**
 * An observation's 512px derivative (spec §9), fetched from its short-lived
 * signed link into a blob URL that stays inside the page. It is shown, never
 * offered: no download, no drag into reference assets, no export (ADR 0014).
 * Under it, always, where it came from and the link to the original ad; when
 * the archive has no image, a placeholder keeps the same caption.
 */
export function AdThumbnail({ ad }: { ad: CompetitorAd }) {
  const { t } = useTranslation('competitors');
  const blobClient = useBlobTransferClient();
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  // A re-polled page signs the same derivative again; only the ad and
  // whether it has a link at all decide a reload.
  const linkRef = useRef(ad.thumbnail);
  linkRef.current = ad.thumbnail;
  const hasLink = Boolean(ad.thumbnail?.url);

  useEffect(() => {
    const link = linkRef.current;
    setSrc(null);
    setFailed(false);
    if (!link?.url) return;
    const controller = new AbortController();
    let url: string | null = null;
    blobClient
      .downloadBlob(
        {
          url: link.url,
          expires_at: link.expires_at,
          content_disposition: 'inline',
        },
        { signal: controller.signal }
      )
      .then((blob) => {
        if (controller.signal.aborted) return;
        url = URL.createObjectURL(blob);
        setSrc(url);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    return () => {
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [ad.ad_id, hasLink, blobClient]);

  const library = libraryUrlOf(ad);
  const video = isVideo(ad);
  const placeholder = !hasLink || failed;

  return (
    <figure className="space-y-1">
      <div
        className="relative aspect-square w-full overflow-hidden rounded-lg bg-gray-100"
        onContextMenu={(event) => event.preventDefault()}
        onDragStart={(event) => event.preventDefault()}
      >
        {src && !placeholder && (
          // eslint-disable-next-line @next/next/no-img-element -- a signed blob, not an optimizable asset
          <img
            src={src}
            alt={t('card-thumbnail-alt', {
              advertiser: ad.advertiser_name ?? ad.ad_archive_id,
            })}
            className="h-full w-full select-none object-contain"
            draggable={false}
            data-testid="competitor-ad-thumbnail"
          />
        )}
        {placeholder && (
          <div
            className="flex h-full w-full flex-col items-center justify-center gap-2 p-4 text-center text-sm text-gray-500"
            data-testid="competitor-ad-thumbnail-placeholder"
            data-media-status={ad.media_status}
          >
            <PhotoIcon className="h-8 w-8" aria-hidden="true" />
            <span>
              {failed
                ? t('card-media-failed')
                : t(`card-media-${ad.media_status}`)}
            </span>
          </div>
        )}
        {!src && !placeholder && (
          <div
            className="h-full w-full animate-pulse bg-gray-200"
            role="status"
            aria-label={t('card-media-loading')}
          />
        )}
        {video && (
          <span
            className="absolute left-2 top-2 rounded bg-gray-900/80 px-1.5 py-0.5 text-xs font-medium text-white"
            data-testid="competitor-ad-video-badge"
          >
            {t('card-video')}
          </span>
        )}
      </div>
      <figcaption
        className="break-words text-xs text-gray-600"
        data-testid="competitor-ad-source"
      >
        {t('card-source')}
        {library && (
          <>
            {' · '}
            <a
              href={library}
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium text-gray-900 underline"
              data-testid="competitor-ad-library-link"
            >
              {t('card-library-link')}
            </a>
          </>
        )}
      </figcaption>
    </figure>
  );
}
