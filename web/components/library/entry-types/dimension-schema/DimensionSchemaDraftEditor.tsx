import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { DimensionSchemaFields } from '@/lib/library/draft-codecs';

interface DimensionSchemaDraftEditorProps {
  session: UseDraftSessionResult;
}

function newDimensionId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `dim_${Math.random().toString(36).slice(2)}`;
}

export function DimensionSchemaDraftEditor({
  session,
}: DimensionSchemaDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as DimensionSchemaFields | undefined;
  if (!fields) return null;

  const dims = fields.dimensions ?? [];

  return (
    <div className="space-y-4">
      <label className="form-control w-full">
        <div className="label">
          <span className="label-text">
            {t('fields.dimension-schema.display-name')}
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

      <fieldset>
        <legend className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {t('fields.dimension-schema.dimensions')}
        </legend>
        <div className="mt-2 space-y-3">
          {dims.map((dim, i) => (
            <div
              key={dim.id}
              className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"
            >
              <div className="flex items-center justify-between">
                <label className="form-control flex-1">
                  <div className="label">
                    <span className="label-text text-xs">
                      {t('fields.dimension-schema.dimension-name')}
                    </span>
                  </div>
                  <input
                    type="text"
                    className="input input-bordered input-sm w-full"
                    value={dim.name}
                    onChange={(e) => {
                      const next = [...dims];
                      next[i] = { ...dim, name: e.target.value };
                      session.updateField('dimensions', next);
                    }}
                    autoComplete="off"
                  />
                </label>
                <button
                  type="button"
                  className="btn btn-ghost btn-xs text-red-500 ml-2 mt-6"
                  onClick={() => {
                    session.updateField(
                      'dimensions',
                      dims.filter((_, j) => j !== i)
                    );
                  }}
                  aria-label={t('fields.dimension-schema.remove-dimension')}
                >
                  ×
                </button>
              </div>
              <label className="form-control mt-2 w-full">
                <div className="label">
                  <span className="label-text text-xs">
                    {t('fields.dimension-schema.dimension-values')}
                  </span>
                </div>
                <textarea
                  className="textarea textarea-bordered textarea-sm w-full"
                  rows={2}
                  value={(dim.values ?? []).join(', ')}
                  onChange={(e) => {
                    const next = [...dims];
                    next[i] = {
                      ...dim,
                      values: e.target.value
                        .split(',')
                        .map((v) => v.trim())
                        .filter(Boolean),
                    };
                    session.updateField('dimensions', next);
                  }}
                />
              </label>
              <div className="mt-2">
                <p className="text-xs font-medium text-gray-500 dark:text-gray-400">
                  {t('fields.dimension-schema.constraints')}
                </p>
                <pre className="mt-1 rounded border border-gray-200 bg-gray-50 p-2 text-xs text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 overflow-x-auto">
                  {JSON.stringify(dim.constraints ?? {}, null, 2)}
                </pre>
              </div>
            </div>
          ))}
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={() => {
              session.updateField('dimensions', [
                ...dims,
                { id: newDimensionId(), name: '', values: [], constraints: {} },
              ]);
            }}
          >
            {t('fields.dimension-schema.add-dimension')}
          </button>
        </div>
      </fieldset>
    </div>
  );
}
