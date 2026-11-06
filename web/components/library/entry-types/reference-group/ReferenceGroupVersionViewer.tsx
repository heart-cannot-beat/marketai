import { useTranslation } from 'next-i18next';

interface ReferenceGroupVersionViewerProps {
  version: Record<string, unknown>;
}

export function ReferenceGroupVersionViewer({
  version,
}: ReferenceGroupVersionViewerProps) {
  const { t } = useTranslation('library');

  return (
    <dl className="space-y-3">
      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.reference-group.display-name')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.display_name as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.reference-group.group-intent')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100 whitespace-pre-wrap">
          {(version.group_intent as string) || t('fields.version.no-value')}
        </dd>
      </div>
    </dl>
  );
}
