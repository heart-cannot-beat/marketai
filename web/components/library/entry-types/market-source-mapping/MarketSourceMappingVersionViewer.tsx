import { useTranslation } from 'next-i18next';

interface MarketSourceMappingVersionViewerProps {
  version: Record<string, unknown>;
}

export function MarketSourceMappingVersionViewer({
  version,
}: MarketSourceMappingVersionViewerProps) {
  const { t } = useTranslation('library');

  return (
    <dl className="space-y-3">
      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.market-source-mapping.display-name')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.display_name as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.market-source-mapping.external-object-id')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100 font-mono text-xs">
          {(version.external_object_id as string) ||
            t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.market-source-mapping.market-code')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.market_code as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.market-source-mapping.execution-mode')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.execution_mode as string) || t('fields.version.no-value')}
        </dd>
      </div>
    </dl>
  );
}
