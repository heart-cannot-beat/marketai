import { useTranslation } from 'next-i18next';

export function ResolutionTab() {
  const { t } = useTranslation('library');

  return (
    <div className="flex items-center justify-center py-12 text-center">
      <p className="text-sm text-gray-500 dark:text-gray-400">
        {t('tabs.placeholder', { tab: 'resolution' })}
      </p>
    </div>
  );
}
