import { useTranslation } from 'next-i18next';
import { DraftEditor } from '@/components/library/draft/DraftEditor';
import { useDraftSession } from '@/hooks/library/useDraftSession';
import type { LibraryEntryType } from '@/lib/library/navigation';

interface DraftSummary {
  id: string;
  entry_id: string;
  revision: number;
  state: string;
  content_schema_version: number;
  /** `DraftSummary.content` (`app/library/service.py::_draft_dict`) -- the
   * editable content itself, which `GET /library/entries/{id}` embeds. */
  content?: Record<string, unknown>;
  /** The ValidationReport stored for this revision, shown until the next
   * validation (a manual fact import stores its row report here, #23). */
  validation?: Record<string, unknown> | null;
  created_at?: string;
  updated_at?: string;
}

interface DraftTabProps {
  entryId: string;
  entryType: LibraryEntryType;
  draft: DraftSummary | null;
}

/**
 * The entry's open Draft: revision / state header plus the editor
 * (`DraftEditor` over `useDraftSession`). Until this tab mounted the editor,
 * a draft's content could only be changed through the API
 * (`PUT /library/entries/{id}/draft`).
 */
export function DraftTab({ entryId, entryType, draft }: DraftTabProps) {
  const { t } = useTranslation('library');
  const session = useDraftSession(entryId, entryType, draft);

  if (!draft) {
    return (
      <div className="flex items-center justify-center py-12 text-center">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('tabs.draft.no-draft')}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {t('tabs.draft.title')}
        </span>
        <span className="badge badge-sm badge-info">
          {t(`status.${draft.state}`, { defaultValue: draft.state })}
        </span>
        <span className="text-xs text-gray-500 dark:text-gray-400">
          {t('tabs.draft.revision')}
        </span>
        <span className="text-xs font-mono text-gray-900 dark:text-gray-100">
          {draft.revision}
        </span>
        {draft.updated_at && (
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {t('tabs.draft.updated')} {draft.updated_at}
          </span>
        )}
      </div>

      <DraftEditor
        entryId={entryId}
        entryType={entryType}
        schemaVersion={draft.content_schema_version}
        session={session}
      />
    </div>
  );
}
