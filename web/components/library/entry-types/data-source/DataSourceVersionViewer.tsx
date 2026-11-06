import { useTranslation } from 'next-i18next';

interface DataSourceVersionViewerProps {
  version: Record<string, unknown>;
}

export function DataSourceVersionViewer({
  version,
}: DataSourceVersionViewerProps) {
  const { t } = useTranslation('library');
  const config = (version.config as Record<string, unknown>) ?? {};
  const configEntries = Object.entries(config);

  return (
    <dl className="space-y-3">
      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.data-source.display-name')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.display_name as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.data-source.adapter-kind')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.adapter_kind as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.data-source.execution-mode')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.execution_mode as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.data-source.config')}
        </dt>
        <dd className="mt-1">
          {configEntries.length > 0 ? (
            <pre className="rounded border border-gray-200 bg-gray-50 p-3 text-xs text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 overflow-x-auto">
              {JSON.stringify(config, null, 2)}
            </pre>
          ) : (
            <span className="text-sm text-gray-500 dark:text-gray-400">
              {t('fields.data-source.config-empty')}
            </span>
          )}
        </dd>
      </div>
    </dl>
  );
}
