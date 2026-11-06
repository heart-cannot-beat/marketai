import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { ReferenceAssetFields } from '@/lib/library/draft-codecs';

interface ReferenceAssetDraftEditorProps {
  session: UseDraftSessionResult;
}

export function ReferenceAssetDraftEditor({
  session,
}: ReferenceAssetDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as ReferenceAssetFields | undefined;
  if (!fields) return null;

  return (
    <div className="space-y-4">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.reference-asset.display-name')}
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
            {t('fields.reference-asset.asset-purpose')}
          </span>
        </div>
        <textarea
          className="textarea textarea-bordered w-full"
          rows={3}
          value={fields.asset_purpose}
          onChange={(e) => session.updateField('asset_purpose', e.target.value)}
        />
      </label>
    </div>
  );
}
