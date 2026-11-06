import { useTranslation } from 'next-i18next';

// Until the Worker checks a data source and its result can be read (#383),
// the panel offers no check and claims none.
export function ConnectionCheckPanel() {
  const { t } = useTranslation('library');

  return (
    <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
      <p className="text-sm text-gray-700 dark:text-gray-300">
        {t('data-source-ops.connection-unavailable')}
      </p>
    </div>
  );
}
