import { useTranslation } from 'next-i18next';
import type { ResolvedVersion } from '@/hooks/library/useResolvedLibraryVersion';

interface VersionDetailProps {
  version: ResolvedVersion;
}

export function VersionDetail({ version }: VersionDetailProps) {
  const { t } = useTranslation('library');

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-4 space-y-3">
      <div className="flex items-center gap-3">
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {t('tabs.versions.version-label', { no: String(version.version_no) })}
        </span>
        <span
          className={`badge badge-sm ${version.state === 'effective' ? 'badge-success' : version.state === 'withdrawn' ? 'badge-error' : 'badge-warning'}`}
        >
          {t(`status.${version.state}`, { defaultValue: version.state })}
        </span>
        <span
          className={`badge badge-sm ${version.eligibility === 'eligible' ? 'badge-success' : 'badge-warning'}`}
        >
          {version.eligibility}
        </span>
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        {version.content_hash && (
          <>
            <dt className="text-gray-500 dark:text-gray-400">
              {t('tabs.versions.content-hash')}
            </dt>
            <dd className="text-gray-900 dark:text-gray-100 font-mono text-xs">
              {version.content_hash.slice(0, 16)}
            </dd>
          </>
        )}
        {version.source_version_id && (
          <>
            <dt className="text-gray-500 dark:text-gray-400">
              {t('tabs.versions.source')}
            </dt>
            <dd className="text-gray-900 dark:text-gray-100 font-mono text-xs">
              {version.source_version_id.slice(0, 8)}
            </dd>
          </>
        )}
        {version.published_at && (
          <>
            <dt className="text-gray-500 dark:text-gray-400">
              {t('tabs.versions.published-at')}
            </dt>
            <dd className="text-gray-900 dark:text-gray-100">
              {version.published_at}
            </dd>
          </>
        )}
        {version.withdraw_reason && (
          <>
            <dt className="text-gray-500 dark:text-gray-400">
              {t('tabs.versions.withdraw-reason')}
            </dt>
            <dd className="text-amber-600 dark:text-amber-400">
              {version.withdraw_reason}
            </dd>
          </>
        )}
      </dl>
    </div>
  );
}
