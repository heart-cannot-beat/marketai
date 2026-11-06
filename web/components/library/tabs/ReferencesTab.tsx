import { useTranslation } from 'next-i18next';
import { useState } from 'react';
import { ReferenceUploadZone } from '../references/ReferenceUploadZone';
import { ReferenceBindingList } from '../references/ReferenceBindingList';
import { DraftReferenceBindings } from '../references/DraftReferenceBindings';
import type { DraftReferenceBinding } from '../references/DraftReferenceBindings';

interface ReferencesTabProps {
  entryId: string;
  entryType?: string;
  versionId: string | null;
  currentVersionId: string | null;
  /** `reference_bindings` of the entry's open Draft (products/persons). */
  draftBindings?: DraftReferenceBinding[];
}

const BINDABLE_ENTRY_TYPES = new Set(['product', 'person']);

export function ReferencesTab({
  entryId,
  entryType,
  versionId,
  currentVersionId,
  draftBindings,
}: ReferencesTabProps) {
  const { t } = useTranslation('library');
  const effectiveVersionId = versionId ?? currentVersionId;
  const [uploadKey, setUploadKey] = useState(0);
  const bindable = !!entryType && BINDABLE_ENTRY_TYPES.has(entryType);

  return (
    <div className="space-y-6">
      <section>
        <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100 mb-3">
          {t('references.upload-zone')}
        </h3>
        <ReferenceUploadZone
          key={uploadKey}
          contextEntryId={entryId}
          bindToContext={bindable}
          onComplete={() => setUploadKey((k) => k + 1)}
        />
      </section>

      {bindable && (
        <section>
          <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100 mb-3">
            {t('references.draft-bindings')}
          </h3>
          <DraftReferenceBindings bindings={draftBindings ?? []} />
        </section>
      )}

      <section>
        <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100 mb-3">
          {t('references.published-bindings')}
        </h3>
        <ReferenceBindingList
          entryId={entryId}
          versionId={effectiveVersionId}
        />
      </section>
    </div>
  );
}
