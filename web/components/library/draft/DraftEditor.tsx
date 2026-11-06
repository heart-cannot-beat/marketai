import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import type { LibraryEntryType } from '@/lib/library/navigation';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import { getDraftFieldsEditor } from '@/components/library/entry-types/draft-editors';
import { UnsupportedSchemaState } from './UnsupportedSchemaState';
import { ValidationPanel } from './ValidationPanel';
import { ConflictDialog } from './ConflictDialog';
import { RawJsonDraftEditor } from './RawJsonDraftEditor';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface DraftEditorProps {
  entryId: string;
  entryType: LibraryEntryType;
  schemaVersion: number;
  session: UseDraftSessionResult;
}

/**
 * The Draft tab's body: the toolbar (save / validate / discard), the server's
 * validation findings, the entry type's form editor when one is aligned with
 * the server validator (`entry-types/draft-editors.ts`), and the raw JSON
 * editor -- the only editor for the other entry types, and a toggle for the
 * rest, since a form never covers every key the validator reads.
 */
export function DraftEditor({
  entryType,
  schemaVersion,
  session,
}: DraftEditorProps) {
  const { t } = useTranslation('library');
  const FieldsEditor = getDraftFieldsEditor(entryType);
  const [showJson, setShowJson] = useState(false);
  const jsonVisible = FieldsEditor === undefined || showJson;

  if (!session.isSupported) {
    return (
      <UnsupportedSchemaState
        schemaVersion={schemaVersion}
        entryType={entryType}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {t('draft.editor-title')}
        </h3>
        <DraftToolbar session={session} />
      </div>

      {session.saveProblem && (
        <div
          className="rounded-lg border border-red-300 bg-red-50 p-3 dark:border-red-700 dark:bg-red-950"
          role="alert"
        >
          <p className="text-sm text-red-800 dark:text-red-200">
            {t('draft.save-error')}
          </p>
          <p className="mt-1 text-xs text-red-700 dark:text-red-300">
            {errorMessage(t, session.saveProblem)}
          </p>
        </div>
      )}

      {session.validation && (
        <ValidationPanel
          validation={session.validation}
          onAcknowledge={session.acknowledgeWarning}
        />
      )}

      {session.buffer && FieldsEditor && (
        <div className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
          <FieldsEditor session={session} />
        </div>
      )}

      {session.buffer && (
        <div className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
          {FieldsEditor && (
            <button
              type="button"
              className="btn btn-ghost btn-xs"
              onClick={() => setShowJson((v) => !v)}
              aria-expanded={jsonVisible}
            >
              {jsonVisible ? t('draft.hide-json') : t('draft.show-json')}
            </button>
          )}
          {!FieldsEditor && (
            <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
              {t('draft.json-only-hint', { type: entryType })}
            </p>
          )}
          {jsonVisible && (
            <RawJsonDraftEditor entryType={entryType} session={session} />
          )}
        </div>
      )}

      {session.conflict && (
        <ConflictDialog
          conflict={session.conflict}
          onReloadServer={session.resolveConflictWithServer}
          onKeepLocal={session.resolveConflictWithLocal}
          onCancel={session.cancelConflict}
        />
      )}
    </div>
  );
}

function DraftToolbar({ session }: { session: UseDraftSessionResult }) {
  const { t } = useTranslation('library');

  return (
    <div
      className="flex items-center gap-2"
      role="toolbar"
      aria-label={t('draft.toolbar')}
    >
      {session.isDirty && (
        <span className="badge badge-xs badge-warning">
          {t('draft.unsaved')}
        </span>
      )}

      <button
        type="button"
        className="btn btn-ghost btn-xs"
        onClick={session.discard}
        disabled={!session.isDirty}
      >
        {t('draft.discard')}
      </button>

      <button
        type="button"
        className="btn btn-ghost btn-xs"
        onClick={() => void session.validate()}
        disabled={session.isValidating || session.isDirty}
      >
        {session.isValidating ? t('draft.validating') : t('draft.validate')}
      </button>

      <button
        type="button"
        className="btn btn-primary btn-xs"
        onClick={() => void session.save()}
        disabled={session.isSaving || !session.isDirty}
      >
        {session.isSaving ? t('draft.saving') : t('draft.save')}
      </button>
    </div>
  );
}
