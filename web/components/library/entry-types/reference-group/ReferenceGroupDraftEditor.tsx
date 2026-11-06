import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { ReferenceGroupFields } from '@/lib/library/draft-codecs';

interface ReferenceGroupDraftEditorProps {
  session: UseDraftSessionResult;
}

export function ReferenceGroupDraftEditor({
  session,
}: ReferenceGroupDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as ReferenceGroupFields | undefined;
  if (!fields) return null;

  return (
    <div className="space-y-4">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.reference-group.display-name')}
          </span>
        </div>
        <input
          type="text"
          className="input input-bordered w-full"
          value={fields.display_name}
          onChange={(e) => session.updateField('display_name', e.target.value)}
          autoComplete="off"
        />
      </label>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.reference-group.group-intent')}
          </span>
        </div>
        <textarea
          className="textarea textarea-bordered w-full"
          rows={3}
          value={fields.group_intent}
          onChange={(e) => session.updateField('group_intent', e.target.value)}
        />
      </label>
    </div>
  );
}
