import { useTranslation } from 'next-i18next';
import { useLibraryVersions } from '@/hooks/library/useLibraryVersions';
import { useResolvedLibraryVersion } from '@/hooks/library/useResolvedLibraryVersion';
import { ProblemState } from '../detail/ProblemState';
import { VersionDetail } from './VersionDetail';

interface VersionsTabProps {
  entryId: string;
  entryDisplayName: string;
  selectedVersionId: string | null;
  onSelectVersion: (versionId: string | null) => void;
  /** Opens the entry a Fork or Clone Product created, on its Draft. */
  onEntryCreated: (entryId: string) => void;
}

export function VersionsTab({
  entryId,
  entryDisplayName,
  selectedVersionId,
  onSelectVersion,
  onEntryCreated,
}: VersionsTabProps) {
  const { t } = useTranslation('library');
  const {
    items,
    hasMore,
    isLoading,
    isLoadingMore,
    problem,
    loadMore,
    refresh,
  } = useLibraryVersions(entryId);

  const {
    version: resolvedVersion,
    problem: versionProblem,
    isLoading: isLoadingVersion,
    mismatch,
    refresh: refreshVersion,
  } = useResolvedLibraryVersion(selectedVersionId, entryId);

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
          {t('tabs.versions.empty')}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {selectedVersionId && resolvedVersion && !mismatch && (
        <VersionDetail
          version={resolvedVersion}
          entryDisplayName={entryDisplayName}
          onEntryCreated={onEntryCreated}
        />
      )}
      {selectedVersionId && isLoadingVersion && (
        <div className="py-4 text-center" aria-busy="true">
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {t('detail.loading')}
          </p>
        </div>
      )}
      {selectedVersionId &&
        versionProblem &&
        !resolvedVersion &&
        !isLoadingVersion && (
          <div className="py-4 text-center" role="alert">
            {versionProblem.status === 404 ? (
              <p className="text-sm text-red-600 dark:text-red-400">
                {t('tabs.versions.not-found')}
              </p>
            ) : (
              <>
                <p className="text-sm text-red-600 dark:text-red-400">
                  {t('problem.description')}
                </p>
                <button
                  type="button"
                  onClick={() => void refreshVersion()}
                  className="btn btn-sm btn-outline mt-2"
                >
                  {t('problem.retry')}
                </button>
              </>
            )}
          </div>
        )}
      {selectedVersionId && mismatch && (
        <div className="py-4 text-center" role="alert">
          <p className="text-sm text-red-600 dark:text-red-400">
            {t('tabs.current.version-mismatch')}
          </p>
        </div>
      )}

      <ul role="list" className="divide-y divide-gray-100 dark:divide-gray-800">
        {items.map((v) => (
          <li key={v.id}>
            <button
              type="button"
              onClick={() =>
                onSelectVersion(v.id === selectedVersionId ? null : v.id)
              }
              aria-current={v.id === selectedVersionId || undefined}
              className={`w-full text-left px-3 py-2 text-sm transition-colors ${
                v.id === selectedVersionId
                  ? 'bg-blue-50 dark:bg-blue-900/20'
                  : 'hover:bg-gray-50 dark:hover:bg-gray-800'
              }`}
            >
              <span className="font-medium text-gray-900 dark:text-gray-100">
                {t('tabs.versions.version-label', { no: String(v.version_no) })}
              </span>
              <span
                className={`ml-2 badge badge-xs ${v.state === 'effective' ? 'badge-success' : v.state === 'withdrawn' ? 'badge-error' : 'badge-warning'}`}
              >
                {t(`status.${v.state}`, { defaultValue: v.state })}
              </span>
            </button>
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
