import { useTranslation } from 'next-i18next';
import type { LibraryEntrySummary } from '@/hooks/library/useLibraryEntries';

interface EntryHeaderProps {
  entry: LibraryEntrySummary;
}

export function EntryHeader({ entry }: EntryHeaderProps) {
  const { t } = useTranslation('library');

  return (
    <div className="mb-4">
      <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
        {entry.display_name}
      </h2>
      <div className="mt-1 flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
        <span
          className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
            entry.lifecycle === 'archived'
              ? 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400'
              : 'bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-400'
          }`}
        >
          {t(`status.${entry.lifecycle}`, { defaultValue: entry.lifecycle })}
        </span>
        {entry.draft_state && (
          <span className="inline-flex items-center rounded-full bg-blue-50 px-2 py-0.5 text-xs font-medium text-blue-700 dark:bg-blue-900/20 dark:text-blue-400">
            {t(`status.${entry.draft_state}`, {
              defaultValue: entry.draft_state,
            })}
          </span>
        )}
        {entry.current_version_number !== null && (
          <span className="text-xs">
            {t('tabs.versions.version-label', {
              no: entry.current_version_number,
            })}
          </span>
        )}
      </div>
    </div>
  );
}
