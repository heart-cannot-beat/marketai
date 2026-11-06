import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { UserCircleIcon } from '@heroicons/react/24/outline';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import { formatCount, type CompetitorCandidate } from './shared';

/**
 * The page a confirmation would bind (spec §4.3). The avatar is the copy
 * the resolve archived, never the provider's link (#332): read from its
 * short-lived signed link into a blob URL, like a board thumbnail, so the
 * page's `img-src` stays `'self' data: blob:`. No avatar, or a failed
 * download, shows the placeholder.
 */
export function CandidateCard({
  candidate,
  websiteDomain,
}: {
  candidate: CompetitorCandidate;
  websiteDomain?: string | null;
}) {
  const { t, i18n } = useTranslation('competitors');
  const probe = candidate.probe;

  return (
    <article
      className="space-y-3 rounded-lg border border-gray-200 bg-white p-4"
      data-testid="competitor-candidate-card"
      data-page-id={candidate.page_id}
    >
      <div className="flex items-start gap-3">
        <CandidateAvatar candidate={candidate} />
        <div className="min-w-0 flex-1">
          <p className="break-words font-medium">
            {candidate.page_name || t('candidate-unnamed')}
          </p>
          <p className="break-all text-sm text-gray-600">
            {candidate.page_alias
              ? `@${candidate.page_alias}`
              : t('candidate-no-alias')}
          </p>
          <p className="text-xs text-gray-500">
            {t('candidate-page-id', { id: candidate.page_id })}
          </p>
        </div>
      </div>
      <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-2">
        <Fact label={t('candidate-category')}>
          {candidate.category || t('candidate-unknown')}
        </Fact>
        <Fact label={t('candidate-likes')}>
          {candidate.likes === null || candidate.likes === undefined
            ? t('candidate-unknown')
            : formatCount(i18n.language, candidate.likes)}
        </Fact>
        <Fact label={t('candidate-instagram')}>
          {candidate.ig_username
            ? candidate.ig_followers !== null &&
              candidate.ig_followers !== undefined
              ? t('candidate-instagram-value', {
                  username: candidate.ig_username,
                  followers: formatCount(i18n.language, candidate.ig_followers),
                })
              : `@${candidate.ig_username}`
            : t('candidate-none')}
        </Fact>
        <Fact label={t('candidate-active-ads')}>
          {probe?.status === 'succeeded' &&
          probe.total_active_ads !== null &&
          probe.total_active_ads !== undefined
            ? formatCount(i18n.language, probe.total_active_ads)
            : probe
              ? t(`candidate-probe-${probe.status}`)
              : t('candidate-not-checked')}
        </Fact>
        <Fact label={t('candidate-landing-domains')}>
          {probe?.landing_domains?.length
            ? probe.landing_domains.join(', ')
            : probe?.status === 'succeeded'
              ? t('candidate-none')
              : t('candidate-not-checked')}
        </Fact>
        <Fact label={t('candidate-domain-hit')}>
          <span
            data-testid="competitor-domain-hit"
            data-hit={String(candidate.domain_hit)}
            className={
              candidate.domain_hit === true
                ? 'font-medium text-green-700'
                : candidate.domain_hit === false
                  ? 'text-amber-700'
                  : 'text-gray-600'
            }
          >
            {candidate.domain_hit === true
              ? t('candidate-domain-hit-yes', { domain: websiteDomain ?? '' })
              : candidate.domain_hit === false
                ? t('candidate-domain-hit-no', { domain: websiteDomain ?? '' })
                : websiteDomain
                  ? t('candidate-not-checked')
                  : t('candidate-domain-hit-no-site')}
          </span>
        </Fact>
      </dl>
      {candidate.excluded_reason && (
        <p className="text-sm text-amber-700">
          {t(`candidate-excluded-${candidate.excluded_reason}`)}
        </p>
      )}
    </article>
  );
}

function CandidateAvatar({ candidate }: { candidate: CompetitorCandidate }) {
  const blobClient = useBlobTransferClient();
  const [src, setSrc] = useState<string | null>(null);
  // A polled resolve signs the same copy again; only the page and whether
  // it has a link at all decide a reload.
  const linkRef = useRef(candidate.avatar);
  linkRef.current = candidate.avatar;
  const hasLink = Boolean(candidate.avatar?.url);

  useEffect(() => {
    const link = linkRef.current;
    setSrc(null);
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
        // The placeholder stays.
      });
    return () => {
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [candidate.page_id, hasLink, blobClient]);

  if (!src) {
    return (
      <UserCircleIcon
        className="h-12 w-12 shrink-0 text-gray-300"
        aria-hidden="true"
        data-testid="competitor-avatar-placeholder"
      />
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element -- a signed blob, not an optimizable asset
    <img
      src={src}
      alt=""
      className="h-12 w-12 shrink-0 select-none rounded-full object-cover"
      draggable={false}
      data-testid="competitor-avatar"
    />
  );
}

function Fact({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-gray-500">{label}</dt>
      <dd className="break-words">{children}</dd>
    </div>
  );
}
