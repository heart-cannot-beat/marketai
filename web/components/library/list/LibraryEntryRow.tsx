import { useTranslation } from 'next-i18next';
import type { LibraryEntrySummary } from '@/hooks/library/useLibraryEntries';

interface LibraryEntryRowProps {
  entry: LibraryEntrySummary;
  isSelected: boolean;
  onSelect: (entryId: string) => void;
}

export function LibraryEntryRow({
  entry,
  isSelected,
  onSelect,
}: LibraryEntryRowProps) {
  const { t } = useTranslation('library');

  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(entry.id)}
        aria-current={isSelected ? 'true' : undefined}
        className={`w-full text-left px-3 py-2 rounded-md transition-colors ${
          isSelected
            ? 'bg-blue-50 dark:bg-blue-900/20 border-l-2 border-blue-600'
            : 'hover:bg-gray-50 dark:hover:bg-gray-800'
        }`}
      >
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
            {entry.display_name}
          </span>
          {entry.lifecycle === 'archived' && (
            <span className="ml-2 inline-flex items-center rounded-full bg-gray-100 px-1.5 py-0.5 text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-400">
              {t('status.archived')}
            </span>
          )}
        </div>
        <div className="mt-0.5 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
          {entry.draft_state && (
            <span>
              {t(`status.${entry.draft_state}`, {
                defaultValue: entry.draft_state,
              })}
            </span>
          )}
          {entry.current_version_number !== null && (
            <span>
              {t('tabs.versions.version-label', {
                no: entry.current_version_number,
              })}
            </span>
          )}
        </div>
      </button>
    </li>
  );
}
