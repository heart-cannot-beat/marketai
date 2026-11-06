import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { MarketSourceMappingFields } from '@/lib/library/draft-codecs';

interface MarketSourceMappingDraftEditorProps {
  session: UseDraftSessionResult;
}

export function MarketSourceMappingDraftEditor({
  session,
}: MarketSourceMappingDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as
    | MarketSourceMappingFields
    | undefined;
  if (!fields) return null;

  return (
    <div className="space-y-4">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.market-source-mapping.display-name')}
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
            {t('fields.market-source-mapping.external-object-id')}
          </span>
        </div>
        <input
          type="text"
          className="input input-bordered w-full font-mono text-sm"
          value={fields.external_object_id}
          onChange={(e) =>
            session.updateField('external_object_id', e.target.value)
          }
          autoComplete="off"
        />
      </label>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.market-source-mapping.market-code')}
          </span>
        </div>
        <input
          type="text"
          className="input input-bordered w-full"
          value={fields.market_code}
          onChange={(e) => session.updateField('market_code', e.target.value)}
          autoComplete="off"
        />
      </label>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.market-source-mapping.execution-mode')}
          </span>
        </div>
        <select
          className="select select-bordered w-full"
          value={fields.execution_mode}
          onChange={(e) =>
            session.updateField('execution_mode', e.target.value)
          }
        >
          <option value="">{t('fields.version.no-value')}</option>
          <option value="manual">
            {t('fields.market-source-mapping.execution-mode-manual')}
          </option>
          <option value="scheduled">
            {t('fields.market-source-mapping.execution-mode-scheduled')}
          </option>
          <option value="streaming">
            {t('fields.market-source-mapping.execution-mode-streaming')}
          </option>
        </select>
      </label>
    </div>
  );
}
