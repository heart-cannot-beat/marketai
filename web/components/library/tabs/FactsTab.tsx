import { useTranslation } from 'next-i18next';
import type { LibraryEntryType } from '@/lib/library/navigation';
import { ManualFactImportPanel } from '../data-sources/ManualFactImportPanel';
import { ManualFactSetFactList } from '../entry-types/manual-fact-set/ManualFactSetFactList';

interface FactsTabProps {
  entryId: string;
  entryType: LibraryEntryType;
  currentVersionId: string | null;
  draftContent: Record<string, unknown> | null;
  /** The draft's stored ValidationReport, which marks its problem rows. */
  draftValidation?: Record<string, unknown> | null;
}

/**
 * A data source's Facts tab imports a facts file into a new fact set draft;
 * a fact set's Facts tab lists its facts read-only (#23). Only a data source
 * id ever reaches the import panel: the import route answers any other
 * entry with `entry_type_mismatch`.
 */
export function FactsTab({
  entryId,
  entryType,
  currentVersionId,
  draftContent,
  draftValidation = null,
}: FactsTabProps) {
  const { t } = useTranslation('library');

  if (entryType === 'data_source') {
    return (
      <div className="space-y-4">
        <ManualFactImportPanel dataSourceEntryId={entryId} />
      </div>
    );
  }

  if (entryType === 'manual_fact_set') {
    return (
      <ManualFactSetFactList
        entryId={entryId}
        currentVersionId={currentVersionId}
        draftContent={draftContent}
        draftValidation={draftValidation}
      />
    );
  }

  return (
    <div className="flex items-center justify-center py-12 text-center">
      <p className="text-sm text-gray-500 dark:text-gray-400">
        {t('tabs.placeholder', { tab: 'facts' })}
      </p>
    </div>
  );
}
