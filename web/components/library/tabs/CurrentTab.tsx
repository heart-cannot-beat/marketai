import { useTranslation } from 'next-i18next';
import { useResolvedLibraryVersion } from '@/hooks/library/useResolvedLibraryVersion';
import { ProblemState } from '../detail/ProblemState';

interface CurrentTabProps {
  entryId: string;
  currentVersionId: string | null;
  onGoToDraft: () => void;
}

export function CurrentTab({
  entryId,
  currentVersionId,
  onGoToDraft,
}: CurrentTabProps) {
  const { t } = useTranslation('library');

  const { version, problem, mismatch, isLoading, refresh } =
    useResolvedLibraryVersion(currentVersionId, entryId);

  if (!currentVersionId) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('tabs.current.no-version')}
        </p>
        <button
          type="button"
          onClick={onGoToDraft}
          className="mt-3 btn btn-sm btn-primary"
        >
          {t('tabs.current.go-to-draft')}
        </button>
      </div>
    );
  }

  if (isLoading) {
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

  if (problem) {
    return <ProblemState onRetry={() => refresh()} />;
  }

  if (!version) return null;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {t('tabs.current.version-label', { no: String(version.version_no) })}
        </span>
        <span
          className={`badge badge-sm ${version.state === 'effective' ? 'badge-success' : 'badge-warning'}`}
        >
          {t(`status.${version.state}`, { defaultValue: version.state })}
        </span>
      </div>
      {version.content_hash && (
        <p className="text-xs text-gray-400 dark:text-gray-500 font-mono">
          {version.content_hash.slice(0, 16)}
        </p>
      )}
      {version.published_at && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          {t('tabs.current.published-at', { date: version.published_at })}
        </p>
      )}
      {version.withdraw_reason && (
        <p className="text-xs text-amber-600 dark:text-amber-400">
          {t('tabs.current.withdraw-reason', {
            reason: version.withdraw_reason,
          })}
        </p>
      )}
    </div>
  );
}
