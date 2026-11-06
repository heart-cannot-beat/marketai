import { useTranslation } from 'next-i18next';
import { useResolvedLibraryVersion } from '@/hooks/library/useResolvedLibraryVersion';
import { useLibraryBindings } from '@/hooks/library/useLibraryBindings';
import { ProblemState } from '../detail/ProblemState';

interface BindingsTabProps {
  entryId: string;
  versionId: string | null;
  currentVersionId: string | null;
}

export function BindingsTab({
  entryId,
  versionId,
  currentVersionId,
}: BindingsTabProps) {
  const { t } = useTranslation('library');
  const effectiveVersionId = versionId ?? currentVersionId;

  const { mismatch, isLoading: isLoadingVersion } = useResolvedLibraryVersion(
    effectiveVersionId,
    entryId
  );

  const {
    items,
    hasMore,
    isLoading,
    isLoadingMore,
    problem,
    loadMore,
    refresh,
  } = useLibraryBindings(effectiveVersionId, { enabled: !mismatch });

  if (!effectiveVersionId) {
    return (
      <div className="flex items-center justify-center py-12 text-center">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('tabs.bindings.no-version')}
        </p>
      </div>
    );
  }

  if (isLoadingVersion || isLoading) {
    return (
      <div className="flex items-center justify-center py-12" aria-busy="true">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('detail.loading')}
        </p>
      </div>
    );
  }

  if (mismatch) {
    return (
      <div className="py-8 text-center" role="alert">
        <p className="text-sm text-red-600 dark:text-red-400">
          {t('tabs.current.version-mismatch')}
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
          {t('tabs.bindings.empty')}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <ul role="list" className="divide-y divide-gray-100 dark:divide-gray-800">
        {items.map((binding) => (
          <li key={binding.id} className="py-2 px-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                {binding.reference_display_name || binding.reference_version_id}
              </span>
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {binding.purpose}
              </span>
            </div>
            <div className="flex gap-1 text-xs text-gray-400 dark:text-gray-500">
              <span>{t('tabs.bindings.reference-asset')}</span>
              {binding.viewpoint && <span>· {binding.viewpoint}</span>}
              <span>
                · {t('tabs.bindings.priority', { priority: binding.priority })}
              </span>
            </div>
          </li>
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
