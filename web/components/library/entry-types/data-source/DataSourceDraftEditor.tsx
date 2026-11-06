import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { DataSourceFields } from '@/lib/library/draft-codecs';

interface DataSourceDraftEditorProps {
  session: UseDraftSessionResult;
}

export function DataSourceDraftEditor({ session }: DataSourceDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as DataSourceFields | undefined;
  if (!fields) return null;

  const configEntries = Object.entries(fields.config ?? {});

  return (
    <div className="space-y-4">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.data-source.display-name')}
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
            {t('fields.data-source.adapter-kind')}
          </span>
        </div>
        <input
          type="text"
          className="input input-bordered w-full"
          value={fields.adapter_kind}
          onChange={(e) => session.updateField('adapter_kind', e.target.value)}
          autoComplete="off"
        />
      </label>

      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.data-source.execution-mode')}
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
            {t('fields.data-source.execution-mode-manual')}
          </option>
          <option value="scheduled">
            {t('fields.data-source.execution-mode-scheduled')}
          </option>
          <option value="streaming">
            {t('fields.data-source.execution-mode-streaming')}
          </option>
        </select>
      </label>

      <div>
        <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {t('fields.data-source.config')}
        </p>
        {configEntries.length > 0 ? (
          <pre className="mt-1 rounded border border-gray-200 bg-gray-50 p-3 text-xs text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 overflow-x-auto">
            {JSON.stringify(fields.config, null, 2)}
          </pre>
        ) : (
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            {t('fields.data-source.config-empty')}
          </p>
        )}
      </div>
    </div>
  );
}
