import useSWR from 'swr';
import { useTranslation } from 'next-i18next';

type Projection = {
  lifecycle: string;
  commandsEnabled: boolean;
  version: number;
  items: Array<{
    key: string;
    labelKey: string;
    complete: boolean;
    completedAt: string | null;
  }>;
};

const fetcher = async (url: string): Promise<Projection> => {
  const response = await fetch(url);
  const body = await response.json();
  if (!response.ok)
    throw new Error(
      body.error?.message ?? 'Unable to load onboarding progress.'
    );
  return body.data;
};

export function FirstValueChecklist({ slug }: { slug: string }) {
  const { t } = useTranslation('common');
  const { data, error } = useSWR(
    `/api/onboarding/${encodeURIComponent(slug)}`,
    fetcher
  );
  if (error) {
    return (
      <div role="alert" className="mb-6 rounded border border-error p-4">
        {t('onboarding-status-unavailable')}
      </div>
    );
  }
  if (!data) {
    return (
      <div
        aria-label="Loading onboarding checklist"
        className="mb-6 h-24 animate-pulse rounded bg-base-200"
      />
    );
  }
  const remaining = data.items.filter((item) => !item.complete).length;
  if (remaining === 0 && data.commandsEnabled) return null;
  return (
    <section
      aria-labelledby="first-value-title"
      className="mb-6 rounded-lg border border-base-300 bg-base-100 p-5"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="first-value-title" className="text-lg font-semibold">
          {t('onboarding-title')}
        </h2>
        <span className="badge badge-outline">
          {data.items.length - remaining}/{data.items.length}
        </span>
      </div>
      {!data.commandsEnabled && (
        <p role="status" className="mt-2 text-sm text-warning">
          {t('onboarding-provisioning-status')}
        </p>
      )}
      <ul className="mt-4 grid gap-2 sm:grid-cols-2">
        {data.items.map((item) => (
          <li key={item.key} className="flex items-center gap-2 text-sm">
            <span
              aria-hidden="true"
              className={
                item.complete ? 'text-success' : 'text-base-content/40'
              }
            >
              {item.complete ? '✓' : '○'}
            </span>
            <span className={item.complete ? 'line-through opacity-60' : ''}>
              {t(item.labelKey)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
