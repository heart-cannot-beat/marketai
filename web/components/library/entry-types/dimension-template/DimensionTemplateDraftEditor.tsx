import { useTranslation } from 'next-i18next';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type {
  DimensionTemplateDimension,
  DimensionTemplateFields,
  DimensionTemplateValue,
} from '@/lib/library/draft-codecs';

interface DimensionTemplateDraftEditorProps {
  session: UseDraftSessionResult;
}

function newDimensionId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `dim_${Math.random().toString(36).slice(2)}`;
}

/** Prompt fragments are typed one per line; blank lines are dropped on blur
 * (while typing, a trailing blank line is kept so Enter still works). */
function parseFragments(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

function emptyValue(): DimensionTemplateValue {
  return { id: '', positive_fragments: [], negative_fragments: [] };
}

/**
 * Edits `dimension_template` content in the shape
 * `app/library/contracts.py::_validate_dimension_template` accepts:
 * every dimension carries a stable `id`, a `name`, and value objects whose
 * `id` is both the stable identity and the label the Generation preview
 * shows for that value, plus `positive_fragments` / `negative_fragments`
 * (prompt fragments). `constraints` are preserved by the codec and edited
 * through the raw JSON editor; `combination_cardinality` has exactly one
 * accepted value today, so it is shown but not editable.
 */
export function DimensionTemplateDraftEditor({
  session,
}: DimensionTemplateDraftEditorProps) {
  const { t } = useTranslation('library');
  const fields = session.buffer?.fields as DimensionTemplateFields | undefined;
  if (!fields) return null;

  const dims = fields.dimensions ?? [];

  const replaceDimension = (
    index: number,
    next: DimensionTemplateDimension
  ) => {
    const copy = [...dims];
    copy[index] = next;
    session.updateField('dimensions', copy);
  };

  const replaceValue = (
    dimIndex: number,
    valueIndex: number,
    next: DimensionTemplateValue
  ) => {
    const dim = dims[dimIndex];
    const values = [...(dim.values ?? [])];
    values[valueIndex] = next;
    replaceDimension(dimIndex, { ...dim, values });
  };

  return (
    <div className="space-y-4">
      <fieldset>
        <legend className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {t('fields.dimension-template.dimensions')}
        </legend>
        <div className="mt-2 space-y-3">
          {dims.map((dim, i) => (
            <div
              key={dim.id || i}
              className="rounded-lg border border-gray-200 p-3 dark:border-gray-700"
            >
              <div className="flex items-start gap-2">
                <label className="form-control flex-1">
                  <div className="label">
                    <span className="label-text text-xs">
                      {t('fields.dimension-template.dimension-name')}
                    </span>
                  </div>
                  <input
                    type="text"
                    className="input input-bordered input-sm w-full"
                    value={dim.name}
                    onChange={(e) =>
                      replaceDimension(i, { ...dim, name: e.target.value })
                    }
                    autoComplete="off"
                  />
                </label>
                <label className="form-control flex-1">
                  <div className="label">
                    <span className="label-text text-xs">
                      {t('fields.dimension-template.dimension-id')}
                    </span>
                  </div>
                  <input
                    type="text"
                    className="input input-bordered input-sm w-full font-mono"
                    value={dim.id}
                    onChange={(e) =>
                      replaceDimension(i, { ...dim, id: e.target.value })
                    }
                    autoComplete="off"
                  />
                </label>
                <button
                  type="button"
                  className="btn btn-ghost btn-xs text-red-500 mt-9"
                  onClick={() =>
                    session.updateField(
                      'dimensions',
                      dims.filter((_, j) => j !== i)
                    )
                  }
                  aria-label={t('fields.dimension-template.remove-dimension')}
                >
                  ×
                </button>
              </div>

              <div className="mt-3 space-y-2">
                <p className="text-xs font-medium text-gray-700 dark:text-gray-300">
                  {t('fields.dimension-template.dimension-values')}
                </p>
                {(dim.values ?? []).map((value, j) => (
                  <div
                    key={j}
                    className="rounded border border-gray-100 p-2 dark:border-gray-800"
                  >
                    <div className="flex items-center gap-2">
                      <input
                        type="text"
                        className="input input-bordered input-sm flex-1"
                        value={value.id}
                        placeholder={t(
                          'fields.dimension-template.value-id-placeholder'
                        )}
                        onChange={(e) =>
                          replaceValue(i, j, { ...value, id: e.target.value })
                        }
                        aria-label={t('fields.dimension-template.value-id', {
                          index: String(j + 1),
                        })}
                        autoComplete="off"
                      />
                      <button
                        type="button"
                        className="btn btn-ghost btn-xs text-red-500"
                        onClick={() =>
                          replaceDimension(i, {
                            ...dim,
                            values: (dim.values ?? []).filter(
                              (_, k) => k !== j
                            ),
                          })
                        }
                        aria-label={t('fields.dimension-template.remove-value')}
                      >
                        ×
                      </button>
                    </div>
                    <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">
                      <label className="form-control w-full">
                        <div className="label py-0">
                          <span className="label-text text-xs">
                            {t('fields.dimension-template.positive-fragments')}
                          </span>
                        </div>
                        <textarea
                          className="textarea textarea-bordered textarea-sm w-full"
                          rows={2}
                          value={(value.positive_fragments ?? []).join('\n')}
                          onChange={(e) =>
                            replaceValue(i, j, {
                              ...value,
                              positive_fragments: e.target.value.split('\n'),
                            })
                          }
                          onBlur={(e) =>
                            replaceValue(i, j, {
                              ...value,
                              positive_fragments: parseFragments(
                                e.target.value
                              ),
                            })
                          }
                        />
                      </label>
                      <label className="form-control w-full">
                        <div className="label py-0">
                          <span className="label-text text-xs">
                            {t('fields.dimension-template.negative-fragments')}
                          </span>
                        </div>
                        <textarea
                          className="textarea textarea-bordered textarea-sm w-full"
                          rows={2}
                          value={(value.negative_fragments ?? []).join('\n')}
                          onChange={(e) =>
                            replaceValue(i, j, {
                              ...value,
                              negative_fragments: e.target.value.split('\n'),
                            })
                          }
                          onBlur={(e) =>
                            replaceValue(i, j, {
                              ...value,
                              negative_fragments: parseFragments(
                                e.target.value
                              ),
                            })
                          }
                        />
                      </label>
                    </div>
                  </div>
                ))}
                <button
                  type="button"
                  className="btn btn-ghost btn-xs"
                  onClick={() =>
                    replaceDimension(i, {
                      ...dim,
                      values: [...(dim.values ?? []), emptyValue()],
                    })
                  }
                >
                  {t('fields.dimension-template.add-value')}
                </button>
              </div>
            </div>
          ))}
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={() => {
              session.updateField('dimensions', [
                ...dims,
                { id: newDimensionId(), name: '', values: [emptyValue()] },
              ]);
            }}
          >
            {t('fields.dimension-template.add-dimension')}
          </button>
        </div>
      </fieldset>

      <dl className="grid grid-cols-2 gap-2 text-sm">
        <dt className="text-gray-500 dark:text-gray-400">
          {t('fields.dimension-template.combination-cardinality')}
        </dt>
        <dd className="font-mono text-xs text-gray-900 dark:text-gray-100">
          {fields.combination_cardinality}
        </dd>
        <dt className="text-gray-500 dark:text-gray-400">
          {t('fields.dimension-template.constraints')}
        </dt>
        <dd className="text-gray-900 dark:text-gray-100">
          {t('fields.dimension-template.constraints-count', {
            count: (fields.constraints ?? []).length,
          })}
        </dd>
      </dl>
    </div>
  );
}
