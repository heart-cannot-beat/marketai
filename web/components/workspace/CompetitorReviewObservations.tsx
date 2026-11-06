import { useTranslation } from 'next-i18next';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type ReviewItem = components['schemas']['ReviewItem'];

/**
 * The competitor observations a plan-review finding points at (#273), each
 * linked to its public Meta Ad Library page. They show what others run, never
 * evidence for this product (ADR 0014).
 */
export function CompetitorReviewObservations({ item }: { item: ReviewItem }) {
  const { t } = useTranslation('competitors');
  const observations = item.observations ?? [];
  if (!observations.length) return null;
  return (
    <div className="mt-2 text-xs text-gray-600">
      <p className="font-medium">{t('review-observations')}</p>
      <ul className="mt-1 space-y-1">
        {observations.map((observation) => (
          <li key={observation.ad_id} className="break-words">
            {observation.advertiser_name || observation.ad_id}
            {observation.days_running != null && (
              <>
                {' · '}
                {t('review-observation-days', {
                  count: observation.days_running,
                })}
              </>
            )}
            {observation.library_url && (
              <>
                {' · '}
                <a
                  href={observation.library_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline"
                >
                  {t('review-observation-source')}
                </a>
              </>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
