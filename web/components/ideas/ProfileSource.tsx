import { useTranslation } from 'next-i18next';
import {
  countryFromMarket,
  countryName,
} from '@/components/competitors/market';
import {
  useFirstBatch,
  useFirstBatchIntake,
} from '@/hooks/ideas/useFirstBatch';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import { formatDate } from '@/lib/i18n/format';
import { pastedLink } from './UnfinishedReadings';

/**
 * Where the product profile came from (design "product profile", "source";
 * #204): the product's name, the page it was read from, when, and where its
 * ads go. Read only: reading the page again is not part of step 1.
 */
export function ProfileSource({ detail }: { detail: IdeasDetail }) {
  const { t, i18n } = useTranslation('ideas');
  const { batch } = useFirstBatch(detail.first_batch?.id ?? null);
  const intake = useFirstBatchIntake(batch?.intake_id ?? null, false);
  const link = pastedLink(intake?.input?.url);
  const country = countryFromMarket(detail.market);
  const rows: [string, string][] = [];
  if (detail.understanding?.product_name)
    rows.push([t('profile-source-name'), detail.understanding.product_name]);
  // Only once the reading is known: a product page, or what was pasted.
  if (intake)
    rows.push([t('profile-source-page'), link ?? t('profile-source-pasted')]);
  if (batch?.created_at)
    rows.push([
      t('profile-source-read-on'),
      formatDate(i18n.language, batch.created_at),
    ]);
  rows.push([
    t('profile-source-market'),
    t('profile-source-market-value', {
      market: country ? countryName(i18n.language, country) : detail.market,
      language: t(`start-ad-language-${detail.ad_language}`, {
        defaultValue: detail.ad_language,
      }),
    }),
  ]);
  return (
    <section
      aria-labelledby="profile-source-title"
      className="space-y-3"
      data-testid="profile-source"
    >
      <h2 id="profile-source-title" className="text-lg font-bold">
        {t('profile-source-title')}
      </h2>
      <dl className="grid gap-x-4 gap-y-2 rounded-xl border border-gray-200 p-4 text-sm dark:border-gray-700 sm:grid-cols-[10rem_minmax(0,1fr)]">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-gray-600 dark:text-gray-400">{label}</dt>
            <dd className="break-words">{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
