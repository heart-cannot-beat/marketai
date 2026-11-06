import { useTranslation } from 'next-i18next';
import { useResolvedLibraryResource } from '@/hooks/library/useResolvedLibraryResource';
import type { LibraryEntryType } from '@/lib/library/navigation';
import { ProblemState } from '../detail/ProblemState';

interface NestedResourcePanelProps {
  rootEntryId: string;
  resourceType: LibraryEntryType;
  resourceId: string;
  onDismiss: () => void;
}

export function NestedResourcePanel({
  rootEntryId,
  resourceType,
  resourceId,
  onDismiss,
}: NestedResourcePanelProps) {
  const { t } = useTranslation('library');
  const { resource, problem, isLoading, notFound, refresh } =
    useResolvedLibraryResource(rootEntryId, resourceType, resourceId);

  if (isLoading) {
    return (
      <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-4">
        <div className="flex items-center justify-center py-8" aria-busy="true">
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {t('detail.loading')}
          </p>
        </div>
      </div>
    );
  }

  if (notFound) {
    return (
      <div
        className="border border-gray-200 dark:border-gray-700 rounded-lg p-4"
        role="alert"
      >
        <div className="flex items-center justify-between mb-3">
          <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
            {t('nested.not-found')}
          </p>
          <button
            type="button"
            onClick={onDismiss}
            className="btn btn-sm btn-ghost"
            aria-label={t('nested.dismiss')}
          >
            {t('nested.dismiss')}
          </button>
        </div>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('nested.not-found-description')}
        </p>
      </div>
    );
  }

  if (problem) {
    return (
      <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-4">
        <ProblemState onRetry={() => refresh()} />
      </div>
    );
  }

  if (!resource) return null;

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100">
            {resource.display_name}
          </h3>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {t(`entry-type.${resource.entry_type}`, {
              defaultValue: resource.entry_type,
            })}
            {' · '}
            {resource.relation_kind}
          </p>
        </div>
        <button
          type="button"
          onClick={onDismiss}
          className="btn btn-sm btn-ghost"
          aria-label={t('nested.dismiss')}
        >
          {t('nested.dismiss')}
        </button>
      </div>
      <dl className="grid grid-cols-2 gap-2 text-sm">
        <dt className="text-gray-500 dark:text-gray-400">
          {t('nested.lifecycle')}
        </dt>
        <dd className="text-gray-900 dark:text-gray-100">
          {t(`status.${resource.lifecycle}`, {
            defaultValue: resource.lifecycle,
          })}
        </dd>
        <dt className="text-gray-500 dark:text-gray-400">{t('nested.root')}</dt>
        <dd className="text-gray-900 dark:text-gray-100 font-mono text-xs">
          {resource.root_entry_id.slice(0, 8)}
        </dd>
      </dl>
    </div>
  );
}
