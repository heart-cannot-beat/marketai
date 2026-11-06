import { useTranslation } from 'next-i18next';
import { useState } from 'react';
import { useLibraryEntries } from '@/hooks/library/useLibraryEntries';
import type { LibraryCategory } from '@/lib/library/navigation';
import { rootTypeForCategory } from '@/lib/library/navigation';
import { CreateRootEntryDialog } from '../commands/CreateRootEntryDialog';
import { LibraryEntryRow } from './LibraryEntryRow';
import { LibraryEmptyCategory } from './LibraryEmptyCategory';
import { ProblemState } from '../detail/ProblemState';

interface LibraryListViewProps {
  category: LibraryCategory;
  selectedEntryId: string | null;
  onSelectEntry: (entryId: string) => void;
}

export function LibraryListView({
  category,
  selectedEntryId,
  onSelectEntry,
}: LibraryListViewProps) {
  const { t } = useTranslation('library');
  const [createOpen, setCreateOpen] = useState(false);
  const rootType = rootTypeForCategory(category);
  const {
    items,
    hasMore,
    isLoading,
    isLoadingMore,
    isRefreshing,
    problem,
    loadMore,
    refresh,
  } = useLibraryEntries(rootType);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12" aria-busy="true">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('list.loading')}
        </p>
      </div>
    );
  }

  if (problem && items.length === 0) {
    return <ProblemState onRetry={() => refresh()} />;
  }

  const createEntryControl = (
    <>
      <div className="px-3 py-2 border-b border-gray-100 dark:border-gray-800">
        <button
          type="button"
          className="btn btn-sm btn-primary w-full"
          onClick={() => setCreateOpen(true)}
        >
          {t('commands.create-entry')}
        </button>
      </div>
      {createOpen && (
        <CreateRootEntryDialog
          category={category}
          open
          onClose={() => setCreateOpen(false)}
          onCreated={(entryId) => {
            setCreateOpen(false);
            onSelectEntry(entryId);
          }}
        />
      )}
    </>
  );

  if (items.length === 0) {
    return (
      <div className="flex flex-col h-full">
        {createEntryControl}
        <LibraryEmptyCategory category={category} />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {createEntryControl}
      {isRefreshing && (
        <div
          className="px-3 py-1 text-xs text-gray-500 dark:text-gray-400"
          aria-live="polite"
        >
          {t('list.refreshing')}
        </div>
      )}
      <ul
        role="list"
        className="flex-1 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-800"
      >
        {items.map((entry) => (
          <LibraryEntryRow
            key={entry.id}
            entry={entry}
            isSelected={entry.id === selectedEntryId}
            onSelect={onSelectEntry}
          />
        ))}
      </ul>
      <div className="px-3 py-2 text-center">
        {problem && items.length > 0 && (
          <p
            className="text-xs text-red-600 dark:text-red-400 mb-1"
            role="alert"
          >
            {t('problem.description')}
          </p>
        )}
        {hasMore ? (
          <button
            type="button"
            onClick={loadMore}
            disabled={isLoadingMore}
            className="btn btn-sm btn-ghost w-full"
          >
            {isLoadingMore ? t('list.loading') : t('list.load-more')}
          </button>
        ) : (
          items.length > 0 && (
            <p className="text-xs text-gray-400 dark:text-gray-500">
              {t('list.end-of-list')}
            </p>
          )
        )}
      </div>
    </div>
  );
}
