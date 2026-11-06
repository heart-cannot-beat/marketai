import { useEffect, useState } from 'react';
import { useTranslation } from 'next-i18next';

interface PreviewValidityProps {
  /** `GenerationPreviewDetail.valid_until` — nullable in the generated type
   * (`lib/domain-client/generated/domain-v1.d.ts`); a preview without an
   * expiry is treated as not expired. */
  validUntil: string | null;
  invalidationReason: string | null;
}

/**
 * TTL countdown + stale indicator for a frozen `GenerationPreviewDetail`.
 * A preview is stale when `invalidation_reason` is set (the draft changed
 * underneath it) or the wall clock has passed `valid_until` — either state
 * takes priority over the live countdown.
 */
export function PreviewValidity({
  validUntil,
  invalidationReason,
}: PreviewValidityProps) {
  const { t } = useTranslation('common');
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(timer);
  }, []);

  if (invalidationReason) {
    return (
      <div className="alert alert-warning text-sm" role="alert">
        <span>
          {t('generation-preview-outdated', { reason: invalidationReason })}
        </span>
      </div>
    );
  }

  const expiresAt = validUntil ? new Date(validUntil).getTime() : null;
  const isExpired = expiresAt !== null && now >= expiresAt;

  if (isExpired) {
    return (
      <div className="alert alert-error text-sm" role="alert">
        <span>{t('generation-preview-expired')}</span>
      </div>
    );
  }

  if (expiresAt === null) {
    return null;
  }

  const remainingMs = expiresAt - now;
  const remainingMin = Math.max(0, Math.ceil(remainingMs / 60_000));

  return (
    <div className="text-xs text-base-content/60" aria-live="polite">
      {t('generation-preview-valid-for', { count: remainingMin })}
    </div>
  );
}
