import { useTranslation } from 'next-i18next';
import type { LibraryEntryType } from '@/lib/library/navigation';
import { TemplateReapplyPanel } from '../templates/TemplateReapplyPanel';

interface SchemaTabProps {
  entryId: string;
  entryType: LibraryEntryType;
  versionId: string | null;
  currentVersionId: string | null;
}

export function SchemaTab({
  entryId,
  entryType,
  versionId,
  currentVersionId,
}: SchemaTabProps) {
  const { t } = useTranslation('library');
  const effectiveVersionId = versionId ?? currentVersionId;

  if (entryType === 'dimension_schema' && effectiveVersionId) {
    return (
      <div className="space-y-4">
        <TemplateReapplyPanel
          templateVersionId={effectiveVersionId}
          schemaEntryId={entryId}
        />
      </div>
    );
  }

  return (
    <div className="flex items-center justify-center py-12 text-center">
      <p className="text-sm text-gray-500 dark:text-gray-400">
        {t('tabs.placeholder', { tab: 'schema' })}
      </p>
    </div>
  );
}
