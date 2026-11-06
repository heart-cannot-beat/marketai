import { useTranslation } from 'next-i18next';
import { useLibraryEvents } from '@/hooks/library/useLibraryEvents';
import { ProblemState } from '../detail/ProblemState';
import { EventTimeline } from './EventTimeline';

interface EventsTabProps {
  entryId: string;
}

export function EventsTab({ entryId }: EventsTabProps) {
  const { t } = useTranslation('library');
  const {
    items,
    hasMore,
    isLoading,
    isLoadingMore,
    problem,
    loadMore,
    refresh,
  } = useLibraryEvents(entryId);

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

  if (items.length === 0) {
    return (
      <div className="flex items-center justify-center py-12 text-center">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('tabs.events.empty')}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <EventTimeline events={items} />
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
