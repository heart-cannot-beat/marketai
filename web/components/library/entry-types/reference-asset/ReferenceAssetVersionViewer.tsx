import { useTranslation } from 'next-i18next';

interface ReferenceAssetVersionViewerProps {
  version: Record<string, unknown>;
}

export function ReferenceAssetVersionViewer({
  version,
}: ReferenceAssetVersionViewerProps) {
  const { t } = useTranslation('library');

  return (
    <dl className="space-y-3">
      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.reference-asset.display-name')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.display_name as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.reference-asset.asset-purpose')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100 whitespace-pre-wrap">
          {(version.asset_purpose as string) || t('fields.version.no-value')}
        </dd>
      </div>
    </dl>
  );
}
